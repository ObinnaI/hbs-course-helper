#!/usr/bin/env python3
"""
ics_feed.py — Canvas deliverables → a subscribable .ics calendar feed.

The AppleScript sync (calendar_sync.py) needs a Mac with Calendar.app. This
writes the same deadlines as an iCalendar file that any calendar app can
subscribe to from a URL, so a run with no Mac at all still keeps the calendar
current. The cloud workflow publishes the file to a secret Gist.

Output (next to canvas_config.json):
  canvas.ics       — the feed
  ics_state.json   — per-event hash + SEQUENCE, so a moved due date updates
                     the existing event instead of adding a second one

Run standalone:
  python3 scripts/ics_feed.py
"""

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import path_config
import canvas_refresh as cr
import deliverables

CAL_NAME      = "Canvas Assignments"
LOOKBACK_DAYS = 60          # keep recent past deadlines so the calendar isn't rewritten history
EVENT_LENGTH  = "PT30M"
UID_DOMAIN    = "hbs-course-helper"


def _state_dir() -> Path:
    return path_config.CONFIG_FILE.parent


def state_file() -> Path:
    return _state_dir() / "ics_state.json"


def out_file() -> Path:
    return _state_dir() / "canvas.ics"


# ── RFC 5545 plumbing ─────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Lines are at most 75 octets; continuation lines start with a space."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out: list[str] = []
    cur = ""
    for ch in line:
        limit = 75 if not out else 74
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur = ""
        cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def _utc(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _fingerprint(a: dict) -> str:
    raw = (f"{a.get('due_at')}|{a.get('name')}|{cr.strip_html(a.get('description') or '')}"
           f"|{'done' if deliverables.submission_done(a) else 'open'}")
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _start(due_at: str) -> str:
    """A 23:59 (Boston) deadline is an all-day event on its date; others keep their time."""
    local = cr.boston_date(due_at)
    if local.hour == 23 and local.minute == 59:
        return f"DTSTART;VALUE=DATE:{local.strftime('%Y%m%d')}"
    return f"DTSTART:{_utc(due_at)}"


# ── Events ────────────────────────────────────────────────────────────────────

def build_events(courses: dict, now: "datetime | None" = None, llm: bool = True) -> list[dict]:
    """
    One event per deliverable (deliverables.py decides what that is) due within
    the last LOOKBACK_DAYS or the next year. Class sessions never appear: the
    school's own calendar feed already has them. A submitted one keeps its
    event, prefixed ✓, so the history stays on the calendar.
    """
    now = now or datetime.now(timezone.utc)
    events = []
    for r in deliverables.collect(courses, now, since_days=LOOKBACK_DAYS,
                                  ahead_days=366, llm=llm):
        if not deliverables.is_deliverable_kind(r["kind"]):
            continue
        a, cid, abbrev = r["a"], r["cid"], r["abbrev"]
        name = a.get("name", "").strip()
        done = deliverables.submission_done(a)
        events.append({
            "uid":         f"{cid}-{a.get('id')}@{UID_DOMAIN}",
            "dtstart":     _start(a["due_at"]),
            "sort":        _utc(a["due_at"]),
            "summary":     f"{'✓ ' if done else 'DUE: '}{name} ({abbrev})",
            "description": cr.strip_html(a.get("description") or "")[:800],
            "url":         a.get("html_url", ""),
            "fingerprint": _fingerprint(a),
        })
    events.sort(key=lambda e: (e["sort"], e["uid"]))
    return events


def _apply_state(events: list[dict], state: dict, now: datetime) -> dict:
    """
    Assign SEQUENCE and DTSTAMP from the stored state. SEQUENCE increases when
    an event's content changes, which is what tells a subscribed calendar to
    replace its copy; DTSTAMP only moves then too, so an unchanged feed is
    byte-identical between runs and never needs republishing.
    """
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    new_state: dict = {}
    for e in events:
        st = state.get(e["uid"])
        if st is None:
            seq, updated = 0, stamp
        elif st.get("hash") != e["fingerprint"]:
            seq, updated = int(st.get("seq", 0)) + 1, stamp
        else:
            seq, updated = int(st.get("seq", 0)), st.get("updated", stamp)
        e["seq"] = seq
        e["dtstamp"] = updated
        new_state[e["uid"]] = {"hash": e["fingerprint"], "seq": seq, "updated": updated}
    return new_state


def render(events: list[dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{UID_DOMAIN}//canvas//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(CAL_NAME)}",
        "X-WR-TIMEZONE:America/New_York",
        "X-PUBLISHED-TTL:PT1H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
    ]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}",
            f"DTSTAMP:{e['dtstamp']}",
            f"LAST-MODIFIED:{e['dtstamp']}",
            f"SEQUENCE:{e['seq']}",
            e["dtstart"],
        ]
        if not e["dtstart"].startswith("DTSTART;VALUE=DATE"):
            lines.append(f"DURATION:{EVENT_LENGTH}")
        lines.append(f"SUMMARY:{_esc(e['summary'])}")
        if e["description"]:
            lines.append(f"DESCRIPTION:{_esc(e['description'])}")
        if e["url"]:
            lines.append(f"URL:{e['url']}")
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER:-P1D",
            "DESCRIPTION:Due tomorrow",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def write(courses: "dict | None" = None, now: "datetime | None" = None) -> Path:
    """Build the feed for every course and write canvas.ics + ics_state.json."""
    if courses is None:
        courses = path_config.resolve()["courses"]
    now = now or datetime.now(timezone.utc)

    try:
        state = json.loads(state_file().read_text()) if state_file().exists() else {}
    except Exception:
        state = {}

    events = build_events(courses, now)
    new_state = _apply_state(events, state, now)

    out = out_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(events), newline="")
    state_file().write_text(json.dumps(new_state, indent=2, sort_keys=True))
    return out


if __name__ == "__main__":
    path = write()
    n = path.read_text().count("BEGIN:VEVENT")
    print(f"✅ {path}  ({n} event(s))")
