#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Prepares Windows 11 with WSL2 + Ubuntu 24.04 for MediaCat development.

.DESCRIPTION
    Idempotent. Safe to re-run. Performs:
      1. Enables Virtual Machine Platform and WSL features if missing.
      2. Installs WSL (Store-based, modern path) if absent.
      3. Sets WSL default version to 2.
      4. Installs the Ubuntu-24.04 distro if missing.
      5. Writes /etc/wsl.conf inside the distro to enable systemd.
      6. Shuts WSL down so the new config takes effect.

    After this script completes, launch "Ubuntu-24.04" from Start, complete
    the initial user setup, then inside that shell run:
        ./scripts/ubuntu-bootstrap.sh

.NOTES
    Requires Windows 11 22H2 or later. Exits non-zero on hard failure.
#>

[CmdletBinding()]
param(
    [string] $Distro = 'Ubuntu-24.04'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Write-Step($msg) { Write-Host "[wsl2-prepare] $msg" -ForegroundColor Cyan }
function Write-Ok  ($msg) { Write-Host "[wsl2-prepare] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[wsl2-prepare] $msg" -ForegroundColor Yellow }

# --- 1. Feature enablement ----------------------------------------------------
Write-Step 'Checking Windows optional features...'
$features = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')
foreach ($f in $features) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $f).State
    if ($state -ne 'Enabled') {
        Write-Step "Enabling feature: $f"
        Enable-WindowsOptionalFeature -Online -FeatureName $f -NoRestart | Out-Null
    } else {
        Write-Ok "Feature already enabled: $f"
    }
}

# --- 2. WSL install (modern Store path) --------------------------------------
Write-Step 'Ensuring WSL is installed...'
$wslStatus = & wsl.exe --status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Step 'Installing WSL (this may prompt for reboot)...'
    & wsl.exe --install --no-distribution
    if ($LASTEXITCODE -ne 0) {
        throw "wsl --install failed with exit code $LASTEXITCODE"
    }
} else {
    Write-Ok 'WSL present.'
}

# --- 3. Default version 2 -----------------------------------------------------
Write-Step 'Setting WSL default version to 2...'
& wsl.exe --set-default-version 2 | Out-Null

# --- 4. Update WSL kernel -----------------------------------------------------
Write-Step 'Updating WSL kernel...'
& wsl.exe --update | Out-Null

# --- 5. Install Ubuntu distro -------------------------------------------------
$installedDistros = (& wsl.exe --list --quiet) -replace "`0", '' -split "`r?`n" | Where-Object { $_ }
if ($installedDistros -notcontains $Distro) {
    Write-Step "Installing distro: $Distro"
    & wsl.exe --install -d $Distro
    if ($LASTEXITCODE -ne 0) {
        throw "wsl --install -d $Distro failed with exit code $LASTEXITCODE"
    }
    Write-Warn "A window should have opened for initial $Distro setup."
    Write-Warn "Complete user creation there, then re-run this script to apply wsl.conf."
    exit 0
} else {
    Write-Ok "Distro already installed: $Distro"
}

# --- 6. Enable systemd inside the distro -------------------------------------
Write-Step 'Ensuring systemd is enabled inside the distro...'
$wslConf = @"
[boot]
systemd=true

[network]
generateResolvConf=true

[interop]
appendWindowsPath=false
"@
# Write via a heredoc executed inside the distro as root.
$tmpPath = [System.IO.Path]::GetTempFileName()
Set-Content -LiteralPath $tmpPath -Value $wslConf -Encoding utf8 -NoNewline
try {
    # Convert Windows temp path to /mnt/... path.
    $wslTmp = & wsl.exe -d $Distro -u root -- wslpath -a $tmpPath
    & wsl.exe -d $Distro -u root -- install -m 0644 $wslTmp /etc/wsl.conf
} finally {
    Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
}

# --- 7. Shutdown so /etc/wsl.conf takes effect -------------------------------
Write-Step 'Shutting WSL down so the new config is picked up...'
& wsl.exe --shutdown

Write-Ok 'Done.'
Write-Host ''
Write-Host 'Next:' -ForegroundColor White
Write-Host "  1. Launch $Distro from Start." -ForegroundColor White
Write-Host '  2. cd to the repo root inside WSL.' -ForegroundColor White
Write-Host '  3. ./scripts/ubuntu-bootstrap.sh' -ForegroundColor White
