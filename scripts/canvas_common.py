"""
canvas_common.py — Small helpers shared by several scripts.

Deliberately imports nothing from path_config: that module resolves folders
and may call Canvas at import time, and these helpers are wanted by tests and
by code paths that should stay free of side effects.
"""

import hashlib
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
