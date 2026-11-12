#!/usr/bin/env python3
"""
inbox_fetch.py — pull emailed class materials straight from Gmail into inbox/.

The original route (tools/gmail_forwarder.gs, an Apps Script in the student's
Google account) went silent for five weeks without anyone noticing: role
sheets for Elcer, V-Cola and Teeing Up Success all sat in Gmail while the
pipeline built cheat sheets from the posting alone. This reads the mailbox
directly over IMAP, in the cloud job and (optionally) in the Mac mirror job,
so the only moving part is a Gmail app password stored as a secret.

Setup, once:
    1. Google Account → Security → 2-Step Verification → App passwords →
       create one named "hbs-coursework" (16 characters, shown once).
    2. gh -R <data repo> secret set GMAIL_USER            (the Gmail address)
       gh -R <data repo> secret set GMAIL_APP_PASSWORD    (the 16 characters)
    On the Mac, the same two keys in the code repo's .env let the mirror job
    fetch every half hour as well.

What it does:
    - Searches the last INBOX_FETCH_DAYS (14) days for messages whose subject
      contains "CONFIDENTIAL ROLE INFORMATION", or that come from an hbs.edu
      address with pairings / nudge / directive / counterpart / "role link"
      in the subject (the same two queries the Apps Script used).
    - Writes inbox/<yymmdd>-<gmail message id>/message.json in the forwarder's
      format ({id, subject, from, date, body, links, attachments}) plus the
      attachments, and records the id in claude/inbox_state.json["fetched"]
      so a message is fetched once. inbox.py then files it as before.
    - Never deletes, labels or marks anything in the mailbox (read-only).

    python3 scripts/inbox_fetch.py            # fetch anything new
    python3 scripts/inbox_fetch.py --dry-run  # list what would be fetched
"""
import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import path_config

IMAP_HOST = "imap.gmail.com"
FETCH_DAYS = int(os.environ.get("INBOX_FETCH_DAYS", "14") or 14)
SUBJECT_RE = re.compile(r"confidential\s+role\s+information", re.I)
HBS_SUBJECT_RE = re.compile(r"\b(pairings?|nudge|directive|counterparts?|role\s+information|role\s+link)\b", re.I)
HBS_FROM_RE = re.compile(r"@([\w.-]+\.)?hbs\.edu\b", re.I)
LINK_RE = re.compile(r"https?://[^\s<>\")\]]+")
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024
_INLINE_IMAGE_RE = re.compile(r"^(image\d+|outlook-[\w-]+|att\d+)\.(png|jpe?g|gif)$", re.I)


def credentials() -> "tuple[str, str] | None":
    """(user, app password) from the environment or .env, or None when unset."""
    try:
        path_config.resolve()
    except Exception:
        pass
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    return (user, pw) if user and pw else None


def wanted(subject: str, sender: str) -> bool:
    """The same two filters the Apps Script forwarder applied."""
    if SUBJECT_RE.search(subject or ""):
        return True
    return bool(HBS_FROM_RE.search(sender or "") and HBS_SUBJECT_RE.search(subject or ""))


def _decode(value) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out)


def _body_and_attachments(msg) -> "tuple[str, list]":
    """Plain-text body (HTML stripped when that is all there is) and [(filename, bytes)]."""
    text, html, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        fname = _decode(part.get_filename())
        ctype = part.get_content_type()
        if fname and ("attachment" in disp or ctype not in ("text/plain", "text/html")):
            if _INLINE_IMAGE_RE.match(fname):
                continue
            payload = part.get_payload(decode=True) or b""
            if 0 < len(payload) <= MAX_ATTACHMENT_BYTES:
                attachments.append((fname, payload))
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        s = payload.decode(charset, "replace")
        (text if ctype == "text/plain" else html).append(s)
    body = "\n".join(text).strip()
    if not body and html:
        h = "\n".join(html)
        h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
        body = re.sub(r"<[^>]+>", " ", h)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    return body, attachments


def _state_path() -> Path:
    import inbox
    return inbox.state_file()


def _load_state() -> dict:
    p = _state_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def _gm_msgid(fetch_meta: bytes) -> str:
    m = re.search(rb"X-GM-MSGID\s+(\d+)", fetch_meta or b"")
    return format(int(m.group(1)), "x") if m else ""


def fetch(dry_run: bool = False, connect=None) -> int:
    """Fetch new matching messages into inbox/. Returns how many were written."""
    creds = credentials()
    if not creds:
        print("  Inbox fetch: GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping (the forwarder, if any, still applies)")
        return 0
    user, pw = creds
    import inbox
    root = inbox.inbox_dir()
    state = _load_state()
    fetched = state.setdefault("fetched", {})

    conn = (connect or _connect)(user, pw)
    try:
        conn.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=FETCH_DAYS)).strftime("%d-%b-%Y")
        ok, data = conn.search(None, "SINCE", since)
        ids = data[0].split() if ok == "OK" and data and data[0] else []
        n = 0
        for num in ids:
            ok, parts = conn.fetch(num, "(X-GM-MSGID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if ok != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            gm_id = _gm_msgid(parts[0][0]) or num.decode()
            hdr = email.message_from_bytes(parts[0][1])
            subject, sender = _decode(hdr.get("Subject")), _decode(hdr.get("From"))
            if not wanted(subject, sender) or gm_id in fetched:
                continue
            ok, full = conn.fetch(num, "(BODY.PEEK[])")
            if ok != "OK" or not full or not isinstance(full[0], tuple):
                continue
            msg = email.message_from_bytes(full[0][1])
            sent = email.utils.parsedate_to_datetime(msg.get("Date") or "") if msg.get("Date") else datetime.now(timezone.utc)
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            body, attachments = _body_and_attachments(msg)
            folder = root / f"{sent.astimezone(timezone.utc).strftime('%y%m%d')}-{gm_id}"
            print(f"    {'would fetch' if dry_run else '↓'} {subject!r} from {sender} "
                  f"({len(attachments)} attachment(s)) → inbox/{folder.name}")
            if dry_run:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            names = []
            for fname, payload in attachments:
                safe = re.sub(r"[\\/:*?\"<>|]+", "-", fname).strip() or "attachment"
                (folder / safe).write_bytes(payload)
                names.append(safe)
            links = []
            for l in LINK_RE.findall(body):
                l = l.rstrip(".,;")
                if l not in links:
                    links.append(l)
            (folder / "message.json").write_text(json.dumps({
                "id": gm_id, "subject": subject, "from": sender,
                "date": sent.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "body": body, "links": links, "attachments": names,
            }, indent=1, ensure_ascii=False))
            fetched[gm_id] = {"subject": subject, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            n += 1
        if n and not dry_run:
            _save_state(state)
        if n:
            print(f"  Inbox fetch: {n} new email(s) saved to inbox/")
        else:
            print("  Inbox fetch: nothing new")
        return n
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _connect(user: str, pw: str):
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    conn.login(user, pw)
    return conn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="list what would be fetched; write nothing")
    args = ap.parse_args(argv)
    try:
        fetch(dry_run=args.dry_run)
    except imaplib.IMAP4.error as e:
        print(f"  ⚠ Inbox fetch: Gmail refused the login ({e}) — is GMAIL_APP_PASSWORD an app password?")
        return 1
    except Exception as e:
        print(f"  ⚠ Inbox fetch failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
