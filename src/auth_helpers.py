"""
Session-based auth helpers (used from app.py).
"""
from __future__ import annotations

import functools
import re
import uuid

from flask import jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

from database import create_user, get_user_by_email, get_user_by_id

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
        user = {k: v for k, v in user.items() if k != "password_hash"}
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
    return {k: v for k, v in user.items() if k != "password_hash"}, None


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
