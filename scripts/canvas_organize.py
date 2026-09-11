#!/usr/bin/env python3
"""
canvas_organize.py — Deduplication and folder organization

Rules (applied in order, each file ends up in exactly one place):

  PPTX/PPT (slides) — always go to General/Slides/, regardless of where
  Canvas placed them (even if in a per-class session folder).

  PDFs / docs in session folders (YYMMDD ABBREV/) — stay there; any
  duplicate copy in General/ is removed.

  PDFs / docs in General/ root:
    - Course-level doc (syllabus, schedule, guide, etc.) → stays in General/
    - Otherwise → General/Supplemental/

  Supplemental/ and Slides/ files — never touched (already canonical).

Safe by design:
  - A file is only ever removed when another copy with identical content
    (size and MD5) exists. Same name, different content → warning, both kept.
  - Dedup looks inside each course folder only, never across courses and
    never at anything outside the configured course folders.
  - Removed duplicates go to the Trash (macOS) or COURSEWORK_ROOT/.trash/
    elsewhere, never straight to deletion.
  - Idempotent — safe to re-run any time

Run manually:    python3 canvas_organize.py
Also called automatically after every canvas_refresh.py sync.
"""

import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import path_config
from canvas_common import same_content

COURSEWORK_ROOT = path_config.COURSEWORK_ROOT
SESSION_RE = re.compile(r'^\d{6}\s')

SLIDE_EXTS = {'.pptx', '.ppt'}

COURSE_LEVEL_KEYWORDS = {
    "syllabus", "syllabi", "calendar", "schedule", "overview", "guide",
    "resource", "reference", "background", "appendix", "rubric", "grading",
    "policy", "handbook", "orientation", "welcome", "introduction",
}


def _is_course_level(filename: str) -> bool:
    stem = Path(filename).stem.lower()
    pattern = r'\b(' + '|'.join(re.escape(k) for k in COURSE_LEVEL_KEYWORDS) + r')\b'
    return bool(re.search(pattern, stem))


def _session_file_index(course_dir: Path) -> dict[str, list[Path]]:
    """Map filename → list of session folder paths where it appears."""
    index: dict[str, list[Path]] = {}
    for d in course_dir.iterdir():
        if d.is_dir() and SESSION_RE.match(d.name):
            for f in d.iterdir():
                if f.is_file() and not f.name.startswith('.'):
                    index.setdefault(f.name, []).append(f)
    return index


def _remove_if_identical(f: Path, dest: Path, counts: dict, key: str, label: str) -> bool:
    """
    Delete `f` only when `dest` already holds the same bytes.

    Two files sharing a name is not evidence they are the same document —
    every course posts an "Exhibits.pdf" or a "Case.pdf" — so the content is
    compared before anything is removed. Returns True if `f` was removed.
    """
    if same_content(f, dest):
        f.unlink()
        counts[key] += 1
        print(f"    ✗ duplicate removed: {f.name}  ({label})")
        return True
    counts["warned"] += 1
    print(f"    ⚠ same name, different content, leaving both: {f.name}  ({label})")
    return False


def _migrate_session_slides(course_dir: Path, slides_dir: Path, counts: dict) -> None:
    """
    Move any .pptx/.ppt files found in session folders → General/Slides/.
    PPTX always belongs in Slides/, never in a per-class folder.
    """
    for d in sorted(course_dir.iterdir()):
        if not (d.is_dir() and SESSION_RE.match(d.name)):
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in SLIDE_EXTS:
                slides_dir.mkdir(parents=True, exist_ok=True)
                dest = slides_dir / f.name
                if dest.exists():
                    _remove_if_identical(f, dest, counts, "slides", f"session {d.name}")
                else:
                    f.rename(dest)
                    counts["slides"] += 1
                    print(f"    → Slides/ (from {d.name}): {f.name}")


def organize_course(course_dir: Path, abbrev: str) -> dict:
    """Organize a single course directory. Returns counts of actions taken."""
    general = course_dir / "General"
    if not general.exists():
        return {}

    slides_dir       = general / "Slides"
    supplemental_dir = general / "Supplemental"

    supplemental_dir.mkdir(exist_ok=True)

    counts = {"removed": 0, "slides": 0, "supplemental": 0, "kept": 0, "warned": 0}

    # ── Step 0: Pull PPTX out of session folders → Slides/ ───────────────────
    _migrate_session_slides(course_dir, slides_dir, counts)

    # Rebuild session index after moving slides (so the index reflects current state)
    session_index = _session_file_index(course_dir)

    # ── Step 1-4: Process files in General/ root ──────────────────────────────
    for f in sorted(general.iterdir()):
        if f.is_dir() or f.name.startswith('.'):
            continue

        # 1. Duplicate of a session file → remove from General/
        if f.name in session_index:
            session_copies = session_index[f.name]
            identical = next((s for s in session_copies if same_content(f, s)), None)
            if identical is not None:
                f.unlink()
                counts["removed"] += 1
                print(f"    ✗ duplicate removed: {f.name}  (in: {identical.parent.name})")
            else:
                counts["warned"] += 1
                locations = ", ".join(p.parent.name for p in session_copies)
                print(f"    ⚠ same name, different content, leaving both: {f.name}  (in: {locations})")
            continue

        # 2. Slides → General/Slides/
        if f.suffix.lower() in SLIDE_EXTS:
            slides_dir.mkdir(exist_ok=True)
            dest = slides_dir / f.name
            if dest.exists():
                _remove_if_identical(f, dest, counts, "slides", "General/")
            else:
                f.rename(dest)
                counts["slides"] += 1
                print(f"    → Slides/: {f.name}")
            continue

        # 3. Course-level doc → keep in General/ root
        if _is_course_level(f.name):
            counts["kept"] += 1
            continue

        # 4. Everything else → General/Supplemental/
        dest = supplemental_dir / f.name
        if dest.exists():
            _remove_if_identical(f, dest, counts, "supplemental", "General/")
        else:
            f.rename(dest)
            counts["supplemental"] += 1
            print(f"    → Supplemental/: {f.name}")

    return counts


# ── Trash ─────────────────────────────────────────────────────────────────────

def _trash_dir(root: Path) -> Path:
    """
    Where removed duplicates go.

    macOS has a Trash the user can look in; a Linux runner does not, and
    shutil.move onto a non-existent ~/.Trash would have renamed the first
    duplicate to a *file* called .Trash and raised on the second. Anywhere
    without a Trash gets COURSEWORK_ROOT/.trash/, which is gitignored in the
    data repo and skipped by the dedup scan. COURSEWORK_TRASH overrides both.
    """
    raw = os.getenv("COURSEWORK_TRASH")
    if raw:
        d = Path(raw).expanduser()
    elif sys.platform == "darwin" and (Path.home() / ".Trash").is_dir():
        d = Path.home() / ".Trash"
    else:
        d = root / ".trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trash(p: Path, trash_dir: Path) -> None:
    """Move a file into trash_dir, handling name collisions."""
    dest = trash_dir / p.name
    i = 1
    while dest.exists():
        dest = trash_dir / f"{p.stem}_{i}{p.suffix}"
        i += 1
    shutil.move(str(p), str(dest))


def _unsafe_root(root: Path) -> bool:
    """
    A coursework root that is the home folder, an ancestor of it, or a
    filesystem root. `_find_course_folder` fuzzy-matches folder names, so a
    misconfigured COURSEWORK_ROOT=~ would have put Documents/ or Desktop/ in
    scope of a routine that moves files to the Trash.
    """
    try:
        r = root.resolve()
        home = Path.home().resolve()
    except OSError:
        return True
    return r == home or r in home.parents or r == Path(r.anchor)


def _course_dirs(paths: dict) -> list[Path]:
    """Existing course folders, in config order. Shared by organize and dedup."""
    out = []
    for data in paths["courses"].values():
        folder = data.get("folder_path")
        if folder and folder.exists():
            out.append(folder)
    return out


# ── Dedup ─────────────────────────────────────────────────────────────────────

def _classify_priority(p: Path, course_dir: Path) -> int:
    """
    Return priority tier for a file (lower number = keep this copy).
      0 — General/Slides/      (canonical home for PPTX)
      1 — session folder       (canonical for PDFs tied to a day)
      2 — General/Supplemental/
      3 — General/ root
      5 — course root (file directly under COURSE/, not in a subfolder)
    """
    try:
        parts = p.relative_to(course_dir).parts
    except ValueError:
        return 9
    if len(parts) <= 1:
        return 5
    sub = parts[0]
    if SESSION_RE.match(sub):
        return 1
    if sub == "General":
        if len(parts) >= 3:
            if parts[1] == "Slides":        return 0
            if parts[1] == "Supplemental":  return 2
        return 3
    return 5


def _session_key(p: Path, course_dir: Path) -> str:
    """YYMMDD of the session folder holding p, or a sentinel that sorts last."""
    try:
        parts = p.relative_to(course_dir).parts
    except ValueError:
        return "999999"
    return next((pt for pt in parts if SESSION_RE.match(pt)), "999999")


def _choose_keep(file_paths: list[Path], course_dir: Path) -> Path:
    """
    The copy that stays: Slides/ for a deck, otherwise the earliest session
    copy, then Supplemental/, then General/, then the course root.
    """
    return min(file_paths, key=lambda p: (_classify_priority(p, course_dir),
                                          _session_key(p, course_dir), str(p)))


def dedup_to_trash(verbose: bool = True) -> int:
    """
    Within each course folder, find files that share a name and move the
    lower-priority copies to the Trash — but only when their content is
    identical to the copy being kept. Returns count of files trashed.

    Priority (highest = keep):
      General/Slides/  >  session folder  >  Supplemental/  >  General/ root
    For duplicate session-folder copies, keep the earliest date.
    """
    paths = path_config.resolve()
    root = paths["coursework_root"]

    if _unsafe_root(root):
        print(f"  ⚠ Refusing to dedup: COURSEWORK_ROOT ({root}) is the home folder "
              f"or a parent of it. Check COURSEWORK_ROOT in .env.")
        return 0

    trash_dir: Path | None = None
    trashed = 0

    for course_dir in _course_dirs(paths):
        by_name: dict[str, list[Path]] = defaultdict(list)
        for f in sorted(course_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(course_dir).parts
            # Hidden files (.notes_meta.json), the .trash/ folder itself, and a
            # co-located claude/ scripts folder are never candidates.
            if any(part.startswith(".") or part == "claude" for part in rel):
                continue
            by_name[f.name].append(f)

        for name, file_paths in sorted(by_name.items()):
            if len(file_paths) == 1:
                continue
            keep = _choose_keep(file_paths, course_dir)
            for p in file_paths:
                if p == keep:
                    continue
                if not same_content(p, keep):
                    if verbose:
                        print(f"    ⚠ same name, different content, keeping both: "
                              f"{p.relative_to(root)}  vs  {keep.relative_to(root)}")
                    continue
                if trash_dir is None:
                    trash_dir = _trash_dir(root)
                if verbose:
                    print(f"    🗑  duplicate → Trash: {p.relative_to(root)}")
                _trash(p, trash_dir)
                trashed += 1

    return trashed


def organize_all(verbose: bool = True) -> None:
    """Run organize_course for every course in COURSEWORK_ROOT."""
    paths = path_config.resolve()
    any_action = False

    for abbrev, data in paths["courses"].items():
        folder_path = data.get("folder_path")
        if not folder_path or not folder_path.exists():
            continue

        counts = organize_course(folder_path, abbrev)
        actions = (counts.get("removed", 0) + counts.get("slides", 0)
                   + counts.get("supplemental", 0) + counts.get("warned", 0))

        if actions > 0:
            any_action = True
            if verbose:
                parts = []
                if counts["removed"]:      parts.append(f"{counts['removed']} duplicates removed")
                if counts["slides"]:       parts.append(f"{counts['slides']} slides → Slides/")
                if counts["supplemental"]: parts.append(f"{counts['supplemental']} → Supplemental/")
                if counts["warned"]:       parts.append(f"{counts['warned']} conflicts skipped")
                print(f"  {abbrev}: {', '.join(parts)}")

    if not any_action and verbose:
        print("  All folders already clean — nothing to do.")


if __name__ == "__main__":
    print(f"\nOrganizing: {COURSEWORK_ROOT}\n")
    organize_all(verbose=True)
    print("\n✅ Done.\n")
