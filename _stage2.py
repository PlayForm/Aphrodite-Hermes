"""
Aphrodite stage-2 compression — semantic reduction of CCR-stored content.

Produces a denser version of content stored in CCR. When the agent calls
aphrodite_retrieve(hash, depth=2), it gets the reduced version instead of
the raw original.

Reduction strategies per content type:
- JSON: whitespace minification + top-level key extraction
- Build/log: error/warning line extraction
- Diff: file-level summary with hunk counts
- Code: function/struct/class signature extraction
- Terminal: pass through
- Other: pass through
"""

import json
import re

# Size threshold — don't bother reducing content under this many bytes
_MIN_STAGE2_SIZE = 80


def _reduce_json(content: str) -> str:
    """Minify JSON and extract top-level structural keys."""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            keys = list(data.keys())[:10]
            structural = f"[json:{len(data)} keys: {', '.join(keys)}]"
        elif isinstance(data, list):
            structural = f"[json_list:{len(data)} items]"
            if data and isinstance(data[0], dict):
                keys = list(data[0].keys())[:10]
                structural += f" schema: {', '.join(keys)}"
        else:
            structural = f"[json:{type(data).__name__}]"
        minified = json.dumps(data, separators=(",", ":"))
        return f"{structural}\n{minified}"
    except (json.JSONDecodeError, TypeError, ValueError):
        return content


def _reduce_build(content: str) -> str:
    """Extract errors, warnings, and summary from build output."""
    errors = []
    warnings = []
    summary_lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if "error" in lower and "error:" not in lower[:20]:
            errors.append(stripped[:200])
        elif "warning" in lower and "warning:" not in lower[:20]:
            warnings.append(stripped[:200])
        elif any(kw in lower for kw in ("compiling", "finished", "running", "test result", "passed", "failed")):
            summary_lines.append(stripped[:200])

    parts = []
    if summary_lines:
        parts.append("[build summary]")
        parts.extend(summary_lines[:10])
    if errors:
        parts.append(f"[{len(errors)} errors]")
        parts.extend(errors[:20])
    if warnings:
        parts.append(f"[{len(warnings)} warnings]")
        parts.extend(warnings[:10])

    if parts:
        return "\n".join(parts)
    return content


def _reduce_diff(content: str) -> str:
    """File-level diff summary with hunk counts."""
    files = []
    current_file = None
    hunks = 0
    for line in content.splitlines():
        if line.startswith("diff --git "):
            if current_file is not None:
                files.append((current_file, hunks))
            current_file = line
            hunks = 0
        elif line.startswith("@@ "):
            hunks += 1
    if current_file is not None:
        files.append((current_file, hunks))

    if files:
        parts = [f"[diff:{len(files)} files]"]
        for fname, hcount in files:
            parts.append(f"  {fname} ({hcount} hunks)")
        return "\n".join(parts)
    return content


def _reduce_code(content: str) -> str:
    """Extract function/struct/class signatures from code."""
    sig_patterns = [
        (r"^(pub\s+)?(async\s+)?fn\s+(\w+)([^{]*)", "fn"),
        (r"^(pub\s+)?struct\s+(\w+)", "struct"),
        (r"^(pub\s+)?enum\s+(\w+)", "enum"),
        (r"^(pub\s+)?trait\s+(\w+)", "trait"),
        (r"^(pub\s+)?impl\b[^{]*", "impl"),
        (r"^def\s+(\w+)\s*\([^)]*\)", "def"),
        (r"^class\s+(\w+)", "class"),
        (r"^func\s+(\w+)\s*\([^)]*\)", "func"),
        (r"^export\s+(?:async\s+)?function\s+(\w+)", "function"),
    ]
    sigs = []
    for pattern, kind in sig_patterns:
        for m in re.finditer(pattern, content, re.MULTILINE):
            sig_line = m.group(0).strip()[:120]
            sigs.append(f"  [{kind}] {sig_line}")

    if sigs:
        return "[code structure]\n" + "\n".join(sigs[:50])
    return content


_REDUCERS = {
    "json": _reduce_json,
    "json_list": _reduce_json,
    "diff": _reduce_diff,
    "git": _reduce_diff,
    "build_output": _reduce_build,
    "build_error": _reduce_build,
    "log": _reduce_build,
    "code_rust": _reduce_code,
    "code_python": _reduce_code,
    "code_go": _reduce_code,
    "code_js": _reduce_code,
    "code_ts": _reduce_code,
    "code_sh": _reduce_code,
    "code": _reduce_code,
}


def compress_stage2(content: str, ccr_type: str) -> str | None:
    """Produce a semantically reduced version of content.

    Returns the reduced string if reduction was beneficial, or None if
    the content is too small or the reducer produced no savings.
    """
    if len(content) < _MIN_STAGE2_SIZE:
        return None

    reducer = _REDUCERS.get(ccr_type)
    if reducer is None:
        return None

    try:
        reduced = reducer(content)
    except Exception:
        return None

    if reduced == content or len(reduced) >= len(content):
        return None

    return reduced
