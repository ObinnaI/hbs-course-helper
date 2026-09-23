#!/usr/bin/env python3
"""
todoist_sync.py — Every Canvas deliverable becomes one Todoist task.

Runs at the end of each refresh (TASKS_BACKEND=todoist, TODOIST_API_TOKEN set).
Tasks live in the project named by TODOIST_PROJECT (default "HBS"), one
section per course, labelled `hbs` plus the course code. The task closes by
itself once Canvas records your submission; a task you tick or delete is
never reopened or recreated; nothing is ever deleted.

State: claude/todoist_state.json (next to canvas_config.json).

    python3 scripts/todoist_sync.py --dry-run     # show what would change
    python3 scripts/todoist_sync.py               # do it
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

import deliverables
import path_config

API = "https://api.todoist.com/api/v1"
LOOKBACK_DAYS, AHEAD_DAYS = 3, 120
DEFAULT_PROJECT = "HBS"
CANVAS_TAG = "Canvas #{id}"
TIMEOUT = 30


class TodoistError(Exception):
    pass


class TodoistRateLimited(TodoistError):
    pass


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Client:
    """Thin JSON client over urllib; tests replace `request`."""

    def __init__(self, token: str):
        self.token = token
        self.writes_stopped = False

    def request(self, method: str, path: str, payload: "dict | None" = None,
                params: "dict | None" = None):
        import canvas_refresh as cr          # its opener drops Authorization on cross-host redirects
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=headers, method=method)
        for attempt in (1, 2):
            try:
                with cr._opener.open(req, timeout=TIMEOUT) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw.strip() else None
            except HTTPError as e:
                if e.code == 429 and attempt == 1:
                    wait = min(int(e.headers.get("Retry-After") or 5), 30)
                    time.sleep(wait)
                    continue
                if e.code == 429:
                    raise TodoistRateLimited("Todoist rate limit")
                detail = e.read()[:200].decode(errors="replace") if hasattr(e, "read") else ""
                raise TodoistError(f"HTTP {e.code} {method} {path}: {detail}")
            except URLError as e:
                raise TodoistError(f"network: {e.reason}")
        raise TodoistError("unreachable")

    def paged(self, path: str, params: "dict | None" = None) -> list:
        out, cursor = [], None
        while True:
            p = dict(params or {})
            if cursor:
                p["cursor"] = cursor
            data = self.request("GET", path, params=p) or {}
            if isinstance(data, list):                 # older shape
                return data
            out.extend(data.get("results") or [])
            cursor = data.get("next_cursor")
            if not cursor:
                return out

    def projects(self):            return self.paged("projects")
    def sections(self, project_id): return self.paged("sections", {"project_id": project_id})
    def tasks_in_project(self, pid): return self.paged("tasks", {"project_id": pid})
    def create_project(self, name): return self.request("POST", "projects", {"name": name})
    def create_section(self, name, pid): return self.request("POST", "sections", {"name": name, "project_id": pid})
    def get_task(self, tid):        return self.request("GET", f"tasks/{tid}")
    def create_task(self, payload): return self.request("POST", "tasks", payload)
    def update_task(self, tid, payload): return self.request("POST", f"tasks/{tid}", payload)
    def close_task(self, tid):      return self.request("POST", f"tasks/{tid}/close")


# ── State ─────────────────────────────────────────────────────────────────────

def state_file() -> Path:
    return path_config.CONFIG_FILE.parent / "todoist_state.json"


def load_state() -> dict:
    try:
        p = state_file()
        st = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        st = {}
    st.setdefault("meta", {"version": 1, "sections": {}})
    st["meta"].setdefault("sections", {})
    st.setdefault("tasks", {})
    return st


def save_state(state: dict) -> None:
    try:
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as e:
        print(f"    ⚠ could not write {state_file().name}: {e}")


# ── Desired tasks ─────────────────────────────────────────────────────────────

def due_fields(due_at: str) -> dict:
    """Canvas 23:59 (Boston) deadlines are all-day tasks; anything else keeps its time."""
    import canvas_refresh as cr
    local = cr.boston_date(due_at)
    if local.hour == 23 and local.minute == 59:
        return {"due_date": local.strftime("%Y-%m-%d")}
    return {"due_datetime": local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def priority_for(due_at: str, now: datetime) -> int:
    """Todoist API scale: 4 is the most urgent (p1 in the app)."""
    due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    left = due - now
    if left <= timedelta(days=2):
        return 4
    if left <= timedelta(days=7):
        return 3
    return 2


def _hash(d: dict) -> str:
    keys = ("content", "due_date", "due_datetime", "description", "priority")
    raw = "|".join(str(d.get(k, "")) for k in keys)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def desired_tasks(rows: list, courses: dict, now: datetime) -> list:
    """One dict per deliverable in the window, ready to compare with Todoist."""
    lo, hi = now - timedelta(days=LOOKBACK_DAYS), now + timedelta(days=AHEAD_DAYS)
    out = []
    for r in rows:
        if not deliverables.is_deliverable_kind(r["kind"]):
            continue
        a = r["a"]
        due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
        if not (lo <= due <= hi):
            continue
        abbrev, aid = r["abbrev"], a.get("id")
        name = (a.get("name") or "").strip()
        desc = deliverables._strip(a.get("description") or "")[:600]
        types = "/".join(a.get("submission_types") or [])
        pts = a.get("points_possible")
        d = {
            "uid": f"{r['cid']}-{aid}",
            "abbrev": abbrev,
            "section_name": (courses.get(abbrev) or {}).get("full_name") or abbrev,
            "content": f"{name} ({abbrev})",
            "description": "\n\n".join(x for x in [
                a.get("html_url") or "", desc,
                f"{pts:g} pts · {types}" if pts else types,
                CANVAS_TAG.format(id=aid)] if x),
            "priority": priority_for(a["due_at"], now),
            "labels": ["hbs", abbrev.lower()],
            "done": deliverables.submission_done(a),
            "due_at": a["due_at"],
            "name": name,
        }
        d.update(due_fields(a["due_at"]))
        d["hash"] = _hash(d)
        out.append(d)
    return out


# ── Reconcile ─────────────────────────────────────────────────────────────────

class Summary:
    def __init__(self):
        self.created = self.updated = self.closed = self.unchanged = self.skipped = 0
        self.lines: list[str] = []

    def __str__(self):
        return (f"Todoist: {self.created} created, {self.updated} updated, "
                f"{self.closed} closed, {self.unchanged} unchanged"
                + (f", {self.skipped} skipped (errors)" if self.skipped else ""))


def _ensure_project(client: Client, state: dict, name: str, dry_run: bool) -> "str | None":
    meta = state["meta"]
    if meta.get("project_id") and meta.get("project_name") == name:
        return meta["project_id"]
    for p in client.projects():
        if (p.get("name") or "").strip().lower() == name.lower():
            meta.update(project_id=p["id"], project_name=name)
            return p["id"]
    if dry_run:
        print(f"    + would create project {name!r}")
        return None
    p = client.create_project(name)
    meta.update(project_id=p["id"], project_name=name)
    return p["id"]


def _ensure_sections(client: Client, state: dict, pid: str, wanted: dict, dry_run: bool) -> dict:
    """{abbrev: section_id}; creates missing sections by course full name."""
    have = state["meta"]["sections"]
    missing = {ab: nm for ab, nm in wanted.items() if ab not in have}
    if not missing:
        return have
    by_name = {(s.get("name") or "").strip().lower(): s["id"] for s in client.sections(pid)}
    for ab, nm in missing.items():
        sid = by_name.get(nm.strip().lower())
        if sid is None:
            if dry_run:
                print(f"    + would create section {nm!r}")
                continue
            sid = client.create_section(nm, pid)["id"]
        have[ab] = sid
    return have


def _adopt(client: Client, state: dict, pid: str) -> None:
    """Tasks made by an earlier run whose state was lost: match on the Canvas trailer."""
    tracked = {v.get("task_id") for v in state["tasks"].values()}
    for t in client.tasks_in_project(pid):
        desc = t.get("description") or ""
        marker = desc.rstrip().rsplit("\n", 1)[-1].strip()
        if not marker.startswith("Canvas #") or t.get("id") in tracked:
            continue
        aid = marker[len("Canvas #"):].strip()
        for uid, st in state["tasks"].items():
            if uid.endswith(f"-{aid}") and not st.get("task_id"):
                st["task_id"] = t["id"]
                break
        else:
            state["tasks"].setdefault(f"?-{aid}", {"task_id": t["id"], "status": "open",
                                                    "hash": "", "adopted": True})


def _task_is_closed(task: dict) -> bool:
    return bool(task.get("checked") or task.get("is_completed") or task.get("completed_at"))


def reconcile(desired: list, seen_by_course: dict, state: dict, client: Client,
              now: datetime, dry_run: bool = False) -> Summary:
    s = Summary()
    stamp = now.isoformat(timespec="seconds")
    tasks = state["tasks"]
    stop = False

    def _call(fn, *a):
        nonlocal stop
        if stop:
            raise TodoistError("writes stopped after rate limit")
        try:
            return fn(*a)
        except TodoistRateLimited:
            stop = True
            raise

    for d in desired:
        uid = d["uid"]
        # A task adopted by trailer before we knew its uid
        if uid not in tasks and f"?-{uid.split('-', 1)[1]}" in tasks:
            tasks[uid] = tasks.pop(f"?-{uid.split('-', 1)[1]}")
        st = tasks.get(uid)
        try:
            if st is None or st.get("status") == "vanished":
                if d["done"]:
                    tasks[uid] = {"task_id": None, "hash": d["hash"], "status": "closed",
                                  "closed_reason": "submitted", "due": d["due_at"],
                                  "name": d["name"], "updated": stamp}
                    s.lines.append(f"  = already submitted, no task: {d['content']}")
                    s.unchanged += 1
                    continue
                s.lines.append(f"  + create: {d['content']}  ({d.get('due_date') or d.get('due_datetime')})")
                if not dry_run:
                    payload = {k: d[k] for k in ("content", "description", "priority", "labels")}
                    payload.update({k: d[k] for k in ("due_date", "due_datetime") if k in d})
                    payload["project_id"] = state["meta"]["project_id"]
                    sid = state["meta"]["sections"].get(d["abbrev"])
                    if sid:
                        payload["section_id"] = sid
                    t = _call(client.create_task, payload)
                    tasks[uid] = {"task_id": t["id"], "hash": d["hash"], "status": "open",
                                  "due": d["due_at"], "name": d["name"], "updated": stamp}
                s.created += 1
                continue

            if st.get("status") in ("closed", "closed_by_user", "gone"):
                s.unchanged += 1
                continue

            # open
            if d["done"]:
                s.lines.append(f"  x close (submitted): {d['content']}")
                if not dry_run:
                    _call(client.close_task, st["task_id"])
                    st.update(status="closed", closed_reason="submitted", updated=stamp)
                s.closed += 1
                continue
            if st.get("hash") != d["hash"]:
                if not dry_run:
                    try:
                        live = _call(client.get_task, st["task_id"])
                    except TodoistError as e:
                        if "HTTP 404" in str(e):
                            st.update(status="gone", updated=stamp)
                            s.lines.append(f"  - gone (deleted in Todoist): {d['content']}")
                            s.unchanged += 1
                            continue
                        raise
                    if live and _task_is_closed(live):
                        st.update(status="closed_by_user", updated=stamp)
                        s.lines.append(f"  = ticked in Todoist, left alone: {d['content']}")
                        s.unchanged += 1
                        continue
                    payload = {k: d[k] for k in ("content", "description", "priority")}
                    payload.update({k: d[k] for k in ("due_date", "due_datetime") if k in d})
                    _call(client.update_task, st["task_id"], payload)
                    st.update(hash=d["hash"], due=d["due_at"], name=d["name"], updated=stamp)
                s.lines.append(f"  ~ update: {d['content']}")
                s.updated += 1
                continue
            s.unchanged += 1
        except TodoistError as e:
            s.skipped += 1
            s.lines.append(f"  ! {d['content']}: {e}")
            if stop:
                break

    # Vanished from Canvas (only when that course's fetch returned something)
    for uid, st in list(tasks.items()):
        if st.get("status") != "open" or uid.startswith("?-"):
            continue
        cid, aid = uid.split("-", 1)
        ids = seen_by_course.get(str(cid))
        if ids and aid not in ids:
            s.lines.append(f"  x close (removed from Canvas): {st.get('name')}")
            if not dry_run:
                try:
                    _call(client.close_task, st["task_id"])
                    st.update(status="vanished", closed_reason="vanished", updated=stamp)
                except TodoistError as e:
                    s.skipped += 1
                    s.lines.append(f"  ! {st.get('name')}: {e}")
                    continue
            s.closed += 1
    return s


# ── Entry point ───────────────────────────────────────────────────────────────

def run(dry_run: bool = False, now: "datetime | None" = None, client: "Client | None" = None,
        llm: bool = True) -> "Summary | None":
    import canvas_refresh as cr
    now = now or datetime.now(timezone.utc)
    token = cr.cfg("TODOIST_API_TOKEN")
    if not token and client is None:
        print("  Todoist: TODOIST_API_TOKEN not set — tasks skipped")
        return None
    client = client or Client(token)
    courses = path_config.resolve()["courses"]
    project_name = cr.cfg("TODOIST_PROJECT") or DEFAULT_PROJECT

    rows = deliverables.collect(courses, now, LOOKBACK_DAYS, AHEAD_DAYS, llm=llm)
    seen_by_course: dict = {}
    for r in rows:
        seen_by_course.setdefault(str(r["cid"]), set()).add(str(r["a"].get("id")))
    # Courses that were fetched but yielded no rows still count as "seen" (empty set
    # means: fetch returned nothing, so do not close anything for it).
    desired = desired_tasks(rows, courses, now)

    state = load_state()
    try:
        pid = _ensure_project(client, state, project_name, dry_run)
        if pid is None:
            summary = Summary()
            for d in desired:
                summary.lines.append(f"  + create: {d['content']}")
            summary.created = len(desired)
            print("\n".join(summary.lines) or "  (nothing)")
            print(f"  {summary} (dry run, project missing)")
            return summary
        wanted = {d["abbrev"]: d["section_name"] for d in desired}
        _ensure_sections(client, state, pid, wanted, dry_run)
        if not state["tasks"]:
            _adopt(client, state, pid)
    except TodoistError as e:
        print(f"  ⚠ Todoist: {e} — tasks skipped this run")
        return None

    summary = reconcile(desired, seen_by_course, state, client, now, dry_run)
    for line in summary.lines:
        print(line)
    print(f"  {summary}{' (dry run — nothing written)' if dry_run else ''}")
    if not dry_run:
        save_state(state)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="Rules only for uncertain postings")
    args = ap.parse_args(argv)
    run(dry_run=args.dry_run, llm=not args.no_llm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
