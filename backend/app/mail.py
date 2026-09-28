"""Sending email.

One function, send(message), and two backends chosen by settings.mail_backend:

- outbox (development, tests): each message is written as NAME.eml into
  settings.outbox_dir, where any mail client opens it exactly as it would
  arrive. Nothing leaves the machine.
- smtp (production): the provider's SMTP relay. Postmark, SendGrid and
  Mailgun all offer one, so no provider-specific API is needed.
"""
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from app.config import settings


class MailError(RuntimeError):
    pass


def build_message(
    *, to: str, subject: str, text: str, html: str, headers: dict[str, str] | None = None
) -> EmailMessage:
    """A multipart message: plain text for every client, HTML for those that
    show it. Both carry the same content."""
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    domain = settings.mail_from.rsplit("@", 1)[-1].rstrip(">").strip() or "localhost"
    message["Message-ID"] = make_msgid(domain=domain)
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def _write_to_outbox(message: EmailMessage) -> None:
    outbox = Path(settings.outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    recipient = message["To"].replace("@", "_at_").replace("/", "_")
    (outbox / f"{stamp}-{recipient}-{uuid.uuid4().hex[:6]}.eml").write_bytes(message.as_bytes())


def _send_smtp(message: EmailMessage) -> None:
    if not settings.smtp_host:
        raise MailError("MAIL_BACKEND=smtp needs SMTP_HOST")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"sending to {message['To']} failed: {exc}") from exc


def send(message: EmailMessage) -> None:
    if settings.mail_backend == "smtp":
        _send_smtp(message)
    else:
        _write_to_outbox(message)
