import json
import os
import stat

import pytest

import canvas_refresh as cr
import notes_backend as nb


FAKE = r'''#!/usr/bin/env bash
# Fake `claude` for tests. Records argv + stdin, answers per FAKE_CLAUDE_MODE.
printf '%s\n' "$@" > "$FAKE_CLAUDE_ARGS"
cat > "$FAKE_CLAUDE_STDIN"
case "${FAKE_CLAUDE_MODE:-ok}" in
  version) echo "9.9.9 (Claude Code)"; exit 0 ;;
  ok)
    sysfile=""
    prev=""
    for a in "$@"; do [ "$prev" = "--append-system-prompt-file" ] && sysfile="$a"; prev="$a"; done
    sys_head="$(head -c 40 "$sysfile" | tr '\n' ' ')"
    printf '{"type":"result","subtype":"success","is_error":false,"result":"# Cheat Sheet: Pave\\n\\n## Body\\n\\n- got system: %s","usage":{"input_tokens":1200,"output_tokens":340}}\n' "$sys_head"
    ;;
  ratelimit)
    printf '{"type":"result","is_error":true,"result":"You have hit your usage limit. Try again in 3 hours."}\n'; exit 1 ;;
  auth)
    echo "Not logged in · Please run /login" >&2; exit 1 ;;
  garbage)
    echo "Some notice first"; printf '{"type":"result","is_error":false,"result":"ok text"}\n' ;;
  empty)
    printf '{"type":"result","is_error":false,"result":""}\n' ;;
  maxturns)
    printf '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"","api_error_status":null}\n'; exit 1 ;;
esac
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.txt"))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.txt"))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    return tmp_path


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("NOTES_BACKEND", raising=False)
    monkeypatch.delenv("NOTES_MODEL", raising=False)
    assert nb.backend() == "claude-code"
    assert nb.model() == "claude-opus-5"
    monkeypatch.setenv("NOTES_BACKEND", "API")
    monkeypatch.setenv("NOTES_MODEL", "claude-sonnet-5")
    assert nb.backend() == "api" and nb.model() == "claude-sonnet-5"


def test_claude_version(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "version")
    assert nb.claude_version() == "9.9.9 (Claude Code)"


def test_generate_ok_passes_system_stdin_and_flags(fake_claude):
    text, usage = nb.generate_with_claude_code(
        fake_claude, "SYSTEM PROMPT HERE with [CLASS-SPECIFIC NOTES]", "do it",
        "=== CANVAS ASSIGNMENT POSTING ===\nq1", "claude-opus-5")
    assert text.startswith("# Cheat Sheet: Pave")
    assert "got system: SYSTEM PROMPT HERE" in text
    assert usage["input_tokens"] == 1200
    args = (fake_claude / "args.txt").read_text().splitlines()
    assert args[:2] == ["-p", "do it"]
    assert "--model" in args and args[args.index("--model") + 1] == "claude-opus-5"
    assert args[args.index("--allowedTools") + 1] == "Read"
    assert "--output-format" in args and "--append-system-prompt-file" in args
    assert "--bare" not in args
    assert (fake_claude / "stdin.txt").read_text() == "=== CANVAS ASSIGNMENT POSTING ===\nq1"


def test_rate_limit_and_auth_are_distinct(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ratelimit")
    with pytest.raises(nb.NotesRateLimited):
        nb.generate_with_claude_code(fake_claude, "s", "i", "c")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    with pytest.raises(nb.NotesAuthError):
        nb.generate_with_claude_code(fake_claude, "s", "i", "c")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "empty")
    with pytest.raises(nb.NotesUnavailable):
        nb.generate_with_claude_code(fake_claude, "s", "i", "c")


def test_tolerates_noise_before_json(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    text, _ = nb.generate_with_claude_code(fake_claude, "s", "i", "c")
    assert text == "ok text"


def test_missing_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(nb.NotesUnavailable):
        nb.generate_with_claude_code(tmp_path, "s", "i", "c")
    assert nb.claude_version() is None


def test_classify_failure():
    assert nb.classify_failure("429 Too Many Requests") is nb.NotesRateLimited
    assert nb.classify_failure("OAuth token expired") is nb.NotesAuthError
    assert nb.classify_failure("segfault") is nb.NotesUnavailable


# ── generate_notes end to end with the fake CLI ───────────────────────────────

def _session(session_dir, abbrev="LTV", date_str="260916"):
    posting = {"id": 1, "name": f"{abbrev} | Class 5: Pave (A)", "description": "<p>Q1? Q2?</p>",
               "submission_types": ["not_graded"], "due_at": "2026-09-16T12:00:00Z"}
    return {"abbrev": abbrev, "course_id": 1, "date_str": date_str, "due_dt": None,
            "assignments": [posting], "assignment": posting}


@pytest.fixture
def notes_env(patch_courses, fake_claude, monkeypatch):
    monkeypatch.setenv("NOTES_BACKEND", "claude-code")
    monkeypatch.setattr(cr, "_NOTES_BLOCKED", None)
    monkeypatch.setattr(cr, "_NOTES_MADE", 0)
    monkeypatch.setattr(cr, "NOTES_MAX", 0)
    monkeypatch.setattr(cr, "pdf_page_count", lambda p: 12)
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: [])
    course = patch_courses["courses"]["LTV"]["folder_path"]
    d = course / "260916 Class 5 - Pave (A)"; d.mkdir()
    (d / "260916 Pave.pdf").write_bytes(b"%PDF-1.4 fake")
    (d / "Pave DRAFT.docx").write_bytes(b"PK")   # unreadable → skipped
    return d


def test_generate_notes_via_claude_code_writes_cheat_sheet(notes_env, fake_claude, monkeypatch, capsys):
    monkeypatch.setattr(cr.ai_config, "extract_text", lambda p: "draft text")
    d = notes_env
    cr.generate_notes(_session(d))

    assert (d / "Cheat Sheet - Pave (A).docx").exists()
    md = (d / ".Cheat Sheet - Pave (A).md").read_text()
    assert "# Launching Tech Ventures: September 16, 2026" in md
    assert "## Body" in md
    meta = json.loads((d / ".notes_meta.json").read_text())
    assert meta["notes_file"] == "Cheat Sheet - Pave (A).docx"
    assert meta["backend"] == "claude-code"
    assert "260916 Pave.pdf" in meta["readings_included"]

    stdin = (fake_claude / "stdin.txt").read_text()
    assert "=== CANVAS ASSIGNMENT POSTING ===" in stdin and "Q1? Q2?" in stdin
    assert "=== READINGS ON DISK" in stdin and "260916 Pave.pdf (12 pages)" in stdin
    assert "=== NOTE: Pave DRAFT.docx ===\ndraft text" in stdin
    assert cr._NOTES_MADE == 1

    # Nothing changed → not stale (the adopted-name lookup finds the cheat sheet).
    stale, reason = cr.notes_are_stale(d, "LTV", "260916", cr.session_hashes(_session(d)),
                                       title="Pave (A)")
    assert (stale, reason) == (False, "up to date")


def test_rate_limit_blocks_rest_of_run(notes_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ratelimit")
    gh_out = tmp_path / "gh_out"; monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    d = notes_env
    cr.generate_notes(_session(d))
    assert not (d / "Cheat Sheet - Pave (A).docx").exists()
    assert cr._NOTES_BLOCKED and "usage limit" in cr._NOTES_BLOCKED
    assert "notes_skipped=true" in gh_out.read_text()

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    cr.generate_notes(_session(d))          # still blocked for this run
    assert not (d / "Cheat Sheet - Pave (A).docx").exists()
    assert "skipped" in capsys.readouterr().out


def test_notes_max_caps_a_run(notes_env, monkeypatch):
    monkeypatch.setattr(cr, "NOTES_MAX", 1)
    d = notes_env
    cr.generate_notes(_session(d))
    assert (d / "Cheat Sheet - Pave (A).docx").exists()
    other = d.parent / "260923 Class 6 - Weiss"; other.mkdir()
    (other / "260923 Weiss.pdf").write_bytes(b"%PDF-1.4 fake")
    cr.generate_notes(_session(other, date_str="260923"))
    assert not list(other.glob("Cheat Sheet*"))


def test_api_backend_still_selectable(notes_env, monkeypatch):
    monkeypatch.setenv("NOTES_BACKEND", "api")
    calls = {}
    monkeypatch.setattr(cr, "_generate_via_api", lambda p, r, s: calls.update(n=1) or "# api notes")
    d = notes_env
    cr.generate_notes(_session(d))
    assert calls == {"n": 1}
    assert (d / ".Cheat Sheet - Pave (A).md").read_text().endswith("# api notes\n")


def test_failure_message_carries_subtype_and_exit_code(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "maxturns")
    with pytest.raises(nb.NotesUnavailable) as e:
        nb.generate_with_claude_code(fake_claude, "sys", "do", "ctx", "claude-opus-5")
    assert "subtype=error_max_turns" in str(e.value) and "(exit 1)" in str(e.value)


# ── readings guard ────────────────────────────────────────────────────────────

CASE_DESC = ('<p>Read the cases <a href="https://hbs.instructure.com/courses/1/files/77?verifier=ab">MFS</a>'
             ' and come prepared.</p>')


def _case_session(session_dir, hours_ahead, desc=CASE_DESC):
    from datetime import datetime, timedelta
    due = datetime.now(tz=cr.BOSTON) + timedelta(hours=hours_ahead)
    posting = {"id": 7, "name": "GTD | Class 7: MFS", "description": desc,
               "submission_types": ["none"], "due_at": due.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"abbrev": "LTV", "course_id": 1, "date_str": session_dir.name[:6], "due_dt": due,
            "assignments": [posting], "assignment": posting}


def test_readings_expected_and_missing(notes_env):
    d = notes_env.parent / "260924 Class 7 - MFS"; d.mkdir()
    (d / "260921 Announcement - Note.md").write_text("# hi")
    (d / "260924 3) Video (YouTube).txt").write_text("link")
    s = _case_session(d, 72)
    assert cr.readings_expected(s["assignments"])
    assert cr.readings_missing(s, d)
    (d / "Massachusetts Financial Services.pdf").write_bytes(b"%PDF")
    assert cr.readings_missing(s, d) is None
    guest = _case_session(d, 72, desc="<p>Guest speaker: bring questions.</p>")
    assert not cr.readings_expected(guest["assignments"])


def test_notes_deferred_until_readings_arrive(notes_env, capsys):
    d = notes_env.parent / "260924 Class 7 - MFS"; d.mkdir()
    cr.generate_notes(_case_session(d, 72))
    assert not list(d.glob("Cheat Sheet*"))
    assert "notes deferred" in capsys.readouterr().out
    assert cr._NOTES_MADE == 0

    (d / "Massachusetts Financial Services.pdf").write_bytes(b"%PDF-1.4 fake")
    cr.generate_notes(_case_session(d, 72))
    assert (d / "Cheat Sheet - MFS.docx").exists()
    assert "Generated without the readings" not in (d / ".Cheat Sheet - MFS.md").read_text()


def test_notes_generated_with_banner_close_to_class(notes_env, capsys):
    d = notes_env.parent / "260924 Class 7 - MFS"; d.mkdir()
    cr.generate_notes(_case_session(d, 10))
    assert "generating from the posting alone" in capsys.readouterr().out
    md = (d / ".Cheat Sheet - MFS.md").read_text()
    assert "Generated without the readings" in md and "## Body" in md


def test_podcast_pass_waits_for_readings(notes_env, monkeypatch, capsys):
    d = notes_env.parent / "260924 Class 7 - MFS"; d.mkdir()
    waiting = _case_session(d, 72)
    ready = _case_session(notes_env, 72)          # notes_env has a PDF
    monkeypatch.setattr(cr, "get_upcoming_sessions", lambda **k: [waiting, ready])
    made = []
    monkeypatch.setattr(cr, "generate_podcast_for_session", lambda s: made.append(s["date_str"]))
    status = []
    monkeypatch.setattr(cr, "_write_podcast_status", lambda pending: status.append([s["date_str"] for s in pending]))
    cr.run_podcast_pass(7)
    assert made == ["260916"]
    assert status[-1] == ["260916"]                # the waiting one is not "pending"
    assert "wait for their readings: LTV 260924" in capsys.readouterr().out
