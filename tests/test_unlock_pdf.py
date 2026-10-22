"""HBS PDFs arrive owner-protected with an empty user password; the pipeline strips that."""
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

import canvas_common as cc
import canvas_refresh as cr


def _pdf(path: Path, user_pw=None, pages=2):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    if user_pw is not None:
        w.encrypt(user_password=user_pw, owner_password="hbs-owner", algorithm="RC4-128")
    with open(path, "wb") as fh:
        w.write(fh)
    return path


def test_empty_password_protection_is_removed_in_place(tmp_path, capsys):
    f = _pdf(tmp_path / "Case.pdf", user_pw="")
    assert PdfReader(str(f)).is_encrypted
    assert cc.unlock_pdf(f) == "unlocked"
    r = PdfReader(str(f))
    assert not r.is_encrypted and len(r.pages) == 2
    assert "unlocked" in capsys.readouterr().out
    assert cc.unlock_pdf(f) is None                      # second pass: nothing to do
    assert not list(tmp_path.glob("*.unlocking"))


def test_real_password_is_left_alone_and_reported(tmp_path, capsys):
    f = _pdf(tmp_path / "Secret.pdf", user_pw="secret")
    before = f.read_bytes()
    assert cc.unlock_pdf(f) == "locked"
    assert f.read_bytes() == before and cc.pdf_locked(f)
    assert "password-protected" in capsys.readouterr().out
    assert cc.unlock_pdf(tmp_path / "notes.docx") is None
    assert not cc.pdf_locked(_pdf(tmp_path / "Open.pdf"))


def test_unlock_readings_sweeps_a_folder(tmp_path):
    _pdf(tmp_path / "A.pdf", user_pw=""); _pdf(tmp_path / "B.pdf"); _pdf(tmp_path / "C.pdf", user_pw="x")
    (tmp_path / "D.txt").write_text("not a pdf")
    assert cc.unlock_readings(tmp_path) == {"A.pdf": "unlocked", "C.pdf": "locked"}
    assert cc.unlock_readings(tmp_path / "missing") == {}


def test_locked_pdf_does_not_count_as_a_reading(tmp_path):
    locked = _pdf(tmp_path / "Case.pdf", user_pw="x")
    assert cr.substantive_readings([locked]) == []
    assert cr.substantive_readings([_pdf(tmp_path / "Open.pdf")]) != []


def test_download_unlocks_what_it_saves(tmp_path, monkeypatch):
    src = _pdf(tmp_path / "src.pdf", user_pw="")
    class Resp:
        def read(self): return src.read_bytes()
    monkeypatch.setattr(cr, "require", lambda k: "tok")
    monkeypatch.setattr(cr._opener, "open", lambda req, timeout=60: Resp())
    dest = tmp_path / "dl" / "Case.pdf"
    assert cr.canvas_download("https://canvas/files/1", dest)
    assert not PdfReader(str(dest)).is_encrypted
