"""
Automated price checking scheduler.
Uses APScheduler to run in the background inside the Flask process.
"""
from __future__ import annotations

import atexit
import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_all_trackers, get_last_price, get_user_by_id, save_price
from notifier import send_price_alert
from scraper import scrape_price_and_packages

logger = logging.getLogger(__name__)

CHECKIN_KEYS = ["checkin", "check_in", "start", "fromDate", "arrive", "arrival", "dateFrom"]
CHECKOUT_KEYS = ["checkout", "check_out", "end", "toDate", "depart", "departure", "dateTo"]


def build_tracking_url(url: str, checkin: str | None, checkout: str | None) -> str:
    if not checkin or not checkout:
        return url

    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(pairs)

    found_checkin = False
    found_checkout = False

    for key in CHECKIN_KEYS:
        if key in query:
            query[key] = checkin
            found_checkin = True

    for key in CHECKOUT_KEYS:
        if key in query:
            query[key] = checkout
            found_checkout = True

    # If no date params exist in URL, add common fallback keys.
    if not found_checkin:
        query["checkin"] = checkin
    if not found_checkout:
        query["checkout"] = checkout

    updated_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=updated_query))


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_requirements(tracker: dict) -> dict:
    meal_plan = (tracker.get("meal_plan") or "none").lower()
    if meal_plan not in {"none", "breakfast", "half_board", "full_board"}:
        meal_plan = "none"

    return {
        "meal_plan": meal_plan,
        "free_cancel": _to_bool(tracker.get("require_free_cancel")),
        "room_keyword": (tracker.get("room_keyword") or "").strip(),
        "reserve_duty_only": _to_bool(tracker.get("reserve_duty_only")),
        "club_membership_only": _to_bool(tracker.get("club_membership_only")),
    }


def _alert_baseline(tracker: dict) -> tuple[float, str]:
    """Price to compare against: paid_price only. Returns (value, kind)."""
    paid = float(tracker.get("paid_price") or 0)
    if paid > 0:
        return paid, "paid"
    return 0.0, "none"


def explain_drop_notification_blocker(
    baseline_price: float,
    current_price: float,
    previous_price: float | None,
    threshold_pct: float,
) -> str | None:
    """
    If a price-drop alert should NOT fire, return a short reason code.
    If None, the drop qualifies for notification (same rules as _should_notify_drop).
    """
    if baseline_price <= 0:
        return "no_baseline"
    if current_price >= baseline_price:
        return "not_below_baseline"
    drop_pct = ((baseline_price - current_price) / baseline_price) * 100
    if drop_pct < threshold_pct:
        return "below_threshold"
    # Avoid duplicate notifications for unchanged/less-good prices.
    if previous_price is not None and current_price >= previous_price:
        return "not_better_than_last_check"
    return None


def _should_notify_drop(
    baseline_price: float,
    current_price: float,
    previous_price: float | None,
    threshold_pct: float,
) -> bool:
    return explain_drop_notification_blocker(
        baseline_price, current_price, previous_price, threshold_pct
    ) is None


def explain_price_alert_blocker(
    tracker: dict,
    baseline_price: float,
    current_price: float,
    previous_price: float | None,
    threshold_pct: float,
) -> str | None:
    """
    Unified gate for scheduled + manual checks. Respects tracker alert_direction:
    - down: below paid by threshold (needs baseline).
    - any: any change vs last check.
    - up: price rose vs last check by threshold (no baseline required).
    Returns None if an alert should be sent, else a reason code.
    """
    direction = (tracker.get("alert_direction") or "down").lower()
    if direction not in ("down", "up", "any"):
        direction = "down"

    if direction == "down":
        return explain_drop_notification_blocker(
            baseline_price, current_price, previous_price, threshold_pct
        )

    # "any" / "up" compare to the last successful scrape (previous_price).
    if previous_price is None:
        return "no_previous_price"

    if direction == "any":
        if abs(current_price - previous_price) < 0.02:
            return "no_change"
        if threshold_pct > 0:
            chg_pct = abs(current_price - previous_price) / previous_price * 100
            if chg_pct < threshold_pct:
                return "below_threshold"
        return None

    if direction == "up":
        if current_price <= previous_price:
            return "not_rising"
        if threshold_pct > 0:
            rise_pct = (current_price - previous_price) / previous_price * 100
            if rise_pct < threshold_pct:
                return "below_threshold"
        return None

    return "unknown"


def alert_kind_for_tracker(tracker: dict) -> str:
    """Maps DB alert_direction to notifier alert_kind."""
    d = (tracker.get("alert_direction") or "down").lower()
    if d == "any":
        return "change"
    if d == "up":
        return "rise"
    return "drop"


def check_all_trackers():
    trackers = get_all_trackers()
    logger.info("[Scheduler] Checking %s trackers...", len(trackers))

    for tracker in trackers:
        try:
            _check_single_tracker(tracker)
        except Exception as e:
            logger.error("[Scheduler] Error checking tracker %s: %s", tracker.get("id"), e)


def _check_single_tracker(tracker: dict):
    tid = tracker["id"]
    url = tracker["url"]
    label = tracker.get("label") or url
    currency = tracker.get("currency", "₪")

    baseline_price, baseline_kind = _alert_baseline(tracker)
    threshold_pct = float(tracker.get("threshold_pct") or 0)
    comparison_label = (
        "You paid"
        if baseline_kind == "paid"
        else "Your expected price"
        if baseline_kind == "expected"
        else "Reference"
    )

    tracking_url = build_tracking_url(url, tracker.get("checkin"), tracker.get("checkout"))
    previous_price = get_last_price(tid, tracking_url)

    current_price, packages = scrape_price_and_packages(
        tracking_url,
        tracker.get("price_selector"),
        requirements=_normalize_requirements(tracker),
    )

    if current_price is None:
        logger.warning("[Tracker %s] No matching price at %s", tid, tracking_url)
        return

    save_price(tid, current_price, tracking_url)
    logger.info("[Tracker %s] %s: %s%s", tid, label, currency, current_price)

    blocker = explain_price_alert_blocker(
        tracker, baseline_price, current_price, previous_price, threshold_pct
    )
    if blocker is None:
        user_email = None
        uid = tracker.get("user_id")
        if uid:
            u = get_user_by_id(uid)
            if u:
                user_email = u.get("email")
        ntfy_topic = (tracker.get("ntfy_topic") or "").strip() or None
        kind = alert_kind_for_tracker(tracker)
        savings = (
            round(baseline_price - current_price, 2)
            if baseline_price > 0
            else None
        )
        mail_info = send_price_alert(
            label=label,
            current_price=current_price,
            currency=currency,
            url=tracking_url,
            alert_kind=kind,
            reference_price=baseline_price if baseline_price > 0 else None,
            previous_price=previous_price,
            savings=savings,
            comparison_label=comparison_label,
            packages=packages,
            user_email=user_email,
            ntfy_topic=ntfy_topic,
        )
        if not mail_info.get("email_sent"):
            logger.warning(
                "[Scheduler] Alert for %s but email not sent: %s",
                tid,
                mail_info.get("email_skip_reason"),
            )


def start_scheduler(interval_minutes: int = 30):
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=check_all_trackers,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="hotel_price_check",
        name="Check all hotel prices",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    atexit.register(lambda: scheduler.shutdown())
    return scheduler
