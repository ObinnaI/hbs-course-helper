"""
notes_backend.py — Generate notes through Claude Code on a subscription.

Two ways to ask Claude for a cheat sheet:

  api          the Anthropic Messages API with ANTHROPIC_API_KEY, billed per
               token (the original path; still in canvas_refresh.generate_notes)
  claude-code  `claude -p` — Claude Code's headless mode — signed in with a
               Claude Pro/Max subscription. On a Mac that is the CLI's own
               login; in CI it is CLAUDE_CODE_OAUTH_TOKEN from `claude
               setup-token`. Uses the plan's allowance instead of API credit.

Only the second is implemented here. The model is given the session folder as
its working directory and the Read tool, so it reads the PDFs itself (in
≤20-page chunks); everything else — Canvas postings, extracted Word/Excel
text, the course brief — arrives on stdin.

Settings (environment or .env):
  NOTES_BACKEND   api | claude-code        default claude-code
  NOTES_MODEL     model for claude-code    default claude-opus-5
  NOTES_MAX_TURNS                          default 150
  NOTES_TIMEOUT   seconds per session      default 1500
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_BACKEND = "claude-code"
DEFAULT_MODEL   = "claude-opus-5"


class NotesError(Exception):
    """Base: the notes could not be produced this run."""


class NotesRateLimited(NotesError):
    """The subscription's allowance is used up for now — try again later."""


class NotesAuthError(NotesError):
    """Claude Code is not signed in (expired OAuth token, no login)."""


class NotesUnavailable(NotesError):
    """CLI missing, timed out, or another non-retryable failure."""


# ── Settings ──────────────────────────────────────────────────────────────────

def _setting(key: str, default: str, cfg=None) -> str:
    if cfg is not None:
        v = cfg(key)
        if v:
            return v
    return os.getenv(key, "") or default


def backend(cfg=None) -> str:
    return _setting("NOTES_BACKEND", DEFAULT_BACKEND, cfg).strip().lower()


def model(cfg=None) -> str:
    return _setting("NOTES_MODEL", DEFAULT_MODEL, cfg).strip()


# ── Claude Code ───────────────────────────────────────────────────────────────

def claude_version() -> "str | None":
    """'2.1.0 (Claude Code)' or None when the CLI is not installed."""
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


_RATE_RE = re.compile(r"rate.?limit|usage limit|hit your limit|limit reached|overloaded|too many requests|\b429\b|try again (later|in)", re.I)
_AUTH_RE = re.compile(r"not logged in|please (run )?/?login|log ?in|authenticat|unauthori[sz]ed|oauth|token.*(invalid|expired|revoked)|\b401\b|invalid api key", re.I)


def _parse_result(stdout: str) -> dict:
    """
    `--output-format json` prints one result object. Be tolerant of anything
    printed before it (a deprecation notice, a hook) by taking the last JSON
    object in the output.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"result": stdout}


def classify_failure(message: str) -> type:
    if _RATE_RE.search(message or ""):
        return NotesRateLimited
    if _AUTH_RE.search(message or ""):
        return NotesAuthError
    return NotesUnavailable


def generate_with_claude_code(cwd: Path, system_prompt: str, instruction: str,
                              context: str, model_name: "str | None" = None,
                              cfg=None) -> "tuple[str, dict]":
    """
    Run one headless Claude Code turn in `cwd` and return (markdown, usage).

    system_prompt  the master prompt (+ course refinement) — appended to the
                   CLI's system prompt from a temp file
    instruction    the short user prompt (`-p`)
    context        the long material (postings, extracted text, course brief);
                   piped on stdin so it never hits the argv length limit
    """
    exe = shutil.which("claude")
    if not exe:
        raise NotesUnavailable(
            "the `claude` CLI is not installed — npm install -g @anthropic-ai/claude-code")

    model_name = model_name or model(cfg)
    max_turns  = _setting("NOTES_MAX_TURNS", "150", cfg)
    timeout    = float(_setting("NOTES_TIMEOUT", "1500", cfg))

    env = dict(os.environ)
    # Inside an interactive Claude Code session these mark "already running";
    # a nested headless call must not inherit them.
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     prefix="notes-system-") as fh:
        fh.write(system_prompt)
        sys_path = fh.name

    cmd = [exe, "-p", instruction,
           "--model", model_name,
           "--allowedTools", "Read",
           "--max-turns", str(max_turns),
           "--output-format", "json",
           "--append-system-prompt-file", sys_path]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), input=context, capture_output=True,
                              text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise NotesUnavailable(f"claude timed out after {int(timeout)}s")
    except OSError as e:
        raise NotesUnavailable(f"could not run claude: {e}")
    finally:
        try:
            os.unlink(sys_path)
        except OSError:
            pass

    data = _parse_result(proc.stdout)
    text = data.get("result") if isinstance(data.get("result"), str) else ""
    if proc.returncode != 0 or data.get("is_error"):
        bits = [text.strip(), (proc.stderr or "").strip()[-400:]]
        for key in ("subtype", "api_error_status"):
            if data.get(key) not in (None, "", "success"):
                bits.append(f"{key}={data[key]}")
        if not data and (proc.stdout or "").strip():
            bits.append("stdout: " + proc.stdout.strip()[-300:])
        msg = " ".join(b for b in bits if b).strip()[:600]
        raise classify_failure(msg)(f"{msg or 'no output'} (exit {proc.returncode})")
    if not text.strip():
        raise NotesUnavailable("claude returned an empty result")
    return text, (data.get("usage") or {})


def describe_usage(usage: dict) -> str:
    if not usage:
        return ""
    parts = []
    for key, label in (("input_tokens", "in"), ("output_tokens", "out"),
                       ("cache_read_input_tokens", "cached")):
        if usage.get(key):
            parts.append(f"{usage[key]:,} {label}")
    return ", ".join(parts)
