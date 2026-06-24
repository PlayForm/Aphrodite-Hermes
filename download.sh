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
	# 1. BINARY_VERSION file - deployed with the plugin, always correct
	if [[ -f "$SCRIPT_DIR/BINARY_VERSION" ]]; then
		BIN_VERSION=$(head -1 "$SCRIPT_DIR/BINARY_VERSION" | tr -d '[:space:]')
	fi
fi
if [[ -z "$BIN_VERSION" ]]; then
	# 2. Cargo.toml - for developers with the full monorepo
	for f in "$SCRIPT_DIR/../../crates/aphrodite/Cargo.toml" \
	         "$SCRIPT_DIR/../../crates/aphrodite-hermes/Cargo.toml" \
	         "$SCRIPT_DIR/../../../crates/aphrodite/Cargo.toml"; do
		if [[ -f "$f" ]]; then
			BIN_VERSION=$(grep '^version' "$f" | head -1 | awk -F'"' '{print $2}')
			[[ -n "$BIN_VERSION" ]] && break
		fi
	done
fi
if [[ -z "$BIN_VERSION" ]]; then
	# 3. GitHub API - query latest release tag (needs network, but reliable)
	if command -v curl &>/dev/null; then
		BIN_VERSION=$(curl -fsS "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null | \
			grep '"tag_name":' | head -1 | sed 's/.*"tag_name": *"Aphrodite\/v\([^"]*\)".*/\1/')
	fi
fi
if [[ -z "$BIN_VERSION" ]]; then
	echo "ERROR: could not determine binary version."
	echo "  Pass explicitly: bash download.sh 1.0.5"
	echo "  Or create a BINARY_VERSION file in $(dirname "$0")"
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

# GitHub release tags include a 'v' prefix with URL-encoded slash: Aphrodite%2Fv1.0.6
V="${BIN_VERSION#v}"  # strip any existing v so we don't double it
BASE_URL="https://github.com/${REPO}/releases/download/Aphrodite%2Fv${V}"
mkdir -p "${BINARY_DIR}"

# fetch_and_validate <asset-name> <dest-path>
# Downloads a release asset and verifies it's a real native binary (ELF/Mach-O/PE),
# restoring any prior copy on failure.
fetch_and_validate() {
	local asset="$1" dest="$2" url="${BASE_URL}/$1"
	echo "  ${asset} -> ${dest}"
	[[ -f "${dest}" ]] && mv "${dest}" "${dest}.bak" 2>/dev/null || true

	if command -v curl &>/dev/null; then
		curl -fSL --progress-bar -o "${dest}" "${url}" || {
			echo "ERROR: curl download failed: ${url}"
			[[ -f "${dest}.bak" ]] && mv "${dest}.bak" "${dest}"
			return 1
		}
	elif command -v wget &>/dev/null; then
		wget -q --show-progress -O "${dest}" "${url}" || {
			echo "ERROR: wget download failed: ${url}"
			[[ -f "${dest}.bak" ]] && mv "${dest}.bak" "${dest}"
			return 1
		}
	else
		echo "ERROR: neither curl nor wget found"
		return 1
	fi

	local size magic valid=0
	size=$(stat -f%z "${dest}" 2>/dev/null || stat -c%s "${dest}" 2>/dev/null || echo 0)
	if [[ "$size" -eq 0 ]]; then
		echo "ERROR: downloaded ${asset} is empty"
		[[ -f "${dest}.bak" ]] && mv "${dest}.bak" "${dest}"
		return 1
	fi
	magic=$(head -c4 "${dest}" | xxd -p | tr -d '\n')
	case "$magic" in
		7f454c46) valid=1 ;;                              # ELF
		cffaedfe|feedfacf|cefaedfe|cafebabe) valid=1 ;;   # Mach-O
		4d5a*) valid=1 ;;                                 # PE
	esac
	if [[ "$valid" -eq 0 ]]; then
		echo "ERROR: ${asset} has invalid magic bytes: ${magic}"
		[[ -f "${dest}.bak" ]] && mv "${dest}.bak" "${dest}"
		return 1
	fi
	rm -f "${dest}.bak"
	echo "  ✓ ${asset} (${size} bytes)"
}

# ── Asset + local names per platform ──
#   binary: the proxy executable (loaded as a subprocess)
#   dylib:  libaphrodite_hermes.* - the cdylib the Python plugin loads via ctypes
BINARY_ASSET="aphrodite-${TARGET}"
if [[ "$TARGET" == *windows* ]]; then
	BINARY_ASSET="${BINARY_ASSET}.exe"
	DYLIB_ASSET="libaphrodite_hermes-${TARGET}.dll"
	DYLIB_DEST="${BINARY_DIR}/aphrodite_hermes.dll"
elif [[ "$TARGET" == *apple* ]]; then
	DYLIB_ASSET="libaphrodite_hermes-${TARGET}.dylib"
	DYLIB_DEST="${BINARY_DIR}/libaphrodite_hermes.dylib"
else
	DYLIB_ASSET="libaphrodite_hermes-${TARGET}.so"
	DYLIB_DEST="${BINARY_DIR}/libaphrodite_hermes.so"
fi
BINARY_PATH="${BINARY_DIR}/aphrodite"

echo "aphrodite: downloading v${BIN_VERSION} for ${TARGET} from ${BASE_URL}"
fetch_and_validate "${BINARY_ASSET}" "${BINARY_PATH}" || exit 1
chmod +x "${BINARY_PATH}"
fetch_and_validate "${DYLIB_ASSET}" "${DYLIB_DEST}" || exit 1

echo "aphrodite v${BIN_VERSION} installed: ${BINARY_PATH} + ${DYLIB_DEST}"
