import json
from datetime import date

import pytest

import canvas_refresh as cr
import quiz


def _post(aid, name, day, hour=14):
    return {"id": aid, "name": name, "submission_types": ["not_graded"], "description": "<p>x</p>",
            "due_at": f"2026-{day}T{hour:02d}:10:00Z"}


POSTS = [
    _post(1, "Thu. Sept 3rd - Course Introduction", "09-03"),
    _post(2, "Thu. Sept 10 - Hamilton Real Estate I", "09-10"),
    _post(3, "Fri. Sept 11 - Hamilton Real Estate II", "09-11"),
    _post(4, "Wed. Sept 16 - Treu Pharma I", "09-16"),
    _post(5, "Thur. Sept 17 - Treu Pharma II + QUIZ 1", "09-17"),
    _post(6, "Fri. Sept 18 - Win As Much As You Can", "09-18"),
    _post(7, "Thur. Sept 24 - Moms.com I", "09-24"),
    _post(8, "Fri. Sept 25 - Moms.com II + QUIZ 2", "09-25"),
    _post(9, "Wed. Sept 30 - Honoring the Contract ", "09-30"),
    _post(10, "Thur. Oct 1 - Viking Investment Negotiation ", "10-01"),
    _post(11, "Fri. Oct 2 - Viking Investment Debrief + QUIZ 3", "10-02"),
    {"id": 99, "name": "Personality Questionnaire", "submission_types": ["online_url"], "description": "",
     "due_at": "2026-10-01T03:59:00Z"},
]
FOLDERS = [{"id": 70, "name": "7_8_Moms.com"}, {"id": 90, "name": "9_Honoring_the_Contract"},
           {"id": 60, "name": "6_Win_As_Much_As_You_Can"}, {"id": 5, "name": "Syllabus"}]
FILES = [{"id": 1, "folder_id": 70, "display_name": "2026_fall_moms_2_04.pdf"},
         {"id": 2, "folder_id": 90, "display_name": "2026_HTC.pdf"},
         {"id": 3, "folder_id": 60, "display_name": "2025_Win As Much As You Can.pdf"},
         {"id": 4, "folder_id": 5, "display_name": "syllabus.docx"}]


def fake_get(path, params=None):
    if path.endswith("/assignments"): return POSTS
    if path.endswith("/folders"): return FOLDERS
    if path.endswith("/files"): return FILES
    return []


@pytest.fixture
def course(patch_courses, monkeypatch):
    monkeypatch.setattr(cr, "canvas_get", fake_get)
    monkeypatch.setattr(cr, "_OVERRIDES", {})
    monkeypatch.setattr(cr, "_NOTES_BLOCKED", None)
    monkeypatch.setattr(cr, "pdf_page_count", lambda p: 12)
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 30))
    folder = patch_courses["courses"]["LTV"]["folder_path"]
    shelf = folder / "Course Materials"; shelf.mkdir()
    for name in ("2026_fall_moms_2_04.pdf", "2025_Win As Much As You Can.pdf"):
        (shelf / name).write_bytes(b"%PDF " + name.encode())
    for d, files in {"260925 Moms.com II + QUIZ 2": {"Betting on the Future.pdf": b"%PDF bet", "Cheat Sheet - Moms.com II.docx": b"PK1",
                                                      "260925 LTV Podcast.m4a": b"audio", ".notes_meta.json": b"{}"},
                     "260930 Honoring the Contract": {"Quantron role.pdf": b"%PDF role", "role copy.pdf": b"%PDF role",
                                                      "Calculator - Honoring the Contract.xlsx": b"PK2"},
                     "261001 Viking Investment Negotiation": {"Viking_SandyWood.pdf": b"%PDF viking"}}.items():
        (folder / d).mkdir()
        for f, data in files.items():
            (folder / d / f).write_bytes(data)
    (folder / "260925 Moms.com II + QUIZ 2" / ".Cheat Sheet - Moms.com II.md").write_text("# Cheat Sheet: Moms\ncontingent contracts")
    return folder


def test_quizzes_and_scope(course):
    days = quiz.class_days(cr, 1)
    assert len(days) == 11 and days[0]["ordinals"] == [1]           # the questionnaire is not a class day
    qs = quiz.find_quizzes(days)
    assert [(q["n"], q["date_str"]) for q in qs] == [(1, "260917"), (2, "260925"), (3, "261002")]
    new, earlier = quiz.scope_for(days, qs, 3)
    assert [d["date_str"] for d in new] == ["260925", "260930", "261001"]   # from the last quiz day to the day before
    assert [d["date_str"] for d in earlier][-1] == "260924"
    first, none_before = quiz.scope_for(days, qs, 1)
    assert [d["date_str"] for d in first] == ["260903", "260910", "260911", "260916"] and none_before == []
    with pytest.raises(KeyError):
        quiz.scope_for(days, qs, 9)


def test_unnumbered_quizzes_are_counted():
    days = [{"date_str": "1", "due": None, "postings": [{"name": "Class 3 + Quiz"}], "ordinals": [1]},
            {"date_str": "2", "due": None, "postings": [{"name": "Class 4"}], "ordinals": [2]},
            {"date_str": "3", "due": None, "postings": [{"name": "Class 5 and quiz"}], "ordinals": [3]}]
    assert [q["n"] for q in quiz.find_quizzes(days)] == [1, 2]


def test_grouping_and_labels(course):
    days = quiz.class_days(cr, 1)
    groups = quiz.group_days(days)
    titles = [g["title"] for g in groups]
    assert titles == ["Course Introduction", "Hamilton Real Estate", "Treu Pharma", "Win As Much As You Can",
                      "Moms.com", "Honoring the Contract", "Viking Investment"]
    ham = groups[1]
    assert ham["ordinals"] == [2, 3] and quiz.group_label(1, ham) == "1 Hamilton Real Estate (Sep 10-11)"
    assert quiz.case_key("Fri. Oct 2 - Viking Investment Debrief + QUIZ 3") == "Viking Investment"


def test_deck_map_reads_class_order_from_folder_names(course):
    decks = quiz.deck_map(cr, 1)
    assert decks["2026_fall_moms_2_04.pdf"] == {7, 8} and decks["2026_HTC.pdf"] == {9}
    assert "syllabus.docx" not in decks


def test_build_makes_the_folder_and_guide(course, monkeypatch, capsys):
    asked = {}
    def fake_ask(cr_, cwd, system, instruction, context):
        asked.update(cwd=cwd, system=system, context=context)
        return "# LTV Quiz 3 Study Guide\n\n## Key terms\n\n| Term | Definition |\n|---|---|\n| BATNA | best alternative |\n"
    monkeypatch.setattr(quiz, "_ask", fake_ask)
    out = quiz.build("LTV", 3)
    q = course / "Quiz 3"
    assert out == q / "Launching Tech Ventures Quiz 3 Study Guide.docx" and out.exists()
    assert (q / ".Launching Tech Ventures Quiz 3 Study Guide.md").read_text().count("BATNA") == 1
    moms = q / "1 Moms.com (Sep 25)"
    assert sorted(p.name for p in moms.iterdir()) == ["2026_fall_moms_2_04.pdf", "Betting on the Future.pdf",
                                                       "Cheat Sheet - Moms.com II.docx"]     # deck in, podcast out
    htc = q / "2 Honoring the Contract (Sep 30)"
    assert [p.name for p in htc.iterdir()] == ["Quantron role.pdf"]       # duplicate and calculator left out
    assert (q / "3 Viking Investment (Oct 1)" / "Viking_SandyWood.pdf").exists()
    meta = json.loads((q / ".quiz_meta.json").read_text())
    assert meta["quiz"] == 3 and meta["date"] == "261002" and meta["fingerprint"]
    ctx = asked["context"]
    assert asked["cwd"] == course and "definitional" in asked["system"]
    assert "Quiz 3, in class on Friday October 2, 2026" in ctx
    assert "Quiz 3/1 Moms.com (Sep 25)/2026_fall_moms_2_04.pdf (12 pages) — DECK, in scope" in ctx
    assert "Course Materials/2025_Win As Much As You Can.pdf (12 pages) — earlier deck" in ctx
    assert "=== CHEAT SHEET: Moms.com II + QUIZ 2 ===" in ctx and "contingent contracts" in ctx
    assert "- Sep 24: Moms.com I" in ctx

    # Nothing changed → no second call.
    monkeypatch.setattr(quiz, "_ask", lambda *a: pytest.fail("must not regenerate"))
    assert quiz.build("LTV", 3) == out and "up to date" in capsys.readouterr().out


def test_a_late_deck_rebuilds_until_the_quiz_then_freezes(course, monkeypatch):
    calls = []
    monkeypatch.setattr(quiz, "_ask", lambda *a: calls.append(1) or "# guide\n")
    quiz.build("LTV", 3)
    (course / "Course Materials" / "2026_HTC.pdf").write_bytes(b"%PDF htc deck")      # posted days later
    quiz.build("LTV", 3)
    assert len(calls) == 2 and (course / "Quiz 3" / "2 Honoring the Contract (Sep 30)" / "2026_HTC.pdf").exists()
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 10, 3))                      # the quiz has happened
    (course / "Course Materials" / "2026_fall_moms_2_04.pdf").write_bytes(b"%PDF revised")
    assert quiz.build("LTV", 3) is None and len(calls) == 2
    quiz.build("LTV", 3, force=True)
    assert len(calls) == 3


def test_hand_made_folder_is_left_alone_and_read_as_earlier(course, monkeypatch, capsys):
    mine = course / "Quiz 2"; mine.mkdir()
    (mine / "Negotiation_Quiz2_Study_Sheet.docx").write_bytes(b"PK")
    (mine / ".Negotiation_Quiz2_Study_Sheet.md").write_text("# Quiz 2 sheet\nPareto efficiency")
    monkeypatch.setattr(quiz, "_ask", lambda *a: pytest.fail("must not write into your folder"))
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 20))
    assert quiz.build("LTV", 2) is None
    assert "you made yourself" in capsys.readouterr().out and not (mine / ".quiz_meta.json").exists()

    seen = {}
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 30))
    monkeypatch.setattr(quiz, "_ask", lambda cr_, cwd, system, instr, ctx: seen.update(ctx=ctx) or "# guide\n")
    quiz.build("LTV", 3)
    assert "=== PREVIOUS STUDY GUIDE: Quiz 2" in seen["ctx"] and "Pareto efficiency" in seen["ctx"]
    assert (course / "Quiz 3" / "Earlier" / "Negotiation_Quiz2_Study_Sheet.docx").exists()


def test_upcoming_and_run_respect_the_week_and_the_cap(course, monkeypatch):
    assert quiz.upcoming(cr) == [("LTV", 3)]                      # Sep 30 → Oct 2 is within the week
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 20))
    assert quiz.upcoming(cr) == [("LTV", 2)]
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 10, 5))
    assert quiz.upcoming(cr) == []
    monkeypatch.setattr(cr, "_today", lambda: date(2026, 9, 30))
    monkeypatch.setattr(quiz, "_ask", lambda *a: "# guide\n")
    assert quiz.run() == 1 and quiz.run() == 0


def test_usage_limit_blocks_cleanly(course, monkeypatch):
    import notes_backend as nb
    def limited(*a):
        raise nb.NotesRateLimited("usage limit")
    monkeypatch.setattr(quiz, "_ask", limited)
    assert quiz.build("LTV", 3) is None
    assert cr._NOTES_BLOCKED and "quiz guide waits" in cr._NOTES_BLOCKED
    assert (course / "Quiz 3" / "1 Moms.com (Sep 25)").exists()       # materials are still gathered


def test_prompt_and_guide_name():
    p = quiz.guide_prompt("NEG")
    assert "## Key terms" in p and "## Practice quiz" in p and "[CLASS-SPECIFIC NOTES]" not in p
    assert quiz.guide_name("Negotiation", 3) == "Negotiation Quiz 3 Study Guide"
    assert quiz.guide_name("Immersive Field Course: China: Geopolitics", 1) == "Immersive Field Course Quiz 1 Study Guide"
