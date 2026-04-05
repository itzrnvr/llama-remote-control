"""
Vast.ai API client for fetching instance data.

This module provides functions to interact with the Vast.ai REST API
to retrieve and display instance information.

KEY DECISIONS:
- Uses httpx for async-capable HTTP requests (though currently sync)
- Rich table for formatted console output
- No classes - pure functional approach as requested
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from rich.table import Table

# Set up module-level logger
logger = logging.getLogger("llama_remote_control.vastai")

API_BASE_URL = "https://console.vast.ai/api/v0"


def fetch_instances(api_key: str) -> list[dict]:
    """
    Fetch all Vast.ai instances via the REST API.

    Args:
        api_key: The Vast.ai API key for authentication.

    Returns:
        A list of instance dictionaries with normalized fields.

    Raises:
        ValueError: If the API key is invalid (401 error).
        ConnectionError: If the API cannot be reached.
    """
    url = f"{API_BASE_URL}/instances/"
    headers = {"Authorization": f"Bearer {api_key}"}

    logger.debug(f"Fetching instances from {url}")

    try:
        response = httpx.get(url, headers=headers, timeout=30.0)
    except httpx.NetworkError as e:
        logger.error(f"Network error connecting to Vast.ai: {e}")
        raise ConnectionError("Cannot reach Vast.ai API") from e
    except httpx.TimeoutException as e:
        logger.error(f"Timeout connecting to Vast.ai: {e}")
        raise ConnectionError("Cannot reach Vast.ai API") from e

    if response.status_code == 401:
        logger.error("Authentication failed: Invalid API key")
        raise ValueError("Invalid API key")

    response.raise_for_status()

    data = response.json()
    raw_instances = data.get("instances", [])

    logger.info(f"Fetched {len(raw_instances)} instances from Vast.ai")

    instances = []
    for raw in raw_instances:
        instance = _parse_instance(raw)
        instances.append(instance)

    return instances


def _parse_instance(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Parse a raw Vast.ai instance response into a normalized dictionary.

    Args:
        raw: The raw instance data from the API.

    Returns:
        A normalized instance dictionary.
    """
    # Extract GPU name from various possible locations
    gpu_name = raw.get("gpu_name", "")
    if not gpu_name:
        device_reqs = raw.get("device_reqs", {})
        if isinstance(device_reqs, dict):
            gpu_name = device_reqs.get("gpu_name", "Unknown")

    # Extract GPU count from device_reqs
    gpu_count = 1
    device_reqs = raw.get("device_reqs", {})
    if isinstance(device_reqs, dict):
        gpu_count = device_reqs.get("gpu_count", 1)

    # Get status
    status = raw.get("actual_status", "unknown")

    # Calculate uptime from start_ts
    uptime: str | None = None
    start_ts = raw.get("start_ts")
    if start_ts and status == "running":
        try:
            start_time = datetime.fromtimestamp(start_ts)
            delta = datetime.now() - start_time
            uptime = _format_timedelta(delta)
        except (ValueError, TypeError, OSError) as e:
            logger.warning(f"Failed to parse start_ts {start_ts}: {e}")
            uptime = None

    # Extract SSH connection info
    # The API returns proxy info in ssh_host/ssh_port, but the direct IP
    # is in public_ipaddr with the SSH port in ports['22/tcp'].
    # Direct IP is much more reliable than the proxy.
    direct_ip = str(raw.get("public_ipaddr", ""))
    direct_port = 22
    ports = raw.get("ports", {})
    if isinstance(ports, dict) and "22/tcp" in ports:
        port_mapping = ports["22/tcp"]
        if isinstance(port_mapping, list) and port_mapping:
            direct_port = int(port_mapping[0].get("HostPort", 22))

    return {
        "id": raw.get("id", 0),
        "gpu_name": str(gpu_name) if gpu_name else "Unknown",
        "gpu_count": int(gpu_count),
        "status": str(status).lower(),
        "cost_per_hour": float(raw.get("dph_total", raw.get("current_bid", 0.0))),
        "ssh_host": direct_ip,  # Direct IP (reliable)
        "ssh_port": direct_port,  # Direct SSH port (reliable)
        "ssh_proxy_host": str(raw.get("ssh_host", "")),  # Proxy (fallback)
        "ssh_proxy_port": int(raw.get("ssh_port", 0)) if raw.get("ssh_port") else None,
        "image": str(raw.get("image", "")),
        "uptime": uptime,
    }


def _format_timedelta(delta: Any) -> str:
    """
    Format a timedelta into a human-readable string.

    Args:
        delta: The timedelta to format.

    Returns:
        A human-readable string like "2h 30m" or "5d 12h".
    """
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def format_instance_table(instances: list[dict]) -> Table:
    """
    Create a Rich table displaying instance information.

    Args:
        instances: A list of instance dictionaries from fetch_instances().

    Returns:
        A Rich Table with formatted instance data.
    """
    from llama_remote_control.theme import Theme

    table = Table(title="Vast.ai Instances")

    table.add_column("#", justify="right", style=Theme.COL_ID, no_wrap=True)
    table.add_column("ID", justify="right", style=Theme.COL_ID, no_wrap=True)
    table.add_column("GPU", style=Theme.COL_GPU)
    table.add_column("Status", justify="center")
    table.add_column("$/hr", justify="right", style=Theme.COL_PRICE)
    table.add_column("Uptime", justify="right", style=Theme.COL_UPTIME)

    for idx, inst in enumerate(instances, start=1):
        status = inst.get("status", "unknown")
        status_style = _get_status_style(status)

        gpu_info = f"{inst.get('gpu_name', 'Unknown')} x{inst.get('gpu_count', 1)}"
        cost = f"${inst.get('cost_per_hour', 0.0):.4f}"
        uptime = inst.get("uptime") or "-"

        table.add_row(
            str(idx),
            str(inst.get("id", "-")),
            gpu_info,
            f"[{status_style}]{status}[/{status_style}]",
            cost,
            uptime,
        )

    return table


def _get_status_style(status: str) -> str:
    """
    Get the Rich style for a given status.

    Args:
        status: The instance status string.

    Returns:
        The Rich color style to use.
    """
    from llama_remote_control.theme import Theme

    status_lower = status.lower()
    if status_lower == "running":
        return Theme.COL_STATUS_RUNNING
    elif status_lower == "stopped":
        return Theme.COL_STATUS_STOPPED
    else:
        return Theme.COL_STATUS_OTHER
