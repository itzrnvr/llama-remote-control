"""
Centralized color theme and styling constants for llama-cli.

All Rich markup tags and table column styles are defined here.
Change this file to retheme the entire application.
"""

from __future__ import annotations

# ── Semantic color tokens ───────────────────────────────────────────────

class Theme:
    """Rich markup color tokens used throughout the UI."""

    # States
    SUCCESS = "bold green"
    ERROR = "red"
    WARNING = "yellow"
    INFO = "cyan"

    # Text hierarchy
    HEADER = "bold cyan"
    ANNOUNCE = "bold blue"       # Step/phase announcements
    BOLD = "bold"
    DIM = "dim"

    # Panel borders
    BORDER_WELCOME = "magenta"
    BORDER_CONNECTED = "green"
    BORDER_WIZARD = "cyan"
    BORDER_SUCCESS = "green"

    # Table columns
    COL_ID = "cyan"
    COL_GPU = "magenta"
    COL_STATUS_RUNNING = "green"
    COL_STATUS_STOPPED = "yellow"
    COL_STATUS_OTHER = "red"
    COL_PRICE = "green"
    COL_UPTIME = "blue"
    COL_SIZE = "green"
    COL_PATH = "dim"
    COL_COMMAND = "cyan"

    # Emoji indicators
    EMOJI_RUNNING = "🟢"
    EMOJI_STOPPED = "🔴"
    EMOJI_WARNING = "🟡"
    EMOJI_FOLDER = "📁"
    EMOJI_SERVER = "🖥️"
    EMOJI_TUNNEL = "🔗"
    EMOJI_MODEL = "📦"


# ── Table style presets ─────────────────────────────────────────────────

def base_table_style() -> dict:
    """Return a dict of common table styling defaults."""
    return {
        "show_header": True,
        "header_style": "bold magenta",
        "border_style": "dim",
        "padding": (0, 1),
    }


# ── Questionary choice formatters ───────────────────────────────────────

def format_instance_choice(instance: dict) -> str:
    """Format an instance dict into a questionary select choice string."""
    gpu = (
        instance.get("gpu_name", "?")
        .replace("NVIDIA GeForce ", "")
        .replace("NVIDIA ", "")
    )
    count = instance.get("gpu_count", 1)
    gpu_str = f"{gpu} ×{count}" if count > 1 else gpu
    status = instance.get("status", "unknown")
    # cost_per_hour is set by _parse_instance (vastai.py)
    cost = instance.get("cost_per_hour", 0.0)
    if isinstance(cost, str):
        cost = float(cost) if cost else 0.0
    price_str = f"${cost:.4f}"
    inst_id = instance.get("id", "?")
    return f"{inst_id} │ {gpu_str} │ {status} │ {price_str}/hr"


def format_menu_choice(index: int, label: str, emoji: str = "") -> str:
    """Format a menu choice with optional emoji."""
    if emoji:
        return f"{emoji} {label}"
    return f"{index}. {label}"
