# Aphrodite 💋 Hermes Plugin

> [!NOTE]
>
> **CCR compression plugin for Hermes Agent - thin Python loader + Rust dylib.**
> Sub-ms tool output compression, 28-type classifier, 13 tools, 6 hooks,
> context engine, dylib hot-reload. Skills ship dev-side, not with the plugin.

Aphrodite intercepts tool output before it reaches the LLM and replaces it with
compact, structured previews. The agent sees 15 tokens of metadata instead of
500 tokens of raw text - and retrieves the full content only when it actually
needs it. **All compression logic runs in the Rust dylib.**

[![plugin](https://img.shields.io/static/v1?label=plugin&message=v2.2.0&color=purple)](plugin.yaml)
[![hermes](https://img.shields.io/static/v1?label=hermes&message=0.16.0%2B&color=blue)](https://github.com/NousResearch/hermes-agent)
[![license](https://img.shields.io/static/v1?label=license&message=CC0-1.0&color=lightgrey)](LICENSE)

---

## Install ⚡

### One-command

**`Terminal`**

```bash
git clone https://github.com/PlayForm/Aphrodite-Hermes.git
ln -s "$(pwd)/Aphrodite-Hermes" ~/.hermes/plugins/aphrodite
hermes plugins enable aphrodite
hermes
```

The `ln -s` line links the plugin into `~/.hermes/plugins/` so Hermes can
discover it. Create it at install time: the plugin's startup layout self-heal
(`layout_check.py`) can only recreate the link once Hermes has already loaded
the plugin from somewhere.

On first launch, the plugin **automatically downloads** the `aphrodite` binary
from [releases](https://github.com/PlayForm/Aphrodite/releases) into the
canonical runtime home `~/.hermes/aphrodite/binaries/`. No Rust toolchain
required.

> [!IMPORTANT]
>
> **Native Windows**: run `pwsh ./download.ps1` instead of `download.sh` - no
> Git Bash/WSL needed. See
> [Windows install](https://github.com/PlayForm/Aphrodite/blob/Current/docs/install/windows.md)
> for the full walkthrough, and
> [Troubleshooting](https://github.com/PlayForm/Aphrodite/blob/Current/docs/install/troubleshooting.md)
> if the proxy doesn't come up after enabling the plugin.

### LLM provider configuration (required)

The aphrodite proxy is an OpenAI-compatible LLM API proxy - it forwards
requests upstream - so it needs **its own** provider credentials even though
Hermes already has a provider configured. The plugin **cannot** read Hermes'
provider config, and there is **no keyless / compression-only mode**: without a
key the proxy refuses to start and the plugin is unusable.

**`Terminal`**

```bash
export APHRODITE_API_KEY="sk-..."                                  # REQUIRED
export APHRODITE_API_URL="https://api.openai.com"                  # optional
export APHRODITE_MODEL="default-model"                             # optional
```

Alternatives: run `aphrodite setup`, or add `api_key` / `api_url` / `model` to
`~/.hermes/aphrodite/aphrodite.toml`. If the proxy fails to start,
`~/.hermes/aphrodite/proxy-stderr.log` shows the reason - `no API key
configured` means the key is missing.

### What changes after install

After installing and launching Hermes once:

**`Filesystem`**

```text
~/.hermes/
├── plugins/
│   └── aphrodite → /path/to/Aphrodite-Hermes    ← manual symlink to this repo
├── aphrodite/
│   ├── binaries/
│   │   ├── aphrodite                            ← auto-downloaded proxy binary (~35 MB)
│   │   └── libaphrodite_hermes.dylib            ← auto-downloaded dylib
│   ├── ccr.db                                    ← SQLite CCR store (on first run)
│   └── proxy-stderr.log                          ← proxy logs (on failure)
```

The plugin also adds to your Hermes config:

**`config.yaml`**

```yaml
# Added automatically on enable
plugins:
    enabled:
        - aphrodite

# Recommended additions (manual)
context:
    engine: aphrodite
    engine_threshold_pct: 100 # 100 = engine effectively off; lower = compress sooner
model:
    context_length: 1000000
```

Two proxy processes launch on `:9797` (cache) and `:9798` (token).
On registration the plugin probes both health endpoints and **reuses an
already-running proxy pair** instead of launching a second instance.

### Verify it's working

**`Terminal`**

```bash
# In a Hermes session:
aphrodite_stats

# Or via CLI:
curl http://127.0.0.1:9798/health
# → {"status":"healthy","version":"<installed binary version - see BINARY_VERSION>"}
```

### Clean uninstall

**`Terminal`**

```bash
hermes plugins disable aphrodite
rm ~/.hermes/plugins/aphrodite   # remove the manual symlink you created
pkill -f "$HOME/.hermes/aphrodite/binaries/aphrodite"
```

---

## Architecture 🏗️

The plugin is a **thin Python registration shim** over a Rust dylib - every
hook, tool, and byte of compression logic lives in Rust. Python exists only to
load the dylib via ctypes and register its surface with Hermes.

### Layers

**`Call chain`**

```text
 Hermes Agent (hooks + tool dispatch)
    │
    ▼
 plugins/aphrodite/__init__.py        ← 1257-line Python loader
    │  ctypes FFI, registers hooks/tools/engine - no logic
    ▼
 libaphrodite_hermes.dylib            ← Hermes bridge (JSON contract)
    │  6 hooks · 13 tools · schemas
    ▼
 libaphrodite (core engine)           ← ALL compression logic
    │  hooks · resolve · retrieve · marker · preview
    │  stage2 · struct_extract · state · session
    │  catalog · prefetch · poll_worker · directives
    │  config_loader · builtin_directives
    ▼
 CCR store (SQLite :9798 / in-memory :9797 / inline)
```

### Data flow

**`Plugin mode`**

```text
 Tool executes → output intercepted by hook
      ↓
 classify → preview → store (SQLite / in-memory / inline)
      ↓
 Agent ← [type:enriched preview] (not raw output)
      ↓
 aphrodite_retrieve(hash) → full content (only when needed)
```

### Dual listeners

The plugin auto-launches two proxy processes (and reuses an already-running
pair instead of starting a second instance):

| Listener | Port  | CCR backend                      | Threshold | Best for                  |
| :------- | :---: | :------------------------------- | :-------: | :------------------------ |
| Cache    | :9797 | In-memory (DashMap, 10K entries) |   >8 KB   | Speed, transient sessions |
| Token    | :9798 | SQLite (persistent)              |   >1 KB   | Durability, tool relay    |

### Hooks

Six Hermes hooks drive the plugin (`provides_hooks` in `plugin.yaml`), all
dispatched to the Rust dylib:

| Hook                        | Role                                                    |
| :-------------------------- | :------------------------------------------------------ |
| `on_session_start`          | Engine bootstrap, directive seeding, proxy health check |
| `transform_tool_result`     | Compress every tool result before it reaches the LLM    |
| `transform_terminal_output` | Compress terminal output with exit-code context         |
| `pre_llm_call`              | Inject directives, compress overflowing middle turns    |
| `post_llm_call`             | Capture savings, update adaptive thresholds             |
| `pre_tool_call`             | Prefetch-aware dispatch, auto-background slow calls     |

> [!NOTE]
>
> **Hot-reload**: rebuild the dylib → mtime change detected → the loader copies
> it to a fresh unique path (`~/.hermes/aphrodite/hotreload/<base>.<pid>.<gen>`)
> and re-`ctypes.CDLL()`s it - the next call picks up the new code
> automatically. Stale copies are reaped on startup and shutdown.

---

## Tools 🛠️

| Tool                        | Description                                                                       |
| :-------------------------- | :-------------------------------------------------------------------------------- |
| `aphrodite_retrieve`        | Resolve `<<<CCR:hash\|type\|size>>>` markers                                      |
| `aphrodite_compress`        | Compress content via CCR with type hint                                           |
| `aphrodite_stats`           | Proxy health, engine status, inline store size                                    |
| `aphrodite_rebuild`         | Report binary/proxy version + a rebuild hint (does not rebuild or restart itself) |
| `aphrodite_files`           | Tracked file references grouped by tool                                           |
| `aphrodite_diff`            | Conversation turn history with summaries                                          |
| `aphrodite_search`          | Search CCR store by keyword or type                                               |
| `aphrodite_directive`       | List/swap/add/remove/reset active behavioral directives                           |
| `aphrodite_test`            | Smoke test suite: quick (1 sample) or full (3 samples)                            |
| `aphrodite_catalog`         | Full CCR catalog with hashes, types, sizes, previews                              |
| `aphrodite_reclassify`      | Retroactive metadata enrichment                                                   |
| `aphrodite_prefetch`        | Read + compress files on demand; markers returned inline                          |
| `aphrodite_prefetch_status` | Live prefetch schedule: loading, ready, errors                                    |

---

## Configuration ⚙️

All tuning in `aphrodite.toml` - searched in `./aphrodite.toml`, then
`~/.hermes/aphrodite/aphrodite.toml` (when `APHRODITE_CONFIG_PATH` is unset):

**`aphrodite.toml`**

```toml
[compression]
engine_threshold_pct = 100   # 100% = engine effectively off (standing feedback); lower = compress sooner
engine_protect_first = 2     # messages to keep at start
engine_protect_last = 5      # messages to keep at end
engine_min_msgs = 8          # minimum before activating
tool_threshold_token = 256   # token proxy threshold (bytes)
tool_threshold_cache = 2048  # cache proxy threshold (bytes)
terminal_threshold  = 512    # terminal output threshold (bytes)
inline_threshold    = 1024   # inline-vs-durable CCR storage cutoff (bytes)
code_multiplier     = 3.0    # keep code in context longer
context_engine      = true   # default-on, no env var needed

[previews]
model_family = "code_first"  # compact | code_first | balance
code_structure_map = true    # show fn/struct/class sigs

[prompts]
retrieve_guidance = "verbose"
ccr_marker_hint = true
```

Env var overrides: `APHRODITE_ENGINE_THRESHOLD_PCT`, `APHRODITE_CONTEXT_ENGINE`, etc.
See [docs/config/env-vars.md](https://github.com/PlayForm/Aphrodite/blob/Current/docs/config/env-vars.md).

`APHRODITE_HOME` relocates the plugin's Python-side data (hot-reload dylib
copies, `proxy-stderr.log`) from the default `~/.hermes/aphrodite`; the Rust
binary does **not** read it - `aphrodite.toml` / `ccr.db` lookup stays put.

Set `APHRODITE_NO_AUTO_LAUNCH=1` to skip the proxy auto-launch entirely, e.g.
when a `cargo watch` dev loop runs the proxy itself.

### Directives

Custom behavioral directives are `name.md` files in
`~/.hermes/aphrodite/directives/` - an empty file means an intentionally
empty directive. The plugin does NOT ship a `directives/` set: the binary
provides them (embedded builtins) and materializes them into the user-data
home at startup/setup, so the plugin dir stays a pure loader. The dylib
reads them from `APHRODITE_DIRECTIVES_DIR` (defaults to
`~/.hermes/aphrodite/directives`, user override wins). If no directive
directory is found, the compiled built-in set loads as a fallback - its
activation is logged. Manage them at runtime with `aphrodite_directive`
(`list`/`swap`/`add`/`load`/`remove`/`reset`).

---

## Dev Install (Rust source)

**`Terminal`**

```bash
git clone https://github.com/PlayForm/Aphrodite.git
cd Aphrodite
cargo build -p aphrodite-hermes
# Dylib: target/debug/libaphrodite_hermes.dylib (crate aphrodite-hermes;
# `-p aphrodite` alone builds only the proxy binary). The loader resolves the
# dylib from the canonical runtime home first (env override
# APHRODITE_HERMES_DYLIB_PATH wins when set), so a dev loop copies it there:
mkdir -p ~/.hermes/aphrodite/binaries
cp target/debug/libaphrodite_hermes.dylib ~/.hermes/aphrodite/binaries/
```

An already-running dev proxy (`cargo run -p aphrodite`) that answers the
health endpoints is reused instead of relaunched - the plugin probes both
ports before launching (see `_start_proxy`).

---

## Files

**`Layout`**

```text
Aphrodite-Hermes/
├── __init__.py          ← 1257-line Python loader (ctypes FFI)
├── plugin.yaml          ← 13 tools, 6 hooks, context engine
├── download.sh          ← Binary auto-downloader (macOS/Linux/Git Bash/WSL)
├── download.ps1         ← Binary auto-downloader (native Windows PowerShell)
├── BINARY_VERSION       ← Pinned binary release tag
├── tests/               ← Plugin test suite
├── README.md            ← This file
└── .gitignore
```

Runtime binaries are **not** stored here - `download.sh` / `download.ps1`
install them into `~/.hermes/aphrodite/binaries/` (the canonical runtime
home), never into this directory.
