# Aphrodite 💋 Hermes Plugin

> **CCR compression plugin for Hermes Agent — thin Python loader + Rust dylib.**
> **Sub‑ms tool output compression, 28‑type classifier, 12 tools, 9 skills.**

Aphrodite intercepts tool output before it reaches the LLM and replaces it with
compact, structured previews. The agent sees 15 tokens of metadata instead of
500 tokens of raw text — and retrieves the full content only when it actually
needs it. **All compression logic runs in the Rust dylib.**

[![plugin](https://img.shields.io/badge/plugin-v2.0.0-purple)](plugin.yaml)
[![hermes](https://img.shields.io/badge/hermes-≥0.16.0-blue)](https://github.com/NousResearch/hermes-agent)
[![license](https://img.shields.io/badge/license-CC0--1.0-lightgrey)](LICENSE)

---

## Install ⚡

> **You install THIS repo** — the standalone `Aphrodite-Hermes` plugin.

```bash
git clone https://github.com/PlayForm/Aphrodite-Hermes.git
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/plugins/aphrodite
hermes plugins enable aphrodite
hermes
```

On first launch, the plugin **automatically downloads** the `aphrodite` binary
from [releases](https://github.com/PlayForm/Aphrodite/releases). No Rust toolchain required.

### Dev Install (Rust source)

```bash
git clone https://github.com/PlayForm/Aphrodite.git
cd Aphrodite
cargo build -p aphrodite
# Dylib at target/debug/libaphrodite.dylib — auto-detected by plugin
```

---

## Architecture 🏗️

```
Python (thin loader)              Rust dylib (all logic)
  __init__.py       145L            libaphrodite.dylib
  headroom_ffi.py   332L              ← 17 C ABI functions
    ↓ ctypes FFI                      ← universal dispatch (14 hooks)
  libaphrodite.dylib                  hooks, resolve, stage2,
                                      struct_extract, state,
                                      catalog, session, marker,
                                      prefetch, config_loader
```

All 14 hooks + 12 tools delegate to Rust. Python serves as fallback.
Hot-reload: rebuild dylib → mtime change detected → next call picks up new code.

---

## Tools 🛠️

| Tool                        | Description                                |
| :-------------------------- | :----------------------------------------- |
| `aphrodite_retrieve`        | Resolve `<<<CCR:hash\|type>>>` markers     |
| `aphrodite_compress`        | Compress content via CCR with type hint    |
| `aphrodite_stats`           | Proxy health, engine status, inline store  |
| `aphrodite_rebuild`         | Rebuild binary + restart proxies           |
| `aphrodite_files`           | Tracked file references grouped by tool    |
| `aphrodite_diff`            | Conversation turn history with summaries   |
| `aphrodite_search`          | Search CCR store by keyword or type        |
| `aphrodite_test`            | Smoke test suite (quick / full / pipeline) |
| `aphrodite_catalog`         | Full CCR catalog with hashes, types, sizes |
| `aphrodite_reclassify`      | Retroactive metadata enrichment            |
| `aphrodite_prefetch`        | Background file read — markers instantly   |
| `aphrodite_prefetch_status` | Prefetch queue status                      |

---

## Configuration 🔧

Copy `aphrodite.toml.example` from the monorepo to `~/.hermes/aphrodite/aphrodite.toml`:

```toml
[defaults]
api_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"

[[proxies]]
name = "cache"
listen = "0.0.0.0:9797"
mode = "cache"

[[proxies]]
name = "token"
listen = "0.0.0.0:9798"
mode = "token"
tool_relay = true

[compression]
engine_threshold_pct = 45
context_engine = true
```

---

## Dev Workflow 🦀

```bash
# Terminal 1: cargo watch (rebuilds dylib on .rs change)
APHRODITE_NO_AUTO_LAUNCH=1 cargo watch -x 'build -p aphrodite'

# Terminal 2: Hermes (loads hot-reloaded dylib)
hermes --profile dev-aphrodite
```

| What changes     | What happens                                              |
|------------------|-----------------------------------------------------------|
| Any `.rs` file   | cargo watch rebuilds → dylib mtime changes                |
| Next hook call   | `headroom_ffi.py` detects mtime → reloads dylib           |
| Any `.py` file   | `/quit` + restart (Hermes caches Python imports)          |
| Proxy binary     | `aphrodite_rebuild` → kill + copy + restart               |

---

## More 🔗

- **[Monorepo](https://github.com/PlayForm/Aphrodite)** — Rust source, docs, benchmarks
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the agent framework

---

_CC0‑1.0 — public domain. A PlayForm project._
