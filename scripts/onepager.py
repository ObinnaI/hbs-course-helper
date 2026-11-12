"""
onepager.py — the counterpart-facing offer one-pager a negotiation cheat sheet ends with.

The Negotiation prompt asks for a level-one heading `# Offer One-Pager: <case> — <role>`
followed by a one-page proposal safe to hand to the other side. It is split out
of the cheat sheet here and written as its own Word file next to the calculator,
so the student has three things on a negotiation day: the playbook, the
workbook, and the page that anchors the conversation.
"""
import re
from pathlib import Path

HEADING_RE = re.compile(r"(?:^|\n)#\s*Offer One-Pager[^\n]*\n", re.I)
PREFIX = "Offer One-Pager - "


def extract(md: str) -> "tuple[str, str | None]":
    """(cheat sheet without the one-pager, the one-pager markdown or None)."""
    m = HEADING_RE.search(md or "")
    if not m:
        return md, None
    start = m.start() if m.start() == 0 or md[m.start()] != "\n" else m.start() + 1
    page = md[start:].strip() + "\n"
    rest = md[:start].rstrip() + "\n"
    return rest, page


def title_of(page: str) -> str:
    m = re.match(r"#\s*Offer One-Pager:?\s*(.*)", page or "")
    return (m.group(1).strip() if m else "Offer One-Pager") or "Offer One-Pager"


def filename(session_title: str) -> str:
    safe = re.sub(r"[\\/:*?\"<>|]+", "-", session_title or "").strip() or "Negotiation"
    return f"{PREFIX}{safe}.docx"


def is_onepager(name: str) -> bool:
    n = name or ""
    return n.startswith(PREFIX) and n.lower().endswith(".docx")


def build(page: str, out: Path, metadata: dict) -> None:
    import canvas_refresh as cr
    title = title_of(page)
    body = re.sub(r"^#[^\n]*\n", "", page, count=1).strip()   # the H1 becomes the document title
    cr.markdown_to_docx(md_text=body, output_path=out, title=title, metadata=metadata)
