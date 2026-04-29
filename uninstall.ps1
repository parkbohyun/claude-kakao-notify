<#
.SYNOPSIS
    claude-kakao-notify uninstaller.
.DESCRIPTION
    - Removes notify.py-related hooks and the mcp__notify__notify permission
      from ~/.claude/settings.json
    - Removes mcpServers.notify from ~/.claude.json
    - Deletes hook / MCP / slash-command files
    - Preserves notify-api.env (manual delete required)
    Edited config files are backed up to *.bak.<timestamp>.
#>

$ErrorActionPreference = 'Stop'

$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir     = Join-Path $ScriptDir 'tools'
$UserHome     = $env:USERPROFILE
$ClaudeDir    = Join-Path $UserHome '.claude'
$ClaudeJson   = Join-Path $UserHome '.claude.json'
$SettingsFile = Join-Path $ClaudeDir 'settings.json'
$EnvFile      = Join-Path $ClaudeDir 'notify-api.env'
$MergeScript  = Join-Path $ToolsDir 'merge_config.py'

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-OK([string]$m)   { Write-Host "    OK: $m" -ForegroundColor Green }
function Write-Note([string]$m) { Write-Host "    note: $m" -ForegroundColor Yellow }

# Detect Python (for merge script)
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

# 1. settings.json
if (Test-Path $SettingsFile) {
    Write-Step 'Cleaning settings.json'
    $bak = "$SettingsFile.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -Force $SettingsFile $bak
    Write-Note "backup: $bak"
    if ($pythonExe) {
        & $pythonExe $MergeScript 'settings-rm' $SettingsFile
        if ($LASTEXITCODE -ne 0) { Write-Warning "settings.json clean failed (exit=$LASTEXITCODE)" }
        else { Write-OK $SettingsFile }
    } else {
        Write-Warning 'Python not found - settings.json must be edited manually.'
    }
}

# 2. ~/.claude.json
if (Test-Path $ClaudeJson) {
    Write-Step 'Cleaning ~/.claude.json'
    $bak = "$ClaudeJson.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -Force $ClaudeJson $bak
    Write-Note "backup: $bak"
    if ($pythonExe) {
        & $pythonExe $MergeScript 'mcp-rm' $ClaudeJson
        if ($LASTEXITCODE -ne 0) { Write-Warning "~/.claude.json clean failed (exit=$LASTEXITCODE)" }
        else { Write-OK $ClaudeJson }
    } else {
        Write-Warning 'Python not found - ~/.claude.json must be edited manually.'
    }
}

# 3. Remove files
Write-Step 'Deleting installed files'
$targets = @(
    (Join-Path $ClaudeDir 'hooks\notify.py'),
    (Join-Path $ClaudeDir 'mcp\notify-mcp\server.py'),
    (Join-Path $ClaudeDir 'commands\rcd.md')
)
foreach ($t in $targets) {
    if (Test-Path $t) {
        Remove-Item -Force $t
        Write-OK $t
    }
}

# Empty subdir cleanup
$cleanup = @(
    (Join-Path $ClaudeDir 'mcp\notify-mcp'),
    (Join-Path $ClaudeDir 'commands'),
    (Join-Path $ClaudeDir 'hooks')
)
foreach ($d in $cleanup) {
    if (Test-Path $d) {
        $items = Get-ChildItem -Force $d -ErrorAction SilentlyContinue
        if (-not $items) {
            Remove-Item -Force $d
            Write-OK "removed empty dir: $d"
        }
    }
}

# 4. .env note
if (Test-Path $EnvFile) {
    Write-Note "notify-api.env preserved (secret protection). Delete manually if desired: $EnvFile"
}

Write-Host ''
Write-Host 'Uninstall complete.' -ForegroundColor Green
