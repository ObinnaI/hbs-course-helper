"""Files linked from a Canvas posting are fetched even when the course file
listing does not include them (the usual case for students)."""
from datetime import datetime, timedelta

import canvas_refresh as cr

VER1 = "f7792e94-a7a6-47e2-a633-e84fb2be9a12"
VER2 = "67b01085-a5e3-4be5-9ae1-d8bcc490c155"
DESC = (
    '<p>Read the cases '
    f'<a href="https://hbs.instructure.com/courses/1/files/1247078?verifier={VER1}&amp;wrap=1">MFS</a> and '
    f'<a href="https://hbs.instructure.com/courses/1/files/1247079?verifier={VER2}&amp;wrap=1">HMC</a>, '
    f'<a href="https://hbs.instructure.com/courses/1/files/1247078?verifier={VER1}&amp;wrap=1">again</a> '
    '<a href="https://hbs.instructure.com/files/555">no verifier</a> '
    '<a href="https://hbs.instructure.com/courses/1/quizzes/17142">a quiz</a></p>'
)


def _file(fid, name):
    return {"id": fid, "display_name": name, "url": f"https://hbs.instructure.com/files/{fid}/download"}


def test_regex_finds_ids_and_verifiers():
    found = [(int(m.group(1)), m.group(2)) for m in cr._FILE_LINK_RE.finditer(DESC)]
    assert found == [(1247078, VER1), (1247079, VER2), (1247078, VER1), (555, None)]


def test_linked_files_fetches_unlisted_by_id(monkeypatch, capsys):
    calls = []
    def fake_get(path, params=None):
        calls.append((path, params))
        fid = int(path.rsplit("/", 1)[1])
        if fid == 555:
            return []                       # 403 → canvas_get gives []
        return _file(fid, f"File {fid}.pdf")
    monkeypatch.setattr(cr, "canvas_get", fake_get)

    files = cr.linked_files(DESC, {1247079: _file(1247079, "Listed.pdf")})

    assert [f["display_name"] for f in files] == ["File 1247078.pdf", "Listed.pdf"]
    assert calls == [("files/1247078", {"verifier": VER1}), ("files/555", None)]
    assert "✗ [linked] file 555: not accessible" in capsys.readouterr().out


def test_sync_course_files_downloads_linked_cases(patch_courses, monkeypatch, capsys):
    due = (datetime.now(tz=cr.BOSTON) + timedelta(days=1)).replace(hour=13, minute=30)
    posting = {"id": 9, "name": "GTD | Class 7: MFS/HMC", "submission_types": ["none"],
               "due_at": due.astimezone(cr.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "description": DESC}

    def fake_get(path, params=None):
        if path.endswith("/assignments"):
            return [posting]
        if path.startswith("files/"):
            fid = int(path.split("/")[1])
            return [] if fid == 555 else _file(fid, {1247078: "Massachusetts Financial Services.pdf",
                                                      1247079: "Incentive Pay at HMC.pdf"}[fid])
        return []                            # folders, course files: empty for students
    downloads = []
    def fake_download(url, dest):
        downloads.append((url, dest)); dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF"); return True
    monkeypatch.setattr(cr, "canvas_get", fake_get)
    monkeypatch.setattr(cr, "canvas_download", fake_download)

    cr.sync_course_files(1, "LTV", target_date_str=cr.yymmdd(due))

    names = sorted(d.name for _, d in downloads)
    assert names == ["Incentive Pay at HMC.pdf", "Massachusetts Financial Services.pdf"]
    session_dir = downloads[0][1].parent
    assert session_dir.parent == patch_courses["courses"]["LTV"]["folder_path"]
    assert session_dir.name.startswith(cr.yymmdd(due))
    out = capsys.readouterr().out
    assert "↓ [linked] Massachusetts Financial Services.pdf" in out
    assert "file 555: not accessible" in out


def test_announcement_body_links_are_downloaded(patch_courses, monkeypatch, tmp_path):
    posted = datetime.now(tz=cr.BOSTON) - timedelta(days=1)
    ann = {"id": 77, "title": "Optional reading", "posted_at": posted.isoformat(),
           "message": f'<p>See <a href="https://hbs.instructure.com/courses/1/files/42?verifier={VER1}">this</a></p>',
           "attachments": []}
    def fake_get(path, params=None):
        if "discussion_topics" in path:
            return [ann]
        if path == "files/42":
            assert params == {"verifier": VER1}
            return _file(42, "Bernstein Report.pdf")
        return []
    downloads = []
    monkeypatch.setattr(cr, "canvas_get", fake_get)
    monkeypatch.setattr(cr, "canvas_download", lambda url, dest: downloads.append(dest) or True)

    course_folder = patch_courses["courses"]["LTV"]["folder_path"]
    n = cr.sync_announcements(1, "LTV", course_folder, {})

    assert n == 2                                    # the .md plus the file
    assert [d.name for d in downloads] == ["Bernstein Report.pdf"]
