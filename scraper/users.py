from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Optional

from bs4 import BeautifulSoup

from .http import SputnikClient


LOGGER = logging.getLogger(__name__)

_DATE_FORMAT = "%m-%d-%y"
_DATETIME_FORMAT = "%m-%d-%y %I:%M %p"


@dataclass(slots=True)
class UserProfile:
    """Minimal public profile information exposed by Sputnikmusic."""

    user_id: str
    display_name: str
    role: Optional[str]
    join_date: Optional[str]
    last_active: Optional[str]
    soundoffs: Optional[int]
    ratings_count: Optional[int]
    objectivity_score: Optional[float]
    member_id: Optional[str]


def fetch_user_profile(user_id: str, *, client: SputnikClient) -> Optional[UserProfile]:
    """Retrieve and parse a user's public profile."""

    response = client.get(f"/user/{user_id}")
    return parse_user_profile(response.text, user_id=user_id)


def parse_user_profile(html: str, *, user_id: str) -> Optional[UserProfile]:
    soup = BeautifulSoup(html, "html.parser")

    display_name = _extract_display_name(soup, fallback=user_id)
    role = _extract_role(soup)

    stats = {
        font.get_text(strip=True): _extract_stat_value(font)
        for font in soup.select("font.category")
    }

    member_id = _extract_member_id(soup)

    join_date = _normalize_date(stats.get("Joined"))
    last_active = _normalize_datetime(stats.get("Last Active"))
    soundoffs = _safe_int(stats.get("Soundoffs"))
    ratings_count = _safe_int(stats.get("Album Ratings"))
    objectivity = _safe_float(stats.get("Objectivity"))

    return UserProfile(
        user_id=user_id,
        display_name=display_name,
        role=role,
        join_date=join_date,
        last_active=last_active,
        soundoffs=soundoffs,
        ratings_count=ratings_count,
        objectivity_score=objectivity,
        member_id=member_id,
    )


def _extract_display_name(soup: BeautifulSoup, fallback: str) -> str:
    name_font = soup.find("font", size=lambda value: value in {"6", "7"})
    if name_font:
        return name_font.get_text(strip=True)

    title = soup.find("title")
    if title and title.string:
        text = title.string.strip()
        if "|" in text:
            candidate = text.split("|", 1)[0].strip()
            if candidate:
                return candidate
        if text:
            return text

    return fallback


def _extract_member_id(soup: BeautifulSoup) -> Optional[str]:
    ratings_link = soup.select_one('a[href*="uservote.php?memberid="]')
    if ratings_link is None:
        return None

    href = str(ratings_link.get("href") or "")
    match = re.search(r"memberid=(\d+)", href)
    if not match:
        return None
    return match.group(1)


def _extract_stat_value(category_font) -> Optional[str]:
    value_font = category_font.find_next("font", class_="normal")
    if value_font is None:
        return None
    return value_font.get_text(strip=True)


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip()
    date_token = cleaned.split()[0]
    try:
        parsed = datetime.strptime(date_token, _DATE_FORMAT)
    except ValueError:
        return None
    year = _resolve_year(parsed.year % 100)
    try:
        normalized = datetime(year, parsed.month, parsed.day)
    except ValueError:
        return None
    return normalized.date().isoformat()


def _normalize_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # Some profiles omit the time component; fall back to date-only parsing.
    try:
        parsed = datetime.strptime(value.strip(), _DATETIME_FORMAT)
    except ValueError:
        return _normalize_date(value)

    year = _resolve_year(parsed.year % 100)
    normalized = parsed.replace(year=year)
    return normalized.isoformat(timespec="seconds")


def _resolve_year(two_digit_year: int) -> int:
    current_year = datetime.now(timezone.utc).year
    cutoff = (current_year % 100) + 5
    return 1900 + two_digit_year if two_digit_year > cutoff else 2000 + two_digit_year


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = value.rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_role(soup: BeautifulSoup) -> Optional[str]:
    badge = soup.select_one("font.brighttext")
    if badge is None:
        return None
    text = badge.get_text(strip=True)
    return text or None
