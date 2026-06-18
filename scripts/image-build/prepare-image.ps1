<#
.SYNOPSIS
  Install the FoodAssistant first-boot provisioner onto an already-flashed SD
  card boot partition (Windows equivalent of prepare-image.sh --boot-dir).

.DESCRIPTION
  After you flash Raspberry Pi OS Lite (64-bit) to a card, its small FAT boot
  partition shows up as a drive letter in Windows. This script copies the
  provisioner payload onto it and wires cmdline.txt so the box configures
  itself on first boot. It does NOT flash the card; use Raspberry Pi Imager or
  balenaEtcher for that.

.PARAMETER BootDrive
  Drive letter of the card's boot partition, e.g. D: or E:. Look in File
  Explorer for a small (~256 MB) drive labelled "bootfs".

.PARAMETER Config
  Path to the appliance config to install. Defaults to image\config.env in the
  repo. Edit that file first to set timezone, hostname, kiosk, etc.

.EXAMPLE
  .\scripts\image-build\prepare-image.ps1 -BootDrive D:

.EXAMPLE
  .\scripts\image-build\prepare-image.ps1 -BootDrive E: -Config C:\my\config.env
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BootDrive,

    [string]$Config
)

$ErrorActionPreference = "Stop"

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Die($msg)  { Write-Host "Error: $msg" -ForegroundColor Red; exit 1 }

# Resolve repo layout relative to this script.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Resolve-Path (Join-Path $ScriptDir "..\..")
if (-not $Config) { $Config = Join-Path $RepoRoot "image\config.env" }

if (-not (Test-Path $Config)) { Die "Config not found: $Config" }

# Normalise the drive to the form "D:\".
$Boot = $BootDrive.TrimEnd('\', ':') + ":\"
if (-not (Test-Path $Boot)) {
    Die "Boot drive not found: $Boot  (check the drive letter in File Explorer)"
}

# Sanity check: a Pi boot partition has cmdline.txt and config.txt. Warn loudly
# if this looks like the wrong drive, so we don't scribble on the user's data.
if (-not (Test-Path (Join-Path $Boot "cmdline.txt"))) {
    Die "No cmdline.txt on $Boot. That does not look like a Pi boot partition. Aborting so nothing is overwritten on the wrong drive."
}

$Assets = @(
    "firstboot.sh",
    "foodassistant-firstrun.sh",
    "foodassistant-firstboot.service",
    "docker-compose.appliance.yml"
)

Say "Installing payload into ${Boot}foodassistant-setup"
$SetupDir = Join-Path $Boot "foodassistant-setup"
New-Item -ItemType Directory -Path $SetupDir -Force | Out-Null

foreach ($a in $Assets) {
    $src = Join-Path $ScriptDir $a
    if (-not (Test-Path $src)) { Die "Missing asset: $src" }
    Copy-Item $src (Join-Path $SetupDir $a) -Force
}

# Config goes into the setup folder and, for easy editing, at the top level.
Copy-Item $Config (Join-Path $SetupDir "config.env") -Force
$TopConfig = Join-Path $Boot "foodassistant.config.env"
if (-not (Test-Path $TopConfig)) {
    Copy-Item $Config $TopConfig -Force
}

# Place our bootstrap script on the boot partition under its own name. Do NOT
# overwrite any firstrun.sh placed by Raspberry Pi Imager (wifi/SSH/user-creation).
$ImagerFirstrun = Join-Path $Boot "firstrun.sh"
if (Test-Path $ImagerFirstrun) {
    Say "NOTE: Raspberry Pi Imager's firstrun.sh is present -- NOT overwriting it."
    Say "      Only adding foodassistant-firstrun.sh alongside it."
}
Copy-Item (Join-Path $ScriptDir "foodassistant-firstrun.sh") (Join-Path $Boot "foodassistant-firstrun.sh") -Force

# Wire cmdline.txt (a single line) to run foodassistant-firstrun.sh once.
# Idempotent: skip if already present. We leave any existing firstrun.sh hook
# (placed by Raspberry Pi Imager) untouched. Write back without a BOM or
# trailing newline so the kernel command line stays clean.
$CmdlinePath = Join-Path $Boot "cmdline.txt"
$cmdline = [System.IO.File]::ReadAllText($CmdlinePath)
$cmdline = $cmdline.Trim()
$hook = "systemd.run=/boot/firmware/foodassistant-firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target"
if ($cmdline -match "systemd\.run=.*foodassistant-firstrun\.sh") {
    Say "cmdline.txt already wired for foodassistant-firstrun.sh; leaving as-is"
} else {
    Say "Wiring cmdline.txt to run foodassistant-firstrun.sh on first boot"
    $cmdline = "$cmdline $hook"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($CmdlinePath, $cmdline, $utf8NoBom)
}

Say "Payload installed. Safely eject the card and boot your device."
