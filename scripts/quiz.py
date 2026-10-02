#!/usr/bin/env python3
"""
quiz.py — a study guide, a review podcast and the key materials for each quiz.

A quiz is announced only in a class title ("Viking Investment Debrief + QUIZ 3").
When one is coming this builds, in the course folder:

    Quiz 3/
      Negotiation Quiz 3 Study Guide.docx
      Quiz 3 Podcast.m4a
      1 Moms.com (Sep 25)/…            the classes since the previous quiz:
      2 Honoring the Contract (Sep 30)/…   decks, readings, role sheets, cheat sheets
      Earlier/…                        the previous quiz's study guide

Scope: the classes from the previous quiz day (its own content was taught
after that quiz was taken) up to the day before this quiz. Earlier classes are
given to the model as "still fair game", with what has already been tested.

The professor's post-class decks live in Canvas folders named by class order
("7_8_Moms.com"); they are matched to classes by those numbers and arrive
days late, so the folder is rebuilt whenever its inputs change, until the
quiz date. A Quiz folder you made yourself is never touched.

    python3 scripts/quiz.py                 # build whatever is due in the next week
    python3 scripts/quiz.py NEG 3 --force   # build one quiz, even a past one
    python3 scripts/quiz.py NEG 3 --podcast # …and its podcast
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import canvas_common
import path_config

QUIZ_RE = re.compile(r"\bquiz\s*#?\s*(\d+)?", re.IGNORECASE)
HORIZON_DAYS = 7
MATERIAL_EXTS = {".pdf", ".docx", ".pptx", ".ppt", ".doc", ".xlsx", ".xls"}
META = ".quiz_meta.json"
_CASE_NOISE_RE = re.compile(
    r"\+\s*quiz\s*#?\s*\d*|\b(debrief|negotiation|day\s*\d+|part\s*\d+)\b|\b(i{1,3}|iv|v)\b\s*$", re.IGNORECASE)


# ── Class days, quizzes, scope ────────────────────────────────────────────────

def class_days(cr, course_id: int) -> list:
    """Every class day of a course in date order, with each posting's ordinal (1 = first class)."""
    posts = [a for a in cr.canvas_get(f"courses/{course_id}/assignments", {"per_page": 100})
             if a.get("due_at") and cr._kind_matches(cr.posting_kind(a), cr.SESSION_KINDS)]
    posts.sort(key=lambda a: (a["due_at"], a.get("id", 0)))
    days: dict = {}
    for ordinal, a in enumerate(posts, 1):
        dt = cr.boston_date(a["due_at"])
        d = days.setdefault(cr.yymmdd(dt), {"date_str": cr.yymmdd(dt), "due": dt, "postings": [], "ordinals": []})
        d["postings"].append(a)
        d["ordinals"].append(ordinal)
    return [days[k] for k in sorted(days)]


def find_quizzes(days: list, pattern: "re.Pattern | None" = None) -> list:
    """[{n, index, date_str, due}] for every class day that announces a quiz."""
    pattern = pattern or QUIZ_RE
    out, seq = [], 0
    for i, d in enumerate(days):
        m = None
        for a in d["postings"]:
            m = pattern.search(a.get("name") or "") or m
        if not m:
            continue
        seq += 1
        n = int(m.group(1)) if m.groups() and m.group(1) else seq
        seq = max(seq, n)
        out.append({"n": n, "index": i, "date_str": d["date_str"], "due": d["due"]})
    return out


def scope_for(days: list, quizzes: list, n: int) -> "tuple[list, list]":
    """(classes this quiz covers, earlier classes). Raises KeyError for an unknown quiz."""
    q = next(x for x in quizzes if x["n"] == n) if any(x["n"] == n for x in quizzes) else None
    if q is None:
        raise KeyError(n)
    earlier_q = [x for x in quizzes if x["index"] < q["index"]]
    start = max(x["index"] for x in earlier_q) if earlier_q else 0
    return days[start:q["index"]], days[:start]


def case_key(title: str) -> str:
    t = canvas_common.extract_case_title(title or "")
    prev = None
    while prev != t:
        prev, t = t, _CASE_NOISE_RE.sub(" ", t).strip(" -–—:+")
    return re.sub(r"\s+", " ", t).strip()


def group_days(days: list) -> list:
    """Consecutive days of the same case become one group: [{title, days, ordinals}]."""
    groups: list = []
    for d in days:
        title = case_key(canvas_common.session_title(d["postings"]) or d["postings"][0].get("name", ""))
        key = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()[:2]
        if groups and groups[-1]["key"] == key:
            groups[-1]["days"].append(d)
            groups[-1]["ordinals"] += d["ordinals"]
        else:
            groups.append({"key": key, "title": title or d["date_str"], "days": [d], "ordinals": list(d["ordinals"])})
    return groups


def group_label(i: int, g: dict) -> str:
    first, last = g["days"][0]["due"], g["days"][-1]["due"]
    when = first.strftime("%b %-d") if first.date() == last.date() else (
        f"{first.strftime('%b %-d')}-{last.strftime('%-d')}" if first.month == last.month
        else f"{first.strftime('%b %-d')}-{last.strftime('%b %-d')}")
    return canvas_common.safe_name(f"{i} {g['title']} ({when})")


def deck_map(cr, course_id: int) -> dict:
    """{file name: {class ordinals}} for files in Canvas folders named like '7_8_Moms.com'."""
    folders = {}
    for f in cr.canvas_get(f"courses/{course_id}/folders", {"per_page": 100}):
        m = re.match(r"^((?:\d+_)+)", f.get("name") or "")
        if m:
            folders[f["id"]] = {int(x) for x in m.group(1).strip("_").split("_")}
    out: dict = {}
    for f in cr.canvas_get(f"courses/{course_id}/files", {"per_page": 100}):
        if f.get("folder_id") in folders:
            out.setdefault(canvas_common.safe_name(f.get("display_name") or ""), set()).update(folders[f["folder_id"]])
    return out


# ── The folder ────────────────────────────────────────────────────────────────

def quiz_dir(course_folder: Path, n: int) -> Path:
    return course_folder / f"Quiz {n}"


def owned(folder: Path) -> bool:
    """We write only into a Quiz folder we made, or one that does not exist yet."""
    return not folder.exists() or (folder / META).exists()


def _materials(session_dir: "Path | None") -> list:
    if not session_dir or not session_dir.exists():
        return []
    return sorted(f for f in session_dir.iterdir()
                  if f.is_file() and not f.name.startswith((".", "~$"))
                  and f.suffix.lower() in MATERIAL_EXTS
                  and not canvas_common.is_generated_file(f.name) and "(skipped)" not in f.name)


def plan_files(course_folder: Path, groups: list, decks: dict) -> list:
    """[(source path, 'group label/file name', is_deck)] for everything the folder should hold."""
    shelf = canvas_common.materials_dir(course_folder)
    plan, digests = [], set()
    for i, g in enumerate(groups, 1):
        label, seen = group_label(i, g), set()
        for d in g["days"]:
            for f in _materials(canvas_common.find_session_dir(course_folder, d["date_str"])):
                digest = canvas_common.file_md5(f)
                if f.name not in seen and digest not in digests:
                    seen.add(f.name); digests.add(digest)
                    plan.append((f, f"{label}/{f.name}", f.name in decks))
        for name, ordinals in sorted(decks.items()):
            src = shelf / name
            if ordinals & set(g["ordinals"]) and src.exists() and name not in seen:
                seen.add(name)
                plan.append((src, f"{label}/{name}", True))
    return plan


def earlier_guides(course_folder: Path, n: int) -> list:
    """[(quiz number, name, markdown text, docx path or None)] for earlier quizzes' study guides."""
    out = []
    for k in range(1, n):
        d = quiz_dir(course_folder, k)
        if not d.exists():
            continue
        mds = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".md" and "stud" in f.name.lower()]
        docx = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".docx"
                and "stud" in f.name.lower() and not f.name.startswith("~$")]
        if mds:
            out.append((k, mds[0].name.lstrip("."), mds[0].read_text(errors="replace"), docx[0] if docx else None))
        elif docx:
            import ai_config
            out.append((k, docx[0].name, ai_config.extract_text(docx[0]), docx[0]))
    return out


def guide_name(course_name: str, n: int) -> str:
    short = re.split(r"[:(]", course_name)[0].strip()
    short = short if len(short) <= 40 else " ".join(short.split()[:3])
    return canvas_common.safe_name(f"{short} Quiz {n} Study Guide")


def guide_prompt(abbrev: str) -> str:
    base = (path_config.PROMPTS_DIR / "quiz_guide_prompt.md").read_text()
    ref = path_config.PROMPTS_DIR / f"quiz_guide_prompt_{abbrev.replace(' ', '_')}_refinement.md"
    notes = ""
    if ref.exists():
        notes = re.sub(r"<!--.*?-->", "", ref.read_text(), flags=re.DOTALL).strip()
    if notes and notes != "# CLASS-SPECIFIC NOTES":
        return re.sub(r"\[CLASS-SPECIFIC NOTES\].*", "[CLASS-SPECIFIC NOTES]\n" + notes, base, flags=re.DOTALL)
    return re.sub(r"\n*\[CLASS-SPECIFIC NOTES\].*", "", base, flags=re.DOTALL).strip() + "\n"


def fingerprint(plan: list, prompt: str, guides: list) -> str:
    h = hashlib.md5()
    for src, rel, _ in sorted(plan, key=lambda x: x[1]):
        h.update(rel.encode()); h.update(canvas_common.file_md5(src).encode())
    h.update(prompt.encode())
    for k, name, text, _ in guides:
        h.update(f"{k}:{len(text)}".encode())
    return h.hexdigest()[:12]


def _read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / META).read_text())
    except Exception:
        return {}


def _write_meta(folder: Path, meta: dict) -> None:
    (folder / META).write_text(json.dumps(meta, indent=2, sort_keys=True))


def _context(cr, abbrev, course_name, n, quiz, course_folder, groups, earlier, plan, decks, guides) -> str:
    import course_brief
    qdir = quiz_dir(course_folder, n)
    parts = [f"=== QUIZ ===\nCourse: {course_name}\nQuiz {n}, in class on {quiz['due'].strftime('%A %B %-d, %Y')}."]
    lines = []
    for i, g in enumerate(groups, 1):
        lines.append(f"- {group_label(i, g)}: " + "; ".join(
            canvas_common.session_title(d["postings"]) or d["date_str"] for d in g["days"]))
    parts.append("=== IN SCOPE (NEW tier) ===\n" + ("\n".join(lines) or "(no classes found in scope)"))
    parts.append("=== EARLIER CLASSES (may reappear) ===\n" + ("\n".join(
        f"- {d['due'].strftime('%b %-d')}: {canvas_common.session_title(d['postings'])}" for d in earlier) or "(none)"))

    files = []
    for src, rel, is_deck in plan:
        if src.suffix.lower() == ".pdf":
            files.append(f"- {(qdir / rel).relative_to(course_folder)} ({cr.pdf_page_count(src)} pages)"
                         f"{' — DECK, in scope: read every page' if is_deck else ' — in scope'}")
    shelf = canvas_common.materials_dir(course_folder)
    earlier_ord = {o for d in earlier for o in d["ordinals"]}
    for name, ordinals in sorted(decks.items()):
        p = shelf / name
        if ordinals & earlier_ord and p.exists() and p.suffix.lower() == ".pdf":
            files.append(f"- {p.relative_to(course_folder)} ({cr.pdf_page_count(p)} pages) — earlier deck: "
                         f"skim for 'Quiz answers' slides and untested terms")
    parts.append("=== FILES TO READ (relative to the working directory; use the Read tool) ===\n"
                 + ("\n".join(files) or "(no PDFs)"))

    for g in groups:
        for d in g["days"]:
            sdir = canvas_common.find_session_dir(course_folder, d["date_str"])
            got = course_brief.notes_text(sdir) if sdir else None
            if got:
                parts.append(f"=== CHEAT SHEET: {canvas_common.session_title(d['postings'])} ===\n{got[1][:25000]}")
    for k, name, text, _ in guides:
        parts.append(f"=== PREVIOUS STUDY GUIDE: Quiz {k} ({name}) ===\n{text[:60000]}")
    brief = course_brief.brief_path(course_folder)
    if brief.exists():
        parts.append("=== COURSE BRIEF ===\n" + brief.read_text(errors="replace")[:12000])
    return "\n\n".join(parts)


def _ask(cr, cwd: Path, system: str, instruction: str, context: str) -> str:
    import notes_backend
    return notes_backend.generate_with_claude_code(cwd, system, instruction, context,
                                                   notes_backend.model(cr.cfg), cr.cfg)[0]


# ── Build ─────────────────────────────────────────────────────────────────────

def build(abbrev: str, n: int, force: bool = False, dry_run: bool = False,
          sync: bool = False) -> "Path | None":
    """Make or refresh `Quiz n` for a course. Returns the study guide path when it exists."""
    import canvas_refresh as cr
    info = cr._COURSES.get(abbrev) or {}
    course_folder, course_id = info.get("folder_path"), info.get("canvas_id")
    if not course_folder or not course_id:
        print(f"  ⚠ {abbrev}: unknown course")
        return None
    course_name = info.get("full_name") or abbrev
    days = class_days(cr, course_id)
    quizzes = find_quizzes(days)
    try:
        in_scope, earlier = scope_for(days, quizzes, n)
    except KeyError:
        print(f"  ⚠ {abbrev}: Canvas shows no Quiz {n} (found: {[q['n'] for q in quizzes] or 'none'})")
        return None
    quiz = next(q for q in quizzes if q["n"] == n)
    qdir = quiz_dir(course_folder, n)
    if not owned(qdir):
        print(f"  – {abbrev} Quiz {n}: '{qdir.name}' is a folder you made yourself; leaving it alone")
        return None
    past = quiz["due"].date() < cr._today()
    if past and not force:
        return None

    groups = group_days(in_scope)
    if sync and not dry_run:
        try:                                   # a deck posted this morning must be on the shelf first
            cr.sync_course_files(course_id, abbrev, target_date_str=None)
        except Exception as e:
            print(f"    ⚠ file sync before the quiz build failed: {e}")
    decks = deck_map(cr, course_id)
    plan = plan_files(course_folder, groups, decks)
    guides = earlier_guides(course_folder, n)
    prompt = guide_prompt(abbrev)
    fp = fingerprint(plan, prompt, guides)
    name = guide_name(course_name, n)
    docx, md = qdir / f"{name}.docx", qdir / f".{name}.md"
    meta = _read_meta(qdir)
    print(f"\n  [{abbrev} Quiz {n}] {quiz['due'].strftime('%a %b %-d')} — "
          f"{len(groups)} case(s), {len(plan)} file(s), {sum(1 for p in plan if p[2])} deck(s)")
    if docx.exists() and meta.get("fingerprint") == fp and not force:
        print("    ✓ Study guide up to date")
        return docx
    if dry_run:
        for _, rel, is_deck in plan:
            print(f"    would copy {'[deck] ' if is_deck else ''}{rel}")
        print(f"    would write {docx.name}")
        return None

    qdir.mkdir(parents=True, exist_ok=True)
    if not (qdir / META).exists():
        _write_meta(qdir, {"quiz": n, "course": abbrev})          # claim the folder before filling it
    for src, rel, _ in plan:
        dest = qdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or not canvas_common.same_content(src, dest):
            shutil.copy2(src, dest)
    if guides and guides[-1][3] is not None:
        (qdir / "Earlier").mkdir(exist_ok=True)
        dest = qdir / "Earlier" / guides[-1][3].name
        if not dest.exists():
            shutil.copy2(guides[-1][3], dest)

    if cr._NOTES_BLOCKED:
        print(f"    – study guide skipped: {cr._NOTES_BLOCKED}")
        return docx if docx.exists() else None
    import notes_backend
    context = _context(cr, abbrev, course_name, n, quiz, course_folder, groups, earlier, plan, decks, guides)
    print(f"    Writing the study guide ({notes_backend.model(cr.cfg)})...", flush=True)
    try:
        text = _ask(cr, course_folder, prompt,
                    f"Write the Quiz {n} study guide for {course_name}, following the system prompt exactly. "
                    "Read every in-scope deck in full before writing. Output only the final Markdown.", context)
    except notes_backend.NotesRateLimited as e:
        cr._block_notes(f"Claude usage limit reached ({str(e)[:80]}) — the quiz guide waits for the next run")
        return docx if docx.exists() else None
    except notes_backend.NotesError as e:
        print(f"    ✗ Study guide not generated: {e}")
        return docx if docx.exists() else None

    generated = datetime.now(tz=cr.BOSTON).strftime("%Y-%m-%d %H:%M")
    metadata = {"Generated": generated, "Course": course_name,
                "Quiz": f"{n}, {quiz['due'].strftime('%A %B %-d')}",
                "Covers": "; ".join(g["title"] for g in groups)}
    title = f"{name}"
    cr.write_markdown(md, title, metadata, text)
    cr.markdown_to_docx(md_text=text, output_path=docx, title=title, metadata=metadata)
    meta = _read_meta(qdir)
    meta.update({"quiz": n, "course": abbrev, "date": quiz["date_str"], "fingerprint": fp, "generated": generated,
                 "guide": docx.name, "files": sorted(rel for _, rel, _ in plan)})
    _write_meta(qdir, meta)
    print(f"    ✅ Generated: {qdir.name}/{docx.name}")
    return docx


def podcast(abbrev: str, n: int, force: bool = False) -> "Path | None":
    """Review-and-drill episode for a quiz whose study guide exists."""
    import canvas_refresh as cr
    import podcast_gen as pg
    info = cr._COURSES.get(abbrev) or {}
    course_folder = info.get("folder_path")
    qdir = quiz_dir(course_folder, n) if course_folder else None
    if not qdir or not (qdir / META).exists():
        return None
    meta = _read_meta(qdir)
    md = qdir / ("." + Path(meta.get("guide", "")).stem + ".md")
    if not meta.get("guide") or not md.exists():
        return None
    guide = md.read_text(errors="replace")
    files, seen = [], set()
    for f in sorted(qdir.rglob("*.pdf")):
        if "Earlier" in f.parts or f.name.startswith("."):
            continue
        digest = canvas_common.file_md5(f)
        if digest in seen or cr.pdf_page_count(f) > cr.PDF_PAGE_LIMIT:
            continue
        seen.add(digest); files.append(f)
    base = (path_config.PROMPTS_DIR / "quiz_podcast_prompt.md").read_text()
    instructions = re.sub(r"\n*\[CLASS-SPECIFIC NOTES\].*", "", base, flags=re.DOTALL).strip()
    fp = pg.source_fingerprint(files, guide, [], instructions)
    out = qdir / f"Quiz {n} Podcast.m4a"
    label = f"{abbrev} Quiz {n}"
    if not (out.exists() and meta.get("podcast_sources") == fp and not force):
        if out.exists():
            print(f"  ↻ {abbrev} Quiz {n}: study guide or materials changed — rebuilding the podcast")
            out.unlink()
        print(f"\n  [{abbrev} Quiz {n} podcast] {len(files)} file(s) + the study guide")
        minutes = asyncio.run(pg._render(label, fp, files, None, [], abbrev, out, "",
                                         instructions=instructions, extra_texts=[("STUDY GUIDE", guide)]))
        if out.exists():
            meta = _read_meta(qdir)
            meta["podcast_sources"] = fp
            if minutes:
                meta["podcast_minutes"] = round(minutes, 1)
            _write_meta(qdir, meta)
    if not out.exists():
        return None

    # Flashcards live in the same NotebookLM notebook as the episode.
    cards = qdir / f"Quiz {n} Flashcards.html"
    card_prompt = (path_config.PROMPTS_DIR / "quiz_flashcards_prompt.md").read_text().strip()
    card_fp = hashlib.md5((fp + card_prompt).encode()).hexdigest()[:10]
    if force or not cards.exists() or _read_meta(qdir).get("flashcards_sources") != card_fp:
        try:
            if asyncio.run(pg.make_flashcards(pg.notebook_title(label, fp), card_prompt, cards)):
                meta = _read_meta(qdir)
                meta["flashcards_sources"] = card_fp
                _write_meta(qdir, meta)
        except Exception as e:
            print(f"  ⚠ flashcards failed: {e}")
    return out


def upcoming(cr, horizon_days: int = HORIZON_DAYS) -> list:
    """[(abbrev, quiz number)] for quizzes from today through the horizon."""
    today = cr._today()
    out = []
    for abbrev, course_id in cr.ACTIVE_COURSES.items():
        try:
            for q in find_quizzes(class_days(cr, course_id)):
                if today <= q["due"].date() <= today + timedelta(days=horizon_days):
                    out.append((abbrev, q["n"]))
        except Exception as e:
            print(f"  ⚠ {abbrev}: quiz check failed: {e}")
    return out


def run(with_podcast: bool = False, max_guides: int = 1) -> int:
    """Scheduled entry point: refresh the guide (and podcast) for each quiz in the next week."""
    import canvas_refresh as cr
    made = 0
    for abbrev, n in upcoming(cr):
        try:
            before = _read_meta(quiz_dir(cr._COURSES[abbrev]["folder_path"], n)).get("generated")
            if made < max_guides:
                build(abbrev, n)
                after = _read_meta(quiz_dir(cr._COURSES[abbrev]["folder_path"], n)).get("generated")
                made += int(after != before)
            if with_podcast:
                podcast(abbrev, n)
        except Exception as e:
            print(f"  ⚠ {abbrev} Quiz {n}: {e}")
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("course", nargs="?", help="Course abbreviation, e.g. NEG")
    ap.add_argument("number", nargs="?", type=int, help="Quiz number")
    ap.add_argument("--force", action="store_true", help="Rebuild even if up to date or past")
    ap.add_argument("--podcast", action="store_true", help="Also make the podcast")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be copied and written")
    args = ap.parse_args(argv)
    if args.course and args.number:
        build(args.course, args.number, force=args.force, dry_run=args.dry_run, sync=True)
        if args.podcast and not args.dry_run:
            podcast(args.course, args.number, force=args.force)
    else:
        run(with_podcast=args.podcast)
    return 0


if __name__ == "__main__":
    sys.exit(main())
