from datetime import datetime, timezone

import canvas_refresh as cr
import ics_feed
import path_config


def _posts():
    return [
        {"id": 10, "name": "Writing Assignment #1", "due_at": "2026-09-20T21:00:00Z",
         "submission_types": ["online_upload"], "description": "<p>Two, pages; max</p>",
         "html_url": "https://c.test/a/10"},
        {"id": 11, "name": "Class 3", "due_at": "2026-09-21T12:00:00Z",
         "submission_types": ["not_graded"], "description": ""},
        {"id": 12, "name": "Odd posting", "due_at": "2026-09-22T12:00:00Z",
         "submission_types": ["discussion_topic"], "description": ""},
        {"id": 13, "name": "Ancient", "due_at": "2020-01-01T12:00:00Z",
         "submission_types": ["online_quiz"], "description": ""},
    ]


COURSES = {"LTV": {"canvas_id": 1, "folder_path": None}}
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _setup(monkeypatch, tmp_path, posts):
    monkeypatch.setattr(path_config, "CONFIG_FILE", tmp_path / "claude" / "canvas_config.json")
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)
    monkeypatch.setenv("TASKS_LLM", "0")


def test_feed_has_deliverables_only(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _posts())
    out = ics_feed.write(COURSES, NOW)
    text = out.read_bytes().decode("utf-8")   # read_text() would fold \r\n

    assert text.count("BEGIN:VEVENT") == 1          # the odd posting is no longer guessed at
    assert "UID:1-10@hbs-course-helper" in text
    assert "SUMMARY:DUE: Writing Assignment #1 (LTV)" in text
    assert "Class 3" not in text and "Ancient" not in text and "Odd posting" not in text
    assert "DTSTART:20260920T210000Z" in text and "DURATION:PT30M" in text
    assert r"DESCRIPTION:Two\, pages\; max" in text
    assert "URL:https://c.test/a/10" in text
    assert "SEQUENCE:0" in text
    assert "\r\n" in text and "\n\n" not in text


def test_unchanged_feed_is_byte_identical_and_moved_date_bumps_sequence(monkeypatch, tmp_path):
    posts = _posts()
    _setup(monkeypatch, tmp_path, posts)
    first = ics_feed.write(COURSES, NOW).read_bytes()
    later = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert ics_feed.write(COURSES, later).read_bytes() == first

    posts[0]["due_at"] = "2026-09-27T21:00:00Z"
    third = ics_feed.write(COURSES, later).read_bytes().decode("utf-8")
    assert third.count("UID:1-10@hbs-course-helper") == 1
    block = third.split("UID:1-10@hbs-course-helper")[1].split("END:VEVENT")[0]
    assert "SEQUENCE:1" in block and "DTSTART:20260927T210000Z" in block
    assert "DTSTAMP:20260915T120000Z" in block


def test_all_day_and_submitted(monkeypatch, tmp_path):
    posts = [
        {"id": 20, "name": "Personality Questionnaire", "due_at": "2026-10-01T03:59:59Z",
         "submission_types": ["online_url"], "description": "", "submission": {"workflow_state": "unsubmitted"}},
        {"id": 21, "name": "Pre-Class Poll: Class 4", "due_at": "2026-09-16T17:00:00Z",
         "submission_types": ["online_quiz"], "description": "", "submission": {"workflow_state": "pending_review"}},
    ]
    _setup(monkeypatch, tmp_path, posts)
    text = ics_feed.write(COURSES, NOW).read_bytes().decode("utf-8")
    q = text.split("UID:1-20@hbs-course-helper")[1].split("END:VEVENT")[0]
    assert "DTSTART;VALUE=DATE:20260930" in q and "DURATION" not in q
    assert "SUMMARY:DUE: Personality Questionnaire (LTV)" in q
    poll = text.split("UID:1-21@hbs-course-helper")[1].split("END:VEVENT")[0]
    assert "SUMMARY:✓ Pre-Class Poll: Class 4 (LTV)" in poll and "DTSTART:20260916T170000Z" in poll

    # Submitting bumps the sequence so a subscribed calendar refreshes the title.
    posts[0]["submission"]["workflow_state"] = "submitted"
    later = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    text2 = ics_feed.write(COURSES, later).read_bytes().decode("utf-8")
    q2 = text2.split("UID:1-20@hbs-course-helper")[1].split("END:VEVENT")[0]
    assert "SEQUENCE:1" in q2 and "SUMMARY:✓ Personality Questionnaire (LTV)" in q2


def test_fold_respects_75_octets():
    line = "DESCRIPTION:" + "é" * 100
    folded = ics_feed._fold(line)
    for i, part in enumerate(folded.split("\r\n")):
        assert len(part.encode("utf-8")) <= 75
        if i:
            assert part.startswith(" ")
    assert folded.replace("\r\n ", "") == line
