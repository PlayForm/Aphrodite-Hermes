# Prompt-Cache Contract (pre_llm_call)

**Status:** contract for the shipped binary - verifiable from this tree.
**Applies to:** the native `pre_llm_call` hook body (implemented in Rust,
`crates/aphrodite/src/flow.rs::build_turn_context`), which is the ONLY
`pre_llm_call` arm Hermes calls in production. This document is the
human-readable contract; the binary must behave as described. Verify against
a shipped dylib with the procedure at the bottom.

## What pre_llm_call does

The hook returns exactly one JSON field, `context` (a string), or `null`
when there is nothing to inject. Hermes prepends that context to the model
prompt; nothing else in the system prompt or history is rewritten,
reordered, or truncated by this plugin. The plugin never removes tokens from
the conversation - it only *prepends* a stable context block.

## Context block structure (top-down, in this exact order)

The block is a sequence of sections joined by `\n`. Present sections appear
in this fixed order - earlier sections are always emitted before later ones:

1. **First-turn orientation** - `[aphrodite: first-turn orientation]`
   header plus session-inject directives. Present only on the first turn of
   a session.
2. **Active directives** - the materialized directive text (focus /
   explore / lazy / any file in `directives/`).
3. **Nudges** - at most 2 `[nudge: <text>]` lines from ephemeral inline
   directives (newest wins).
4. **Poll-worker status** - background-task status lines, when the poll
   worker is enabled.
5. **Recall catalog** - `[recall]` block: a delta-only summary of what is
   already in the content-addressable store.

## Prefix stability (the prompt-cache property)

- **The order is fixed**: section 1 (when present) is always the first
  thing the model sees, then 2, 3, 4, 5. A section never jumps ahead of an
  earlier section.
- **Always-survive floor**: sections 1-3 (orientation, directives, nudges)
  are the "never-drop" set. Under a shrinking budget the assembler pops
  sections **from the bottom only** (recall first, then poll status) and
  stops at the floor - so the leading prefix the provider's prompt cache
  keys on (orientation + directives + nudges) is byte-stable across turns
  once a session is past its first turn.
- **Budget**: the total block is bounded by `flow_budget_chars`
  (`APHRODITE_FLOW_BUDGET_CHARS`, default in `aphrodite.toml`). The
  always-survive sections are exempt from the drop loop; recall and
  poll-status absorb the trimming.
- **Empty block** → `null` → nothing prepended; the prompt is untouched.

## What is NOT done

- No rewriting of `conversation_history`, user messages, or prior assistant
  turns.
- No truncation or deletion of existing prompt tokens.
- No per-turn random/variable prefix: for a stable session (same active
  directives, no first-turn block), the leading block is identical every
  turn - that is the prompt-cache stability the contract guarantees.

## Verification procedure (from this tree, against a shipped dylib)

The pinned tree carries the dylib in `binaries/` (gitignored, pulled from
the immutable release by the publish action; `download.sh` is the explicit
setup fallback). To verify the contract holds for the exact shipped binary:

```sh
# 1. Build a probe that calls the hook through the plugin's own FFI:
python3 - <<'PY'
import ctypes, json
d = ctypes.CDLL("binaries/libaphrodite_hermes.dylib")
d.aphrodite_hermes_call_hook.restype = ctypes.c_void_p
d.aphrodite_hermes_call_hook.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
d.aphrodite_hermes_free_string.restype = None
d.aphrodite_hermes_free_string.argtypes = [ctypes.c_void_p]
payload = json.dumps({"conversation_history": []}).encode()
ptr = d.aphrodite_hermes_call_hook(b"pre_llm_call", payload)
out = ctypes.cast(ptr, ctypes.c_char_p).value.decode()
d.aphrodite_hermes_free_string(ptr)
print(json.loads(out))
PY
```

Pass conditions:

- Output is `{"context": "<block>"}` or `null` - never any other key, never
  an error.
- If `<block>` is non-empty, it starts with `[aphrodite: first-turn
  orientation]` (first turn) or a directive section (later turns) - never a
  recall/poll section first.
- Call the hook twice with identical inputs: the two `context` values are
  byte-identical (prefix stability).
- Drop the budget env var to a tiny value and re-call: the block shrinks
  from the bottom (recall disappears first), the always-survive sections
  remain.

## Source of truth

`crates/aphrodite/src/flow.rs::build_turn_context` (monorepo, Development
branch). The binary is built from the tagged commit whose `BINARY_VERSION`
matches `BINARY_VERSION` in this tree; `download.sh` verifies SHA-256
against the release's checksums file and refuses on mismatch.