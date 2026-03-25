"""
Notifications: email (SMTP) and optional ntfy.sh push.
Price-drop alerts go to the user's registered email when SMTP is configured.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "hotel-price-tracker")

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
# Port 465 typically uses implicit TLS (SMTP_SSL); 587 uses STARTTLS.
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"} or SMTP_PORT == 465
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER or "").strip()


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and MAIL_FROM and SMTP_USER and SMTP_PASSWORD)


def get_smtp_status() -> dict:
    """Expose for API/UI: whether outbound SMTP is configured on this server."""
    return {"smtp_configured": _smtp_configured()}


def send_email(to_addr: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on success."""
    if not _smtp_configured():
        logger.warning("[Notifier] SMTP not fully configured; cannot send email to %s", to_addr)
        return False
    if not to_addr or "@" not in to_addr:
        logger.warning("[Notifier] Invalid recipient: %s", to_addr)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                if SMTP_USE_TLS:
                    smtp.starttls()
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        logger.info("[Notifier] Email sent to %s", to_addr)
        return True
    except Exception as e:
        logger.error("[Notifier] Email failed: %s", e)
        return False


def send_password_reset_email(to_addr: str, reset_url: str) -> bool:
    body = (
        "You asked to reset your password for Astral Hotels Price Tracker.\n\n"
        f"Open this link to set a new password (valid for 1 hour):\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email.\n"
    )
    return send_email(to_addr, "Astral Hotels Price Tracker — password reset", body)


def send_check_now_email(
    *,
    user_email: str | None,
    label: str,
    url: str,
    currency: str,
    current_price: float,
    previous_price: float | None,
    trend: str | None,
) -> dict:
    """
    Manual "Check now" email. This is sent even when alert rules do not qualify,
    to help users verify SMTP and see a friendly trend message.
    """
    result: dict = {
        "smtp_configured": _smtp_configured(),
        "email_sent": False,
        "email_skip_reason": None,
    }
    if not user_email:
        result["email_skip_reason"] = "no_user_email"
        return result
    if not _smtp_configured():
        result["email_skip_reason"] = "smtp_not_configured"
        return result

    title = f"Astral price update: {label}"

    if previous_price is None or trend is None:
        headline = f"Baseline saved: {currency}{current_price:.2f}"
        lines = [
            headline,
            "",
            "This is your first successful check for this tracker.",
            "Run Check now again later to see if the price changed.",
            "",
            f"Open: {url}",
        ]
    else:
        if trend == "down":
            headline = f"Good news — price dropped to {currency}{current_price:.2f}"
        elif trend == "up":
            headline = f"Heads up — price increased to {currency}{current_price:.2f}"
        else:
            headline = f"No change — still {currency}{current_price:.2f}"

        delta = current_price - previous_price
        sign = "+" if delta > 0 else ""
        lines = [
            headline,
            "",
            f"Previous: {currency}{previous_price:.2f}",
            f"Current:  {currency}{current_price:.2f}",
            f"Change:   {sign}{currency}{delta:.2f}",
            "",
            f"Open: {url}",
        ]

    body = "\n".join(lines)
    if send_email(user_email, title, body):
        result["email_sent"] = True
    else:
        result["email_skip_reason"] = "send_failed"
    return result


def send_price_alert(
    label: str,
    current_price: float,
    currency: str,
    url: str,
    *,
    alert_kind: str = "drop",
    reference_price: float | None = None,
    previous_price: float | None = None,
    savings: float | None = None,
    comparison_label: str = "You paid",
    packages: list | None = None,
    user_email: str | None = None,
    ntfy_topic: str | None = None,
) -> dict:
    """
    Email + optional ntfy for drop, rise, or any price change vs last check.
    """
    result: dict = {
        "smtp_configured": _smtp_configured(),
        "email_sent": False,
        "email_skip_reason": None,
    }

    lines: list[str] = [f"Hotel: {label}", ""]

    if alert_kind == "change":
        title = f"Price changed: {label}"
        if previous_price is not None:
            lines.append(f"Previous price: {currency}{previous_price:.2f}")
        lines.append(f"Current price: {currency}{current_price:.2f}")
        lines.append("")
    elif alert_kind == "rise":
        title = f"Price increased: {label}"
        if previous_price is not None:
            lines.append(f"Was: {currency}{previous_price:.2f}")
        lines.append(f"Now: {currency}{current_price:.2f}")
        if previous_price is not None:
            lines.append(f"Change: +{currency}{(current_price - previous_price):.2f}")
        lines.append("")
    else:
        title = f"Price drop: {label}"
        lines.append(f"New price: {currency}{current_price:.2f}")
        if reference_price is not None:
            lines.append(f"{comparison_label}: {currency}{reference_price:.2f}")
        if savings is not None:
            lines.append(f"Potential saving: {currency}{savings:.2f}")
        lines.append("")

    if packages and alert_kind == "drop":
        best = packages[0]
        extras = []
        if best.get("breakfast"):
            extras.append("Breakfast included")
        if best.get("free_cancel"):
            extras.append("Free cancellation")
        if extras:
            lines.append("Package: " + " | ".join(extras))
            lines.append("")
    lines.append(f"Book / check: {url}")

    body = "\n".join(lines)

    if not user_email:
        result["email_skip_reason"] = "no_user_email"
    elif not _smtp_configured():
        result["email_skip_reason"] = "smtp_not_configured"
        logger.warning(
            "[Notifier] No email to %s: set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM in .env",
            user_email,
        )
    elif send_email(user_email, title, body):
        result["email_sent"] = True
    else:
        result["email_skip_reason"] = "send_failed"

    if ntfy_topic:
        _send_ntfy(title, body.replace("\n", "  "), url, topic=ntfy_topic)

    return result


def send_price_drop_alert(
    label: str,
    current_price: float,
    reference_price: float,
    savings: float,
    currency: str,
    url: str,
    packages: list | None = None,
    *,
    comparison_label: str = "You paid",
    user_email: str | None = None,
    ntfy_topic: str | None = None,
) -> dict:
    """Backward-compatible wrapper for drop-only alerts."""
    return send_price_alert(
        label=label,
        current_price=current_price,
        currency=currency,
        url=url,
        alert_kind="drop",
        reference_price=reference_price,
        previous_price=None,
        savings=savings,
        comparison_label=comparison_label,
        packages=packages,
        user_email=user_email,
        ntfy_topic=ntfy_topic,
    )


def send_test_notification_email(to_addr: str | None) -> bool:
    """Send a test email to verify SMTP (and optionally still ping ntfy)."""
    if to_addr and _smtp_configured():
        return send_email(
            to_addr,
            "Astral Hotels Price Tracker: test email",
            "Your email notifications are configured correctly.",
        )
    return False


def send_test_notification(topic: str | None = None):
    """Send a test ping to ntfy (legacy)."""
    _send_ntfy(
        title="Astral Hotels Price Tracker",
        message="Your tracker is connected and monitoring Astral booking prices.",
        url=None,
        topic=topic,
        priority="default",
        tags=["white_check_mark"],
    )


def _send_ntfy(
    title: str,
    message: str,
    url: str | None,
    topic: str | None = None,
    priority: str = "default",
    tags: list | None = None,
):
    topic = topic or NTFY_TOPIC
    endpoint = f"{NTFY_SERVER}/{topic}"

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": ",".join(tags or []),
    }
    if url:
        headers["Actions"] = f"view, Rebook Now, {url}, clear=true"
        headers["Click"] = url

    try:
        resp = requests.post(endpoint, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info("[Notifier] ntfy alert sent to topic '%s'.", topic)
    except requests.RequestException as e:
        logger.error("[Notifier] ntfy failed: %s", e)
