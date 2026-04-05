"""
PURPOSE: Interactive CLI tool for managing llama.cpp on Vast.ai GPU instances.
         Lists instances, builds llama.cpp from source, downloads models,
         starts servers, and provides an interactive SSH shell with
         slash commands for tunneling, status, and process management.

KEY DECISIONS:
- Python + rich + prompt_toolkit + paramiko (best-in-class for interactive SSH REPLs)
- Config stored at ~/.llama-cli.json (SSH key path, API key, recent models, last instance)
- No instance creation/destruction — user manages that on vast.ai web UI
- Persistent SSH connection via paramiko — no re-auth per command
- Ctrl+C sends SIGINT to remote process (not to the CLI itself)

GOTCHAS:
- Vast.ai API key is required. Set in ~/.llama-cli.json or VASTAI_API_KEY env var.
- SSH key must be registered on Vast.ai. Default: ~/.ssh/id_ed25519_vastai
- Direct IP connection preferred over proxy (proxy can be flaky)
"""

__version__ = "0.1.0"
