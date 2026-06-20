#!/usr/bin/env bash
# aphrodite — download prebuilt binary from GitHub Releases
# Usage: bash download.sh [version] [target-triple]
#   version: default from plugin.yaml (auto-detected)
#   target:  auto-detected from uname -sm

set -euo pipefail

BINARY_DIR="${BINARY_DIR:-binaries}"
REPO="${REPO:-PlayForm/Aphrodite}"
BIN_VERSION="${1:-}"
TARGET="${2:-}"

# ── Auto-detect version from plugin.yaml ──
if [[ -z "$BIN_VERSION" ]]; then
	# Try plugin.yaml in same dir, then parent dir
	for f in plugin.yaml ../plugin.yaml; do
		if [[ -f "$f" ]]; then
			BIN_VERSION=$(grep '^version:' "$f" | head -1 | awk '{print $2}' | tr -d '"')
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

DOWNLOAD_URL="https://github.com/${REPO}/releases/download/Aphrodite/${BIN_VERSION}/${BINARY_NAME}"
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
