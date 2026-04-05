"""
Real-time GPU/CPU/RAM monitoring dashboard.

Provides a live-updating Rich Live display showing system metrics
on the remote instance, updated every 2 seconds.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from llama_remote_control.ssh import SSHConnection


def _make_bar(percent: float, width: int = 30, full_char: str = "█", empty_char: str = "░") -> str:
    """Create a text progress bar."""
    filled = int(width * percent / 100)
    return full_char * filled + empty_char * (width - filled)


def _get_gpu_info(ssh: SSHConnection) -> dict:
    """Fetch GPU info via nvidia-smi."""
    cmd = (
        "nvidia-smi --query-gpu=name,memory.used,memory.total,"
        "utilization.gpu,utilization.memory,temperature.gpu,power.draw,power.limit,"
        "clocks.gr,clocks.mem --format=csv,noheader"
    )
    exit_code, stdout, _ = ssh.exec_command(cmd, timeout=10)
    if exit_code != 0 or not stdout.strip():
        return {"error": True}

    gpus = []
    for line in stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue
        try:
            gpus.append({
                "name": parts[0].replace("NVIDIA GeForce ", "").replace("NVIDIA ", ""),
                "mem_used_mb": float(parts[1].replace(" MiB", "")),
                "mem_total_mb": float(parts[2].replace(" MiB", "")),
                "gpu_util": float(parts[3].replace(" %", "")),
                "mem_util": float(parts[4].replace(" %", "")),
                "temp": float(parts[5].replace(" C", "")),
                "power_draw": float(parts[6].replace(" W", "")) if "W" in parts[6] else 0,
                "power_limit": float(parts[7].replace(" W", "")) if "W" in parts[7] else 0,
                "clock_gpu": parts[8].replace(" MHz", ""),
                "clock_mem": parts[9].replace(" MHz", ""),
            })
        except (ValueError, IndexError):
            continue

    return {"gpus": gpus}


def _get_cpu_info(ssh: SSHConnection) -> dict:
    """Fetch CPU usage from /proc/stat."""
    # Read first sample
    exit_code, stdout, _ = ssh.exec_command(
        "head -1 /proc/stat && nproc",
        timeout=5,
    )
    if exit_code != 0 or not stdout.strip():
        return {"error": True}

    lines = stdout.strip().split("\n")
    stat_parts = lines[0].split()
    if len(stat_parts) < 5:
        return {"error": True}

    cores = int(lines[1]) if len(lines) > 1 else 1
    user, nice, system, idle = int(stat_parts[1]), int(stat_parts[2]), int(stat_parts[3]), int(stat_parts[4])
    total1 = user + nice + system + idle + sum(int(x) for x in stat_parts[5:]) if len(stat_parts) > 5 else user + nice + system + idle

    # Wait briefly for second sample
    time.sleep(0.5)

    exit_code, stdout, _ = ssh.exec_command("head -1 /proc/stat", timeout=5)
    if exit_code != 0:
        return {"error": True}

    stat_parts = stdout.strip().split()
    user, nice, system, idle = int(stat_parts[1]), int(stat_parts[2]), int(stat_parts[3]), int(stat_parts[4])
    total2 = user + nice + system + idle + sum(int(x) for x in stat_parts[5:]) if len(stat_parts) > 5 else user + nice + system + idle

    total_diff = total2 - total1
    idle_diff = idle - int(lines[0].split()[4]) if len(lines) > 0 else 0

    # Re-parse first sample for idle
    first_idle = int(stdout.strip().split()[4]) if exit_code == 0 else 0

    usage = ((total_diff - idle_diff) / total_diff * 100) if total_diff > 0 else 0

    # Get load average
    exit_code, load_out, _ = ssh.exec_command("cat /proc/loadavg", timeout=5)
    load = load_out.strip().split()[:3] if exit_code == 0 else ["?", "?", "?"]

    return {
        "usage": min(usage, 100),
        "cores": cores,
        "load": load,
    }


def _get_ram_info(ssh: SSHConnection) -> dict:
    """Fetch RAM info from /proc/meminfo."""
    exit_code, stdout, _ = ssh.exec_command("cat /proc/meminfo", timeout=5)
    if exit_code != 0:
        return {"error": True}

    mem = {}
    for line in stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            value = int(parts[1])  # in kB
            mem[key] = value

    total_kb = mem.get("MemTotal", 0)
    available_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    used_kb = total_kb - available_kb

    return {
        "total_gb": total_kb / (1024 * 1024),
        "used_gb": used_kb / (1024 * 1024),
        "available_gb": available_kb / (1024 * 1024),
        "percent": (used_kb / total_kb * 100) if total_kb > 0 else 0,
    }


def _render_dashboard(ssh: SSHConnection) -> Panel:
    """Render the full monitoring dashboard as a Rich Panel."""
    gpu_info = _get_gpu_info(ssh)
    cpu_info = _get_cpu_info(ssh)
    ram_info = _get_ram_info(ssh)

    lines = []

    # GPU section
    if gpu_info.get("error"):
        lines.append("[red]GPU: nvidia-smi query failed[/red]")
    else:
        for idx, gpu in enumerate(gpu_info.get("gpus", []), 1):
            mem_pct = (gpu["mem_used_mb"] / gpu["mem_total_mb"] * 100) if gpu["mem_total_mb"] > 0 else 0
            mem_bar = _make_bar(mem_pct)
            power_pct = (gpu["power_draw"] / gpu["power_limit"] * 100) if gpu["power_limit"] > 0 else 0
            power_bar = _make_bar(power_pct)
            temp_color = "green" if gpu["temp"] < 70 else "yellow" if gpu["temp"] < 85 else "red"
            temp_bar = _make_bar(min(gpu["temp"], 100))

            lines.append(f"[bold cyan]GPU {idx}:[/bold cyan] [magenta]{gpu['name']}[/magenta]")
            lines.append(f"  VRAM:  {gpu['mem_used_mb']:.0f}/{gpu['mem_total_mb']:.0f} MB  {mem_bar}  {mem_pct:.0f}%")
            lines.append(f"  Power: {gpu['power_draw']:.0f}W/{gpu['power_limit']:.0f}W  {power_bar}  {power_pct:.0f}%")
            lines.append(f"  Temp:  [{temp_color}]{gpu['temp']:.0f}°C[/{temp_color}]  {temp_bar}")
            lines.append(f"  Clock: GPU {gpu['clock_gpu']} MHz | Mem {gpu['clock_mem']} MHz")
            if idx < len(gpu_info.get("gpus", [])):
                lines.append("")

    # Separator
    lines.append("[dim]" + "─" * 60 + "[/dim]")

    # CPU section
    if cpu_info.get("error"):
        lines.append("[red]CPU: query failed[/red]")
    else:
        cpu_pct = cpu_info["usage"]
        cpu_bar = _make_bar(cpu_pct)
        cpu_color = "green" if cpu_pct < 50 else "yellow" if cpu_pct < 80 else "red"
        lines.append(f"[bold cyan]CPU:[/bold cyan] {cpu_info['cores']} cores")
        lines.append(f"  Usage: [{cpu_color}]{cpu_pct:.1f}%[/{cpu_color}]  {cpu_bar}")
        lines.append(f"  Load:  {cpu_info['load'][0]} / {cpu_info['load'][1]} / {cpu_info['load'][2]} (1/5/15 min)")

    # Separator
    lines.append("[dim]" + "─" * 60 + "[/dim]")

    # RAM section
    if ram_info.get("error"):
        lines.append("[red]RAM: query failed[/red]")
    else:
        ram_pct = ram_info["percent"]
        ram_bar = _make_bar(ram_pct)
        ram_color = "green" if ram_pct < 60 else "yellow" if ram_pct < 85 else "red"
        lines.append(f"[bold cyan]RAM:[/bold cyan] {ram_info['total_gb']:.1f} GB total")
        lines.append(f"  Used:  [{ram_color}]{ram_info['used_gb']:.1f} GB[/{ram_color}] / {ram_info['total_gb']:.1f} GB  {ram_bar}  {ram_pct:.1f}%")
        lines.append(f"  Free:  {ram_info['available_gb']:.1f} GB")

    content = "\n".join(lines)

    return Panel(
        content,
        title="[bold magenta]⚡ System Monitor[/bold magenta]",
        subtitle="[dim]Press Ctrl+C or q to exit[/dim]",
        border_style="magenta",
        box=box.DOUBLE,
    )


def run_monitor(ssh: SSHConnection, refresh_interval: float = 2.0) -> None:
    """
    Run the live monitoring dashboard.

    Uses Rich Live to continuously update the dashboard at the specified
    refresh interval. Exits on Ctrl+C.

    Args:
        ssh: SSH connection to the remote instance
        refresh_interval: Seconds between updates
    """
    console = Console()

    try:
        with Live(
            _render_dashboard(ssh),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            while True:
                time.sleep(refresh_interval)
                live.update(_render_dashboard(ssh))
    except KeyboardInterrupt:
        console.print("[dim]Monitor exited.[/dim]")
