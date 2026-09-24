import json

import pytest

import canvas_refresh as cr
import deliverables as dv
import path_config

DUE = "2026-09-25T03:59:59Z"


def _p(aid, name, types, pts=0.0, desc="", due=DUE, **extra):
    return {"id": aid, "name": name, "submission_types": types, "points_possible": pts,
            "description": desc, "due_at": due, **extra}


IFC_SESSION = _p(1, "Industrial Policy at Scale: Electric Vehicles", ["online_text_entry"],
                 desc="<p>Introduction How did a country… Materials Case: Tariffs, Bans, and Subsidies (725-026)</p>")
GEO = _p(2, "GEO Assignment #1: Early Semester Action Items", ["not_graded"], None,
         desc="<p>Sign the IFC Course Policies & International Travel Waiver…</p>")
NEG_Q = _p(3, "Personality Questionnaire Assignment due by Sep. 30", ["online_url"])
MP_POLL = _p(4, "Pre-Class Poll: Class 4 (Counts Towards Participation) ", ["online_quiz"], 10.0)
INVS_DUE = _p(5, "DUE: First Meeting with Project Partner and Set Up Two More Formal Meetings",
              ["online_text_entry", "online_upload"])
MIDTERM = _p(6, "Class 7: Midterm Presentation DUE", ["online_upload"])
FINAL = _p(7, "FINAL PROJECT", ["online_text_entry", "online_upload"])
OFFICE = _p(8, "GTD | Office Hours", ["online_text_entry"], due=None)
LTV = _p(9, "LTV | Class 3 | Ginkgo", ["not_graded"], None, desc="<p>Read the case.</p>")
NEG_QUIZ_DAY = _p(10, "Thur. Sept 17 - Treu Pharma II + QUIZ 1", ["not_graded"], None)
PASSPORT = _p(11, "China IFC Passport Poll", ["not_graded"], None)
MEMO = _p(12, "Module Reflection Memo: Module I - Incentive Systems", ["online_text_entry"])
ODD = _p(13, "Odd posting", ["discussion_topic"])
GUEST = _p(14, "Class 4: Management Analyst Meeting", ["none"])
TBD = _p(15, "TBD", ["online_text_entry"])


@pytest.mark.parametrize("posting, expected", [
    (IFC_SESSION, "session"), (GEO, "deliverable"), (NEG_Q, "deliverable"),
    (MP_POLL, "deliverable"), (INVS_DUE, "deliverable"), (MIDTERM, "both"),
    (FINAL, "deliverable"), (OFFICE, "skip"), (LTV, "session"),
    (NEG_QUIZ_DAY, "session"), (PASSPORT, "deliverable"), (MEMO, "deliverable"),
    (GUEST, "session"), (ODD, "ambiguous"), (TBD, "session"),
])
def test_rule_table(posting, expected):
    kind, reason = dv.classify_posting(posting)
    assert kind == expected and reason


def test_override_wins():
    assert dv.classify_posting(IFC_SESSION, {"1": "deliverable"}) == ("deliverable", "override")
    assert dv.classify_posting(MP_POLL, {"4": "skip"})[0] == "skip"
    assert dv.classify_posting(OFFICE, {"8": "deliverable"})[0] == "skip"   # no due date stays skipped


def test_resolver_answers_uncertain_only():
    asked = []
    def resolve(a):
        asked.append(a["id"]); return ("deliverable", "Claude says")
    assert dv.classify_posting(ODD, resolve=resolve) == ("deliverable", "Claude says")
    assert dv.classify_posting(LTV, resolve=resolve)[0] == "session"
    assert asked == [13]


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "claude" / "canvas_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"deliverable_overrides": {"14": "deliverable"}}))
    monkeypatch.setattr(path_config, "CONFIG_FILE", cfg)
    monkeypatch.delenv("TASKS_LLM", raising=False)
    return cfg.parent


def test_classify_many_batches_and_caches(state_dir, monkeypatch):
    calls = []
    def fake_llm(items):
        calls.append([i["id"] for i in items])
        return {"13": ("session", "looks like a class")}
    monkeypatch.setattr(dv, "_llm_resolve", fake_llm)

    out = dv.classify_many([ODD, LTV, GUEST, IFC_SESSION], course="X")
    assert out["13"] == ("session", "Claude: looks like a class")
    assert out["9"][0] == "session" and out["14"] == ("deliverable", "override")
    assert calls == [["13"]]
    cached = json.loads((state_dir / "deliverables_state.json").read_text())["llm"]["13"]
    assert cached["kind"] == "session" and cached["fp"] == dv.fingerprint(ODD)

    out2 = dv.classify_many([ODD], course="X")            # cache hit: no call
    assert out2["13"][0] == "session" and calls == [["13"]]

    changed = dict(ODD, name="Odd posting, renamed")     # fingerprint moved: ask again
    dv.classify_many([changed], course="X")
    assert calls == [["13"], ["13"]]


def test_llm_failure_falls_back_without_caching(state_dir, monkeypatch, capsys):
    import notes_backend as nb
    def boom(items):
        raise nb.NotesRateLimited("limit")
    monkeypatch.setattr(dv, "_llm_resolve", boom)
    out = dv.classify_many([ODD], course="X")
    assert out["13"] == ("ambiguous", "fallback")
    assert "tie-break unavailable" in capsys.readouterr().out
    assert not (state_dir / "deliverables_state.json").exists()


def test_tasks_llm_env_disables(state_dir, monkeypatch):
    monkeypatch.setenv("TASKS_LLM", "0")
    monkeypatch.setattr(dv, "_llm_resolve", lambda items: pytest.fail("must not call"))
    assert dv.classify_many([ODD])["13"] == ("ambiguous", "fallback")


def test_collect_windows_and_includes_submission(state_dir, monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    posts = [
        dict(NEG_Q, due_at="2026-10-01T03:59:59Z", submission={"workflow_state": "unsubmitted"}),
        dict(MP_POLL, due_at="2026-09-16T17:00:00Z", submission={"workflow_state": "pending_review"}),
        dict(FINAL, due_at="2027-06-01T03:59:59Z"),
        OFFICE,
    ]
    seen = []
    def fake_get(path, params=None):
        seen.append((path, params)); return posts
    monkeypatch.setattr(cr, "canvas_get", fake_get)
    courses = {"NEG": {"canvas_id": 40001, "term_end": None},
               "OLD": {"canvas_id": 1, "term_end": "2025-12-15T00:00:00Z"}}
    rows = dv.collect(courses, now=now, llm=False)
    assert seen == [("courses/40001/assignments", {"per_page": 100, "include[]": "submission"})]
    assert [r["a"]["id"] for r in rows] == [3]           # poll too old, final too far, office undated
    assert rows[0]["kind"] == "deliverable" and not dv.submission_done(rows[0]["a"])
    assert dv.submission_done(posts[1])
