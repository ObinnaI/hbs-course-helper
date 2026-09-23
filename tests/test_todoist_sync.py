import json
from datetime import datetime, timezone

import pytest

import canvas_refresh as cr
import path_config
import todoist_sync as ts

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class FakeClient(ts.Client):
    """Canned Todoist. Records every write; raises what `fail` says."""

    def __init__(self, projects=None, sections=None, tasks=None):
        super().__init__("tok")
        self._projects = projects if projects is not None else [{"id": "P1", "name": "HBS"}]
        self._sections = sections or []
        self._tasks = {t["id"]: t for t in (tasks or [])}
        self.calls = []
        self.fail = {}            # method name → exception to raise
        self.n = 100

    def request(self, method, path, payload=None, params=None):
        self.calls.append((method, path, payload))
        name = path.split("/")[0]
        if method == "GET":
            if path == "projects": return {"results": self._projects}
            if path == "sections": return {"results": self._sections}
            if path == "tasks": return {"results": list(self._tasks.values())}
            tid = path.split("/")[1]
            if tid not in self._tasks: raise ts.TodoistError(f"HTTP 404 GET {path}: ")
            return self._tasks[tid]
        if name in self.fail: raise self.fail[name]
        if path == "projects":
            p = {"id": f"P{self.n}", "name": payload["name"]}; self.n += 1
            self._projects.append(p); return p
        if path == "sections":
            s = {"id": f"S{self.n}", **payload}; self.n += 1
            self._sections.append(s); return s
        if path == "tasks":
            t = {"id": f"T{self.n}", "checked": False, **payload}; self.n += 1
            self._tasks[t["id"]] = t; return t
        if path.endswith("/close"):
            self._tasks[path.split("/")[1]]["checked"] = True; return None
        tid = path.split("/")[1]
        self._tasks[tid].update(payload); return self._tasks[tid]

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m == "POST"]


def _post(aid, name, due, types=("online_upload",), sub=None, desc="<p>Upload it.</p>", pts=0):
    return {"id": aid, "name": name, "due_at": due, "submission_types": list(types),
            "description": desc, "points_possible": pts, "html_url": f"https://hbs.instructure.com/courses/1/assignments/{aid}",
            "submission": {"workflow_state": sub or "unsubmitted"}}


POSTS = [
    _post(10, "Personality Questionnaire", "2026-10-01T03:59:59Z", ("online_url",)),
    _post(11, "Pre-Class Poll: Class 4", "2026-09-22T17:00:00Z", ("online_quiz",), sub="pending_review", pts=10),
    _post(12, "LTV | Class 5: Pave (A)", "2026-09-30T13:30:00Z", ("not_graded",)),
]


@pytest.fixture
def env(patch_courses, monkeypatch, tmp_path):
    cfgf = tmp_path / "claude" / "canvas_config.json"; cfgf.parent.mkdir()
    cfgf.write_text("{}")
    monkeypatch.setattr(path_config, "CONFIG_FILE", cfgf)
    monkeypatch.setenv("TASKS_LLM", "0")
    monkeypatch.setenv("TODOIST_API_TOKEN", "tok")
    monkeypatch.delenv("TODOIST_PROJECT", raising=False)
    posts = [dict(p, submission=dict(p["submission"])) for p in POSTS]
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: posts)
    return posts


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_due_fields_all_day_in_boston_time():
    assert ts.due_fields("2026-10-01T03:59:59Z") == {"due_date": "2026-09-30"}   # EDT
    assert ts.due_fields("2026-11-20T04:59:00Z") == {"due_date": "2026-11-19"}   # EST
    assert ts.due_fields("2026-09-30T21:00:00Z") == {"due_datetime": "2026-09-30T21:00:00Z"}


def test_priority_boundaries():
    assert ts.priority_for("2026-09-24T12:00:00Z", NOW) == 4
    assert ts.priority_for("2026-09-29T12:00:00Z", NOW) == 3
    assert ts.priority_for("2026-10-15T12:00:00Z", NOW) == 2


# ── run() end to end ──────────────────────────────────────────────────────────

def test_first_run_creates_tasks_and_sections(env):
    c = FakeClient()
    s = ts.run(now=NOW, client=c)
    assert (s.created, s.updated, s.closed) == (1, 0, 0)
    assert ("POST", "sections") in c.writes()
    created = [p for m, path, p in c.calls if (m, path) == ("POST", "tasks")]
    assert len(created) == 1
    t = created[0]
    assert t["content"] == "Personality Questionnaire (LTV)" and t["due_date"] == "2026-09-30"
    assert t["labels"] == ["hbs", "ltv"] and t["project_id"] == "P1" and t["section_id"].startswith("S")
    assert t["description"].endswith("Canvas #10") and "https://hbs.instructure.com" in t["description"]
    state = json.loads(ts.state_file().read_text())
    assert state["tasks"]["1-10"]["status"] == "open"
    assert state["tasks"]["1-11"]["status"] == "closed"          # already submitted: no task made
    assert "1-12" not in state["tasks"]                           # class posting, not a deliverable
    assert state["meta"]["sections"]["LTV"].startswith("S")


def test_second_run_is_idempotent(env):
    c = FakeClient()
    ts.run(now=NOW, client=c)
    n = len(c.writes())
    s = ts.run(now=NOW, client=c)
    assert len(c.writes()) == n and s.created == 0 and s.updated == 0 and s.closed == 0


def test_rename_updates_without_labels(env):
    c = FakeClient()
    ts.run(now=NOW, client=c)
    env[0]["name"] = "Personality Questionnaire (extended)"
    s = ts.run(now=NOW, client=c)
    assert s.updated == 1
    upd = [p for m, path, p in c.calls if m == "POST" and path.startswith("tasks/T") and not path.endswith("close")]
    assert upd and "labels" not in upd[-1] and upd[-1]["content"].startswith("Personality Questionnaire (extended)")


def test_ticked_in_todoist_is_left_alone(env):
    c = FakeClient()
    ts.run(now=NOW, client=c)
    tid = json.loads(ts.state_file().read_text())["tasks"]["1-10"]["task_id"]
    c._tasks[tid]["checked"] = True
    env[0]["name"] = "Renamed"
    s = ts.run(now=NOW, client=c)
    assert s.updated == 0
    assert json.loads(ts.state_file().read_text())["tasks"]["1-10"]["status"] == "closed_by_user"
    env[0]["submission"]["workflow_state"] = "unsubmitted"
    ts.run(now=NOW, client=c)
    assert not any(path.endswith("reopen") for _, path in c.writes())


def test_deleted_in_todoist_is_not_recreated(env):
    c = FakeClient()
    ts.run(now=NOW, client=c)
    tid = json.loads(ts.state_file().read_text())["tasks"]["1-10"]["task_id"]
    del c._tasks[tid]
    env[0]["name"] = "Renamed"
    ts.run(now=NOW, client=c)
    assert json.loads(ts.state_file().read_text())["tasks"]["1-10"]["status"] == "gone"
    n = len(c.writes())
    ts.run(now=NOW, client=c)
    assert len(c.writes()) == n


def test_submission_closes_and_never_reopens(env):
    c = FakeClient()
    ts.run(now=NOW, client=c)
    env[0]["submission"]["workflow_state"] = "submitted"
    s = ts.run(now=NOW, client=c)
    assert s.closed == 1 and any(path.endswith("/close") for _, path in c.writes())
    env[0]["submission"]["workflow_state"] = "unsubmitted"
    n = len(c.writes())
    ts.run(now=NOW, client=c)
    assert len(c.writes()) == n


def test_vanished_closes_but_empty_fetch_does_not(env, monkeypatch):
    c = FakeClient()
    ts.run(now=NOW, client=c)
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: [])          # Canvas hiccup
    s = ts.run(now=NOW, client=c)
    assert s.closed == 0
    monkeypatch.setattr(cr, "canvas_get", lambda *a, **k: [env[2]])    # posting really gone
    s = ts.run(now=NOW, client=c)
    assert s.closed == 1
    assert json.loads(ts.state_file().read_text())["tasks"]["1-10"]["status"] == "vanished"


def test_adoption_by_trailer_prevents_duplicates(env):
    c = FakeClient(tasks=[{"id": "T7", "checked": False, "description": "old\n\nCanvas #10"}])
    s = ts.run(now=NOW, client=c)
    assert s.created == 0 and ("POST", "tasks") not in c.writes()
    st = json.loads(ts.state_file().read_text())["tasks"]["1-10"]
    assert st["task_id"] == "T7" and st["status"] == "open"


def test_errors_are_skipped_and_rate_limit_stops(env, capsys):
    c = FakeClient(); c.fail["tasks"] = ts.TodoistError("HTTP 500 POST tasks: boom")
    s = ts.run(now=NOW, client=c)
    assert s.skipped == 1 and s.created == 0
    assert not json.loads(ts.state_file().read_text())["tasks"].get("1-10")
    c = FakeClient(); c.fail["tasks"] = ts.TodoistRateLimited("429")
    s = ts.run(now=NOW, client=c)
    assert s.skipped == 1 and "429" in " ".join(s.lines)


def test_dry_run_writes_nothing(env, capsys):
    c = FakeClient()
    s = ts.run(dry_run=True, now=NOW, client=c)
    assert s.created == 1 and c.writes() == []
    assert not ts.state_file().exists()
    assert "+ create: Personality Questionnaire (LTV)" in capsys.readouterr().out


def test_missing_token_skips(env, monkeypatch, capsys):
    monkeypatch.delenv("TODOIST_API_TOKEN")
    assert ts.run(now=NOW) is None
    assert "TODOIST_API_TOKEN not set" in capsys.readouterr().out


def test_project_created_when_absent(env, monkeypatch):
    monkeypatch.setenv("TODOIST_PROJECT", "HBS Deadlines")
    c = FakeClient(projects=[])
    ts.run(now=NOW, client=c)
    assert ("POST", "projects") in c.writes()
    assert json.loads(ts.state_file().read_text())["meta"]["project_name"] == "HBS Deadlines"
