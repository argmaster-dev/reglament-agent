"""Expert answers by e-mail: read the quality-service mailbox, match replies to tickets, publish.

The expert answers the ticket letter with an ordinary «Reply all». `ReplyPoller` reads new letters
through a `MailboxReader` (Mailpit HTTP API on the stand, IMAP in production), matches each to a
ticket (In-Reply-To / References first, ticket number in the subject second), takes the expert's
own words (`reply_text`) and delivers them through the same code as the cabinet (`answer_ticket`).
A later reply replaces the earlier answer and its FAQ entry (`replace_ticket_answer`).
Every Message-ID is recorded in `mail_replies` first, so a re-delivered letter is processed once.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass, field
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Callable, Protocol

import httpx
import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from reglament_agent.ingest.embedding import Embedder, make_embedder

from .faq import TicketError, answer_ticket, replace_ticket_answer
from .mail import MailSettings
from .reply_text import extract_answer

log = logging.getLogger(__name__)

OWN_MESSAGE_ID = re.compile(r"@reglament-agent\.local>?$")
TICKET_NUMBER = re.compile(r"(?<![\d-])(\d{4}-\d{4,})(?![\d-])")


@dataclass
class ParsedMail:
    message_id: str
    from_addr: str
    subject: str
    text: str = ""
    html: str = ""
    in_reply_to: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    date: dt.datetime | None = None


class MailboxReader(Protocol):
    def fetch_new(self, is_known: Callable[[str], bool] = lambda _id: False) -> list[ParsedMail]:
        """Letters of the mailbox, oldest first; `is_known(message_id)` lets the reader skip heavy fetches."""
        ...


def parse_raw(raw: bytes) -> ParsedMail:
    msg = email.message_from_bytes(raw, policy=policy.default)

    def part(kind: str) -> str:
        body = msg.get_body(preferencelist=(kind,))
        try:
            return body.get_content() if body is not None else ""
        except (LookupError, UnicodeError):
            return ""

    ids = lambda name: re.findall(r"<[^<>\s]+>", " ".join(str(v) for v in msg.get_all(name, [])))  # noqa: E731
    date = None
    try:
        date = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    mid = (str(msg["Message-ID"] or "").strip()) or "<nomid-" + hashlib.sha256(raw).hexdigest()[:32] + ">"
    return ParsedMail(message_id=mid, from_addr=parseaddr(str(msg["From"] or ""))[1].lower(),
                      subject=" ".join(str(msg["Subject"] or "").split()), text=part("plain"), html=part("html"),
                      in_reply_to=ids("In-Reply-To"), references=ids("References"), date=date)


class MailpitReader:
    """Mailpit HTTP API: GET /api/v1/messages, then GET /api/v1/message/{ID}/raw for unknown letters."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None, limit: int = 200, timeout: float = 10.0):
        self.client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.base = "" if client is None else base_url.rstrip("/")
        self.limit = limit

    def fetch_new(self, is_known: Callable[[str], bool] = lambda _id: False) -> list[ParsedMail]:
        r = self.client.get(f"{self.base}/api/v1/messages", params={"limit": self.limit})
        r.raise_for_status()
        out = []
        for item in r.json().get("messages", []):
            if item.get("MessageID") and is_known(f"<{item['MessageID'].strip('<>')}>"):
                continue
            raw = self.client.get(f"{self.base}/api/v1/message/{item['ID']}/raw")
            raw.raise_for_status()
            out.append(parse_raw(raw.content))
        return _oldest_first(out)


class ImapReader:
    """IMAP mailbox (production). Reads without changing flags (BODY.PEEK); dedup is done by Message-ID."""

    def __init__(self, host: str, user: str, password: str, *, port: int = 993, folder: str = "INBOX",
                 limit: int = 200, connect=None):
        self.host, self.user, self.password, self.port = host, user, password, port
        self.folder, self.limit = folder, limit
        self.connect = connect or (lambda: imaplib.IMAP4_SSL(self.host, self.port))

    def fetch_new(self, is_known: Callable[[str], bool] = lambda _id: False) -> list[ParsedMail]:
        imap = self.connect()
        try:
            imap.login(self.user, self.password)
            imap.select(self.folder, readonly=True)
            _, data = imap.uid("SEARCH", None, "ALL")
            uids = (data[0] or b"").split()[-self.limit:]
            out = []
            for uid in uids:
                _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw is None:
                    continue
                mail = parse_raw(raw)
                if not is_known(mail.message_id):
                    out.append(mail)
            return _oldest_first(out)
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass


def _oldest_first(mails: list[ParsedMail]) -> list[ParsedMail]:
    far = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return sorted(mails, key=lambda m: m.date or far)  # stable: arrival order breaks ties


def make_reader(s: MailSettings) -> MailboxReader:
    if s.reply_source == "imap":
        return ImapReader(s.imap_host, s.imap_user, s.imap_password, port=s.imap_port, folder=s.imap_folder)
    if s.reply_source == "mailpit":
        return MailpitReader(s.mailpit_url)
    raise ValueError(f"Unknown MAIL_REPLY_SOURCE: {s.reply_source}")


def _find_ticket(cur, mail: ParsedMail) -> dict | None:
    refs = list(dict.fromkeys([*mail.in_reply_to, *mail.references]))
    if refs:
        row = cur.execute("SELECT t.* FROM mail_outbox o JOIN tickets t ON t.id=o.ticket_id "
                          "WHERE o.message_id_header = ANY(%s) ORDER BY o.id DESC LIMIT 1", (refs,)).fetchone()
        if row:
            return row
    for number in TICKET_NUMBER.findall(mail.subject):
        row = cur.execute("SELECT * FROM tickets WHERE number=%s", (number,)).fetchone()
        if row:
            return row
    return None


def process_reply(conn: Connection, mail: ParsedMail, settings: MailSettings, embedder: Embedder | None) -> str:
    """Handle one letter in one transaction; returns the outcome (also stored in `mail_replies`)."""
    sender = mail.from_addr.strip().lower()
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        claimed = cur.execute("INSERT INTO mail_replies (message_id, sender, subject, sent_at) VALUES (%s,%s,%s,%s) "
                              "ON CONFLICT (message_id) DO NOTHING RETURNING id",
                              (mail.message_id, sender, mail.subject, mail.date)).fetchone()
        if claimed is None:
            return "duplicate"

        def done(outcome: str, ticket: dict | None = None, text: str | None = None) -> str:
            cur.execute("UPDATE mail_replies SET outcome=%s, ticket_id=%s, answer_text=%s WHERE id=%s",
                        (outcome, ticket["id"] if ticket else None, text, claimed["id"]))
            return outcome

        if OWN_MESSAGE_ID.search(mail.message_id) or sender == settings.from_addr.lower():
            return done("own_mail")  # our own ticket letter / answer copy, also stored in the shared mailbox
        ticket = _find_ticket(cur, mail)
        if ticket is None:
            return done("unmatched")
        if sender == ticket["user_email"].lower():
            return done("user_reply", ticket)  # the employee's own letter is never an answer
        allowed = {a.strip().lower() for a in settings.quality_addresses.split(",") if a.strip()}
        if allowed and sender not in allowed:
            return done("sender_rejected", ticket)
        if ticket["status"] not in ("open", "answered"):
            return done("closed_ticket", ticket)
        text = extract_answer(mail.text, mail.html)
        if not text:
            return done("empty", ticket)
        if mail.date is not None:
            newer = cur.execute("SELECT 1 FROM mail_replies WHERE ticket_id=%s AND outcome IN ('answered','updated') "
                                "AND sent_at > %s LIMIT 1", (ticket["id"], mail.date)).fetchone()
            if newer:
                return done("superseded", ticket, text)  # a later reply was already processed
        try:
            if ticket["status"] == "answered":
                replace_ticket_answer(conn, number=ticket["number"], expert_email=sender, answer=text)
                return done("updated", ticket, text)
            return done(_publish(conn, ticket, sender, text, embedder), ticket, text)
        except TicketError as exc:  # permanent: remember the letter, do not retry it forever
            log.warning("reply %s rejected: %s", mail.message_id, exc)
            return done("rejected", ticket, text)


def _publish(conn: Connection, ticket: dict, sender: str, text: str, embedder: Embedder | None) -> str:
    question = (ticket["restated_question"] or ticket["question"]).strip()
    alts = [ticket["question"]] if ticket["question"].strip() != question else []
    answer_ticket(conn, number=ticket["number"], expert_email=sender, answer=text,
                  basis_kind="outside", basis_points=[], visibility="faq", faq_question=question,
                  alt_questions=alts, embedder=embedder,
                  department=(ticket["profile"] or {}).get("department"), source="email", notify_mail=False)
    return "answered"


class _LazyEmbedder:
    """Built on first use, so letters that need no FAQ entry never touch the model."""

    def __init__(self, factory: Callable[[], Embedder]):
        self.factory, self.inner = factory, None

    def embed(self, texts):
        if self.inner is None:
            self.inner = self.factory()
        return self.inner.embed(texts)


class ReplyPoller:
    def __init__(self, dsn: str, settings: MailSettings | None = None, reader: MailboxReader | None = None,
                 embedder_factory: Callable[[], Embedder] = make_embedder):
        self.dsn = dsn
        self.settings = settings or MailSettings()
        self.reader = reader or make_reader(self.settings)
        self.embedder_factory = embedder_factory

    def _run_once(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        embedder = _LazyEmbedder(self.embedder_factory)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            def known(mid: str) -> bool:
                return conn.execute("SELECT 1 FROM mail_replies WHERE message_id=%s", (mid,)).fetchone() is not None

            for mail in self.reader.fetch_new(known):
                try:
                    outcome = process_reply(conn, mail, self.settings, embedder)
                except Exception:  # noqa: BLE001 - not recorded, so the next poll retries the letter
                    log.exception("reply %s failed", mail.message_id)
                    continue
                counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def log_trust_policy(self) -> None:
        allowed = [a.strip() for a in self.settings.quality_addresses.split(",") if a.strip()]
        if allowed:
            log.info("expert replies: allow-list ON, answers accepted only from %s", ", ".join(allowed))
        else:
            log.warning("expert replies: allow-list OFF (MAIL_QUALITY_ADDRESSES is empty), any sender except "
                        "the ticket's user can publish an answer; set the quality-service and process-owner addresses")

    async def run_once(self) -> dict[str, int]:
        """One poll; returns {outcome: count}."""
        return await asyncio.to_thread(self._run_once)

    async def run_forever(self, interval: float | None = None, *, stop: asyncio.Event | None = None) -> None:
        stop = stop if stop is not None else asyncio.Event()
        interval = self.settings.reply_interval if interval is None else interval
        self.log_trust_policy()
        while not stop.is_set():
            try:
                counts = await self.run_once()
                if counts.get("answered") or counts.get("updated"):
                    log.info("expert replies processed: %s", counts)
            except Exception:
                log.exception("reply poll failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
