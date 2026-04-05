"""
SSH connection management for Vast.ai instances.

This module provides SSHConnection class for managing SSH connections
to remote Vast.ai GPU instances using paramiko. It supports interactive
commands, background process execution, and context manager protocol.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import TYPE_CHECKING

import paramiko
from rich.console import Console

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger("llama_remote_control.ssh")
console = Console()


class SSHConnectionError(Exception):
    """Raised when SSH connection fails."""

    pass


class SSHTimeoutError(Exception):
    """Raised when SSH command times out."""

    pass


class SSHConnection:
    """
    Manages SSH connections to Vast.ai instances.

    This class provides methods for executing commands, running interactive
    sessions, managing background processes, and handling connection lifecycle.

    Attributes:
        host: SSH hostname or IP address
        port: SSH port number
        username: SSH username (default: root)
        key_path: Path to SSH private key file
        connect_timeout: Connection timeout in seconds
        client: Paramiko SSHClient instance
        current_pid: PID of current background process
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str = "root",
        key_path: str | None = None,
        connect_timeout: int = 15,
    ) -> None:
        """
        Initialize SSH connection parameters.

        Args:
            host: SSH hostname or IP address
            port: SSH port number
            username: SSH username (default: root)
            key_path: Path to SSH private key file (optional)
            connect_timeout: Connection timeout in seconds (default: 15)
        """
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self.connect_timeout = connect_timeout
        self.client: paramiko.SSHClient | None = None
        self.current_pid: int | None = None
        self._extra_paths: list[str] = []  # Paths added to PATH during session

    def connect(self) -> None:
        """
        Open SSH connection to the remote host.

        Creates an SSHClient, sets AutoAddPolicy for host keys,
        loads the SSH private key, and establishes the connection.

        Raises:
            SSHConnectionError: If connection fails
        """
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # Load private key
            pkey: paramiko.PKey | None = None
            if self.key_path:
                try:
                    # Try Ed25519 first (modern default)
                    pkey = paramiko.Ed25519Key.from_private_key_file(self.key_path)
                except (paramiko.SSHException, ValueError):
                    try:
                        # Fallback to RSA
                        pkey = paramiko.RSAKey.from_private_key_file(self.key_path)
                    except (paramiko.SSHException, ValueError) as e:
                        logger.warning(
                            f"Could not load SSH key from {self.key_path}: {e}"
                        )
                        pkey = None

            # Connect with or without key
            connect_kwargs: dict = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": self.connect_timeout,
                "allow_agent": True,
                "look_for_keys": True,
            }
            if pkey:
                connect_kwargs["pkey"] = pkey

            self.client.connect(**connect_kwargs)
            logger.info(f"SSH connected to {self.username}@{self.host}:{self.port}")

        except paramiko.AuthenticationException as e:
            raise SSHConnectionError(
                f"Authentication failed for {self.username}@{self.host}:{self.port}"
            ) from e
        except paramiko.SSHException as e:
            raise SSHConnectionError(
                f"SSH connection failed to {self.host}:{self.port}: {e}"
            ) from e
        except TimeoutError as e:
            raise SSHConnectionError(
                f"Connection timeout to {self.host}:{self.port}"
            ) from e
        except Exception as e:
            raise SSHConnectionError(
                f"Failed to connect to {self.host}:{self.port}: {e}"
            ) from e

    def disconnect(self) -> None:
        """
        Close SSH connection if open.

        Sends a signal to any running background process before closing.
        """
        if self.client:
            try:
                # Try to clean up background process
                if self.current_pid:
                    try:
                        self.send_interrupt()
                    except Exception:
                        pass

                self.client.close()
                logger.info(f"SSH disconnected from {self.host}:{self.port}")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.client = None

    def __enter__(self) -> SSHConnection:
        """Context manager entry - connect and return self."""
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context manager exit - disconnect."""
        self.disconnect()

    def is_connected(self) -> bool:
        """
        Check if SSH transport is active.

        Returns:
            True if transport exists and is active, False otherwise
        """
        if not self.client:
            return False
        try:
            transport = self.client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    def exec_command(self, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
        """
        Execute a short-lived command and return results.

        Args:
            cmd: Command to execute
            timeout: Command timeout in seconds (default: 120)

        Returns:
            Tuple of (exit_code, stdout_text, stderr_text)

        Raises:
            SSHConnectionError: If not connected
            SSHTimeoutError: If command times out
        """
        if not self.client or not self.is_connected():
            raise SSHConnectionError("Not connected to SSH server")

        # Wrap with PATH if extra paths were added
        wrapped_cmd = self._wrap_cmd(cmd)

        logger.debug(f"Executing command (timeout={timeout}s): {wrapped_cmd}")

        try:
            stdin, stdout, stderr = self.client.exec_command(wrapped_cmd, timeout=timeout)

            # Wait for command to complete and read output
            exit_code = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")

            logger.debug(f"Command exited with code {exit_code}")
            return exit_code, stdout_text, stderr_text

        except paramiko.SSHException as e:
            raise SSHConnectionError(f"SSH error executing command: {e}") from e
        except TimeoutError as e:
            raise SSHTimeoutError(f"Command timed out after {timeout}s: {cmd}") from e
        except Exception as e:
            raise SSHConnectionError(f"Error executing command: {e}") from e

    def exec_interactive(self, cmd: str) -> int:
        """
        Execute command and stream stdout+stderr to console in real-time.

        Args:
            cmd: Command to execute

        Returns:
            Exit code from the command

        Raises:
            SSHConnectionError: If not connected
        """
        if not self.client or not self.is_connected():
            raise SSHConnectionError("Not connected to SSH server")

        # Wrap with PATH if extra paths were added
        wrapped_cmd = self._wrap_cmd(cmd)

        logger.debug(f"Executing interactive command: {wrapped_cmd}")
        console.print(f"[dim]$ {cmd}[/dim]")

        try:
            # Get PTY for proper TTY behavior and line-buffered output
            stdin, stdout, stderr = self.client.exec_command(wrapped_cmd, get_pty=True)

            # Stream output in real-time
            while not stdout.channel.exit_status_ready():
                if stdout.channel.recv_ready():
                    data = stdout.channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        console.print(data, end="")
                if stderr.channel.recv_ready():
                    data = stderr.channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        console.print(data, end="", style="red")

                time.sleep(0.01)

            # Drain any remaining buffered output after exit
            stdout.channel.settimeout(2.0)
            try:
                while True:
                    data = stdout.channel.recv(4096).decode("utf-8", errors="replace")
                    if not data:
                        break
                    console.print(data, end="")
            except (socket.timeout, paramiko.SSHException):
                pass

            exit_code = stdout.channel.recv_exit_status()
            logger.debug(f"Interactive command exited with code {exit_code}")
            return exit_code

        except paramiko.SSHException as e:
            raise SSHConnectionError(f"SSH error in interactive command: {e}") from e
        except Exception as e:
            raise SSHConnectionError(f"Error in interactive command: {e}") from e

    def exec_background(self, cmd: str) -> int:
        """
        Run command in background using nohup.

        The command output is redirected to /workspace/llama-server.log.
        The PID is stored in self.current_pid.

        Args:
            cmd: Command to run in background

        Returns:
            Process ID of the background process

        Raises:
            SSHConnectionError: If not connected or PID cannot be parsed
        """
        if not self.client or not self.is_connected():
            raise SSHConnectionError("Not connected to SSH server")

        # Wrap with PATH if extra paths were added
        wrapped_cmd = self._wrap_cmd(cmd)

        # Construct nohup command that captures PID
        background_cmd = f"nohup {wrapped_cmd} > /workspace/llama-server.log 2>&1 & echo $!"
        logger.debug(f"Executing background command: {background_cmd}")

        try:
            exit_code, stdout, stderr = self.exec_command(background_cmd)

            if exit_code != 0:
                raise SSHConnectionError(
                    f"Failed to start background process: {stderr}"
                )

            # Parse PID from output
            pid_str = stdout.strip()
            try:
                pid = int(pid_str)
                self.current_pid = pid
                logger.info(f"Background process started with PID {pid}")
                return pid
            except ValueError as e:
                raise SSHConnectionError(
                    f"Could not parse PID from output: {pid_str}"
                ) from e

        except Exception as e:
            if isinstance(e, SSHConnectionError):
                raise
            raise SSHConnectionError(f"Error starting background process: {e}") from e

    def send_interrupt(self) -> None:
        """
        Send SIGINT to the current background process.

        If self.current_pid is set, sends kill -INT to that PID
        and clears the stored PID.
        """
        if not self.current_pid:
            logger.debug("No background process PID to interrupt")
            return

        pid = self.current_pid
        logger.info(f"Sending SIGINT to process {pid}")

        try:
            self.exec_command(f"kill -INT {pid}", timeout=10)
        except Exception as e:
            logger.warning(f"Error sending interrupt to PID {pid}: {e}")
        finally:
            self.current_pid = None

    def kill_server(self) -> None:
        """
        Kill all llama-server processes.

        Runs pkill -f llama-server and clears self.current_pid.
        """
        logger.info("Killing all llama-server processes")

        try:
            self.exec_command("pkill -f llama-server", timeout=10)
        except Exception as e:
            logger.warning(f"Error killing llama-server: {e}")
        finally:
            self.current_pid = None

    def get_working_dir(self) -> str:
        """
        Get the current working directory on the remote host.

        Returns:
            Current working directory path

        Raises:
            SSHConnectionError: If not connected or command fails
        """
        exit_code, stdout, stderr = self.exec_command("pwd")

        if exit_code != 0:
            raise SSHConnectionError(f"Failed to get working directory: {stderr}")

        return stdout.strip()

    def ensure_path(self, path_to_add: str) -> bool:
        """
        Ensure a path is added to the remote PATH in .bashrc and current session.

        This persists across SSH sessions by modifying ~/.bashrc, and also
        tracks the path for current session command execution.

        Args:
            path_to_add: Directory to add to PATH

        Returns:
            True if successful, False otherwise
        """
        if not self.client or not self.is_connected():
            raise SSHConnectionError("Not connected to SSH server")

        # Check if path is already in .bashrc
        grep_cmd = f"grep -q '{path_to_add}' ~/.bashrc 2>/dev/null && echo found || echo not_found"
        exit_code, stdout, _ = self.exec_command(grep_cmd)
        if "not_found" in stdout:
            # Add to .bashrc
            append_cmd = f'echo \'export PATH="{path_to_add}:$PATH"\' >> ~/.bashrc'
            exit_code, _, _ = self.exec_command(append_cmd)
            if exit_code != 0:
                logger.warning(f"Failed to add {path_to_add} to .bashrc")
                return False

        # Track for current session - prepended to every command
        if path_to_add not in self._extra_paths:
            self._extra_paths.append(path_to_add)
            logger.info(f"Added {path_to_add} to session PATH")

        return True

    def _wrap_cmd(self, cmd: str) -> str:
        """
        Prepend extra PATH entries to command if they were added during session.

        Uses `env` to set PATH so it works with nohup/background processes
        (nohup doesn't understand inline VAR=value shell assignments).
        """
        if not self._extra_paths:
            return cmd
        path_prefix = ":".join(self._extra_paths)
        return f'env PATH="{path_prefix}:$PATH" {cmd}'

    @staticmethod
    def resolve_ssh_target(
        instance: dict, prefer_direct: bool = True
    ) -> tuple[str, int]:
        """
        Resolve SSH connection target from instance dictionary.

        Given an instance dict from Vast.ai API, returns the appropriate
        host and port for SSH connection.

        Args:
            instance: Instance dictionary from Vast.ai API
            prefer_direct: If True, prefer direct SSH connection over proxy

        Returns:
            Tuple of (host, port) for SSH connection
        """
        # Try direct connection first if preferred
        if prefer_direct:
            ssh_host = instance.get("ssh_host")
            ssh_port = instance.get("ssh_port")
            if ssh_host and ssh_port:
                logger.debug(f"Using direct SSH: {ssh_host}:{ssh_port}")
                return str(ssh_host), int(ssh_port)

        # Fall back to proxy connection
        proxy_host = instance.get("ssh_proxy_host")
        proxy_port = instance.get("ssh_proxy_port")
        if proxy_host and proxy_port:
            logger.debug(f"Using proxy SSH: {proxy_host}:{proxy_port}")
            return str(proxy_host), int(proxy_port)

        # If no direct but we didn't prefer it, try direct now
        if not prefer_direct:
            ssh_host = instance.get("ssh_host")
            ssh_port = instance.get("ssh_port")
            if ssh_host and ssh_port:
                logger.debug(f"Using direct SSH (fallback): {ssh_host}:{ssh_port}")
                return str(ssh_host), int(ssh_port)

        # Last resort - try hostname and default SSH port
        hostname = instance.get("hostname", instance.get("public_ip", "unknown"))
        logger.debug(f"Using hostname fallback: {hostname}:22")
        return str(hostname), 22
