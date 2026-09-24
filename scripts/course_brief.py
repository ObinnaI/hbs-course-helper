#!/usr/bin/env python3
"""
course_brief.py — The running knowledge base for each course.

A Cowork project gave you three things: standing instructions, attached
files, and memory across chats. Here those are the course folder, the
materials folder, and this file:

    <Course>/<Course Materials>/Course Brief.md

    # <Course> — course brief
    ## About this course                 ← written once; yours to edit
    ## How this professor runs class     ← yours to edit (same role as the refinement prompt)
    ## Lenses and frameworks so far      ← one auto block per class that has happened
    ## Threads to carry forward          ← auto, rolled up from the class blocks
    ## Materials index                   ← auto, one line per file in the materials folder

Auto blocks are fenced with <!-- auto:… --> markers and are the only text
this script rewrites; everything else in the file is preserved. After each
class, the block for it is written from the cheat sheet, the readings and
whatever arrived after class (wrap-up slides, announcements), by one short
Claude call. New files in the materials folder get a one-line summary once
(cached by content hash in .materials_index.json).

The cheat sheet for class N receives the brief, the previous class's
bottom lines and post-class material, and a peek at class N+1
(context_for), so lenses carry across the course.

Run standalone:
  python3 scripts/course_brief.py                # update every active course
  python3 scripts/course_brief.py --bootstrap    # first fill, all past classes
  python3 scripts/course_brief.py --course MP    # one course
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import path_config
import canvas_common
import notes_backend
import ai_config

BRIEF_NAME   = "Course Brief.md"
INDEX_NAME   = ".materials_index.json"
STATE_NAME   = ".brief_state.json"
MAX_BLOCK_WORDS = 300
INDEXABLE = {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt"}

_AUTO_RE = re.compile(r"<!-- auto:(?P<key>[\w.\-]+) -->\n?(?P<body>.*?)<!-- /auto:(?P=key) -->\n?",
                      re.DOTALL)


# ── Paths ─────────────────────────────────────────────────────────────────────

def brief_path(course_folder: Path) -> Path:
    return canvas_common.materials_dir(course_folder) / BRIEF_NAME


def _index_path(course_folder: Path) -> Path:
    return canvas_common.materials_dir(course_folder) / INDEX_NAME


def _state_path(course_folder: Path) -> Path:
    return canvas_common.materials_dir(course_folder) / STATE_NAME


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))


# ── The brief document ────────────────────────────────────────────────────────

TEMPLATE = """# {name} — course brief

Read this first when working on anything in this course. The sections marked
auto are maintained by the scripts after each class; edit anything else freely.

## About this course

<!-- auto:about -->
_(filled in from the syllabus on the first run)_
<!-- /auto:about -->

## How this professor runs class

_Your notes: cold-call style, what earns credit, how numbers are used, pet
frameworks. The same text as prompts/cheat_sheet_prompt_{code}_refinement.md
is a good starting point._

## Lenses and frameworks so far

<!-- auto:lenses-index -->
_(one block per class appears here after the class has happened)_
<!-- /auto:lenses-index -->

## Threads to carry forward

<!-- auto:threads -->
_(open questions and "we'll come back to this" notes, rolled up from the class blocks)_
<!-- /auto:threads -->

## Materials index

<!-- auto:materials -->
_(every file in the materials folder, one line each)_
<!-- /auto:materials -->
"""

CLAUDE_MD = """# {name}

You are helping an HBS MBA student with the course **{name}** ({abbrev}).

- Read `{materials}/Course Brief.md` first — it holds the professor's style,
  the lenses and frameworks introduced so far, open threads, and an index of
  the course materials.
- Class-day folders are named `YYMMDD Class N - Title`; each holds the
  readings, the notes (`Cheat Sheet - *.docx` with a `.md` twin), sometimes a
  podcast, and anything posted after class.
- When asked about a class, read its folder (the `.md` cheat sheet is the
  quickest way in). When asked to apply a lens from an earlier class, find it
  in the brief and cite the class it came from.
- Course-level readings live in `{materials}/`.
"""


def ensure_brief(course_folder: Path, abbrev: str, full_name: str) -> Path:
    """Create the brief and the folder CLAUDE.md if missing; return the brief path."""
    p = brief_path(course_folder)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATE.format(name=full_name, code=abbrev.replace(" ", "_")))
        print(f"    + created {p.relative_to(course_folder)}")
    claude_md = course_folder / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(CLAUDE_MD.format(
            name=full_name, abbrev=abbrev, materials=canvas_common.materials_dir_name(course_folder)))
    return p


def blocks(text: str) -> dict:
    """{key: body} for every auto block in the brief."""
    return {m.group("key"): m.group("body") for m in _AUTO_RE.finditer(text)}


def upsert_block(text: str, key: str, body: str) -> str:
    """Replace the auto block `key`, or add it at the end of the lenses index."""
    body = body.strip("\n") + "\n"
    block = f"<!-- auto:{key} -->\n{body}<!-- /auto:{key} -->\n"
    pattern = re.compile(rf"<!-- auto:{re.escape(key)} -->\n?.*?<!-- /auto:{re.escape(key)} -->\n?",
                         re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    # New class block: goes just before the lenses-index closing marker's
    # section end, i.e. appended after the index placeholder.
    anchor = "<!-- /auto:lenses-index -->\n"
    if anchor in text:
        return text.replace(anchor, anchor + "\n" + block, 1)
    return text.rstrip("\n") + "\n\n" + block


# ── Materials index ───────────────────────────────────────────────────────────

def _materials_files(course_folder: Path) -> list:
    mdir = canvas_common.materials_dir(course_folder)
    if not mdir.exists():
        return []
    out = []
    for f in sorted(mdir.rglob("*")):
        if not f.is_file() or f.name.startswith(".") or f.name.startswith("~$"):
            continue
        if f.name == BRIEF_NAME or f.suffix.lower() not in INDEXABLE:
            continue
        out.append(f)
    return out


def _excerpt(f: Path, limit: int = 3000) -> str:
    """The first few thousand characters of a file, enough for a one-line summary."""
    if f.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(f))
            text = ""
            for page in reader.pages[:4]:
                text += (page.extract_text() or "") + "\n"
                if len(text) > limit:
                    break
            return text[:limit]
        except Exception:
            return ""
    return ai_config.extract_text(f)[:limit]


def update_materials_index(course_folder: Path, abbrev: str, dry_run: bool = False) -> list:
    """
    One line per file in the materials folder. New or changed files are
    summarised in a single Claude call; everything else comes from the cache.
    Returns the index lines.
    """
    idx_path = _index_path(course_folder)
    idx = _load_json(idx_path)
    files = _materials_files(course_folder)
    mdir = canvas_common.materials_dir(course_folder)

    todo = []
    for f in files:
        rel = str(f.relative_to(mdir))
        digest = canvas_common.file_md5(f)
        if idx.get(rel, {}).get("md5") != digest:
            todo.append((rel, digest, f))

    if todo and not dry_run:
        prompt_parts = []
        for rel, digest, f in todo[:40]:
            prompt_parts.append(f"=== FILE: {rel} ===\n{_excerpt(f)}")
        instruction = ("For each file below, write ONE line: `<file>: <what it is and what "
                       "it is useful for in this course, ≤ 25 words>`. Output only those "
                       "lines, one per file, same order, nothing else.")
        try:
            text, _ = _ask(mdir, "You index course materials for an MBA student.",
                           instruction, "\n\n".join(prompt_parts))
            summaries = {}
            for line in text.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    summaries[k.strip().lstrip("-•* ").strip("`").strip()] = v.strip()
            for rel, digest, f in todo[:40]:
                idx[rel] = {"md5": digest, "summary": summaries.get(rel, ""),
                            "indexed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        except notes_backend.NotesError as e:
            print(f"    (materials index not updated: {e})")

    # Drop entries for files that are gone.
    present = {str(f.relative_to(mdir)) for f in files}
    for k in list(idx):
        if k not in present:
            del idx[k]
    if not dry_run:
        _save_json(idx_path, idx)

    lines = []
    for f in files:
        rel = str(f.relative_to(mdir))
        summary = idx.get(rel, {}).get("summary") or "(not yet summarised)"
        lines.append(f"- `{rel}`: {summary}")
    return lines


# ── Class blocks ──────────────────────────────────────────────────────────────

def _session_dirs(course_folder: Path) -> list:
    """[(date_str, path)] of every class-day folder, oldest first."""
    if not course_folder.exists():
        return []
    out = []
    for d in course_folder.iterdir():
        if canvas_common.is_session_dir(d):
            out.append((canvas_common.session_date(d.name), d))
    return sorted(out)


def _notes_md(session_dir: Path) -> "Path | None":
    """The Markdown twin of the notes, or a .md cheat sheet the user made."""
    if not session_dir.exists():
        return None
    for f in sorted(session_dir.iterdir()):       # includes the hidden ".Cheat Sheet - X.md" twin
        if f.is_file() and f.suffix.lower() == ".md" and canvas_common.is_notes_file(f.name):
            return f
    return None


def notes_text(session_dir: Path) -> "tuple[str, str] | None":
    """
    (name, text) of the class's notes: the Markdown twin when there is one,
    else the text of a Word cheat sheet. Adopted folders only have the .docx,
    and handing the model a filename it cannot read produced a complaint
    where a brief entry should have been.
    """
    md = _notes_md(session_dir)
    if md:
        return md.name, md.read_text(errors="replace")
    for f in sorted(session_dir.glob("*.docx")):
        if canvas_common.is_notes_file(f.name) and not f.name.startswith("~$"):
            text = ai_config.extract_text(f)
            if text.strip():
                return f.name, text
    return None


def _post_class_files(session_dir: Path) -> list:
    """Files that were not part of the notes' readings: wrap-ups, announcements."""
    meta = _load_json(session_dir / ".notes_meta.json")
    known = set(meta.get("readings", {})) | {meta.get("notes_file", "")}
    out = []
    for f in sorted(session_dir.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.name.startswith("~$"):
            continue
        if canvas_common.is_notes_file(f.name) or f.suffix.lower() == ".m4a":
            continue
        if f.name in known or "(skipped)" in f.name:
            continue
        out.append(f)
    return out


def bottom_lines(md_text: str, limit: int = 4000) -> str:
    """
    The skimmable spine of a cheat sheet: headings, 20-second answers,
    syntheses, contrarian lines. Enough to carry the class forward without
    the whole document.
    """
    keep = []
    for line in md_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") or re.match(r"\*\*(the 20-second answer|my bottom line|discussion-ready synthesis|if the room)", s, re.I) \
                or re.match(r"\*\*[A-Z]\.\s", s):
            keep.append(s)
    text = "\n".join(keep)
    return text[:limit]


def _class_inputs(session_dir: Path, date_str: str) -> "tuple[str, list, str]":
    """(context text, files the model should Read, fingerprint) for one class."""
    parts = []
    on_disk = []
    fp = [date_str]
    notes = notes_text(session_dir)
    if notes:
        name, text = notes
        # A Markdown sheet has headings to skim; extracted Word text does not,
        # so take its opening instead.
        spine = bottom_lines(text, 6000) if name.endswith(".md") else text[:8000]
        parts.append(f"=== CHEAT SHEET ({name}) — the student's own analysis ===\n{spine}")
        fp.append(hashlib.md5(text.encode()).hexdigest())
    post_class = _post_class_files(session_dir)
    for f in post_class:
        fp.append(f.name + canvas_common.file_md5(f))
        if f.suffix.lower() == ".pdf":
            on_disk.append(f)
        else:
            parts.append(f"=== POST-CLASS: {f.name} ===\n{ai_config.extract_text(f)[:20000]}")
    post_names = {f.name for f in post_class}
    readings = [f for f in sorted(session_dir.iterdir())
                if f.is_file() and f.suffix.lower() in {".pdf", ".docx", ".pptx"}
                and not canvas_common.is_notes_file(f.name) and not f.name.startswith("~$")
                and f.name not in post_names]        # a wrap-up is not a reading
    if readings:
        parts.append("=== READINGS THAT DAY ===\n" + "\n".join(f"- {r.name}" for r in readings))
        # The Read tool handles PDFs; Word and PowerPoint arrive extracted.
        for f in readings:
            if f.suffix.lower() in (".docx", ".pptx"):
                text = ai_config.extract_text(f)
                if text.strip():
                    parts.append(f"=== READING: {f.name} ===\n{text[:12000]}")
        pdfs = [f for f in readings if f.suffix.lower() == ".pdf"]
        if pdfs:
            on_disk = pdfs + on_disk
    if on_disk:
        parts.append("=== FILES ON DISK (read with the Read tool; PDFs in ≤20-page chunks) ===\n"
                     + "\n".join(f"- {f.name}" for f in on_disk))
    return "\n\n".join(parts), on_disk, hashlib.md5("|".join(fp).encode()).hexdigest()[:12]


BLOCK_INSTRUCTION = """Write the course-brief entry for this class, ≤ {words} words, in this exact shape:

### Class {label}
**Lenses and frameworks introduced:** named frameworks/mental models, each with one line on when to use it and where it came from (case, note, wrap-up, professor).
**Key takeaways:** 3–5 bullets, the conclusions the class actually reached (use the wrap-up over the cheat sheet where they differ).
**Numbers worth remembering:** 1–3 bullets, if any.
**Threads to carry forward:** 1–3 bullets: open questions or "we'll return to this".
**Glossary:** `term — definition`, one per line, only terms new to this course.

Output only that Markdown, nothing before or after."""


def _ask(cwd: Path, system: str, instruction: str, context: str) -> "tuple[str, dict]":
    model = notes_backend._setting("BRIEF_MODEL", "claude-sonnet-5")
    return notes_backend.generate_with_claude_code(cwd, system, instruction, context, model)


def update_class_block(course_folder: Path, session_dir: Path, date_str: str,
                       brief_text: str, state: dict, force: bool = False) -> str:
    """Write/refresh the block for one past class if its inputs changed."""
    context, on_disk, fingerprint = _class_inputs(session_dir, date_str)
    key = f"class-{date_str}"
    if not force and state.get("classes", {}).get(date_str) == fingerprint and key in blocks(brief_text):
        return brief_text
    if "=== CHEAT SHEET" not in context and "=== READING" not in context \
            and "=== POST-CLASS" not in context and not on_disk:
        # An empty folder (a syllabus day, a class whose files never synced):
        # there is nothing to distil, and asking anyway yields a complaint.
        return brief_text
    label = session_dir.name[7:] or date_str
    label = re.sub(r"^(?:class|session)\s+", "", label, flags=re.IGNORECASE)
    system = ("You maintain the running knowledge base ('course brief') of one course for an HBS "
              "MBA student: what each class taught, in a form that can be applied to later cases.")
    try:
        text, _ = _ask(session_dir, system,
                       BLOCK_INSTRUCTION.format(words=MAX_BLOCK_WORDS, label=label), context)
    except notes_backend.NotesError as e:
        print(f"    ✗ {session_dir.name}: brief entry not written ({e})")
        return brief_text
    text = tidy_entry(text.strip(), label)
    if not looks_like_entry(text):
        # The model asked a question or explained why it could not proceed.
        # Leave the old block (if any) and the fingerprint alone so it is
        # retried next run, rather than filing the complaint as course memory.
        print(f"    ✗ {session_dir.name}: response was not a brief entry — skipped "
              f"({text[:80]!r})")
        return brief_text
    state.setdefault("classes", {})[date_str] = fingerprint
    print(f"    + brief entry: {session_dir.name}")
    return upsert_block(brief_text, key, text)


_ENTRY_MARKERS = ("**Lenses and frameworks", "**Key takeaways", "**Threads to carry forward")


def tidy_entry(text: str, label: str) -> str:
    """
    The entry as it should be filed: from its first heading onward (the model
    sometimes narrates — "here's the final entry:" — before it), one heading
    only, and a heading at all.
    """
    i = text.find("### ")
    if i > 0:
        text = text[i:]
    if not text.startswith("### "):
        text = f"### Class {label}\n" + text
    lines = text.splitlines()
    head = lines[0]
    body = [l for l in lines[1:] if l.strip() != head.strip()]   # drop a repeated heading
    return "\n".join([head] + body).strip()


def looks_like_entry(text: str) -> bool:
    """A brief entry has the requested bold section labels; a complaint does not."""
    return sum(m in text for m in _ENTRY_MARKERS) >= 2


def _roll_up_threads(brief_text: str) -> str:
    """Collect every 'Threads to carry forward' bullet from the class blocks."""
    threads = []
    for key, body in blocks(brief_text).items():
        if not key.startswith("class-"):
            continue
        m = re.search(r"\*\*Threads to carry forward:\*\*(.*?)(?=\n\*\*|\Z)", body, re.DOTALL)
        if m:
            label = re.search(r"### (.+)", body)
            tag = f" _({label.group(1).strip()})_" if label else ""
            for line in m.group(1).splitlines():
                line = line.strip()
                if line.startswith(("-", "•", "*")):
                    threads.append(f"{line.rstrip()}{tag}")
    body = "\n".join(threads) if threads else "_(none yet)_"
    return upsert_block(brief_text, "threads", body)


def _about(course_folder: Path, abbrev: str, full_name: str, brief_text: str,
           state: dict) -> str:
    """Fill the About section once, from a syllabus if one is in the materials."""
    if state.get("about_done"):
        return brief_text
    syllabus = next((f for f in _materials_files(course_folder)
                     if re.search(r"syllab|course (overview|outline|description)", f.name, re.I)), None)
    if syllabus is None:
        return brief_text
    context = f"=== SYLLABUS: {syllabus.name} ===\n{_excerpt(syllabus, 12000)}"
    try:
        text, _ = _ask(course_folder, "You summarise a course syllabus for the student taking it.",
                       "In ≤ 200 words of Markdown: what the course is about, the professor, how it is "
                       "graded and what participation counts for, the arc of the term, and any rules "
                       "about AI or collaboration. Output only the Markdown.", context)
    except notes_backend.NotesError as e:
        print(f"    (about section not written: {e})")
        return brief_text
    state["about_done"] = True
    return upsert_block(brief_text, "about", text.strip())


def _has_inputs(session_dir: Path, date_str: str) -> bool:
    context, on_disk, _ = _class_inputs(session_dir, date_str)
    return bool(on_disk) or any(k in context for k in ("=== CHEAT SHEET", "=== READING", "=== POST-CLASS"))


def prune_stale_blocks(course_folder: Path, text: str, state: dict) -> str:
    """
    Drop class blocks whose folder is gone or holds nothing to distil — a
    syllabus day that never had files, a folder the user removed — so the
    brief only remembers classes that exist. The threads roll-up follows.
    """
    dirs = {ds: d for ds, d in _session_dirs(course_folder)}
    removed = False
    for key in list(blocks(text)):
        if not key.startswith("class-"):
            continue
        ds = key.split("-", 1)[1]
        d = dirs.get(ds)
        if d is None or not _has_inputs(d, ds):
            text = re.sub(rf"<!-- auto:{re.escape(key)} -->\n?.*?<!-- /auto:{re.escape(key)} -->\n?",
                          "", text, count=1, flags=re.DOTALL)
            state.get("classes", {}).pop(ds, None)
            print(f"    – dropped brief entry for {ds}: nothing on disk to base it on")
            removed = True
    return _roll_up_threads(text) if removed else text


def update_course(abbrev: str, info: dict, bootstrap: bool = False,
                  force: bool = False, today=None) -> None:
    course_folder = info.get("folder_path")
    if not course_folder or not course_folder.exists():
        return
    full_name = info.get("full_name", abbrev)
    p = ensure_brief(course_folder, abbrev, full_name)
    text = p.read_text()
    state = _load_json(_state_path(course_folder))
    today = today or datetime.now(timezone.utc).date()

    text = _about(course_folder, abbrev, full_name, text, state)
    text = prune_stale_blocks(course_folder, text, state)

    lines = update_materials_index(course_folder, abbrev)
    text = upsert_block(text, "materials", "\n".join(lines) if lines else "_(no files yet)_")

    past = []
    for date_str, d in _session_dirs(course_folder):
        try:
            from datetime import date as _date
            when = _date(int("20" + date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
        except ValueError:
            continue
        if when < today:
            past.append((date_str, d))
    if past:
        text = upsert_block(text, "lenses-index",
                            "\n".join(f"- {d.name}" for _, d in past))
    for date_str, d in past:
        text = update_class_block(course_folder, d, date_str, text, state, force=force)
    text = _roll_up_threads(text)

    p.write_text(text)
    _save_json(_state_path(course_folder), state)


def update_all(courses: dict, active_only: bool = True, only: "str | None" = None,
               bootstrap: bool = False, force: bool = False) -> None:
    for abbrev, info in sorted(courses.items()):
        if only and abbrev != only:
            continue
        if active_only and not path_config.is_active(info):
            continue
        if not info.get("folder_path"):
            continue
        print(f"  {abbrev}:")
        try:
            update_course(abbrev, info, bootstrap=bootstrap, force=force)
        except Exception as e:
            print(f"    ✗ brief update failed: {e}")


# ── Context for the cheat sheet of one class ─────────────────────────────────

def _next_posting(session: dict, date_str: str) -> "dict | None":
    """The next class posting after date_str (title, questions, reading titles)."""
    try:
        import canvas_refresh as cr
        upcoming = []
        for a in cr.canvas_get(f"courses/{session['course_id']}/assignments", {"per_page": 100}):
            if not a.get("due_at"):
                continue
            if not cr._kind_matches(cr.posting_kind(a), cr.SESSION_KINDS):
                continue
            ds = cr.yymmdd(cr.boston_date(a["due_at"]))
            if ds > date_str:
                upcoming.append((ds, a))
        if not upcoming:
            return None
        ds, a = min(upcoming, key=lambda t: t[0])
        import canvas_readings
        links = [l["title"] for l in canvas_readings.extract_links(a.get("description") or "")]
        return {"date": ds, "name": a.get("name", ""),
                "questions": cr.strip_html(a.get("description") or "")[:1500],
                "readings": links}
    except (Exception, SystemExit):     # canvas_get exits when no token is configured
        return None


def context_for(session: dict, session_dir: Path) -> str:
    """
    The course-level blocks appended to a cheat-sheet prompt:
    COURSE BRIEF, PREVIOUS CLASS, NEXT CLASS (peek), MATERIALS INDEX.
    """
    course_folder = session_dir.parent
    date_str = session["date_str"]
    parts = []

    bp = brief_path(course_folder)
    if bp.exists():
        text = bp.read_text(errors="replace")
        # The materials index is presented separately, with paths.
        text = re.sub(r"<!-- auto:materials -->.*?<!-- /auto:materials -->", "", text, flags=re.DOTALL)
        parts.append(f"=== COURSE BRIEF ===\n{text.strip()}")

    earlier = [(ds, d) for ds, d in _session_dirs(course_folder) if ds < date_str]
    if earlier:
        prev_ds, prev = earlier[-1]
        chunks = [f"Folder: {prev.name}"]
        md = _notes_md(prev)
        if md:
            chunks.append("Bottom lines from its cheat sheet:\n" + bottom_lines(md.read_text(errors="replace")))
        post = _post_class_files(prev)
        if post:
            chunks.append("Posted after that class (read the PDFs with the Read tool if useful):\n"
                          + "\n".join(f"- {prev.name}/{f.name}" for f in post))
            for f in post:
                if f.suffix.lower() in (".md", ".txt"):
                    chunks.append(f"--- {f.name} ---\n{f.read_text(errors='replace')[:6000]}")
        parts.append("=== PREVIOUS CLASS ===\n" + "\n\n".join(chunks))

    nxt = _next_posting(session, date_str)
    if nxt:
        parts.append("=== NEXT CLASS (peek) ===\n"
                     f"{nxt['date']}: {nxt['name']}\n"
                     + (f"Readings: {'; '.join(nxt['readings'])}\n" if nxt["readings"] else "")
                     + f"Posting: {nxt['questions']}")

    mdir = canvas_common.materials_dir(course_folder)
    idx = _load_json(_index_path(course_folder))
    if idx:
        lines = [f"- `{mdir.name}/{rel}`: {v.get('summary') or ''}".rstrip(": ")
                 for rel, v in sorted(idx.items())]
        parts.append("=== MATERIALS INDEX (course-level files; Read any that is relevant) ===\n"
                     + "\n".join(lines))

    return ("\n\n".join(parts) + "\n") if parts else ""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bootstrap", action="store_true",
                    help="First fill: every past class of every course (same as default, "
                         "but also courses whose term has ended)")
    ap.add_argument("--course", metavar="ABBREV", help="Only this course")
    ap.add_argument("--force", action="store_true", help="Rewrite class blocks even if unchanged")
    args = ap.parse_args()
    paths = path_config.resolve()
    update_all(paths["courses"], active_only=not args.bootstrap, only=args.course,
               bootstrap=args.bootstrap, force=args.force)


if __name__ == "__main__":
    main()
