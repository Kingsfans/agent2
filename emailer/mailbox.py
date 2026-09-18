"""Gmail mailbox access for the outreach bot -- standard library only.

One Gmail app password gets all four things the bot needs, with no OAuth flow,
no client secrets and no third-party dependency:

  create_draft()  IMAP APPEND into [Gmail]/Drafts -- a real draft in the real
                  mailbox, which you can open, edit and send by hand
  send()          SMTP over STARTTLS -- the message actually goes out
  scan_replies()  IMAP SEARCH over the inbox for humans who wrote back
  scan_bounces()  IMAP SEARCH for delivery failures, parsed for the dead address

Setup (once), for the mailbox in SENDER_EMAIL:
  1. Turn on 2-Step Verification on the Google account.
  2. Create an app password: https://myaccount.google.com/apppasswords
  3. Put it in .env as GMAIL_APP_PASSWORD. It is gitignored; it never gets
     committed and never leaves the machine.

Works against any IMAP/SMTP provider, not just Gmail -- override IMAP_HOST,
SMTP_HOST and IMAP_DRAFTS_FOLDER in .env.
"""
import email
import email.message
import email.utils
import imaplib
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "outreach"))
import serve_send as ss  # noqa: E402

IMAP_HOST = ss.cfg("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(ss.cfg("IMAP_PORT", "993"))
SMTP_HOST = ss.cfg("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(ss.cfg("SMTP_PORT", "587"))
DRAFTS_FOLDER = ss.cfg("IMAP_DRAFTS_FOLDER", "[Gmail]/Drafts")
INBOX_FOLDER = ss.cfg("IMAP_INBOX_FOLDER", "INBOX")

# Addresses that send delivery failures rather than a human reply.
DAEMON_RE = re.compile(r"(mailer-daemon|postmaster|no-?reply|delivery|notification)", re.I)
# The dead address inside a bounce report, in preference order.
FINAL_RECIP_RE = re.compile(r"^(?:Final-Recipient|Original-Recipient):\s*(?:rfc822;)?\s*([^\s;]+@[^\s;]+)",
                            re.I | re.M)
DIAG_ADDR_RE = re.compile(r"<([^\s<>]+@[^\s<>]+)>")
# "Your message ... couldn't be delivered to X" style text, as a last resort.
BOUNCE_TEXT_RE = re.compile(r"(?:to|address)\s+<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?", re.I)
UNSUB_RE = re.compile(r"\b(unsubscribe|remove me|take me off|opt.?out|stop (?:emailing|contacting)|do not (?:email|contact))\b",
                      re.I)


def credentials():
    """Sender identity plus the app password. Fails loudly rather than silently
    doing nothing -- a bot that quietly sends zero mail is worse than one that stops."""
    sd = ss.sender()  # already exits if SENDER_NAME / SENDER_EMAIL are unset
    pw = ss.cfg("GMAIL_APP_PASSWORD", "") or ss.cfg("SMTP_PASSWORD", "")
    if not pw:
        sys.exit(
            "ERROR: no mailbox password. Create a Gmail app password at\n"
            "  https://myaccount.google.com/apppasswords\n"
            "and set GMAIL_APP_PASSWORD in .env (2-Step Verification must be on first).")
    return sd, pw


def build_message(to_addr, subject, body, sd):
    """An RFC-5322 message with the headers that keep a cold send deliverable.

    List-Unsubscribe / List-Unsubscribe-Post give the recipient a one-click opt-out
    in Gmail's own UI. They are required for bulk senders and they are the single
    cheapest thing you can do for inbox placement -- a one-click unsubscribe costs
    one lead, a spam complaint costs the whole domain."""
    msg = email.message.EmailMessage()
    msg["From"] = email.utils.formataddr((sd["SENDER_NAME"], sd["SENDER_EMAIL"]))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sd["SENDER_EMAIL"].split("@", 1)[1])
    msg["Reply-To"] = sd["SENDER_EMAIL"]
    msg["List-Unsubscribe"] = f'<mailto:{sd["SENDER_EMAIL"]}?subject=unsubscribe>'
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)
    return msg


class Mailbox:
    """IMAP + SMTP against one mailbox. Use as a context manager."""

    def __init__(self):
        self.sd, self._pw = credentials()
        self.address = self.sd["SENDER_EMAIL"]
        self._imap = None

    # -- connections ---------------------------------------------------------
    def imap(self):
        if self._imap is None:
            self._imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            self._imap.login(self.address, self._pw)
        return self._imap

    def close(self):
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- outbound ------------------------------------------------------------
    def create_draft(self, to_addr, subject, body):
        """Append a real draft to the mailbox. Returns the draft's Message-ID."""
        msg = build_message(to_addr, subject, body, self.sd)
        imap = self.imap()
        stamp = imaplib.Time2Internaldate(datetime.now(timezone.utc).timestamp())
        typ, _ = imap.append(DRAFTS_FOLDER, "\\Draft", stamp, msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"IMAP APPEND to {DRAFTS_FOLDER} failed: {typ}")
        return msg["Message-ID"]

    def send(self, to_addr, subject, body):
        """Send for real over SMTP. Returns the sent Message-ID."""
        msg = build_message(to_addr, subject, body, self.sd)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(self.address, self._pw)
            s.send_message(msg)
        return msg["Message-ID"]

    # -- inbound -------------------------------------------------------------
    def _search_since(self, days):
        imap = self.imap()
        imap.select(INBOX_FOLDER, readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, "SINCE", since)
        if typ != "OK":
            return []
        return data[0].split()

    def _fetch(self, uid):
        typ, data = self.imap().fetch(uid, "(RFC822)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def scan_inbox(self, days=7):
        """Read recent inbox mail once and classify it.

        Returns (replies, bounces, unsubscribes) as sets of lowercase addresses.
        One pass, because fetching the same messages three times is three times
        the IMAP round trips for no extra information."""
        replies, bounces, unsubs = set(), set(), set()
        for uid in self._search_since(days):
            msg = self._fetch(uid)
            if msg is None:
                continue
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            subject = msg.get("Subject", "") or ""
            body = _body_text(msg)

            if _is_bounce(from_addr, subject, msg):
                dead = _bounced_address(msg, body)
                if dead:
                    bounces.add(dead.lower())
                continue

            if not from_addr or from_addr == self.address.lower():
                continue

            # An unsubscribe is still a reply, but it is the stronger signal, so
            # it wins: both stop the cadence, only this one suppresses forever.
            if UNSUB_RE.search(subject) or UNSUB_RE.search(body[:2000]):
                unsubs.add(from_addr)
            elif not DAEMON_RE.search(from_addr):
                replies.add(from_addr)
        return replies, bounces, unsubs


def _body_text(msg):
    """Plain-text body, best effort, whatever the MIME shape."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    parts.append(part.get_payload(decode=True).decode("utf-8", "replace"))
                except Exception:
                    continue
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode("utf-8", "replace"))
        except Exception:
            pass
    return "\n".join(parts)


def _is_bounce(from_addr, subject, msg):
    if msg.get_content_type() == "multipart/report":
        return True
    if DAEMON_RE.search(from_addr or ""):
        return True
    return bool(re.search(r"(undeliverable|delivery (status|has failed|failure)|returned mail"
                          r"|address not found|mail delivery failed)", subject or "", re.I))


def _bounced_address(msg, body):
    """The address that actually failed, which is almost never in the From header."""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("message/delivery-status", "text/plain", "message/rfc822"):
            try:
                text = part.get_payload(decode=True).decode("utf-8", "replace")
            except Exception:
                continue
            m = FINAL_RECIP_RE.search(text)
            if m:
                return m.group(1).strip("<>")
    m = FINAL_RECIP_RE.search(body)
    if m:
        return m.group(1).strip("<>")
    # Gmail puts the address in the human-readable part when there's no DSN.
    for candidate in DIAG_ADDR_RE.findall(body) + BOUNCE_TEXT_RE.findall(body):
        if not DAEMON_RE.search(candidate):
            return candidate
    return ""


def check():
    """`python3 emailer/mailbox.py` -- verify credentials without sending anything."""
    sd, _ = credentials()
    print(f"sender:  {sd['SENDER_NAME']} <{sd['SENDER_EMAIL']}>")
    print(f"imap:    {IMAP_HOST}:{IMAP_PORT}  drafts={DRAFTS_FOLDER}")
    print(f"smtp:    {SMTP_HOST}:{SMTP_PORT}")
    with Mailbox() as mb:
        imap = mb.imap()
        typ, _ = imap.select(INBOX_FOLDER, readonly=True)
        print(f"login:   OK (inbox select {typ})")
        typ, _ = imap.select(DRAFTS_FOLDER, readonly=True)
        print(f"drafts:  {typ} ({DRAFTS_FOLDER})")
    print("\nMailbox is reachable. Nothing was sent.")


if __name__ == "__main__":
    check()
