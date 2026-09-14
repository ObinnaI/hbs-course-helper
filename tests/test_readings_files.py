import zipfile

import canvas_readings as rd


def _zip_with(path, *names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, "<x/>")
    return path


def test_fix_extension_sniffs_office_zips(tmp_path):
    xlsx = _zip_with(tmp_path / "exhibit.pdf", "[Content_Types].xml", "xl/workbook.xml")
    docx = _zip_with(tmp_path / "memo.pdf", "[Content_Types].xml", "word/document.xml")
    pptx = _zip_with(tmp_path / "deck.pdf", "[Content_Types].xml", "ppt/presentation.xml")

    assert rd._fix_extension(xlsx).name == "exhibit.xlsx"
    assert rd._fix_extension(docx).name == "memo.docx"
    assert rd._fix_extension(pptx).name == "deck.pptx"
    assert not xlsx.exists() and not docx.exists() and not pptx.exists()


def test_fix_extension_leaves_real_pdf_and_unknown_zip(tmp_path):
    pdf = tmp_path / "case.pdf"
    pdf.write_bytes(b"%PDF-1.4 ...")
    assert rd._fix_extension(pdf) == pdf and pdf.exists()

    other = _zip_with(tmp_path / "bundle.pdf", "readme.txt")
    assert rd._fix_extension(other) == other and other.exists()

    already = _zip_with(tmp_path / "ok.xlsx", "xl/workbook.xml")
    assert rd._fix_extension(already) == already


def test_fix_extension_pdf_named_docx(tmp_path):
    p = tmp_path / "case.docx"
    p.write_bytes(b"%PDF-1.7")
    assert rd._fix_extension(p).name == "case.pdf"


def test_fix_extension_never_clobbers(tmp_path):
    existing = tmp_path / "exhibit.xlsx"
    existing.write_bytes(b"keep me")
    mis = _zip_with(tmp_path / "exhibit.pdf", "xl/workbook.xml")
    assert rd._fix_extension(mis) == mis
    assert existing.read_bytes() == b"keep me"


def test_looks_like_login():
    long = "word " * 400
    assert rd._looks_like_login("Log in | WSJ", "https://wsj.com/x", long)
    assert rd._looks_like_login("Sign In - NYT", "https://nytimes.com/x", long)
    assert rd._looks_like_login("Article", "https://site.com/login?next=/x", long)
    assert rd._looks_like_login("Just a moment...", "https://site.com/x", long)
    assert rd._looks_like_login("The idea maze", "https://cdixon.org/x", "too short")
    assert rd._looks_like_login("The idea maze", "https://cdixon.org/x", long) is None
    # "log in" in body text is normal nav chrome, not a wall.
    assert rd._looks_like_login("The idea maze", "https://cdixon.org/x",
                                long + " log in subscribe") is None
