"""
SSH Port Forwarding Tunnel Manager

This module manages local SSH port forwarding tunnels using paramiko's transport,
threading, and sockets. It allows creating, managing, and testing SSH tunnels
for forwarding local ports to remote destinations through an SSH connection.

Example:
    >>> from llama_remote_control.ssh import SSHConnection
    >>> from llama_remote_control.tunnel import TunnelManager
    >>> ssh = SSHConnection()
    >>> ssh.connect('example.com', username='user', password='pass')
    >>> tunnel_mgr = TunnelManager(ssh)
    >>> tunnel_mgr.create_tunnel(local_port=8080, remote_port=80)
    True
    >>> tunnel_mgr.test_tunnel(8080)
    True
    >>> tunnel_mgr.close_all()
"""

from __future__ import annotations

import logging
import select
import socket
import threading
from typing import TYPE_CHECKING

import httpx
import paramiko

if TYPE_CHECKING:
    from llama_remote_control.ssh import SSHConnection

logger = logging.getLogger("llama_remote_control.tunnel")


class _TunnelThread(threading.Thread):
    """
    Internal thread class that manages a single SSH port forwarding tunnel.

    This thread listens on a local port and forwards connections through
    an SSH channel to a remote host:port combination.
    """

    def __init__(
        self,
        transport: paramiko.Transport,
        local_port: int,
        remote_host: str,
        remote_port: int,
    ) -> None:
        """
        Initialize the tunnel thread.

        Args:
            transport: The paramiko Transport object for creating SSH channels
            local_port: The local port to bind and listen on
            remote_host: The remote host to forward connections to
            remote_port: The remote port to forward connections to
        """
        super().__init__(daemon=True)
        self.transport = transport
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._stop_event = threading.Event()
        self._server_socket: socket.socket | None = None

    def _shuttle_data(
        self, client_sock: socket.socket, channel: paramiko.Channel
    ) -> None:
        """
        Shuttle data between client socket and SSH channel.

        This method runs in a separate thread and continuously transfers data
        between the local client socket and the remote SSH channel until
        either side closes the connection or an error occurs.

        Args:
            client_sock: The local client socket
            channel: The paramiko channel for SSH communication
        """
        try:
            while not self._stop_event.is_set() and channel.active:
                readable, _, _ = select.select([client_sock, channel], [], [], 1.0)
                if not readable:
                    continue

                for ready in readable:
                    if ready is client_sock:
                        data = client_sock.recv(4096)
                        if not data:
                            return
                        channel.send(data)
                    elif ready is channel:
                        data = channel.recv(4096)
                        if not data:
                            return
                        client_sock.send(data)
        except (socket.error, paramiko.SSHException) as e:
            logger.debug(f"Tunnel shuttle error on port {self.local_port}: {e}")
        finally:
            try:
                channel.close()
            except Exception:
                pass
            try:
                client_sock.close()
            except Exception:
                pass

    def run(self) -> None:
        """
        Main thread execution loop.

        Creates a server socket on the local port and accepts incoming
        connections, forwarding them through the SSH transport.
        """
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind(("127.0.0.1", self.local_port))
            self._server_socket.listen(5)
            self._server_socket.settimeout(1.0)
            logger.info(
                f"Tunnel started: 127.0.0.1:{self.local_port} -> "
                f"{self.remote_host}:{self.remote_port}"
            )

            while not self._stop_event.is_set():
                try:
                    client_sock, client_addr = self._server_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    # Socket was closed
                    break

                try:
                    channel = self.transport.open_channel(
                        "direct-tcpip",
                        (self.remote_host, self.remote_port),
                        client_addr,
                    )
                    if channel is None:
                        logger.warning(
                            f"Failed to open channel for tunnel on port {self.local_port}"
                        )
                        client_sock.close()
                        continue

                    # Spawn shuttle thread for this connection
                    shuttle_thread = threading.Thread(
                        target=self._shuttle_data,
                        args=(client_sock, channel),
                        daemon=True,
                    )
                    shuttle_thread.start()

                except Exception as e:
                    logger.error(f"Error handling tunnel connection: {e}")
                    try:
                        client_sock.close()
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Tunnel thread error on port {self.local_port}: {e}")
        finally:
            if self._server_socket:
                try:
                    self._server_socket.close()
                except Exception:
                    pass
            logger.info(f"Tunnel stopped on port {self.local_port}")

    def stop(self) -> None:
        """Signal the tunnel thread to stop running."""
        self._stop_event.set()
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass


class TunnelManager:
    """
    Manages multiple SSH port forwarding tunnels.

    This class provides methods to create, monitor, and close SSH tunnels
    for forwarding local ports to remote destinations through an SSH connection.
    """

    def __init__(self, ssh_connection: "SSHConnection") -> None:
        """
        Initialize the tunnel manager.

        Args:
            ssh_connection: An established SSHConnection instance
        """
        self.ssh = ssh_connection
        self.active_tunnels: dict[int, dict] = {}
        logger.debug("TunnelManager initialized")

    def _is_port_available(self, port: int) -> bool:
        """
        Check if a local port is available for binding.

        Args:
            port: The port number to check

        Returns:
            True if the port is available, False otherwise
        """
        try:
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_socket.bind(("127.0.0.1", port))
            test_socket.close()
            return True
        except (socket.error, OSError):
            return False

    def create_tunnel(
        self, local_port: int, remote_port: int, remote_host: str = "localhost"
    ) -> bool:
        """
        Create a new SSH port forwarding tunnel.

        Args:
            local_port: The local port to bind and listen on
            remote_port: The remote port to forward connections to
            remote_host: The remote host to forward connections to (default: localhost)

        Returns:
            True if tunnel was created successfully, False otherwise
        """
        # Check if local port is available
        if not self._is_port_available(local_port):
            logger.warning(f"Local port {local_port} is already in use")
            return False

        # Get paramiko transport
        transport = self.ssh.client.get_transport()
        if transport is None:
            logger.error("No SSH transport available")
            return False

        # Check if tunnel already exists
        if local_port in self.active_tunnels:
            logger.warning(f"Tunnel on port {local_port} already exists")
            return False

        # Create and start tunnel thread
        tunnel_thread = _TunnelThread(transport, local_port, remote_host, remote_port)
        tunnel_thread.start()

        # Store tunnel info
        self.active_tunnels[local_port] = {
            "local_port": local_port,
            "remote_port": remote_port,
            "remote_host": remote_host,
            "thread": tunnel_thread,
        }

        logger.info(
            f"Created tunnel: local:{local_port} -> {remote_host}:{remote_port}"
        )
        return True

    def close_tunnel(self, local_port: int) -> bool:
        """
        Close a specific tunnel by local port.

        Args:
            local_port: The local port of the tunnel to close

        Returns:
            True if tunnel existed and was closed, False otherwise
        """
        if local_port not in self.active_tunnels:
            logger.debug(f"No active tunnel on port {local_port}")
            return False

        tunnel_info = self.active_tunnels[local_port]
        thread: _TunnelThread = tunnel_info["thread"]

        # Signal thread to stop
        thread.stop()

        # Wait briefly for thread to finish
        thread.join(timeout=1.0)

        # Remove from active tunnels
        del self.active_tunnels[local_port]

        logger.info(f"Closed tunnel on port {local_port}")
        return True

    def close_all(self) -> None:
        """Close all active tunnels."""
        ports = list(self.active_tunnels.keys())
        for local_port in ports:
            self.close_tunnel(local_port)
        logger.info("All tunnels closed")

    def list_tunnels(self) -> list[dict]:
        """
        Get a list of all active tunnel information.

        Returns:
            List of dictionaries containing tunnel details
        """
        return [
            {
                "local_port": info["local_port"],
                "remote_port": info["remote_port"],
                "remote_host": info["remote_host"],
            }
            for info in self.active_tunnels.values()
        ]

    def test_tunnel(self, local_port: int, timeout: int = 5) -> bool:
        """
        Test if a tunnel is working by making an HTTP GET request.

        Args:
            local_port: The local port of the tunnel to test
            timeout: Request timeout in seconds (default: 5)

        Returns:
            True if any response received (even non-2xx), False on connection error
        """
        try:
            response = httpx.get(
                f"http://127.0.0.1:{local_port}/health",
                timeout=timeout,
                follow_redirects=True,
            )
            # Return True if we got any response (even non-2xx)
            logger.debug(
                f"Tunnel test on port {local_port}: status {response.status_code}"
            )
            return True
        except httpx.RequestError as e:
            logger.debug(f"Tunnel test failed on port {local_port}: {e}")
            return False
        except Exception as e:
            logger.debug(f"Tunnel test error on port {local_port}: {e}")
            return False
