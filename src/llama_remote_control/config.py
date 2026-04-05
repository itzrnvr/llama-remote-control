"""Configuration management for the llama-deploy CLI.

This module handles reading and writing the CLI configuration file stored
at ~/.llama-cli.json. It provides functions to load, save, and manipulate
configuration settings including SSH keys, API keys, and recent model history.

The config file uses JSON format with the following structure:
    {
        "ssh_key": null,           # Path to SSH private key
        "vastai_api_key": null,    # Vast.ai API key
        "recent_models": [],       # List of recently used models
        "last_instance_id": null   # ID of most recently used instance
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger: logging.Logger = logging.getLogger("llama_remote_control.config")

CONFIG_PATH: Path = Path.home() / ".llama-cli.json"

DEFAULT_CONFIG: dict = {
    "ssh_key": None,
    "vastai_api_key": None,
    "recent_models": [],
    "last_instance_id": None,
}

DEFAULT_SSH_KEY_PATH: Path = Path.home() / ".ssh" / "id_ed25519_vastai"


def load_config() -> dict:
    """Load configuration from the config file.

    Reads the configuration from ~/.llama-cli.json. If the file does not
    exist, returns the default configuration. If the file contains malformed
    JSON, logs a warning and returns the default configuration.

    Returns:
        dict: The loaded configuration or defaults if file is missing/invalid.
    """
    if not CONFIG_PATH.exists():
        logger.debug("Config file not found at %s, using defaults", CONFIG_PATH)
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config: dict = json.load(f)
        logger.debug("Loaded config from %s", CONFIG_PATH)
        return config
    except json.JSONDecodeError as e:
        logger.warning(
            "Malformed JSON in config file %s: %s. Using defaults.", CONFIG_PATH, e
        )
        return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    """Write configuration to the config file.

    Writes the given configuration dictionary to ~/.llama-cli.json
    with indentation for readability.

    Args:
        config: The configuration dictionary to save.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    logger.debug("Saved config to %s", CONFIG_PATH)


def get_ssh_key_path(config: dict) -> str:
    """Get the SSH key path from configuration.

    Checks the config["ssh_key"] setting first, then falls back to the
    default path ~/.ssh/id_ed25519_vastai. Raises FileNotFoundError if
    the resolved path does not exist.

    Args:
        config: The configuration dictionary.

    Returns:
        str: The path to the SSH private key file.

    Raises:
        FileNotFoundError: If the SSH key file does not exist.
    """
    ssh_key_path: Path
    if config.get("ssh_key"):
        ssh_key_path = Path(config["ssh_key"])
    else:
        ssh_key_path = DEFAULT_SSH_KEY_PATH

    if not ssh_key_path.exists():
        raise FileNotFoundError(f"SSH key not found at {ssh_key_path}")

    return str(ssh_key_path)


def get_api_key(config: dict) -> str:
    """Get the Vast.ai API key from configuration.

    Checks the config["vastai_api_key"] setting first, then falls back to
    the VASTAI_API_KEY environment variable. Raises RuntimeError if neither
    is set.

    Args:
        config: The configuration dictionary.

    Returns:
        str: The Vast.ai API key.

    Raises:
        RuntimeError: If no API key is configured.
    """
    api_key: str | None = config.get("vastai_api_key") or os.environ.get(
        "VASTAI_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "Vast.ai API key not found. Set it in config or VASTAI_API_KEY environment variable."
        )

    return api_key


def add_recent_model(config: dict, url: str, filename: str) -> dict:
    """Add a model to the recent models list.

    Adds a new model entry to the recent_models list. The list is limited
    to 10 entries, with the most recent first. Duplicate entries based on
    filename are removed before adding the new entry.

    Args:
        config: The configuration dictionary to update.
        url: The URL of the model.
        filename: The filename of the model.

    Returns:
        dict: The updated configuration dictionary.
    """
    recent_models: list[dict] = config.get("recent_models", [])

    # Remove any existing entry with the same filename (deduplication)
    recent_models = [m for m in recent_models if m.get("filename") != filename]

    # Add new model at the beginning (most recent first)
    recent_models.insert(0, {"url": url, "filename": filename})

    # Limit to 10 entries
    recent_models = recent_models[:10]

    config["recent_models"] = recent_models
    return config


def set_last_instance(config: dict, instance_id: int) -> dict:
    """Set the last used instance ID.

    Updates the configuration with the most recently used instance ID.

    Args:
        config: The configuration dictionary to update.
        instance_id: The ID of the instance.

    Returns:
        dict: The updated configuration dictionary.
    """
    config["last_instance_id"] = instance_id
    return config
