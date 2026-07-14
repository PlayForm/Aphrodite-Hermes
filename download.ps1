#!/usr/bin/env pwsh
# aphrodite - download prebuilt binary from GitHub Releases (PowerShell)
#
# Native equivalent of download.sh - runs the same on Windows PowerShell (5.1+)
# and cross-platform PowerShell (pwsh 7+ on macOS/Linux). Prefer this over
# download.sh on native Windows (no bash/Git Bash/WSL required).
#
# Usage: pwsh ./download.ps1 [-Version <version>] [-Target <target-triple>]
#   -Version: auto-detected from BINARY_VERSION / Cargo.toml / GitHub API if omitted
#   -Target:  auto-detected from the current OS + architecture if omitted

[CmdletBinding()]
param(
	[string]$Version,
	[string]$Target
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 03-F18: default BinaryDir relative to the script's own directory, not
# $PWD (wherever this script happened to be invoked from) - see download.sh's
# matching fix for the same bug.
$BinaryDir = if ($env:BINARY_DIR) { $env:BINARY_DIR } else { Join-Path $ScriptDir 'binaries' }
$Repo = if ($env:REPO) { $env:REPO } else { 'PlayForm/Aphrodite' }

# ── Auto-detect version ──────────────────────────────────────────────
function Resolve-Version {
	param([string]$Explicit)
	if ($Explicit) { return $Explicit }

	# 1. BINARY_VERSION file - deployed with the plugin, always correct
	$versionFile = Join-Path $ScriptDir 'BINARY_VERSION'
	if (Test-Path $versionFile) {
		$v = (Get-Content $versionFile -First 1).Trim()
		if ($v) { return $v }
	}

	# 2. Cargo.toml - for developers with the full monorepo
	$candidates = @(
		(Join-Path $ScriptDir '../../crates/aphrodite/Cargo.toml'),
		(Join-Path $ScriptDir '../../crates/aphrodite-hermes/Cargo.toml'),
		(Join-Path $ScriptDir '../../../crates/aphrodite/Cargo.toml')
	)
	foreach ($f in $candidates) {
		if (Test-Path $f) {
			$line = Select-String -Path $f -Pattern '^version' | Select-Object -First 1
			if ($line) {
				$v = ($line.Line -split '"')[1]
				if ($v) { return $v }
			}
		}
	}

	# 3. GitHub API - query the latest release tag (needs network, but reliable)
	try {
		$release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -TimeoutSec 30 -ErrorAction Stop
		if ($release.tag_name -match 'Aphrodite/v(.+)$') { return $Matches[1] }
	} catch {
		# fall through to the error below
	}

	throw "could not determine binary version - pass -Version explicitly or create a BINARY_VERSION file in $ScriptDir"
}

# ── Auto-detect platform ─────────────────────────────────────────────
function Resolve-Target {
	param([string]$Explicit)
	if ($Explicit) { return $Explicit }

	$arch = if ([Environment]::Is64BitOperatingSystem) {
		$procArch = $env:PROCESSOR_ARCHITECTURE
		if ($procArch -eq 'ARM64' -or (Get-Variable -Name IsMacOS -ErrorAction SilentlyContinue) -and $IsMacOS -and (uname -m) -eq 'arm64') {
			'aarch64'
		} else {
			'x86_64'
		}
	} else {
		'x86_64'
	}

	# $IsWindows/$IsMacOS/$IsLinux only exist on PowerShell Core (pwsh 6+);
	# Windows PowerShell 5.1 has no such variables, so absence means Windows.
	$onWindows = -not (Test-Path variable:IsWindows) -or $IsWindows
	$onMacOS = (Test-Path variable:IsMacOS) -and $IsMacOS
	$onLinux = (Test-Path variable:IsLinux) -and $IsLinux

	if ($onWindows) { return "$arch-pc-windows-msvc" }
	if ($onMacOS) { return "$arch-apple-darwin" }
	if ($onLinux) { return "$arch-unknown-linux-gnu" }
	throw 'unsupported OS - pass -Target explicitly'
}

$BinVersion = Resolve-Version -Explicit $Version
$ResolvedTarget = Resolve-Target -Explicit $Target
$V = $BinVersion.TrimStart('v')
$BaseUrl = "https://github.com/$Repo/releases/download/Aphrodite%2Fv$V"

New-Item -ItemType Directory -Force -Path $BinaryDir | Out-Null

# ── Asset + local names per platform ─────────────────────────────────
#   binary: the proxy executable (loaded as a subprocess)
#   dylib:  libaphrodite_hermes.* - the cdylib the Python plugin loads via ctypes
$BinaryAsset = "aphrodite-$ResolvedTarget"
if ($ResolvedTarget -like '*windows*') {
	$BinaryAsset = "$BinaryAsset.exe"
	$BinaryPath = Join-Path $BinaryDir 'aphrodite.exe'
	$DylibAsset = "libaphrodite_hermes-$ResolvedTarget.dll"
	$DylibDest = Join-Path $BinaryDir 'aphrodite_hermes.dll'
} elseif ($ResolvedTarget -like '*apple*') {
	$BinaryPath = Join-Path $BinaryDir 'aphrodite'
	$DylibAsset = "libaphrodite_hermes-$ResolvedTarget.dylib"
	$DylibDest = Join-Path $BinaryDir 'libaphrodite_hermes.dylib'
} else {
	$BinaryPath = Join-Path $BinaryDir 'aphrodite'
	$DylibAsset = "libaphrodite_hermes-$ResolvedTarget.so"
	$DylibDest = Join-Path $BinaryDir 'libaphrodite_hermes.so'
}

# SHA-256 checksum verification (mirrors download.sh's verify_checksum) - a
# missing sums file (e.g. a release cut before this was added) degrades to a
# loud warning rather than a hard failure, so older tags remain installable.
$SumsAsset = "SHA256SUMS-$ResolvedTarget.txt"
$SumsEntries = $null
try {
	$sumsText = Invoke-RestMethod -Uri "$BaseUrl/$SumsAsset" -TimeoutSec 30 -ErrorAction Stop
	$SumsEntries = @{}
	foreach ($line in ($sumsText -split "`n")) {
		if ($line -match '^([0-9a-fA-F]{64})\s+(\S+)') { $SumsEntries[$Matches[2]] = $Matches[1].ToLower() }
	}
	Write-Host "  OK fetched $SumsAsset"
} catch {
	Write-Host "WARNING: $SumsAsset not found - skipping checksum verification for this release"
}

# Fetch a release asset and verify it's a real native binary (PE/Mach-O/ELF)
# and (when a sums file was found) its SHA-256 checksum, restoring any prior
# copy on failure.
function Get-ValidatedAsset {
	param([string]$Asset, [string]$Dest)

	$url = "$BaseUrl/$Asset"
	Write-Host "  $Asset -> $Dest"
	$backup = "$Dest.bak"
	if (Test-Path $Dest) { Move-Item -Force $Dest $backup }

	try {
		# -TimeoutSec bounds the whole request (stall/hang guard, not a
		# realistic-bandwidth budget - these binaries run ~10-40MB).
		Invoke-WebRequest -Uri $url -OutFile $Dest -TimeoutSec 120 -ErrorAction Stop
	} catch {
		if (Test-Path $backup) { Move-Item -Force $backup $Dest }
		throw "download failed: $url ($_)"
	}

	$size = (Get-Item $Dest).Length
	if ($size -eq 0) {
		if (Test-Path $backup) { Move-Item -Force $backup $Dest }
		throw "downloaded $Asset is empty"
	}

	$bytes = [System.IO.File]::ReadAllBytes($Dest)[0..3]
	$magic = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
	$valid = switch ($magic) {
		'7f454c46' { $true }                                    # ELF
		{ $_ -in @('cffaedfe', 'feedfacf', 'cefaedfe', 'cafebabe') } { $true } # Mach-O
		{ $_.StartsWith('4d5a') } { $true }                     # PE
		default { $false }
	}
	if (-not $valid) {
		if (Test-Path $backup) { Move-Item -Force $backup $Dest }
		throw "$Asset has invalid magic bytes: $magic"
	}

	if ($SumsEntries -and $SumsEntries.ContainsKey($Asset)) {
		$actual = (Get-FileHash $Dest -Algorithm SHA256).Hash.ToLower()
		if ($actual -ne $SumsEntries[$Asset]) {
			if (Test-Path $backup) { Move-Item -Force $backup $Dest }
			throw "checksum mismatch for $Asset`: expected $($SumsEntries[$Asset]), got $actual"
		}
		Write-Host "  OK $Asset checksum verified"
	} elseif ($SumsEntries) {
		Write-Host "WARNING: $Asset has no entry in $SumsAsset - skipping checksum check for this asset"
	}

	Remove-Item -Force $backup -ErrorAction SilentlyContinue
	Write-Host "  OK $Asset ($size bytes)"
}

Write-Host "aphrodite: downloading v$BinVersion for $ResolvedTarget from $BaseUrl"
Get-ValidatedAsset -Asset $BinaryAsset -Dest $BinaryPath
if ($IsWindows -or (-not (Test-Path variable:IsWindows))) {
	# no chmod needed on Windows
} else {
	& chmod +x $BinaryPath
}
Get-ValidatedAsset -Asset $DylibAsset -Dest $DylibDest

Write-Host "aphrodite v$BinVersion installed: $BinaryPath + $DylibDest"
