import json
from datetime import date

import pytest

import canvas_common as cc
import canvas_refresh as cr
import course_brief as cb
import notes_backend as nb


@pytest.fixture
def course(patch_courses, tmp_path):
    folder = patch_courses["courses"]["LTV"]["folder_path"]
    (folder / "Course Textbook and Materials").mkdir()
    (folder / "Course Textbook and Materials" / "Syllabus LTV.pdf").write_bytes(b"%PDF-1.4")
    past = folder / "260909 Class 4 - Equity"; past.mkdir()
    (past / "260909 Equity.pdf").write_bytes(b"%PDF-1.4")
    (past / "Cheat Sheet - Equity.md").write_text(
        "# Cheat Sheet: Equity\n\n### Q1. Why?\n**The 20-second answer, say this first:** because.\n"
        "**A. Options are levered**\ndetail\n**Discussion-ready synthesis:** say this.\n")
    (past / ".notes_meta.json").write_text(json.dumps(
        {"readings": {"260909 Equity.pdf": "x"}, "notes_file": "Cheat Sheet - Equity.docx"}))
    (past / "260910 Announcement - Wrap-up.md").write_text("# Wrap-up\n\nKey lens: vesting cliffs.\n")
    nxt = folder / "260923 Class 6 - Weiss"; nxt.mkdir()
    return folder


def _fake_ask(responses):
    calls = []
    def ask(cwd, system, instruction, context):
        calls.append((cwd, system, instruction, context))
        return responses.pop(0), {}
    return ask, calls


def test_materials_dir_adopts_existing_folder(course, tmp_path):
    assert cc.materials_dir_name(course) == "Course Textbook and Materials"
    fresh = tmp_path / "fresh"; fresh.mkdir()
    assert cc.materials_dir_name(fresh) == "Course Materials"
    (fresh / "General").mkdir()
    assert cc.materials_dir_name(fresh) == "General"
    (fresh / "Course Materials").mkdir()
    assert cc.materials_dir_name(fresh) == "Course Materials"


def test_ensure_brief_and_claude_md(course):
    p = cb.ensure_brief(course, "LTV", "Launching Tech Ventures")
    assert p == course / "Course Textbook and Materials" / "Course Brief.md"
    text = p.read_text()
    assert text.startswith("# Launching Tech Ventures — course brief")
    assert set(cb.blocks(text)) == {"about", "lenses-index", "threads", "materials"}
    claude_md = (course / "CLAUDE.md").read_text()
    assert "Course Textbook and Materials/Course Brief.md" in claude_md
    # Idempotent and never overwrites edits.
    p.write_text(text.replace("_Your notes:", "MY EDIT"))
    cb.ensure_brief(course, "LTV", "Launching Tech Ventures")
    assert "MY EDIT" in p.read_text()


def test_upsert_block_preserves_hand_edits():
    text = cb.TEMPLATE.format(name="X", code="X")
    text = text.replace("_Your notes:", "hand-written paragraph")
    text = cb.upsert_block(text, "class-260909", "### Class 4\n**Lenses:** a")
    text = cb.upsert_block(text, "class-260909", "### Class 4\n**Lenses:** b")
    assert text.count("<!-- auto:class-260909 -->") == 1
    assert "**Lenses:** b" in text and "**Lenses:** a" not in text
    assert "hand-written paragraph" in text
    idx = text.index("<!-- /auto:lenses-index -->")
    assert text.index("<!-- auto:class-260909 -->") > idx


def test_bottom_lines_keeps_the_spine():
    md = ("# T\nlong paragraph\n### Q1. Why?\n**The 20-second answer, say this first:** x\n"
          "**A. Point**\nevidence evidence\n**Discussion-ready synthesis:** y\n- bullet\n")
    out = cb.bottom_lines(md)
    assert "# T" in out and "### Q1. Why?" in out and "**A. Point**" in out
    assert "evidence evidence" not in out and "- bullet" not in out


def test_update_course_writes_blocks_once(course, monkeypatch):
    ask, calls = _fake_ask([
        "**Launching Tech Ventures** is about building startups.",     # About (syllabus found)
        "- `Syllabus LTV.pdf`: the syllabus; grading and schedule",    # materials index
        "### Class 4 - Equity\n**Lenses and frameworks introduced:** leverage\n"
        "**Key takeaways:**\n- vesting cliffs matter\n**Threads to carry forward:**\n- revisit in class 8\n",
    ])
    monkeypatch.setattr(cb, "_ask", ask)
    monkeypatch.setattr(cb, "_excerpt", lambda f, limit=3000: "syllabus text")
    monkeypatch.setattr(cr.ai_config, "extract_text", lambda p: "wrap text")

    info = {"folder_path": course, "full_name": "Launching Tech Ventures", "term_end": None}
    cb.update_course("LTV", info, today=date(2026, 9, 18))

    text = cb.brief_path(course).read_text()
    b = cb.blocks(text)
    assert "class-260909" in b and "class-260923" not in b       # only past classes
    assert "vesting cliffs matter" in b["class-260909"]
    assert "- revisit in class 8 _(Class 4 - Equity)_" in b["threads"]
    assert "`Syllabus LTV.pdf`: the syllabus" in b["materials"]
    assert "is about building startups" in b["about"]
    assert "- 260909 Class 4 - Equity" in b["lenses-index"]
    # The class call saw the cheat sheet spine and the wrap-up.
    ctx = calls[-1][3]
    assert "=== CHEAT SHEET" in ctx and "20-second answer" in ctx
    assert "=== POST-CLASS: 260910 Announcement - Wrap-up.md ===" in ctx
    assert "=== READINGS THAT DAY ===\n- 260909 Equity.pdf" in ctx

    # Second run: nothing changed → no Claude calls at all.
    n = len(calls)
    cb.update_course("LTV", info, today=date(2026, 9, 18))
    assert len(calls) == n
    # A new wrap-up file → that class block is refreshed.
    (course / "260909 Class 4 - Equity" / "Wrap-up slides.pdf").write_bytes(b"%PDF")
    ask2, calls2 = _fake_ask(["### Class 4 - Equity\n**Lenses and frameworks introduced:** x\n**Key takeaways:**\n- updated\n"])
    monkeypatch.setattr(cb, "_ask", ask2)
    cb.update_course("LTV", info, today=date(2026, 9, 18))
    assert len(calls2) == 1 and "FILES ON DISK" in calls2[0][3]
    assert calls2[0][3].count("- Wrap-up slides.pdf") == 1        # listed once, as post-class
    assert "- updated" in cb.brief_path(course).read_text()


def test_rate_limit_leaves_brief_intact(course, monkeypatch):
    def boom(*a, **k):
        raise nb.NotesRateLimited("limit")
    monkeypatch.setattr(cb, "_ask", boom)
    info = {"folder_path": course, "full_name": "LTV", "term_end": None}
    cb.update_course("LTV", info, today=date(2026, 9, 18))
    text = cb.brief_path(course).read_text()
    assert "class-260909" not in cb.blocks(text)
    assert "(not yet summarised)" in cb.blocks(text)["materials"]


def test_context_for_previous_next_and_index(course, monkeypatch):
    cb.ensure_brief(course, "LTV", "Launching Tech Ventures")
    cb._save_json(cb._index_path(course), {"Syllabus LTV.pdf": {"md5": "x", "summary": "the syllabus"}})
    posts = [
        {"id": 1, "name": "LTV | Class 4: Equity", "due_at": "2026-09-09T12:00:00Z",
         "submission_types": ["not_graded"], "description": ""},
        {"id": 2, "name": "LTV | Class 5: Pave", "due_at": "2026-09-16T12:00:00Z",
         "submission_types": ["not_graded"], "description": "<p>Q?</p>"},
        {"id": 3, "name": "LTV | Class 6: Weiss", "due_at": "2026-09-23T12:00:00Z",
         "submission_types": ["not_graded"],
         "description": '<p>Read <a href="https://hbsp.harvard.edu/tu/1">Gerald Weiss (2023)</a>. Why?</p>'},
    ]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)
    session = {"abbrev": "LTV", "course_id": 1, "date_str": "260916", "assignments": [posts[1]]}
    sdir = course / "260916 Class 5 - Pave"; sdir.mkdir()

    ctx = cb.context_for(session, sdir)
    assert "=== COURSE BRIEF ===" in ctx and "Launching Tech Ventures — course brief" in ctx
    assert "=== PREVIOUS CLASS ===\nFolder: 260909 Class 4 - Equity" in ctx
    assert "Bottom lines from its cheat sheet:" in ctx and "20-second answer" in ctx
    assert "260910 Announcement - Wrap-up.md" in ctx and "Key lens: vesting cliffs." in ctx
    assert "=== NEXT CLASS (peek) ===\n260923: LTV | Class 6: Weiss" in ctx
    assert "Readings: Gerald Weiss (2023)" in ctx and "Why?" in ctx
    assert "=== MATERIALS INDEX" in ctx and "Course Textbook and Materials/Syllabus LTV.pdf`: the syllabus" in ctx
    assert "<!-- auto:materials -->" not in ctx


# ── notes freeze after class; post-class sync routing ─────────────────────────

def test_notes_frozen_after_class(patch_courses, monkeypatch):
    d = patch_courses["courses"]["LTV"]["folder_path"] / "260902 LTV"; d.mkdir()
    (d / "260902 LTV Notes.docx").write_bytes(b"PK")
    cr._write_notes_meta(d, {"assignments": {"42": "abc"}, "prompt_hash": "p", "readings": {}})
    (d / "wrap-up.pdf").write_bytes(b"%PDF")                       # arrived after class
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 18))
    assert cr.notes_are_stale(d, "LTV", "260902", {"42": "zzz"}) == (False, "class is past; notes frozen")
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 1))   # before class: still live
    assert cr.notes_are_stale(d, "LTV", "260902", {"42": "abc"})[0] is True   # new reading


def test_sync_modules_and_announcements_route_by_class_number(patch_courses, monkeypatch, tmp_path):
    folder = patch_courses["courses"]["LTV"]["folder_path"]
    monkeypatch.setattr(cr.path_config, "CONFIG_FILE", tmp_path / "claude" / "canvas_config.json")
    posts = [{"id": 1, "name": "LTV | Class 4: Equity", "due_at": "2026-09-09T12:00:00Z",
              "submission_types": ["not_graded"], "description": ""}]
    modules = [{"id": 10, "name": "Class 4 materials", "items": [
                    {"id": 100, "type": "File", "title": "Wrap-up slides", "content_id": 500},
                    {"id": 101, "type": "Page", "title": "How to read a 10-K", "page_url": "how-to"},
                    {"id": 102, "type": "SubHeader", "title": "x"}]},
               {"id": 11, "name": "Course resources", "items": [
                    {"id": 103, "type": "File", "title": "Syllabus", "content_id": 501}]}]
    announcements = [{"id": 7, "title": "Class 4 recap", "posted_at": "2026-09-10T15:00:00Z",
                      "message": "<p>Great discussion. Remember <b>vesting</b>.</p>", "attachments": []},
                     {"id": 8, "title": "Guest speaker next month", "posted_at": "2026-09-11T15:00:00Z",
                      "message": "<p>Save the date.</p>"},
                     {"id": 9, "title": "Ancient", "posted_at": "2026-01-01T15:00:00Z", "message": "old"}]
    files = {500: {"url": "https://c/500", "display_name": "Wrap-up slides.pdf"},
             501: {"url": "https://c/501", "display_name": "Syllabus.pdf"}}
    pages = {"how-to": {"title": "How to read a 10-K", "body": "<p>Start with the MD&amp;A.</p>"}}

    def fake_get(path, params=None):
        if path.endswith("/assignments"): return posts
        if path.endswith("/modules"): return modules
        if path.endswith("/discussion_topics"): return announcements
        if path.startswith("files/"): return files[int(path.split("/")[1])]
        if "/pages/" in path: return pages[path.split("/pages/")[1]]
        return []
    downloaded = []
    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"%PDF"); downloaded.append(dest); return True
    monkeypatch.setattr(cr, "canvas_get", fake_get)
    monkeypatch.setattr(cr, "canvas_download", fake_download)
    monkeypatch.setattr(cr, "datetime", _FrozenDatetime)

    synced = {}
    n = cr.sync_modules(1, "LTV", folder, synced)
    n += cr.sync_announcements(1, "LTV", folder, synced, days=21)

    cls = folder / "260909 Class 4 - Equity"
    assert (cls / "Wrap-up slides.pdf").exists()
    assert (cls / "How to read a 10-K.md").read_text().startswith("# How to read a 10-K\n\nStart with the MD&A.")
    assert (folder / "Course Materials" / "Syllabus.pdf").exists()
    assert (cls / "260910 Announcement - Class 4 recap.md").read_text().count("vesting") == 1
    assert (folder / "Course Materials" / "Announcements" / "260911 Announcement - Guest speaker next month.md").exists()
    assert not any("Ancient" in p.name for p in folder.rglob("*.md"))
    assert n == 5
    assert set(synced["1"]) == {"module:100", "module:101", "module:103", "announcement:7", "announcement:8"}

    # Second pass: everything remembered, nothing re-fetched.
    before = len(downloaded)
    assert cr.sync_modules(1, "LTV", folder, synced) == 0
    assert cr.sync_announcements(1, "LTV", folder, synced, days=21) == 0
    assert len(downloaded) == before


class _FrozenDatetime(cr.datetime):
    @classmethod
    def now(cls, tz=None):
        return cr.datetime(2026, 9, 18, 12, 0, tzinfo=tz or cr.timezone.utc)


def test_docx_cheat_sheet_is_read_and_complaints_are_rejected(course, monkeypatch):
    past = course / "260909 Class 4 - Equity"
    (past / "Cheat Sheet - Equity.md").unlink()           # adopted folders have only the .docx
    (past / "Cheat Sheet - Equity.docx").write_bytes(b"PK")
    monkeypatch.setattr(cr.ai_config, "extract_text", lambda p: f"TEXT OF {p.name}")
    ctx, on_disk, _ = cb._class_inputs(past, "260909")
    assert "=== CHEAT SHEET (Cheat Sheet - Equity.docx)" in ctx and "TEXT OF Cheat Sheet - Equity.docx" in ctx
    assert on_disk and on_disk[0].name == "260909 Equity.pdf"     # PDFs are listed for the Read tool

    assert cb.looks_like_entry("### Class 4\n**Lenses and frameworks introduced:** x\n**Key takeaways:**\n- y")
    assert not cb.looks_like_entry("I've hit a hard blocker: I can't extract text from the .docx files.")

    ask, calls = _fake_ask(["I can't read these files. How would you like to proceed?"])
    monkeypatch.setattr(cb, "_ask", ask)
    text = cb.TEMPLATE.format(name="X", code="X")
    state = {}
    out = cb.update_class_block(course, past, "260909", text, state)
    assert "class-260909" not in cb.blocks(out)
    assert "classes" not in state                         # retried next run


def test_empty_class_folder_is_skipped_without_a_call(course, monkeypatch):
    empty = course / "260911 Class 3 - Safelite"; empty.mkdir()
    ask, calls = _fake_ask([])
    monkeypatch.setattr(cb, "_ask", ask)
    text = cb.TEMPLATE.format(name="X", code="X")
    out = cb.update_class_block(course, empty, "260911", text, {})
    assert calls == [] and "class-260911" not in cb.blocks(out)


def test_label_does_not_double_the_word_class(course, monkeypatch):
    past = course / "260909 Class 4 - Equity"
    ask, calls = _fake_ask(["**Lenses and frameworks introduced:** a\n**Key takeaways:**\n- b\n"])
    monkeypatch.setattr(cb, "_ask", ask)
    out = cb.update_class_block(course, past, "260909", cb.TEMPLATE.format(name="X", code="X"), {})
    assert "### Class 4 - Equity\n" in out and "Class Class" not in out


def test_tidy_entry_strips_preamble_and_duplicate_heading():
    raw = ("I'll just count the words manually — looks within budget. Here's the final entry:\n\n"
           "### Class 5 - Pave\n**Lenses and frameworks introduced:** a\n### Class 5 - Pave\n**Key takeaways:**\n- b")
    out = cb.tidy_entry(raw, "5 - Pave")
    assert out.startswith("### Class 5 - Pave\n**Lenses")
    assert out.count("### Class 5 - Pave") == 1 and "Here's the final" not in out
    assert cb.tidy_entry("**Key takeaways:**\n- x", "5 - Pave").startswith("### Class 5 - Pave\n")
