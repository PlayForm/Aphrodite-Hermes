#!/usr/bin/env bash
# aphrodite - download prebuilt binary from GitHub Releases
# Usage: bash download.sh [version] [target-triple]
#   version: auto-detected from Cargo.toml (monorepo), falls back to plugin.yaml
#   target:  auto-detected from uname -sm

set -euo pipefail

# 03-F18: BINARY_DIR used to default to a bare "binaries" - relative to
# $PWD (wherever this script happened to be invoked FROM), not to the
# script's own directory. Running `bash download.sh` from anywhere other
# than `plugins/aphrodite/` silently wrote binaries to the wrong place,
# not the `binaries/` directory the Hermes plugin's `__init__.py` actually
# looks in.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINARY_DIR="${BINARY_DIR:-$SCRIPT_DIR/binaries}"
REPO="${REPO:-PlayForm/Aphrodite}"
BIN_VERSION="${1:-}"
TARGET="${2:-}"

# ── Auto-detect version ──
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
		BIN_VERSION=$(curl -fsS --connect-timeout 10 --max-time 30 "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null | \
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

# ── SHA-256 checksum verification (report 12 F9/T8) ──────────────────
# Build.yml publishes one SHA256SUMS-<target>.txt per release asset bundle.
# Fetch it once per run; a missing sums file (e.g. a release cut before this
# was added) degrades to a loud warning rather than a hard failure, so older
# tags remain installable.
SUMS_FILE="$(mktemp)"
SUMS_ASSET="SHA256SUMS-${TARGET}.txt"
SUMS_OK=0
trap 'rm -f "${SUMS_FILE}"' EXIT
if command -v curl &>/dev/null; then
	curl -fsSL --connect-timeout 10 --max-time 30 -o "${SUMS_FILE}" "${BASE_URL}/${SUMS_ASSET}" 2>/dev/null && SUMS_OK=1
elif command -v wget &>/dev/null; then
	wget -q --connect-timeout=10 --timeout=30 -O "${SUMS_FILE}" "${BASE_URL}/${SUMS_ASSET}" 2>/dev/null && SUMS_OK=1
fi
if [[ "$SUMS_OK" -eq 1 ]]; then
	echo "  ✓ fetched ${SUMS_ASSET}"
else
	echo "WARNING: ${SUMS_ASSET} not found - skipping checksum verification for this release (older release, or the sums asset failed to publish)"
fi

# verify_checksum <asset-name> <dest-path>
# Checks `dest` against the SHA-256 recorded for `asset` in SUMS_FILE. A
# no-op (returns success) when SUMS_OK=0, so callers don't need to branch.
verify_checksum() {
	local asset="$1" dest="$2"
	[[ "$SUMS_OK" -eq 1 ]] || return 0
	local expected actual
	# Exact match on the filename field (awk's $2), not a substring grep -
	# `shasum`'s `<hash>  <filename>` format means a substring match could
	# hit a *different*, longer asset name that happens to start with this
	# one (not a risk with today's two-line-per-target sums files, but this
	# is exact regardless).
	expected=$(awk -v want="${asset}" '$2 == want { print $1; exit }' "${SUMS_FILE}" 2>/dev/null)
	if [[ -z "$expected" ]]; then
		echo "WARNING: ${asset} has no entry in ${SUMS_ASSET} - skipping checksum check for this asset"
		return 0
	fi
	if command -v shasum &>/dev/null; then
		actual=$(shasum -a 256 "${dest}" | awk '{print $1}')
	elif command -v sha256sum &>/dev/null; then
		actual=$(sha256sum "${dest}" | awk '{print $1}')
	else
		echo "WARNING: no shasum/sha256sum binary found - skipping checksum check for ${asset}"
		return 0
	fi
	if [[ "$expected" != "$actual" ]]; then
		echo "ERROR: checksum mismatch for ${asset}"
		echo "  expected: ${expected}"
		echo "  actual:   ${actual}"
		return 1
	fi
	echo "  ✓ ${asset} checksum verified"
}

# fetch_and_validate <asset-name> <dest-path>
# Downloads a release asset, verifies it's a real native binary
# (ELF/Mach-O/PE) and (when a sums file was found) its SHA-256 checksum,
# restoring any prior copy on failure.
fetch_and_validate() {
	local asset="$1" dest="$2" url="${BASE_URL}/$1"
	echo "  ${asset} -> ${dest}"
	[[ -f "${dest}" ]] && mv "${dest}" "${dest}.bak" 2>/dev/null || true

	if command -v curl &>/dev/null; then
		# --connect-timeout bounds the initial handshake (fail fast on an
		# unreachable host); --max-time bounds the whole transfer generously
		# (these binaries run ~10-40MB, so this is a stall/hang guard, not a
		# realistic-bandwidth budget).
		curl -fSL --connect-timeout 10 --max-time 120 --progress-bar -o "${dest}" "${url}" || {
			echo "ERROR: curl download failed: ${url}"
			[[ -f "${dest}.bak" ]] && mv "${dest}.bak" "${dest}"
			return 1
		}
	elif command -v wget &>/dev/null; then
		wget -q --connect-timeout=10 --timeout=120 --show-progress -O "${dest}" "${url}" || {
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
	if ! verify_checksum "${asset}" "${dest}"; then
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
	BINARY_PATH="${BINARY_DIR}/aphrodite.exe"
	DYLIB_ASSET="libaphrodite_hermes-${TARGET}.dll"
	DYLIB_DEST="${BINARY_DIR}/aphrodite_hermes.dll"
elif [[ "$TARGET" == *apple* ]]; then
	BINARY_PATH="${BINARY_DIR}/aphrodite"
	DYLIB_ASSET="libaphrodite_hermes-${TARGET}.dylib"
	DYLIB_DEST="${BINARY_DIR}/libaphrodite_hermes.dylib"
else
	BINARY_PATH="${BINARY_DIR}/aphrodite"
	DYLIB_ASSET="libaphrodite_hermes-${TARGET}.so"
	DYLIB_DEST="${BINARY_DIR}/libaphrodite_hermes.so"
fi

echo "aphrodite: downloading v${BIN_VERSION} for ${TARGET} from ${BASE_URL}"
fetch_and_validate "${BINARY_ASSET}" "${BINARY_PATH}" || exit 1
chmod +x "${BINARY_PATH}"
fetch_and_validate "${DYLIB_ASSET}" "${DYLIB_DEST}" || exit 1

echo "aphrodite v${BIN_VERSION} installed: ${BINARY_PATH} + ${DYLIB_DEST}"
