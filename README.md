# llama-remote-control

Interactive CLI for managing llama.cpp on Vast.ai GPU instances via SSH.

Build, download models, start servers, and expose them locally — all from one terminal.

## Quick Start

```bash
# Install
pip install -e .

# Configure Vast.ai API key (one of these)
export VASTAI_API_KEY="your_key_here"
# Or create ~/.llama-cli.json:
#   {"api_key": "your_key_here", "ssh_key_path": "~/.ssh/id_ed25519"}

# Run
llama
```

The CLI will:
1. Fetch your Vast.ai instances
2. Let you pick one
3. Connect via SSH
4. Show a menu based on what's already set up on the instance

## Features

- **One-command setup** — Build llama.cpp, download models (with mmproj), start server
- **Interactive shell** — Full PTY SSH session, run any command with your own flags
- **Background processes** — Run servers detached like tmux, attach/detach anytime
- **SSH tunneling** — Expose remote ports locally, access llama-server from your browser
- **Vision model support** — Auto-detect and manage mmproj files for multimodal models
- **Real-time monitoring** — GPU/CPU/RAM dashboard

## Slash Commands

### Server Control

| Command | Description |
|---------|-------------|
| `/start [model] [--port N] [--mmproj file]` | Start llama-server (auto-detects model, port, mmproj) |
| `/kill` | Kill llama-server on remote |
| `/shell` | Interactive SSH shell — run any command with your own flags |

### Background Processes (tmux-like)

| Command | Description |
|---------|-------------|
| `/bg-proc <command>` | Start any command in background (detached) |
| `/bg-list` | List running background processes with status |
| `/bg-attach <pid>` | Attach to a process's output (Ctrl+C to detach, process keeps running) |
| `/bg-stop <pid>` | Stop a background process |

### Tunneling

| Command | Description |
|---------|-------------|
| `/tunnel <remote_port> [local_port]` | Create SSH tunnel (defaults to same port on both sides) |
| `/tunnels` | List active tunnels |
| `/close <port>` | Close a specific tunnel |
| `/close --all` | Close all tunnels |

### Setup & Models

| Command | Description |
|---------|-------------|
| `/build [version]` | Build/update llama.cpp from source |
| `/download <url> [name]` | Download a model from HuggingFace |
| `/models` | List all .gguf files on remote (models + mmproj) |

### Monitoring

| Command | Description |
|---------|-------------|
| `/status` | GPU, server, tunnels, models overview |
| `/monitor` | Real-time GPU/CPU/RAM dashboard |
| `/logs [n]` | Tail server log (default 20 lines) |

### Navigation

| Command | Description |
|---------|-------------|
| `/switch` | Switch to a different Vast.ai instance |
| `/test` | Test tunnel connectivity |
| `/clear` | Clear terminal |
| `/help` | Show all commands |
| `/exit` | Disconnect and quit |

## Typical Workflows

### Quick Start (Auto Mode)

```
$ llama
# Select instance → Setup wizard builds llama.cpp and downloads model
# Then start server with /start or /bg-proc
```

### Manual Server with Custom Flags

```
$ llama

# Open interactive shell
root@34186938 [RTX 3090] /workspace> /shell

# Run llama-server with YOUR flags, live output
$ llama-server -m /workspace/model.gguf --port 8080 -ngl 99 -c 8192 --host 0.0.0.0

# Ctrl+C to stop the server
$ exit

# Back in the REPL, create tunnel
root@34186938 [RTX 3090] /workspace> /tunnel 8080

# Open http://localhost:8080 in your browser
```

### Background Server (Like tmux)

```
$ llama

# Start server in background — it keeps running even if you close the CLI
root@34186938 [RTX 3090] /workspace> /bg-proc llama-server -m model.gguf --port 8080 -ngl 99

# Started (PID 1234). Go do other things.

# Check on it later
root@34186938 [RTX 3090] /workspace> /bg-list

# Attach to see live output (Ctrl+C to detach without stopping)
root@34186938 [RTX 3090] /workspace> /bg-attach 1234

# Stop it when done
root@34186938 [RTX 3090] /workspace> /bg-stop 1234
```

### Vision Models (mmproj)

The setup wizard asks about vision support before downloading:

```
? Model URL: https://huggingface.co/.../model.gguf
? Filename: (model.gguf)
? Does this model have vision (multimodal)? Yes
? mmproj download URL: https://huggingface.co/.../mmproj.gguf
? Build llama.cpp and download model.gguf + mmproj.gguf?

Step 1/3: Building llama.cpp...
Step 2/3: Downloading model...
Step 3/3: Downloading mmproj...
```

`/start` auto-detects mmproj files and asks if you want to use them:
```
/start model.gguf
# "Found mmproj file: mmproj.gguf. Use it?" → Yes
# Starts: llama-server -m model.gguf --mmproj mmproj.gguf --port 8080
```

Or pass it manually:
```
/start model.gguf --mmproj /workspace/mmproj.gguf --port 9000
```

## Config File

`~/.llama-cli.json`:
```json
{
  "api_key": "your_vastai_api_key",
  "ssh_key_path": "/home/user/.ssh/id_ed25519",
  "default_instance": 12345,
  "recent_models": [
    {
      "url": "https://huggingface.co/.../model.gguf",
      "filename": "model.gguf"
    }
  ]
}
```

## Requirements

- Python 3.10+
- SSH key configured on Vast.ai
- Vast.ai API key

## License

MIT
