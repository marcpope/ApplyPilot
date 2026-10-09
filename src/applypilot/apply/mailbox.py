"""Applicant mailbox access over IMAP/SMTP.

The auto-apply agent uses this (through apply/email_mcp.py) to read account
verification codes and links, and to send applications for email-only jobs.
Configured in ~/.applypilot/.env:

    APPLY_EMAIL=you@example.com
    APPLY_EMAIL_PASSWORD=...
    APPLY_EMAIL_IMAP_HOST=mail.example.com
    APPLY_EMAIL_SMTP_HOST=mail.example.com   # optional, defaults to the IMAP host
    APPLY_EMAIL_IMAP_PORT=993                # optional
    APPLY_EMAIL_SMTP_PORT=465                # optional; 587 uses STARTTLS
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup


@dataclass
class MailboxConfig:
    address: str
    password: str
    imap_host: str
    smtp_host: str
    imap_port: int = 993
    smtp_port: int = 465

    @classmethod
    def from_env(cls) -> "MailboxConfig | None":
        """Read the mailbox settings, or None when no applicant mailbox is configured."""
        address = os.environ.get("APPLY_EMAIL", "").strip()
        password = os.environ.get("APPLY_EMAIL_PASSWORD", "")
        imap_host = os.environ.get("APPLY_EMAIL_IMAP_HOST", "").strip()
        if not (address and password and imap_host):
            return None
        return cls(
            address=address,
            password=password,
            imap_host=imap_host,
            smtp_host=os.environ.get("APPLY_EMAIL_SMTP_HOST", "").strip() or imap_host,
            imap_port=int(os.environ.get("APPLY_EMAIL_IMAP_PORT", "993")),
            smtp_port=int(os.environ.get("APPLY_EMAIL_SMTP_PORT", "465")),
        )


@dataclass
class Message:
    id: str  # "<folder>:<uid>"
    sender: str
    subject: str
    received: datetime
    text: str = ""
    links: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"id": self.id, "from": self.sender, "subject": self.subject,
                "received": self.received.isoformat(), "snippet": self.text[:200]}

    def full(self) -> dict:
        return {"id": self.id, "from": self.sender, "subject": self.subject,
                "received": self.received.isoformat(), "verification_codes": self.codes,
                "verification_links": self.links, "text": self.text[:6000]}


# -- Parsing -----------------------------------------------------------------

_VERIFY_WORDS = ("verif", "confirm", "activat", "validat", "token", "magic", "login", "sign-in",
                 "signin", "reset", "account")
_CODE_CONTEXT = re.compile(
    # "code is 482913", "verification code: AB12CD", "OTP - 1234"
    r"(?:code|otp|pin|passcode|one[- ]time|verification)[^\n]{0,30}?\b((?=[A-Z0-9]*\d)[A-Z0-9]{4,8})\b"
    # "482913 is your code"
    r"|\b(\d{4,8})\b[^.\n]{0,40}(?:is your|code|otp|passcode)",
    re.I,
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message) -> tuple[str, list[tuple[str, str]]]:
    """Return (plain text, [(href, anchor text)]) for a message, preferring text/plain."""
    plain, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    hrefs: list[tuple[str, str]] = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [(a["href"], a.get_text(" ", strip=True)) for a in soup.find_all("a", href=True)]
        if not plain:
            plain = soup.get_text("\n")
    plain = re.sub(r"\n\s*\n+", "\n\n", plain).strip()
    return plain, hrefs


def extract_codes(text: str) -> list[str]:
    """Likely one-time codes, most likely first."""
    codes: list[str] = []
    for match in _CODE_CONTEXT.finditer(text):
        code = match.group(1) or match.group(2)
        # A code needs a digit; this skips words like "BELOW" after "code".
        if code and any(c.isdigit() for c in code) and code not in codes:
            codes.append(code)
    return codes


def extract_links(text: str, hrefs: list[tuple[str, str]]) -> list[str]:
    """Verification links first, then any other http(s) links (max 10).

    A link counts as a verification link when its URL or its button text
    says so: senders often wrap the real URL in a click-tracking redirect
    (click.sendgrid.net/...) whose only clue is the "Verify email" label.
    """
    anchors = {href: label for href, label in hrefs}
    found = [h for h, _ in hrefs] + re.findall(r"https?://[^\s<>\"')\]]+", text)
    urls = [u for u in dict.fromkeys(found) if u.startswith("http")]
    urls = [u for u in urls if "unsubscribe" not in (u + anchors.get(u, "")).lower()]

    def is_verify(url: str) -> bool:
        return any(w in f"{url} {anchors.get(url, '')}".lower() for w in _VERIFY_WORDS)

    verify = [u for u in urls if is_verify(u)]
    rest = [u for u in urls if u not in verify]
    return (verify + rest)[:10]


def parse_message(msg_id: str, raw: bytes) -> Message:
    msg = email.message_from_bytes(raw)
    try:
        received = parsedate_to_datetime(msg.get("Date"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
    except Exception:
        received = datetime.now(timezone.utc)
    text, hrefs = _body_text(msg)
    subject = _decode(msg.get("Subject"))
    return Message(
        id=msg_id, sender=_decode(msg.get("From")), subject=subject, received=received,
        text=text, links=extract_links(text, hrefs),
        # HTML-to-text splits inline tags onto separate lines ("code is\n<b>123456</b>").
        codes=extract_codes(" ".join(f"{subject}\n{text}".split())),
    )


# -- IMAP / SMTP ---------------------------------------------------------------

# One LIST response line: (flags) "delimiter" name
_LIST_LINE = re.compile(r'^\([^)]*\) (?:"(?:[^"\\]|\\.)*"|NIL) (.+)$')

class Mailbox:
    def __init__(self, cfg: MailboxConfig) -> None:
        self.cfg = cfg

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port, timeout=30)
        conn.login(self.cfg.address, self.cfg.password)
        return conn

    @staticmethod
    def _folders(conn: imaplib.IMAP4_SSL) -> list[str]:
        """INBOX plus spam/junk folders, where verification mail often lands."""
        names = ["INBOX"]
        for line in conn.list()[1] or []:
            match = _LIST_LINE.match(line.decode(errors="replace"))
            name = match.group(1).strip('"') if match else ""
            if name and name != "INBOX" and re.search(r"spam|junk|bulk", name, re.I):
                names.append(name)
        return names

    def search(self, query: str = "", since_minutes: int = 60, limit: int = 10) -> list[Message]:
        """Newest-first messages received in the window whose sender, subject or
        body contains ``query`` (case-insensitive)."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")  # IMAP SINCE is day-granular
        found: list[Message] = []
        conn = self._connect()
        try:
            for folder in self._folders(conn):
                if conn.select(f'"{folder}"', readonly=True)[0] != "OK":
                    continue
                _, data = conn.uid("search", None, "SINCE", since)
                for uid in reversed((data[0] or b"").split()[-50:]):
                    _, parts = conn.uid("fetch", uid, "(RFC822)")
                    raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                    if raw is None:
                        continue
                    msg = parse_message(f"{folder}:{uid.decode()}", raw)
                    if msg.received < cutoff:
                        continue
                    haystack = f"{msg.sender}\n{msg.subject}\n{msg.text}".lower()
                    if query.lower() in haystack:
                        found.append(msg)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        found.sort(key=lambda m: m.received, reverse=True)
        return found[:limit]

    def read(self, msg_id: str) -> Message | None:
        folder, _, uid = msg_id.rpartition(":")
        conn = self._connect()
        try:
            if conn.select(f'"{folder}"', readonly=True)[0] != "OK":
                return None
            _, parts = conn.uid("fetch", uid, "(RFC822)")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            return parse_message(msg_id, raw) if raw else None
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def wait_for(self, query: str = "", timeout_seconds: int = 180, since_minutes: int = 15,
                 poll_seconds: int = 10) -> Message | None:
        """Poll until a matching message arrives (or one already did in the window)."""
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            matches = self.search(query, since_minutes=since_minutes, limit=1)
            if matches:
                return matches[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_seconds)

    def send(self, to: str, subject: str, body: str, attachments: list[Path] | None = None) -> None:
        msg = EmailMessage()
        msg["From"] = self.cfg.address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        for path in attachments or []:
            msg.add_attachment(path.read_bytes(), maintype="application",
                               subtype="pdf" if path.suffix.lower() == ".pdf" else "octet-stream",
                               filename=path.name)
        context = ssl.create_default_context()
        if self.cfg.smtp_port == 465:
            with smtplib.SMTP_SSL(self.cfg.smtp_host, 465, timeout=30, context=context) as smtp:
                smtp.login(self.cfg.address, self.cfg.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.cfg.address, self.cfg.password)
                smtp.send_message(msg)
