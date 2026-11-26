"""Readiness for the Compose mail worker, without sending a message."""

import os
import smtplib

import psycopg

from reglament_agent.quality.mail import MailSettings


def check() -> None:
    settings = MailSettings()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2) as conn:
        conn.execute("SET LOCAL statement_timeout = '2s'")
        conn.execute("SELECT id FROM mail_outbox LIMIT 0")
    with smtplib.SMTP(settings.host, settings.port, timeout=2) as smtp:
        if settings.use_tls:
            smtp.starttls()
        code, _ = smtp.noop()
        if code != 250:
            raise smtplib.SMTPException("SMTP did not accept NOOP")


def main() -> None:
    try:
        check()
    except (KeyError, OSError, psycopg.Error) as error:
        raise SystemExit(f"Sender readiness check failed: {type(error).__name__}") from None


if __name__ == "__main__":
    main()
