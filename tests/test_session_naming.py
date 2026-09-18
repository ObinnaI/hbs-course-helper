import json

import canvas_common as cc


def _a(name):
    return {"name": name}


def test_session_dirname_class_and_title():
    assert cc.session_dirname("260916", "MP", [_a("MP | Class 5: Pave (A)")]) \
        == "260916 Class 5 - Pave (A)"
    assert cc.session_dirname("260910", "NEG", [_a("Class 3 - Treu Pharma")]) \
        == "260910 Class 3 - Treu Pharma"


def test_session_dirname_sanitises_and_truncates():
    assert cc.session_dirname("260916", "MP", [_a("Class 3: Treu Pharma/ Negotiating: the JV")]) \
        == "260916 Class 3 - Treu Pharma- Negotiating- the JV"
    long = "Class 7: " + " ".join(["word"] * 30)
    name = cc.session_dirname("260916", "MP", [_a(long)])
    assert name.startswith("260916 Class 7 - word word")
    assert len(name) <= len("260916 Class 7 - ") + cc.MAX_LABEL
    assert not name.endswith(" ")


def test_session_dirname_degrades_gracefully():
    assert cc.session_dirname("260916", "MP", [_a("MP | Class 5")]) == "260916 Class 5"
    assert cc.session_dirname("260916", "MP", [_a("Guest speaker")]) == "260916 Guest speaker"
    assert cc.session_dirname("260916", "MP", []) == "260916 MP"
    assert cc.session_dirname("260916", "MP", [_a("")]) == "260916 MP"


def test_two_postings_same_day():
    posts = [_a("MP | Class 5: Pave (A)"), _a("MP | Class 6: Pave (B)")]
    assert cc.session_dirname("260916", "MP", posts) == "260916 Class 5 & 6 - Pave (A)"
    same = [_a("MP | Class 5: Pave (A)"), _a("MP | Class 5: Pave (A) — exhibits")]
    assert cc.session_dirname("260916", "MP", same) == "260916 Class 5 - Pave (A)"


def test_session_date_and_is_session_dir(tmp_path):
    assert cc.session_date("260916 Class 5 - Pave") == "260916"
    assert cc.session_date("260916") == "260916"
    assert cc.session_date("2609160 x") is None
    assert cc.session_date("Class 5 - Pave") is None
    d = tmp_path / "260916 Class 5 - Pave"; d.mkdir()
    assert cc.is_session_dir(d)
    assert not cc.is_session_dir(tmp_path / "Quiz 1")


def test_find_session_dir_prefers_generated_folder(tmp_path, capsys):
    assert cc.find_session_dir(tmp_path, "260916") is None
    legacy = tmp_path / "260916 MP"; legacy.mkdir()
    assert cc.find_session_dir(tmp_path, "260916") == legacy

    adopted = tmp_path / "260916 Class 5 - Pave"; adopted.mkdir()
    (adopted / ".notes_meta.json").write_text("{}")
    assert cc.find_session_dir(tmp_path, "260916") == adopted
    assert "two folders for 260916" in capsys.readouterr().out

    # Without meta, the fuller folder wins.
    (adopted / ".notes_meta.json").unlink()
    (legacy / "a.pdf").write_bytes(b"x"); (legacy / "b.pdf").write_bytes(b"x")
    assert cc.find_session_dir(tmp_path, "260916") == legacy


def test_session_dir_for_never_rederives_an_existing_label(tmp_path):
    existing = tmp_path / "260916 Class 5 - Pave"; existing.mkdir()
    renamed = [_a("MP | Class 5: Pave (B) — updated")]
    assert cc.session_dir_for(tmp_path, "260916", "MP", renamed) == existing
    assert cc.session_dir_for(tmp_path, "260923", "MP", renamed) \
        == tmp_path / "260923 Class 5 - Pave (B) — updated"


def test_is_notes_file():
    assert cc.is_notes_file("Cheat Sheet - Pave (A).docx")
    assert cc.is_notes_file("260916 MP Notes.docx")
    assert cc.is_notes_file("260916 MP Notes.md")
    assert cc.is_notes_file("RH Class 3 Cheat Sheet - The Research Process.docx")
    assert not cc.is_notes_file("Pave DRAFT.docx")
    assert not cc.is_notes_file("Notes on Pave.pdf")
    assert not cc.is_notes_file("~$Cheat Sheet - Pave.docx")
    assert not cc.is_notes_file("Negotiation_Quiz1_Study_Sheet.docx")


def test_notes_paths_discovery_order(tmp_path):
    d = tmp_path / "260916 Class 5 - Pave"; d.mkdir()
    np = cc.notes_paths(d, "260916", "MP", "Pave (A)")
    assert np.docx == d / "Cheat Sheet - Pave (A).docx"
    assert np.md == d / "Cheat Sheet - Pave (A).md"
    assert np.existing is None

    hand = d / "Cheat Sheet - Six Challenges.docx"; hand.write_bytes(b"PK")
    assert cc.notes_paths(d, "260916", "MP", "Pave (A)").existing == hand

    (d / ".notes_meta.json").write_text(json.dumps({"notes_file": "My notes.docx"}))
    (d / "My notes.docx").write_bytes(b"PK")
    assert cc.notes_paths(d, "260916", "MP", "Pave (A)").existing == d / "My notes.docx"

    np.docx.write_bytes(b"PK")
    assert cc.notes_paths(d, "260916", "MP", "Pave (A)").existing == np.docx

    assert cc.notes_paths(d, "260916", "MP", "").docx == d / "260916 MP Notes.docx"


def test_is_protected_dir():
    for name in ("Quiz 1", "Course Docs", "Course Textbook and Materials",
                 "Course Materials", "General", "Overview", "RH", "claude"):
        assert cc.is_protected_dir(name), name
    assert not cc.is_protected_dir("Class 5 - Pave")
    assert not cc.is_protected_dir("260916 Class 5 - Pave")


def test_session_number_word():
    assert cc.class_number("NEG | Session 3 | Treu Pharma") == 3
    assert cc.session_dirname("260910", "NEG", [_a("NEG | Session 3 | Treu Pharma")]) \
        == "260910 Class 3 - Treu Pharma"


def test_extract_case_title_variants():
    assert cc.extract_case_title("CFO | Class 3: The DCF Method") == "The DCF Method"
    assert cc.extract_case_title("MP | Class 5 – Pave (A)") == "Pave (A)"
    assert cc.extract_case_title("CATS | Class 1 | Capitalism") == "Capitalism"
    assert cc.extract_case_title("Plain title") == "Plain title"


def test_date_titled_postings_do_not_repeat_the_date():
    assert cc.extract_case_title("Wed. Sept 16 - Treu Pharma I") == "Treu Pharma I"
    assert cc.extract_case_title("Thur. Sept 17 - Treu Pharma II + QUIZ 1") == "Treu Pharma II + QUIZ 1"
    assert cc.extract_case_title("Thu. Sept 3rd - Course Introduction") == "Course Introduction"
    assert cc.extract_case_title("Oct 1 - Viking Investment Negotiation") == "Viking Investment Negotiation"
    assert cc.session_dirname("260916", "NEG", [_a("Wed. Sept 16 - Treu Pharma I")]) \
        == "260916 Treu Pharma I"
    # A real title that merely starts with a month-like word is untouched.
    assert cc.extract_case_title("March of the Penguins") == "March of the Penguins"
    assert cc.extract_case_title("MP | Class 5: Pave (A)") == "Pave (A)"
