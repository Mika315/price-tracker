import logging
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from flask import Flask, jsonify, request, send_from_directory, session

from astral_urls import astral_url_error_message, is_astral_booking_url
from auth_helpers import (
    complete_password_reset,
    login_required,
    login_user,
    register_user,
    start_password_reset,
)
from url_sanitize import sanitize_url as _sanitize_url
from database import (
    delete_tracker,
    get_all_trackers,
    get_last_price,
    get_price_history,
    get_tracker,
    get_user_by_id,
    init_db,
    save_price,
    upsert_tracker,
)
from notifier import (
    get_smtp_status,
    send_check_now_email,
    send_price_alert,
    send_test_notification,
    test_notification_email_reason,
)
from scheduler import (
    _alert_baseline,
    alert_kind_for_tracker,
    explain_price_alert_blocker,
    build_tracking_url,
    check_all_trackers,
    start_scheduler,
)
from scraper import scrape_price_and_packages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("tracker.log")],
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-change-me-set-SECRET_KEY-in-production")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.getenv("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}:
    app.config["SESSION_COOKIE_SECURE"] = True


def _user_id() -> str | None:
    return session.get("user_id")


_PRIVATE_USER_KEYS = frozenset({"password_hash", "password_reset_token", "password_reset_expires"})


def _public_user(u: dict | None) -> dict | None:
    if not u:
        return None
    return {k: v for k, v in u.items() if k not in _PRIVATE_USER_KEYS}


def _request_base_url() -> str:
    return (
        (os.getenv("PUBLIC_APP_URL") or "").strip()
        or (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
        or request.url_root.rstrip("/")
    )


def _ensure_astral_tracker_urls(data: dict) -> str | None:
    if not is_astral_booking_url(data.get("url") or ""):
        return astral_url_error_message()
    for u in data.get("alternative_urls") or []:
        if isinstance(u, str) and u.strip() and not is_astral_booking_url(u):
            return astral_url_error_message()
    return None


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _validate_dates(checkin: str | None, checkout: str | None):
    if not checkin or not checkout:
        return None
    try:
        in_date = datetime.strptime(checkin, "%Y-%m-%d")
        out_date = datetime.strptime(checkout, "%Y-%m-%d")
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD."

    if out_date <= in_date:
        return "Departure date must be after arrival date."
    return None


def _normalize_tracker_payload(payload: dict, existing_id: str | None = None) -> dict:
    data = dict(payload)

    if existing_id:
        data["id"] = existing_id
    elif not data.get("id"):
        data["id"] = str(uuid.uuid4())[:8]

    data["label"] = (data.get("label") or "").strip()
    data["url"] = _sanitize_url((data.get("url") or ""))

    meal_plan = (data.get("meal_plan") or "none").lower()
    if meal_plan not in {"none", "breakfast", "half_board", "full_board"}:
        meal_plan = "none"
    data["meal_plan"] = meal_plan

    data["require_free_cancel"] = 1 if _to_bool(data.get("require_free_cancel")) else 0
    data["reserve_duty_only"] = 1 if _to_bool(data.get("reserve_duty_only")) else 0
    data["club_membership_only"] = 1 if _to_bool(data.get("club_membership_only")) else 0
    data["active"] = 1 if _to_bool(data.get("active", True)) else 0

    data["room_keyword"] = (data.get("room_keyword") or "").strip()
    data["currency"] = data.get("currency") or "₪"

    try:
        data["threshold_pct"] = float(data.get("threshold_pct") or 0)
    except (TypeError, ValueError):
        data["threshold_pct"] = 0

    alert_direction = (data.get("alert_direction") or "down").lower()
    if alert_direction not in {"any", "up", "down"}:
        alert_direction = "down"
    data["alert_direction"] = alert_direction

    paid_price = data.get("paid_price")
    try:
        data["paid_price"] = float(paid_price) if paid_price not in (None, "") else None
    except (TypeError, ValueError):
        data["paid_price"] = None

    if not isinstance(data.get("alternative_urls"), list):
        data["alternative_urls"] = []
    else:
        cleaned: list = []
        for item in data["alternative_urls"]:
            if isinstance(item, str):
                cleaned.append(_sanitize_url(item))
            else:
                cleaned.append(item)
        data["alternative_urls"] = cleaned

    return data


def _requirements_from_tracker(tracker: dict) -> dict:
    return {
        "meal_plan": tracker.get("meal_plan", "none"),
        "free_cancel": _to_bool(tracker.get("require_free_cancel")),
        "room_keyword": tracker.get("room_keyword", ""),
        "reserve_duty_only": _to_bool(tracker.get("reserve_duty_only")),
        "club_membership_only": _to_bool(tracker.get("club_membership_only")),
    }


# --- Auth ---


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    raw = request.json or {}
    user, err = register_user(raw.get("email") or "", raw.get("password") or "")
    if err:
        return jsonify({"error": err}), 400
    session["user_id"] = user["id"]
    session.permanent = bool(os.getenv("PERMANENT_SESSION", "true").lower() in {"1", "true", "yes"})
    return jsonify({"user": user})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    raw = request.json or {}
    user, err = login_user(raw.get("email") or "", raw.get("password") or "")
    if err:
        return jsonify({"error": err}), 401
    session["user_id"] = user["id"]
    session.permanent = bool(os.getenv("PERMANENT_SESSION", "true").lower() in {"1", "true", "yes"})
    return jsonify({"user": user})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    uid = _user_id()
    smtp = get_smtp_status()
    if not uid:
        return jsonify({"user": None, **smtp})
    u = _public_user(get_user_by_id(uid))
    return jsonify({"user": u, **smtp})


@app.route("/api/auth/forgot-password", methods=["POST"])
def auth_forgot_password():
    raw = request.json or {}
    ok, err = start_password_reset(raw.get("email") or "", _request_base_url())
    if not ok and err == "smtp_not_configured":
        return jsonify(
            {"error": "Password reset is unavailable: outgoing email (SMTP) is not configured on this server."}
        ), 503
    if not ok and err == "send_failed":
        return jsonify({"error": "Could not send the reset email. Try again later."}), 502
    return jsonify(
        {
            "message": "If an account exists for that email, we sent a reset link. Check your inbox and spam folder.",
        }
    )


@app.route("/api/auth/reset-password", methods=["POST"])
def auth_reset_password():
    raw = request.json or {}
    token = (raw.get("token") or "").strip()
    password = raw.get("password") or ""
    user, err = complete_password_reset(token, password)
    if err:
        return jsonify({"error": err}), 400
    if not user:
        return jsonify({"error": "Reset failed."}), 500
    session["user_id"] = user["id"]
    session.permanent = bool(os.getenv("PERMANENT_SESSION", "true").lower() in {"1", "true", "yes"})
    return jsonify({"user": user})


# --- App ---


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/trackers", methods=["GET"])
@login_required
def list_trackers():
    uid = _user_id()
    result = []
    for tracker in get_all_trackers(uid):
        history = get_price_history(tracker["id"], limit=30)
        tracker["current_price"] = history[0]["price"] if history else None
        tracker["previous_price"] = history[1]["price"] if len(history) > 1 else None
        tracker["history"] = history
        result.append(tracker)
    return jsonify(result)


@app.route("/api/trackers", methods=["POST"])
@login_required
def add_tracker():
    uid = _user_id()
    raw = request.json or {}
    data = _normalize_tracker_payload(raw)
    data["user_id"] = uid

    if not data.get("url"):
        return jsonify({"error": "URL is required."}), 400

    date_error = _validate_dates(data.get("checkin"), data.get("checkout"))
    if date_error:
        return jsonify({"error": date_error}), 400

    astral_err = _ensure_astral_tracker_urls(data)
    if astral_err:
        return jsonify({"error": astral_err}), 400

    try:
        upsert_tracker(data)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    logger.info("[Tracker] Saved: %s - %s", data["id"], data.get("label") or data["url"])
    return jsonify({"status": "ok", "id": data["id"]})


@app.route("/api/trackers/<tid>", methods=["PUT"])
@login_required
def update_tracker(tid):
    uid = _user_id()
    if not get_tracker(uid, tid):
        return jsonify({"error": "Tracker not found."}), 404

    raw = request.json or {}
    data = _normalize_tracker_payload(raw, existing_id=tid)
    data["user_id"] = uid

    date_error = _validate_dates(data.get("checkin"), data.get("checkout"))
    if date_error:
        return jsonify({"error": date_error}), 400

    astral_err = _ensure_astral_tracker_urls(data)
    if astral_err:
        return jsonify({"error": astral_err}), 400

    try:
        upsert_tracker(data)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"status": "ok"})


@app.route("/api/trackers/<tid>", methods=["DELETE"])
@login_required
def remove_tracker(tid):
    uid = _user_id()
    if not delete_tracker(tid, uid):
        return jsonify({"error": "Tracker not found."}), 404
    logger.info("[Tracker] Deleted: %s", tid)
    return jsonify({"status": "ok"})


@app.route("/api/trackers/<tid>/check", methods=["POST"])
@login_required
def check_now(tid):
    uid = _user_id()
    tracker = get_tracker(uid, tid)
    if not tracker:
        return jsonify({"error": "Tracker not found."}), 404

    if not is_astral_booking_url(tracker.get("url") or ""):
        return jsonify({"error": astral_url_error_message()}), 400

    check_url = build_tracking_url(tracker["url"], tracker.get("checkin"), tracker.get("checkout"))
    prev = get_last_price(tid, check_url)

    price, packages = scrape_price_and_packages(
        check_url,
        tracker.get("price_selector"),
        requirements=_requirements_from_tracker(tracker),
    )

    if price is None:
        err = (
            "Could not read a price from this Astral link. "
            "Make sure your link includes the correct stay dates (check-in / check-out) and guests/rooms, "
            "then copy the full URL again from the browser address bar. "
            "If you selected filters (meal plan / Stars / reserve duty), try turning them off and re-copying the URL."
        )
        return jsonify(
            {
                "error": err,
                "url": check_url,
            }
        ), 422

    save_price(tid, price, check_url)

    baseline_price, baseline_kind = _alert_baseline(tracker)
    comparison_label = "You paid"
    threshold_pct = float(tracker.get("threshold_pct") or 0)
    drop_pct = ((baseline_price - price) / baseline_price * 100) if baseline_price > 0 else 0
    delta = (price - prev) if prev is not None else None
    trend = (
        None
        if prev is None
        else ("down" if price < prev else "up" if price > prev else "same")
    )

    # Scheduled alerts use the alert rules.
    # Manual "Check Now" emails are for testing: send only if price changed vs what the user paid.
    blocker = explain_price_alert_blocker(tracker, baseline_price, price, prev, threshold_pct)
    u = get_user_by_id(uid)
    user_email = (u or {}).get("email")

    smtp_status = get_smtp_status()
    paid_price = baseline_price if baseline_kind == "paid" else None
    paid_changed = (
        paid_price is not None and abs(float(price) - float(paid_price)) >= 0.01
    )

    check_mail: dict | None = None
    check_email_skip_reason: str | None = None
    if not smtp_status.get("smtp_configured"):
        check_email_skip_reason = "smtp_not_configured"
    elif paid_price is None:
        check_email_skip_reason = "no_paid_price"
    elif not paid_changed:
        check_email_skip_reason = "no_price_change"
    else:
        trend_vs_paid = "down" if float(price) < float(paid_price) else "up"
        check_mail = send_check_now_email(
            user_email=user_email,
            label=tracker.get("label") or tracker.get("url") or tid,
            url=check_url,
            currency=tracker.get("currency", "₪"),
            current_price=float(price),
            previous_price=float(paid_price),
            trend=trend_vs_paid,
        )

    notification: dict = {
        "eligible": blocker is None,
        "blocker": blocker,
        "email_sent": False,
        **smtp_status,
        "email_skip_reason": None,
        "check_email_sent": bool((check_mail or {}).get("email_sent")),
        "check_email_skip_reason": (check_mail or {}).get("email_skip_reason") or check_email_skip_reason,
    }
    if notification["check_email_sent"]:
        logger.info("[Check now] Result email sent for tracker %s", tid)
    elif notification.get("check_email_skip_reason"):
        logger.warning("[Check now] Result email not sent (%s)", notification.get("check_email_skip_reason"))

    return jsonify(
        {
            "price": price,
            "previous_price": prev,
            "delta": delta,
            "trend": trend,
            "paid_price": tracker.get("paid_price"),
            "currency": tracker.get("currency", "₪"),
            "packages": packages,
            "is_drop": bool(baseline_price > 0 and price < baseline_price),
            "meets_threshold": bool(drop_pct >= threshold_pct),
            "checked_url": check_url,
            # True only if SMTP actually delivered (not just "drop qualified")
            "notification_sent": notification.get("email_sent", False),
            "notification": notification,
        }
    )


@app.route("/api/debug/run-check", methods=["POST"])
@login_required
def debug_run_check():
    logger.info("[Debug] Manual scheduler run triggered via API")
    check_all_trackers()
    return jsonify({"status": "done", "message": "Scheduler ran now. Check terminal logs."})


@app.route("/api/debug/logs", methods=["GET"])
@login_required
def debug_logs():
    try:
        with open("tracker.log", encoding="utf-8") as f:
            lines = f.readlines()
        return jsonify({"lines": lines[-50:]})
    except FileNotFoundError:
        return jsonify({"lines": []})


@app.route("/api/test-notification", methods=["POST"])
@login_required
def test_notification():
    raw = request.json or {}
    topic = raw.get("topic")
    uid = _user_id()
    u = get_user_by_id(uid) if uid else None
    email = (u or {}).get("email")
    smtp = get_smtp_status()
    smtp_ok = bool(smtp.get("smtp_configured"))

    email_skip = test_notification_email_reason(email)
    sent_mail = email_skip is None

    send_test_notification(topic)

    return jsonify(
        {
            **smtp,
            "email_sent": sent_mail,
            "email_skip_reason": email_skip,
            "email_user_message": (
                "Great — test email sent. Check inbox and spam."
                if sent_mail
                else (
                    "Email sending is not set up yet. Please connect your email sender in server settings."
                    if email_skip == "smtp_not_configured"
                    else (
                        "Could not send email: login to your email provider failed. Usually this means wrong app password."
                        if email_skip == "auth_failed"
                        else (
                            "Could not send email: sender address was rejected. Set MAIL_FROM to the same address as SMTP_USER."
                            if email_skip == "from_rejected"
                            else (
                                "Could not send email: email server is unreachable. Check SMTP_HOST/SMTP_PORT and internet access."
                                if email_skip == "host_unreachable"
                                else (
                                "Could not send email: this host cannot reach the email server network right now. Try again later or switch email provider."
                                if email_skip == "network_unreachable"
                                else (
                                    "Could not send email: request timed out. Try again in a minute."
                                    if email_skip == "timeout"
                                    else (
                                        "Could not send email: no account email found."
                                        if email_skip == "no_user_email"
                                        else "Could not send email right now. Please try again."
                                    )
                                )
                            )
                        )
                    )
                )
            ),
            "ntfy_ping": True,
            "hint": None
            if smtp_ok
            else "Add SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, and MAIL_FROM to your host (e.g. Render) environment, then redeploy.",
        }
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    interval = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 5000))
    start_scheduler(interval_minutes=interval)
    logger.info(
        "[App] Astral Hotels Price Tracker ready | Astral-only | checks every %s minutes",
        interval,
    )
    logger.info("[App] Open: http://%s:%s", host, port)
    logger.info(
        "[App] Running local server mode. For production deployment, use a WSGI server on your hosting platform."
    )
    app.run(debug=False, host=host, port=port)
