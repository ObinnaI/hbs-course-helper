#!/usr/bin/env python3
"""
deliverables.py — Which Canvas postings are things you must hand in?

Canvas's `submission_types` is not a reliable answer at HBS: one course posts
its class sessions as `online_text_entry`, another posts real to-dos as
`not_graded`. So every posting goes through a short list of rules, the few
that stay uncertain are put to Claude once (cached), and you can pin any
posting by hand in canvas_config.json:

    "deliverable_overrides": {"1177030": "deliverable", "1175395": "session"}

Kinds:
    deliverable  something to submit → a task and a calendar deadline
    session      a class day → a folder, a cheat sheet, a podcast
    both         a class day whose posting is also the hand-in (midterm presentation)
    skip         no due date, unpublished, or pinned away
    ambiguous    the rules gave up and Claude was not available (old behaviour)

Audit what the rules decide for every active course:

    python3 scripts/deliverables.py --classify            # rules + Claude
    python3 scripts/deliverables.py --classify --no-llm   # rules only
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import canvas_common
import path_config

KINDS = ("deliverable", "session", "both", "skip", "ambiguous")
DONE_STATES = {"submitted", "pending_review", "graded", "complete"}

_TODO_RE = re.compile(
    r"\b(due|submit|submission|upload|turn in|hand in|poll|quiz|survey|questionnaire|"
    r"reflection|memo|essay|paper|project|presentation|sign|waiver|homework|"
    r"write-?up|deliverable|action items?|rsvp|class party|social)\b", re.I)
_ASSIGNMENT_RE = re.compile(r"\bassignments?\b", re.I)
_SESSION_HINT_RE = re.compile(
    r"\b(materials|case:|readings?|discussion questions|assignment questions|"
    r"study questions|prepare|come to class|guest)\b", re.I)
_QUIZ_LIKE_RE = re.compile(r"\b(poll|quiz|survey|questionnaire)\b", re.I)
_HAND_IN_TYPES = {"online_upload", "online_url", "media_recording", "on_paper"}
_PASSIVE_TYPES = {"not_graded", "none"}


def _strip(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    for ent, rep in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")]:
        text = text.replace(ent, rep)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_class_posting(name: str) -> bool:
    """'Class 7: …', 'Session 3 | …', 'Thu. Sept 17 - Treu Pharma II + QUIZ 1'."""
    return canvas_common.class_number(name) is not None or \
        bool(canvas_common._DATE_PREFIX_RE.match(name or ""))


def fingerprint(a: dict) -> str:
    raw = "|".join([
        str(a.get("name", "")), ",".join(sorted(a.get("submission_types") or [])),
        str(a.get("points_possible")), str(a.get("due_at")),
        _strip(a.get("description") or "")[:300],
    ])
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Rules ─────────────────────────────────────────────────────────────────────

def rule_classify(a: dict) -> "tuple[str, str] | None":
    """Deterministic decision, or None when the rules cannot tell."""
    if not a.get("due_at"):
        return "skip", "no due date"
    if a.get("published") is False:
        return "skip", "unpublished"
    types = set(a.get("submission_types") or [])
    pts = a.get("points_possible") or 0
    name = (a.get("name") or "").strip()
    desc = _strip(a.get("description") or "")
    classy = _looks_like_class_posting(name)

    if "online_quiz" in types:
        return "deliverable", "quiz"
    if types & _HAND_IN_TYPES or ("external_tool" in types and pts > 0):
        return "deliverable", "hand-in type"
    if "online_text_entry" in types:
        if pts > 0:
            return "deliverable", "graded text entry"
        if re.fullmatch(r"(tbd|tba|title tbd|to be announced)\.?", name, re.I):
            return "session", "placeholder class posting"
        if _TODO_RE.search(name) and not classy:
            return "deliverable", "to-do word in name"
        if classy or _SESSION_HINT_RE.search(desc):
            return "session", "class posting typed as text entry"
        return None
    if types and types <= _PASSIVE_TYPES:
        if classy:
            return "session", "class posting"
        if _TODO_RE.search(name):
            return "deliverable", "admin to-do"
        if _ASSIGNMENT_RE.search(name):
            return None
        return "session", "class posting"
    return None


def _post_rule(kind: str, reason: str, a: dict) -> "tuple[str, str]":
    """A hand-in that is also a class day (midterm presentation) is both."""
    name = a.get("name") or ""
    if kind == "deliverable" and canvas_common.class_number(name) is not None \
            and not _QUIZ_LIKE_RE.search(name):
        return "both", reason + "; class-day hand-in"
    return kind, reason


def classify_posting(a: dict, overrides: "dict | None" = None,
                     resolve: "Callable[[dict], tuple[str, str] | None] | None" = None
                     ) -> "tuple[str, str]":
    """(kind, reason) for one posting. `resolve` answers the uncertain ones."""
    if a.get("due_at") and overrides:
        pinned = overrides.get(str(a.get("id")))
        if pinned in KINDS:
            return pinned, "override"
    decided = rule_classify(a)
    if decided is None and resolve is not None:
        decided = resolve(a)
    if decided is None:
        legacy = canvas_common.classify_submission(a.get("submission_types"))
        decided = (legacy, "fallback")
    return _post_rule(*decided, a)


def is_session_kind(kind: str) -> bool:
    return kind in ("session", "both", "ambiguous")


def is_deliverable_kind(kind: str) -> bool:
    return kind in ("deliverable", "both")


def load_overrides(config: "dict | None" = None) -> dict:
    cfg = config if config is not None else path_config._load_config()
    raw = cfg.get("deliverable_overrides") or {}
    return {str(k): v for k, v in raw.items() if v in KINDS}


# ── Claude tie-break, cached ──────────────────────────────────────────────────

_RUBRIC = """You classify Canvas postings for an HBS MBA student. For each posting decide:
- "deliverable": the student must hand something in or complete an action (upload, quiz, poll, survey, form, sign a waiver, book a meeting).
- "session": a class meeting posting — the case, readings and discussion questions to prepare. Nothing is submitted.
- "both": a class day whose posting is also the hand-in (a presentation due in class).
- "skip": administrative noise with nothing to do.
Reply with JSON only: {"<id>": {"kind": "...", "reason": "<10 words>"}, ...}"""


def state_file() -> Path:
    return path_config.CONFIG_FILE.parent / "deliverables_state.json"


def _load_state() -> dict:
    try:
        p = state_file()
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as e:
        print(f"    ⚠ could not write {state_file().name}: {e}")


def _llm_resolve(items: list) -> dict:
    """{id: (kind, reason)} from one Claude Code call. Raises NotesError."""
    import notes_backend
    model = os.getenv("CLASSIFY_MODEL") or "claude-sonnet-5"
    text, _ = notes_backend.generate_with_claude_code(
        path_config.CONFIG_FILE.parent, _RUBRIC,
        "Classify each posting in the JSON on stdin. Reply with JSON only.",
        json.dumps(items, ensure_ascii=False), model)
    start = text.find("{")
    data = json.loads(text[start:]) if start >= 0 else {}
    out = {}
    for k, v in data.items():
        kind = (v or {}).get("kind") if isinstance(v, dict) else v
        if kind in ("deliverable", "session", "both", "skip"):
            out[str(k)] = (kind, ((v or {}).get("reason") if isinstance(v, dict) else "") or "Claude")
    return out


def classify_many(postings: list, overrides: "dict | None" = None,
                  llm: bool = True, course: str = "") -> dict:
    """{assignment id: (kind, reason)} for a list of postings; batched LLM tie-break."""
    overrides = overrides if overrides is not None else load_overrides()
    if os.getenv("TASKS_LLM", "1") in ("0", "false", "no"):
        llm = False
    state = _load_state() if llm else {}
    cache = state.setdefault("llm", {}) if llm else {}
    results, uncertain = {}, []
    for a in postings:
        aid = str(a.get("id"))
        pinned = overrides.get(aid)
        if a.get("due_at") and pinned in KINDS:
            results[aid] = (pinned, "override")        # a pin is taken literally
            continue
        decided = rule_classify(a)
        if decided is not None:
            results[aid] = _post_rule(*decided, a)
            continue
        hit = cache.get(aid)
        if hit and hit.get("fp") == fingerprint(a):
            results[aid] = _post_rule(hit["kind"], f"Claude: {hit.get('reason', '')}".strip(), a)
            continue
        uncertain.append(a)

    if uncertain and llm:
        items = [{"id": str(a.get("id")), "course": course, "name": a.get("name"),
                  "submission_types": a.get("submission_types"),
                  "points": a.get("points_possible"), "due": a.get("due_at"),
                  "description": _strip(a.get("description") or "")[:500]}
                 for a in uncertain[:40]]
        try:
            answers = _llm_resolve(items)
        except Exception as e:   # NotesError or a parse problem: fall back this run
            print(f"    ⚠ Claude tie-break unavailable ({str(e)[:80]}); using submission types")
            answers = {}
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for a in uncertain:
            aid = str(a.get("id"))
            ans = answers.get(aid)
            if ans:
                cache[aid] = {"fp": fingerprint(a), "kind": ans[0], "reason": ans[1], "at": stamp}
                results[aid] = _post_rule(ans[0], f"Claude: {ans[1]}", a)
        if answers:
            _save_state(state)
    for a in uncertain:
        aid = str(a.get("id"))
        if aid not in results:
            results[aid] = _post_rule(
                canvas_common.classify_submission(a.get("submission_types")), "fallback", a)
    return results


# ── Pre-class actions inside class postings ──────────────────────────────────
#
# Polls and surveys often live inside a class posting rather than as postings
# of their own: "please enter your initial assessment in this survey by 8pm on
# Wednesday (10/21): <Qualtrics link>", or a link to a Canvas poll that has no
# due date of its own ("Pre-Class Poll: Class 16"). Neither reaches the
# deadline list on its own, so each becomes a synthetic deliverable row: the
# undated Canvas poll takes its class's start time; a survey named in the text
# takes the deadline the text gives (Claude reads it, cached per posting) or,
# failing that, the class's start time.

ACTION_BEHIND_DAYS, ACTION_AHEAD_DAYS = 14, 30
_ACTION_RE = re.compile(
    r"\b(poll|survey|questionnaire|qualtrics|google form|sign[- ]?up|fill (in|out)|"
    r"submit(ted)?|complete (the|this|a|your)|enter your|before class|prior to class)\b", re.I)
_FORM_LINK_RE = re.compile(
    r"https?://[^\s\"'<>)]*(qualtrics\.com|forms\.gle|docs\.google\.com/forms|"
    r"forms\.office\.com|surveymonkey\.com|typeform\.com)[^\s\"'<>)]*", re.I)
_CANVAS_LINK_RE = re.compile(r"/courses/\d+/(assignments|quizzes)/(\d+)")

_ACTIONS_RUBRIC = """You read Canvas class postings for an HBS MBA student and list the things the student must DO outside class before a deadline: complete a survey or poll, fill in a form, submit results or an assessment through a link, sign up for something.
Not actions: reading the case or articles, preparing discussion questions, optional material, anything done in class.
Skip links to Canvas quizzes or assignments (URLs containing /quizzes/ or /assignments/); those are tracked separately.
For each posting you get its id, when the class meets (Boston time), today's date and the text with links in parentheses.
Reply with JSON only: {"<id>": [{"title": "<imperative, at most 8 words, e.g. Complete SG Cowen assessment survey>", "deadline": "<YYYY-MM-DDTHH:MM Boston time, or null when the text gives none>", "link": "<URL or null>"}], ...}
Resolve weekday-only deadlines ("by Wednesday evening") against the class date: normally the nearest such day on or before the class; when the text says the work follows the class (results submitted after a negotiation), the nearest such day after it. "Evening" without a time is 20:00, "midnight" is 23:59 of that day, a day without a time is 23:59. An id with no actions maps to []."""


def _text_with_links(html: str) -> str:
    html = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: f"{m.group(2)} ({m.group(1).replace('&amp;', '&')})", html or "",
                  flags=re.I | re.S)
    return _strip(html)


def _local_to_utc(local: str) -> "str | None":
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(local.strip()[:16])
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _llm_actions(items: list) -> dict:
    """{posting id: [action, ...]} from one Claude Code call. Raises on failure."""
    import notes_backend
    model = os.getenv("CLASSIFY_MODEL") or "claude-sonnet-5"
    text, _ = notes_backend.generate_with_claude_code(
        path_config.CONFIG_FILE.parent, _ACTIONS_RUBRIC,
        "List the actions in each posting in the JSON on stdin. Reply with JSON only.",
        json.dumps(items, ensure_ascii=False), model)
    start = text.find("{")
    data = json.loads(text[start:text.rfind("}") + 1]) if start >= 0 else {}
    return {str(k): [x for x in (v or []) if isinstance(x, dict) and x.get("title")]
            for k, v in data.items() if isinstance(v, list)}


def _rule_actions(text: str) -> list:
    """Without Claude: one action per survey/form link, due before class."""
    out, seen = [], set()
    for m in _FORM_LINK_RE.finditer(text):
        link = m.group(0).rstrip(".,;")
        if link not in seen:
            seen.add(link)
            out.append({"title": "Complete the survey linked in the posting",
                        "deadline": None, "link": link, "guess": True})
    return out


def pre_class_actions(sessions: list, postings: list, now: datetime,
                      llm: bool = True, course: str = "") -> "list[tuple[dict, str]]":
    """Synthetic deliverables (assignment-shaped dicts, reason) for the class postings given."""
    lo, hi = now - timedelta(days=ACTION_BEHIND_DAYS), now + timedelta(days=ACTION_AHEAD_DAYS)
    sessions = [s for s in sessions if s.get("due_at") and lo <= _due_utc(s) <= hi]
    undated = [a for a in postings if not a.get("due_at") and a.get("published") is not False]
    by_assignment = {str(a.get("id")): a for a in undated}
    by_quiz = {str(a.get("quiz_id")): a for a in undated if a.get("quiz_id")}
    out: list = []
    used: set = set()

    # 1. Undated Canvas polls linked from, or numbered like, a class posting.
    for s in sessions:
        number = canvas_common.class_number(s.get("name") or "")
        hits = []
        for kind, num in _CANVAS_LINK_RE.findall(s.get("description") or ""):
            hits.append((by_assignment if kind == "assignments" else by_quiz).get(num))
        if number is not None:
            hits += [a for a in undated if _QUIZ_LIKE_RE.search(a.get("name") or "")
                     and canvas_common.class_number(a.get("name") or "") == number]
        for a in hits:
            if a is None or str(a.get("id")) in used:
                continue
            used.add(str(a.get("id")))
            out.append((dict(a, due_at=s["due_at"]),
                        f"undated poll; due before {(s.get('name') or '').strip()}"))

    # 2. Surveys and forms named in the posting's text.
    texts = {str(s.get("id")): _text_with_links(s.get("description") or "") for s in sessions}
    candidates = [s for s in sessions if _ACTION_RE.search(texts[str(s.get("id"))])]
    if os.getenv("TASKS_LLM", "1") in ("0", "false", "no"):
        llm = False
    state = _load_state()
    cache = state.setdefault("actions", {})
    todo = [s for s in candidates
            if (cache.get(str(s.get("id"))) or {}).get("fp")
            != hashlib.md5(texts[str(s.get("id"))].encode()).hexdigest()[:12]]
    answers: dict = {}
    if todo and llm:
        import canvas_refresh as cr
        items = [{"id": str(s.get("id")), "course": course, "name": s.get("name"),
                  "class_meets": cr.boston_date(s["due_at"]).strftime("%a %Y-%m-%d %H:%M"),
                  "today": cr.boston_date(now.isoformat()).strftime("%a %Y-%m-%d"),
                  "text": texts[str(s.get("id"))][:3000]} for s in todo[:20]]
        try:
            answers = _llm_actions(items)
        except Exception as e:
            print(f"    ⚠ Claude could not read pre-class actions ({str(e)[:80]}); using survey links only")
    if answers:
        for s in todo:
            sid = str(s.get("id"))
            if sid in answers:
                cache[sid] = {"fp": hashlib.md5(texts[sid].encode()).hexdigest()[:12],
                              "actions": answers[sid]}
        _save_state(state)

    for s in candidates:
        sid = str(s.get("id"))
        hit = cache.get(sid)
        fresh = hit and hit.get("fp") == hashlib.md5(texts[sid].encode()).hexdigest()[:12]
        actions = hit["actions"] if fresh else _rule_actions(texts[sid])
        cls = (s.get("name") or "").strip()
        for act in actions:
            link = (act.get("link") or "").strip() or None
            if link and _CANVAS_LINK_RE.search(link):
                continue                              # a Canvas poll: handled in step 1
            due = _local_to_utc(act["deadline"]) if act.get("deadline") else None
            # A deadline a week past the class is a misreading; none at all means before class.
            if not due or _due_utc({"due_at": due}) > _due_utc(s) + timedelta(days=7):
                due = s["due_at"]
            key = hashlib.md5((link or act["title"].lower()).encode()).hexdigest()[:6]
            note = ("Deadline not stated; due before class. " if due == s["due_at"] else "") + \
                   ("Check the posting for an earlier deadline. " if act.get("guess") else "")
            out.append(({
                "id": f"{sid}-{key}",
                "name": act["title"].strip(),
                "due_at": due,
                "html_url": link or s.get("html_url", ""),
                "description": f"{note}From the class posting “{cls}”: {s.get('html_url', '')}",
                "submission_types": ["external"],
                "points_possible": 0,
                "submission": {},
            }, f"named in the posting for {cls}"))
    return out


# ── Collection across courses ─────────────────────────────────────────────────

def _due_utc(a: dict) -> datetime:
    return datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))


def submission_done(a: dict) -> bool:
    sub = a.get("submission") or {}
    return sub.get("workflow_state") in DONE_STATES or bool(sub.get("submitted_at"))


def collect(courses: dict, now: "datetime | None" = None, since_days: int = 3,
            ahead_days: int = 120, llm: bool = True) -> list:
    """
    Rows {abbrev, cid, a, kind, reason} for every dated posting of every active
    course whose due date is within [now - since_days, now + ahead_days]. One
    Canvas call per course, with the student's own submission included.
    """
    import canvas_refresh as cr
    now = now or datetime.now(timezone.utc)
    lo, hi = now - timedelta(days=since_days), now + timedelta(days=ahead_days)
    overrides = load_overrides()
    rows = []
    for abbrev in sorted(courses):
        info = courses[abbrev]
        cid = info.get("canvas_id")
        if not cid or not path_config.is_active(info, now):
            continue
        postings = cr.canvas_get(f"courses/{cid}/assignments",
                                 {"per_page": 100, "include[]": "submission"})
        window = [a for a in postings if a.get("due_at") and lo <= _due_utc(a) <= hi]
        kinds = classify_many(window, overrides, llm=llm, course=abbrev)
        for a in window:
            kind, reason = kinds[str(a.get("id"))]
            rows.append({"abbrev": abbrev, "cid": cid, "a": a, "kind": kind, "reason": reason})
        sessions = [a for a in window if is_session_kind(kinds[str(a.get("id"))][0])]
        for a, reason in pre_class_actions(sessions, postings, now, llm=llm, course=abbrev):
            rows.append({"abbrev": abbrev, "cid": cid, "a": a, "kind": "deliverable", "reason": reason})
    rows.sort(key=lambda r: (r["a"]["due_at"], r["abbrev"], str(r["a"].get("id"))))
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--classify", action="store_true", help="Print the decision for every posting")
    ap.add_argument("--no-llm", action="store_true", help="Rules only, no Claude tie-break")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    ap.add_argument("--course", metavar="ABBREV")
    ap.add_argument("--since", type=int, default=60, help="Days back (default 60)")
    ap.add_argument("--ahead", type=int, default=180, help="Days ahead (default 180)")
    args = ap.parse_args(argv)
    if not args.classify:
        ap.print_help()
        return 0
    courses = path_config.resolve()["courses"]
    if args.course:
        courses = {k: v for k, v in courses.items() if k == args.course}
    rows = collect(courses, since_days=args.since, ahead_days=args.ahead, llm=not args.no_llm)
    if args.json:
        print(json.dumps([{"id": r["a"].get("id"), "course": r["abbrev"], "kind": r["kind"],
                           "reason": r["reason"], "name": r["a"].get("name"),
                           "due": r["a"].get("due_at")} for r in rows], indent=2))
        return 0
    for r in rows:
        a = r["a"]
        mark = "*" if r["reason"].startswith("Claude") else " "
        types = "/".join(a.get("submission_types") or []) or "-"
        done = " ✓" if submission_done(a) else ""
        print(f"{a['due_at'][:10]}  {r['abbrev']:<5} {r['kind']:<11}{mark} "
              f"{types:<32} {str(a.get('points_possible') or 0):>4}  {a.get('name', '')[:60]}{done}"
              f"   ({r['reason']})")
    print("\n* decided by Claude; pin any row with deliverable_overrides in canvas_config.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
