from __future__ import annotations

import logging
import random
import re
import time
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

MEAL_KEYWORDS = {
    "breakfast": [
        "ארוחת בוקר",
        "לינה וארוחת בוקר",
        "כולל ארוחת בוקר",
        "עם ארוחת בוקר",
        "breakfast",
        "breakfast included",
        "bed and breakfast",
        "b&b",
    ],
    "half_board": [
        "חצי פנסיון",
        "חצי-פנסיון",
        "half board",
        "half-board",
        "breakfast and dinner",
    ],
    "full_board": [
        "פנסיון מלא",
        "full board",
        "full-board",
        "all inclusive",
        "all-inclusive",
        "הכל כלול",
    ],
}

RESERVE_DUTY_KEYWORDS = [
    "מילואים",
    "למשרתי המילואים",
    "משרתי מילואים",
    "מילואימ",
    "reservist",
    "reserve duty",
    "soldier discount",
]
RESERVE_DUTY_EXCLUDE_TOKENS = [
    "מילואים",
    "למשרתי המילואים",
    "משרתי מילואים",
    "טופס 3010",
]
CLUB_MEMBERSHIP_KEYWORDS = [
    "stars",
    "הנחת stars",
    "חברי stars",
    "מועדון",
    "club",
    "member",
]

FREE_CANCEL_RE = re.compile(r"ביטול\s?חינם|free\s?cancel|no\s?charge|fully\s?refund", re.I)
CURRENCY_RE = r"(?:₪|\$|€|£|ש[\"׳]?ח|שח|שקל(?:ים)?|ILS|USD|EUR|GBP)"
NUMBER_RE = r"([\d]{1,3}(?:,[\d]{3})+(?:\.\d{1,2})?|[\d]{3,6}(?:\.\d{1,2})?)"
PRICE_BEFORE = re.compile(CURRENCY_RE + r"\s?" + NUMBER_RE, re.UNICODE)
PRICE_AFTER = re.compile(NUMBER_RE + r"\s?" + CURRENCY_RE, re.UNICODE)
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass
class RoomPackage:
    price: float
    room_name: str = ""
    breakfast: bool = False
    half_board: bool = False
    full_board: bool = False
    free_cancel: bool = False
    reserve_duty: bool = False
    raw_text: str = ""


@dataclass(frozen=True)
class SiteConnector:
    name: str
    matcher: callable
    extractor: callable


def _build_single_package(price: float, req: dict, combined_text: str) -> list[dict]:
    meal = (req.get("meal_plan") or "none").lower()
    pkg = RoomPackage(
        price=price,
        breakfast=meal == "breakfast",
        half_board=meal == "half_board",
        full_board=meal == "full_board",
        free_cancel=bool(req.get("free_cancel")),
        reserve_duty=bool(req.get("reserve_duty_only")),
        raw_text=(combined_text or "")[:1000],
    )
    return [_pkg_to_dict(pkg)]


CONNECTORS: list[SiteConnector] = [
    SiteConnector(
        name="Jacob",
        matcher=lambda host, _url: "booking.jacobhotels.com" in host,
        extractor=lambda text, req: _extract_jacob_offer_price(text, req),
    ),
    SiteConnector(
        name="Isrotel",
        matcher=lambda host, _url: "isrotel.co.il" in host,
        extractor=lambda text, req: _extract_isrotel_offer_price(text, req),
    ),
    SiteConnector(
        name="SimpleBooking",
        matcher=lambda host, fixed_url: "simplebooking" in host or "/ibe2/hotel/" in fixed_url.lower(),
        extractor=lambda text, req: _extract_simplebooking_offer_price(text, req),
    ),
    SiteConnector(
        name="Dan",
        matcher=lambda host, _url: "danhotels.co.il" in host,
        extractor=lambda text, req: _extract_dan_offer_price(text, req),
    ),
    SiteConnector(
        name="Fattal",
        matcher=lambda host, _url: "fattal.co.il" in host,
        extractor=lambda text, req: _extract_fattal_offer_price(text, req),
    ),
]


def scrape_price_and_packages(
    url: str,
    price_selector: str | None = None,
    requirements: dict | None = None,
) -> tuple[float | None, list[dict]]:
    req = requirements or {}
    strict_requirements = _has_strict_requirements(req)
    fixed_url = _fix_url(url)
    host = (urlparse(fixed_url).hostname or "").lower()
    is_astral = "astralhotels.co.il" in host

    if not is_astral:
        logger.warning("[Scraper] Non-Astral URL rejected (Astral-only app): %s", fixed_url[:120])
        return None, []

    # Fast-path Astral via backend API (no browser). Includes Stars/club: API has
    # priceAfterClubMemberDiscount on each planMeal (browser path was unreliable for club).
    if is_astral:
        api_price, api_packages = _scrape_astral_via_api(fixed_url, req)
        if api_price is not None:
            logger.info("[Scraper] Astral API matched: %s", api_price)
            return api_price, api_packages

    visible_text, html = _fetch_page(fixed_url, price_selector, req)
    if not visible_text and not html:
        return None, []

    html_text = _html_to_text(html or "")
    combined_text = f"{visible_text or ''}\n{html_text}"

    # Connector registry: each adapter can be tested/extended independently.
    for connector in CONNECTORS:
        if connector.matcher(host, fixed_url):
            price = connector.extractor(combined_text, req)
            if price is not None:
                logger.info("[Scraper] %s connector matched: %s", connector.name, price)
                return price, _build_single_package(price, req, combined_text)

    # Astral: prefer contextual extraction (page copy changes frequently).
    # If strict requirements are set (meal/free-cancel/miluim/room keyword), do NOT fallback
    # to a random number elsewhere on the page.
    if is_astral:
        # For Astral specifically, the meal-plan label is often NOT printed next to the numeric price.
        # We already attempt to click the relevant meal/discount toggles in the browser;
        # so for extraction, we prefer the *visible* text and relax the meal keyword constraint.
        astral_req = dict(req)
        meal = (astral_req.get("meal_plan") or "none").lower()
        if meal != "none":
            astral_req["meal_plan"] = "none"

        text_for_astral = (visible_text or "").strip() or combined_text

        astral_price = _extract_contextual_price(text_for_astral, astral_req)
        if astral_price is None:
            # Safe fallback for Astral: first price from the filtered visible content.
            astral_price = _extract_first_price_from_text(text_for_astral)

        if astral_price is None:
            logger.info("[Scraper] Astral: no matching price found (strict=%s)", strict_requirements)
            return None, []

        pkg = RoomPackage(
            price=astral_price,
            breakfast=(req.get("meal_plan") == "breakfast") or _has_meal_plan(combined_text, "breakfast"),
            half_board=(req.get("meal_plan") == "half_board") or _has_meal_plan(combined_text, "half_board"),
            full_board=(req.get("meal_plan") == "full_board") or _has_meal_plan(combined_text, "full_board"),
            free_cancel=bool(req.get("free_cancel")) or bool(FREE_CANCEL_RE.search(combined_text)),
            reserve_duty=bool(req.get("reserve_duty_only")) or _has_reserve_duty(combined_text),
            raw_text=(combined_text or "")[:1000],
        )
        logger.info("[Scraper] Astral contextual matched: %s", astral_price)
        return astral_price, [_pkg_to_dict(pkg)]

    matched_price = _extract_contextual_price(combined_text, req)
    if matched_price is not None:
        pkg = RoomPackage(
            price=matched_price,
            breakfast=(req.get("meal_plan") == "breakfast") or _has_meal_plan(combined_text, "breakfast"),
            half_board=(req.get("meal_plan") == "half_board") or _has_meal_plan(combined_text, "half_board"),
            full_board=(req.get("meal_plan") == "full_board") or _has_meal_plan(combined_text, "full_board"),
            free_cancel=bool(req.get("free_cancel")) or bool(FREE_CANCEL_RE.search(combined_text)),
            reserve_duty=bool(req.get("reserve_duty_only")) or _has_reserve_duty(combined_text),
            raw_text=(combined_text or "")[:1000],
        )
        return matched_price, [_pkg_to_dict(pkg)]

    if strict_requirements:
        logger.info("[Scraper] Strict requirements set but no matching price found.")
        return None, []

    fallback = _extract_first_price_from_text(combined_text)
    if fallback is None:
        logger.warning("[Scraper] No price extracted for %s", fixed_url)
        return None, []

    return fallback, [_pkg_to_dict(RoomPackage(price=fallback, raw_text=combined_text[:500]))]


def scrape_price(url: str, price_selector: str | None = None) -> float | None:
    price, _ = scrape_price_and_packages(url, price_selector)
    return price

def _has_strict_requirements(req: dict) -> bool:
    meal_plan = (req.get("meal_plan") or "none").lower()
    if meal_plan != "none":
        return True
    if req.get("free_cancel"):
        return True
    if req.get("reserve_duty_only"):
        return True
    if req.get("club_membership_only"):
        return True
    if (req.get("room_keyword") or "").strip():
        return True
    return False


def _fix_url(url: str) -> str:
    # Some links are double-encoded (%255B...); decode once only.
    decoded = unquote(url)
    if decoded != url:
        logger.info("[Scraper] Decoded URL (single pass)")
    return decoded


def _fetch_page(url: str, selector: str | None = None, requirements: dict | None = None) -> tuple[str | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed.")
        return None, None

    req = requirements or {}
    is_simplebooking = "simplebooking" in (url or "").lower()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="he-IL",
            )
            page = ctx.new_page()
            # Avoid multi-minute stalls on heavy pages.
            page.set_default_timeout(10_000)
            page.set_default_navigation_timeout(25_000)
            page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,webp}", lambda r: r.abort())

            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if not is_simplebooking:
                for sel in ["[class*='price']", "[class*='rate']", "[class*='amount']", "[class*='package']", "button"]:
                    try:
                        page.wait_for_selector(sel, timeout=4_000)
                        break
                    except Exception:
                        continue
                time.sleep(random.uniform(1.0, 2.0))
            else:
                # SimpleBooking engines often render key blocks quickly; avoid selector polling.
                page.wait_for_timeout(8000)

            _dismiss_popups(page)
            if not is_simplebooking:
                _reveal_prices_if_needed(page)
            _apply_dynamic_filters(page, req)
            if not is_simplebooking:
                _reveal_prices_if_needed(page)
            _domain_specific_actions(page, url, req)

            if not is_simplebooking:
                page.evaluate("window.scrollBy(0, 1200)")
                time.sleep(0.8)

            if selector:
                el = page.query_selector(selector)
                try:
                    visible_text = el.inner_text(timeout=7_000) if el else page.inner_text("body", timeout=7_000)
                except Exception:
                    visible_text = page.inner_text("body", timeout=7_000)
            else:
                visible_text = page.inner_text("body", timeout=7_000)

            html = page.content()
            browser.close()

            logger.info("[Scraper] fetched text=%s html=%s", len(visible_text), len(html))
            return visible_text, html
    except Exception as e:
        logger.error("[Scraper] Playwright error: %s", e)
        return None, None


def _looks_like_astral_internal_room_code(s: str) -> bool:
    """API sometimes returns internal codes like '171SuperSu' instead of the site label."""
    s = (s or "").strip()
    if len(s) < 4:
        return False
    return bool(re.match(r"^\d{2,}[A-Za-z]", s))


def _astral_display_room_name(
    room: dict,
    rp: dict,
    offer: dict,
    pm: dict,
    room_category: str,
    plan_code: str,
) -> str:
    """
    Prefer human-readable room + meal labels from Astral SearchRooms JSON
    (room names, descriptions) over short internal roomCategory codes.
    """
    collected: list[str] = []

    def _take(d: dict, *keys: str) -> str:
        for k in keys:
            v = (d.get(k) or "").strip()
            if not v:
                continue
            if _looks_like_astral_internal_room_code(v):
                continue
            return v
        return ""

    for chunk in (
        _take(room, "roomName", "name", "roomDescription", "description", "roomTitle"),
        _take(rp, "roomName", "roomDescription", "roomCategoryName", "roomTitle"),
        _take(offer, "roomName", "description", "title"),
    ):
        if chunk:
            collected.append(chunk)
            break

    if not collected and room_category:
        rc = room_category.strip()
        if not _looks_like_astral_internal_room_code(rc):
            collected.append(rc)
        elif (offer.get("description") or "").strip():
            collected.append((offer.get("description") or "").strip()[:120])

    room_line = collected[0] if collected else (room_category or "").strip() or "Room"

    meal_label = (
        (pm.get("planName") or "").strip()
        or (pm.get("planDescription") or "").strip()
        or (pm.get("mealPlanDescription") or "").strip()
    )
    if not meal_label:
        pc = (plan_code or "").strip().lower()
        meal_label = {
            "b/b": "Bed & breakfast",
            "bb": "Bed & breakfast",
            "h/b": "Half board",
            "hb": "Half board",
            "f/b": "Full board",
            "fb": "Full board",
        }.get(pc, "")
    if not meal_label:
        pid = (pm.get("planId") or "").strip()
        if pid and not pid.isdigit() and not _looks_like_astral_internal_room_code(pid):
            meal_label = pid

    if _looks_like_astral_internal_room_code(room_line):
        od = (offer.get("description") or "").strip()
        if od:
            room_line = od[:160]

    if meal_label:
        return f"{room_line} — {meal_label}"
    return room_line


def _scrape_astral_via_api(url: str, req: dict) -> tuple[float | None, list[dict]]:
    """
    Fast-path Astral extraction using their backend API.
    This avoids brittle DOM interactions and is much faster than headless browsing.
    """
    host = (urlparse(url).hostname or "").lower()
    if "astralhotels.co.il" not in host:
        return None, []

    parsed = urlparse(url)
    q = parse_qs(parsed.query)

    def _first(key: str) -> str | None:
        v = q.get(key)
        return v[0] if v else None

    hotel_id_raw = _first("hotelIdList") or _first("hotelId") or _first("hotel")
    from_date = _first("fromDate") or _first("checkin") or _first("checkinDate")
    to_date = _first("toDate") or _first("checkout") or _first("checkoutDate")
    rooms_guests_raw = _first("roomsGuests")

    if not (hotel_id_raw and from_date and to_date and rooms_guests_raw):
        return None, []

    try:
        hotel_id = int(str(hotel_id_raw).split(",")[0])
    except ValueError:
        return None, []

    # Astral links sometimes double-encode roomsGuests (%255B...).
    decoded_rooms = unquote(unquote(rooms_guests_raw))
    try:
        rooms_guests = json.loads(decoded_rooms)
    except Exception:
        return None, []

    payload = {
        "hotelIdList": [hotel_id],
        "fromDate": from_date,
        "toDate": to_date,
        "isLocal": True,
        "roomsGuests": rooms_guests,
    }

    try:
        import requests
    except ImportError:
        logger.error("[Scraper] requests not installed.")
        return None, []

    try:
        r = requests.post(
            "https://websiteapi.astralhotels.co.il/api/search/SearchRooms",
            json=payload,
            timeout=25,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
            },
        )
        data = r.json()
    except Exception as e:
        logger.warning("[Scraper] Astral API error: %s", e)
        return None, []

    if (data or {}).get("errorCode") not in (0, "0", None):
        return None, []

    meal_plan = (req.get("meal_plan") or "none").lower()
    # Meal fallback: if the requested plan does not exist for the stay, fall back to the next
    # best available (full_board -> half_board -> breakfast -> any).
    meal_code_groups: list[set[str]] = []
    if meal_plan == "full_board":
        meal_code_groups = [{"f/b", "fb"}, {"h/b", "hb"}, {"b/b", "bb"}, set()]
    elif meal_plan == "half_board":
        meal_code_groups = [{"h/b", "hb"}, {"b/b", "bb"}, set()]
    elif meal_plan == "breakfast":
        meal_code_groups = [{"b/b", "bb"}, set()]
    else:
        meal_code_groups = [set()]

    best_price: float | None = None
    packages: list[dict] = []
    candidates: list[dict] = []

    hotels = ((data.get("body") or {}).get("hotels") or []) if isinstance(data, dict) else []
    for hotel in hotels:
        for room in hotel.get("rooms") or []:
            for rp in room.get("roomPrices") or []:
                room_category = (rp.get("roomCategory") or "").strip()
                for offer in rp.get("roomPrices") or []:
                    price_code = (offer.get("priceCode") or "").strip()
                    desc = (offer.get("description") or "").strip()
                    reserve_hit = _has_reserve_duty(f"{price_code}\n{desc}")
                    reserve_only = bool(req.get("reserve_duty_only"))
                    if (not reserve_only) and reserve_hit:
                        # Tracker is normal (no duty), skip Miluim-only promos.
                        continue

                    for pm in offer.get("planMeals") or []:
                        plan_code = (pm.get("planCode") or "").strip().lower()
                        club_only = bool(req.get("club_membership_only"))
                        has_club_price = pm.get("priceAfterClubMemberDiscount") is not None

                        # Prefer club price when requested; fall back to normal price if club is unavailable.
                        raw_price = None
                        used_club = False
                        if club_only and has_club_price:
                            raw_price = pm.get("priceAfterClubMemberDiscount")
                            used_club = True
                        if raw_price is None:
                            raw_price = (
                                pm.get("priceAfterInternetDiscount")
                                if pm.get("priceAfterInternetDiscount") is not None
                                else pm.get("basePrice")
                            )
                        if raw_price is None:
                            continue

                        try:
                            price_val = float(raw_price)
                        except (TypeError, ValueError):
                            continue

                        plan_name = (pm.get("planId") or "").strip()
                        display_name = _astral_display_room_name(
                            room, rp, offer, pm, room_category, plan_code
                        )
                        pkg = RoomPackage(
                            price=price_val,
                            room_name=display_name,
                            breakfast=plan_code in {"b/b", "bb"} or "ארוחת בוקר" in plan_name,
                            half_board=plan_code in {"h/b", "hb"} or "חצי" in plan_name,
                            full_board=plan_code in {"f/b", "fb"} or "מלא" in plan_name,
                            reserve_duty=reserve_hit,
                            raw_text=(f"{room_category}\n{price_code}\n{plan_code}\n{plan_name}\n{desc}")[:1000],
                        )
                        candidates.append(
                            {
                                "pkg": _pkg_to_dict(pkg),
                                "price": price_val,
                                "plan_code": plan_code,
                                "reserve_hit": reserve_hit,
                                "used_club": used_club,
                            }
                        )

    if best_price is None:
        # We'll compute best_price after applying fallbacks below.
        pass

    if not candidates:
        return None, []

    # Fallback for reserve duty: if "reserve only" yields no offers, return normal price.
    reserve_only = bool(req.get("reserve_duty_only"))
    if reserve_only:
        reserve_candidates = [c for c in candidates if c["reserve_hit"]]
        if reserve_candidates:
            candidates = reserve_candidates

    # Fallback for club membership: if club is requested but no club price exists, return normal price.
    club_only = bool(req.get("club_membership_only"))
    if club_only:
        club_candidates = [c for c in candidates if c["used_club"]]
        if club_candidates:
            candidates = club_candidates

    # Meal-plan fallback by groups.
    filtered: list[dict] = []
    for group in meal_code_groups:
        if not group:
            filtered = candidates
            break
        subset = [c for c in candidates if c["plan_code"] in group]
        if subset:
            filtered = subset
            break

    if not filtered:
        filtered = candidates

    best_price = min((c["price"] for c in filtered), default=None)
    if best_price is None:
        return None, []

    packages = [c["pkg"] for c in filtered]

    # Keep only a few packages to avoid huge payloads.
    packages = sorted(packages, key=lambda p: p.get("price") or 1e18)[:8]
    return best_price, packages


def _dismiss_popups(page):
    for sel in ["button[class*='close']", "#onetrust-accept-btn-handler", "button:has-text('אישור')", "button:has-text('Accept')"]:
        try:
            page.click(sel, timeout=600)
        except Exception:
            pass


def _domain_specific_actions(page, url: str, req: dict):
    host = (urlparse(url).hostname or "").lower()

    if "astralhotels.co.il" in host:
        # Astral's booking widget is JS-driven and heavy; avoid repeated full-page innerText calls.
        # We try to apply meal/miluim toggles, then wait briefly for any currency-ish numbers to appear.
        meal = (req.get("meal_plan") or "none").lower()
        if meal == "half_board":
            _click_first_matching_text(page, ["חצי פנסיון", "half board", "half-board"], force=True)
        elif meal == "full_board":
            _click_first_matching_text(page, ["פנסיון מלא", "full board", "full-board", "הכל כלול"], force=True)
        elif meal == "breakfast":
            _click_first_matching_text(page, ["ארוחת בוקר", "breakfast", "bed and breakfast"], force=True)

        if req.get("reserve_duty_only"):
            _click_first_matching_text(page, ["מילואים", "למשרתי המילואים", "טופס 3010"], force=True)
        if req.get("club_membership_only"):
            _click_first_matching_text(page, ["Stars", "הנחת Stars", "מועדון Stars", "Club"], force=True)

        for _ in range(4):
            try:
                page.evaluate("window.scrollBy(0, 900)")
            except Exception:
                pass
            _reveal_prices_if_needed(page)
            _apply_dynamic_filters(page, req)
            try:
                page.wait_for_function(
                    "(() => /(?:₪|ש\\\"ח|שח|ILS|\\$|€|£)\\s*\\d|\\d\\s*(?:₪|ש\\\"ח|שח|ILS|\\$|€|£)/.test(document.body && document.body.innerText || ''))()",
                    timeout=6_000
                )
                break
            except Exception:
                time.sleep(0.8)

    if "booking.simplex-ltd.com" in host:
        # This engine usually requires explicit "show price" clicks per row.
        _click_many_matching_text(page, ["הצג מחיר", "הצג מחירים", "show price"], max_clicks=20, force=True)
        time.sleep(1.4)

    if "fattal.co.il" in host:
        # Fattal often switches price values only after selecting meal tab.
        meal = (req.get("meal_plan") or "none").lower()
        if meal == "none":
            _click_first_matching_text(page, ["לינה בלבד", "room only", "no breakfast"], force=True)
        elif meal == "breakfast":
            _click_first_matching_text(page, ["לינה וארוחת בוקר", "ארוחת בוקר", "breakfast"], force=True)
        elif meal == "half_board":
            _click_first_matching_text(page, ["חצי פנסיון", "half board"], force=True)
        elif meal == "full_board":
            _click_first_matching_text(page, ["פנסיון מלא", "full board"], force=True)
        page.wait_for_timeout(1200)


def _reveal_prices_if_needed(page):
    _click_many_matching_text(
        page,
        ["הצג מחיר", "הצג מחירים", "show price", "show prices", "check price", "display price"],
        max_clicks=20,
        force=True,
    )
    time.sleep(0.5)


def _apply_dynamic_filters(page, req: dict):
    meal_plan = (req.get("meal_plan") or "none").lower()

    if meal_plan == "half_board":
        _click_first_matching_text(page, ["חצי פנסיון", "half board", "half-board"], force=True)
    elif meal_plan == "full_board":
        _click_first_matching_text(page, ["פנסיון מלא", "full board", "all inclusive"], force=True)
    elif meal_plan == "breakfast":
        _click_first_matching_text(page, ["ארוחת בוקר", "breakfast"], force=True)

    if req.get("reserve_duty_only"):
        _click_first_matching_text(page, ["מילואים", "למשרתי המילואים", "reservist", "reserve duty"], force=True)
    if req.get("club_membership_only"):
        _click_first_matching_text(page, ["Stars", "הנחת Stars", "מועדון Stars", "club"], force=True)

    time.sleep(0.8)


def _click_first_matching_text(page, texts: list[str], force: bool = False):
    for token in texts:
        if _click_by_role_or_text(page, token, force=force):
            return
        if _click_by_js_token(page, token):
            return


def _click_many_matching_text(page, texts: list[str], max_clicks: int = 8, force: bool = False) -> bool:
    did_click = False
    for token in texts:
        pattern = re.compile(re.escape(token), re.I)

        for role in ["button", "link"]:
            try:
                loc = page.get_by_role(role, name=pattern)
                count = min(loc.count(), max_clicks)
                for i in range(count):
                    try:
                        item = loc.nth(i)
                        item.wait_for(state="visible", timeout=700)
                        item.click(timeout=1400, force=force)
                        did_click = True
                        time.sleep(0.1)
                    except Exception:
                        continue
            except Exception:
                pass

        try:
            loc = page.get_by_text(pattern)
            count = min(loc.count(), max_clicks)
            for i in range(count):
                try:
                    item = loc.nth(i)
                    item.wait_for(state="visible", timeout=700)
                    item.click(timeout=1400, force=force)
                    did_click = True
                    time.sleep(0.1)
                except Exception:
                    continue
        except Exception:
            pass

        if not did_click and _click_by_js_token(page, token):
            did_click = True

    return did_click


def _click_by_role_or_text(page, token: str, force: bool = False) -> bool:
    pattern = re.compile(re.escape(token), re.I)

    for role in ["radio", "button", "link"]:
        try:
            locator = page.get_by_role(role, name=pattern).first
            locator.wait_for(state="visible", timeout=900)
            locator.click(timeout=1400, force=force)
            return True
        except Exception:
            pass

    try:
        locator = page.get_by_text(pattern).first
        locator.wait_for(state="visible", timeout=900)
        locator.click(timeout=1400, force=force)
        return True
    except Exception:
        return False


def _click_by_js_token(page, token: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (token) => {
                  const norm = (s) => (s || '').toLowerCase();
                  const q = norm(token);

                  const labels = Array.from(document.querySelectorAll('label, span, div, button, a'));
                  let clicks = 0;

                  const trigger = (el) => {
                    if (!el) return false;
                    try {
                      el.click();
                      return true;
                    } catch {
                      return false;
                    }
                  };

                  for (const node of labels) {
                    const txt = norm(node.innerText || node.textContent || '');
                    if (!txt || !txt.includes(q)) continue;

                    const box = node.closest('label,[role="radio"],button,a,[class*="radio"],[class*="option"],[class*="meal"],[class*="board"]') || node;

                    // 1) Prefer real radio input selection.
                    let radio = null;
                    if (box.querySelector) {
                      radio = box.querySelector('input[type="radio"]');
                    }
                    if (!radio) {
                      const near = box.parentElement || box;
                      radio = near.querySelector && near.querySelector('input[type="radio"]');
                    }

                    if (radio) {
                      try {
                        radio.checked = true;
                        radio.dispatchEvent(new Event('input', { bubbles: true }));
                        radio.dispatchEvent(new Event('change', { bubbles: true }));
                      } catch {}
                    }

                    // 2) Click container + nearest clickable control.
                    trigger(box);
                    const clickable = box.querySelector && box.querySelector('button,[role="button"],a,[class*="radio"],[class*="option"]');
                    if (clickable) trigger(clickable);

                    clicks += 1;
                    if (clicks >= 8) break;
                  }

                  return clicks > 0;
                }
                """,
                token,
            )
        )
    except Exception:
        return False

def _extract_first_price_from_text(text: str) -> float | None:
    ordered: list[tuple[int, float]] = []
    for pattern in (PRICE_BEFORE, PRICE_AFTER):
        for m in pattern.finditer(text or ""):
            value = _to_float(m.group(1))
            if value is None:
                continue
            ordered.append((m.start(), value))

    if not ordered:
        return None
    ordered.sort(key=lambda x: x[0])
    return ordered[0][1]


def _extract_price_candidates_with_context(text: str) -> list[tuple[float, int, str]]:
    candidates: list[tuple[float, int, str]] = []

    for pattern in (PRICE_BEFORE, PRICE_AFTER):
        for m in pattern.finditer(text or ""):
            value = _to_float(m.group(1))
            if value is None:
                continue

            pos = m.start()
            window = (text or "")[max(0, pos - 260): pos + 260].lower()
            candidates.append((value, pos, window))

    return candidates


def _extract_astral_offer_price(url: str, text: str, req: dict) -> float | None:
    """
    Kept for backward compatibility (older versions used a very specific Astral copy pattern).
    The current flow uses `_extract_contextual_price` instead.
    """
    host = (urlparse(url).hostname or "").lower()
    if "astralhotels.co.il" not in host:
        return None
    return _extract_contextual_price(text, req)


def _extract_jacob_offer_price(text: str, req: dict) -> float | None:
    """
    Jacob booking engine (booking.jacobhotels.com).
    Uses Hebrew meal labels like:
      - "לינה בלבד"            (room only)
      - "לינה וארוחת בוקר"     (breakfast)
      - "חצי פנסיון"           (half board)
      - "פנסיון מלא"           (full board)

    For each relevant label, we scan a local window and pick the closest
    "מחיר מועדון" price; if none, we fall back to "מחיר אתר", then to the first price.
    """
    if "ג'ייקוב" not in text and "ג׳ייקוב" not in text and "ג'ייקוב" not in text:
        # Fast rejection if nothing Jacob-like is present in text.
        # (We still guard by hostname at call-site, this is just defensive.)
        pass

    meal_plan = (req.get("meal_plan") or "none").lower()
    prefer_club_price = bool(req.get("club_membership_only"))

    if meal_plan == "breakfast":
        labels = ["לינה וארוחת בוקר"]
    elif meal_plan == "half_board":
        labels = ["חצי פנסיון"]
    elif meal_plan == "full_board":
        labels = ["פנסיון מלא"]
    else:
        # No explicit preference: treat as "room only".
        labels = ["לינה בלבד"]

    best_price: float | None = None

    lower = text or ""

    for label in labels:
        for m in re.finditer(re.escape(label), lower):
            start = m.start()
            block = lower[start: start + 800]

            site_val: float | None = None
            club_val: float | None = None

            site_idx = block.find("מחיר אתר")
            if site_idx != -1:
                window = block[site_idx: site_idx + 260]
                site_val = _extract_first_price_from_text(window)

            club_idx = block.find("מחיר מועדון")
            if club_idx != -1:
                window = block[club_idx: club_idx + 260]
                club_val = _extract_first_price_from_text(window)

            chosen: float | None = None
            if prefer_club_price:
                chosen = club_val if club_val is not None else site_val
            else:
                chosen = site_val if site_val is not None else club_val

            if chosen is None:
                # Last resort: any price in the local block.
                chosen = _extract_first_price_from_text(block)

            if chosen is not None and (best_price is None or chosen < best_price):
                best_price = chosen

    return best_price


def _extract_isrotel_offer_price(text: str, req: dict) -> float | None:
    """
    Isrotel search result / room-selection pages.
    Typical block pattern (per room & package):

        לינה וא. בוקר
        חצי פנסיון
        5,156 ₪
        5% הנחת אתר
        4,898 ₪
        הנחת מועדון
        4,640 ₪
        ...

    We:
      * choose the correct meal label,
      * within its block, prefer "הנחת מועדון" price (club),
      * else fall back to the preceding "מחיר באתר"/"5% הנחת אתר" price.
    """
    meal_plan = (req.get("meal_plan") or "none").lower()
    reserve_only = bool(req.get("reserve_duty_only"))
    prefer_club_price = bool(req.get("club_membership_only"))

    if meal_plan == "breakfast":
        labels = ["לינה וא. בוקר", "לינה וארוחת בוקר"]
    elif meal_plan == "half_board":
        labels = ["חצי פנסיון"]
    elif meal_plan == "full_board":
        labels = ["פנסיון מלא"]
    else:
        labels = ["לינה בלבד"]

    best_price: float | None = None

    for label in labels:
        for m in re.finditer(re.escape(label), text or ""):
            start = m.start()
            block = (text or "")[start: start + 900]
            lower_block = block.lower()

            # Skip/require Miluim promo per tracker toggle.
            has_miluim = ("מילואים" in block) or ("שובר נופש" in block)
            if reserve_only and not has_miluim:
                continue
            if not reserve_only and has_miluim:
                continue

            # Inside the block, find both club/site prices if present.
            club_idx = lower_block.find("הנחת מועדון")
            club_price: float | None = None
            if club_idx != -1:
                club_window = block[max(0, club_idx - 120): club_idx + 160]
                club_price = _extract_first_price_from_text(club_window)

            site_idx = lower_block.find("הנחת אתר")
            if site_idx == -1:
                site_idx = lower_block.find("מחיר באתר")
            site_price: float | None = None
            if site_idx != -1:
                site_window = block[max(0, site_idx - 120): site_idx + 160]
                site_price = _extract_first_price_from_text(site_window)

            chosen: float | None = None
            if prefer_club_price:
                chosen = club_price if club_price is not None else site_price
            else:
                chosen = site_price if site_price is not None else club_price

            if chosen is None:
                chosen = _extract_first_price_from_text(block)

            if chosen is not None and (best_price is None or chosen < best_price):
                best_price = chosen

    return best_price


def _extract_simplebooking_offer_price(text: str, req: dict) -> float | None:
    """
    SimpleBooking engines (e.g. Inbal, Brown) show meal labels and prices together, e.g.:
      "Room Only ₪3,588 ₪3,661 Reserve"
      "Breakfast Included ₪3,920 ₪4,000 Reserve"

    Rule:
      - If reserve_duty_only is true: pick the duty/reserve price (the later one near "Reserve"/"Reservists").
      - Otherwise: pick the non-duty price (the first one near the meal label).
    """
    meal_plan = (req.get("meal_plan") or "none").lower()
    reserve_only = bool(req.get("reserve_duty_only"))

    if not text:
        return None

    lower = text.lower()

    # Meal label tokens (extend when you find new variants).
    tokens_by_meal = {
        "none": ["room only", "no breakfast", "no meals"],
        "breakfast": [
            "breakfast included",
            "with breakfast",
            "bed and breakfast",
            "b&b",
            "b/b",
        ],
        "half_board": ["half board", "half-board", "breakfast and dinner", "dinner + breakfast", "hb", "h/b"],
        "full_board": ["full board", "full-board", "all inclusive", "all-inclusive", "fb", "f/b"],
    }

    target_tokens = tokens_by_meal.get(meal_plan, [])
    if not target_tokens:
        return None

    # Reserve indicators in SimpleBooking pages
    reserve_markers = [
        "reserve duty",
        "reserve",
        "reservist",
        "reservists",
        "miluim",
        "מילואים",
        "3010",
    ]

    best: float | None = None

    for token in target_tokens:
        for m in re.finditer(re.escape(token), lower):
            start = m.start()
            # Window covering the label + its adjacent prices and reserve text.
            window = text[start: start + 500]
            window_lower = window.lower()

            # Collect all price candidates in this window.
            prices: list[float] = []
            for pattern in (PRICE_BEFORE, PRICE_AFTER):
                for pm in pattern.finditer(window):
                    val = _to_float(pm.group(1))
                    if val is not None:
                        prices.append(val)

            if not prices:
                continue

            has_reserve = any(mark in window_lower for mark in reserve_markers)

            chosen: float | None = None
            if reserve_only:
                # Try to select the last price when reserve text exists; otherwise take the first.
                if has_reserve:
                    chosen = prices[-1]
                else:
                    chosen = prices[0]
            else:
                # Non-duty: take the first price.
                chosen = prices[0]

            if chosen is not None and (best is None or chosen < best):
                best = chosen

    return best
def _extract_contextual_price(text: str, req: dict) -> float | None:
    candidates = _extract_price_candidates_with_context(text)
    if not candidates:
        return None

    meal_plan = (req.get("meal_plan") or "none").lower()
    meal_words = [w.lower() for w in MEAL_KEYWORDS.get(meal_plan, [])] if meal_plan != "none" else []
    reserve_required = bool(req.get("reserve_duty_only"))
    club_required = bool(req.get("club_membership_only"))
    free_cancel_required = bool(req.get("free_cancel"))
    room_keyword = (req.get("room_keyword") or "").strip().lower()

    scored: list[tuple[int, int, float]] = []

    for value, pos, ctx in candidates:
        score = 0
        wide_ctx = (text or "")[max(0, pos - 900): pos + 900].lower()
        has_reserve_marker = any(word in ctx for word in RESERVE_DUTY_EXCLUDE_TOKENS)

        if meal_words:
            if any(word in ctx for word in meal_words) or any(word in wide_ctx for word in meal_words):
                score += 20
            else:
                continue

        if reserve_required:
            if any(word in ctx for word in RESERVE_DUTY_KEYWORDS):
                score += 15
            else:
                continue
        else:
            if has_reserve_marker:
                continue

        if club_required:
            has_club = any(word in ctx for word in CLUB_MEMBERSHIP_KEYWORDS) or any(word in wide_ctx for word in CLUB_MEMBERSHIP_KEYWORDS)
            if has_club:
                score += 10
            else:
                continue

        if free_cancel_required:
            if FREE_CANCEL_RE.search(ctx):
                score += 8
            else:
                continue

        if room_keyword:
            if room_keyword in ctx:
                score += 10
            else:
                continue

        # Prefer primary/actual offers in context.
        if "מחיר אתר" in ctx or "מחיר מיוחד" in ctx or "from" in ctx:
            score += 3

        # Avoid crossed/old prices hints.
        if "\u20aa" in ctx and "\u0336" in ctx:
            score -= 2

        scored.append((score, pos, value))

    if not scored:
        return None

    # Highest score first; if tied, earlier on page first (not global min).
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0][2]
    logger.info("[Scraper] Contextual price matched: %s", best)
    return best


def _dan_meal_section_end(text: str, section_start: int, token_len: int) -> int:
    """Cut a meal row before the next tariff heading so we don't mix room-only and breakfast prices."""
    scan = section_start + max(token_len, 6)
    end = len(text)
    markers = [
        "לינה בלבד",
        "חדר בלבד",
        "כולל ארוחת בוקר",
        "חצי פנסיון",
        "פנסיון מלא",
        "הכל כלול",
        "room only",
        "with breakfast",
        "breakfast included",
        "half board",
        "full board",
        "all inclusive",
    ]
    for lbl in markers:
        p = text.find(lbl, scan)
        if p != -1 and p < end:
            end = p
    return min(end, section_start + 950)


def _dan_site_and_club_in_section(block: str) -> tuple[float | None, float | None]:
    """
    Take מחיר אתר / מחיר באתר first, then מחיר חברי מועדון *after* that site line
    so breakfast+club is not confused with the previous row's club-only price.
    """
    site_price: float | None = None
    club_price: float | None = None

    m_site = re.search(r"מחיר\s*באתר[^0-9]*([0-9][0-9,]{2,})", block, flags=re.IGNORECASE)
    if not m_site:
        m_site = re.search(r"מחיר\s*אתר[^0-9]*([0-9][0-9,]{2,})", block, flags=re.IGNORECASE)
    if m_site:
        site_price = _to_float(m_site.group(1))
        tail = block[m_site.end() :]
    else:
        tail = block

    m_club = re.search(r"מחיר\s*חברי\s*מועדון[^0-9]*([0-9][0-9,]{2,})", tail, flags=re.IGNORECASE)
    if m_club:
        club_price = _to_float(m_club.group(1))

    if site_price is None and club_price is None:
        m_site2 = re.search(r"מחיר\s*ב?אתר[^0-9]*([0-9][0-9,]{2,})", block, flags=re.IGNORECASE)
        if m_site2:
            site_price = _to_float(m_site2.group(1))
        m_club2 = re.search(r"מחיר\s*חברי\s*מועדון[^0-9]*([0-9][0-9,]{2,})", block, flags=re.IGNORECASE)
        if m_club2:
            club_price = _to_float(m_club2.group(1))

    return site_price, club_price


def _extract_dan_offer_price(text: str, req: dict) -> float | None:
    """
    Dan hotels search results pages show meal labels in Hebrew and prices as plain numbers,
    with separate "מחיר אתר" (site price) and "מחיר חברי מועדון" (club price).

    Example snippet:
      "חדר בלבד ... מחיר אתר 2,521 ... מחיר חברי מועדון E-DAN 2,320"
      "כולל ארוחת בוקר ... מחיר אתר 3,014 ... מחיר חברי מועדון E-DAN 2,773"
    """
    meal_plan = (req.get("meal_plan") or "none").lower()
    reserve_only = bool(req.get("reserve_duty_only"))
    prefer_club_price = bool(req.get("club_membership_only"))

    if not text:
        return None

    lower = text.lower()

    if meal_plan == "breakfast":
        meal_tokens = ["כולל ארוחת בוקר", "with breakfast", "breakfast included"]
    elif meal_plan == "half_board":
        meal_tokens = ["חצי פנסיון", "half board", "half-board"]
    elif meal_plan == "full_board":
        meal_tokens = ["פנסיון מלא", "הכל כלול", "full board", "all inclusive"]
    else:
        meal_tokens = ["לינה בלבד", "חדר בלבד", "room only", "no meals", "no breakfast"]

    reserve_markers = [
        "מילואים",
        "3010",
        "reserve duty",
        "reservist",
        "משרתי המילואים",
        "שובר נופש",
    ]

    best: float | None = None

    for token in meal_tokens:
        for m in re.finditer(re.escape(token.lower()), lower):
            start = m.start()
            end = _dan_meal_section_end(text, start, len(token))
            block = text[start:end]
            block_lower = block.lower()

            has_reserve = any(r in block_lower for r in reserve_markers)
            if reserve_only and not has_reserve:
                continue
            if not reserve_only and has_reserve:
                # If we want non-duty, skip blocks that clearly show duty markers.
                continue

            site_price, club_price = _dan_site_and_club_in_section(block)

            chosen: float | None = None
            if prefer_club_price and club_price is not None:
                chosen = club_price
            elif site_price is not None:
                chosen = site_price
            elif club_price is not None:
                chosen = club_price
            else:
                # Last resort: first large-looking number in the block.
                nums = re.findall(r"[0-9][0-9,]{2,}", block)
                for n in nums:
                    val = _to_float(n)
                    if val is not None:
                        chosen = val
                        break

            if chosen is not None and (best is None or chosen < best):
                best = chosen

    # If room-only label isn't available, use the cheapest site/club price as a fallback.
    if best is None and meal_plan not in ("breakfast", "half_board", "full_board"):
        prices: list[float] = []
        if prefer_club_price:
            for m in re.finditer(r"מחיר\s*חברי\s*מועדון[^0-9]*([0-9][0-9,]{2,})", lower, flags=re.IGNORECASE):
                v = _to_float(m.group(1))
                if v is not None:
                    prices.append(v)
        else:
            for m in re.finditer(r"מחיר\s*ב?אתר[^0-9]*([0-9][0-9,]{2,})", lower, flags=re.IGNORECASE):
                v = _to_float(m.group(1))
                if v is not None:
                    prices.append(v)
        if prices:
            best = min(prices)

    return best


def _extract_fattal_offer_price(text: str, req: dict) -> float | None:
    """
    Fattal room-selection pages show meal labels plus both "מחיר באתר" and "מחיר לחברי מועדון".
    We extract the meal-specific site price and apply Miluim/reserve-duty filtering inside that block.
    """
    if not text:
        return None

    meal_plan = (req.get("meal_plan") or "none").lower()
    reserve_only = bool(req.get("reserve_duty_only"))
    prefer_club_price = bool(req.get("club_membership_only"))
    lower = text.lower()

    if meal_plan == "breakfast":
        meal_tokens = ["לינה וארוחת בוקר", "ארוחת בוקר", "breakfast included", "with breakfast", "b&b", "bb"]
    elif meal_plan == "half_board":
        meal_tokens = ["חצי פנסיון", "half board", "half-board", "breakfast and dinner"]
    elif meal_plan == "full_board":
        meal_tokens = ["פנסיון מלא", "הכל כלול", "full board", "all inclusive"]
    else:
        meal_tokens = ["לינה בלבד", "חדר בלבד", "room only", "no meals", "no breakfast"]

    reserve_markers = [
        "מילואים",
        "3010",
        "reservist",
        "reserve duty",
        "שובר נופש",
        "משרתי מילואים",
        "המילואים",
    ]

    best: float | None = None

    for token in meal_tokens:
        for m in re.finditer(re.escape(token.lower()), lower):
            start = m.start()
            block = text[start : start + 1100]
            block_lower = block.lower()

            has_reserve = any(r in block_lower for r in reserve_markers)
            if reserve_only and not has_reserve:
                continue
            if not reserve_only and has_reserve:
                continue

            # Prefer site price in this block.
            site_idx = block_lower.find("מחיר באתר")
            club_idx = block_lower.find("מחיר לחברי מועדון")

            site_price: float | None = None
            club_price: float | None = None

            def _first_number_near_label(window: str) -> float | None:
                nums = re.findall(r"[0-9][0-9,]{2,}", window)
                if not nums:
                    return None
                for n in nums:
                    v = _to_float(n)
                    if v is not None:
                        return v
                return None

            if site_idx != -1:
                site_window = block[site_idx : site_idx + 260]
                site_price = _first_number_near_label(site_window)
            if club_idx != -1:
                club_window = block[club_idx : club_idx + 260]
                club_price = _first_number_near_label(club_window)

            if prefer_club_price and club_price is not None:
                chosen = club_price
            else:
                chosen = site_price if site_price is not None else club_price

            if chosen is None:
                # Fallback: any price in the block
                chosen = _extract_first_price_from_text(block)

            if chosen is not None and (best is None or chosen < best):
                best = chosen

    # If "room only" label doesn't exist on the page, fall back to cheapest site/club price.
    if best is None and meal_plan not in ("breakfast", "half_board", "full_board"):
        prices: list[float] = []
        if prefer_club_price:
            for m in re.finditer(r"מחיר\s*לחברי\s*מועדון[^0-9]*([0-9][0-9,]{2,})", text, flags=re.IGNORECASE):
                v = _to_float(m.group(1))
                if v is not None:
                    prices.append(v)
        else:
            for m in re.finditer(r"מחיר\s*באתר[^0-9]*([0-9][0-9,]{2,})", text, flags=re.IGNORECASE):
                v = _to_float(m.group(1))
                if v is not None:
                    prices.append(v)
            if not prices:
                for m in re.finditer(r"מחיר\s*אתר[^0-9]*([0-9][0-9,]{2,})", text, flags=re.IGNORECASE):
                    v = _to_float(m.group(1))
                    if v is not None:
                        prices.append(v)
        if prices:
            best = min(prices)

    return best


def _has_meal_plan(text: str, plan: str) -> bool:
    if plan == "none":
        return True
    lower = (text or "").lower()
    return any(keyword.lower() in lower for keyword in MEAL_KEYWORDS.get(plan, []))


def _has_reserve_duty(text: str) -> bool:
    lower = (text or "").lower()
    return any(token in lower for token in RESERVE_DUTY_KEYWORDS)


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    clean = SCRIPT_STYLE_RE.sub(" ", html)
    clean = TAG_RE.sub(" ", clean)
    clean = clean.replace("&nbsp;", " ").replace("&#160;", " ")
    clean = SPACE_RE.sub(" ", clean)
    return clean


def _to_float(raw: str) -> float | None:
    try:
        value = float((raw or "").replace(",", ""))
    except ValueError:
        return None
    if 200 <= value <= 100_000:
        return value
    return None


def _pkg_to_dict(pkg: RoomPackage) -> dict:
    return {
        "price": pkg.price,
        "room_name": pkg.room_name,
        "breakfast": pkg.breakfast,
        "half_board": pkg.half_board,
        "full_board": pkg.full_board,
        "free_cancel": pkg.free_cancel,
        "reserve_duty": pkg.reserve_duty,
    }














