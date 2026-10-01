#!/usr/bin/env python3
"""
inbox.py — file emailed class materials into the class folder they belong to.

Some courses send materials by email rather than Canvas: a negotiation's
confidential role sheet arrives two or three days before class, as an
attachment or an HBSP link. A small Google Apps Script (tools/gmail_forwarder.gs)
drops each such email into the data repo:

    inbox/<YYMMDD>-<message id>/message.json     {id, subject, from, date, body, links}
    inbox/<YYMMDD>-<message id>/<attachments…>

`route_inbox()` runs at the start of every refresh. For each item it works out
the course (from the code in the subject, e.g. "NEG-04", "MPGTD-00") and the
class day (the posting whose title best matches the email), moves the
attachments into that day's folder, downloads any HBSP link, saves the email
text beside them (it names your role), and removes the inbox item. Whatever
cannot be placed stays in inbox/ and is reported.

    python3 scripts/inbox.py            # route what is waiting
    python3 scripts/inbox.py --dry-run  # say where each item would go
"""

import argparse
import asyncio
import difflib
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import canvas_common
import path_config

LOOKAHEAD_DAYS = 14
MATCH_THRESHOLD = 0.6
_SUBJECT_TITLE_RE = re.compile(r"(?:confidential\s+)?role\s+info(?:rmation)?\s*[:\-–]\s*(.+)$", re.I)
_INLINE_IMAGE_RE = re.compile(r"^(image\d+|outlook-[\w-]+|att\d+)\.(png|jpe?g|gif)$", re.I)
_HBSP_RE = re.compile(r"https?://[\w.]*hbsp\.harvard\.edu/\S+", re.I)


def inbox_dir() -> Path:
    return path_config.resolve()["coursework_root"] / "inbox"


def state_file() -> Path:
    return path_config.CONFIG_FILE.parent / "inbox_state.json"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ── Which course ──────────────────────────────────────────────────────────────

def course_codes(abbrev: str, info: dict, config: dict) -> list:
    """Strings that identify a course in an email subject, longest first."""
    codes = set((config.get("email_codes") or {}).get(abbrev) or [])
    cc = info.get("course_code") or ""
    for c in (cc, re.split(r"[\s\-:]+\d", cc)[0] if cc else "", abbrev):
        if c and len(c.strip()) >= 2:
            codes.add(c.strip())
    return sorted(codes, key=len, reverse=True)


def course_for(subject: str, body: str, courses: dict, config: dict) -> "str | None":
    """The course an email belongs to, by its code in the subject, else its name in the text."""
    subj = (subject or "").upper()
    best = None
    for abbrev, info in courses.items():
        for code in course_codes(abbrev, info, config):
            if re.search(rf"(?<![A-Z0-9]){re.escape(code.upper())}(?![A-Z])", subj):
                if best is None or len(code) > best[0]:
                    best = (len(code), abbrev)
    if best:
        return best[1]
    text = _norm(subject + " " + (body or "")[:1500])
    for abbrev, info in courses.items():
        name = _norm(info.get("full_name", ""))
        if name and name in text:
            return abbrev
    return None


# ── Which class day ───────────────────────────────────────────────────────────

def subject_title(subject: str) -> str:
    m = _SUBJECT_TITLE_RE.search(subject or "")
    return (m.group(1) if m else re.sub(r"^[A-Z]{2,8}[-\s]?\d*\s*:?", "", subject or "")).strip()


def _score(posting_title: str, title: str, body: str) -> float:
    pt = _norm(posting_title)
    if not pt:
        return 0.0
    ratio = difflib.SequenceMatcher(None, pt, _norm(title)).ratio()
    # "Viking Investments" vs "Viking Investment Negotiation": shared leading words count.
    words = [w for w in pt.split() if len(w) > 3 and w not in ("negotiation", "debrief", "quiz", "class")]
    if words:
        hay_title, hay_body = _norm(title), _norm(body)
        in_title = sum(w in hay_title or w.rstrip("s") in hay_title for w in words) / len(words)
        in_body = sum(w in hay_body for w in words) / len(words)
        ratio = max(ratio, 0.95 * in_title, 0.85 * in_body)
    return ratio


def session_for(title: str, body: str, sent: datetime, days: dict) -> "tuple[str, str] | None":
    """
    (date_str, how) for the class day an email is about. `days` is
    {date_str: [postings]} for the course's upcoming class days. A negotiation
    is prepared on its first day, so a debrief posting never wins a tie.
    """
    scored = []
    for ds, postings in sorted(days.items()):
        name = canvas_common.session_title(postings) or ""
        s = max(_score(canvas_common.extract_case_title(a.get("name", "")), title, body) for a in postings)
        if re.search(r"\bdebrief\b", name, re.I):
            s -= 0.2
        scored.append((s, ds))
    if not scored:
        return None
    best = max(scored, key=lambda x: (round(x[0], 3), -int(x[1])))     # earliest day wins a tie
    if best[0] >= MATCH_THRESHOLD:
        return best[1], f"title match {best[0]:.2f}"
    return None


# ── Routing ───────────────────────────────────────────────────────────────────

def _upcoming_days(cr, course_id: int, sent: datetime) -> dict:
    lo, hi = sent - timedelta(days=1), sent + timedelta(days=LOOKAHEAD_DAYS)
    days: dict = {}
    for a in cr.canvas_get(f"courses/{course_id}/assignments", {"per_page": 100}):
        if not a.get("due_at"):
            continue
        dt = cr.boston_date(a["due_at"])
        if not (lo <= dt <= hi) or not cr._kind_matches(cr.posting_kind(a), cr.SESSION_KINDS):
            continue
        days.setdefault(cr.yymmdd(dt), []).append(a)
    return days


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for n in range(2, 50):
        cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not cand.exists():
            return cand
    return dest


def route_item(item: Path, courses: dict, config: dict, cr, dry_run: bool = False) -> "tuple[bool, str]":
    """Place one inbox item. Returns (routed, message for the log)."""
    try:
        msg = json.loads((item / "message.json").read_text())
    except Exception as e:
        return False, f"unreadable message.json ({e})"
    subject, body = msg.get("subject", ""), msg.get("body", "")
    try:
        sent = datetime.fromisoformat(str(msg.get("date", "")).replace("Z", "+00:00"))
    except ValueError:
        sent = datetime.now(timezone.utc)
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)

    abbrev = course_for(subject, body, courses, config)
    if not abbrev:
        return False, f"no course recognised in {subject!r} (add its code to email_codes in canvas_config.json)"
    info = courses[abbrev]
    days = _upcoming_days(cr, info["canvas_id"], sent)
    hit = session_for(subject_title(subject), body, sent, days)
    if not hit:
        return False, f"{abbrev}: no class in the next {LOOKAHEAD_DAYS} days matches {subject_title(subject)!r}"
    ds, how = hit
    course_folder = info.get("folder_path")
    if not course_folder:
        return False, f"{abbrev}: course has no folder"
    session_dir = canvas_common.session_dir_for(course_folder, ds, abbrev, days[ds])
    files = [f for f in sorted(item.iterdir())
             if f.is_file() and f.name != "message.json" and not _INLINE_IMAGE_RE.match(f.name)]
    links = [l for l in (msg.get("links") or _HBSP_RE.findall(body)) if _HBSP_RE.match(l)]
    where = f"{abbrev} {session_dir.name} ({how}): {len(files)} file(s), {len(links)} link(s)"
    if dry_run:
        return True, "would file → " + where

    session_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        dest = session_dir / canvas_common.safe_name(f.name)
        if dest.exists() and canvas_common.same_content(dest, f):
            continue
        shutil.copy2(f, _unique(dest))
    if links:
        import canvas_readings
        title = f"{subject_title(subject)} - role (confidential)"
        try:
            asyncio.run(canvas_readings._run([{"href": l, "title": title, "optional": False} for l in links],
                                             session_dir, ds))
        except Exception as e:
            print(f"    ✗ link download failed: {e}")
    note = session_dir / f"{cr.yymmdd(cr.boston_date(sent.isoformat()))} Email - {canvas_common.safe_name(subject)[:90]}.md"
    if not note.exists():
        note.write_text(f"# {subject}\n\nFrom {msg.get('from', '')} on {sent.strftime('%Y-%m-%d')} "
                        f"— emailed class material, filed automatically.\n\n{body.strip()}\n")
    shutil.rmtree(item, ignore_errors=True)
    return True, "filed → " + where


def route_inbox(dry_run: bool = False) -> int:
    """Route every waiting item; returns how many were placed."""
    import canvas_refresh as cr
    root = inbox_dir()
    items = sorted(p for p in root.iterdir() if (p / "message.json").exists()) if root.exists() else []
    if not items:
        return 0
    paths = path_config.resolve()
    courses, config = paths["courses"], path_config._load_config()
    try:
        state = json.loads(state_file().read_text()) if state_file().exists() else {}
    except Exception:
        state = {}
    print(f"\n  Emailed materials: {len(items)} waiting in inbox/")
    n = 0
    for item in items:
        try:
            ok, text = route_item(item, courses, config, cr, dry_run)
        except Exception as e:
            ok, text = False, f"error: {e}"
        print(f"    {'↓' if ok else '⚠'} {item.name}: {text}")
        if ok and not dry_run:
            state[item.name] = {"result": text, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            n += 1
    if n:
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps(state, indent=2, sort_keys=True))
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    route_inbox(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
