from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from .http import DEFAULT_BASE_URL
from .http import SputnikClient


LOGGER = logging.getLogger(__name__)

_ALBUM_ID_RE = re.compile(r"/album/(\d+)/")


@dataclass(slots=True)
class ArtistReleaseEntry:
    artist_id: int
    release_id: int
    title: str
    release_type: Optional[str]
    release_year: Optional[int]
    art_url: Optional[str]
    avg_rating: Optional[float]
    ratings_count: Optional[int]
    source_url: str


def fetch_artist_discography(
    artist_id: int,
    *,
    client: Optional[SputnikClient] = None,
) -> List[ArtistReleaseEntry]:
    owns_client = client is None
    active_client = client or SputnikClient()
    try:
        response = active_client.get(f"/bands/{artist_id}/")
        return parse_artist_discography(
            response.text,
            artist_id=artist_id,
            source_url=response.url,
            base_url=active_client.base_url,
        )
    finally:
        if owns_client:
            active_client.close()


def parse_artist_discography(
    html: str,
    *,
    artist_id: int,
    source_url: str,
    base_url: str = DEFAULT_BASE_URL,
) -> List[ArtistReleaseEntry]:
    soup = BeautifulSoup(html, "html.parser")
    releases: List[ArtistReleaseEntry] = []

    table = soup.find("table", class_="discog")
    if table is None:
        LOGGER.debug("No discography table found for artist %s", artist_id)
        return []

    rows = table.find_all("tr")
    for row in rows:
        columns = row.find_all("td")
        if len(columns) < 3:
            continue

        anchor = columns[0].find("a", href=True)
        if not anchor:
            continue

        release_id = _extract_release_id(anchor["href"])
        if release_id is None:
            continue

        title = anchor.get_text(strip=True)
        art_url = _resolve_art_url(columns[0], base_url)
        release_type = columns[1].get_text(strip=True) or None
        year = _parse_year(columns[2].get_text(strip=True))

        rating_cell = columns[3] if len(columns) > 3 else None
        avg_rating, ratings_count = _extract_rating_info(rating_cell)

        releases.append(
            ArtistReleaseEntry(
                artist_id=artist_id,
                release_id=release_id,
                title=title,
                release_type=release_type,
                release_year=year,
                art_url=art_url,
                avg_rating=avg_rating,
                ratings_count=ratings_count,
                source_url=source_url,
            )
        )

    return releases


def _extract_release_id(href: str) -> Optional[int]:
    match = _ALBUM_ID_RE.search(href)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_year(value: str) -> Optional[int]:
    value = value.strip()
    if not value or not value.isdigit():
        return None
    try:
        year = int(value)
    except ValueError:
        return None
    if year < 1900 or year > 2100:
        return None
    return year


def _extract_rating_info(cell: Optional[Tag]) -> tuple[Optional[float], Optional[int]]:
    if cell is None:
        return None, None
    rating_text = cell.get_text(strip=True)
    if not rating_text:
        return None, None

    rating = None
    count = None

    rating_match = re.search(r"[0-5](?:\.\d)?", rating_text)
    if rating_match:
        try:
            rating = float(rating_match.group(0))
        except ValueError:
            rating = None

    count_match = re.search(r"(\d[\d,]*)\s*ratings", rating_text, re.IGNORECASE)
    if count_match:
        digits = count_match.group(1).replace(",", "")
        try:
            count = int(digits)
        except ValueError:
            count = None

    return rating, count


def _resolve_art_url(container: Tag, base_url: str) -> Optional[str]:
    img = container.find("img")
    if not img:
        return None
    candidate = img.get("data-original") or img.get("data-src") or img.get("src")
    if not candidate:
        return None
    return urljoin(f"{base_url.rstrip('/')}/", candidate.lstrip("/"))
