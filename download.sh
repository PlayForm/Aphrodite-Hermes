#!/usr/bin/env bash
# aphrodite - download prebuilt binary from GitHub Releases
# Usage: bash download.sh [version] [target-triple]
#   version: auto-detected from Cargo.toml (monorepo), falls back to plugin.yaml
#   target:  auto-detected from uname -sm

set -euo pipefail

BINARY_DIR="${BINARY_DIR:-binaries}"
REPO="${REPO:-PlayForm/Aphrodite}"
BIN_VERSION="${1:-}"
TARGET="${2:-}"

# ── Auto-detect version ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$BIN_VERSION" ]]; then
	# Resolve paths relative to the script itself, not cwd
	for f in "$SCRIPT_DIR/../../crates/aphrodite/Cargo.toml" \
	         "$SCRIPT_DIR/../../crates/aphrodite-hermes/Cargo.toml" \
	         "$SCRIPT_DIR/../../../crates/aphrodite/Cargo.toml"; do
		if [[ -f "$f" ]]; then
			BIN_VERSION=$(grep '^version' "$f" | head -1 | awk -F'"' '{print $2}')
			if [[ -n "$BIN_VERSION" ]]; then
				break
			fi
		fi
	done
fi
if [[ -z "$BIN_VERSION" ]]; then
	# Last resort: try plugin.yaml — WARNING: this is the PLUGIN version,
	# NOT the binary version. The two tracks can be completely different
	# (e.g. plugin v2.0.1 vs binary v1.0.4). Only use if you know they match.
	for f in "$SCRIPT_DIR/plugin.yaml" "$SCRIPT_DIR/../plugin.yaml"; do
		if [[ -f "$f" ]]; then
			BIN_VERSION=$(grep '^version:' "$f" | head -1 | awk '{print $2}' | tr -d '"')
			echo "WARNING: using plugin version $BIN_VERSION as binary version — these may differ!"
			break
		fi
	done
fi
if [[ -z "$BIN_VERSION" ]]; then
	echo "ERROR: could not determine version. Pass as argument: bash download.sh 0.9.4"
	exit 1
fi

# ── Auto-detect platform ──
if [[ -z "$TARGET" ]]; then
	ARCH=$(uname -m)
	case "$ARCH" in
		arm64|aarch64) ARCH="aarch64" ;;
		x86_64|amd64)   ARCH="x86_64" ;;
	esac
	OS=$(uname -s | tr '[:upper:]' '[:lower:]')
	case "$OS" in
		darwin)  TARGET="${ARCH}-apple-darwin" ;;
		linux)   TARGET="${ARCH}-unknown-linux-gnu" ;;
		mingw*|msys*|cygwin*) TARGET="${ARCH}-pc-windows-msvc" ;;
		*)       echo "ERROR: unsupported OS: $OS"; exit 1 ;;
	esac
fi

# ── Download ──
BINARY_NAME="aphrodite-${TARGET}"
if [[ "$TARGET" == *windows* ]]; then
	BINARY_NAME="${BINARY_NAME}.exe"
fi

# GitHub release tags include a 'v' prefix: Aphrodite/v1.0.4
V="${BIN_VERSION#v}"  # strip any existing v so we don't double it
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/Aphrodite/v${V}/${BINARY_NAME}"
BINARY_PATH="${BINARY_DIR}/aphrodite"

echo "aphrodite: downloading v${BIN_VERSION} for ${TARGET}..."
echo "  from: ${DOWNLOAD_URL}"
echo "  to:   ${BINARY_PATH}"

mkdir -p "${BINARY_DIR}"

# Backup existing binary
if [[ -f "${BINARY_PATH}" ]]; then
	mv "${BINARY_PATH}" "${BINARY_PATH}.bak" 2>/dev/null || true
fi

# Download with curl (fallback: wget)
if command -v curl &>/dev/null; then
	curl -fSL --progress-bar -o "${BINARY_PATH}" "${DOWNLOAD_URL}" || {
		echo "ERROR: curl download failed"
		[[ -f "${BINARY_PATH}.bak" ]] && mv "${BINARY_PATH}.bak" "${BINARY_PATH}"
		exit 1
	}
elif command -v wget &>/dev/null; then
	wget -q --show-progress -O "${BINARY_PATH}" "${DOWNLOAD_URL}" || {
		echo "ERROR: wget download failed"
		[[ -f "${BINARY_PATH}.bak" ]] && mv "${BINARY_PATH}.bak" "${BINARY_PATH}"
		exit 1
	}
else
	echo "ERROR: neither curl nor wget found"
	exit 1
fi

# ── Validate ──
SIZE=$(stat -f%z "${BINARY_PATH}" 2>/dev/null || stat -c%s "${BINARY_PATH}" 2>/dev/null || echo 0)
if [[ "$SIZE" -eq 0 ]]; then
	echo "ERROR: downloaded binary is empty"
	[[ -f "${BINARY_PATH}.bak" ]] && mv "${BINARY_PATH}.bak" "${BINARY_PATH}"
	exit 1
fi

# Check magic bytes (ELF, Mach-O, PE)
MAGIC=$(head -c4 "${BINARY_PATH}" | xxd -p | tr -d '\n')
VALID=0
case "$MAGIC" in
	7f454c46)               VALID=1 ;;  # ELF
	cffaedfe|feedfacf|cefaedfe|cafebabe) VALID=1 ;;  # Mach-O
	4d5a*)                  VALID=1 ;;  # PE
esac
if [[ "$VALID" -eq 0 ]]; then
	echo "ERROR: invalid magic bytes: ${MAGIC}"
	[[ -f "${BINARY_PATH}.bak" ]] && mv "${BINARY_PATH}.bak" "${BINARY_PATH}"
	exit 1
fi

chmod +x "${BINARY_PATH}"
rm -f "${BINARY_PATH}.bak"

echo "aphrodite v${BIN_VERSION} installed: ${BINARY_PATH} (${SIZE} bytes)"
