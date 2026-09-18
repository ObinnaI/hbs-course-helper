import pytest

pytest.importorskip("notebooklm")

import podcast_gen as pg


def test_cheat_sheet_text_prefers_markdown_twin(tmp_path):
    d = tmp_path / "260917 Class 5 - Pave"; d.mkdir()
    assert pg.cheat_sheet_text(d, "260917", "MP", "Pave") is None

    (d / "Cheat Sheet - Pave (A).docx").write_bytes(b"PK")   # unreadable on its own
    (d / "Cheat Sheet - Pave (A).md").write_text("# Cheat Sheet: Pave\n\nbottom line")
    assert "bottom line" in pg.cheat_sheet_text(d, "260917", "MP", "Pave (A)")
    # Found by discovery even when the title we ask for differs.
    assert "bottom line" in pg.cheat_sheet_text(d, "260917", "MP", "")


def test_cheat_sheet_text_falls_back_to_docx_extraction(tmp_path, monkeypatch):
    d = tmp_path / "260917 Class 5 - Pave"; d.mkdir()
    (d / "Cheat Sheet - Pave (A).docx").write_bytes(b"PK")
    monkeypatch.setattr(pg.ai_config, "extract_text", lambda p: "word text")
    assert pg.cheat_sheet_text(d, "260917", "MP", "Pave (A)") == "word text"


def test_audio_options_default_to_long_deep_dive(monkeypatch):
    from notebooklm.types import AudioFormat, AudioLength
    monkeypatch.delenv("PODCAST_FORMAT", raising=False)
    monkeypatch.delenv("PODCAST_LENGTH", raising=False)
    monkeypatch.setattr(pg._cr, "cfg", lambda k: "")
    assert pg._audio_options() == (AudioFormat.DEEP_DIVE, AudioLength.LONG)
    monkeypatch.setattr(pg._cr, "cfg", lambda k: {"PODCAST_FORMAT": "brief", "PODCAST_LENGTH": "short"}.get(k, ""))
    assert pg._audio_options() == (AudioFormat.BRIEF, AudioLength.SHORT)
    monkeypatch.setattr(pg._cr, "cfg", lambda k: {"PODCAST_FORMAT": "nonsense"}.get(k, ""))
    assert pg._audio_options()[0] == AudioFormat.DEEP_DIVE


def test_instructions_tell_hosts_to_separate_case_from_cheat_sheet(tmp_path):
    text = pg._build_instructions([tmp_path / "case.pdf"], "MP")
    assert "CHEAT SHEET" in text and "NOT the case" in text
    assert "page by page" in text and "anchor numbers" in text
    assert "[CLASS-SPECIFIC NOTES]" not in text
    with_supp = pg._build_instructions([tmp_path / "case.pdf", tmp_path / "note.pdf"], "MP")
    assert "Frameworks" in with_supp
