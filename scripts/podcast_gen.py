#!/usr/bin/env python3
"""
HBS Podcast Generator
Creates a long NotebookLM "Deep Dive" audio overview for a class session.

Output: YYMMDD CLASSCODE Podcast.m4a  (saved in the session folder)

Usage:
  ./.venv/bin/python scripts/podcast_gen.py 260902 LTV
  ./.venv/bin/python scripts/podcast_gen.py 260908 CATS

How it works:
  1. Finds the session folder, the readings, and the cheat sheet if one exists
  2. Creates (or reuses) a NotebookLM notebook for the session
  3. Uploads the readings, the cheat sheet (as a clearly labelled text source —
     the hosts are told it is the student's analysis, not the case) and the
     Canvas posting with the discussion questions
  4. Generates a Deep Dive at the longest length NotebookLM offers and waits
  5. Downloads the podcast to the session folder as an .m4a file

  PODCAST_FORMAT / PODCAST_LENGTH (env or .env) override the format
  (deep_dive | brief | critique | debate) and length (short | default | long).

Prerequisites:
  - notebooklm-py installed: pip install 'notebooklm-py[browser]'
  - One-time login:
      ~/repos/hbs-course-helper/.venv/bin/notebooklm login
  - After that, cookies are cached in ~/.notebooklm/profiles/default/
    and all scripts use them automatically.
"""

import asyncio
import re as _re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import path_config
import ai_config
import canvas_common as _cc
import canvas_refresh as _cr
from notebooklm.exceptions import ArtifactInProgressTimeoutError

_paths  = path_config.resolve()
DEST_ROOT = _paths["coursework_root"]
_COURSES  = _paths["courses"]
COURSE_IDS = {a: d["canvas_id"] for a, d in _COURSES.items()}

_PROMPTS_DIR = _paths["prompts_dir"]

CHEAT_SHEET_TITLE = "CHEAT SHEET — the student's own prep notes, NOT the case"
POSTING_TITLE     = "CANVAS POSTING — the professor's discussion questions"


def _audio_options():
    """
    (AudioFormat, AudioLength) from PODCAST_FORMAT / PODCAST_LENGTH.
    Default is the longest Deep Dive NotebookLM will make.
    """
    from notebooklm.types import AudioFormat, AudioLength
    fmt = (_cr.cfg("PODCAST_FORMAT") or "deep_dive").strip().upper()
    ln  = (_cr.cfg("PODCAST_LENGTH") or "long").strip().upper()
    return (getattr(AudioFormat, fmt, AudioFormat.DEEP_DIVE),
            getattr(AudioLength, ln, AudioLength.LONG))


def cheat_sheet_text(session_dir: Path, date_str: str, abbrev: str,
                     title: str = "") -> "str | None":
    """
    The cheat sheet as plain text for a NotebookLM text source — the
    Markdown twin when there is one, else the Word file's text.
    """
    np = _cc.notes_paths(session_dir, date_str, abbrev, title)
    if np.md.exists():
        return np.md.read_text(errors="replace")
    existing = np.existing
    if existing is None:
        return None
    for twin in (existing.parent / f".{existing.stem}.md", existing.with_suffix(".md")):
        if twin.exists():
            return twin.read_text(errors="replace")
    text = ai_config.extract_text(existing)
    return text if text.strip() else None


def source_fingerprint(reading_files: list, sheet: "str | None", assignments: list) -> str:
    """
    What the episode is built from: the readings' names and bytes, the cheat
    sheet text, the postings. A notebook carries this in its title, so a
    later run with different sources renders a new episode instead of
    collecting the old audio.
    """
    import hashlib
    h = hashlib.md5()
    for f in sorted(reading_files, key=lambda f: f.name):
        h.update(f.name.encode()); h.update(_cc.file_md5(f).encode())
    h.update((sheet or "").encode("utf-8", "replace"))
    for a in assignments:
        h.update(str(a.get("id")).encode()); h.update((a.get("description") or "").encode("utf-8", "replace"))
    return h.hexdigest()[:10]


def notebook_title(session_label: str, fingerprint: str) -> str:
    return f"{session_label} · {fingerprint}"


def is_stale_notebook(title: str, session_label: str, current_title: str) -> bool:
    """An earlier notebook for the same class day built from other sources."""
    if title == current_title:
        return False
    return title == session_label or title.startswith(f"{session_label} · ")


def _build_instructions(reading_files: list, abbrev: str) -> str:
    """Load podcast prompt from file, append course-specific notes if present."""
    # The "supplemental" prompt adds a frameworks section when there is more
    # than the case itself to draw on.
    has_supplemental = len(reading_files) > 1
    prompt_file = (
        _PROMPTS_DIR / "podcast_prompt_supplemental.md"
        if has_supplemental
        else _PROMPTS_DIR / "podcast_prompt.md"
    )
    base = prompt_file.read_text() if prompt_file.exists() else ""

    # Load per-course refinement (same pattern as cheat sheet)
    code = abbrev.replace(" ", "_")
    refinement_file = _PROMPTS_DIR / f"podcast_prompt_{code}_refinement.md"
    class_notes = ""
    if refinement_file.exists():
        raw = refinement_file.read_text().strip()
        raw = _re.sub(r"<!--.*?-->", "", raw, flags=_re.DOTALL).strip()
        if raw and raw != "# CLASS-SPECIFIC NOTES":
            class_notes = f"\n\n{raw}"

    if class_notes:
        instructions = _re.sub(r"\[CLASS-SPECIFIC NOTES\].*", class_notes, base, flags=_re.DOTALL)
    else:
        instructions = _re.sub(r"\n*\[CLASS-SPECIFIC NOTES\].*", "", base, flags=_re.DOTALL)

    return instructions.strip()


async def _generate(date_str: str, abbrev: str):
    from notebooklm import NotebookLMClient

    course_folder = _COURSES.get(abbrev, {}).get("folder_path") or DEST_ROOT / abbrev
    session_label = _cc.artifact_stem(date_str, abbrev)   # NotebookLM notebook title

    # The folder is found by its date; if it doesn't exist yet its name needs
    # the Canvas posting, so that is fetched before anything else.
    session_dir = _cc.find_session_dir(course_folder, date_str)
    if session_dir is not None and _cc.podcast_path(session_dir, date_str, abbrev).exists():
        print(f"Already exists: {_cc.podcast_path(session_dir, date_str, abbrev)}")
        return

    course_id = COURSE_IDS[abbrev]
    print("Fetching Canvas assignment...", end=" ", flush=True)
    assignments = _cr.assignments_on(course_id, date_str)
    print("found: " + "; ".join(a["name"] for a in assignments) if assignments else "not found")

    if session_dir is None:
        session_dir = course_folder / _cc.session_dirname(date_str, abbrev, assignments)
    podcast_file = _cc.podcast_path(session_dir, date_str, abbrev)
    session_dir.mkdir(parents=True, exist_ok=True)

    # ── Reading files ──────────────────────────────────────────────────────────
    reading_files = sorted(
        (f for f in session_dir.iterdir()
         if f.is_file() and f.suffix.lower() in _cr.READING_EXTS
         and not _cc.is_notes_file(f.name)
         and f.suffix.lower() != ".m4a"
         # "(skipped)" stubs say a reading was left out — uploading one as a
         # source tells the hosts about a file they cannot see. "~$" files are
         # Word lock files, not documents.
         and "(skipped)" not in f.name and not f.name.startswith("~$")),
        key=lambda f: (-f.stat().st_size if f.suffix.lower() == ".pdf" else 0, f.name),
    )
    # Exclude PDFs over the page limit, and byte-identical repeats of a reading
    # Canvas attached in two places.
    usable, seen = [], {}
    for f in reading_files:
        if f.suffix.lower() == ".pdf":
            pages = _cr.pdf_page_count(f)
            if pages > _cr.PDF_PAGE_LIMIT:
                print(f"  ⚠ Skipping ({pages}p > {_cr.PDF_PAGE_LIMIT}p limit): {f.name}")
                continue
        digest = _cc.file_md5(f)
        if digest in seen:
            print(f"  – Duplicate of {seen[digest]}, uploading once: {f.name}")
            continue
        seen[digest] = f.name
        usable.append(f)
    reading_files = usable

    sheet = cheat_sheet_text(session_dir, date_str, abbrev, _cc.session_title(assignments))

    print(f"\nPodcast: {abbrev} {date_str}")
    print(f"Session: {session_dir}")
    print(f"Readings ({len(reading_files)}):")
    for f in reading_files:
        print(f"  • {f.name}")
    print(f"Cheat sheet: {'yes' if sheet else 'none yet'}")

    if not reading_files and not assignments:
        sys.exit("No readings and no Canvas assignment — nothing to generate from.")

    # ── NotebookLM ─────────────────────────────────────────────────────────────
    async with NotebookLMClient.from_storage() as client:

        # Find or create the notebook for THESE sources. A notebook made from
        # other sources (a case that arrived later) is stale: its finished
        # audio must not be collected as if it were this episode.
        title = notebook_title(session_label, source_fingerprint(reading_files, sheet, assignments))
        notebooks = await client.notebooks.list()
        nb = next((n for n in notebooks if n.title == title), None)
        for old in notebooks:
            if is_stale_notebook(old.title, session_label, title):
                try:
                    await client.notebooks.delete(old.id)
                    print(f"Removed stale notebook: {old.title}")
                except Exception as e:
                    print(f"  (could not remove stale notebook {old.title}: {e})")
        if nb:
            print(f"Reusing notebook: {nb.title}")
        else:
            nb = await client.notebooks.create(title)
            print(f"Created notebook:  {nb.title}")

        # Upload sources only if notebook is empty (avoids re-uploading on retry)
        existing = await client.sources.list(nb.id)
        if existing:
            print(f"  {len(existing)} source(s) already in notebook — skipping upload")
        else:
            for f in reading_files:
                print(f"  ↑ Uploading {f.name}...")
                await client.sources.add_file(nb.id, str(f), wait=True, wait_timeout=180.0)

            for a in assignments:
                title = a.get("name", f"{session_label} Assignment")
                desc  = _cr.strip_html(a.get("description") or "")
                print(f"  + Adding Canvas assignment: {title}")
                await client.sources.add_text(nb.id, f"{POSTING_TITLE}: {title}", desc, wait=True)

            # The cheat sheet goes in as its own, unmistakably labelled source
            # so the hosts can separate "the case says" from "the prep notes
            # argue" — the prompt tells them to keep those apart out loud.
            if sheet:
                print(f"  + Adding the cheat sheet as a labelled source")
                await client.sources.add_text(nb.id, CHEAT_SHEET_TITLE, sheet, wait=True)

        # Generate
        instructions = _build_instructions(reading_files, abbrev)
        has_supplemental = len(reading_files) > 1
        audio_format, audio_length = _audio_options()
        # A render that outran the poll ceiling keeps going on NotebookLM's side.
        # Collect a finished one rather than paying for the same audio twice —
        # without this, every retry queued a second render and waited again.
        existing_audio = None
        try:
            for art in await client.artifacts.list_audio(nb.id):
                if art.is_completed:
                    existing_audio = art
                    break
        except Exception as e:
            print(f"  (could not list existing audio: {e})")

        if existing_audio is not None:
            mins = int((existing_audio.duration_seconds or 0) // 60)
            print(f"\n  Audio from an earlier run has finished ({mins} min) — "
                  f"collecting it instead of generating again.")
        else:
            print(f"\nGenerating {audio_format.name.lower().replace('_', ' ')} "
                  f"({audio_length.name.lower()} length; a long one renders 10–30 min)"
                  f"{' [with supplemental frameworks]' if has_supplemental else ''}...",
                  flush=True)
            status = await client.artifacts.generate_audio(
                nb.id,
                instructions=instructions,
                audio_format=audio_format,
                audio_length=audio_length,
            )
            print(f"  Task: {status.task_id}")

            def _on_change(s):
                print(f"  → {s.status}")

            try:
                await client.artifacts.wait_for_completion(
                    nb.id,
                    status.task_id,
                    # A six-source notebook regularly runs past 20 minutes. The
                    # old ceiling abandoned finished work and reported failure.
                    timeout=2700.0,
                    on_status_change=_on_change,
                )
            except ArtifactInProgressTimeoutError:
                print(f"\n  Still rendering after 45 min. NotebookLM keeps going "
                      f"without us — re-run this command later and it will "
                      f"download the finished audio:")
                print(f"    ./.venv/bin/python scripts/podcast_gen.py "
                      f"{date_str} {abbrev}")
                return

        # Download
        print(f"  ↓ Downloading...")
        await client.artifacts.download_audio(nb.id, str(podcast_file))

    shrink_for_speech(podcast_file)
    print(f"\n✅ Saved: {podcast_file}")
    print(f"   Play:  open '{podcast_file}'")


def shrink_for_speech(path: Path) -> None:
    """
    Re-encode to a speech bitrate when ffmpeg is available.

    NotebookLM hands back ~260 kb/s stereo — 2 MB per minute — so a long Deep
    Dive lands near GitHub's 100 MB per-file limit and a term of episodes
    runs to gigabytes. 64 kb/s mono AAC is indistinguishable for two voices
    and a quarter of the size. PODCAST_BITRATE overrides; "0" disables.
    """
    import shutil
    import subprocess
    bitrate = (_cr.cfg("PODCAST_BITRATE") or "64k").strip()
    if bitrate == "0" or not path.exists():
        return
    bps = int(float(bitrate.lower().rstrip("k")) * 1000) if bitrate.lower().endswith("k") else int(bitrate)
    ffmpeg = shutil.which("ffmpeg")
    afconvert = shutil.which("afconvert")      # ships with macOS; no install needed
    tmp = path.with_suffix(".tmp.m4a")
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
               "-vn", "-ac", "1", "-c:a", "aac", "-b:a", bitrate,
               "-movflags", "+faststart", str(tmp)]
    elif afconvert:
        # afconvert cannot downmix to mono without a channel map, and at this
        # bitrate stereo is already a quarter of the size; keep the channels.
        cmd = [afconvert, "-f", "m4af", "-d", "aac", "-b", str(bps),
               str(path), str(tmp)]
    else:
        return
    before = path.stat().st_size
    try:
        subprocess.run(cmd, check=True, timeout=900, capture_output=True)
        after = tmp.stat().st_size
        if 0 < after < before:
            tmp.replace(path)
            print(f"  ⇣ Re-encoded for speech: {before/1e6:.0f} MB → {after/1e6:.0f} MB ({bitrate} mono)")
        else:
            tmp.unlink(missing_ok=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"  (kept original audio; ffmpeg failed: {e})")
        tmp.unlink(missing_ok=True)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    date_str = sys.argv[1]
    abbrev   = " ".join(sys.argv[2:]).upper()

    if abbrev not in COURSE_IDS:
        sys.exit(f"Unknown course '{abbrev}'. Known: {', '.join(COURSE_IDS)}")

    try:
        asyncio.run(_generate(date_str, abbrev))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
