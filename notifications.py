from __future__ import annotations

import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from flask import current_app


def send_telegram_message(chat_id: str, text: str) -> bool:
    """Send a Telegram message via Bot API."""
    token = current_app.config.get("TELEGRAM_BOT_TOKEN")
    if not current_app.config.get("TELEGRAM_BOT_ENABLED") or not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")

    try:
        request = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                current_app.logger.warning("Telegram returned non-200 status: %s", response.status)
                return False
        return True
    except urllib.error.URLError as exc:
        current_app.logger.exception("Telegram notification failed: %s", exc)
        return False


def send_email_message(to_email: str, subject: str, body: str) -> bool:
    """Send a plain text email through configured SMTP server."""
    mail_server = current_app.config.get("MAIL_SERVER")
    if not mail_server:
        current_app.logger.info("MAIL_SERVER is not configured, skip email notification")
        return False

    sender = (
        current_app.config.get("MAIL_DEFAULT_SENDER")
        or current_app.config.get("MAIL_USERNAME")
        or "noreply@localhost"
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message.set_content(body)

    try:
        smtp = smtplib.SMTP(mail_server, current_app.config.get("MAIL_PORT", 25), timeout=15)
        with smtp:
            if current_app.config.get("MAIL_USE_TLS"):
                smtp.starttls()
            if current_app.config.get("MAIL_USERNAME"):
                smtp.login(
                    current_app.config.get("MAIL_USERNAME"),
                    current_app.config.get("MAIL_PASSWORD"),
                )
            smtp.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Email notification failed: %s", exc)
        return False


def notify_contact(
    *,
    email: str | None,
    telegram: str | None,
    subject: str,
    body: str,
) -> str | None:
    """Notify recipient using Telegram first, then Email."""
    if telegram:
        if send_telegram_message(telegram, body):
            return "telegram"

    if email:
        plain_body = body.replace("<b>", "").replace("</b>", "")
        if send_email_message(email, subject, plain_body):
            return "email"

    return None


def notify_user(user, subject: str, body: str) -> str | None:
    """Notify user-like object with fields: active, email, telegram_chat_id."""
    if not user:
        return None
    if hasattr(user, "active") and not getattr(user, "active"):
        return None
    return notify_contact(
        email=getattr(user, "email", None),
        telegram=getattr(user, "telegram_chat_id", None),
        subject=subject,
        body=body,
    )


def notify_employee(employee, subject: str, body: str) -> str | None:
    """Notify employee-like object with fields: is_active, email, telegram."""
    if not employee:
        return None
    if hasattr(employee, "is_active") and not getattr(employee, "is_active"):
        return None
    return notify_contact(
        email=getattr(employee, "email", None),
        telegram=getattr(employee, "telegram", None),
        subject=subject,
        body=body,
    )
