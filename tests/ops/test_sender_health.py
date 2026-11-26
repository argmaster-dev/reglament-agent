from unittest.mock import MagicMock

import pytest

from reglament_agent.ops import sender_health


def test_sender_health_checks_outbox_without_sending_mail(monkeypatch):
    db = MagicMock()
    smtp = MagicMock()
    db_connection = db.return_value.__enter__.return_value
    mail_connection = smtp.return_value.__enter__.return_value
    mail_connection.noop.return_value = (250, b"OK")
    monkeypatch.setenv("DATABASE_URL", "postgresql://sender@database/regulations")
    monkeypatch.setenv("MAIL_HOST", "mailpit")
    monkeypatch.setenv("MAIL_PORT", "1025")
    monkeypatch.setattr(sender_health.psycopg, "connect", db)
    monkeypatch.setattr(sender_health.smtplib, "SMTP", smtp)

    sender_health.check()

    db_connection.execute.assert_called_once_with("SELECT id FROM mail_outbox LIMIT 0")
    mail_connection.noop.assert_called_once_with()
    mail_connection.send_message.assert_not_called()

    mail_connection.noop.return_value = (421, b"Closing connection")
    with pytest.raises(sender_health.smtplib.SMTPException):
        sender_health.check()


def test_sender_health_reports_failure_when_queue_is_unavailable(monkeypatch):
    def no_connection(*args, **kwargs):
        raise sender_health.psycopg.OperationalError("database unavailable")

    monkeypatch.setenv("DATABASE_URL", "postgresql://sender@database/regulations")
    monkeypatch.setattr(sender_health.psycopg, "connect", no_connection)
    with pytest.raises(SystemExit, match="Sender readiness check failed: OperationalError"):
        sender_health.main()
