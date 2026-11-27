"""Mail settings and ticket e-mail composition."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate

from pydantic_settings import BaseSettings, SettingsConfigDict

REASONS = {
    "no_basis": "в регламентах нет ответа на вопрос",
    "external_doc": "для ответа нужен регламент, которого нет в базе",
    "conflict": "положения регламентов противоречат друг другу",
    "low_confidence": "ответ найден, но уверенность в нём недостаточна",
    "clarify_exhausted": "не удалось уточнить вопрос за три попытки",
    "user_dispute": "пользователь не согласен с ответом",
}


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAIL_", extra="ignore")

    host: str = "localhost"
    port: int = 1025
    from_addr: str = "reglament-agent@demo.local"
    service_email: str = "quality@demo.local"
    use_tls: bool = False
    timeout: float = 10.0
    max_attempts: int = 8
    backoff_base: float = 30.0  # seconds; delay = base * 2**(attempts-1), capped at one hour
    public_base_url: str = "http://localhost:8000"
    # Ответ эксперта по почте (ADR-015): агент сам забирает «ответить всем» из ящика службы качества.
    reply_enabled: bool = False  # MAIL_REPLY_ENABLED
    reply_interval: float = 5.0  # MAIL_REPLY_INTERVAL, seconds between polls (cheap: one local HTTP call)
    reply_source: str = "mailpit"  # MAIL_REPLY_SOURCE: mailpit | imap
    mailpit_url: str = "http://localhost:8025"  # MAIL_MAILPIT_URL
    quality_addresses: str = ""  # MAIL_QUALITY_ADDRESSES: comma list of who may answer; empty = any sender except the user (warned)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"


def message_id_for(number: str, kind: str = "ticket_created") -> str:
    suffix = "" if kind == "ticket_created" else f".{kind}"
    return f"<ticket-{number}{suffix}@reglament-agent.local>"


def subject_for(number: str, question: str) -> str:
    return f"[Обращение №{number}] {' '.join(question.split())[:60]}"


def build_body(*, number: str, question: str, restated: str | None, reason: str, user_email: str,
               profile: dict, clarifications: list[dict], candidates: list[dict],
               public_base_url: str) -> str:
    lines = [f"Обращение №{number}", "", "Вопрос:", question, ""]
    if restated:
        lines += ["Переформулировка:", restated, ""]
    lines += [f"Причина обращения: {REASONS.get(reason, reason)}", "",
              f"Пользователь: {user_email}"]
    prof = ", ".join(f"{k}: {v}" for k, v in (profile or {}).items() if v)
    lines.append(f"Профиль: {prof or 'не указан'}")
    if clarifications:
        lines += ["", "Уточнения:"]
        for i, c in enumerate(clarifications, 1):
            lines.append(f"{i}. {c.get('question', '')} — {c.get('answer', '')}")
    if candidates:
        lines += ["", "Найденные пункты регламентов:"]
        for c in candidates:
            line = f"- {c.get('title') or c.get('doc_id')}, п. {c.get('point')}"
            if version := c.get("version_no") or c.get("version"):
                line += f" (версия {version})"
            if isinstance(c.get("score"), (int, float)):
                line += f", релевантность {c['score']:.2f}"
            lines.append(line)
    lines += ["", f"Ответить: {public_base_url.rstrip('/')}/admin/tickets/{number}", ""]
    return "\n".join(lines)


def to_message(row: dict, from_addr: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = row["to_addr"]
    if row["cc_addr"]:
        msg["Cc"] = row["cc_addr"]
    msg["Subject"] = row["subject"]
    msg["Message-ID"] = row["message_id_header"]
    msg["Date"] = formatdate(localtime=False)
    msg.set_content(row["body"])
    return msg
