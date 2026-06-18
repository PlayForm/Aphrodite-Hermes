# Aphrodite 💋 Hermes Plugin

> **CCR compression plugin for Hermes Agent — sub‑ms tool output compression,
> 28‑type classifier, 13 tools, context engine.**

Aphrodite intercepts tool output before it reaches the LLM and replaces it with
compact, structured previews. The agent sees 15 tokens of metadata instead of
500 tokens of raw text — and retrieves the full content only when it actually
needs it.

[![plugin](https://img.shields.io/badge/plugin-v1.62.27-purple)](plugin.yaml)
[![hermes](https://img.shields.io/badge/hermes-≥0.16.0-blue)](https://github.com/NousResearch/hermes-agent)
[![license](https://img.shields.io/badge/license-CC0--1.0-lightgrey)](LICENSE)

---

## ⚡ Installation

> **You install this repo — the standalone `Aphrodite-Hermes` plugin. Do NOT
> clone the monorepo ([PlayForm/Aphrodite](https://github.com/PlayForm/Aphrodite))
> — that's the full Rust proxy source + docs.**

### 1. Clone the plugin

```bash
git clone https://github.com/PlayForm/Aphrodite-Hermes.git
```

### 2. Link into your Hermes profile

```bash
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/profiles/<your-profile>/plugins/aphrodite
```

Or for the default profile:

```bash
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/plugins/aphrodite
```

### 3. Enable and restart

```bash
hermes plugins enable aphrodite
hermes  # restart
```

### 4. Binary auto‑download

On first launch, the plugin **automatically downloads** the `aphrodite` binary
from [GitHub Releases](https://github.com/PlayForm/Aphrodite/releases). No
Rust toolchain, no `cargo build`, no manual steps.

- **macOS ARM64** → `aphrodite-aarch64-apple-darwin`
- **Linux x86_64** → `aphrodite-x86_64-unknown-linux-gnu`

The binary is placed at `~/.hermes/aphrodite/aphrodite` and version‑checked on
every restart. If your platform isn't available, [build from
source](https://github.com/PlayForm/Aphrodite#quick-start).

### 5. Set your API key

```bash
export APHRODITE_API_KEY=<your-upstream-api-key>
```

Also ensure Hermes passes env vars to subprocesses (required for the proxy):

```bash
hermes config set terminal.env_passthrough '["APHRODITE_API_KEY","PATH","HOME"]'
```

---

## 📦 What Ships

| File | Purpose |
|------|---------|
| `__init__.py` | Entry point — proxy auto‑launch, version exports |
| `plugin.yaml` | Hermes plugin manifest (13 tools, 5 hooks) |
| `_core/` | Constants, TOML loader, config resolvers, settings |
| `_engine.py` | ContextEngine — compresses middle turns to CCR |
| `_hooks/` | Hermes hook handlers (transform, catalog, stats, …) |
| `_marker/` | 28‑type classifier, template renderer, marker parse |
| `_proxy/` | Proxy lifecycle (env, health, launch, markers) |
| `_resolve.py` | Recursive CCR marker expansion (3 levels deep) |
| `_binary.py` | Binary auto‑download + platform detection |
| `_tools.py` | 13 aphrodite_* tool handlers + JSON schemas |
| `_inline.py` | zlib fallback (works without proxy) |
| `_automation.py` | Rhai scripting engine |
| `pyproject.toml` | Python ≥3.11, no runtime deps |
| `skills/` | 9 bundled skills (compression, proxy, tools, …) |

---

## 🔧 Configuration

All settings live in `aphrodite.toml` (searched in: CWD → `~/.hermes/aphrodite/`
→ repo root).

```toml
# Minimal example — place in ~/.hermes/aphrodite/aphrodite.toml
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

See the [monorepo](https://github.com/PlayForm/Aphrodite) for the full schema
and all available options.

---

## 🛠️ Tools

| Tool | Description |
| :--- | :--- |
| `aphrodite_retrieve` | Resolve `<<<CCR:hash\|type>>>` markers |
| `aphrodite_compress` | Compress content via CCR with type hint |
| `aphrodite_stats` | Proxy health, engine status, inline store |
| `aphrodite_rebuild` | Rebuild binary + restart proxies |
| `aphrodite_files` | Tracked file references grouped by tool |
| `aphrodite_diff` | Conversation turn history with summaries |
| `aphrodite_search` | Search CCR store by keyword or type |
| `aphrodite_test` | Smoke test suite (quick / full / pipeline) |
| `aphrodite_catalog` | Full CCR catalog with hashes, types, sizes |
| `aphrodite_reclassify` | Retroactive metadata enrichment |
| `aphrodite_prefetch` | Background file read — markers instantly, files load concurrently |
| `aphrodite_prefetch_status` | Prefetch queue status |
| `aphrodite_poll_container` | Container health/heartbeat |

---

## 🔗 More

- **[Monorepo](https://github.com/PlayForm/Aphrodite)** — full docs, benchmark
  data, Rust proxy source
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the agent
  framework this plugin targets

---

*CC0‑1.0 — public domain. A PlayForm project.*
