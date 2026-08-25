"""One-time clinic invitation delivery without logging or persisting raw tokens."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from urllib.parse import quote

from app.core.config import settings


def deliver_membership_invitation(*, recipient: str, token: str) -> None:
    """Deliver the raw one-time code directly to the verified email channel."""

    if not settings.emails_enabled or settings.SMTP_HOST is None:
        raise RuntimeError("Invitation email delivery is not configured")

    message = EmailMessage()
    message["Subject"] = "Your Nightingale clinic invitation"
    message["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
    message["To"] = recipient
    acceptance_url = (
        f"{str(settings.FRONTEND_HOST).rstrip('/')}/accept-invitation#"
        f"{quote(token, safe='')}"
    )
    message.set_content(
        "A clinic invited you to Nightingale. Enter this one-time code in the "
        "invitation acceptance form. The code expires in 24 hours:\n\n"
        f"{token}\n\nOr open this fragment-only link (the code is not sent in an "
        f"HTTP request):\n{acceptance_url}\n\n"
        "If you did not expect this invitation, ignore this message."
    )

    smtp_type: type[smtplib.SMTP] = (
        smtplib.SMTP_SSL if settings.SMTP_SSL else smtplib.SMTP
    )
    with smtp_type(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
        if settings.SMTP_TLS and not settings.SMTP_SSL:
            smtp.starttls()
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(message)
