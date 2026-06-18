# Aphrodite 💋 Hermes Plugin

> **CCR compression plugin for Hermes Agent — sub‑ms tool output compression,
> 28‑type classifier, 12 tools, context engine.**

Aphrodite intercepts tool output before it reaches the LLM and replaces it with
compact, structured previews. The agent sees 15 tokens of metadata instead of
500 tokens of raw text — and retrieves the full content only when it actually
needs it.

[![plugin](https://img.shields.io/badge/plugin-v1.62.42-purple)](plugin.yaml)
[![hermes](https://img.shields.io/badge/hermes-≥0.16.0-blue)](https://github.com/NousResearch/hermes-agent)
[![license](https://img.shields.io/badge/license-CC0--1.0-lightgrey)](LICENSE)

---

## Install ⚡

> **You install THIS repo** — the standalone `Aphrodite-Hermes` plugin. Do NOT
> clone the monorepo
> ([PlayForm/Aphrodite](https://github.com/PlayForm/Aphrodite)). The monorepo
> contains the full Rust proxy source, docs, benchmarks, and builds. This repo
> is `./plugins/aphrodite/` — the plugin that ships to users.

```bash
# 1. Clone the standalone plugin repo
git clone https://github.com/PlayForm/Aphrodite-Hermes.git

# 2. Symlink into your Hermes profile (standard plugin convention)
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/plugins/aphrodite

# Or for a named profile:
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/profiles/ < name > /plugins/aphrodite

# 3. Enable and restart
hermes plugins enable aphrodite
hermes
```

On first launch, the plugin **automatically downloads** the `aphrodite` binary
from [releases](https://github.com/PlayForm/Aphrodite/releases) to
`~/.hermes/aphrodite/aphrodite`. No Rust toolchain required.

### Repository Structure 📦

```
Aphrodite-Hermes/          ← you are here (standalone plugin)
├── __init__.py              entry point, proxy auto-launch
├── plugin.yaml              Hermes manifest (12 tools, 5 hooks)
├── _core/                   constants, TOML loader, settings
├── _engine.py               ContextEngine (offloads messages)
├── _hooks/                  transform_tool_result, terminal, etc.
├── _marker/                 28-type classifier, preview templates
├── _proxy/                  proxy lifecycle management
├── _resolve.py              recursive CCR expansion
├── _binary.py               auto-downloads binary from releases
├── _tools.py                12 aphrodite_* tool handlers
├── _inline.py               zlib fallback (works without proxy)
├── skills/                  9 bundled skills
├── tests/                   41 integration tests
└── pyproject.toml           Python ≥3.11, zero runtime deps

Aphrodite/                  ← monorepo (NOT what you install)
├── crates/aphrodite/         Rust proxy source
├── plugins/aphrodite/ ←──    this repo (git submodule)
├── docs/                     full documentation
├── scripts/                  build, benchmark, release
└── aphrodite.toml.example    template config
```

### Submodule Relationship 🔗

This standalone repo is a **git submodule** inside the
[PlayForm/Aphrodite](https://github.com/PlayForm/Aphrodite) monorepo at
`./plugins/aphrodite/`. Changes you push here are pulled into the monorepo via
`git submodule update --remote`. The monorepo tracks a specific commit pointer —
whenever you push here, the monorepo must update its submodule ref to point at
your new commit.

---

## Configuration 🔧

Copy `aphrodite.toml.example` from the monorepo to
`~/.hermes/aphrodite/aphrodite.toml` (or rely on the defaults — it works out of
the box with `APHRODITE_API_KEY`).

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

## More 🔗

- **[Monorepo](https://github.com/PlayForm/Aphrodite)** — full docs, benchmarks,
  Rust source
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the agent
  framework

---

_CC0‑1.0 — public domain. A PlayForm project._
