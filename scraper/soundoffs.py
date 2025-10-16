from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import List
from typing import Optional
from typing import cast

from bs4 import BeautifulSoup
from bs4.element import Tag

from .http import SputnikClient


LOGGER = logging.getLogger(__name__)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_DATE_PATTERN = re.compile(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{2}|\d{4})")


@dataclass(slots=True)
class SoundoffEntry:
    """Single user rating scraped from a Sputnikmusic soundoff page."""

    album_id: int
    user_id: str
    user_display: str
    user_role: Optional[str]
    rating: float
    rating_label: str
    rating_date: Optional[str]
    soundoff_text: Optional[str]
    source_url: str


def fetch_soundoffs(
    album_id: int, *, client: SputnikClient, limit: Optional[int] = None
) -> List[SoundoffEntry]:
    """Download and parse the complete set of soundoffs for a release."""

    response = client.get("/soundoff.php", params={"albumid": album_id})
    return parse_soundoff_page(
        response.text,
        album_id=album_id,
        source_url=response.url,
        limit=limit,
    )


def parse_soundoff_page(
    html: str,
    *,
    album_id: int,
    source_url: str,
    limit: Optional[int] = None,
) -> List[SoundoffEntry]:
    """Convert a raw soundoff page into structured entries."""

    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one('table[style*="border-top:8px solid"]')
    if container is None:
        LOGGER.debug("No soundoff container found for album %s", album_id)
        return []

    tables = container.select('table[style*="border-bottom:1px dotted"]')
    entries: List[SoundoffEntry] = []

    for table in tables:
        if limit is not None and len(entries) >= limit:
            break

        rating_value = _extract_rating_value(table)
        rating_label = _extract_rating_label(table)
        user_id, user_display, user_role = _extract_user_identity(table)
        if rating_value is None or rating_label is None or user_id is None:
            continue

        rating_date = _extract_rating_date(table)
        soundoff_text = _extract_soundoff_text(table)

        entries.append(
            SoundoffEntry(
                album_id=album_id,
                user_id=user_id,
                user_display=user_display or user_id,
                user_role=user_role,
                rating=rating_value,
                rating_label=rating_label,
                rating_date=rating_date,
                soundoff_text=soundoff_text,
                source_url=source_url,
            )
        )

    return entries


def _extract_rating_value(table: Tag) -> Optional[float]:
    rating_font = table.find("font", class_="reviewheading")
    if rating_font is None:
        return None
    bold = rating_font.find("b")
    text = bold.get_text(strip=True) if bold else rating_font.get_text(strip=True)
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _extract_rating_label(table: Tag) -> Optional[str]:
    medium_fonts = table.find_all("font", class_="mediumtext")
    if not medium_fonts:
        return None
    return medium_fonts[0].get_text(strip=True)


def _extract_user_identity(
    table: Tag,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    anchor = table.find("a", href=lambda href: isinstance(href, str) and href.startswith("/user/"))
    if anchor is None:
        return None, None, None
    href_value = anchor.get("href")
    href = cast(str, href_value or "").strip("/")
    parts = href.split("/")
    user_id = parts[1] if len(parts) > 1 and parts[0] == "user" else parts[-1]
    user_display = anchor.get_text(strip=True)
    user_role = _extract_user_role(anchor)
    return user_id or None, user_display or None, user_role


def _extract_rating_date(table: Tag) -> Optional[str]:
    medium_fonts = table.find_all("font", class_="mediumtext")
    if not medium_fonts:
        return None
    user_font = medium_fonts[-1]
    tail_text = user_font.get_text(separator=" ", strip=True)
    if "|" not in tail_text:
        return None

    date_text = tail_text.split("|")[-1].strip()
    if not date_text:
        return None

    match = _DATE_PATTERN.search(date_text)
    if not match:
        return None

    month_name, day_text, year_text = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None

    try:
        day = int(day_text)
    except ValueError:
        return None

    try:
        year = int(year_text)
    except ValueError:
        return None

    if year < 100:
        current_year = datetime.now(timezone.utc).year
        cutoff = (current_year % 100) + 5
        year = 1900 + year if year > cutoff else 2000 + year

    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _extract_soundoff_text(table: Tag) -> Optional[str]:
    # The public soundoff listing typically omits review text unless logged in.
    # We keep the hook in case the markup exposes it for some entries.
    next_table = table.find_next_sibling("table")
    if not next_table:
        return None
    has_anchor = next_table.find(
        "a", href=lambda href: isinstance(href, str) and href.startswith("/user/")
    )
    rating_font = next_table.find("font", class_="reviewheading")
    if has_anchor or rating_font:
        return None
    text = next_table.get_text(" ", strip=True)
    return text or None


def _extract_user_role(anchor: Tag) -> Optional[str]:
    parent = anchor.parent
    while parent is not None and parent.name not in {"table"}:
        role_font = parent.find("font", class_="brighttext")
        if role_font is not None:
            role_text = role_font.get_text(strip=True)
            return role_text or None
        parent = parent.parent

    sibling_font = anchor.find_next("font", class_="brighttext")
    if sibling_font is not None:
        role_text = sibling_font.get_text(strip=True)
        return role_text or None

    return None
