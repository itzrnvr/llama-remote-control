# llama-remote-control

Interactive CLI for managing llama.cpp on Vast.ai GPU instances via SSH.

## Quick Start

1. **Install**:
```bash
pip install -e .
```

2. **Configure** your Vast.ai API key:
```bash
# Set environment variable
export VASTAI_API_KEY="your_key_here"

# Or create ~/.llama-cli.json:
{
  "api_key": "your_key_here",
  "ssh_key_path": "~/.ssh/id_ed25519"
}
```

3. **Run**:
```bash
llama
```

## Features

- 🚀 **One-command setup** — Builds llama.cpp, downloads models, starts server
- 🔧 **Interactive shell** — Full SSH session to run custom commands
- 🖥️ **Background processes** — Run servers detached, attach later (like tmux)
- 🔌 **SSH tunneling** — Expose remote ports locally with `/tunnel`
- 📊 **Real-time monitoring** — GPU/CPU/RAM dashboard

## Commands

| Command | Description |
|---------|-------------|
| `/start [model]` | Start llama-server with auto-detected model |
| `/shell` | Interactive SSH shell — run any command |
| `/bg-proc <cmd>` | Start background process (detached) |
| `/bg-list` | List running background processes |
| `/bg-attach <pid>` | Attach to process output |
| `/bg-stop <pid>` | Stop a background process |
| `/tunnel <remote> [local]` | Create SSH tunnel |
| `/tunnels` | List active tunnels |
| `/close <port>` | Close a tunnel |
| `/status` | Show instance status |
| `/monitor` | Real-time GPU dashboard |
| `/models` | List downloaded models |
| `/download <url>` | Download a model from HuggingFace |
| `/build [version]` | Build/update llama.cpp |
| `/kill` | Kill llama-server |
| `/help` | Show all commands |

## Typical Workflow

```
# Connect to Vast.ai instance
$ llama

# Start server in background
root@12345 [RTX 3090] /workspace> /bg-proc llama-server -m model.gguf --port 8080 -ngl 99

# Create tunnel to access it locally
root@12345 [RTX 3090] /workspace> /tunnel 8080

# Open http://localhost:8080 in your browser

# Check on the process
root@12345 [RTX 3090] /workspace> /bg-list
root@12345 [RTX 3090] /workspace> /bg-attach 1234  # view output

# Stop when done
root@12345 [RTX 3090] /workspace> /bg-stop 1234
```

## Config File

`~/.llama-cli.json`:
```json
{
  "api_key": "your_vastai_api_key",
  "ssh_key_path": "/home/user/.ssh/id_ed25519",
  "default_instance": 12345
}
```

## Requirements

- Python 3.10+
- SSH key configured on Vast.ai
- Vast.ai API key

## License

MIT
