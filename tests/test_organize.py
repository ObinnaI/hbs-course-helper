import sys
from pathlib import Path

import pytest

import canvas_organize as co
import path_config


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def trash(monkeypatch, tmp_path):
    """Never touch the real ~/.Trash from a test."""
    d = tmp_path / "trash"
    monkeypatch.setenv("COURSEWORK_TRASH", str(d))
    return d


# ── dedup_to_trash ────────────────────────────────────────────────────────────

def test_same_name_different_content_both_survive(patch_courses, trash):
    ltv = patch_courses["courses"]["LTV"]["folder_path"]
    a = _write(ltv / "260902 LTV" / "Exhibit.xlsx", b"aaa")
    b = _write(ltv / "260908 LTV" / "Exhibit.xlsx", b"bbb")

    assert co.dedup_to_trash(verbose=False) == 0
    assert a.exists() and b.exists()
    assert not trash.exists() or not any(trash.iterdir())


def test_identical_lower_priority_copy_trashed_once(patch_courses, trash):
    ltv = patch_courses["courses"]["LTV"]["folder_path"]
    session = _write(ltv / "260902 LTV" / "Case.pdf", b"%PDF-same")
    general = _write(ltv / "General" / "Case.pdf", b"%PDF-same")

    assert co.dedup_to_trash(verbose=False) == 1
    assert session.exists()
    assert not general.exists()
    assert (trash / "Case.pdf").read_bytes() == b"%PDF-same"

    # Idempotent: nothing left to do.
    assert co.dedup_to_trash(verbose=False) == 0


def test_earliest_session_copy_is_kept(patch_courses, trash):
    ltv = patch_courses["courses"]["LTV"]["folder_path"]
    early = _write(ltv / "260902 LTV" / "Note.pdf", b"x")
    late  = _write(ltv / "260915 LTV" / "Note.pdf", b"x")

    assert co.dedup_to_trash(verbose=False) == 1
    assert early.exists() and not late.exists()


def test_slides_copy_wins_over_session_copy(patch_courses, trash):
    ltv = patch_courses["courses"]["LTV"]["folder_path"]
    slides  = _write(ltv / "General" / "Slides" / "deck.pptx", b"PK")
    session = _write(ltv / "260902 LTV" / "deck.pptx", b"PK")

    assert co.dedup_to_trash(verbose=False) == 1
    assert slides.exists() and not session.exists()


def test_cross_course_same_name_untouched(patch_courses, trash):
    root = patch_courses["coursework_root"]
    cfo = root / "CFO"
    patch_courses["courses"]["CFO"] = {
        "canvas_id": 2, "full_name": "CFO", "folder_name": "CFO",
        "folder_path": cfo, "refinement_prompt": None,
    }
    a = _write(patch_courses["courses"]["LTV"]["folder_path"] / "General" / "Citation Guide.pdf", b"same")
    b = _write(cfo / "General" / "Citation Guide.pdf", b"same")

    assert co.dedup_to_trash(verbose=False) == 0
    assert a.exists() and b.exists()


def test_dotfiles_and_trash_dir_are_ignored(patch_courses, trash, monkeypatch):
    ltv = patch_courses["courses"]["LTV"]["folder_path"]
    root = patch_courses["coursework_root"]
    monkeypatch.setenv("COURSEWORK_TRASH", str(root / ".trash"))
    keep = _write(ltv / "260902 LTV" / "Case.pdf", b"same")
    _write(ltv / "General" / "Case.pdf", b"same")
    _write(ltv / "260902 LTV" / ".notes_meta.json", b"{}")
    _write(ltv / "260908 LTV" / ".notes_meta.json", b"{}")

    assert co.dedup_to_trash(verbose=False) == 1
    assert (root / ".trash" / "Case.pdf").exists()
    # A second pass must not re-trash the copy now sitting in .trash/.
    assert co.dedup_to_trash(verbose=False) == 0
    assert keep.exists()


def test_dedup_ignores_protected_subfolders(patch_courses, trash):
    ltv = patch_courses["courses"]["LTV"]["folder_path"]
    keep = _write(ltv / "260909 Class 4 - Equity" / "Six Challenges.pdf", b"same")
    also = _write(ltv / "Course Textbook and Materials" / "Six Challenges.pdf", b"same")
    quiz = _write(ltv / "Quiz 1" / "Six Challenges.pdf", b"same")
    assert co.dedup_to_trash(verbose=False) == 0
    assert keep.exists() and also.exists() and quiz.exists()


def test_refuses_home_root(patch_courses, trash, monkeypatch):
    unsafe = dict(patch_courses)
    unsafe["coursework_root"] = Path.home()
    monkeypatch.setattr(path_config, "resolve", lambda *a, **k: unsafe)
    assert co.dedup_to_trash(verbose=False) == 0


def test_trash_dir_linux_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("COURSEWORK_TRASH", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert co._trash_dir(tmp_path) == tmp_path / ".trash"
    assert (tmp_path / ".trash").is_dir()


def test_trash_collision_gets_suffix(tmp_path, trash):
    a = _write(tmp_path / "a" / "x.pdf", b"1")
    b = _write(tmp_path / "b" / "x.pdf", b"2")
    co._trash(a, co._trash_dir(tmp_path))
    co._trash(b, co._trash_dir(tmp_path))
    assert (trash / "x.pdf").read_bytes() == b"1"
    assert (trash / "x_1.pdf").read_bytes() == b"2"


# ── organize_course ───────────────────────────────────────────────────────────

def test_organize_course_keeps_mismatched_content(tmp_path):
    course = tmp_path / "LTV"
    deck_general = _write(course / "General" / "deck.pptx", b"x")
    deck_slides  = _write(course / "General" / "Slides" / "deck.pptx", b"yy")
    pdf_general  = _write(course / "General" / "x.pdf", b"1")
    pdf_supp     = _write(course / "General" / "Supplemental" / "x.pdf", b"22")

    counts = co.organize_course(course, "LTV")

    assert deck_general.exists() and deck_slides.exists()
    assert pdf_general.exists() and pdf_supp.exists()
    assert counts["warned"] == 2
    assert counts["slides"] == 0 and counts["supplemental"] == 0


def test_organize_course_removes_identical_copies(tmp_path):
    course = tmp_path / "LTV"
    session = _write(course / "260902 LTV" / "Case.pdf", b"same")
    general = _write(course / "General" / "Case.pdf", b"same")
    deck_session = _write(course / "260902 LTV" / "deck.pptx", b"PK")
    deck_slides  = _write(course / "General" / "Slides" / "deck.pptx", b"PK")

    counts = co.organize_course(course, "LTV")

    assert session.exists() and not general.exists()
    assert deck_slides.exists() and not deck_session.exists()
    assert counts["removed"] == 1 and counts["slides"] == 1


def test_organize_course_moves_when_no_conflict(tmp_path):
    course = tmp_path / "LTV"
    deck = _write(course / "General" / "deck.pptx", b"PK")
    supp = _write(course / "General" / "reading.pdf", b"%PDF")
    syll = _write(course / "General" / "Syllabus.pdf", b"%PDF")

    counts = co.organize_course(course, "LTV")

    assert (course / "General" / "Slides" / "deck.pptx").exists() and not deck.exists()
    assert (course / "General" / "Supplemental" / "reading.pdf").exists() and not supp.exists()
    assert syll.exists()
    assert counts == {"removed": 0, "slides": 1, "supplemental": 1, "kept": 1, "warned": 0}
