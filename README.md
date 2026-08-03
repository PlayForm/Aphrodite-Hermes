# Aphrodite 💋 Hermes Plugin

> **CCR compression plugin for Hermes Agent - thin Python loader + Rust dylib.**
> **Sub-ms tool output compression, 26-type classifier, 13 tools, 9 skills.**

Aphrodite intercepts tool output before it reaches the LLM and replaces it with
compact, structured previews. The agent sees 15 tokens of metadata instead of
500 tokens of raw text - and retrieves the full content only when it actually
needs it. **All compression logic runs in the Rust dylib.**

[![plugin](https://img.shields.io/badge/plugin-v2.0.10-purple)](plugin.yaml)
[![hermes](https://img.shields.io/badge/hermes-≥0.16.0-blue)](https://github.com/NousResearch/hermes-agent)
[![license](https://img.shields.io/badge/license-CC0--1.0-lightgrey)](LICENSE)

---

## Install ⚡

### One-command

```bash
git clone https://github.com/PlayForm/Aphrodite-Hermes.git
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/plugins/aphrodite
hermes plugins enable aphrodite
hermes
```

On first launch, the plugin **automatically downloads** the `aphrodite` binary
from [releases](https://github.com/PlayForm/Aphrodite/releases). No Rust
toolchain required.

> **Native Windows**: run `pwsh ./download.ps1` instead of `download.sh` - no
> Git Bash/WSL needed. See
> [Windows install](https://github.com/PlayForm/Aphrodite/blob/Current/docs/install/windows.md)
> for the full walkthrough, and
> [Troubleshooting](https://github.com/PlayForm/Aphrodite/blob/Current/docs/install/troubleshooting.md)
> if the proxy doesn't come up after enabling the plugin.

### What changes after install

After installing and launching Hermes once:

```
~/.hermes/
├── plugins/
│   └── aphrodite → /path/to/Aphrodite-Hermes    ← symlink to this repo
├── aphrodite/
│   ├── aphrodite                                 ← auto-downloaded binary (~12 MB)
│   └── ccr.db                                    ← SQLite CCR store (on first run)
└── profiles/<name>/
    └── plugins/
        └── aphrodite → ~/.hermes/plugins/aphrodite
```

The plugin also adds to your Hermes config:

```yaml
# Added automatically on enable
plugins:
  enabled:
    - aphrodite

# Recommended additions (manual)
context:
  engine: aphrodite
  engine_threshold_pct: 55
model:
  context_length: 1000000
```

Two proxy processes launch on `:9797` (cache) and `:9798` (token).

### Verify it's working

```bash
# In a Hermes session:
aphrodite_stats

# Or via CLI:
curl http://127.0.0.1:9798/health
# → {"status":"ok","version":"<current aphrodite version - see the badge above>"}
```

### Clean uninstall

```bash
hermes plugins disable aphrodite
rm ~/.hermes/plugins/aphrodite
pkill -f "aphrodite/binaries/aphrodite"
```

---

## Architecture 🏗️

```
Python (thin loader)              Rust dylib (all logic)
  __init__.py       421L            libaphrodite_hermes.dylib
    ↓ ctypes FFI                      ← universal dispatch (5 hooks)
  libaphrodite_hermes.dylib           ← 13 tool handlers, delegates into
                                      libaphrodite (core engine): hooks,
                                      resolve, stage2, struct_extract, state,
                                      catalog, session, marker, prefetch,
                                      config_loader
```

All 5 hooks + 13 tools delegate to Rust. Python serves as fallback.
Hot-reload: rebuild dylib → mtime change detected → next call picks up new code.

---

## Tools 🛠️

| Tool                   | Description                                          |
| :--------------------- | :--------------------------------------------------- |
| `aphrodite_retrieve`   | Resolve `<<<CCR:hash\|type>>>` markers                |
| `aphrodite_compress`   | Compress content via CCR with type hint               |
| `aphrodite_stats`      | Proxy health, engine status, inline store size        |
| `aphrodite_rebuild`    | Report binary/proxy version + a rebuild hint (does not rebuild or restart itself) |
| `aphrodite_files`      | Tracked file references grouped by tool               |
| `aphrodite_diff`       | Conversation turn history with summaries              |
| `aphrodite_search`     | Search CCR store by keyword or type                   |
| `aphrodite_directive`  | List/swap/add/remove/reset active behavioral directives |
| `aphrodite_test`       | Smoke test suite: quick (1 sample) or full (3 samples) |
| `aphrodite_catalog`    | Full CCR catalog with hashes, types, sizes, previews  |
| `aphrodite_reclassify` | Retroactive metadata enrichment                       |
| `aphrodite_prefetch`   | Background file read + compress (markers return instantly) |
| `aphrodite_prefetch_status` | Live prefetch schedule: loading, ready, errors    |

---

## Configuration ⚙️

All tuning in `aphrodite.toml` (searched: CWD → `~/.hermes/aphrodite/` → repo root):

```toml
[compression]
engine_threshold_pct = 45    # compress at 45% context fill
engine_protect_first = 2     # messages to keep at start
engine_protect_last = 5      # messages to keep at end
engine_min_msgs = 8          # minimum before activating
tool_threshold_token = 512   # token proxy threshold (bytes)
tool_threshold_cache = 4096  # cache proxy threshold (bytes)
code_multiplier = 3.0        # keep code in context longer
context_engine = true        # default-on, no env var needed

[previews]
model_family = "code_first"  # compact | code_first | balance
code_structure_map = true    # show fn/struct/class sigs

[prompts]
retrieve_guidance = "verbose"
ccr_marker_hint = true
```

Env var overrides: `APHRODITE_ENGINE_THRESHOLD_PCT`, `APHRODITE_CONTEXT_ENGINE`, etc.

---

## Dev Install (Rust source)

```bash
git clone https://github.com/PlayForm/Aphrodite.git
cd Aphrodite
cargo build -p aphrodite
# Dylib at target/debug/libaphrodite.dylib - auto-detected by plugin
```

---

## Files

```
Aphrodite-Hermes/
├── __init__.py          ← 421-line Python loader (ctypes FFI)
├── plugin.yaml          ← 13 tools, 5 hooks, context engine
├── download.sh          ← Binary auto-downloader (macOS/Linux/Git Bash/WSL)
├── download.ps1         ← Binary auto-downloader (native Windows PowerShell)
├── binaries/            ← Platform-native dylib + proxy binary
├── README.md            ← This file
└── .gitignore
```

