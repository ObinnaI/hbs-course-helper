"""
canvas_common.py — Small helpers shared by several scripts.

Deliberately imports nothing from path_config: that module resolves folders
and may call Canvas at import time, and these helpers are wanted by tests and
by code paths that should stay free of side effects.

Also the one place that knows how a class day is named on disk:

    <course folder>/<YYMMDD Class N - Title>/
        Cheat Sheet - <Title>.docx              ← the notes (+ a hidden .md twin for the tools)
        YYMMDD ABBREV Podcast.m4a
        .notes_meta.json
        <readings>

The six-digit date prefix is the key; everything after it is a human label
that is derived once, when the folder is created, and never re-derived — so
a professor renaming a posting cannot spawn a second folder.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


# ── File identity ─────────────────────────────────────────────────────────────

def file_md5(path: Path, chunk: int = 1 << 20) -> str:
    """MD5 of a file, read in chunks so a 150-page scanned case isn't held twice."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def same_content(a: Path, b: Path) -> bool:
    """
    True only when two files are byte-identical.

    Sizes are compared first because the common case — same name, different
    document — costs one stat that way instead of two full reads. A matching
    size alone is not evidence of anything: two exhibits exported from the same
    template are routinely the same size.
    """
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    return file_md5(a) == file_md5(b)


# ── Canvas assignment classification ─────────────────────────────────────────

# submission_types that indicate a class-session prep posting (not a deliverable)
_SESSION_TYPES = {("not_graded",), ("none",)}

# submission_types that require the student to hand something in
_ACTION_TYPES = {
    "online_upload", "online_quiz", "online_text_entry",
    "media_recording", "on_paper", "external_tool", "online_url",
}


def classify_submission(sub_types: "list | None") -> str:
    """
    'session', 'deliverable', or 'ambiguous' for a Canvas assignment's
    submission_types.

    A class posting is what a professor uses to publish the case and the
    discussion questions; it is not graded and nothing is submitted. Anything
    with an upload, quiz, or text entry is a deliverable and gets a calendar
    event rather than a session folder. Everything else is flagged so a
    mis-typed class posting is visible instead of silently dropped.
    """
    sub_types = sub_types or []
    if tuple(sorted(sub_types)) in _SESSION_TYPES:
        return "session"
    if any(s in _ACTION_TYPES for s in sub_types):
        return "deliverable"
    return "ambiguous"


# ── Names ─────────────────────────────────────────────────────────────────────

def safe_name(s: str, max_len: int = 200) -> str:
    """A string that is safe as a file or folder name on macOS, Linux, iCloud."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", s)
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"-{2,}", "-", s).strip(". -")[:max_len]


def class_number(text: str) -> "int | None":
    """'MP | Class 5: Pave' → 5; 'NEG | Session 3 | Treu' → 3 (HBS uses both words)."""
    m = re.search(r"\b(?:class|session)\s+(\d+)\b", text or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


_CLASS_PREFIX_RE = re.compile(r"^\s*(?:session|class)\s+\d+\s*[:\-–—]?\s*", re.IGNORECASE)

# Some courses title every posting with its date — "Thu. Sept 17 - Treu Pharma
# II". The folder already starts with the date, so repeating it there reads
# badly and buries the case name.
_DATE_PREFIX_RE = re.compile(
    r"""^\s*
        (?:(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat|sun)[a-z]*\.?\s*,?\s*)?   # Thu. / Thursday,
        (?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*    # Sept.
        \d{1,2}(?:st|nd|rd|th)?                                                 # 17th
        \s*[-–—:]\s*""",
    re.IGNORECASE | re.VERBOSE)


def extract_case_title(name: str) -> str:
    """
    Strip 'COURSE | Class N: ' boilerplate, return the case/topic title.
    e.g. "CFO | Class 3: The DCF Method" → "The DCF Method"
    """
    name = _DATE_PREFIX_RE.sub("", name or "", count=1).strip()
    m = re.search(r"(?:class|session)\s+\d+\s*[:\-–—|]\s*(.*)", name, re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    if "|" in name:
        return name.split("|")[-1].strip()
    return name.strip()


# ── Session folders ───────────────────────────────────────────────────────────

SESSION_RE = re.compile(r"^(\d{6})(?:\s|$)")
MAX_LABEL = 60

# Folders inside a course that are never class days and must never be renamed,
# deduplicated against, or treated as readings by the scripts.
PROTECTED_DIR_RE = re.compile(
    r"^(quiz|exam|course docs|course textbook|course materials|general|overview|"
    r"claude|announcements|rh)\b", re.IGNORECASE)

# What a notes document is called, whatever style produced it.
NOTES_FILE_RE = re.compile(r"(cheat\s*sheet|\bnotes\b)", re.IGNORECASE)


def session_date(name: str) -> "str | None":
    """'260916 Class 5 - Pave' → '260916'; None for anything else."""
    m = SESSION_RE.match(name or "")
    return m.group(1) if m else None


def is_session_dir(p: Path) -> bool:
    return p.is_dir() and SESSION_RE.match(p.name) is not None


def is_protected_dir(name: str) -> bool:
    return PROTECTED_DIR_RE.match(name or "") is not None


def is_notes_file(name: str) -> bool:
    """'Cheat Sheet - Pave (A).docx', '260916 MP Notes.md' → True; readings → False."""
    p = Path(name or "")
    if p.suffix.lower() not in (".docx", ".md") or p.name.startswith("~$"):
        return False
    return NOTES_FILE_RE.search(p.stem) is not None


def _truncate_words(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    cut = s[:limit + 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" -–—:,;")


def session_title(assignments) -> str:
    """The human title of a class day: the first posting's title, cleaned."""
    for a in assignments or ():
        title = extract_case_title(a.get("name", "") if isinstance(a, dict) else "")
        title = _CLASS_PREFIX_RE.sub("", title).strip()
        if title:
            return _truncate_words(safe_name(title), MAX_LABEL)
    return ""


def session_label(assignments, abbrev: str) -> str:
    """
    'Class 5 - Pave' | 'Class 5 & 6 - Pave' | 'Class 5' | 'Pave' | abbrev.
    """
    numbers = sorted({cn for a in (assignments or ())
                      if (cn := class_number(a.get("name", ""))) is not None})
    title = session_title(assignments)
    num = ("Class " + " & ".join(str(n) for n in numbers)) if numbers else ""
    if num and title:
        return f"{num} - {title}"
    return num or title or abbrev


def session_dirname(date_str: str, abbrev: str, assignments=()) -> str:
    """Folder name for a class day. With no postings this is 'YYMMDD ABBREV'."""
    return f"{date_str} {session_label(assignments, abbrev)}"


def artifact_stem(date_str: str, abbrev: str) -> str:
    """Prefix for per-day files that keep the short form: '260916 MP'."""
    return f"{date_str} {abbrev}"


def find_session_dir(course_folder: Path, date_str: str) -> "Path | None":
    """
    The existing folder for a class day, found by its date prefix alone.

    More than one (a legacy '260916 MP' beside an adopted '260916 Class 5 -
    Pave'): prefer the one that has been generated into, then the fuller one.
    Never merges.
    """
    if not course_folder.exists():
        return None
    hits = [d for d in course_folder.iterdir()
            if is_session_dir(d) and session_date(d.name) == date_str]
    if not hits:
        return None
    if len(hits) > 1:
        def rank(d: Path):
            return (0 if (d / ".notes_meta.json").exists() else 1,
                    -sum(1 for f in d.iterdir() if f.is_file()),
                    d.name)
        hits.sort(key=rank)
        print(f"    ⚠ two folders for {date_str} in {course_folder.name}: "
              f"using '{hits[0].name}', ignoring '{hits[1].name}'")
    return hits[0]


def session_dir_for(course_folder: Path, date_str: str, abbrev: str,
                    assignments=()) -> Path:
    """The folder for a class day: the existing one, else the name it would get."""
    return (find_session_dir(course_folder, date_str)
            or course_folder / session_dirname(date_str, abbrev, assignments))


def podcast_path(session_dir: Path, date_str: str, abbrev: str) -> Path:
    return session_dir / f"{artifact_stem(date_str, abbrev)} Podcast.m4a"


# ── Course-level materials folder ─────────────────────────────────────────────

MATERIALS_DEFAULT = "Course Materials"
_MATERIALS_RE = re.compile(r"^course\s+(docs|materials|textbook|readings|notes)", re.IGNORECASE)


def materials_dir_name(course_folder: Path) -> str:
    """
    The course-level folder (syllabus, textbook, wrap-ups, the course brief).
    A folder the user already keeps — "Course Textbook and Materials",
    "Course Docs" — is adopted rather than duplicated; the tool's own older
    "General" is honoured; otherwise "Course Materials" is created.
    """
    if course_folder.exists():
        names = [d.name for d in course_folder.iterdir() if d.is_dir()]
        if MATERIALS_DEFAULT in names:
            return MATERIALS_DEFAULT
        for n in sorted(names):
            if _MATERIALS_RE.match(n):
                return n
        if "General" in names:
            return "General"
    return MATERIALS_DEFAULT


def materials_dir(course_folder: Path) -> Path:
    return course_folder / materials_dir_name(course_folder)


# ── Notes files ───────────────────────────────────────────────────────────────

@dataclass
class NotesPaths:
    docx: Path              # where the writer puts the notes
    md: Path                # the Markdown twin
    meta: Path              # .notes_meta.json
    existing: "Path | None" # the notes this folder already has, whatever they're called


def notes_filename(date_str: str, abbrev: str, title: str = "") -> str:
    """'Cheat Sheet - Pave (A)' when the day has a title, else '260916 MP Notes'."""
    title = _truncate_words(safe_name(title or ""), 80)
    return f"Cheat Sheet - {title}" if title else f"{artifact_stem(date_str, abbrev)} Notes"


def notes_paths(session_dir: Path, date_str: str, abbrev: str,
                title: str = "") -> NotesPaths:
    stem = notes_filename(date_str, abbrev, title)
    docx = session_dir / f"{stem}.docx"
    md   = session_dir / f".{stem}.md"       # hidden: the Word file is the cheat sheet you see
    meta = session_dir / ".notes_meta.json"

    existing: "Path | None" = None
    if docx.exists():
        existing = docx
    else:
        # A folder the user (or an earlier version) wrote into under another name.
        recorded = None
        if meta.exists():
            try:
                import json
                recorded = json.loads(meta.read_text()).get("notes_file")
            except Exception:
                recorded = None
        if recorded and (session_dir / recorded).exists():
            existing = session_dir / recorded
        elif session_dir.exists():
            candidates = sorted(p for p in session_dir.iterdir()
                                if p.is_file() and p.suffix.lower() == ".docx"
                                and is_notes_file(p.name))
            if candidates:
                if len(candidates) > 1:
                    print(f"    ⚠ several notes files in {session_dir.name}; "
                          f"using '{candidates[0].name}'")
                existing = candidates[0]
    return NotesPaths(docx=docx, md=md, meta=meta, existing=existing)
