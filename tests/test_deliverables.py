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
PARTY = _p(16, "Class Party 10/15", ["none"], None, desc="")


@pytest.mark.parametrize("posting, expected", [
    (IFC_SESSION, "session"), (GEO, "deliverable"), (NEG_Q, "deliverable"),
    (MP_POLL, "deliverable"), (INVS_DUE, "deliverable"), (MIDTERM, "both"),
    (FINAL, "deliverable"), (OFFICE, "skip"), (LTV, "session"),
    (NEG_QUIZ_DAY, "session"), (PASSPORT, "deliverable"), (MEMO, "deliverable"),
    (GUEST, "session"), (ODD, "ambiguous"), (TBD, "session"), (PARTY, "deliverable"),
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


# ── Pre-class actions inside class postings ───────────────────────────────────

from datetime import datetime, timezone

NOW = datetime(2026, 10, 20, 16, tzinfo=timezone.utc)
SG = _p(501, "Class 2 (Oct 22): SG Cowen (Case)", ["none"], due="2026-10-22T15:50:00Z",
        html_url="https://canvas/a/501",
        desc='<p>Please enter your initial assessment in this survey by 8pm on Wednesday evening (10/21): '
             '<a href="https://hbs.qualtrics.com/jfe/form/SV_9vpP">https://hbs.qualtrics.com/jfe/form/SV_9vpP</a></p>')
PCS = _p(502, "GTD | Class 16: Pravost Consulting Services", ["none"], due="2026-10-22T17:30:00Z",
         desc='<p>Please complete this pre-class poll: '
              '<a href="https://hbs.instructure.com/courses/17077/quizzes/17139">Pre-Class Poll: Class 16</a></p>')
POLL16 = _p(1174372, "Pre-Class Poll: Class 16 (Counts Towards Participation)", ["online_quiz"],
            due=None, quiz_id=17139)
POLL17 = _p(1174373, "Pre-Class Poll: Class 17", ["online_quiz"], due=None)
CLASS17 = _p(503, "GTD | Class 17: Somewhere", ["none"], due="2026-10-23T17:30:00Z", desc="<p>Read the case.</p>")
OLD_CLASS = _p(504, "Class 1: Long ago", ["none"], due="2026-09-01T15:50:00Z",
               desc='<p>Fill out <a href="https://forms.gle/x">this form</a></p>')


def test_undated_canvas_polls_take_their_class_time(state_dir, monkeypatch):
    monkeypatch.setenv("TASKS_LLM", "0")
    sessions = [PCS, CLASS17]
    out = dv.pre_class_actions(sessions, sessions + [POLL16, POLL17], NOW)
    got = {a["id"]: (a["due_at"], why) for a, why in out}
    assert got[1174372][0] == PCS["due_at"] and "Class 16" in got[1174372][1]   # by link
    assert got[1174373][0] == CLASS17["due_at"]                                 # by class number
    assert len(out) == 2                    # the Canvas quiz link is not also a text action


def test_survey_links_without_claude_are_due_before_class(state_dir, monkeypatch):
    monkeypatch.setenv("TASKS_LLM", "0")
    out = dv.pre_class_actions([SG, OLD_CLASS], [SG, OLD_CLASS], NOW)
    assert len(out) == 1                    # the September class is outside the window
    a, why = out[0]
    assert a["due_at"] == SG["due_at"] and a["html_url"] == "https://hbs.qualtrics.com/jfe/form/SV_9vpP"
    assert "earlier deadline" in a["description"] and "SG Cowen" in why
    assert not dv.submission_done(a)


def test_claude_reads_deadlines_once_and_converts_boston_time(state_dir, monkeypatch):
    calls = []
    def fake(items):
        calls.append([i["id"] for i in items])
        assert "SV_9vpP" in items[0]["text"] and items[0]["class_meets"].startswith("Thu 2026-10-22")
        return {"501": [
            {"title": "Complete SG Cowen assessment survey", "deadline": "2026-10-21T20:00",
             "link": "https://hbs.qualtrics.com/jfe/form/SV_9vpP"},
            {"title": "Answer the Canvas poll", "deadline": None,
             "link": "https://hbs.instructure.com/courses/1/quizzes/9"},          # Canvas: skipped
            {"title": "Misread deadline", "deadline": "2026-12-25T09:00", "link": None},
        ]}
    monkeypatch.setattr(dv, "_llm_actions", fake)
    out = dv.pre_class_actions([SG], [SG], NOW)
    by_name = {a["name"]: a for a, _ in out}
    assert set(by_name) == {"Complete SG Cowen assessment survey", "Misread deadline"}
    assert by_name["Complete SG Cowen assessment survey"]["due_at"] == "2026-10-22T00:00:00Z"   # 8pm EDT
    assert by_name["Misread deadline"]["due_at"] == SG["due_at"]                  # pulled back to class
    again = dv.pre_class_actions([SG], [SG], NOW)
    assert calls == [["501"]] and [a["id"] for a, _ in again] == [a["id"] for a, _ in out]
    edited = dict(SG, description=SG["description"] + "<p>Updated.</p>")
    dv.pre_class_actions([edited], [edited], NOW)
    assert calls == [["501"], ["501"]]


def test_claude_failure_falls_back_to_links_without_caching(state_dir, monkeypatch, capsys):
    def boom(items):
        raise RuntimeError("usage limit")
    monkeypatch.setattr(dv, "_llm_actions", boom)
    out = dv.pre_class_actions([SG], [SG], NOW)
    assert [a["name"] for a, _ in out] == ["Complete the survey linked in the posting"]
    assert "could not read pre-class actions" in capsys.readouterr().out
    state = state_dir / "deliverables_state.json"
    assert not state.exists() or "501" not in json.loads(state.read_text()).get("actions", {})


def test_collect_adds_pre_class_rows_that_todoist_accepts(state_dir, monkeypatch):
    import todoist_sync as ts
    monkeypatch.setenv("TASKS_LLM", "0")
    posts = [SG, PCS, POLL16]
    monkeypatch.setattr(cr, "canvas_get", lambda path, params=None: posts)
    courses = {"MHC": {"canvas_id": 17040, "term_end": None, "full_name": "Managing Human Capital"}}
    rows = dv.collect(courses, now=NOW, llm=False)
    extra = [r for r in rows if r["reason"].startswith(("undated poll", "named in"))]
    assert {r["kind"] for r in extra} == {"deliverable"} and len(extra) == 2
    tasks = ts.desired_tasks(rows, courses, NOW)
    names = {t["content"] for t in tasks}
    assert "Complete the survey linked in the posting (MHC)" in names
    assert "Pre-Class Poll: Class 16 (Counts Towards Participation) (MHC)" in names
    assert all(t["uid"].startswith("17040-") for t in tasks)
