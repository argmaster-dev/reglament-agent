"""Quality service: tickets for questions the regulations cannot answer, and the mail outbox."""
from .mail import MailSettings
from .outbox import OutboxSender
from .tickets import create_ticket, get_ticket, get_ticket_for_user, list_user_tickets

__all__ = ["MailSettings", "OutboxSender", "create_ticket", "get_ticket", "get_ticket_for_user",
           "list_user_tickets"]
