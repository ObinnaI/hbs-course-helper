#!/usr/bin/env python3
"""
migrate_folders.py — Adopt class folders made by hand into the tool's scheme.

Your "Class 5 - Pave" becomes "260916 Class 5 - Pave" (the date comes from
the matching Canvas posting), every file inside is kept, and an existing
"Cheat Sheet - *.docx" is recorded as that day's notes so nothing is
regenerated. Folders that are not class days — "Quiz 1", "Course Docs",
"Course Textbook and Materials", "RH" — are listed and left alone.

Dry run by default; nothing changes without --apply.

  python3 scripts/migrate_folders.py                    # report only
  python3 scripts/migrate_folders.py --apply
  python3 scripts/migrate_folders.py --root "~/Library/Mobile Documents/com~apple~CloudDocs/HBS/Classes/2026"
  python3 scripts/migrate_folders.py --map "Fall/Negotiations/Class 3 - Treu Pharma=260910"
  python3 scripts/migrate_folders.py --accept-fuzzy     # also apply title-only matches
  python3 scripts/migrate_folders.py --relabel          # rename already-dated folders whose label drifted

Run it against the iCloud folder FIRST, then import into the data-repo
clone: the mirror never deletes, so migrating the clone and letting it
sync back would leave iCloud with both names.
"""

import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="Coursework root to migrate (default: COURSEWORK_ROOT)")
    ap.add_argument("--course", metavar="ABBREV", help="Only this course")
    ap.add_argument("--apply", action="store_true", help="Actually rename (default: dry run)")
    ap.add_argument("--map", action="append", default=[], metavar="PATH=YYMMDD|ID",
                    help="Force a folder to a class day: relative path (or folder name) = date or Canvas assignment id")
    ap.add_argument("--accept-fuzzy", action="store_true",
                    help="Apply matches made on title similarity alone (no class number)")
    ap.add_argument("--relabel", action="store_true",
                    help="Also propose renames for dated folders whose label no longer matches Canvas")
    ap.add_argument("--force", action="store_true", help="Rewrite .notes_meta.json even if present")
    return ap.parse_args(argv)


def _import_scripts():
    sys.path.insert(0, str(Path(__file__).parent))
    import path_config, canvas_common, canvas_refresh   # noqa: E402
    return path_config, canvas_common, canvas_refresh


_TITLE_AFTER_CLASS = re.compile(r"^\s*(?:class|session)\s+\d+\s*[-–—:]?\s*(.*)$", re.IGNORECASE)


def _dir_title(name: str) -> str:
    m = _TITLE_AFTER_CLASS.match(name)
    return (m.group(1) if m else name).strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class Plan:
    """One proposed action for one folder."""
    def __init__(self, kind, course, src, dst=None, note="", posting=None, date_str=None):
        self.kind, self.course, self.src, self.dst = kind, course, src, dst
        self.note, self.posting, self.date_str = note, posting, date_str


def postings_for(cr, cc, course_id: int) -> list:
    """[(class number or None, date_str, posting)] for every class posting."""
    out = []
    for a in cr.canvas_get(f"courses/{course_id}/assignments", {"per_page": 100}):
        if not a.get("due_at"):
            continue
        if not cr._kind_matches(cr.posting_kind(a), cr.SESSION_KINDS):
            continue
        out.append((cc.class_number(a.get("name", "")), cr.yymmdd(cr.boston_date(a["due_at"])), a))
    return out


def plan_course(abbrev: str, info: dict, root: Path, postings: list, maps: dict,
                accept_fuzzy: bool, relabel: bool, cc) -> list:
    folder = info["folder_path"]
    plans = []
    if not folder or not folder.exists():
        return plans
    by_cn: dict = {}
    for cn, ds, a in postings:
        if cn is not None:
            by_cn.setdefault(cn, []).append((ds, a))
    by_id = {str(a.get("id")): (ds, a) for _, ds, a in postings}
    by_ds: dict = {}
    for _, ds, a in postings:
        by_ds.setdefault(ds, []).append(a)

    for d in sorted(folder.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        rel = str(d.relative_to(root))
        forced = maps.get(rel) or maps.get(d.name)

        if cc.is_session_dir(d) and not forced:
            if relabel:
                ds = cc.session_date(d.name)
                if ds in by_ds:
                    want = cc.session_dirname(ds, abbrev, by_ds[ds])
                    if want != d.name:
                        plans.append(Plan("RELABEL", abbrev, d, folder / want,
                                          "label drifted from Canvas", by_ds[ds][0], ds))
                        continue
            plans.append(Plan("DONE", abbrev, d, note="already in the scheme"))
            continue
        if cc.is_protected_dir(d.name) and not forced:
            plans.append(Plan("KEEP", abbrev, d, note="protected — not a class day"))
            continue

        match = None      # (date_str, posting, how)
        if forced:
            if forced in by_id:
                match = (*by_id[forced], "--map")
            elif forced in by_ds:
                match = (forced, by_ds[forced][0], "--map")
            elif re.fullmatch(r"\d{6}", forced):
                match = (forced, None, "--map (no posting that day)")
            else:
                plans.append(Plan("UNMATCHED", abbrev, d, note=f"--map value {forced!r} is neither a date nor an assignment id"))
                continue
        else:
            cn = cc.class_number(d.name)
            title = _dir_title(d.name)
            if cn is not None and cn in by_cn:
                cands = by_cn[cn]
                if len(cands) == 1:
                    match = (*cands[0], f"class {cn}")
                else:
                    scored = sorted(((_ratio(title, cc.extract_case_title(a.get("name", ""))), ds, a)
                                     for ds, a in cands), reverse=True)
                    if scored[0][0] >= 0.5:
                        match = (scored[0][1], scored[0][2], f"class {cn}, title {scored[0][0]:.2f}")
                    else:
                        opts = ", ".join(f'{ds} "{a.get("name","")[:40]}"' for _, ds, a in scored)
                        plans.append(Plan("AMBIGUOUS", abbrev, d,
                                          note=f"class {cn} posted more than once ({opts}); "
                                               f'add --map "{rel}=YYMMDD"'))
                        continue
            elif cn is not None:
                plans.append(Plan("UNMATCHED", abbrev, d,
                                  note=f'no "Class {cn}" posting in Canvas; add --map "{rel}=YYMMDD"'))
                continue
            else:
                scored = sorted(((_ratio(title, cc.extract_case_title(a.get("name", ""))), ds, a)
                                 for _, ds, a in postings), reverse=True)
                if scored and scored[0][0] >= 0.75:
                    how = f"title {scored[0][0]:.2f}"
                    if not accept_fuzzy:
                        plans.append(Plan("FUZZY", abbrev, d,
                                          folder / cc.session_dirname(scored[0][1], abbrev, [scored[0][2]]),
                                          f"{how} — apply with --accept-fuzzy", scored[0][2], scored[0][1]))
                        continue
                    match = (scored[0][1], scored[0][2], how)
                else:
                    plans.append(Plan("UNMATCHED", abbrev, d,
                                      note=f'no class number and no similar posting; add --map "{rel}=YYMMDD"'))
                    continue

        ds, posting, how = match
        target = folder / cc.session_dirname(ds, abbrev, [posting] if posting else [])
        if target.exists() and target != d:
            plans.append(Plan("CONFLICT", abbrev, d, target, f"{how}; target exists — not merging"))
            continue
        plans.append(Plan("RENAME", abbrev, d, target, how, posting, ds))
    return plans


def _extras(d: Path, cc) -> list:
    """Word/PowerPoint files that will count as readings (drafts you may want to move)."""
    return sorted(f.name for f in d.iterdir()
                  if f.is_file() and f.suffix.lower() in (".docx", ".pptx")
                  and not cc.is_notes_file(f.name) and not f.name.startswith("~$"))


def report(plans: list, root: Path, cc) -> str:
    lines = []
    by_course: dict = {}
    for p in plans:
        by_course.setdefault(p.course, []).append(p)
    for course, ps in by_course.items():
        lines.append(f"{course}")
        for p in ps:
            src = str(p.src.relative_to(root))
            if p.kind in ("RENAME", "RELABEL", "FUZZY", "CONFLICT"):
                lines.append(f"  {p.kind:<9} {src}")
                lines.append(f"            → {p.dst.name}   [{p.note}]")
                if p.kind in ("RENAME", "FUZZY"):
                    notes = cc.notes_paths(p.src, p.date_str or "000000", course).existing
                    lines.append(f"            notes: {notes.name if notes else '(none — will be generated)'}")
                    extra = _extras(p.src, cc)
                    if extra:
                        lines.append(f"            other .docx/.pptx will be treated as readings: {', '.join(extra)}")
            else:
                lines.append(f"  {p.kind:<9} {src}   [{p.note}]")
    counts = {}
    for p in plans:
        counts[p.kind] = counts.get(p.kind, 0) + 1
    lines.append("Summary: " + ", ".join(f"{v} {k.lower()}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


def write_md_twin(docx: "Path | None") -> None:
    """
    A Markdown copy beside an adopted Word cheat sheet, so the brief-writer,
    the podcast and the phone app can read it. Plain paragraphs only —
    python-docx does not carry heading levels through extract_text.
    """
    if docx is None or docx.suffix.lower() != ".docx":
        return
    twin = docx.parent / f".{docx.stem}.md"
    if twin.exists() or docx.with_suffix(".md").exists():
        return
    import ai_config
    text = ai_config.extract_text(docx)
    if text.strip():
        twin.write_text(f"# {docx.stem}\n\n_(text of {docx.name})_\n\n{text}\n")


def apply(plans: list, root: Path, cr, cc, path_config, force: bool = False) -> int:
    log_path = path_config.CONFIG_FILE.parent / "migrations.json"
    try:
        log = json.loads(log_path.read_text()) if log_path.exists() else []
    except Exception:
        log = []
    done = 0
    for p in plans:
        if p.kind not in ("RENAME", "RELABEL") and not (p.kind == "FUZZY" and False):
            continue
        if p.dst.exists():
            print(f"  ✗ {p.src.name}: target appeared — skipped")
            continue
        os.rename(p.src, p.dst)
        print(f"  ↩ {p.src.name}  →  {p.dst.name}")
        done += 1
        entry = {"course": p.course, "from": str(p.src.relative_to(root)),
                 "to": str(p.dst.relative_to(root)), "date": p.date_str,
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        log.append(entry)
        if p.kind == "RENAME" and p.date_str:
            meta_path = p.dst / ".notes_meta.json"
            if force or not meta_path.exists():
                session = {"abbrev": p.course, "date_str": p.date_str,
                           "assignments": [p.posting] if p.posting else [], "assignment": p.posting}
                title = cc.session_title(session["assignments"])
                existing = cc.notes_paths(p.dst, p.date_str, p.course, title).existing
                write_md_twin(existing)
                cr._write_notes_meta(p.dst, {
                    "assignments":       cr.session_hashes(session),
                    "prompt_hash":       cr.prompt_hash(p.course),
                    "readings":          cr.readings_fingerprint(p.dst),
                    "notes_file":        existing.name if existing else None,
                    "generated":         None,
                    "canvas":            [p.posting.get("name", "")] if p.posting else [],
                    "readings_included": [], "skipped": [],
                    "adopted":           True,
                    "adopted_from":      p.src.name,
                    "adopted_at":        entry["at"],
                })
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log, indent=2))
    return done


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.root:
        os.environ["COURSEWORK_ROOT"] = str(Path(args.root).expanduser())
    # This tool only ever renames folders that already exist. Importing
    # canvas_refresh resolves paths at import time, which would otherwise
    # create a folder for every course before the report is even printed —
    # and the course list stays wherever CANVAS_CONFIG_FILE / .env says, so
    # --root never silently starts a second config.
    previous_no_create = os.environ.get("CANVAS_NO_CREATE_FOLDERS")
    os.environ["CANVAS_NO_CREATE_FOLDERS"] = "1"
    try:
        return _run(args, path_config=None)
    finally:
        if previous_no_create is None:
            os.environ.pop("CANVAS_NO_CREATE_FOLDERS", None)
        else:
            os.environ["CANVAS_NO_CREATE_FOLDERS"] = previous_no_create


def _run(args, path_config=None) -> int:
    path_config, cc, cr = _import_scripts()
    paths = path_config.resolve(create_folders=False)
    root = paths["coursework_root"]
    maps = {}
    for m in args.map:
        k, sep, v = m.partition("=")
        if not sep:
            sys.exit(f"--map needs PATH=VALUE, got {m!r}")
        maps[k.strip().strip("/")] = v.strip()

    plans = []
    for abbrev, info in sorted(paths["courses"].items()):
        if args.course and abbrev != args.course:
            continue
        if not info.get("folder_path"):
            continue
        try:
            postings = postings_for(cr, cc, info["canvas_id"])
        except SystemExit:
            raise
        except Exception as e:
            print(f"  {abbrev}: Canvas unreachable ({e}) — folders listed as unmatched")
            postings = []
        if accept := args.accept_fuzzy:
            pass
        plans += plan_course(abbrev, info, root, postings, maps, accept_fuzzy=args.accept_fuzzy,
                             relabel=args.relabel, cc=cc)
        if args.accept_fuzzy:
            for p in plans:
                if p.kind == "FUZZY":
                    p.kind = "RENAME"

    print(f"\nCoursework root: {root}\n")
    print(report(plans, root, cc))
    if not args.apply:
        print("\nNothing changed (dry run). Re-run with --apply to rename.")
        return 0
    n = apply(plans, root, cr, cc, path_config, force=args.force)
    print(f"\n✅ {n} folder(s) renamed. Log: {path_config.CONFIG_FILE.parent / 'migrations.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
