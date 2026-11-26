"""Mail outbox sender: at-least-once delivery with exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import smtplib

import psycopg
from psycopg.rows import dict_row

from .mail import MailSettings, to_message

log = logging.getLogger(__name__)


class OutboxSender:
    def __init__(self, dsn: str, smtp_settings: MailSettings | None = None):
        self.dsn = dsn
        self.settings = smtp_settings or MailSettings()

    async def run_once(self, batch: int = 20) -> int:
        """Send due rows; returns the number actually sent."""
        return await asyncio.to_thread(self._run_once, batch)

    async def run_forever(self, interval: float = 5.0) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox iteration failed")
            await asyncio.sleep(interval)

    def _send(self, row: dict) -> None:
        s = self.settings
        msg = to_message(row, s.from_addr)
        with smtplib.SMTP(s.host, s.port, timeout=s.timeout) as smtp:
            if s.use_tls:
                smtp.starttls()
            smtp.send_message(msg, from_addr=s.from_addr,
                              to_addrs=[a for a in (row["to_addr"], row["cc_addr"]) if a])

    def _run_once(self, batch: int) -> int:
        sent = 0
        with psycopg.connect(self.dsn) as conn:
            # One transaction per row keeps the row lock while sending, so a concurrent
            # sender skips it (SKIP LOCKED) and never mails the same row twice.
            while True:
                with conn.transaction():
                    row = conn.cursor(row_factory=dict_row).execute(
                        "SELECT * FROM mail_outbox WHERE status IN ('pending','error') "
                        "AND attempts < %s AND next_attempt_at <= now() "
                        "ORDER BY next_attempt_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
                        (self.settings.max_attempts,)).fetchone()
                    if row is None or batch <= 0:
                        return sent
                    batch -= 1
                    try:
                        self._send(row)
                    except Exception as exc:  # noqa: BLE001 - any failure is retried
                        attempts = row["attempts"] + 1
                        delay = min(self.settings.backoff_base * 2 ** (attempts - 1), 3600.0)
                        conn.execute(
                            "UPDATE mail_outbox SET status='error', attempts=%s, last_error=%s, "
                            "next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
                            (attempts, f"{type(exc).__name__}: {exc}"[:500], delay, row["id"]))
                    else:
                        conn.execute(
                            "UPDATE mail_outbox SET status='sent', attempts=attempts+1, "
                            "last_error=NULL, sent_at=now() WHERE id=%s", (row["id"],))
                        sent += 1
