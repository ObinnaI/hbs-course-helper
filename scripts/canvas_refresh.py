#!/usr/bin/env python3
"""
canvas_refresh.py — Smart cheat sheet refresh

Modes:
  --daily   Sync files + refresh Notes for sessions in the next 2 calendar days
  --weekly  Full forward scan: sync all courses, fill missing Notes for next 6 weeks

Never looks back — sessions with due dates in the past are always skipped.

Scheduled via launchd:
  Daily  5pm  →  canvas_refresh.py --daily
  Sunday 8am  →  canvas_refresh.py --weekly
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

# ── Path resolution (tolerates folder renames/moves) ─────────────────────────
# sys.path first: the sibling imports below only worked before because the
# script's own folder happens to be sys.path[0] when run directly.
sys.path.insert(0, str(Path(__file__).parent))
import path_config
import ai_config
import canvas_common
import canvas_organize
import canvas_readings
import weekly_overview
import calendar_sync
import participation_tracker
_paths = path_config.resolve()

DEST_ROOT    = _paths["coursework_root"]
PROMPT_FILE  = _paths["master_prompt"]
ENV_FILE     = _paths["env_file"] or Path("/dev/null")
_COURSES     = _paths["courses"]   # abbrev → {canvas_id, folder_path, ...}
COURSE_NAMES = path_config.COURSE_NAMES

# Build flat dicts for callers that need them. ACTIVE_COURSES drives the
# scheduled loops (no API calls for a term that ended a month ago); COURSES
# stays complete so on-demand work on an old session still resolves.
COURSES = {a: d["canvas_id"] for a, d in _COURSES.items() if d["folder_path"]}
ACTIVE_COURSES = {a: d["canvas_id"] for a, d in _COURSES.items()
                  if d["folder_path"] and path_config.is_active(d)}

CANVAS_BASE = _paths["canvas_base"]
# Canvas due dates are wall-clock Boston time. ZoneInfo handles the EDT->EST
# switch in early November; a fixed -4 offset silently shifted every date
# bucket by an hour for the rest of the term.
BOSTON = ZoneInfo("America/New_York")
MODEL  = ai_config.MODEL

READING_EXTS = {".pdf", ".docx", ".pptx", ".doc", ".ppt", ".txt"}
SLIDE_EXTS   = {".pptx", ".ppt"}   # always routed to General/Slides/

# PDF budget. Token cost is measured with the free count_tokens endpoint rather
# than guessed from file size: the old 200k-tokens-per-MB rule over-counted a
# scanned case by ~8x (a 23-page case measures 45k, was estimated at 372k) and
# silently dropped readings that fit with room to spare.
MAX_PDF_TOKEN_BUDGET = 700_000       # Sonnet holds 1M; leave room for prompt + output
PDF_PAGE_LIMIT = 150  # a genuinely outsized document, not a normal long case

# Podcasts get their own, longer horizon than the file sync. They were made in
# the daily run's 2-day loop, so one could never exist more than two days out —
# there was no way to have a week of them ready, whatever the schedule said.
# Generation skips any session whose .m4a already exists, so widening this only
# adds the episodes that are actually missing.
PODCAST_HORIZON_DAYS = 7

# ── Env / config ──────────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()

def cfg(key: str) -> str:
    return os.getenv(key, ENV.get(key, ""))

def require(key: str) -> str:
    v = cfg(key)
    if not v:
        sys.exit(f"\nMissing {key} — add to {ENV_FILE} or set as env var.\n")
    return v

# ── Canvas API ────────────────────────────────────────────────────────────────

class _StripAuthOnRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req:
            from urllib.parse import urlparse
            if urlparse(newurl).netloc != urlparse(req.full_url).netloc:
                new_req.remove_header("Authorization")
        return new_req

_opener = build_opener(_StripAuthOnRedirect())

def canvas_get(path: str, params: dict | None = None) -> list | dict:
    token = require("CANVAS_API_TOKEN")
    url = f"{CANVAS_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urlencode(params)
    results = []
    while url:
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            resp = _opener.open(req, timeout=30)
        except URLError as e:
            # No network (asleep laptop, captive wifi). Previously uncaught, so
            # one unreachable moment ended the whole scheduled run in a traceback.
            print(f"    Network error, skipping this request: {e.reason}")
            return []
        except HTTPError as e:
            if e.code in (401, 403):
                # Distinct from a transient failure: the token cannot read this
                # course at all. Usually an enrolment change or a revoked token,
                # not something a retry or a code change will fix.
                print(f"    HTTP {e.code} — no access to this course "
                      f"(enrolment ended, or token revoked): {url}")
            else:
                print(f"    HTTP {e.code}: {url}")
            return []
        data = json.loads(resp.read())
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        link = resp.headers.get("Link", "")
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<(.+?)>", part)
                if m:
                    url = m.group(1)
    return results

def canvas_download(url: str, dest: Path) -> bool:
    """Download a Canvas file URL; returns True if newly downloaded."""
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = require("CANVAS_API_TOKEN")
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = _opener.open(req, timeout=60)
        dest.write_bytes(resp.read())
        return True
    except (HTTPError, URLError, OSError) as e:
        print(f"      ✗ {dest.name}: {e}")
        return False

# ── Utilities ─────────────────────────────────────────────────────────────────

def _write_skip_stub(reading_file: Path, reason: str) -> None:
    """
    Write a .txt stub next to a skipped reading so it doesn't fail silently.
    e.g. "What Is A Good Activation Rate (skipped).txt"
    """
    stub = reading_file.parent / f"{reading_file.stem} (skipped).txt"
    if not stub.exists():
        stub.write_text(
            f"NOT included in notes — {reason}\n"
            f"File: {reading_file.name}\n"
        )


safe_name = canvas_common.safe_name

def boston_date(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BOSTON)

def yymmdd(dt: datetime) -> str:
    return dt.strftime("%y%m%d")

def notes_heading(date_str: str, abbrev: str) -> str:
    """
    Build the Notes document heading: 'Full Course Name: Month D, YYYY'
    e.g. 'Corporate Financial Operations: September 2, 2026'
    """
    from datetime import date as _date
    y = int("20" + date_str[:2])
    m = int(date_str[2:4])
    d = int(date_str[4:6])
    date_long = _date(y, m, d).strftime("%B %-d, %Y")
    full_name = COURSE_NAMES.get(abbrev, abbrev)
    return f"{full_name}: {date_long}"

def pdf_page_count(path: Path) -> int:
    """Return page count of a PDF. Uses pypdf if available, falls back to regex."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except ImportError:
        pass
    except Exception:
        return 0
    # Regex fallback: /Count N appears in the Pages tree (largest value = total)
    try:
        counts = re.findall(rb'/Count\s+(\d+)', path.read_bytes())
        return max((int(c) for c in counts), default=0)
    except Exception:
        return 0


def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, rep in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")]:
        text = text.replace(ent, rep)
    return re.sub(r"[ \t]+", " ", text).strip()

class_number = canvas_common.class_number

# ── Session discovery ─────────────────────────────────────────────────────────

# Which kinds of Canvas posting get a session folder. Deliverables (quizzes,
# uploads, papers) do not: they used to get a folder of their own, and when
# one was due the same day as a class the two shared "YYMMDD ABBREV", took
# turns overwriting .notes_meta.json, and made the Notes regenerate — and bill
# — on every run. "ambiguous" stays in so a class posting a professor typed
# with an odd submission type is not silently dropped.
SESSION_KINDS = ("session", "ambiguous")


def _assignment_sort_key(a: dict):
    return (a.get("due_at") or "", a.get("id") or 0)


def _finish_session(s: dict) -> dict:
    s["assignments"].sort(key=_assignment_sort_key)
    s["assignment"] = s["assignments"][0]   # kept for callers that want a label
    return s


def get_upcoming_sessions(horizon_days: int,
                          kinds: tuple = SESSION_KINDS) -> list[dict]:
    """
    Return one entry per (course, due date) for class sessions between now
    and now+horizon_days, sorted by due date.

    Each entry: {abbrev, course_id, date_str, due_dt, assignments, assignment}
    `assignments` holds every posting due that day, earliest first;
    `assignment` is the first of them.
    """
    now = datetime.now(tz=BOSTON)
    cutoff = now + timedelta(days=horizon_days)
    sessions: dict[tuple[str, str], dict] = {}

    for abbrev, course_id in ACTIVE_COURSES.items():
        assignments = canvas_get(f"courses/{course_id}/assignments", {"per_page": 100})
        for a in assignments:
            if not a.get("due_at"):
                continue
            dt = boston_date(a["due_at"])
            if dt < now or dt > cutoff:
                continue
            kind = canvas_common.classify_submission(a.get("submission_types"))
            if kind not in kinds:
                subs = "/".join(a.get("submission_types") or [])
                print(f"    – {abbrev} {yymmdd(dt)}: not a class session, skipping "
                      f"{kind} '{a.get('name', '')[:50]}' ({subs})")
                continue
            key = (abbrev, yymmdd(dt))
            s = sessions.get(key)
            if s is None:
                s = sessions[key] = {
                    "abbrev":      abbrev,
                    "course_id":   course_id,
                    "date_str":    yymmdd(dt),
                    "due_dt":      dt,
                    "assignments": [],
                }
            s["assignments"].append(a)
            if dt < s["due_dt"]:
                s["due_dt"] = dt

    return sorted((_finish_session(s) for s in sessions.values()),
                  key=lambda s: s["due_dt"])


def assignments_on(course_id: int, date_str: str,
                   kinds: tuple = SESSION_KINDS) -> list[dict]:
    """Every posting for a course due on YYMMDD (Boston), earliest first."""
    out = []
    for a in canvas_get(f"courses/{course_id}/assignments", {"per_page": 100}):
        if not a.get("due_at") or yymmdd(boston_date(a["due_at"])) != date_str:
            continue
        if canvas_common.classify_submission(a.get("submission_types")) not in kinds:
            continue
        out.append(a)
    out.sort(key=_assignment_sort_key)
    return out


def build_session(abbrev: str, date_str: str,
                  kinds: tuple = SESSION_KINDS) -> dict:
    """
    The session dict for one specific day — what the on-demand scripts
    (cheat_sheet.py, podcast_gen.py) need to share code with the scheduled run.
    `assignment` is None when Canvas has no posting for that day.
    """
    course_id = COURSES[abbrev]
    assignments = assignments_on(course_id, date_str, kinds)
    return {
        "abbrev":      abbrev,
        "course_id":   course_id,
        "date_str":    date_str,
        "due_dt":      boston_date(assignments[0]["due_at"]) if assignments else None,
        "assignments": assignments,
        "assignment":  assignments[0] if assignments else None,
    }


def _session_assignments(session: dict) -> list[dict]:
    """Postings for a session; tolerates the older single-`assignment` shape."""
    if session.get("assignments"):
        return session["assignments"]
    return [session["assignment"]] if session.get("assignment") else []

# ── File sync (targeted) ──────────────────────────────────────────────────────

def sync_course_files(course_id: int, abbrev: str, target_date_str: str | None = None):
    """
    Download Canvas files for a course.
    If target_date_str given, only sync files relevant to that session
    (files linked in the assignment + per-class Canvas folders).
    Otherwise sync all course files to General/.
    """
    course_folder = (_COURSES.get(abbrev, {}).get("folder_path") or DEST_ROOT / abbrev)
    general_dir = course_folder / "General"
    general_dir.mkdir(parents=True, exist_ok=True)

    folders = canvas_get(f"courses/{course_id}/folders", {"per_page": 100})
    class_folders: dict[int, dict] = {}
    class_folder_ids: set[int] = set()
    for f in folders:
        n = class_number(f.get("name", ""))
        if n is not None:
            class_folders[n] = f
            class_folder_ids.add(f["id"])

    all_files = canvas_get(f"courses/{course_id}/files", {"per_page": 100})
    file_by_id = {f["id"]: f for f in all_files}

    slides_dir = general_dir / "Slides"

    # Build index of filenames already placed on disk (session folders + Slides/ + Supplemental/)
    # to avoid re-downloading files that canvas_organize already placed correctly.
    _placed: set[str] = set()
    if course_folder.exists():
        for d in course_folder.iterdir():
            if canvas_common.is_session_dir(d):
                _placed.update(f2.name for f2 in d.iterdir() if f2.is_file())
        for subdir_name in ("Slides", "Supplemental"):
            subdir = general_dir / subdir_name
            if subdir.exists():
                _placed.update(f2.name for f2 in subdir.iterdir() if f2.is_file())

    # General files: route PPTX → General/Slides/, others → General/ root
    # Skip anything already placed on disk.
    for f in all_files:
        if f.get("folder_id") not in class_folder_ids:
            fname = safe_name(f["display_name"])
            if fname in _placed:
                continue  # already on disk in the right place
            if Path(fname).suffix.lower() in SLIDE_EXTS:
                slides_dir.mkdir(parents=True, exist_ok=True)
                dest = slides_dir / fname
            else:
                dest = general_dir / fname
            if canvas_download(f["url"], dest):
                print(f"    ↓ [General] {f['display_name']}")

    if not target_date_str:
        return

    # Targeted: sync files for the specific session. Only class postings — a
    # quiz due the same day must not pull its attachments into the class folder.
    assignments = assignments_on(course_id, target_date_str)
    if not assignments:
        return
    session_dir = canvas_common.session_dir_for(course_folder, target_date_str, abbrev, assignments)
    session_dir.mkdir(parents=True, exist_ok=True)
    for a in assignments:
        cn = class_number(a.get("name", ""))

        # Per-class Canvas folder
        if cn and cn in class_folders:
            folder = class_folders[cn]
            for f in canvas_get(f"folders/{folder['id']}/files", {"per_page": 100}):
                fname = safe_name(f["display_name"])
                # PPTX always goes to General/Slides/, never in session folder
                if Path(fname).suffix.lower() in SLIDE_EXTS:
                    slides_dir.mkdir(parents=True, exist_ok=True)
                    dest = slides_dir / fname
                else:
                    dest = session_dir / fname
                if canvas_download(f["url"], dest):
                    print(f"    ↓ [canvas folder] {f['display_name']}")

        # Files linked in assignment description
        desc = a.get("description") or ""
        ids = re.findall(
            rf"instructure\.com/(?:courses/{course_id}/)?files/(\d+)", desc
        )
        for fid_str in ids:
            fid = int(fid_str)
            if fid in file_by_id:
                f = file_by_id[fid]
                fname = safe_name(f["display_name"])
                if Path(fname).suffix.lower() in SLIDE_EXTS:
                    slides_dir.mkdir(parents=True, exist_ok=True)
                    dest = slides_dir / fname
                else:
                    dest = session_dir / fname
                if canvas_download(f["url"], dest):
                    print(f"    ↓ [linked] {f['display_name']}")

# ── Markdown → docx conversion ───────────────────────────────────────────────

def _parse_inline(para, text: str) -> None:
    """Add runs to a paragraph with **bold** and *italic* applied. Always 12pt."""
    from docx.shared import Pt
    for seg in re.split(r'(\*\*[^*]+\*\*|\*[^*]+\*)', text):
        if seg.startswith('**') and seg.endswith('**'):
            r = para.add_run(seg[2:-2])
            r.bold = True
            r.font.size = Pt(12)
        elif seg.startswith('*') and seg.endswith('*'):
            r = para.add_run(seg[1:-1])
            r.italic = True
            r.font.size = Pt(12)
        elif seg:
            para.add_run(seg).font.size = Pt(12)


def _tight(para) -> None:
    """Enforce 12pt font and tight spacing on a paragraph."""
    from docx.shared import Pt
    pf = para.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after  = Pt(6)


def markdown_to_docx(md_text: str, output_path: Path,
                     title: str, metadata: dict) -> None:
    """Convert Claude's markdown output → formatted .docx file."""
    try:
        from docx import Document
        from docx.shared import Inches, Pt
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
    except ImportError:
        sys.exit("python-docx not installed — run: uv pip install python-docx")

    doc = Document()

    # Force 12pt + tight spacing on all built-in styles up front
    for style_name in ('Normal', 'Title', 'Heading 1', 'Heading 2', 'Heading 3',
                       'List Bullet', 'List Bullet 2', 'List Number'):
        try:
            st = doc.styles[style_name]
            st.font.size = Pt(12)
            st.paragraph_format.space_before = Pt(0)
            st.paragraph_format.space_after  = Pt(6)
        except KeyError:
            pass

    # Margins: 1.25 in sides, 1 in top/bottom
    for section in doc.sections:
        section.top_margin    = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin   = Inches(1.25)
        section.right_margin  = Inches(1.25)

    # Title
    p = doc.add_heading(title, level=0)
    _tight(p)

    # Metadata block
    for key, val in metadata.items():
        p = doc.add_paragraph()
        r = p.add_run(f"{key}: ")
        r.bold = True
        r.font.size = Pt(12)
        p.add_run(val).font.size = Pt(12)
        _tight(p)

    for line in md_text.split('\n'):
        s = line.rstrip()

        if not s:
            continue
        elif s.startswith('### '):
            p = doc.add_heading(level=3)
            _parse_inline(p, s[4:])
            _tight(p)
        elif s.startswith('## '):
            p = doc.add_heading(level=2)
            _parse_inline(p, s[3:])
            _tight(p)
        elif s.startswith('# '):
            p = doc.add_heading(level=1)
            _parse_inline(p, s[2:])
            _tight(p)
        elif re.match(r'^[-*_]{3,}$', s):
            # Horizontal rule via paragraph bottom border
            p = doc.add_paragraph()
            pPr = p._p.get_or_add_pPr()
            pBdr = OxmlElement('w:pBdr')
            bottom = OxmlElement('w:bottom')
            bottom.set(qn('w:val'), 'single')
            bottom.set(qn('w:sz'), '6')
            bottom.set(qn('w:space'), '1')
            bottom.set(qn('w:color'), 'AAAAAA')
            pBdr.append(bottom)
            pPr.append(pBdr)
            _tight(p)
        elif re.match(r'^    [-*] |^  [-*] ', line):
            p = doc.add_paragraph(style='List Bullet 2')
            _parse_inline(p, s.lstrip('*- \t'))
            _tight(p)
        elif s.startswith('- ') or s.startswith('* '):
            p = doc.add_paragraph(style='List Bullet')
            _parse_inline(p, s[2:])
            _tight(p)
        elif re.match(r'^\d+\. ', s):
            p = doc.add_paragraph(style='List Number')
            _parse_inline(p, re.sub(r'^\d+\. ', '', s))
            _tight(p)
        else:
            p = doc.add_paragraph(style='Normal')
            _parse_inline(p, s)
            _tight(p)

    doc.save(str(output_path))


def write_markdown(md_path: Path, title: str, metadata: dict, md_text: str) -> None:
    """
    The same document as plain Markdown, next to the .docx.

    GitHub's mobile app renders Markdown but not Word files, so this is what
    gets read on a phone when the coursework lives in a repo. Written before
    the .docx so a python-docx failure still leaves the text on disk.
    """
    lines = [f"# {title}", ""]
    for key, val in metadata.items():
        lines.append(f"**{key}:** {val}  ")
    lines += ["", md_text.strip(), ""]
    md_path.write_text("\n".join(lines))


# ── Notes metadata / staleness ────────────────────────────────────────────────
#
# What was on disk and in Canvas when the Notes were last generated lives in
# .notes_meta.json, all as content hashes. The previous check compared file
# mtimes against the Notes file's mtime, which git cannot preserve: after a
# checkout every file is "new", so a run in CI would have regenerated every
# Notes document, every day.
#
#   {
#     "assignments": {"<canvas assignment id>": "<md5 of description>[:12]"},
#     "prompt_hash": "<md5 of master prompt with refinement applied>[:12]",
#     "readings":    {"<filename>": "<md5>"},
#     "generated":   "<iso timestamp>",  "canvas": [...], "readings_included": [...], "skipped": [...]
#   }

def _reading_files(session_dir: Path) -> list[Path]:
    """Readings in a session folder, largest PDF first (proxy for the main case)."""
    if not session_dir.exists():
        return []
    return sorted(
        (f for f in session_dir.iterdir()
         if f.is_file() and f.suffix.lower() in READING_EXTS
         and not canvas_common.is_notes_file(f.name)   # a cheat sheet is not a reading
         and "(skipped)" not in f.name
         and not f.name.startswith("~$")),          # Word lock files
        key=lambda f: (-f.stat().st_size if f.suffix.lower() == ".pdf" else 0, f.name),
    )


def build_master_prompt(abbrev: str) -> str:
    """Master prompt with the course's refinement injected at [CLASS-SPECIFIC NOTES]."""
    master = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ""
    code = abbrev.replace(" ", "_")
    refinement = (_COURSES.get(abbrev, {}).get("refinement_prompt")
                  or path_config.PROMPTS_DIR / f"cheat_sheet_prompt_{code}_refinement.md")
    if refinement.exists():
        raw = re.sub(r"<!--.*?-->", "", refinement.read_text(), flags=re.DOTALL).strip()
        if raw and raw != "# CLASS-SPECIFIC NOTES":
            master = re.sub(
                r"\[CLASS-SPECIFIC NOTES.*?\].*",
                f"[CLASS-SPECIFIC NOTES]\n{raw}",
                master, flags=re.DOTALL,
            )
    return master


def _short_md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


def prompt_hash(abbrev: str) -> str:
    return _short_md5(build_master_prompt(abbrev))


def session_hashes(session: dict) -> dict[str, str]:
    """{assignment id: hash of its description} for every posting in the session."""
    return {str(a.get("id")): _short_md5(strip_html(a.get("description") or ""))
            for a in _session_assignments(session)}


def readings_fingerprint(session_dir: Path) -> dict[str, str]:
    return {f.name: canvas_common.file_md5(f) for f in _reading_files(session_dir)}


def _read_notes_meta(session_dir: Path) -> dict:
    meta_file = session_dir / ".notes_meta.json"
    try:
        stored = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    except Exception:
        return {}
    # Legacy format held one canvas_hash — whichever posting was generated last.
    if "assignments" not in stored and "canvas_hash" in stored:
        stored = {"legacy_canvas_hash": stored["canvas_hash"]}
    return stored


def _write_notes_meta(session_dir: Path, meta: dict) -> None:
    (session_dir / ".notes_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))


def _mtime_stale(notes_file: Path, session_dir: Path, abbrev: str,
                 skip_prompt_regen: bool, check_readings: bool,
                 check_prompt: bool) -> tuple[bool, str]:
    """
    The pre-hash check, kept for Notes generated before fingerprints were
    recorded. Runs once per legacy folder; the next generation writes hashes.
    """
    notes_mtime = notes_file.stat().st_mtime
    if check_readings:
        for f in _reading_files(session_dir):
            if f.stat().st_mtime > notes_mtime:
                return True, f"new reading: {f.name}"
    if check_prompt and not skip_prompt_regen:
        code = abbrev.replace(" ", "_")
        refinement = (_COURSES.get(abbrev, {}).get("refinement_prompt")
                      or path_config.PROMPTS_DIR / f"cheat_sheet_prompt_{code}_refinement.md")
        for prompt_f in [PROMPT_FILE, refinement]:
            if prompt_f and prompt_f.exists() and prompt_f.stat().st_mtime > notes_mtime:
                return True, f"prompt updated: {prompt_f.name}"
    return False, "up to date"


def notes_are_stale(session_dir: Path, abbrev: str, date_str: str,
                    canvas_hashes: dict | None = None,
                    skip_prompt_regen: bool = False,
                    title: str = "") -> tuple[bool, str]:
    """
    Returns (should_regenerate, reason).

    Stale when: no notes document; a Canvas posting's description changed;
    the set of reading files or any file's content changed; or (unless
    skip_prompt_regen) the master/refinement prompt changed. Nothing here
    looks at modification times. A hand-made "Cheat Sheet - X.docx" counts
    as the notes (see canvas_common.notes_paths).
    """
    notes_file = canvas_common.notes_paths(session_dir, date_str, abbrev, title).existing
    if notes_file is None:
        return True, "no Notes file yet"

    meta = _read_notes_meta(session_dir)
    canvas_hashes = canvas_hashes or {}

    # Canvas assignment description changed (professor edited the posting)
    stored_hashes = meta.get("assignments")
    if stored_hashes is None:
        legacy = meta.get("legacy_canvas_hash")
        # Exact for the single-posting case; a day that merged two postings
        # regenerates once and is recorded properly from then on.
        stored_hashes = {aid: legacy for aid in canvas_hashes} if legacy else {}
    for aid, h in canvas_hashes.items():
        if stored_hashes.get(aid) != h:
            return True, "Canvas assignment description changed"

    # Readings: names and content, not mtimes
    have_readings = "readings" in meta
    if have_readings:
        current = readings_fingerprint(session_dir)
        stored  = meta["readings"]
        for name in current:
            if name not in stored:
                return True, f"new reading: {name}"
        for name in stored:
            if name not in current:
                return True, f"reading removed: {name}"
        for name, digest in current.items():
            if stored[name] != digest:
                return True, f"reading changed: {name}"

    # Prompt text
    have_prompt = "prompt_hash" in meta
    if have_prompt and not skip_prompt_regen and meta["prompt_hash"] != prompt_hash(abbrev):
        return True, "prompt updated"

    if not (have_readings and have_prompt):
        return _mtime_stale(notes_file, session_dir, abbrev, skip_prompt_regen,
                            check_readings=not have_readings, check_prompt=not have_prompt)

    return False, "up to date"

# ── Cheat sheet generation ────────────────────────────────────────────────────

def generate_notes(session: dict):
    abbrev    = session["abbrev"]
    date_str  = session["date_str"]
    assignments = _session_assignments(session)
    course_folder = (_COURSES.get(abbrev, {}).get("folder_path") or DEST_ROOT / abbrev)
    session_dir = canvas_common.session_dir_for(course_folder, date_str, abbrev, assignments)
    np = canvas_common.notes_paths(session_dir, date_str, abbrev,
                                   canvas_common.session_title(assignments))
    output_file, md_file = np.docx, np.md

    session_dir.mkdir(parents=True, exist_ok=True)

    reading_files = _reading_files(session_dir)

    # Build prompt. Two genuine class postings on one day (rare) both go in,
    # each under its own header, so the Notes cover the whole folder.
    master = build_master_prompt(abbrev)

    canvas_block = ""
    for i, a in enumerate(assignments, 1):
        name = a.get("name", "")
        desc = strip_html(a.get("description") or "")
        header = ("=== CANVAS ASSIGNMENT POSTING ===" if len(assignments) == 1
                  else f"=== CANVAS ASSIGNMENT POSTING ({i} of {len(assignments)}) ===")
        canvas_block += f"\n\n{header}\nTitle: {name}\n\n{desc}"

    prompt_text = master + canvas_block

    if not reading_files and not assignments:
        print(f"    ⚠ Nothing to generate for {date_str} {abbrev} — skipping")
        return

    # Build message content
    try:
        import anthropic as ant
    except ImportError:
        sys.exit("anthropic not installed — run: pip install anthropic")

    api_key = require("ANTHROPIC_API_KEY")
    client  = ant.Anthropic(api_key=api_key)

    # Drop byte-identical copies of the same reading. Canvas sometimes attaches a
    # file both to the assignment and to the class folder, and paying to send the
    # same case twice also costs context the model could spend on real material.
    unique_files = []
    seen_digests: dict[str, Path] = {}
    for f in reading_files:
        digest = canvas_common.file_md5(f)
        first = seen_digests.get(digest)
        if first is not None:
            print(f"    - Duplicate of {first.name}, sending once: {f.name}")
            continue
        seen_digests[digest] = f
        unique_files.append(f)
    reading_files = unique_files

    content: list[dict] = []
    skipped: list[str] = []
    pdf_token_used = 0
    if reading_files:
        content.append({"type": "text", "text": "Here are the assigned readings:"})
        for f in reading_files:
            if f.suffix.lower() == ".pdf":
                pages = pdf_page_count(f)
                if pages > PDF_PAGE_LIMIT:
                    reason = f"too long ({pages} pages, limit {PDF_PAGE_LIMIT})"
                    print(f"    WARN Skipped ({pages}p > {PDF_PAGE_LIMIT}-page limit): {f.name}")
                    skipped.append(f"{f.name} ({pages}p, too long)")
                    _write_skip_stub(f, reason)
                    content.append({"type": "text", "text": (
                        f"=== {f.name} ===\n"
                        f"[Skipped: {pages} pages exceeds the {PDF_PAGE_LIMIT}-page limit. "
                        f"Raise PDF_PAGE_LIMIT, delete the Notes file and re-run to include it.]"
                    )})
                    continue
                # Validate it's actually a PDF before sending to Claude
                raw = f.read_bytes()
                if not raw[:4].startswith(b"%PDF"):
                    print(f"    WARN Skipped (not a valid PDF, got {raw[:4]!r}): {f.name}")
                    skipped.append(f.name)
                    content.append({"type": "text", "text": (
                        f"=== {f.name} ===\n"
                        f"[File has wrong format - not a valid PDF. Summarize from Canvas description.]"
                    )})
                    continue
                block = {
                    "type": "document",
                    "source": {"type": "base64",
                               "media_type": "application/pdf",
                               "data": base64.standard_b64encode(raw).decode()},
                    "title": f.name,
                }
                # Measured, not guessed. The old size-based rule over-counted a
                # scanned case by ~8x and dropped readings that fit comfortably.
                actual = ai_config.count_document_tokens(client, block, MODEL, pages)
                if pdf_token_used + actual > MAX_PDF_TOKEN_BUDGET:
                    reason = f"token budget ({actual//1000}k tokens measured)"
                    print(f"    WARN Skipped (would exceed {MAX_PDF_TOKEN_BUDGET//1000}k budget, "
                          f"{actual//1000}k tokens): {f.name}")
                    skipped.append(f.name)
                    _write_skip_stub(f, reason)
                    content.append({"type": "text", "text": (
                        f"=== {f.name} ===\n"
                        f"[File omitted to stay within the context limit "
                        f"(~{actual//1000}k tokens). Summarize from Canvas description.]"
                    )})
                    continue
                pdf_token_used += actual
                print(f"    + {f.name} ({pages}p, {actual//1000}k tokens)")
                content.append(block)
            else:
                # .docx and .pptx are ZIP containers; read_text() on them returned
                # compressed binary, so a Word reading arrived as noise and slide
                # decks were skipped outright. Both are extracted properly now.
                text = ai_config.extract_text(f)
                if not text.strip():
                    print(f"    WARN No readable text: {f.name}")
                    skipped.append(f.name)
                    continue
                if len(text) > 400_000:  # ~100k tokens
                    text = text[:400_000] + "\n[... truncated - file too large ...]"
                print(f"    + {f.name} ({len(text):,} chars of text)")
                content.append({"type": "text", "text": f"=== {f.name} ===\n{text}"})

    content.append({"type": "text", "text": prompt_text})

    msg = client.messages.create(
        # Thinking tokens count against max_tokens on newer models; 8192 was
        # enough for the answer alone but not for the reasoning in front of it.
        model=MODEL, max_tokens=16000,
        messages=[{"role": "user", "content": content}],
    )
    if msg.stop_reason == "max_tokens":
        print(f"    ⚠ Output truncated (hit max_tokens limit) — consider splitting readings")
    cost = ai_config.estimate_cost(msg.usage, MODEL)
    print(f"    Tokens: {msg.usage.input_tokens:,} in / {msg.usage.output_tokens:,} out  (~${cost:.3f})")
    result = ai_config.response_text(msg)

    skipped_names = {entry.split(" (")[0] for entry in skipped}
    included = [f.name for f in reading_files if f.name not in skipped_names]

    metadata: dict[str, str] = {
        "Generated": datetime.now(tz=BOSTON).strftime("%Y-%m-%d %H:%M"),
    }
    if assignments:
        metadata["Canvas"] = "; ".join(a.get("name", "") for a in assignments)
    if included:
        metadata["Readings"] = ", ".join(included)
    if skipped:
        metadata["Skipped"] = ", ".join(skipped)

    title = notes_heading(date_str, abbrev)
    write_markdown(md_file, title, metadata, result)
    markdown_to_docx(
        md_text     = result,
        output_path = output_file,
        title       = title,
        metadata    = metadata,
    )

    # Record what these Notes were generated from, so the next run can tell
    # whether anything has changed without trusting file timestamps.
    _write_notes_meta(session_dir, {
        "assignments":       session_hashes(session),
        "prompt_hash":       prompt_hash(abbrev),
        "readings":          readings_fingerprint(session_dir),
        "notes_file":        output_file.name,
        "generated":         metadata["Generated"],
        "canvas":            [a.get("name", "") for a in assignments],
        "readings_included": included,
        "skipped":           skipped,
    })
    print(f"    ✅ Generated: {output_file.name}")

# ── Calendar ──────────────────────────────────────────────────────────────────

def _sync_calendar() -> None:
    """
    Apple Calendar via AppleScript on a Mac; a subscribable .ics feed anywhere
    else, or wherever CALENDAR_BACKEND=ics says so. The feed is what a run
    with no Mac can produce, and a subscribed calendar updates itself.
    """
    backend = cfg("CALENDAR_BACKEND") or ("apple" if sys.platform == "darwin" else "ics")
    if backend == "apple":
        calendar_sync.run()
    elif backend == "ics":
        import ics_feed
        out = ics_feed.write()
        print(f"  ICS feed written: {out}")
    else:
        print(f"  Calendar sync skipped (CALENDAR_BACKEND={backend})")


# ── Podcast generation (wraps podcast_gen.py) ─────────────────────────────────

# Set the first time NotebookLM rejects the stored login this run. Every later
# episode is skipped rather than failing the same way five more times.
_PODCAST_AUTH_FAILED: "str | None" = None


def _is_auth_error(e: BaseException) -> bool:
    names = {c.__name__ for c in type(e).__mro__}
    if names & {"AuthError", "AuthenticationError", "ConfigurationError"}:
        return True
    return isinstance(e, FileNotFoundError) and "storage_state" in str(e)


def _relogin_help() -> str:
    repo = os.getenv("GITHUB_REPOSITORY", "<owner>/<data repo>")
    return (
        "    ✗ NotebookLM login is missing or expired. On the Mac, run:\n"
        "        cd ~/hbs-course-helper && ./.venv/bin/notebooklm login\n"
        "      and, if podcasts run in the cloud, upload the fresh login state:\n"
        f"        gh -R {repo} secret set NOTEBOOKLM_AUTH_JSON "
        "< ~/.notebooklm/profiles/default/storage_state.json\n"
        "      Remaining podcasts are skipped this run and picked up by the next one."
    )


def generate_podcast_for_session(session: dict):
    """
    Generate a NotebookLM podcast for a session (synchronous wrapper).
    Skipped if the .m4a already exists.
    Requires notebooklm-py and a valid NotebookLM login (~/.notebooklm, or
    NOTEBOOKLM_AUTH_JSON in the environment).
    """
    global _PODCAST_AUTH_FAILED
    import asyncio
    abbrev    = session["abbrev"]
    date_str  = session["date_str"]
    course_folder = (_COURSES.get(abbrev, {}).get("folder_path") or DEST_ROOT / abbrev)
    session_dir   = canvas_common.session_dir_for(course_folder, date_str, abbrev,
                                                  _session_assignments(session))
    podcast_file  = canvas_common.podcast_path(session_dir, date_str, abbrev)

    if podcast_file.exists():
        print(f"    ✓ Podcast exists: {podcast_file.name}")
        return
    if _PODCAST_AUTH_FAILED:
        print("    – skipped: NotebookLM login failed earlier in this run")
        return

    try:
        import podcast_gen as _pg
    except ImportError as e:
        print(f"    ⚠ podcast support unavailable ({e}) — skipping podcast")
        return
    # NotebookLM intermittently times out on a single RPC ("Request timed out
    # calling GET_NOTEBOOK") and one such blip cost a whole episode. A second
    # attempt is cheap: the notebook is reused, sources are not re-uploaded, and
    # a render that has since finished is collected rather than started again.
    for attempt in (1, 2):
        try:
            asyncio.run(_pg._generate(date_str, abbrev))
            return
        except Exception as e:
            if _is_auth_error(e):
                _PODCAST_AUTH_FAILED = str(e) or type(e).__name__
                print(_relogin_help())
                return
            if attempt == 1:
                print(f"    Podcast attempt failed ({e}) — retrying once...")
                time.sleep(20)
            else:
                print(f"    ✗ Podcast generation failed: {e}")


def _podcast_path(s: dict) -> Path:
    folder = (_COURSES.get(s["abbrev"], {}).get("folder_path") or DEST_ROOT / s["abbrev"])
    session_dir = canvas_common.session_dir_for(folder, s["date_str"], s["abbrev"],
                                                _session_assignments(s))
    return canvas_common.podcast_path(session_dir, s["date_str"], s["abbrev"])


def _write_podcast_status(pending: list[dict]) -> None:
    """
    Leave a note for whoever runs next. The Mac mirror job reads it to decide
    whether to generate the episodes a cloud run could not; a CI step reads
    the GITHUB_OUTPUT line to raise a warning.
    """
    status = {
        "auth_ok":    _PODCAST_AUTH_FAILED is None,
        "error":      _PODCAST_AUTH_FAILED,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pending":    [f"{s['date_str']} {s['abbrev']}" for s in pending],
    }
    path = path_config.CONFIG_FILE.parent / "podcast_status.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2))
    except OSError as e:
        print(f"  ⚠ could not write {path.name}: {e}")
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"podcasts_skipped={'true' if _PODCAST_AUTH_FAILED else 'false'}\n")


# ── Connectivity ──────────────────────────────────────────────────────────────

def wait_for_canvas(attempts: int = 5, delay: int = 30) -> bool:
    """
    Return True once Canvas answers, False if it never does.

    launchd fires at 5pm whether or not the Mac is awake and on wifi; a machine
    that has just woken often has no DNS for a few seconds. Waiting a little
    turns a wasted run into a normal one.
    """
    from urllib.parse import urlparse
    host = urlparse(CANVAS_BASE).netloc
    for attempt in range(1, attempts + 1):
        try:
            req = Request(f"{CANVAS_BASE}/users/self",
                          headers={"Authorization": f"Bearer {cfg('CANVAS_API_TOKEN')}"})
            _opener.open(req, timeout=15)
            return True
        except HTTPError:
            return True          # reachable — auth problems surface later, per course
        except (URLError, OSError) as e:
            if attempt == attempts:
                print(f"  Cannot reach {host} after {attempts} attempts: {e}")
                return False
            print(f"  {host} unreachable ({e}) — retrying in {delay}s "
                  f"[{attempt}/{attempts}]")
            time.sleep(delay)
    return False


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_podcast_pass(horizon_days: int = PODCAST_HORIZON_DAYS,
                     max_per_run: int = 0):
    """
    Fill in missing podcasts across the whole horizon, not just the sync window.

    Runs after the sync so a session whose readings arrived tonight is included.
    Sessions that already have an .m4a are skipped, so the first run does the
    backlog and later ones do only what is new. Each episode takes 5-15 minutes
    and they render one at a time, so a full backlog is a long unattended job -
    which is why this sits at the end, after everything else has been written.
    max_per_run caps one run (a CI job has a time limit); the rest wait.
    """
    sessions = get_upcoming_sessions(horizon_days=horizon_days)
    pending = [s for s in sessions if not _podcast_path(s).exists()]

    print(f"\n{'─'*55}")
    print(f"  PODCASTS — next {horizon_days} days")
    print(f"{'─'*55}")
    if not pending:
        print(f"  All {len(sessions)} session(s) already have one.")
        _write_podcast_status([])
        return
    todo = pending
    if max_per_run and len(pending) > max_per_run:
        print(f"  {len(pending)} missing; doing {max_per_run} this run, the rest next time.")
        todo = pending[:max_per_run]
    print(f"  {len(todo)} of {len(sessions)} session(s) still need one "
          f"(~{len(todo) * 10} min, one at a time):")
    for s in todo:
        print(f"    {s['due_dt'].strftime('%a %d %b')}  {s['abbrev']}")

    made = failed = 0
    for s in todo:
        if _PODCAST_AUTH_FAILED:
            break
        print(f"\n  [{s['date_str']} {s['abbrev']}]")
        generate_podcast_for_session(s)
        if _podcast_path(s).exists():
            made += 1
        else:
            failed += 1

    still_missing = [s for s in pending if not _podcast_path(s).exists()]
    _write_podcast_status(still_missing)
    print(f"\n  Podcasts: {made} made, {len(still_missing)} still missing.")
    if still_missing and not _PODCAST_AUTH_FAILED:
        print("  Missing ones are retried on the next run; a render that outran "
              "its window is collected rather than restarted.")


def run_daily(skip_prompt_regen: bool = False, with_podcast: bool = False,
              podcast_days: int = PODCAST_HORIZON_DAYS, podcast_max: int = 0):
    """Sync files + refresh Notes for sessions in the next 2 calendar days."""
    now = datetime.now(tz=BOSTON)
    today = now.date()
    # "next 2 calendar days" = today and tomorrow
    cutoff_date = today + timedelta(days=2)

    print(f"\n{'─'*55}")
    print(f"  DAILY REFRESH — sessions through {cutoff_date}")
    print(f"{'─'*55}")

    sessions = get_upcoming_sessions(horizon_days=2)

    if not sessions:
        print("  No upcoming sessions in the next 2 days.")
        return

    for s in sessions:
        abbrev   = s["abbrev"]
        date_str = s["date_str"]
        label    = s["assignment"].get("name", f"{date_str} {abbrev}")[:60]
        print(f"\n  [{date_str}] {abbrev} — {label}")

        # Sync Canvas files + externally-linked readings for this session
        sync_course_files(s["course_id"], abbrev, target_date_str=date_str)

        course_folder = (_COURSES.get(abbrev, {}).get("folder_path") or DEST_ROOT / abbrev)
        session_dir = canvas_common.session_dir_for(course_folder, date_str, abbrev, s["assignments"])
        n_read = sum(canvas_readings.sync_reading_links(a, session_dir)
                     for a in s["assignments"])
        if n_read:
            print(f"    ↓ {n_read} reading(s) saved")

        stale, reason = notes_are_stale(
            session_dir, abbrev, date_str,
            canvas_hashes=session_hashes(s), skip_prompt_regen=skip_prompt_regen,
            title=canvas_common.session_title(s["assignments"]),
        )
        if stale:
            print(f"    → Regenerating Notes ({reason})")
            generate_notes(s)
        else:
            print(f"    ✓ Notes up to date")

    print("\n  Organizing folders...")
    canvas_organize.organize_all(verbose=True)

    print("\n  Checking for duplicates...")
    trashed = canvas_organize.dedup_to_trash(verbose=True)
    if trashed:
        print(f"  {trashed} duplicate(s) moved to Trash.")

    print("\n  Syncing calendar...")
    _sync_calendar()

    if with_podcast:
        run_podcast_pass(podcast_days, podcast_max)

    print(f"\n{'─'*55}")
    print("  Daily refresh complete.")
    print(f"{'─'*55}\n")


def run_weekly(skip_prompt_regen: bool = False, with_podcast: bool = False,
               podcast_days: int = PODCAST_HORIZON_DAYS, podcast_max: int = 0):
    """
    Full forward scan:
      - Sync files for all courses (6-week horizon)
      - Generate Notes only for sessions within the next 2 weeks
        (avoids burning compute on sessions that may still change)
    """
    now = datetime.now(tz=BOSTON)
    notes_cutoff = now + timedelta(days=14)

    print(f"\n{'─'*55}")
    print(f"  WEEKLY REFRESH — sync 6 weeks, notes ≤ 2 weeks")
    print(f"{'─'*55}")

    # Full file sync for all courses (6-week horizon)
    print("\n  Syncing all course files...")
    for abbrev, course_id in ACTIVE_COURSES.items():
        print(f"  {abbrev}...")
        sync_course_files(course_id, abbrev, target_date_str=None)

    # Notes: only sessions in the next 2 weeks
    all_sessions = get_upcoming_sessions(horizon_days=42)
    notes_sessions = [s for s in all_sessions if s["due_dt"] <= notes_cutoff]
    later_sessions = [s for s in all_sessions if s["due_dt"] > notes_cutoff]

    print(f"\n  Upcoming sessions: {len(all_sessions)} total, "
          f"{len(notes_sessions)} within 2 weeks (notes), "
          f"{len(later_sessions)} later (files only).")

    for s in all_sessions:
        abbrev   = s["abbrev"]
        date_str = s["date_str"]
        label    = s["assignment"].get("name", f"{date_str} {abbrev}")[:60]
        course_folder = (_COURSES.get(abbrev, {}).get("folder_path") or DEST_ROOT / abbrev)
        session_dir = canvas_common.session_dir_for(course_folder, date_str, abbrev, s["assignments"])

        if s["due_dt"] > notes_cutoff:
            print(f"  [{date_str}] {abbrev} — files only (>2 weeks out)")
            continue

        # Always sync Canvas folder files + external reading links for sessions in window
        print(f"\n  [{date_str}] {abbrev} — {label}")
        sync_course_files(s["course_id"], abbrev, target_date_str=date_str)
        n_read = sum(canvas_readings.sync_reading_links(a, session_dir)
                     for a in s["assignments"])
        if n_read:
            print(f"    ↓ {n_read} reading(s) saved")

        stale, reason = notes_are_stale(
            session_dir, abbrev, date_str,
            canvas_hashes=session_hashes(s), skip_prompt_regen=skip_prompt_regen,
            title=canvas_common.session_title(s["assignments"]),
        )
        if stale:
            print(f"    → Regenerating Notes ({reason})")
            generate_notes(s)
        else:
            print(f"    ✓ Notes up to date")

    print("\n  Organizing folders...")
    canvas_organize.organize_all(verbose=True)

    print("\n  Checking for duplicates...")
    trashed = canvas_organize.dedup_to_trash(verbose=True)
    if trashed:
        print(f"  {trashed} duplicate(s) moved to Trash.")
    else:
        print("  No duplicates found.")

    print("\n  Generating weekly overview...")
    ov = weekly_overview.generate()
    print(f"  Saved: {ov}")

    print("\n  Syncing calendar...")
    _sync_calendar()

    print("\n  Refreshing participation tracker...")
    try:
        participation_tracker.refresh()
    except Exception as e:
        print(f"  ⚠ Participation tracker failed: {e}")

    # The pick-which-to-skip prompt needs someone at a terminal. A scheduled or
    # CI run has nobody there (input() on a closed stdin used to return "" and
    # silently mean "all of them"), so it takes the same path as the daily run.
    interactive = sys.platform == "darwin" and sys.stdin.isatty()
    if with_podcast and not interactive:
        run_podcast_pass(podcast_days, podcast_max)
    elif with_podcast:
        import subprocess
        subprocess.run(["open", str(ov)], check=False)

        print(f"\n{'─'*55}")
        print(f"  PODCAST GENERATION — {len(notes_sessions)} session(s) in range")
        print(f"{'─'*55}")
        for i, s in enumerate(notes_sessions, 1):
            day_label = s["due_dt"].strftime("%a %b %-d")
            name  = s["assignment"].get("name", f"{s['date_str']} {s['abbrev']}")
            parts = name.split("|")
            short = (parts[-1].strip() if len(parts) > 1 else name.strip())[:55]
            print(f"  {i:2}. {day_label}  {s['abbrev']:<10} — {short}")

        print(f"\n  Enter numbers to SKIP (comma-separated), or press Enter for all:")
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""

        skip_indices: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(notes_sessions):
                    skip_indices.add(idx)

        print()
        for i, s in enumerate(notes_sessions):
            if i in skip_indices:
                print(f"  – Skipped: {s['abbrev']} {s['date_str']}")
            else:
                generate_podcast_for_session(s)

    print(f"\n{'─'*55}")
    print("  Weekly refresh complete.")
    print(f"{'─'*55}\n")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--daily",  action="store_true", help="Next 2 calendar days")
    group.add_argument("--weekly", action="store_true", help="Full 6-week forward scan")
    group.add_argument("--podcasts-only", action="store_true",
                       help="Skip the sync; just generate missing podcasts in the window "
                            "(what the Mac mirror job runs when the cloud could not)")
    parser.add_argument("--skip-prompt-regen", action="store_true",
                        help="Don't regenerate notes just because the master prompt changed "
                             "(useful after minor prompt tweaks)")
    parser.add_argument("--with-podcast", action="store_true",
                        help="Also generate NotebookLM podcasts for sessions within the podcast "
                             "window (requires notebooklm login; adds ~10 min per session)")
    parser.add_argument("--podcast-days", type=int, default=PODCAST_HORIZON_DAYS,
                        metavar="N",
                        help=f"How many days ahead to make podcasts for "
                             f"(default {PODCAST_HORIZON_DAYS}). Use a smaller number to cover "
                             f"just the rest of this week rather than into next.")
    parser.add_argument("--podcast-max", type=int, metavar="N",
                        default=int(cfg("PODCAST_MAX_PER_RUN") or 0),
                        help="At most N podcasts per run, 0 = no cap (default: "
                             "PODCAST_MAX_PER_RUN env, else 0)")
    args = parser.parse_args()

    # A run with no courses would otherwise print "No upcoming sessions" and
    # exit 0 — which in CI looks exactly like a quiet day rather than a
    # missing secret.
    if not COURSES:
        sys.exit("\n  No courses configured. Set CANVAS_API_TOKEN and CANVAS_BASE_URL "
                 "(environment or .env) and check canvas_config.json.\n")
    if not ACTIVE_COURSES:
        print("  ⚠ Every known course's term has ended; nothing to sync until the next "
              "term's courses appear in Canvas (run path_config.py --discover to check).")

    if not wait_for_canvas():
        print("  Skipping this run — nothing was changed. The next scheduled run "
              "will pick up whatever was missed.")
        return

    if args.podcasts_only:
        run_podcast_pass(args.podcast_days, args.podcast_max)
    elif args.daily:
        run_daily(skip_prompt_regen=args.skip_prompt_regen, with_podcast=args.with_podcast,
                  podcast_days=args.podcast_days, podcast_max=args.podcast_max)
    else:
        run_weekly(skip_prompt_regen=args.skip_prompt_regen, with_podcast=args.with_podcast,
                   podcast_days=args.podcast_days, podcast_max=args.podcast_max)


if __name__ == "__main__":
    main()
