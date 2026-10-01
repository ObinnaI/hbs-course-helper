import json
from datetime import datetime, timedelta, timezone

import pytest

import canvas_refresh as cr
import inbox
import path_config

SENT = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)


def _post(aid, name, day, hour=14):
    return {"id": aid, "name": name, "submission_types": ["not_graded"], "description": "<p>x</p>",
            "due_at": f"2026-{day}T{hour:02d}:10:00Z"}


NEG_POSTS = [
    _post(1, "Wed. Sept 30 - Honoring the Contract ", "09-30"),
    _post(2, "Thur. Oct 1 - Viking Investment Negotiation ", "10-01"),
    _post(3, "Fri. Oct 2 - Viking Investment Debrief + QUIZ 3", "10-02"),
]
MP_POSTS = [
    {"id": 9, "name": "GTD | Class 9: Chung and Dasgupta", "submission_types": ["none"],
     "description": "<p>Read the case</p>", "due_at": "2026-09-30T17:30:00Z"},
    {"id": 10, "name": "GTD | Class 10: Kaleo Legal", "submission_types": ["none"],
     "description": "<p>Read</p>", "due_at": "2026-10-01T17:30:00Z"},
]


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "coursework"
    neg = root / "Fall" / "Negotiations"; mp = root / "Fall" / "Motivating People"
    neg.mkdir(parents=True); mp.mkdir(parents=True)
    courses = {
        "NEG": {"canvas_id": 17057, "full_name": "Negotiation", "folder_path": neg, "course_code": "NEG-04"},
        "MP": {"canvas_id": 17077, "full_name": "Motivating People to Get Things Done", "folder_path": mp,
               "course_code": "MPGTD-00"},
    }
    cfg = root / "claude" / "canvas_config.json"; cfg.parent.mkdir()
    cfg.write_text("{}")
    monkeypatch.setattr(path_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(path_config, "resolve", lambda *a, **k: {"coursework_root": root, "courses": courses})
    monkeypatch.setattr(cr, "_OVERRIDES", {})
    monkeypatch.setattr(cr, "canvas_get",
                        lambda path, params=None: NEG_POSTS if "17057" in path else MP_POSTS)
    return root, courses


def _item(root, name, subject, body="", files=(), links=()):
    d = root / "inbox" / name; d.mkdir(parents=True)
    for fname, data in files:
        (d / fname).write_bytes(data)
    (d / "message.json").write_text(json.dumps(
        {"id": name, "subject": subject, "from": "tcolsonleaning@hbs.edu", "date": SENT.isoformat(),
         "body": body, "links": list(links)}))
    return d


def test_course_from_subject_code_and_name(world):
    _, courses = world
    assert inbox.course_for("NEG-04: CONFIDENTIAL ROLE INFORMATION: Viking Investments", "", courses, {}) == "NEG"
    assert inbox.course_for("MPGTD-00 CONFIDENTIAL ROLE INFORMATION: Jordan Ramirez", "", courses, {}) == "MP"
    assert inbox.course_for("Role info", "for your Negotiation class", courses, {}) == "NEG"
    assert inbox.course_for("Lunch on Friday?", "see you", courses, {}) is None
    assert inbox.course_for("XYZ-1: role", "", courses, {"email_codes": {"MP": ["XYZ"]}}) == "MP"


def test_subject_title():
    assert inbox.subject_title("NEG-04: CONFIDENTIAL ROLE INFORMATION: Viking Investments") == "Viking Investments"
    assert inbox.subject_title("MPGTD-00 CONFIDENTIAL ROLE INFORMATION: Jordan Ramirez") == "Jordan Ramirez"


def test_attachment_goes_to_the_negotiation_day_not_the_debrief(world, capsys):
    root, courses = world
    _item(root, "260928-a1", "NEG-04: CONFIDENTIAL ROLE INFORMATION: Viking Investments",
          body="You have been assigned the role of Sandy Wood.",
          files=[("Viking_SandyWoodEDIT.pdf", b"%PDF role"), ("image001.jpg", b"sig")])
    assert inbox.route_inbox() == 1
    day = courses["NEG"]["folder_path"] / "261001 Viking Investment Negotiation"
    assert (day / "Viking_SandyWoodEDIT.pdf").read_bytes() == b"%PDF role"
    assert not (day / "image001.jpg").exists()                      # signature images are dropped
    note = next(day.glob("260928 Email - *.md")).read_text()
    assert "Sandy Wood" in note
    assert not (root / "inbox" / "260928-a1").exists()
    state = json.loads(inbox.state_file().read_text())
    assert "261001 Viking Investment Negotiation" in state["260928-a1"]["result"]
    assert "filed →" in capsys.readouterr().out
    assert inbox.route_inbox() == 0                                  # nothing left, nothing duplicated


def test_role_named_in_subject_is_matched_through_the_body(world):
    root, courses = world
    _item(root, "260925-b2", "MPGTD-00 CONFIDENTIAL ROLE INFORMATION: Jordan Ramirez",
          body="In preparation for your Chung and Dasgupta exercise next Wednesday, read your role material.",
          files=[("Chung and Dasgupta - Information for Jordan Ramirez.pdf", b"%PDF")])
    assert inbox.route_inbox() == 1
    assert (courses["MP"]["folder_path"] / "260930 Class 9 - Chung and Dasgupta"
            / "Chung and Dasgupta - Information for Jordan Ramirez.pdf").exists()


def test_hbsp_link_is_downloaded(world, monkeypatch):
    root, courses = world
    calls = []
    import canvas_readings
    async def fake_run(links, session_dir, date_str):
        calls.append((links, session_dir.name, date_str)); return 1
    monkeypatch.setattr(canvas_readings, "_run", fake_run)
    _item(root, "260928-c3", "NEG-04: CONFIDENTIAL ROLE INFORMATION: Honoring the Contract",
          body="Quantron Role Link: https://hbsp.harvard.edu/tu/3e8c67c8 and https://example.com/x",
          links=["https://hbsp.harvard.edu/tu/3e8c67c8", "https://example.com/x"])
    assert inbox.route_inbox() == 1
    links, folder, ds = calls[0]
    assert [l["href"] for l in links] == ["https://hbsp.harvard.edu/tu/3e8c67c8"]
    assert folder == "260930 Honoring the Contract" and ds == "260930"


def test_unmatched_stays_and_dry_run_moves_nothing(world, capsys):
    root, _ = world
    _item(root, "260928-d4", "NEG-04: CONFIDENTIAL ROLE INFORMATION: Zebra Widgets", files=[("z.pdf", b"%PDF")])
    _item(root, "260928-e5", "Lunch?", files=[("menu.pdf", b"%PDF")])
    _item(root, "260928-f6", "NEG-04: CONFIDENTIAL ROLE INFORMATION: Viking Investments", files=[("v.pdf", b"%PDF")])
    assert inbox.route_inbox(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "would file → NEG 261001" in out and "no class in the next" in out and "no course recognised" in out
    assert (root / "inbox" / "260928-f6" / "v.pdf").exists()
    assert inbox.route_inbox() == 1
    assert (root / "inbox" / "260928-d4").exists() and (root / "inbox" / "260928-e5").exists()


def test_no_inbox_is_quiet(world, capsys):
    assert inbox.route_inbox() == 0
    assert capsys.readouterr().out == ""
