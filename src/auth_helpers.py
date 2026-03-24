"""
Session-based auth helpers (used from app.py).
"""
from __future__ import annotations

import functools
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

from database import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_user_by_valid_reset_token,
    set_password_reset_token,
    update_user_password_clear_reset,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SENSITIVE = frozenset({"password_hash", "password_reset_token", "password_reset_expires"})


def public_user_row(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SENSITIVE}


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def validate_email(email: str) -> str | None:
    e = (email or "").strip().lower()
    if not e or not EMAIL_RE.match(e):
        return None
    return e


def register_user(email: str, password: str) -> tuple[dict | None, str | None]:
    if len(password) < 8:
        return None, "Password must be at least 8 characters."
    e = validate_email(email)
    if not e:
        return None, "Invalid email address."
    if get_user_by_email(e):
        return None, "An account with this email already exists."
    uid = str(uuid.uuid4())
    create_user(uid, e, hash_password(password))
    user = get_user_by_id(uid)
    if user:
        user = public_user_row(user)
    return user, None


def login_user(email: str, password: str) -> tuple[dict | None, str | None]:
    e = validate_email(email)
    if not e:
        return None, "Invalid email address."
    user = get_user_by_email(e)
    if not user:
        return None, "Account does not exist"
    if not verify_password(user["password_hash"], password):
        return None, "Incorrect password."
    return public_user_row(user), None


def start_password_reset(email: str, app_base_url: str) -> tuple[bool, str | None]:
    """
    Returns (allow_generic_success_message, error_code).
    When email is unknown, returns (True, None) to avoid account enumeration.
    """
    from notifier import get_smtp_status, send_password_reset_email

    if not get_smtp_status().get("smtp_configured"):
        return False, "smtp_not_configured"
    e = validate_email(email)
    if not e:
        return True, None
    user = get_user_by_email(e)
    if not user:
        return True, None
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    set_password_reset_token(user["id"], token, expires_at)
    link = f"{app_base_url.rstrip('/')}/?reset={token}"
    if not send_password_reset_email(user["email"], link):
        return False, "send_failed"
    return True, None


def complete_password_reset(token: str, new_password: str) -> tuple[dict | None, str | None]:
    if len(new_password) < 8:
        return None, "Password must be at least 8 characters."
    user = get_user_by_valid_reset_token(token)
    if not user:
        return None, "This reset link is invalid or has expired. Request a new one."
    update_user_password_clear_reset(user["id"], hash_password(new_password))
    refreshed = get_user_by_id(user["id"])
    return (public_user_row(refreshed) if refreshed else None), None


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return jsonify({"error": "Authentication required."}), 401
        user = get_user_by_id(uid)
        if not user:
            session.clear()
            return jsonify({"error": "Session invalid. Please sign in again."}), 401
        return fn(*args, **kwargs)

    return wrapper


def current_user_id() -> str | None:
    return session.get("user_id")
