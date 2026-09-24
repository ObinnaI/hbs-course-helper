import json
import os
import time
from datetime import datetime, timedelta

import pytest

import canvas_refresh as cr
from canvas_common import classify_submission


# ── classify_submission ───────────────────────────────────────────────────────

@pytest.mark.parametrize("sub_types, expected", [
    (["not_graded"], "session"),
    (["none"], "session"),
    (["online_quiz"], "deliverable"),
    (["online_upload", "online_text_entry"], "deliverable"),
    (["on_paper"], "deliverable"),
    ([], "ambiguous"),
    (None, "ambiguous"),
    (["discussion_topic"], "ambiguous"),
])
def test_classify_submission(sub_types, expected):
    assert classify_submission(sub_types) == expected


# ── get_upcoming_sessions ─────────────────────────────────────────────────────

def _due(hours_ahead: float) -> str:
    dt = datetime.now(tz=cr.BOSTON) + timedelta(hours=hours_ahead)
    return dt.astimezone(cr.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _due_tomorrow(hour: int) -> str:
    """A fixed clock time tomorrow (Boston), so two postings share a day
    whatever time of day the suite runs."""
    dt = (datetime.now(tz=cr.BOSTON) + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return dt.astimezone(cr.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assignment(aid, name, sub_types, hours_ahead=None, desc="<p>q</p>", hour=None):
    due = _due(hours_ahead) if hours_ahead is not None else _due_tomorrow(hour or 10)
    return {"id": aid, "name": name, "submission_types": sub_types,
            "due_at": due, "description": desc}


def test_quiz_on_class_day_is_not_a_session(patch_courses, monkeypatch, capsys):
    posts = [
        _assignment(1, "LTV | Class 3 | Ginkgo", ["not_graded"], hour=10),
        _assignment(2, "Quiz 1", ["online_quiz"], hour=12),
    ]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)

    sessions = cr.get_upcoming_sessions(horizon_days=2)

    assert len(sessions) == 1
    assert [a["id"] for a in sessions[0]["assignments"]] == [1]
    assert sessions[0]["assignment"]["name"] == "LTV | Class 3 | Ginkgo"
    assert "skipping deliverable 'Quiz 1'" in capsys.readouterr().out

    # Explicitly asking for deliverables merges them into the same day.
    both = cr.get_upcoming_sessions(horizon_days=2, kinds=("session", "deliverable"))
    assert [a["id"] for a in both[0]["assignments"]] == [1, 2]


def test_two_class_postings_same_day_merge(patch_courses, monkeypatch):
    posts = [
        _assignment(2, "Class 4 (afternoon)", ["not_graded"], hour=14),
        _assignment(1, "Class 3 (morning)", ["not_graded"], hour=9),
    ]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)

    sessions = cr.get_upcoming_sessions(horizon_days=2)

    assert len(sessions) == 1
    assert [a["id"] for a in sessions[0]["assignments"]] == [1, 2]
    assert sessions[0]["due_dt"] == cr.boston_date(posts[1]["due_at"])


def test_past_and_far_future_excluded(patch_courses, monkeypatch):
    posts = [
        _assignment(1, "yesterday", ["not_graded"], hours_ahead=-5),
        _assignment(2, "next month", ["not_graded"], hours_ahead=24 * 30),
        _assignment(3, "tomorrow", ["not_graded"], hour=10),
    ]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)
    assert [s["assignment"]["id"] for s in cr.get_upcoming_sessions(2)] == [3]


def test_build_session_without_posting(patch_courses, monkeypatch):
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: [])
    s = cr.build_session("LTV", "260902")
    assert s["assignments"] == [] and s["assignment"] is None
    assert s["course_id"] == 1 and s["date_str"] == "260902"


# ── notes_are_stale ───────────────────────────────────────────────────────────

@pytest.fixture
def session_dir(patch_courses):
    d = patch_courses["courses"]["LTV"]["folder_path"] / "260902 LTV"
    d.mkdir()
    (d / "260902 LTV Notes.docx").write_bytes(b"PK")
    (d / "case.pdf").write_bytes(b"%PDF-1")
    return d


def _meta_for(session_dir, abbrev="LTV", hashes=None):
    return {
        "assignments": hashes or {"42": "abc"},
        "prompt_hash": cr.prompt_hash(abbrev),
        "readings":    cr.readings_fingerprint(session_dir),
    }


def test_missing_notes_is_stale(patch_courses):
    d = patch_courses["courses"]["LTV"]["folder_path"] / "260902 LTV"
    d.mkdir()
    (d / "260902 LTV Notes.md").write_text("only markdown")
    assert cr.notes_are_stale(d, "LTV", "260902") == (True, "no Notes file yet")


def test_up_to_date_ignores_mtimes(session_dir):
    cr._write_notes_meta(session_dir, _meta_for(session_dir))

    # Simulate a fresh git checkout: every file is newer than the Notes.
    future = time.time() + 3600
    for f in session_dir.iterdir():
        os.utime(f, (future, future))
    os.utime(cr.PROMPT_FILE, None) if cr.PROMPT_FILE.exists() else None

    stale, reason = cr.notes_are_stale(session_dir, "LTV", "260902",
                                       canvas_hashes={"42": "abc"})
    assert (stale, reason) == (False, "up to date")


def test_changed_reading_content_is_stale(session_dir):
    cr._write_notes_meta(session_dir, _meta_for(session_dir))
    (session_dir / "case.pdf").write_bytes(b"%PDF-2")
    stale, reason = cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "abc"})
    assert stale and reason == "reading changed: case.pdf"


def test_new_reading_is_stale(session_dir):
    cr._write_notes_meta(session_dir, _meta_for(session_dir))
    (session_dir / "article.pdf").write_bytes(b"%PDF")
    stale, reason = cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "abc"})
    assert stale and reason == "new reading: article.pdf"


def test_skip_stub_does_not_count_as_reading(session_dir):
    cr._write_notes_meta(session_dir, _meta_for(session_dir))
    (session_dir / "case (skipped).txt").write_text("NOT included")
    assert cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "abc"})[0] is False


def test_canvas_description_change_is_stale(session_dir):
    cr._write_notes_meta(session_dir, _meta_for(session_dir))
    stale, reason = cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "zzz"})
    assert stale and reason == "Canvas assignment description changed"


def test_prompt_change_is_stale_unless_skipped(session_dir, monkeypatch):
    cr._write_notes_meta(session_dir, _meta_for(session_dir))
    monkeypatch.setattr(cr, "prompt_hash", lambda abbrev: "different")
    assert cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "abc"}) \
        == (True, "prompt updated")
    assert cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "abc"},
                              skip_prompt_regen=True)[0] is False


def test_legacy_meta_migrates(session_dir):
    (session_dir / ".notes_meta.json").write_text(json.dumps({"canvas_hash": "abc"}))
    # Legacy files carry no reading/prompt fingerprints, so the mtime check
    # runs once; make the Notes newest so it passes.
    future = time.time() + 3600
    os.utime(session_dir / "260902 LTV Notes.docx", (future, future))

    assert cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "abc"},
                              skip_prompt_regen=True) == (False, "up to date")
    assert cr.notes_are_stale(session_dir, "LTV", "260902", {"42": "new"},
                              skip_prompt_regen=True)[0] is True

    cr._write_notes_meta(session_dir, {"assignments": {"42": "abc"}})
    stored = json.loads((session_dir / ".notes_meta.json").read_text())
    assert stored == {"assignments": {"42": "abc"}}


def test_session_hashes_cover_every_posting():
    s = {"assignments": [{"id": 1, "description": "<p>a</p>"},
                         {"id": 2, "description": "<p>b</p>"}]}
    hashes = cr.session_hashes(s)
    assert set(hashes) == {"1", "2"} and hashes["1"] != hashes["2"]
    legacy = {"assignment": {"id": 7, "description": ""}}
    assert set(cr.session_hashes(legacy)) == {"7"}


# ── write_markdown ────────────────────────────────────────────────────────────

def test_write_markdown(tmp_path):
    out = tmp_path / "x.md"
    cr.write_markdown(out, "Title Here", {"Generated": "now", "Canvas": "C"}, "## Body\n- one\n")
    text = out.read_text()
    assert text.startswith("# Title Here\n\n**Generated:** now  \n**Canvas:** C  \n\n## Body")
    assert text.endswith("- one\n")


# ── classification routed through deliverables.py ─────────────────────────────

def test_text_entry_class_posting_is_a_session(patch_courses, monkeypatch, capsys):
    posts = [
        _assignment(1, "Industrial Policy at Scale: Electric Vehicles", ["online_text_entry"], hour=10,
                    desc="<p>Introduction … Materials Case: Tariffs, Bans, and Subsidies</p>"),
        _assignment(2, "GEO Assignment #1: Early Semester Action Items", ["not_graded"], hour=12,
                    desc="<p>Sign the waiver</p>"),
    ]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)
    monkeypatch.setattr(cr, "_OVERRIDES", {})
    sessions = cr.get_upcoming_sessions(horizon_days=2)
    assert [a["id"] for a in sessions[0]["assignments"]] == [1]
    assert "skipping deliverable 'GEO Assignment #1" in capsys.readouterr().out


def test_class_day_hand_in_joins_the_session(patch_courses, monkeypatch):
    posts = [_assignment(6, "Class 7: Midterm Presentation DUE", ["online_upload"], hour=13)]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)
    monkeypatch.setattr(cr, "_OVERRIDES", {})
    assert [a["id"] for a in cr.get_upcoming_sessions(horizon_days=2)[0]["assignments"]] == [6]
    assert cr.get_upcoming_sessions(horizon_days=2, kinds=("deliverable",))[0]["assignments"][0]["id"] == 6


def test_quiet_day_still_syncs_tasks(patch_courses, monkeypatch):
    monkeypatch.setattr(cr, "get_upcoming_sessions", lambda **k: [])
    calls = []
    monkeypatch.setattr(cr, "_sync_tasks", lambda dry_run=False: calls.append(dry_run))
    cr.run_daily()
    assert calls == [False]


def test_sync_tasks_gating(monkeypatch, capsys):
    monkeypatch.delenv("TASKS_BACKEND", raising=False)
    cr._sync_tasks()
    assert "skipped (TASKS_BACKEND=none)" in capsys.readouterr().out
    monkeypatch.setenv("TASKS_BACKEND", "todoist")
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
    cr._sync_tasks()
    assert "TODOIST_API_TOKEN not set" in capsys.readouterr().out
    monkeypatch.setenv("TODOIST_API_TOKEN", "x")
    import todoist_sync
    def boom(dry_run=False):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(todoist_sync, "run", boom)
    cr._sync_tasks()
    assert "Task sync failed: kaboom" in capsys.readouterr().out


def test_visible_md_twins_are_hidden(tmp_path):
    d = tmp_path / "Fall" / "MP" / "260924 Class 7 - MFS"; d.mkdir(parents=True)
    (d / "Cheat Sheet - MFS.docx").write_bytes(b"PK")
    (d / "Cheat Sheet - MFS.md").write_text("# twin")
    (d / "Course Brief.md").write_text("keep")                  # not a cheat sheet
    other = tmp_path / "Fall" / "MP" / "260925 Class 8 - Yard"; other.mkdir()
    (other / "Cheat Sheet - Yard.md").write_text("user's own, no docx")
    assert cr.hide_notes_twins(tmp_path) == 1
    assert (d / ".Cheat Sheet - MFS.md").read_text() == "# twin"
    assert not (d / "Cheat Sheet - MFS.md").exists()
    assert (other / "Cheat Sheet - Yard.md").exists()            # no Word file: left alone
    assert cr.hide_notes_twins(tmp_path) == 0


def test_daily_horizon_setting(monkeypatch):
    monkeypatch.delenv("DAILY_HORIZON_DAYS", raising=False)
    assert cr.daily_horizon_days() == 3
    monkeypatch.setenv("DAILY_HORIZON_DAYS", "5")
    assert cr.daily_horizon_days() == 5
    monkeypatch.setenv("DAILY_HORIZON_DAYS", "junk")
    assert cr.daily_horizon_days() == 3
