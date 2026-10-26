<#
.SYNOPSIS
  Installs the busy-claude plugin (BUSY Bar status display) into Claude Code on Windows.

.DESCRIPTION
  irm https://raw.githubusercontent.com/nicoSWD/busybar-claude/main/install.ps1 | iex

  To pass options, run it as a script block:
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/nicoSWD/busybar-claude/main/install.ps1))) -Yes

  It runs only Claude Code's own commands (claude plugin ...). The password for a Wi-Fi bar is handed
  to Claude Code through a pipe, never on a command line.

.PARAMETER Yes
  Do not ask anything, use the defaults (USB bar, no statusline).
.PARAMETER DryRun
  Print what would run, change nothing.
.PARAMETER Statusline
  Also set up the optional statusline (plan usage on the READY screen).
.PARAMETER Uninstall
  Remove the plugin, the marketplace and the statusline wrapper.
#>
param(
  [switch]$Yes,
  [switch]$DryRun,
  [switch]$Statusline,
  [switch]$Uninstall
)

# Native commands write to stderr for ordinary messages; check $LASTEXITCODE instead of stopping.
$ErrorActionPreference = 'Continue'

$Repo = if ($env:BUSYBAR_REPO) { $env:BUSYBAR_REPO } else { 'nicoSWD/busybar-claude' }
$Marketplace = 'busybar-claude'
$Plugin = 'busy-claude'
$Id = "$Plugin@$Marketplace"

function Say($text) { Write-Host $text }
function Step($text) { Write-Host ""; Write-Host "==> $text" }
function Fail($text) { throw $text }  # caught at the bottom; `exit` would close the terminal under `irm | iex`

function Test-Interactive {
  return (-not $Yes) -and (-not $DryRun) -and [Environment]::UserInteractive -and (-not [Console]::IsInputRedirected)
}

function Ask($question, [bool]$defaultYes) {
  if (-not (Test-Interactive)) { return $defaultYes }
  $hint = if ($defaultYes) { 'Y/n' } else { 'y/N' }
  $reply = Read-Host "$question [$hint]"
  if ($reply -match '^[Yy]') { return $true }
  if ($reply -match '^[Nn]') { return $false }
  return $defaultYes
}

# Runs a native command, showing it first. Returns the exit code (0 in a dry run).
function Run([string]$file, [string[]]$arguments) {
  Say "+ $file $($arguments -join ' ')"
  if ($DryRun) { return 0 }
  & $file @arguments | Out-Host
  return $LASTEXITCODE
}

# True when the given python executable really is Python 3.9 or newer (the Store alias stub is not).
function Test-Python([string]$exe) {
  try {
    $version = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    return ($LASTEXITCODE -eq 0) -and ([version]$version -ge [version]'3.9')
  } catch {
    return $false
  }
}

function Get-PluginScript {
  $base = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
  try {
    $installed = Get-Content -Raw (Join-Path $base 'plugins\installed_plugins.json') | ConvertFrom-Json
    $path = $installed.plugins.PSObject.Properties[$Id].Value[0].installPath
    $script = Join-Path $path 'scripts\busybar_status.py'
    if (Test-Path $script) { return $script }
  } catch { }
  return $null
}

function Main {
  if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Fail 'Claude Code was not found. Install it first: https://claude.com/claude-code'
  }

  if ($Uninstall) {
    Step 'Removing busy-claude'
    $script = Get-PluginScript
    if ((-not $DryRun) -and $script) { & python3 $script statusline-uninstall }
    Run 'claude' @('plugin', 'uninstall', $Id) | Out-Null
    Run 'claude' @('plugin', 'marketplace', 'remove', $Marketplace) | Out-Null
    Say 'Done.'
    return
  }

  Step 'Checking requirements'
  # The plugin's hooks run `python3` directly, so it must be a real python3.exe on PATH.
  if (-not (Test-Python 'python3')) {
    $found = $null
    foreach ($candidate in @('python', 'py')) {
      if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $probe = if ($candidate -eq 'py') { & py -3 -c 'import sys; print(sys.executable)' 2>$null } else { & python -c 'import sys; print(sys.executable)' 2>$null }
        if ($probe -and (Test-Path $probe) -and (Test-Python $probe)) { $found = $probe; break }
      }
    }
    if (-not $found) {
      Fail "Python 3.9 or newer was not found. Install it (for example: winget install Python.Python.3.12), open a new terminal and run this again."
    }
    Say "python3 is not available, but Python was found at $found."
    Say 'The plugin runs `python3`, so it needs a python3.exe next to it.'
    $target = Join-Path (Split-Path $found) 'python3.exe'
    if (Ask "Copy it to $target ?" $true) {
      if (-not $DryRun) { Copy-Item $found $target -Force }
      Say "+ Copy-Item $found $target"
      if ((-not $DryRun) -and (-not (Test-Python 'python3'))) {
        Say 'python3 still resolves to something else (probably the Microsoft Store alias).'
        Say 'Open Settings > Apps > Advanced app settings > App execution aliases and turn off "python3.exe".'
        Fail 'Then open a new terminal and run this installer again.'
      }
    } else {
      Fail 'Cannot continue without python3.exe. See the README section "Linux and Windows".'
    }
  }
  Say "claude: $((Get-Command claude).Source)"
  Say "python3: $(& python3 --version 2>&1)"
  $claudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
  if (Test-Path (Join-Path $claudeDir 'skills\busy-claude\.claude-plugin\plugin.json')) {
    Say 'warning: ~/.claude/skills/busy-claude also exists. Remove it, or every hook will run twice.'
  }

  Step "Adding the marketplace ($Repo)"
  if ($DryRun) {
    Run 'claude' @('plugin', 'marketplace', 'add', $Repo) | Out-Null
  } else {
    $out = & claude plugin marketplace add $Repo 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
      Say $out.Trim()
    } elseif ((& claude plugin marketplace list 2>&1 | Out-String) -match [regex]::Escape($Marketplace)) {
      Say 'Already added, refreshing it.'
      Run 'claude' @('plugin', 'marketplace', 'update', $Marketplace) | Out-Null
    } else {
      Say $out.Trim()
      Fail "could not add the marketplace $Repo"
    }
  }

  Step "Installing $Id"
  if ($DryRun) {
    Run 'claude' @('plugin', 'install', $Id, '--scope', 'user') | Out-Null
  } else {
    $out = & claude plugin install $Id --scope user 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
      Say $out.Trim()
      Say '("options not yet set" is fine: the defaults suit a bar on USB.)'
    } elseif ((& claude plugin list 2>&1 | Out-String) -match [regex]::Escape($Id)) {
      Say 'Already installed, updating it.'
      Run 'claude' @('plugin', 'update', $Id) | Out-Null
    } else {
      Say $out.Trim()
      Fail "could not install $Id"
    }
  }

  if (-not (Ask 'Is the BUSY Bar connected with a USB cable?' $true)) {
    Step 'Wi-Fi setup'
    Say 'Enable the Wi-Fi API once over USB first (README: Connect the BUSY Bar).'
    $address = (Read-Host 'Address of the bar (for example 192.168.1.20)').Trim()
    $secure = Read-Host 'API password (hidden)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($address -notmatch '://') { $address = "http://$address" }
    $address = $address.TrimEnd('/')
    if (-not $address.EndsWith('/api')) { $address = "$address/api" }
    $values = @{ DEVICE_URL = $address; AUTH_MODE = 'wifi' }
    if ($password) { $values.AUTH_TOKEN = $password }
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $values | ConvertTo-Json -Compress | & claude plugin configure $Id --values-stdin
    if ($LASTEXITCODE -ne 0) { Say "Could not save the settings. In Claude Code, run: /plugin configure $Id" }
  }

  $wantStatusline = $Statusline.IsPresent
  if (-not $wantStatusline) {
    $wantStatusline = Ask "Show your plan usage on the bar's READY screen (sets up the optional statusline)?" $false
  }
  if ($wantStatusline) {
    Step 'Setting up the statusline'
    if ($DryRun) {
      Say '+ python3 <installed plugin>\scripts\busybar_status.py statusline-install'
    } else {
      $script = Get-PluginScript
      if ($script) { & python3 $script statusline-install }
      else { Say 'Could not find the installed plugin; run /busy-claude:setup in Claude Code instead.' }
    }
  }

  Step 'Done'
  Say 'Plug the bar in, then start a new Claude Code session (or run /reload-plugins in an open one).'
  Say 'Run /busy-claude:setup there to check that everything is connected.'
}

$failed = $false
try {
  Main
} catch {
  Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
  $failed = $true
}
if ($failed -and $MyInvocation.MyCommand.Path) { exit 1 }  # a real script file may set the exit code; iex must not
