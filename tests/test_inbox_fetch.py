"""inbox_fetch: role emails straight from Gmail into inbox/, once each, read-only."""
import json
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import inbox_fetch as fx   # noqa: E402


def _mail(subject, sender, body, attachments=(), html=False, date="Mon, 09 Nov 2026 14:30:00 +0000"):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"] = subject, sender, "me@example.com", date
    if html:
        m.set_content("plain fallback")
        m.add_alternative(f"<html><body><p>{body}</p><p>Link: <a href='https://hbsp.harvard.edu/tu/abc'>role</a></p></body></html>", subtype="html")
    else:
        m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return m.as_bytes()


class FakeIMAP:
    """Just enough of imaplib for fetch(): select, search, fetch, logout."""
    def __init__(self, mails):
        self.mails = mails           # {num: (gm_id_int, bytes)}
        self.logged_out = False
        self.writes = []

    def select(self, box, readonly=False):
        assert readonly, "the mailbox must be opened read-only"
        return "OK", [b"3"]

    def search(self, charset, *criteria):
        assert "SINCE" in criteria
        return "OK", [b" ".join(str(n).encode() for n in self.mails)]

    def fetch(self, num, what):
        gm, raw = self.mails[int(num)]
        if "HEADER.FIELDS" in what:
            hdr = b"".join(line + b"\r\n" for line in raw.split(b"\r\n") if line.split(b":")[0] in (b"Subject", b"From", b"Date"))
            return "OK", [(f"{num.decode()} (X-GM-MSGID {gm} BODY[HEADER.FIELDS (SUBJECT FROM DATE)] {{{len(hdr)}}}".encode(), hdr), b")"]
        return "OK", [(f"{num.decode()} (BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self):
        self.logged_out = True


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "coursework"; root.mkdir()
    (root / "claude").mkdir()
    (root / "claude" / "canvas_config.json").write_text(json.dumps({"courses": {}}))
    monkeypatch.setenv("COURSEWORK_ROOT", str(root))
    monkeypatch.setenv("CANVAS_CONFIG_FILE", str(root / "claude" / "canvas_config.json"))
    monkeypatch.setenv("CANVAS_API_TOKEN", "t")
    monkeypatch.setenv("GMAIL_USER", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    import path_config
    path_config.resolve.cache_clear() if hasattr(path_config.resolve, "cache_clear") else None
    monkeypatch.setattr(fx, "_state_path", lambda: root / "claude" / "inbox_state.json")
    import inbox
    monkeypatch.setattr(inbox, "inbox_dir", lambda: root / "inbox")
    return root


def test_wanted_mirrors_the_forwarder_queries():
    assert fx.wanted("NEG-04: CONFIDENTIAL ROLE INFORMATION: V-Cola", "tara@hbs.edu")
    assert fx.wanted("04 Pairings for Windham Negotiation", '"Colson Leaning, Tara" <tcolsonleaning@hbs.edu>')
    assert fx.wanted("Nudge: your Windham negotiation approach", "tcolsonleaning@hbs.edu")
    assert not fx.wanted("04 Pairings for Windham Negotiation", "someone@gmail.com")
    assert not fx.wanted("Lunch on Friday?", "tara@hbs.edu")
    assert not fx.wanted("H RC/EC + Trevor Fetter Mixer", "nscott@mba2027.hbs.edu")


def test_fetch_writes_forwarder_format_once(world, capsys):
    role_pdf = b"%PDF-1.4 role"
    mails = {
        1: (0x1a1c0b779b70e65f, _mail("NEG-04: CONFIDENTIAL ROLE INFORMATION: V-Cola", "tcolsonleaning@hbs.edu",
                                      "You have been assigned the role of Cash Adman.\n\nCash Adman role link: https://hbsp.harvard.edu/tu/d02afa18\n\nBest, Tara")),
        2: (0x1a1c1b16110edee0, _mail("MPGTD-00 CONFIDENTIAL ROLE INFORMATION: Kate Wize", "stait@hbs.edu",
                                      "Role attached.", attachments=[("Teeing Up Success - Kate Wize.pdf", role_pdf)])),
        3: (0x99, _mail("Photo Shoot", "esmith@mba2027.hbs.edu", "Baker backside, 5pm")),
    }
    imap = FakeIMAP(mails)
    n = fx.fetch(connect=lambda u, p: imap)
    assert n == 2 and imap.logged_out
    inbox = world / "inbox"
    v = json.loads((inbox / "261109-1a1c0b779b70e65f" / "message.json").read_text())
    assert v["subject"].endswith("V-Cola") and v["links"] == ["https://hbsp.harvard.edu/tu/d02afa18"]
    assert v["attachments"] == [] and v["date"] == "2026-11-09T14:30:00.000Z"
    k = json.loads((inbox / "261109-1a1c1b16110edee0" / "message.json").read_text())
    assert k["attachments"] == ["Teeing Up Success - Kate Wize.pdf"]
    assert (inbox / "261109-1a1c1b16110edee0" / "Teeing Up Success - Kate Wize.pdf").read_bytes() == role_pdf
    assert not (inbox / "261109-99").exists()
    state = json.loads((world / "claude" / "inbox_state.json").read_text())
    assert set(state["fetched"]) == {"1a1c0b779b70e65f", "1a1c1b16110edee0"}

    # Second pass: nothing new, nothing rewritten.
    (inbox / "261109-1a1c0b779b70e65f").rename(inbox / "gone")
    assert fx.fetch(connect=lambda u, p: FakeIMAP(mails)) == 0
    assert not (inbox / "261109-1a1c0b779b70e65f").exists()
    assert "nothing new" in capsys.readouterr().out


def test_html_only_body_is_flattened_and_links_kept(world):
    mails = {1: (0x5, _mail("NEG-04: CONFIDENTIAL ROLE INFORMATION: Rio", "tcolsonleaning@hbs.edu", "Role of Buyer.", html=True))}
    fx.fetch(connect=lambda u, p: FakeIMAP(mails))
    m = json.loads((world / "inbox" / "261109-5" / "message.json").read_text())
    assert "plain fallback" in m["body"]           # text part wins when present
    assert "https://hbsp.harvard.edu/tu/abc" in m["links"] or m["links"] == []


def test_no_credentials_is_a_quiet_skip(world, monkeypatch, capsys):
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    assert fx.fetch(connect=lambda u, p: pytest.fail("must not connect")) == 0
    assert "not set" in capsys.readouterr().out


def test_dry_run_writes_nothing(world, capsys):
    mails = {1: (0x7, _mail("NEG-04: CONFIDENTIAL ROLE INFORMATION: Rio", "tcolsonleaning@hbs.edu", "hi"))}
    assert fx.fetch(dry_run=True, connect=lambda u, p: FakeIMAP(mails)) == 0
    assert not (world / "inbox").exists() and "would fetch" in capsys.readouterr().out
