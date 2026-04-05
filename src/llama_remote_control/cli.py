"""
PURPOSE: Main CLI orchestrator for llama-cli.
         Provides the interactive REPL shell, instance selection,
         slash commands, and the setup wizard entry point.

KEY DECISIONS:
- Uses prompt_toolkit for the REPL (history, completion, Ctrl+C handling)
- Slash commands start with '/' and are handled locally (never sent to remote)
- Regular input is executed as SSH commands on the remote instance
- Ctrl+C during command execution sends SIGINT to remote process
- Ctrl+C at the prompt does nothing (no accidental exit)
- Ctrl+D exits the CLI

GOTCHAS:
- prompt_toolkit's PromptSession runs its own event loop — don't mix with asyncio
- Rich Console.print() and prompt_toolkit can conflict for terminal control
  We use prompt_toolkit's print_formatted_text for prompt output and
  Rich Console only for structured output (tables, panels, status)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

import questionary
import typer
from alive_progress import alive_bar
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from llama_remote_control import config, setup, ssh as ssh_module, tunnel, vastai
from llama_remote_control.theme import Theme, format_instance_choice

logger = logging.getLogger("llama_remote_control.cli")

HISTORY_PATH = Path.home() / ".llama-cli-history"

# All slash commands
SLASH_COMMANDS = [
    "/tunnel",
    "/tunnels",
    "/close",
    "/bg-proc",
    "/bg-list",
    "/bg-attach",
    "/bg-stop",
    "/kill",
    "/status",
    "/monitor",
    "/start",
    "/models",
    "/build",
    "/download",
    "/logs",
    "/shell",
    "/switch",
    "/test",
    "/clear",
    "/help",
    "/exit",
]


class LlamaCLI:
    """Main CLI application state and REPL loop."""

    def __init__(self) -> None:
        self.console = Console()
        self.cfg = config.load_config()
        self.api_key: str = ""
        self.ssh_key_path: str = ""
        self.ssh: ssh_module.SSHConnection | None = None
        self.tunnel_mgr: tunnel.TunnelManager | None = None
        self.instance: dict | None = None
        self.instances: list[dict] = []
        self.cwd: str = "/workspace"
        self._command_running = False  # True while a remote command is executing
        self.session: PromptSession | None = None
        self.bg_procs: dict[int, dict] = {}  # PID -> process info

    # ── Instance selection ──────────────────────────────────────────────

    def select_instance(self) -> bool:
        """Fetch instances from Vast.ai and let the user pick one."""
        try:
            self.api_key = config.get_api_key(self.cfg)
        except RuntimeError as e:
            self.console.print(f"[{Theme.ERROR}]{e}[/{Theme.ERROR}]")
            self.console.print(
                f"[{Theme.DIM}]Run: set VASTAI_API_KEY=<your_key>  or  edit ~/.llama-cli.json[/{Theme.DIM}]"
            )
            return False

        try:
            self.ssh_key_path = config.get_ssh_key_path(self.cfg)
        except FileNotFoundError as e:
            self.console.print(f"[{Theme.ERROR}]{e}[/{Theme.ERROR}]")
            return False

        # Fetch instances with spinner
        with alive_bar(
            title="Fetching instances from Vast.ai...",
            spinner="dots",
            length=30,
            manual=True,
        ) as bar:
            bar(0)
            try:
                self.instances = vastai.fetch_instances(self.api_key)
            except (ValueError, ConnectionError) as e:
                self.console.print(f"[{Theme.ERROR}]{e}[/{Theme.ERROR}]")
                return False
            bar(1.0)

        if not self.instances:
            self.console.print(
                f"[{Theme.WARNING}]No instances found.[/{Theme.WARNING}]"
            )
            return False

        # Show full table
        table = vastai.format_instance_table(self.instances)
        self.console.print(table)

        # Only show running instances as options
        running = [i for i in self.instances if i["status"] == "running"]
        if not running:
            self.console.print(
                f"[{Theme.WARNING}]No running instances. Start one on vast.ai first.[/{Theme.WARNING}]"
            )
            return False

        # Auto-select if only one running instance
        if len(running) == 1:
            self.instance = running[0]
            self.console.print(
                f"[{Theme.DIM}]Auto-selected only running instance: {self.instance['id']}[/{Theme.DIM}]"
            )
        else:
            # Prompt for selection with questionary
            choices = []
            for inst in running:
                display = format_instance_choice(inst)
                choices.append(questionary.Choice(title=display, value=inst))

            choice = questionary.select(
                "Select instance to connect:",
                choices=choices,
                style=questionary.Style(
                    [
                        ("selected", "fg:#00ff00 bold"),
                        ("highlighted", "fg:#00ffff bold"),
                        ("answer", "fg:#00ffff bold"),
                        ("pointer", "fg:#00ff00 bold"),
                    ]
                ),
            ).ask()

            if choice is None:  # User pressed Ctrl+C
                return False

            self.instance = choice

        return True

    # ── Connection ──────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to the selected instance via SSH."""
        if not self.instance:
            return False

        host, port = ssh_module.SSHConnection.resolve_ssh_target(self.instance)
        instance_id = self.instance["id"]

        # Connection with spinner
        with alive_bar(
            title=f"Connecting to {instance_id} ({host}:{port})...",
            spinner="pulse",
            length=40,
        ) as bar:
            self.ssh = ssh_module.SSHConnection(
                host=host,
                port=port,
                username="root",
                key_path=self.ssh_key_path,
            )

            try:
                self.ssh.connect()
                self.tunnel_mgr = tunnel.TunnelManager(self.ssh)
                self.cwd = self.ssh.get_working_dir()

                # Auto-detect and register llama.cpp PATH if already built
                code, out, _ = self.ssh.exec_command(
                    "test -d /workspace/llama.cpp/build/bin && echo yes || echo no",
                    timeout=5,
                )
                if out.strip() == "yes":
                    self.ssh.ensure_path("/workspace/llama.cpp/build/bin")
            except ssh_module.SSHConnectionError as e:
                self.console.print(
                    f"[{Theme.ERROR}]Connection failed: {e}[/{Theme.ERROR}]"
                )
                self.ssh = None
                return False

        # Save last instance
        self.cfg = config.set_last_instance(self.cfg, self.instance["id"])
        config.save_config(self.cfg)
        return True

    # ── Setup flow ──────────────────────────────────────────────────────

    def run_setup(self) -> bool:
        """Run the interactive setup wizard (build + download)."""
        if not self.ssh:
            self.console.print("[red]Not connected.[/red]")
            return False

        result = setup.run_setup_wizard(self.console, self.ssh)
        return result is not None

    # ── Shell prompt ────────────────────────────────────────────────────

    def _get_prompt_text(self) -> str:
        """Build the shell prompt string."""
        if self.instance:
            gpu = (
                self.instance.get("gpu_name", "?")
                .replace("NVIDIA GeForce ", "")
                .replace("NVIDIA ", "")
            )
            return f"root@{self.instance['id']} [{gpu}] {self.cwd}> "
        return "llama> "

    # ── Remote state detection ──────────────────────────────────────────

    def detect_remote_state(self) -> dict:
        """
        Detect what's already set up on the remote instance.

        Returns a dict with keys:
        - llama_built: bool (llama.cpp binary exists)
        - llama_version: str (version tag if built)
        - llama_server_path: str (full path to llama-server binary)
        - models: list of model dicts (each has 'path' with full path)
        - server_running: bool
        - server_pid: int or None
        - server_port: int or None
        - server_model: str or None (model file being served)
        - free_port: int (an available port for starting a new server)
        """
        state = {
            "llama_built": False,
            "llama_version": "",
            "llama_server_path": "",
            "models": [],
            "server_running": False,
            "server_pid": None,
            "server_port": None,
            "server_model": None,
            "free_port": 8080,
        }

        if not self.ssh:
            return state

        # Check if llama.cpp binary exists and get full path
        code, out, _ = self.ssh.exec_command(
            "test -f /workspace/llama.cpp/build/bin/llama-server && echo yes || echo no",
            timeout=5,
        )
        state["llama_built"] = out.strip() == "yes"
        if state["llama_built"]:
            state["llama_server_path"] = "/workspace/llama.cpp/build/bin/llama-server"

        # Get llama version
        if state["llama_built"]:
            code, out, _ = self.ssh.exec_command(
                "cd /workspace/llama.cpp && git describe --tags --always 2>/dev/null || echo unknown",
                timeout=5,
            )
            state["llama_version"] = out.strip()

        # List models (returns full paths in m["path"])
        state["models"] = setup.list_models(self.ssh)

        # Check if server is running
        code, out, _ = self.ssh.exec_command(
            "ps aux | grep '[l]lama-server'",
            timeout=5,
        )
        if out.strip():
            state["server_running"] = True
            # Extract PID
            parts = out.strip().split()
            if len(parts) >= 2:
                try:
                    state["server_pid"] = int(parts[1])
                except ValueError:
                    pass

            # Extract port and model from command line
            cmd_line = out.strip()
            for part in cmd_line.split():
                if part.startswith("--port"):
                    port_val = part.replace("--port", "").strip("=")
                    if not port_val:
                        # --port 8080 form
                        idx = cmd_line.split().index(part)
                        if idx + 1 < len(cmd_line.split()):
                            port_val = cmd_line.split()[idx + 1]
                    try:
                        state["server_port"] = int(port_val)
                    except ValueError:
                        pass
                elif part.startswith("-m ") or part.startswith("--model "):
                    model_path = part.split(" ", 1)[-1]
                    state["server_model"] = model_path.split("/")[-1]
                elif part == "-m" or part == "--model":
                    # -m model.gguf form
                    idx = cmd_line.split().index(part)
                    if idx + 1 < len(cmd_line.split()):
                        model_path = cmd_line.split()[idx + 1]
                        state["server_model"] = model_path.split("/")[-1]

        # Find a free port for starting a new server
        state["free_port"] = self._find_free_port(state.get("server_port"))

        return state

    def _find_free_port(self, exclude_port: int | None = None) -> int:
        """
        Find a free port on the remote instance for llama-server.

        Checks common ports (8080, 8081, 8000, 8001) first, then falls back
        to letting the OS pick a free port. Skips the given exclude_port.

        Args:
            exclude_port: Port to skip (e.g. current server's port).

        Returns:
            A free port number.
        """
        if not self.ssh:
            return 8080

        # Common ports to try, in preference order
        # 8080 is Jupyter's default on Vast.ai, so we try alternatives first
        preferred = [8081, 8000, 8080, 8001, 5000, 3000]
        if exclude_port:
            preferred = [p for p in preferred if p != exclude_port]

        for port in preferred:
            code, out, _ = self.ssh.exec_command(
                f"ss -tlnp | grep -q ':{port} ' && echo busy || echo free",
                timeout=5,
            )
            if "free" in out:
                return port

        # All preferred ports busy — ask the OS for a random free port
        code, out, _ = self.ssh.exec_command(
            "python3 -c \"import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()\"",
            timeout=5,
        )
        if code == 0 and out.strip():
            try:
                return int(out.strip())
            except ValueError:
                pass

        return 8080  # Last resort fallback

    # ── Command execution ───────────────────────────────────────────────

    def _exec_remote(self, cmd: str) -> None:
        """Execute a command on the remote instance."""
        if not self.ssh:
            self.console.print("[red]Not connected.[/red]")
            return

        self._command_running = True

        # Detect llama-server commands — run in background, stream log
        if "llama-server" in cmd and ("-m " in cmd or "--model " in cmd):
            self._exec_server(cmd)
        else:
            # Regular command — run interactively, stream output
            try:
                exit_code = self.ssh.exec_interactive(cmd)
                if exit_code != 0:
                    self.console.print(f"[dim]exit code: {exit_code}[/dim]")
                # Update cwd after cd commands
                if cmd.strip().startswith("cd "):
                    self.cwd = self.ssh.get_working_dir()
            except ssh_module.SSHConnectionError as e:
                self.console.print(f"[red]{e}[/red]")

        self._command_running = False

    def _exec_server(self, cmd: str) -> None:
        """Run llama-server in background and stream its log."""
        if not self.ssh:
            return

        self.console.print("[cyan]Starting llama-server in background...[/cyan]")
        self.console.print("[dim]Press Ctrl+C to stop the server.[/dim]")

        # Kill any existing server first
        self.ssh.kill_server()

        # Start the server in background
        try:
            pid = self.ssh.exec_background(cmd)
            self.console.print(f"[green]Server started (PID {pid})[/green]")
        except ssh_module.SSHConnectionError as e:
            self.console.print(f"[red]Failed to start server: {e}[/red]")
            return

        # Stream the log file
        try:
            import time as _time

            log_file = "/workspace/llama-server.log"
            self.console.print("[dim]--- server log ---[/dim]")

            while True:
                try:
                    exit_code, output, _ = self.ssh.exec_command(
                        f"tail -20 {log_file}", timeout=5
                    )
                    if exit_code == 0 and output.strip():
                        self.console.print(output, end="")

                    # Check if server is still running
                    _, check, _ = self.ssh.exec_command(
                        f"kill -0 {pid} 2>/dev/null && echo running || echo stopped",
                        timeout=5,
                    )
                    if "stopped" in check:
                        self.console.print("[yellow]Server process exited.[/yellow]")
                        break

                    _time.sleep(2)
                except KeyboardInterrupt:
                    # Ctrl+C pressed — kill the server
                    self.console.print("\n[yellow]Stopping server...[/yellow]")
                    self.ssh.kill_server()
                    self.console.print("[green]Server stopped.[/green]")
                    break
                except ssh_module.SSHTimeoutError:
                    continue
                except ssh_module.SSHConnectionError:
                    self.console.print("[red]Connection lost.[/red]")
                    break

            self.console.print("[dim]--- end log ---[/dim]")
        except KeyboardInterrupt:
            self.ssh.kill_server()
            self.console.print("[yellow]Server stopped.[/yellow]")

    # ── Slash commands ──────────────────────────────────────────────────

    def _handle_slash(self, line: str) -> bool:
        """Handle a slash command. Returns True if the command was handled."""

        parts = line.strip().split(maxsplit=2)
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        if cmd == "/tunnel":
            return self._cmd_tunnel(args)
        elif cmd == "/tunnels":
            return self._cmd_tunnels()
        elif cmd == "/close":
            return self._cmd_close(args)
        elif cmd == "/bg-proc":
            return self._cmd_bg_proc(args)
        elif cmd == "/bg-list":
            return self._cmd_bg_list()
        elif cmd == "/bg-attach":
            return self._cmd_bg_attach(args)
        elif cmd == "/bg-stop":
            return self._cmd_bg_stop(args)
        elif cmd == "/kill":
            return self._cmd_kill()
        elif cmd == "/status":
            return self._cmd_status()
        elif cmd == "/monitor":
            return self._cmd_monitor()
        elif cmd == "/start":
            return self._cmd_start(args)
        elif cmd == "/models":
            return self._cmd_models()
        elif cmd == "/build":
            return self._cmd_build(args)
        elif cmd == "/download":
            return self._cmd_download(args)
        elif cmd == "/logs":
            return self._cmd_logs(args)
        elif cmd == "/shell":
            return self._cmd_shell()
        elif cmd == "/switch":
            return self._cmd_switch()
        elif cmd == "/test":
            return self._cmd_test()
        elif cmd == "/clear":
            self.console.clear()
            return True
        elif cmd in ("/help", "/?"):
            return self._cmd_help()
        elif cmd == "/exit" or cmd == "/quit":
            return self._cmd_exit()
        else:
            self.console.print(f"[red]Unknown command: {cmd}[/red]")
            self.console.print("[dim]Type /help for available commands.[/dim]")
            return True

    def _cmd_port(self, args: list[str]) -> bool:
        """Create SSH tunnel: /port <local> <remote>"""
        if not self.tunnel_mgr:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        if len(args) < 2:
            self.console.print(
                f"[{Theme.ERROR}]Usage: /port <local_port> <remote_port>[/{Theme.ERROR}]"
            )
            self.console.print(f"[{Theme.DIM}]Example: /port 8000 8081[/{Theme.DIM}]")
            return True

        try:
            local_port = int(args[0])
            remote_port = int(args[1])
        except ValueError:
            self.console.print(f"[{Theme.ERROR}]Ports must be numbers.[/{Theme.ERROR}]")
            return True

        self.console.print(
            f"Creating tunnel: [{Theme.INFO}]localhost:{local_port}[/{Theme.INFO}] -> "
            f"[{Theme.INFO}]remote:{remote_port}[/{Theme.INFO}]"
        )

        ok = self.tunnel_mgr.create_tunnel(local_port, remote_port)
        if ok:
            self.console.print(
                f"[{Theme.SUCCESS}]Tunnel active: http://localhost:{local_port}[/{Theme.SUCCESS}]"
            )
        else:
            self.console.print(
                f"[{Theme.ERROR}]Failed to create tunnel. Port {local_port} may be in use.[/{Theme.ERROR}]"
            )
        return True

    def _cmd_tunnel(self, args: list[str]) -> bool:
        """
        Create SSH tunnel: /tunnel <remote_port> [local_port]

        Shortcut for /port but with remote-first argument order.
        /tunnel 8080          -> tunnel localhost:8080 -> remote:8080
        /tunnel 8080 9000    -> tunnel localhost:9000 -> remote:8080

        This lets you run llama-server manually on the remote (e.g. on port
        8080) and then connect to it locally via localhost:8080.
        """
        if not self.tunnel_mgr:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        if len(args) < 1:
            self.console.print(
                f"[{Theme.ERROR}]Usage: /tunnel <remote_port> [local_port][/{Theme.ERROR}]"
            )
            self.console.print(
                f"[{Theme.DIM}]Example: /tunnel 8080       (local 8080 -> remote 8080)[/{Theme.DIM}]"
            )
            self.console.print(
                f"[{Theme.DIM}]Example: /tunnel 8080 9000 (local 9000 -> remote 8080)[/{Theme.DIM}]"
            )
            return True

        try:
            remote_port = int(args[0])
        except ValueError:
            self.console.print(
                f"[{Theme.ERROR}]remote_port must be a number.[/{Theme.ERROR}]"
            )
            return True

        local_port = remote_port  # Default: same port on both sides
        if len(args) >= 2:
            try:
                local_port = int(args[1])
            except ValueError:
                self.console.print(
                    f"[{Theme.ERROR}]local_port must be a number.[/{Theme.ERROR}]"
                )
                return True

        self.console.print(
            f"Creating tunnel: [{Theme.INFO}]localhost:{local_port}[/{Theme.INFO}] -> "
            f"[{Theme.INFO}]remote:{remote_port}[/{Theme.INFO}]"
        )

        ok = self.tunnel_mgr.create_tunnel(local_port, remote_port)
        if ok:
            self.console.print(
                f"[{Theme.SUCCESS}]Tunnel active: http://localhost:{local_port}[/{Theme.SUCCESS}]"
            )
            self.console.print(
                f"[{Theme.DIM}]Run your server manually on remote (e.g. llama-server -m model.gguf --port {remote_port}), then hit localhost:{local_port} locally.[/{Theme.DIM}]"
            )
        else:
            self.console.print(
                f"[{Theme.ERROR}]Failed to create tunnel. Port {local_port} may already be in use.[/{Theme.ERROR}]"
            )
        return True

    def _cmd_tunnels(self) -> bool:
        """List active tunnels."""
        if not self.tunnel_mgr:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        tunnels = self.tunnel_mgr.list_tunnels()
        if not tunnels:
            self.console.print(f"[{Theme.DIM}]No active tunnels.[/{Theme.DIM}]")
            return True

        table = Table(title="Active Tunnels")
        table.add_column("Local", style=Theme.COL_ID)
        table.add_column("->", style=Theme.DIM)
        table.add_column("Remote", style=Theme.COL_ID)
        for t in tunnels:
            table.add_row(
                f"localhost:{t['local_port']}",
                "->",
                f"{t['remote_host']}:{t['remote_port']}",
            )
        self.console.print(table)
        return True

    def _cmd_close(self, args: list[str]) -> bool:
        """
        Close a tunnel by local port: /close <local_port>
        Or close all tunnels: /close --all
        """
        if not self.tunnel_mgr:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        tunnels = self.tunnel_mgr.list_tunnels()
        if not tunnels:
            self.console.print(
                f"[{Theme.DIM}]No active tunnels to close.[/{Theme.DIM}]"
            )
            return True

        if not args:
            # Show usage with current tunnels
            ports = ", ".join(str(t["local_port"]) for t in tunnels)
            self.console.print(
                f"[{Theme.ERROR}]Usage: /close <local_port>  (active tunnels on ports: {ports})[/{Theme.ERROR}]"
            )
            self.console.print(
                f"[{Theme.DIM}]Or: /close --all to close all tunnels[/{Theme.DIM}]"
            )
            return True

        arg = args[0]

        if arg == "--all":
            self.tunnel_mgr.close_all()
            self.console.print(
                f"[{Theme.SUCCESS}]All tunnels closed.[/{Theme.SUCCESS}]"
            )
            return True

        # Close specific port
        try:
            local_port = int(arg)
        except ValueError:
            self.console.print(
                f"[{Theme.ERROR}]Port must be a number or --all.[/{Theme.ERROR}]"
            )
            return True

        if self.tunnel_mgr.close_tunnel(local_port):
            self.console.print(
                f"[{Theme.SUCCESS}]Tunnel on localhost:{local_port} closed.[/{Theme.SUCCESS}]"
            )
        else:
            self.console.print(
                f"[{Theme.ERROR}]No active tunnel on port {local_port}.[/{Theme.ERROR}]"
            )
        return True

    def _cmd_bg_proc(self, args: list[str]) -> bool:
        """
        Start a background process: /bg-proc <command>

        Like tmux but simpler - runs any command in background with nohup.
        Output is saved to a log file you can attach to later.

        Examples:
            /bg-proc llama-server -m model.gguf --port 8080
            /bg-proc python train.py
            /bg-proc ./some-script.sh
        """
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        if not args:
            self.console.print(
                f"[{Theme.ERROR}]Usage: /bg-proc <command> [args...][/{Theme.ERROR}]"
            )
            self.console.print(
                f"[{Theme.DIM}]Example: /bg-proc llama-server -m model.gguf --port 8080[/{Theme.DIM}]"
            )
            return True

        # Reconstruct the full command
        full_cmd = " ".join(args)

        # Create a unique log file for this process
        import time

        timestamp = int(time.time())
        log_file = f"/workspace/.bg-{timestamp}.log"

        # Wrap with nohup and redirect output
        # Use setsid to create a new session so Ctrl+C doesn't kill it
        bg_cmd = f"cd {self.cwd} && nohup {full_cmd} > {log_file} 2>&1 & echo $!"

        self.console.print(
            f"[{Theme.ANNOUNCE}]Starting background process...[/{Theme.ANNOUNCE}]"
        )
        self.console.print(f"[{Theme.DIM}]Command: {full_cmd}[/{Theme.DIM}]")

        try:
            exit_code, stdout, stderr = self.ssh.exec_command(bg_cmd, timeout=10)
            if exit_code != 0:
                self.console.print(
                    f"[{Theme.ERROR}]Failed to start: {stderr}[/{Theme.ERROR}]"
                )
                return True

            pid_str = stdout.strip()
            try:
                pid = int(pid_str)
            except ValueError:
                self.console.print(
                    f"[{Theme.ERROR}]Could not parse PID: {pid_str}[/{Theme.ERROR}]"
                )
                return True

            # Store process info
            self.bg_procs[pid] = {
                "pid": pid,
                "command": full_cmd,
                "log_file": log_file,
                "start_time": timestamp,
            }

            self.console.print(
                f"[{Theme.SUCCESS}]Started (PID {pid})[/{Theme.SUCCESS}]"
            )
            self.console.print(f"[{Theme.DIM}]Log: {log_file}[/{Theme.DIM}]")
            self.console.print(
                f"[{Theme.DIM}]Use /bg-list to see all processes, /bg-attach {pid} to view output[/{Theme.DIM}]"
            )

        except Exception as e:
            self.console.print(f"[{Theme.ERROR}]Error: {e}[/{Theme.ERROR}]")

        return True

    def _cmd_bg_list(self) -> bool:
        """List all background processes started via /bg-proc."""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        if not self.bg_procs:
            self.console.print(
                f"[{Theme.DIM}]No background processes started.[/{Theme.DIM}]"
            )
            return True

        # Check which processes are still running
        active = []
        dead = []
        for pid, info in self.bg_procs.items():
            try:
                code, out, _ = self.ssh.exec_command(
                    f"kill -0 {pid} 2>/dev/null && echo running || echo dead", timeout=5
                )
                if "running" in out:
                    active.append(info)
                else:
                    dead.append(pid)
            except Exception:
                dead.append(pid)

        # Clean up dead processes from our tracking
        for pid in dead:
            del self.bg_procs[pid]

        if not self.bg_procs:
            self.console.print(
                f"[{Theme.DIM}]No active background processes.[/{Theme.DIM}]"
            )
            return True

        # Show active processes
        table = Table(title="Background Processes")
        table.add_column("PID", style=Theme.COL_ID, no_wrap=True)
        table.add_column("Command", style=Theme.COL_PATH)
        table.add_column("Log File", style=Theme.DIM)

        for info in active:
            cmd_short = (
                info["command"][:50] + "..."
                if len(info["command"]) > 50
                else info["command"]
            )
            table.add_row(
                str(info["pid"]),
                cmd_short,
                info["log_file"],
            )

        self.console.print(table)
        self.console.print()
        self.console.print(f"[{Theme.DIM}]Commands:[/{Theme.DIM}]")
        self.console.print(
            f"  /bg-attach <pid>  - View process output (Ctrl+C to detach)"
        )
        self.console.print(f"  /bg-stop <pid>    - Stop a process")
        return True

    def _cmd_bg_attach(self, args: list[str]) -> bool:
        """
        Attach to a background process's output: /bg-attach <pid>

        Streams the log file live (like tail -f). Press Ctrl+C to detach
        without stopping the process.
        """
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        if not args:
            # Show list if no PID provided
            self.console.print(
                f"[{Theme.ERROR}]Usage: /bg-attach <pid>[/{Theme.ERROR}]"
            )
            self._cmd_bg_list()
            return True

        try:
            pid = int(args[0])
        except ValueError:
            self.console.print(f"[{Theme.ERROR}]PID must be a number.[/{Theme.ERROR}]")
            return True

        if pid not in self.bg_procs:
            self.console.print(
                f"[{Theme.ERROR}]PID {pid} not found. Use /bg-list.[/{Theme.ERROR}]"
            )
            return True

        info = self.bg_procs[pid]
        log_file = info["log_file"]

        self.console.print()
        self.console.print(
            f"[{Theme.ANNOUNCE}]Attaching to PID {pid}...[/{Theme.ANNOUNCE}]"
        )
        self.console.print(f"[{Theme.DIM}]Command: {info['command']}[/{Theme.DIM}]")
        self.console.print(f"[{Theme.DIM}]Log: {log_file}[/{Theme.DIM}]")
        self.console.print(
            f"[{Theme.DIM}]--- Press Ctrl+C to detach (process keeps running) ---[/{Theme.DIM}]"
        )
        self.console.print()

        try:
            import time

            # Show existing log first
            code, out, _ = self.ssh.exec_command(
                f"cat {log_file} 2>/dev/null", timeout=5
            )
            if out:
                self.console.print(out, end="")

            # Then follow new output
            while True:
                try:
                    code, out, _ = self.ssh.exec_command(
                        f"tail -5 {log_file} 2>/dev/null", timeout=5
                    )
                    if out:
                        self.console.print(out, end="")

                    # Check if process is still running
                    code, check, _ = self.ssh.exec_command(
                        f"kill -0 {pid} 2>/dev/null && echo running || echo dead",
                        timeout=5,
                    )
                    if "dead" in check:
                        self.console.print(
                            f"\n[{Theme.WARNING}]Process exited.[/{Theme.WARNING}]"
                        )
                        del self.bg_procs[pid]
                        break

                    time.sleep(1)
                except KeyboardInterrupt:
                    self.console.print(
                        f"\n[{Theme.DIM}]Detached. Process still running.[/{Theme.DIM}]"
                    )
                    break

        except Exception as e:
            self.console.print(f"[{Theme.ERROR}]Error: {e}[/{Theme.ERROR}]")

        return True

    def _cmd_bg_stop(self, args: list[str]) -> bool:
        """
        Stop a background process: /bg-stop <pid>

        Sends SIGTERM to the process. Use /bg-list to see PIDs.
        """
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        if not args:
            self.console.print(f"[{Theme.ERROR}]Usage: /bg-stop <pid>[/{Theme.ERROR}]")
            self._cmd_bg_list()
            return True

        try:
            pid = int(args[0])
        except ValueError:
            self.console.print(f"[{Theme.ERROR}]PID must be a number.[/{Theme.ERROR}]")
            return True

        if pid not in self.bg_procs:
            self.console.print(
                f"[{Theme.ERROR}]PID {pid} not found. Use /bg-list.[/{Theme.ERROR}]"
            )
            return True

        confirm = questionary.confirm(
            f"Stop process {pid}?",
            default=False,
        ).ask()

        if not confirm:
            self.console.print(f"[{Theme.DIM}]Cancelled.[/{Theme.DIM}]")
            return True

        try:
            self.ssh.exec_command(f"kill -TERM {pid}", timeout=5)
            self.console.print(
                f"[{Theme.SUCCESS}]Sent stop signal to PID {pid}.[/{Theme.SUCCESS}]"
            )

            # Wait a moment and check if it's dead
            import time

            time.sleep(0.5)
            code, out, _ = self.ssh.exec_command(
                f"kill -0 {pid} 2>/dev/null && echo running || echo dead", timeout=5
            )
            if "dead" in out:
                self.console.print(
                    f"[{Theme.SUCCESS}]Process stopped.[/{Theme.SUCCESS}]"
                )
                del self.bg_procs[pid]
            else:
                self.console.print(
                    f"[{Theme.WARNING}]Process still running. Use /bg-stop again or kill -9.[/{Theme.WARNING}]"
                )

        except Exception as e:
            self.console.print(f"[{Theme.ERROR}]Error: {e}[/{Theme.ERROR}]")

        return True

    def _cmd_kill(self) -> bool:
        """Kill llama-server on remote."""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        confirm = questionary.confirm(
            "This will kill the running llama-server. Continue?",
            default=False,
        ).ask()

        if not confirm:
            self.console.print(f"[{Theme.DIM}]Cancelled.[/{Theme.DIM}]")
            return True

        self.console.print(
            f"[{Theme.WARNING}]Killing llama-server...[/{Theme.WARNING}]"
        )
        self.ssh.kill_server()
        self.console.print(f"[{Theme.SUCCESS}]Done.[/{Theme.SUCCESS}]")
        return True

    def _cmd_status(self) -> bool:
        """Show GPU, server, and tunnel status."""
        if not self.ssh or not self.instance:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        # Build hierarchical tree
        tree = Tree(
            f"[{Theme.HEADER}]Instance {self.instance['id']}[/{Theme.HEADER}]",
            guide_style=Theme.DIM,
        )

        # GPU info
        try:
            code, out, _ = self.ssh.exec_command(
                "nvidia-smi --query-gpu=name,memory.used,memory.total,power.draw,clocks.gr "
                "--format=csv,noheader",
                timeout=10,
            )
            if code == 0 and out.strip():
                gpu_parts = [p.strip() for p in out.strip().split(",")]
                if len(gpu_parts) >= 5:
                    gpu_label = (
                        gpu_parts[0]
                        .replace("NVIDIA GeForce ", "")
                        .replace("NVIDIA ", "")
                    )
                    tree.add(
                        f"{Theme.EMOJI_RUNNING} [{Theme.COL_GPU}]{gpu_label}[/{Theme.COL_GPU}] │ "
                        f"VRAM: {gpu_parts[1].strip()}/{gpu_parts[2].strip()} │ "
                        f"Power: {gpu_parts[3].strip()} │ "
                        f"Clock: {gpu_parts[4].strip()}"
                    )
        except Exception:
            tree.add(f"{Theme.EMOJI_WARNING} GPU: query failed")

        # Server process
        server_running = False
        try:
            code, out, _ = self.ssh.exec_command(
                "ps aux | grep '[l]lama-server'", timeout=5
            )
            if out.strip():
                # Extract port from command line
                port_match = ""
                for part in out.split():
                    if part.startswith("--port"):
                        port_match = part.replace("--port", "").strip("=")
                        break
                    if part == "--port" and out.split().index(part) + 1 < len(
                        out.split()
                    ):
                        idx = out.split().index(part)
                        port_match = out.split()[idx + 1]
                        break
                status = f"running on :{port_match}" if port_match else "running"
                server_running = True
            else:
                status = "not running"
        except Exception:
            status = "query failed"

        server_branch = tree.add(
            f"{Theme.EMOJI_RUNNING if server_running else Theme.EMOJI_STOPPED} "
            f"[{Theme.SUCCESS if server_running else Theme.WARNING}]Server: {status}[/{Theme.SUCCESS if server_running else Theme.WARNING}]"
        )

        # Models (sub-branch of server)
        try:
            all_models = setup.list_models(self.ssh)
            models = [m for m in all_models if not m.get("is_mmproj", False)]
            mmproj_models = [m for m in all_models if m.get("is_mmproj", False)]
            if models:
                model_branch = server_branch.add(
                    f"{Theme.EMOJI_MODEL} {len(models)} model(s) available"
                )
                for m in models:
                    model_branch.add(
                        f"[{Theme.COL_PATH}]{m['filename']} ({m['size_gb']} GB)[/{Theme.COL_PATH}]"
                    )
                for m in mmproj_models:
                    model_branch.add(
                        f"[{Theme.DIM}]+ mmproj: {m['filename']} ({m['size_gb']} GB)[/{Theme.DIM}]"
                    )
        except Exception:
            pass

        # Tunnels
        if self.tunnel_mgr:
            tunnels = self.tunnel_mgr.list_tunnels()
            if tunnels:
                tunnel_branch = tree.add(
                    f"{Theme.EMOJI_TUNNEL} Tunnels active ({len(tunnels)})"
                )
                for t in tunnels:
                    tunnel_branch.add(
                        f"[{Theme.INFO}]localhost:{t['local_port']}[/{Theme.INFO}] → "
                        f"[{Theme.INFO}]:{t['remote_port']}[/{Theme.INFO}]"
                    )
            else:
                tree.add(f"{Theme.EMOJI_TUNNEL} Tunnels: none")

        # Standalone models if not under server
        if not server_branch or not server_branch.children:
            try:
                all_models = setup.list_models(self.ssh)
                models = [m for m in all_models if not m.get("is_mmproj", False)]
                mmproj_models = [m for m in all_models if m.get("is_mmproj", False)]
                if models:
                    total_gb = sum(m["size_gb"] for m in models)
                    model_branch = tree.add(
                        f"{Theme.EMOJI_FOLDER} [{Theme.INFO}]{len(models)} models ({total_gb:.1f} GB total)[/{Theme.INFO}]"
                    )
                    for m in models:
                        model_branch.add(
                            f"[{Theme.COL_PATH}]{m['filename']} ({m['size_gb']} GB)[/{Theme.COL_PATH}]"
                        )
                    for m in mmproj_models:
                        model_branch.add(
                            f"[{Theme.DIM}]+ mmproj: {m['filename']} ({m['size_gb']} GB)[/{Theme.DIM}]"
                        )
            except Exception:
                pass

        self.console.print(tree)
        return True

    def _cmd_monitor(self) -> bool:
        """Show real-time GPU/CPU/RAM monitoring dashboard."""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        from llama_remote_control.monitor import run_monitor

        self.console.print(
            f"[{Theme.ANNOUNCE}]Starting real-time monitor...[/{Theme.ANNOUNCE}]"
        )
        self.console.print(f"[{Theme.DIM}]Press Ctrl+C to exit monitor.[/{Theme.DIM}]")

        try:
            run_monitor(self.ssh)
        except KeyboardInterrupt:
            self.console.print(f"[{Theme.DIM}]Monitor exited.[/{Theme.DIM}]")

        return True

    def _cmd_start(self, args: list[str]) -> bool:
        """Start llama-server: /start [model.gguf] [--port PORT] [--mmproj file.gguf]"""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        # Detect available models
        state = self.detect_remote_state()
        if state["server_running"]:
            self.console.print(
                f"[{Theme.WARNING}]Server already running (PID {state['server_pid']}). Use /kill first.[/{Theme.WARNING}]"
            )
            return True

        # Filter out mmproj files from model selection
        models = [m for m in state["models"] if not m.get("is_mmproj", False)]
        mmproj_models = [m for m in state["models"] if m.get("is_mmproj", False)]

        if not models:
            self.console.print(
                f"[{Theme.ERROR}]No models found. Download one first with /download.[/{Theme.ERROR}]"
            )
            return True

        # Determine model to use
        model_file = args[0] if args and not args[0].startswith("--") else None
        if model_file:
            # Verify model exists
            matching = [m for m in models if m["filename"] == model_file]
            if not matching:
                available = ", ".join(m["filename"] for m in models)
                self.console.print(
                    f"[{Theme.ERROR}]Model '{model_file}' not found. Available: {available}[/{Theme.ERROR}]"
                )
                return True
            model_path = matching[0]["path"]
        elif len(models) == 1:
            # Only one model, use it
            model_path = models[0]["path"]
            model_file = models[0]["filename"]
        else:
            # Multiple models — let user choose
            choices = [
                questionary.Choice(title=m["filename"], value=m["path"]) for m in models
            ]
            model_path = questionary.select(
                "Which model to serve?",
                choices=choices,
            ).ask()
            if not model_path:
                self.console.print(f"[{Theme.DIM}]Cancelled.[/{Theme.DIM}]")
                return True
            model_file = model_path.split("/")[-1]

        # Determine port: use user-specified port, or auto-detect free port
        port = None
        for i, arg in enumerate(args):
            if arg in ("--port", "-p") and i + 1 < len(args):
                try:
                    port = int(args[i + 1])
                except ValueError:
                    pass
        if port is None:
            port = state["free_port"]

        # Determine mmproj: use user-specified, or auto-detect
        mmproj_path = None
        for i, arg in enumerate(args):
            if arg in ("--mmproj", "--mmproj-path") and i + 1 < len(args):
                mmproj_path = args[i + 1]
                break

        if not mmproj_path and mmproj_models:
            # Auto-detect: if exactly one mmproj file exists, offer to use it
            if len(mmproj_models) == 1:
                use_mmproj = questionary.confirm(
                    f"Found mmproj file: {mmproj_models[0]['filename']}. Use it?",
                    default=True,
                ).ask()
                if use_mmproj:
                    mmproj_path = mmproj_models[0]["path"]
            else:
                # Multiple mmproj files — let user choose
                mmproj_choices = [
                    questionary.Choice(title=m["filename"], value=m["path"])
                    for m in mmproj_models
                ]
                mmproj_choices.append(
                    questionary.Choice(title="None (text-only model)", value=None)
                )
                mmproj_path = questionary.select(
                    "Select mmproj file (or None):",
                    choices=mmproj_choices,
                ).ask()

        # Use full binary path if available
        server_bin = state.get("llama_server_path") or "llama-server"

        # Build command
        cmd = f"{server_bin} -m {model_path} --host 0.0.0.0 --port {port}"
        if mmproj_path:
            cmd += f" --mmproj {mmproj_path}"

        # Start server
        mmproj_info = f" + mmproj" if mmproj_path else ""
        self.console.print(
            f"[{Theme.ANNOUNCE}]Starting llama-server with {model_file}{mmproj_info} on port {port}...[/{Theme.ANNOUNCE}]"
        )
        self._exec_server(cmd)
        return True

    def _cmd_models(self) -> bool:
        """List models on remote."""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        models = setup.list_models(self.ssh)
        if not models:
            self.console.print(
                f"[{Theme.DIM}]No .gguf files found in /workspace.[/{Theme.DIM}]"
            )
            return True

        table = Table(title="Models on /workspace")
        table.add_column("Type", style=Theme.DIM, no_wrap=True, width=8)
        table.add_column("Filename", style=Theme.COL_ID)
        table.add_column("Size", justify="right", style=Theme.COL_SIZE)
        for m in models:
            file_type = "mmproj" if m.get("is_mmproj") else "model"
            style = Theme.COL_PATH if not m.get("is_mmproj") else Theme.DIM
            table.add_row(
                f"[{Theme.DIM}]{file_type}[/{Theme.DIM}]",
                f"[{style}]{m['filename']}[/{style}]",
                f"{m['size_gb']} GB",
            )
        self.console.print(table)
        return True

    def _cmd_build(self, args: list[str]) -> bool:
        """Build llama.cpp: /build [version]"""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        version = args[0] if args else "latest"
        self.console.print(
            f"[{Theme.ANNOUNCE}]Building llama.cpp ({version})...[/{Theme.ANNOUNCE}]"
        )
        ok = setup.build_llama_cpp(self.console, self.ssh, version=version)
        if ok:
            # Add binaries to PATH
            setup.setup_llama_path(self.ssh)
            self.console.print(f"[{Theme.SUCCESS}]Build complete.[/{Theme.SUCCESS}]")
        else:
            self.console.print(f"[{Theme.ERROR}]Build failed.[/{Theme.ERROR}]")
        return True

    def _cmd_download(self, args: list[str]) -> bool:
        """Download model: /download <url> [filename]"""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        # If no args, prompt for URL
        if len(args) < 1:
            url = questionary.text(
                "Model URL:",
            ).ask()
            if not url:
                self.console.print(f"[{Theme.DIM}]Cancelled.[/{Theme.DIM}]")
                return True
        else:
            url = args[0]

        filename = args[1] if len(args) > 1 else url.split("/")[-1]
        if not filename or "." not in filename:
            filename = "model.gguf"

        # Confirm
        confirm = questionary.confirm(
            f"Download {filename}?",
            default=True,
        ).ask()
        if not confirm:
            self.console.print(f"[{Theme.DIM}]Cancelled.[/{Theme.DIM}]")
            return True

        ok = setup.download_model(self.console, self.ssh, url, filename)
        if ok:
            self.cfg = config.add_recent_model(self.cfg, url, filename)
            config.save_config(self.cfg)
        return True

    def _cmd_logs(self, args: list[str]) -> bool:
        """Tail server log: /logs [n_lines]"""
        if not self.ssh:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        n = 20
        if args:
            try:
                n = int(args[0])
            except ValueError:
                pass

        try:
            code, out, _ = self.ssh.exec_command(
                f"tail -{n} /workspace/llama-server.log 2>/dev/null", timeout=10
            )
            if code == 0 and out.strip():
                self.console.print(out)
            else:
                self.console.print(f"[{Theme.DIM}]No server log found.[/{Theme.DIM}]")
        except Exception as e:
            self.console.print(f"[{Theme.ERROR}]{e}[/{Theme.ERROR}]")
        return True

    def _cmd_shell(self) -> bool:
        """
        Open an interactive PTY shell on the remote instance.

        This gives you a full terminal session on the Vast.ai GPU instance.
        You can run llama-server manually with your own flags:

            llama-server -m /workspace/model.gguf --port 8080 -ngl 99 -ctk llama -cml 32

        Press Ctrl+C to stop a running server.
        Type 'exit' to close the shell and return to the REPL.
        """
        if not self.ssh or not self.ssh.client:
            self.console.print(f"[{Theme.ERROR}]Not connected.[/{Theme.ERROR}]")
            return True

        self.console.print()
        self.console.print(
            f"[{Theme.ANNOUNCE}]Opening interactive shell on remote...[/{Theme.ANNOUNCE}]"
        )
        self.console.print(
            f"[{Theme.DIM}]Run llama-server with your own flags. 'exit' to return.[/{Theme.DIM}]"
        )
        self.console.print()

        try:
            import select
            import threading
            import time

            client = self.ssh.client
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                self.console.print(
                    f"[{Theme.ERROR}]SSH transport not active.[/{Theme.ERROR}]"
                )
                return True

            # Open an interactive session channel with PTY
            channel = transport.open_session()
            channel.get_pty(
                term="xterm-256color",
                width=140,
                height=50,
            )
            channel.invoke_shell()

            def read_from_channel(stop_event: threading.Event) -> None:
                """Background thread: read from SSH channel and write to stdout."""
                try:
                    while not stop_event.is_set():
                        if channel.recv_ready():
                            data = channel.recv(65536).decode("utf-8", errors="replace")
                            if data:
                                sys.stdout.write(data)
                                sys.stdout.flush()
                        else:
                            time.sleep(0.01)
                        if channel.closed:
                            break
                except Exception:
                    pass

            stop_event = threading.Event()
            reader_thread = threading.Thread(
                target=read_from_channel,
                args=(stop_event,),
                daemon=True,
            )
            reader_thread.start()

            is_windows = sys.platform.startswith("win")

            if is_windows:
                # Windows: poll channel output, send stdin when available
                import msvcrt

                def windows_stdin_read() -> bytes | None:
                    """Non-blocking read from Windows console, returns bytes or None."""
                    if msvcrt.kbhit():
                        return msvcrt.getwch().encode("utf-8", errors="replace")
                    return None

                try:
                    while not stop_event.is_set():
                        if channel.closed:
                            break
                        if channel.recv_ready():
                            data = channel.recv(65536).decode("utf-8", errors="replace")
                            if data:
                                sys.stdout.write(data)
                                sys.stdout.flush()
                        # Send any keypresses to the channel
                        key = windows_stdin_read()
                        if key:
                            channel.send(key)
                        time.sleep(0.01)
                except (OSError, EOFError):
                    pass
            else:
                # Unix: use select on stdin + channel
                try:
                    while not stop_event.is_set():
                        readable, _, _ = select.select([channel], [], [], 0.1)
                        if channel in readable:
                            try:
                                data = os.read(sys.stdin.fileno(), 4096)
                                if data:
                                    channel.send(data)
                                else:
                                    break
                            except (OSError, EOFError):
                                break
                        if channel.closed:
                            break
                except (select.error, OSError, EOFError):
                    pass

            # Clean up
            stop_event.set()
            reader_thread.join(timeout=2)

            try:
                channel.close()
            except Exception:
                pass

        except Exception as e:
            self.console.print(f"[{Theme.ERROR}]Shell error: {e}[/{Theme.ERROR}]")

        self.console.print()
        self.console.print(f"[{Theme.DIM}]Shell closed. Back to REPL.[/{Theme.DIM}]")
        return True

    def _cmd_switch(self) -> bool:
        """Switch to a different instance."""
        # Clean up current connection
        if self.tunnel_mgr:
            self.tunnel_mgr.close_all()
        if self.ssh:
            self.ssh.disconnect()

        self.ssh = None
        self.tunnel_mgr = None
        self.instance = None

        return False  # False = go back to instance selection

    def _cmd_test(self) -> bool:
        """Test server through tunnel."""
        if not self.tunnel_mgr:
            self.console.print(
                f"[{Theme.ERROR}]No tunnels active. Use /port first.[/{Theme.ERROR}]"
            )
            return True

        tunnels = self.tunnel_mgr.list_tunnels()
        if not tunnels:
            self.console.print(f"[{Theme.ERROR}]No tunnels active.[/{Theme.ERROR}]")
            return True

        for t in tunnels:
            port = t["local_port"]
            ok = self.tunnel_mgr.test_tunnel(port)
            if ok:
                self.console.print(
                    f"[{Theme.SUCCESS}]localhost:{port} -> responding[/{Theme.SUCCESS}]"
                )
            else:
                self.console.print(
                    f"[{Theme.ERROR}]localhost:{port} -> no response[/{Theme.ERROR}]"
                )
        return True

    def _cmd_help(self) -> bool:
        """Show help."""
        table = Table(title="Slash Commands", show_lines=True)
        table.add_column("Command", style=Theme.COL_COMMAND, no_wrap=True)
        table.add_column("Description")
        table.add_row(
            "/start [model] [--port N]", "Start llama-server (auto-detects model)"
        )
        table.add_row(
            "/shell",
            "Interactive PTY shell — run llama-server manually with your own flags",
        )
        table.add_row(
            "/tunnel <remote_port> [local_port]",
            "Create SSH tunnel (local defaults to remote_port)",
        )
        table.add_row("/tunnels", "List active tunnels")
        table.add_row("/close <port | --all>", "Close a tunnel or all tunnels")
        table.add_row("/kill", "Kill llama-server on remote")
        table.add_row("/status", "GPU, server, tunnels overview")
        table.add_row("/monitor", "Real-time GPU/CPU/RAM dashboard")
        table.add_row("/models", "List .gguf files on remote")
        table.add_row("/build [version]", "Build/update llama.cpp")
        table.add_row("/download [url] [name]", "Download a model (prompts if no URL)")
        table.add_row("/logs [n]", "Tail server log (default 20)")
        table.add_row("/switch", "Switch to a different instance")
        table.add_row("/test", "Test tunnel connectivity")
        table.add_row("/clear", "Clear terminal")
        table.add_row("/help", "Show this help")
        table.add_row("/exit", "Disconnect and quit")
        self.console.print(table)
        self.console.print()
        self.console.print(
            f"[{Theme.DIM}]Typical workflow:[/{Theme.DIM}]\n"
            f"  1. [cyan]/shell[/{cyan}]                    — SSH into remote\n"
            f"  2. [cyan]llama-server -m model.gguf ...[/{cyan}]  — Run with your own flags\n"
            f"  3. Ctrl+C to stop server\n"
            f"  4. [cyan]exit[/{cyan}] to close shell\n"
            f"  5. [cyan]/tunnel 8080[/{cyan}]              — Tunnel remote:8080 to localhost:8080\n"
            f"  6. Open [cyan]http://localhost:8080[/{cyan}] on your local PC"
        )
        return True

    def _cmd_exit(self) -> bool:
        """Exit the CLI."""
        self.console.print("[dim]Disconnecting...[/dim]")
        raise EOFError  # Handled by the REPL loop

    # ── Initial menu ────────────────────────────────────────────────────

    def show_menu(self) -> str:
        """
        Show context-aware menu based on remote state.

        Detects what's already on the instance and offers relevant options.
        Returns action string: "setup", "start_server", "shell", "status", "download", "monitor"
        """
        # Detect remote state
        state = self.detect_remote_state()
        # Filter out mmproj files — they're not standalone models
        model_files = [m for m in state["models"] if not m.get("is_mmproj", False)]
        has_models = len(model_files) > 0
        has_llama = state["llama_built"]
        server_running = state["server_running"]

        # Build summary of detected state
        summary_parts = []
        if has_llama:
            summary_parts.append(f"[green]llama.cpp[/green] {state['llama_version']}")
        if has_models:
            model_names = ", ".join(m["filename"] for m in model_files)
            summary_parts.append(f"[green]Models:[/green] {model_names}")
        if server_running:
            port_str = f":{state['server_port']}" if state["server_port"] else ""
            model_str = f" ({state['server_model']})" if state["server_model"] else ""
            summary_parts.append(
                f"[green]Server running[/green] on {port_str}{model_str} [dim]PID {state['server_pid']}[/dim]"
            )

        # Show state summary
        if summary_parts:
            self.console.print()
            self.console.print(
                f"[{Theme.DIM}]Detected: {' │ '.join(summary_parts)}[/{Theme.DIM}]"
            )
        else:
            self.console.print()
            self.console.print(
                f"[{Theme.DIM}]No setup detected — fresh instance[/{Theme.DIM}]"
            )

        # Build context-aware choices
        choices = []

        # Option 1: Always offer to start server if not running and we have models
        if not server_running and has_models and has_llama:
            # Auto-pick first model
            model_file = model_files[0]["filename"]
            port = state["free_port"]
            port_hint = f" :{port}" if port != 8080 else ""
            choices.append(
                questionary.Choice(
                    title=f"🚀 Start server ({model_file}{port_hint})",
                    value=f"start_server:{model_file}",
                )
            )
        elif server_running:
            choices.append(
                questionary.Choice(
                    title=f"🔄 Restart server ({state['server_model'] or 'last model'})",
                    value="restart_server",
                )
            )

        # Option 2: Setup (build + download)
        setup_title = "🛠️  Setup (build llama.cpp + download model)"
        if has_llama and has_models:
            setup_title = "🛠️  Setup (rebuild / download more models)"
        choices.append(
            questionary.Choice(
                title=setup_title,
                value="setup",
            )
        )

        # Option 3: Download only
        if has_llama and not has_models:
            choices.append(
                questionary.Choice(
                    title="📦 Download model (llama.cpp already built)",
                    value="download_only",
                )
            )
        elif has_models:
            choices.append(
                questionary.Choice(
                    title="📦 Download another model",
                    value="download_only",
                )
            )

        # Option 4: Monitor
        choices.append(
            questionary.Choice(
                title="📊 Status & Monitor",
                value="monitor",
            )
        )

        # Option 5: Shell
        choices.append(
            questionary.Choice(
                title="🐚 Shell (interactive)",
                value="shell",
            )
        )

        choice = questionary.select(
            "What do you want to do?",
            choices=choices,
            style=questionary.Style(
                [
                    ("selected", "fg:#00ff00 bold"),
                    ("highlighted", "fg:#00ffff bold"),
                    ("answer", "fg:#00ffff bold"),
                    ("pointer", "fg:#00ff00 bold"),
                ]
            ),
        ).ask()

        return choice or "shell"  # Default to Shell if user cancels

    # ── REPL loop ───────────────────────────────────────────────────────

    def repl(self) -> None:
        """Run the interactive REPL shell."""
        if not self.ssh:
            return

        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            """Ctrl+C: if a command is running, send interrupt. Otherwise do nothing."""
            if self._command_running:
                event.current_buffer.validate_and_handle()

        # Build prompt session with history and completion
        completer = WordCompleter(
            SLASH_COMMANDS,
            sentence=True,
            ignore_case=True,
        )
        self.session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            completer=completer,
            key_bindings=kb,
            multiline=False,
        )

        self.console.print()
        self.console.print(
            Panel(
                f"[{Theme.SUCCESS}]Connected![/{Theme.SUCCESS}]  "
                f"Type commands or [cyan]/help[/cyan] for slash commands.\n"
                "Ctrl+C stops the current command. Ctrl+D or /exit to quit.",
                border_style=Theme.BORDER_CONNECTED,
            )
        )
        self.console.print()

        while True:
            try:
                prompt_text = self._get_prompt_text()
                user_input = self.session.prompt(prompt_text).strip()

                if not user_input:
                    continue

                # Handle slash commands
                if user_input.startswith("/"):
                    result = self._handle_slash(user_input)
                    if result is False and user_input.lower() in (
                        "/switch",
                        "/exit",
                        "/quit",
                    ):
                        if user_input.lower() == "/switch":
                            break  # Go back to instance selection
                        else:
                            return  # Exit CLI
                    continue

                # Handle cd commands (update cwd locally)
                if user_input.startswith("cd "):
                    target = user_input[3:].strip()
                    if target == "-":
                        target = "/workspace"
                    elif not target.startswith("/"):
                        target = f"{self.cwd}/{target}"

                    # Resolve .. and .
                    try:
                        code, out, _ = self.ssh.exec_command(
                            f"cd {target} 2>/dev/null && pwd", timeout=5
                        )
                        if code == 0 and out.strip():
                            self.cwd = out.strip()
                        else:
                            self.console.print(
                                f"[red]cd: {target}: No such directory[/red]"
                            )
                    except Exception:
                        pass
                    continue

                # Execute remote command
                self._exec_remote(user_input)

            except EOFError:
                # Ctrl+D or /exit — exit completely
                self.console.print("\n[dim]Goodbye![/dim]")
                return True  # Signal to exit CLI
            except KeyboardInterrupt:
                # Ctrl+C at prompt — do nothing
                self.console.print()
                continue

        # /switch was used — return False to loop back to instance selection
        return False

    # ── Main entry ──────────────────────────────────────────────────────

    def run(self) -> bool:
        """
        Main entry point for the CLI.

        Returns:
            True if user exited completely (/exit or Ctrl+D)
            False if user switched instances (caller should reconnect)
        """
        self.console.print(
            Panel(
                f"[{Theme.BOLD}]llama[/{Theme.BOLD}] — "
                "interactive llama.cpp manager for Vast.ai",
                border_style=Theme.BORDER_WELCOME,
            )
        )

        # Step 1: Select instance (loop until success or quit)
        while True:
            if not self.select_instance():
                return True  # User quit during selection

            # Step 2: Connect
            if not self.connect():
                return True  # Connection failed

            # Step 3: Show context-aware menu
            choice = self.show_menu()

            if choice == "setup":
                ok = self.run_setup()
                if not ok:
                    self.console.print(
                        f"[{Theme.WARNING}]Setup cancelled or failed.[/{Theme.WARNING}]"
                    )

            elif choice == "download_only":
                self._cmd_download([])

            elif choice == "monitor":
                self._cmd_status()
                self._cmd_monitor()

            elif choice.startswith("start_server:"):
                model_file = choice.split(":", 1)[1]
                # Find the model's actual full path from detected models
                state = self.detect_remote_state()
                model_path = f"/workspace/{model_file}"  # fallback
                for m in state["models"]:
                    if m["filename"] == model_file:
                        model_path = m["path"]
                        break

                port = state["free_port"]
                server_bin = state.get("llama_server_path") or "llama-server"
                self.console.print(
                    f"[{Theme.ANNOUNCE}]Starting llama-server with {model_file} on port {port}...[/{Theme.ANNOUNCE}]"
                )
                cmd = f"{server_bin} -m {model_path} --host 0.0.0.0 --port {port}"
                self._exec_server(cmd)

            elif choice == "restart_server":
                self.console.print(
                    f"[{Theme.ANNOUNCE}]Restarting llama-server...[/{Theme.ANNOUNCE}]"
                )
                self.ssh.kill_server()
                # Re-detect state after killing server (frees port)
                state = self.detect_remote_state()
                non_mmproj = [
                    m for m in state["models"] if not m.get("is_mmproj", False)
                ]
                if non_mmproj:
                    model_path = non_mmproj[0]["path"]
                    port = state["free_port"]
                    server_bin = state.get("llama_server_path") or "llama-server"
                    cmd = f"{server_bin} -m {model_path} --host 0.0.0.0 --port {port}"
                    self.console.print(
                        f"[{Theme.INFO}]Using model: {non_mmproj[0]['filename']} on port {port}[/{Theme.INFO}]"
                    )
                    self._exec_server(cmd)
                else:
                    self.console.print(
                        f"[{Theme.WARNING}]No models found. Run /shell to start server manually.[/{Theme.WARNING}]"
                    )

            # choice == "shell" or anything else -> go straight to shell

            # Step 4: REPL
            exit_completely = self.repl()
            if exit_completely:
                # User typed /exit or Ctrl+D — quit the CLI
                return True

            # /switch was used — loop back to instance selection
            # Clean up current connection
            if self.tunnel_mgr:
                self.tunnel_mgr.close_all()
            if self.ssh:
                self.ssh.disconnect()
            self.ssh = None
            self.tunnel_mgr = None
            self.instance = None

        return True  # Should not reach here


def main() -> None:
    """Entry point called by the `llama` command."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s: %(message)s",
    )

    # Create Typer app
    app = typer.Typer(
        name="llama",
        help="Interactive llama.cpp manager for Vast.ai GPU instances",
        epilog="Run without subcommands for interactive mode.",
        rich_markup_mode="rich",
    )

    @app.callback(invoke_without_command=True)
    def callback(ctx: typer.Context) -> None:
        """
        Interactive llama.cpp manager for Vast.ai.

        Run without subcommands to enter interactive mode:
        - Select a GPU instance from Vast.ai
        - Connect via SSH
        - Build llama.cpp and download models
        - Manage GPU server and SSH tunnels
        """
        # If no subcommand was given, run interactive mode
        if ctx.invoked_subcommand is None:
            run_interactive()

    @app.command()
    def connect(
        instance_id: int = typer.Option(
            None, "--id", "-i", help="Instance ID to connect to (skips selection)"
        ),
    ) -> None:
        """Connect to a Vast.ai instance and start the shell."""
        logging.basicConfig(
            level=logging.WARNING,
            format="%(name)s: %(message)s",
        )
        cli = LlamaCLI()
        if instance_id:
            # Skip selection, find instance by ID
            cli.api_key = config.get_api_key(cli.cfg)
            cli.ssh_key_path = config.get_ssh_key_path(cli.cfg)
            cli.instances = vastai.fetch_instances(cli.api_key)
            cli.instance = next(
                (i for i in cli.instances if i["id"] == instance_id), None
            )
            if not cli.instance:
                typer.echo(f"Instance {instance_id} not found.")
                raise typer.Exit(1)
        if cli.run() is not False:
            # Clean up on exit
            if cli.tunnel_mgr:
                cli.tunnel_mgr.close_all()
            if cli.ssh:
                cli.ssh.disconnect()

    @app.command()
    def status() -> None:
        """Show status of the last connected instance."""
        logging.basicConfig(
            level=logging.WARNING,
            format="%(name)s: %(message)s",
        )
        typer.echo("Status command - use interactive mode for now.")
        typer.echo("Run: llama")

    @app.command()
    def version() -> None:
        """Show llama-cli version."""
        typer.echo("llama-cli v0.1.0")

    # Run the Typer app
    app()


def run_interactive() -> None:
    """Run the interactive CLI loop."""
    try:
        app = LlamaCLI()
        result = app.run()
        # Clean up
        if app.tunnel_mgr:
            app.tunnel_mgr.close_all()
        if app.ssh:
            app.ssh.disconnect()
    except KeyboardInterrupt:
        print("\nGoodbye!")
    except Exception as e:
        Console().print(f"[{Theme.ERROR}]Fatal error: {e}[/{Theme.ERROR}]")
        sys.exit(1)
