import json
from datetime import date

import pytest

import canvas_common as cc
import canvas_refresh as cr
import migrate_folders as mf
import path_config


POSTS = {
    "MP": [
        {"id": 41, "name": "MP | Class 4: Equity-Based Pay", "due_at": "2026-09-09T12:00:00Z",
         "submission_types": ["not_graded"], "description": "<p>q4</p>"},
        {"id": 51, "name": "MP | Class 5: Pave (A)", "due_at": "2026-09-16T12:00:00Z",
         "submission_types": ["not_graded"], "description": "<p>q5</p>"},
        {"id": 61, "name": "MP | Class 6: Gerald Weiss (2023)", "due_at": "2026-09-23T12:00:00Z",
         "submission_types": ["not_graded"], "description": "<p>q6</p>"},
        {"id": 99, "name": "Reflection paper", "due_at": "2026-09-30T12:00:00Z",
         "submission_types": ["online_upload"], "description": ""},
    ],
    "NEG": [
        {"id": 71, "name": "NEG | Session 1 | Hamilton", "due_at": "2026-09-03T12:00:00Z",
         "submission_types": ["not_graded"], "description": ""},
        {"id": 72, "name": "NEG | Treu Pharma negotiation", "due_at": "2026-09-10T12:00:00Z",
         "submission_types": ["not_graded"], "description": ""},
    ],
}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "2026"
    mp  = root / "Fall" / "Motivating People"
    neg = root / "Fall" / "Negotiations"
    for d, files in {
        mp / "Class 4 - Equity": ["1. SIX CHALLENGES.pdf", "Cheat Sheet - Six Challenges.docx"],
        mp / "Class 5 - Pave": ["Cheat Sheet - Pave (A).docx", "Pave DRAFT.docx"],
        mp / "Class 6 - Gerald Weiss": ["Case - Gerald Weiss (2023).pdf", "Cheat Sheet - Gerald Weiss (2023).docx"],
        mp / "Course Textbook and Materials": ["Mixed Signals.pdf"],
        neg / "Class 3 - Treu Pharma": ["Treu Pharam Case.pdf"],
        neg / "Quiz 1": ["study.docx"],
        neg / "Hamilton prep": ["hamilton.pdf"],
    }.items():
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_bytes(b"%PDF" if f.endswith(".pdf") else b"PK")
    (root / "claude").mkdir()
    monkeypatch.setattr(path_config, "CONFIG_FILE", root / "claude" / "canvas_config.json")
    ids = {"MP": 1, "NEG": 2}
    monkeypatch.setattr(cr, "canvas_get",
                        lambda path, params=None: POSTS["MP" if path.startswith("courses/1/") else "NEG"])
    courses = {
        "MP":  {"canvas_id": 1, "full_name": "Motivating People", "folder_path": mp, "term": "Fall"},
        "NEG": {"canvas_id": 2, "full_name": "Negotiations", "folder_path": neg, "term": "Fall"},
    }
    monkeypatch.setattr(path_config, "resolve",
                        lambda *a, **k: {"coursework_root": root, "courses": courses})
    monkeypatch.setattr(cr, "_COURSES", courses)
    monkeypatch.setattr(cr, "COURSE_NAMES", {"MP": "Motivating People", "NEG": "Negotiations"})
    return root


def _plans(root, **kw):
    paths = path_config.resolve()
    out = []
    for abbrev, info in sorted(paths["courses"].items()):
        postings = mf.postings_for(cr, cc, info["canvas_id"])
        out += mf.plan_course(abbrev, info, root, postings, kw.get("maps", {}),
                              kw.get("accept_fuzzy", False), kw.get("relabel", False), cc)
    return out


def test_dry_run_reports_and_changes_nothing(tree, capsys):
    before = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))
    rc = mf.main(["--root", str(tree)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RENAME    Fall/Motivating People/Class 4 - Equity" in out
    assert "→ 260909 Class 4 - Equity-Based Pay" in out
    assert "→ 260916 Class 5 - Pave (A)" in out
    assert "→ 260923 Class 6 - Gerald Weiss (2023)" in out
    assert "notes: Cheat Sheet - Pave (A).docx" in out
    assert "treated as readings: Pave DRAFT.docx" in out
    assert "KEEP      Fall/Motivating People/Course Textbook and Materials" in out
    assert "KEEP      Fall/Negotiations/Quiz 1" in out
    assert 'UNMATCHED Fall/Negotiations/Class 3 - Treu Pharma' in out and "no \"Class 3\" posting" in out
    assert "FUZZY     Fall/Negotiations/Hamilton prep" in out and "--accept-fuzzy" in out
    assert "Nothing changed (dry run)" in out
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*")) == before


def test_apply_renames_and_adopts_cheat_sheets(tree, monkeypatch):
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 1))
    plans = _plans(tree, maps={"Fall/Negotiations/Class 3 - Treu Pharma": "72"})
    n = mf.apply(plans, tree, cr, cc, path_config)
    assert n == 4

    mp = tree / "Fall" / "Motivating People"
    pave = mp / "260916 Class 5 - Pave (A)"
    assert pave.is_dir() and not (mp / "Class 5 - Pave").exists()
    assert (pave / "Cheat Sheet - Pave (A).docx").exists() and (pave / "Pave DRAFT.docx").exists()
    meta = json.loads((pave / ".notes_meta.json").read_text())
    assert meta["adopted"] and meta["adopted_from"] == "Class 5 - Pave"
    assert meta["notes_file"] == "Cheat Sheet - Pave (A).docx"
    assert set(meta["assignments"]) == {"51"}
    assert "Pave DRAFT.docx" in meta["readings"]

    treu = tree / "Fall" / "Negotiations" / "260910 Treu Pharma negotiation"
    assert treu.is_dir()
    assert json.loads((treu / ".notes_meta.json").read_text())["notes_file"] is None

    # Adopted notes are up to date: nothing regenerates.
    posting = POSTS["MP"][1]
    session = {"abbrev": "MP", "date_str": "260916", "assignments": [posting], "assignment": posting}
    assert cr.notes_are_stale(pave, "MP", "260916", cr.session_hashes(session),
                              title=cc.session_title([posting])) == (False, "up to date")

    log = json.loads((tree / "claude" / "migrations.json").read_text())
    assert {e["from"] for e in log} >= {"Fall/Motivating People/Class 5 - Pave"}

    # Second pass: everything is DONE; nothing renamed.
    plans2 = _plans(tree)
    assert {p.kind for p in plans2 if p.course == "MP"} == {"DONE", "KEEP"}
    assert mf.apply(plans2, tree, cr, cc, path_config) == 0


def test_conflict_is_skipped_not_merged(tree):
    mp = tree / "Fall" / "Motivating People"
    (mp / "260916 Class 5 - Pave (A)").mkdir()
    plans = _plans(tree)
    pave = next(p for p in plans if p.src.name == "Class 5 - Pave")
    assert pave.kind == "CONFLICT"
    assert mf.apply(plans, tree, cr, cc, path_config) == 2
    assert (mp / "Class 5 - Pave").exists()


def test_accept_fuzzy_and_relabel(tree):
    plans = _plans(tree, accept_fuzzy=True)
    ham = next(p for p in plans if p.src.name == "Hamilton prep")
    assert ham.kind == "RENAME" and ham.dst.name == "260903 Class 1 - Hamilton"

    neg = tree / "Fall" / "Negotiations"
    (neg / "260903 old label").mkdir()
    plans = _plans(tree, relabel=True)
    old = next(p for p in plans if p.src.name == "260903 old label")
    assert old.kind == "RELABEL" and old.dst.name == "260903 Class 1 - Hamilton"
    plans = _plans(tree)
    assert next(p for p in plans if p.src.name == "260903 old label").kind == "DONE"
