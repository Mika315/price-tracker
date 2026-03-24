"""
Astral Hotels booking URL validation — app scope is astralhotels.co.il only.
"""
from __future__ import annotations

from urllib.parse import urlparse

ASTRAL_HOST_MARKER = "astralhotels.co.il"


def is_astral_booking_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return False
    return ASTRAL_HOST_MARKER in host


def astral_url_error_message() -> str:
    return (
        "Only Astral Hotels (astralhotels.co.il) booking links are allowed. "
        "Open the hotel on astralhotels.co.il with your dates, then copy the full URL from the address bar."
    )
