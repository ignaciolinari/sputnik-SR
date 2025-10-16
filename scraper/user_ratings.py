from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from .http import SputnikClient

LOGGER = logging.getLogger(__name__)

_PAGE_LINK_RE = re.compile(r"page=(\d+)")
_RATING_RE = re.compile(r"([0-5](?:\.5)?)")


@dataclass(slots=True)
class UserRatingEntry:
    user_id: str
    release_id: int
    release_title: str
    artist_name: str
    rating: Optional[float]
    rating_date: Optional[str]
    url: str


def fetch_user_ratings(
    user_id: str,
    *,
    client: SputnikClient,
    max_pages: Optional[int] = None,
) -> List[UserRatingEntry]:
    page = 1
    results: List[UserRatingEntry] = []

    while True:
        response = client.get(
            f"/user/{user_id}/ratings/",
            params={"page": page} if page > 1 else None,
        )
        page_entries, has_more = parse_user_ratings_page(
            response.text,
            user_id=user_id,
            source_url=response.url,
        )
        results.extend(page_entries)

        if not has_more:
            break
        if max_pages is not None and page >= max_pages:
            break
        page += 1

    return results


def parse_user_ratings_page(
    html: str,
    *,
    user_id: str,
    source_url: str,
) -> tuple[List[UserRatingEntry], bool]:
    soup = BeautifulSoup(html, "html.parser")
    entries: List[UserRatingEntry] = []

    table = soup.find("table", class_="ratings")
    if table is None:
        LOGGER.debug("No ratings table found for user %s", user_id)
        return [], False

    rows = table.find_all("tr")
    for row in rows:
        columns = row.find_all("td")
        if len(columns) < 4:
            continue

        release_anchor = columns[0].find("a", href=True)
        if not release_anchor:
            continue

        release_id = _extract_release_id(release_anchor["href"])
        release_title = release_anchor.get_text(strip=True)

        artist_anchor = columns[1].find("a", href=True)
        artist_name = (
            artist_anchor.get_text(strip=True) if artist_anchor else columns[1].get_text(strip=True)
        )

        rating_value = _extract_rating(columns[2].get_text(strip=True))
        rating_date = _parse_date(columns[3].get_text(strip=True))

        if release_id is None:
            continue

        entries.append(
            UserRatingEntry(
                user_id=user_id,
                release_id=release_id,
                release_title=release_title,
                artist_name=artist_name,
                rating=rating_value,
                rating_date=rating_date,
                url=source_url,
            )
        )

    has_more = _has_next_page(soup)
    return entries, has_more


def _extract_release_id(href: str) -> Optional[int]:
    match = re.search(r"/album/(\d+)/", href)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_rating(value: str) -> Optional[float]:
    match = _RATING_RE.search(value)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_date(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None

    try:
        parsed = datetime.strptime(value, "%m/%d/%y")
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%B %d, %Y")
        except ValueError:
            return None

    year = parsed.year
    if year < 100:
        current_year = datetime.now(timezone.utc).year
        cutoff = (current_year % 100) + 5
        year = 1900 + year if year > cutoff else 2000 + year

    try:
        normalized = parsed.replace(year=year)
    except ValueError:
        return None
    return normalized.date().isoformat()


def _has_next_page(soup: BeautifulSoup) -> bool:
    pager = soup.find("div", class_="pagination")
    if pager is None:
        return False

    for link in pager.find_all("a", href=True):
        if link.get_text(strip=True).lower() in {"next", ">"}:
            return True
        if _PAGE_LINK_RE.search(link["href"] or ""):
            return True
    return False
