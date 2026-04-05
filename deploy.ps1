<#
.SYNOPSIS
    llama-deploy - Set up llama.cpp on a Vast.ai instance with one command.
    Streams all output live so you can watch everything in real-time.

.DESCRIPTION
    Reads config.json, connects to a Vast.ai instance via SSH, builds/gets
    llama.cpp, downloads the model, starts the server, and creates a local
    SSH tunnel to localhost:8000.

.USAGE
    .\deploy.ps1              # Full setup (build + download + start + tunnel)
    .\deploy.ps1 -SkipBuild   # Skip llama.cpp build (already built)
    .\deploy.ps1 -SkipModel   # Skip model download (already downloaded)
    .\deploy.ps1 -ServerOnly  # Just restart the server + tunnel
    .\deploy.ps1 -TunnelOnly  # Just restart the SSH tunnel
    .\deploy.ps1 -Stop        # Kill server + tunnel
    .\deploy.ps1 -Status      # Check instance status
#>

param(
    [switch]$SkipBuild,
    [switch]$SkipModel,
    [switch]$ServerOnly,
    [switch]$TunnelOnly,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir "config.json"

# ── Colors & helpers ─────────────────────────────────────────────────────
function Write-Section([string]$Text) {
    Write-Host ""
    Write-Host "  ┌─────────────────────────────────────────────" -ForegroundColor DarkCyan
    Write-Host "  │  $Text" -ForegroundColor Cyan
    Write-Host "  └─────────────────────────────────────────────" -ForegroundColor DarkCyan
    Write-Host ""
}

function Write-Ok([string]$Text) {
    Write-Host "  [OK] $Text" -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host "  [!] $Text" -ForegroundColor Yellow
}

function Write-Fail([string]$Text) {
    Write-Host "  [FAIL] $Text" -ForegroundColor Red
}

function Write-Step([string]$Text) {
    Write-Host "  -> $Text" -ForegroundColor White
}

function Write-Dim([string]$Text) {
    Write-Host "     $Text" -ForegroundColor DarkGray
}

function Get-Elapsed([datetime]$Start) {
    $dur = (Get-Date) - $Start
    if ($dur.TotalSeconds -lt 60) { return "$([math]::Round($dur.TotalSeconds, 1))s" }
    $min = [math]::Floor($dur.TotalMinutes)
    $sec = [math]::Round($dur.TotalSeconds % 60)
    return "${min}m ${sec}s"
}

# ── Load Config ──────────────────────────────────────────────────────────
if (-not (Test-Path $ConfigPath)) {
    Write-Fail "config.json not found at $ConfigPath"
    exit 1
}

$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$SshKey = $Config.ssh.key_path
$ProxyHost = $Config.ssh.proxy_host
$ProxyPort = $Config.ssh.proxy_port
$DirectIP = $Config.ssh.direct_ip
$DirectPort = $Config.ssh.direct_port
$LocalPort = $Config.tunnel.local_port
$RemotePort = $Config.server.port

# ── SSH helpers ──────────────────────────────────────────────────────────
function Get-SshTarget {
    if ($ProxyHost -and $ProxyPort) {
        return @{ Host = $ProxyHost; Port = $ProxyPort }
    }
    if ($DirectIP -and $DirectPort) {
        return @{ Host = $DirectIP; Port = $DirectPort }
    }
    Write-Fail "Set either proxy_host+proxy_port or direct_ip+direct_port in config.json"
    exit 1
}

# Run SSH command and stream ALL output to the terminal live (stdout + stderr)
function Invoke-Stream([string]$Cmd) {
    $Target = Get-SshTarget
    # Using & operator directly streams both stdout and stderr to the terminal
    # Last exit code is captured via $LASTEXITCODE
    & ssh -i $SshKey -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p $Target.Port "root@$($Target.Host)" $Cmd 2>&1
    return $LASTEXITCODE
}

# Run SSH command and capture output (for when we need the result)
function Invoke-Capture([string]$Cmd) {
    $Target = Get-SshTarget
    $output = & ssh -i $SshKey -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p $Target.Port "root@$($Target.Host)" $Cmd 2>$null
    return $output
}

# ── Step 1: Build llama.cpp ─────────────────────────────────────────────
function Step-Build {
    Write-Section "BUILDING LLAMA.CPP"
    $t = Get-Date

    $Version = $Config.llama.version
    $Repo = $Config.llama.repo

    if ($Version -eq "latest") {
        Write-Step "Fetching latest release tag..."
        $Tag = Invoke-Capture "curl -sL https://api.github.com/repos/$Repo/releases/latest | grep tag_name | head -1 | grep -oP 'v[0-9.]+'"
        $Tag = ($Tag -join "").Trim()
        if (-not $Tag) { $Tag = "master" }
        Write-Ok "Latest release: $Tag"
    } else {
        $Tag = $Version
        Write-Step "Using version: $Tag"
    }

    Write-Step "Cleaning old build..."
    Invoke-Stream "rm -rf /workspace/llama.cpp"

    Write-Step "Cloning llama.cpp (depth 1, branch $Tag)..."
    Invoke-Stream "cd /workspace && git clone --depth 1 --branch $Tag https://github.com/$Repo.git llama.cpp"

    Write-Step "Configuring CUDA build..."
    Invoke-Stream "cd /workspace/llama.cpp && cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release"

    Write-Step "Compiling (this takes ~10-15 min)..."
    Write-Host ""
    Invoke-Stream "cd /workspace/llama.cpp/build && make -j`$(nproc)"
    Write-Host ""

    Write-Ok "Build complete in $(Get-Elapsed $t)"
}

# ── Step 2: Download model ──────────────────────────────────────────────
function Step-DownloadModel {
    Write-Section "DOWNLOADING MODEL"
    $t = Get-Date

    $Url = $Config.model.url
    $Filename = $Config.model.filename

    Write-Dim "URL: $Url"
    Write-Dim "File: $Filename"
    Write-Host ""

    Write-Step "Ensuring aria2c is installed..."
    Invoke-Stream "which aria2c > /dev/null 2>&1 || apt-get install -y -qq aria2 2>&1"

    Write-Step "Starting download (16 connections)..."
    Write-Host ""
    $exitCode = Invoke-Stream "cd /workspace && rm -f $Filename && aria2c -x 16 -s 16 -d /workspace -o $Filename '$Url' --summary-interval=5"
    Write-Host ""

    if ($exitCode -eq 0) {
        $Size = Invoke-Capture "ls -lh /workspace/$Filename"
        Write-Ok "Download complete in $(Get-Elapsed $t)"
        Write-Dim "$($Size -join '')"
    } else {
        Write-Warn "aria2c exited with code $exitCode, trying wget fallback..."
        Invoke-Stream "cd /workspace && wget -q --show-progress -O $Filename '$Url'"
        $Size = Invoke-Capture "ls -lh /workspace/$Filename"
        Write-Ok "Download complete (wget fallback) in $(Get-Elapsed $t)"
    }
}

# ── Step 3: Start server ────────────────────────────────────────────────
function Step-StartServer {
    Write-Section "STARTING LLAMA.CPP SERVER"
    $t = Get-Date

    $S = $Config.server
    $ModelFile = "/workspace/$($Config.model.filename)"

    # Build the command
    $Cmd = "/workspace/llama.cpp/build/bin/llama-server"
    $Cmd += " -m $ModelFile"
    $Cmd += " --host $($S.host)"
    $Cmd += " --port $($S.port)"
    $Cmd += " --device $($S.device)"
    $Cmd += " --ctx-size $($S.ctx_size)"
    $Cmd += " -np $($S.np)"

    if ($S.flash_attn) { $Cmd += " --flash-attn on" }
    if ($S.cache_type_k) { $Cmd += " --cache-type-k $($S.cache_type_k)" }
    if ($S.cache_type_v) { $Cmd += " --cache-type-v $($S.cache_type_v)" }
    if ($S.temp -ne $null) { $Cmd += " --temp $($S.temp)" }
    if ($S.top_k -ne $null) { $Cmd += " --top-k $($S.top_k)" }
    if ($S.presence_penalty -ne $null) { $Cmd += " --presence-penalty $($S.presence_penalty)" }
    if ($S.top_p -ne $null) { $Cmd += " --top-p $($S.top_p)" }
    if ($S.min_p -ne $null) { $Cmd += " --min-p $($S.min_p)" }
    if ($S.seed -ne $null) { $Cmd += " --seed $($S.seed)" }
    if ($S.jinja) { $Cmd += " --jinja" }
    if ($S.alias) { $Cmd += " --alias $($S.alias)" }
    if ($S.extra_flags) { $Cmd += " $($S.extra_flags)" }

    Write-Dim "Command:"
    Write-Host "  $Cmd" -ForegroundColor DarkGray
    Write-Host ""

    Write-Step "Killing any existing server..."
    Invoke-Stream "pkill -f llama-server 2>/dev/null; echo done"

    Write-Step "Starting server in background..."
    Invoke-Stream "nohup $Cmd > /workspace/server.log 2>&1 & echo SERVER_PID=`$!"

    Write-Step "Waiting for model to load..."
    Write-Host ""

    # Tail the log live until we see "server is listening" or an error
    $timeout = 120
    $elapsed = 0
    $interval = 3
    $ready = $false

    while ($elapsed -lt $timeout) {
        Start-Sleep -Seconds $interval
        $elapsed += $interval

        $lines = Invoke-Capture "tail -8 /workspace/server.log 2>/dev/null"

        foreach ($line in $lines) {
            $trimmed = $line.Trim()
            if ($trimmed -match "loading model|load_model|ggml_cuda_init|LLAMA_FILE|print_info|system info|main: model|main: server|error|Error|failed") {
                # Strip the SSH welcome noise
                if ($trimmed -notmatch "Welcome|vast.ai|authentication") {
                    $color = "DarkGray"
                    if ($trimmed -match "server is listening") { $color = "Green" }
                    if ($trimmed -match "error|Error|failed") { $color = "Red" }
                    Write-Host "  $trimmed" -ForegroundColor $color
                }
            }
        }

        $allLines = $lines -join ""
        if ($allLines -match "server is listening") {
            $ready = $true
            break
        }
        if ($allLines -match "error.*loading|failed to load") {
            Write-Fail "Model failed to load. Check /workspace/server.log"
            break
        }
    }

    Write-Host ""
    if ($ready) {
        Write-Ok "Server ready in $(Get-Elapsed $t)"
    } else {
        Write-Warn "Server may still be loading (timed out after ${timeout}s)"
        Write-Dim "Run this to check: ssh ... 'tail -f /workspace/server.log'"
    }
}

# ── Step 4: SSH tunnel ──────────────────────────────────────────────────
function Step-Tunnel {
    Write-Section "SETTING UP SSH TUNNEL"

    # Kill existing tunnel
    $Existing = Get-NetTCPConnection -LocalPort $LocalPort -ErrorAction SilentlyContinue
    if ($Existing) {
        $Existing | ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        Write-Step "Killed existing tunnel on port $LocalPort"
        Start-Sleep -Seconds 2
    }

    $Target = Get-SshTarget
    Write-Step "Forwarding localhost:$LocalPort -> remote:$RemotePort"
    Write-Dim "SSH: $($Target.Host):$($Target.Port)"

    Start-Process -FilePath "ssh" -ArgumentList @(
        "-i", $SshKey,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=5",
        "-N",
        "-L", "$LocalPort`:localhost:$RemotePort",
        "-p", $Target.Port,
        "root@$($Target.Host)"
    ) -WindowStyle Hidden

    # Wait and verify tunnel port is listening
    $retries = 0
    while ($retries -lt 5) {
        Start-Sleep -Seconds 2
        $Check = Get-NetTCPConnection -LocalPort $LocalPort -ErrorAction SilentlyContinue
        if ($Check) {
            Write-Ok "Tunnel active: http://localhost:$LocalPort"
            # Verify the full chain works (tunnel -> server)
            Start-Sleep -Seconds 2
            try {
                $Health = Invoke-RestMethod -Uri "http://localhost:$LocalPort/health" -TimeoutSec 10
                Write-Ok "Server health check: $($Health.status)"
            } catch {
                Write-Warn "Server not responding through tunnel (may still be loading)"
            }
            return
        }
        $retries++
    }

    Write-Fail "Tunnel failed to start. Check SSH connection."
    exit 1
}

# ── Status check ────────────────────────────────────────────────────────
function Step-Status {
    Write-Section "STATUS"

    # Check tunnel
    $Tunnel = Get-NetTCPConnection -LocalPort $LocalPort -ErrorAction SilentlyContinue
    if ($Tunnel) {
        Write-Ok "Tunnel: http://localhost:$LocalPort -> remote:$RemotePort"
        try {
            $Health = Invoke-RestMethod -Uri "http://localhost:$LocalPort/health" -TimeoutSec 5
            Write-Ok "Server health: $($Health.status)"
        } catch {
            Write-Warn "Server not responding on localhost:$LocalPort"
        }
    } else {
        Write-Warn "No tunnel on port $LocalPort"
    }

    # Check remote
    try {
        Write-Step "Checking remote instance..."
        $Target = Get-SshTarget
        $GpuInfo = Invoke-Capture "nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu --format=csv,noheader" 2>$null
        if ($GpuInfo) {
            Write-Dim "GPU: $($GpuInfo -join ' ')"
        }

        $DiskInfo = Invoke-Capture "df -h /workspace --output=used,avail | tail -1" 2>$null
        if ($DiskInfo) {
            Write-Dim "Disk: $($DiskInfo -join ' ')"
        }

        $Proc = Invoke-Capture "ps aux | grep '[l]lama-server'" 2>$null
        if ($Proc) {
            Write-Dim "Server: RUNNING"
        } else {
            Write-Dim "Server: NOT RUNNING"
        }
    } catch {
        Write-Warn "Cannot reach instance"
    }
}

# ── Stop everything ─────────────────────────────────────────────────────
function Step-Stop {
    Write-Section "STOPPING"

    # Kill tunnel
    $Existing = Get-NetTCPConnection -LocalPort $LocalPort -ErrorAction SilentlyContinue
    if ($Existing) {
        $Existing | ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        Write-Ok "Tunnel killed"
    } else {
        Write-Dim "No tunnel running"
    }

    # Kill remote server
    try {
        $Result = Invoke-Capture "pkill -f llama-server 2>/dev/null && echo killed || echo not-running"
        Write-Ok "Remote server: $($Result -join '')"
    } catch {
        Write-Warn "Cannot reach instance"
    }
}

# ── Check config ────────────────────────────────────────────────────────
function Confirm-Config {
    $NeedsConfig = (-not $ProxyPort) -and (-not $DirectPort)
    if ($NeedsConfig) {
        Write-Host ""
        Write-Fail "No SSH connection configured in config.json."
        Write-Host ""
        Write-Host "  Set ONE of these under `"ssh`":" -ForegroundColor Yellow
        Write-Host '    "proxy_host": "ssh7.vast.ai", "proxy_port": 28313' -ForegroundColor DarkGray
        Write-Host '    "direct_ip": "1.2.3.4", "direct_port": 40066' -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  Find these on: Vast.ai instance page -> SSH / Connect" -ForegroundColor DarkGray
        Write-Host ""
        exit 1
    }

    $Target = Get-SshTarget
    Write-Dim "SSH: $($Target.Host):$($Target.Port)"
    Write-Dim "Model: $($Config.model.filename)"
    Write-Dim "Server: http://localhost:$LocalPort -> remote:$RemotePort"
    Write-Host ""
}

# ── Main ────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  llama-deploy" -ForegroundColor Magenta
Write-Host "  ═════════════" -ForegroundColor Magenta
Write-Host ""

Confirm-Config

if ($Stop) { Step-Stop; exit 0 }
if ($Status) { Step-Status; exit 0 }
if ($TunnelOnly) { Step-Tunnel; exit 0 }
if ($ServerOnly) {
    Step-StartServer
    Step-Tunnel
    Step-Status
    exit 0
}

$TotalStart = Get-Date

if (-not $SkipBuild) { Step-Build }
if (-not $SkipModel) { Step-DownloadModel }

Step-StartServer
Step-Tunnel

Write-Host ""
Write-Host "  ════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  DONE in $(Get-Elapsed $TotalStart)" -ForegroundColor Green
Write-Host "  Model: http://localhost:$LocalPort" -ForegroundColor Green
Write-Host "  ════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""

Step-Status
