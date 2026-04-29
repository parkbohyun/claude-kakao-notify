<#
.SYNOPSIS
    claude-kakao-notify installer (Windows + PowerShell 5.1+).
.DESCRIPTION
    - Detects Python (3.10+) and installs the 'mcp' package
    - Copies hook / MCP / slash-command files into ~/.claude/
    - Prompts for NAS host/port/API key and writes ~/.claude/notify-api.env
    - Merges hooks + permissions into ~/.claude/settings.json
    - Merges mcpServers.notify into ~/.claude.json
    Existing config files are backed up to *.bak.<timestamp> before changes.
#>

[CmdletBinding()]
param(
    [string]$NasHost,
    [string]$NasPort,
    [string]$ApiKey,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$FilesDir     = Join-Path $ScriptDir 'files'
$ToolsDir     = Join-Path $ScriptDir 'tools'
$UserHome     = $env:USERPROFILE
$ClaudeDir    = Join-Path $UserHome '.claude'
$ClaudeJson   = Join-Path $UserHome '.claude.json'
$EnvFile      = Join-Path $ClaudeDir 'notify-api.env'
$SettingsFile = Join-Path $ClaudeDir 'settings.json'
$MergeScript  = Join-Path $ToolsDir 'merge_config.py'

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-OK([string]$m)   { Write-Host "    OK: $m" -ForegroundColor Green }
function Write-Note([string]$m) { Write-Host "    note: $m" -ForegroundColor Yellow }

# 1. Detect Python
Write-Step 'Detecting Python'
$pythonExe = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c) {
        $resolved = $c.Source
        if (-not $resolved -and $c.Path) { $resolved = $c.Path }
        if ($resolved -and (Test-Path $resolved)) {
            $pythonExe = $resolved
            break
        }
    }
}
if (-not $pythonExe) {
    throw 'Python not found. Install 3.10+ from https://www.python.org/downloads/ and re-run.'
}
$pyVer = & $pythonExe --version 2>&1
Write-OK "$pyVer @ $pythonExe"

# 2. Install mcp package
Write-Step 'Installing mcp package (user scope)'
& $pythonExe -m pip install --quiet --upgrade --user mcp
if ($LASTEXITCODE -ne 0) { throw "pip install mcp failed (exit=$LASTEXITCODE)" }
Write-OK 'mcp installed'

# 3. Create directories
Write-Step 'Preparing ~/.claude directories'
$dirs = @(
    $ClaudeDir,
    (Join-Path $ClaudeDir 'hooks'),
    (Join-Path $ClaudeDir 'mcp\notify-mcp'),
    (Join-Path $ClaudeDir 'commands')
)
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
}
Write-OK $ClaudeDir

# 4. Copy files
Write-Step 'Copying files'
$copies = @(
    @{ src = 'hooks\notify.py';            dst = 'hooks\notify.py' },
    @{ src = 'mcp\notify-mcp\server.py';   dst = 'mcp\notify-mcp\server.py' },
    @{ src = 'commands\rcd.md';            dst = 'commands\rcd.md' }
)
foreach ($c in $copies) {
    $srcPath = Join-Path $FilesDir $c.src
    $dstPath = Join-Path $ClaudeDir $c.dst
    if (-not (Test-Path $srcPath)) { throw "Source missing: $srcPath" }
    Copy-Item -Force $srcPath $dstPath
    Write-OK $c.dst
}

# 5. notify-api.env (read existing as defaults, then prompt)
Write-Step 'Configuring notify-api.env'
$existingHost = ''
$existingPort = '8002'
$existingKey  = ''
if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile)) {
        $t = $line.Trim()
        if ($t -match '^\s*NOTIFY_API_HOST\s*=\s*(.+)$') { $existingHost = $matches[1].Trim() }
        elseif ($t -match '^\s*NOTIFY_API_PORT\s*=\s*(.+)$') { $existingPort = $matches[1].Trim() }
        elseif ($t -match '^\s*NOTIFY_API_KEY\s*=\s*(.+)$')  { $existingKey  = $matches[1].Trim() }
        elseif ($t -match '^\s*NOTIFY_API_URL\s*=\s*https?://([^:/\s]+)(?::(\d+))?') {
            if (-not $existingHost) { $existingHost = $matches[1] }
            if ($matches[2] -and $existingPort -eq '8002') { $existingPort = $matches[2] }
        }
    }
}

# host
if ($NasHost) {
    $inHost = $NasHost
} elseif ($NonInteractive) {
    $inHost = $existingHost
} else {
    $prompt = if ($existingHost) { "NAS host or IP [$existingHost]" } else { 'NAS host or IP' }
    $r = Read-Host $prompt
    $inHost = if ($r) { $r } else { $existingHost }
}
if (-not $inHost) { throw 'NAS host is required.' }

# port
if ($NasPort) {
    $inPort = $NasPort
} elseif ($NonInteractive) {
    $inPort = $existingPort
} else {
    $r = Read-Host "NAS port [$existingPort]"
    $inPort = if ($r) { $r } else { $existingPort }
}

# key
if ($ApiKey) {
    $inKey = $ApiKey
} elseif ($NonInteractive) {
    $inKey = $existingKey
} else {
    $promptK = if ($existingKey) { 'API key (Enter to keep existing)' } else { 'API key' }
    $sec = Read-Host $promptK -AsSecureString
    if ($sec.Length -eq 0) {
        $inKey = $existingKey
    } else {
        $inKey = [System.Net.NetworkCredential]::new('', $sec).Password
    }
}
if (-not $inKey) { throw 'API key is required.' }

$envBody = "NOTIFY_API_HOST=$inHost`nNOTIFY_API_PORT=$inPort`nNOTIFY_API_KEY=$inKey`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($EnvFile, $envBody, $utf8NoBom)
Write-OK "notify-api.env (host=$inHost, port=$inPort)"

# 6. Merge settings.json
Write-Step 'Merging settings.json (hooks + permissions)'
if (Test-Path $SettingsFile) {
    $bak = "$SettingsFile.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -Force $SettingsFile $bak
    Write-Note "backup: $bak"
}
& $pythonExe $MergeScript 'settings-add' $SettingsFile
if ($LASTEXITCODE -ne 0) { throw "settings.json merge failed (exit=$LASTEXITCODE)" }
Write-OK $SettingsFile

# 7. Merge ~/.claude.json
Write-Step 'Merging ~/.claude.json (mcpServers.notify)'
if (Test-Path $ClaudeJson) {
    $bak = "$ClaudeJson.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -Force $ClaudeJson $bak
    Write-Note "backup: $bak"
}
& $pythonExe $MergeScript 'mcp-add' $ClaudeJson $pythonExe
if ($LASTEXITCODE -ne 0) { throw "~/.claude.json merge failed (exit=$LASTEXITCODE)" }
Write-OK $ClaudeJson

# 8. Done
Write-Host ''
Write-Host '========================================' -ForegroundColor Green
Write-Host '  Install complete' -ForegroundColor Green
Write-Host '========================================' -ForegroundColor Green
Write-Host ''
Write-Host 'Next:' -ForegroundColor Cyan
Write-Host '  - Start a new Claude Code session and confirm a KakaoTalk message arrives'
Write-Host '  - Run /remote-control then /rcd (or /rcd URL) to send the RC URL'
Write-Host ''
Write-Host 'Files:' -ForegroundColor Cyan
Write-Host "  $EnvFile"
Write-Host "  $SettingsFile"
Write-Host "  $ClaudeJson"
Write-Host ''
