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
    r"write-?up|deliverable|action items?)\b", re.I)
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
    rows.sort(key=lambda r: (r["a"]["due_at"], r["abbrev"], r["a"].get("id")))
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
