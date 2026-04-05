"""
Setup module for llama-deploy CLI.

This module handles building llama.cpp from source and downloading models
on remote instances via SSH. It provides functions for:

- Building llama.cpp from the official GitHub repository with CUDA support
- Downloading GGUF models using aria2c (with wget fallback)
- Listing available models on the remote instance
- Running an interactive setup wizard for streamlined configuration

Example:
    >>> from llama_remote_control.setup import build_llama_cpp, download_model
    >>> from llama_remote_control.ssh import SSHConnection
    >>> from rich.console import Console
    >>>
    >>> console = Console()
    >>> ssh = SSHConnection("user@host")
    >>> build_llama_cpp(console, ssh, version="b4400")
    >>> download_model(console, ssh, "https://...", "model.gguf")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import questionary
from alive_progress import alive_bar
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

if TYPE_CHECKING:
    from llama_remote_control.ssh import SSHConnection

logger = logging.getLogger("llama_remote_control.setup")


def build_llama_cpp(
    console: Console,
    ssh: "SSHConnection",
    version: str = "latest",
    repo: str = "ggerganov/llama.cpp",
) -> bool:
    """
    Build llama.cpp from source on the remote instance.

    Clones the repository, configures with CUDA support, and builds the project.
    Uses interactive execution for clone/configure/build steps to stream output live.

    Args:
        console: Rich console for output display
        ssh: SSH connection to the remote instance
        version: Git tag/version to build (default: "latest" fetches newest release)
        repo: GitHub repository in "owner/repo" format

    Returns:
        True if build succeeds and binary is verified, False otherwise
    """
    from llama_remote_control.theme import Theme
    clone_tag = version
    if version == "latest":
        console.print("[bold blue]Fetching latest llama.cpp release tag...[/bold blue]")
        try:
            r = httpx.get(
                f"https://api.github.com/repos/{repo}/releases/latest",
                timeout=15,
                follow_redirects=True,
            )
            data = r.json()
            clone_tag = data.get("tag_name", "")
            # Strip leading 'b' or 'v' prefix if present (e.g. "b8665" -> "8665") for display
            display_tag = clone_tag.lstrip("bBvV")
            if display_tag:
                console.print(f"[green]Latest release: {clone_tag}[/green]")
            else:
                raise ValueError("empty tag")
        except Exception as e:
            console.print(
                f"[yellow]Could not fetch latest tag ({e}), falling back to 'master'[/yellow]"
            )
            clone_tag = "master"
    else:
        console.print(f"[blue]Using specified version: {version}[/blue]")

    # Clean old build
    console.print(f"[{Theme.ANNOUNCE}]Cleaning old build...[/{Theme.ANNOUNCE}]")
    result = ssh.exec_interactive("rm -rf /workspace/llama.cpp")
    if result != 0:
        console.print(f"[{Theme.ERROR}]Failed to clean old build directory[/{Theme.ERROR}]")
        return False

    # Clone repository
    console.print(f"[{Theme.ANNOUNCE}]Cloning llama.cpp (tag: {clone_tag})...[/{Theme.ANNOUNCE}]")
    clone_cmd = (
        f"cd /workspace && "
        f"git clone --depth 1 --branch {clone_tag} https://github.com/{repo}.git llama.cpp"
    )
    with alive_bar(
        title="Cloning repository...",
        spinner="arrows",
        length=30,
    ):
        result = ssh.exec_interactive(clone_cmd)
    if result != 0:
        console.print(f"[{Theme.ERROR}]Failed to clone repository[/{Theme.ERROR}]")
        return False
    console.print(f"[{Theme.SUCCESS}]✓ Clone complete[/{Theme.SUCCESS}]")

    # Configure with CMake
    console.print(f"[{Theme.ANNOUNCE}]Configuring build with CUDA support...[/{Theme.ANNOUNCE}]")
    cmake_cmd = "cd /workspace/llama.cpp && cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release"
    with alive_bar(
        title="Running CMake configuration...",
        spinner="arrows2",
        length=30,
    ):
        result = ssh.exec_interactive(cmake_cmd)
    if result != 0:
        console.print(f"[{Theme.ERROR}]CMake configuration failed[/{Theme.ERROR}]")
        return False
    console.print(f"[{Theme.SUCCESS}]✓ Configuration complete[/{Theme.SUCCESS}]")

    # Build
    console.print(
        f"[{Theme.ANNOUNCE}]Building llama.cpp (this may take several minutes)...[/{Theme.ANNOUNCE}]"
    )
    build_cmd = "cd /workspace/llama.cpp/build && cmake --build . -j$(nproc)"
    with alive_bar(
        title="Compiling with CUDA...",
        spinner="triangles",
        length=40,
        calibrate=5000,
    ):
        result = ssh.exec_interactive(build_cmd)
    if result != 0:
        console.print(f"[{Theme.ERROR}]Build failed[/{Theme.ERROR}]")
        return False

    # Verify binary exists
    console.print(f"[{Theme.ANNOUNCE}]Verifying llama-server binary...[/{Theme.ANNOUNCE}]")
    with alive_bar(
        title="Checking binary...",
        spinner="dots",
        length=20,
    ):
        verify_cmd = "test -f /workspace/llama.cpp/build/bin/llama-server"
        exit_code, _, _ = ssh.exec_command(verify_cmd)
    if exit_code != 0:
        console.print(f"[{Theme.ERROR}]llama-server binary not found after build[/{Theme.ERROR}]")
        return False

    console.print(f"[{Theme.SUCCESS}]✓ llama.cpp built successfully![/{Theme.SUCCESS}]")
    return True


def setup_llama_path(ssh: "SSHConnection") -> bool:
    """
    Add llama.cpp binaries to the remote PATH.

    After building llama.cpp, this ensures the binary directory
    is in PATH so commands like llama-server, llama-cli, etc. work directly.

    Args:
        ssh: SSH connection to the remote instance

    Returns:
        True if PATH was updated successfully
    """
    from llama_remote_control.theme import Theme

    bin_path = "/workspace/llama.cpp/build/bin"
    console = Console()
    console.print(f"[{Theme.ANNOUNCE}]Adding llama.cpp binaries to PATH...[/{Theme.ANNOUNCE}]")

    # Verify binary directory exists
    exit_code, _, _ = ssh.exec_command(f"test -d {bin_path}")
    if exit_code != 0:
        console.print(f"[{Theme.WARNING}]Binary directory not found: {bin_path}[/{Theme.WARNING}]")
        return False

    success = ssh.ensure_path(bin_path)
    if success:
        console.print(f"[{Theme.SUCCESS}]✓ PATH updated: {bin_path}[/{Theme.SUCCESS}]")
    else:
        console.print(f"[{Theme.ERROR}]Failed to update PATH[/{Theme.ERROR}]")

    return success


def download_model(
    console: Console,
    ssh: "SSHConnection",
    url: str,
    filename: str,
) -> bool:
    """
    Download a GGUF model file to the remote instance.

    Uses aria2c for fast parallel downloads (16 connections), with wget as fallback.
    Automatically installs aria2c if not present on the remote system.

    Args:
        console: Rich console for output display
        ssh: SSH connection to the remote instance
        url: Direct download URL for the model
        filename: Name to save the file as (e.g., "model.gguf")

    Returns:
        True if download succeeds and file is verified, False otherwise
    """
    from llama_remote_control.theme import Theme

    # Ensure aria2c is available
    console.print(f"[{Theme.ANNOUNCE}]Checking for aria2c...[/{Theme.ANNOUNCE}]")
    check_cmd = "which aria2c || apt-get install -y -qq aria2"
    with alive_bar(
        title="Setting up download tools...",
        spinner="wait",
        length=30,
    ):
        result = ssh.exec_interactive(check_cmd)
    if result != 0:
        console.print(
            f"[{Theme.WARNING}]Could not ensure aria2c is installed, will try wget fallback[/{Theme.WARNING}]"
        )

    # Remove existing file
    console.print(f"[{Theme.ANNOUNCE}]Removing existing file if present...[/{Theme.ANNOUNCE}]")
    ssh.exec_interactive(f"rm -f /workspace/{filename}")

    # Download with aria2c
    console.print(f"[{Theme.ANNOUNCE}]Downloading model with aria2c...[/{Theme.ANNOUNCE}]")
    download_cmd = (
        f"cd /workspace && "
        f"aria2c -x 16 -s 16 -d /workspace -o {filename} '{url}' --summary-interval=5"
    )
    result = ssh.exec_interactive(download_cmd)

    # Fallback to wget if aria2c fails
    if result != 0:
        console.print(f"[{Theme.WARNING}]aria2c failed, trying wget...[/{Theme.WARNING}]")
        with alive_bar(
            title=f"Downloading {filename}...",
            spinner="dots_waves",
            length=30,
        ):
            wget_cmd = f"cd /workspace && wget -O {filename} '{url}'"
            result = ssh.exec_interactive(wget_cmd)
        if result != 0:
            console.print(f"[{Theme.ERROR}]Download failed with both aria2c and wget[/{Theme.ERROR}]")
            return False

    # Verify file exists and has size > 0
    console.print(f"[{Theme.ANNOUNCE}]Verifying downloaded file...[/{Theme.ANNOUNCE}]")
    with alive_bar(
        title="Verifying...",
        spinner="dots",
        length=20,
    ):
        verify_cmd = f"test -s /workspace/{filename}"
        exit_code, _, _ = ssh.exec_command(verify_cmd)
    if exit_code != 0:
        console.print(f"[{Theme.ERROR}]Downloaded file does not exist or is empty[/{Theme.ERROR}]")
        return False

    # Get file size
    size_cmd = f"du -h /workspace/{filename} | cut -f1"
    exit_code, stdout, _ = ssh.exec_command(size_cmd)
    if exit_code == 0:
        size_str = stdout.strip()
        console.print(
            f"[{Theme.SUCCESS}]✓ Model downloaded successfully ({size_str})![/{Theme.SUCCESS}]"
        )
    else:
        console.print(f"[{Theme.SUCCESS}]✓ Model downloaded successfully![/{Theme.SUCCESS}]")

    return True


def list_models(ssh: "SSHConnection") -> list[dict]:
    """
    List available GGUF models in /workspace on the remote instance.

    Args:
        ssh: SSH connection to the remote instance

    Returns:
        List of model dictionaries with keys: filename, size_gb, path.
        Returns empty list if no models found or command fails.
    """
    cmd = "ls -lh /workspace/*.gguf 2>/dev/null"
    exit_code, stdout, _ = ssh.exec_command(cmd)

    if exit_code != 0 or not stdout.strip():
        return []

    models = []
    for line in stdout.strip().split("\n"):
        # Parse ls -lh output: permissions size date name
        # Example: -rw-r--r-- 1 user user 4.1G Jan 1 12:00 model.gguf
        parts = line.split()
        if len(parts) < 9:
            continue

        size_str = parts[4]  # Size column
        filename = parts[-1]  # Last column is filename (full path)

        # Convert size to GB
        size_gb = 0.0
        try:
            if size_str.endswith("G"):
                size_gb = float(size_str[:-1])
            elif size_str.endswith("M"):
                size_gb = float(size_str[:-1]) / 1024
            elif size_str.endswith("K"):
                size_gb = float(size_str[:-1]) / (1024 * 1024)
            elif size_str.endswith("T"):
                size_gb = float(size_str[:-1]) * 1024
            else:
                # Assume bytes
                size_gb = float(size_str) / (1024 * 1024 * 1024)
        except (ValueError, IndexError):
            pass

        models.append(
            {
                "filename": filename.split("/")[-1],  # Just the basename
                "size_gb": round(size_gb, 2),
                "path": filename,
            }
        )

    return models


def run_setup_wizard(
    console: Console,
    ssh: "SSHConnection",
) -> tuple[str, str] | None:
    """
    Run an interactive setup wizard for building llama.cpp and downloading a model.

    Guides the user through:
    1. Choosing whether to use the latest llama.cpp release
    2. Providing a model download URL (with recent model suggestions)
    3. Setting the filename (auto-detected from URL)
    4. Confirming before execution

    On success, saves the model to recent_models in config.

    Args:
        console: Rich console for output display
        ssh: SSH connection to the remote instance

    Returns:
        Tuple of (model_path, llama_version) on success, None on failure or cancellation.
        model_path is the full path to the downloaded model on the remote instance.
    """
    from llama_remote_control.theme import Theme

    console.print(
        Panel.fit(
            "[bold cyan]Llama.cpp Setup Wizard[/bold cyan]",
            border_style=Theme.BORDER_WIZARD,
        )
    )

    # Load config for recent model suggestions
    from llama_remote_control import config

    cfg = config.load_config()

    # Ask about version using questionary
    use_latest = questionary.confirm(
        "Use latest llama.cpp?",
        default=True,
    ).ask()

    if use_latest is None:  # User cancelled
        return None

    version = "latest" if use_latest else "master"

    # Show recent models as suggestions if available
    suggestions = []
    recent = cfg.get("recent_models", [])
    if recent:
        suggestions = [m.get("url", "") for m in recent[-5:]]
        recent_display = ", ".join(m.get("filename", "?") for m in recent[-5:])
        console.print(f"[{Theme.DIM}]Recent: {recent_display}[/{Theme.DIM}]")

    # Ask for model URL using questionary with autocomplete
    url_question = questionary.text(
        "Model URL:",
        instruction="(HuggingFace or direct download URL)",
    )
    if suggestions:
        url_question = questionary.autocomplete(
            "Model URL:",
            choices=suggestions,
            instruction="Use Tab to autocomplete recent URL",
        )

    url = url_question.ask()

    if not url:
        console.print(f"[{Theme.ERROR}]No URL provided, canceling setup.[/{Theme.ERROR}]")
        return None

    # Auto-detect filename from URL
    default_filename = url.split("/")[-1]
    if not default_filename or "." not in default_filename:
        default_filename = "model.gguf"

    # Ask for filename
    filename = questionary.text(
        "Filename:",
        default=default_filename,
    ).ask()

    if not filename:
        filename = default_filename

    # Confirm before execution
    confirm = questionary.confirm(
        f"Build llama.cpp and download {filename}?",
        default=True,
    ).ask()

    if not confirm:
        console.print(f"[{Theme.DIM}]Setup cancelled.[/{Theme.DIM}]")
        return None

    console.print()

    # Build llama.cpp
    console.print(f"[{Theme.ANNOUNCE}]Step 1/2: Building llama.cpp...[/{Theme.ANNOUNCE}]")
    build_success = build_llama_cpp(console, ssh, version=version)
    if not build_success:
        console.print(f"[{Theme.ERROR}]Build failed, aborting setup.[/{Theme.ERROR}]")
        return None

    # Add binaries to PATH
    setup_llama_path(ssh)

    # Determine version string for return value
    llama_version = version
    if version == "latest":
        # Get the actual tag that was built
        exit_code, stdout, _ = ssh.exec_command(
            "cd /workspace/llama.cpp && git describe --tags --always 2>/dev/null || echo 'unknown'"
        )
        if exit_code == 0 and stdout.strip():
            llama_version = stdout.strip()

    # Download model
    console.print()
    console.print(f"[{Theme.ANNOUNCE}]Step 2/2: Downloading model...[/{Theme.ANNOUNCE}]")
    download_success = download_model(console, ssh, url, filename)
    if not download_success:
        console.print(f"[{Theme.ERROR}]Download failed, setup incomplete.[/{Theme.ERROR}]")
        return None

    # Save to recent models
    model_path = f"/workspace/{filename}"
    config.add_recent_model(cfg, url, filename)
    config.save_config(cfg)

    console.print()
    console.print(
        Panel.fit(
            f"[{Theme.SUCCESS}]✓ Setup complete![/{Theme.SUCCESS}]\n"
            f"[{Theme.DIM}]Model:[/{Theme.DIM}] {filename}\n"
            f"[{Theme.DIM}]Path:[/{Theme.DIM}] {model_path}\n"
            f"[{Theme.DIM}]llama.cpp:[/{Theme.DIM}] {llama_version}",
            border_style=Theme.BORDER_SUCCESS,
        )
    )

    return (model_path, llama_version)
