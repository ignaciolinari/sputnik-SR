from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag
from requests import TooManyRedirects

from .http import DEFAULT_BASE_URL
from .http import SputnikClient


LOGGER = logging.getLogger(__name__)

_ALBUM_ID_RE = re.compile(r"/album/(\d+)/")
_BAND_URL_RE = re.compile(r"/bands/([^/]+)/(?P<artist_id>\d+)/")

_CATEGORY_MAP: dict[str, str] = {
    "lp": "LP",
    "lps": "LP",
    "full-length": "LP",
    "full length": "LP",
    "albums": "LP",
    "album": "LP",
    "live": "LP",
    "live albums": "LP",
    "split": "LP",
    "splits": "LP",
    "split albums": "LP",
    "demos": "LP",
    "demo": "LP",
    "eps": "EP",
    "ep": "EP",
    "mini albums": "EP",
    "singles": "Single",
    "single": "Single",
    "compilation": "Compilation",
    "compilations": "Compilation",
    "soundtracks": "Compilation",
    "soundtrack": "Compilation",
    "boxsets": "Compilation",
    "box sets": "Compilation",
    "box set": "Compilation",
    "best of": "Compilation",
    "misc": "Compilation",
    "miscellaneous": "Compilation",
}


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


@dataclass(slots=True)
class ArtistDiscographyPage:
    artist_id: int
    source_url: str
    releases: List[ArtistReleaseEntry]
    genres: List[str]


def fetch_artist_discography(
    artist_id: int,
    *,
    artist_slug: Optional[str] = None,
    client: Optional[SputnikClient] = None,
) -> ArtistDiscographyPage:
    owns_client = client is None
    active_client = client or SputnikClient()
    try:
        candidate_slugs: list[str] = []
        if artist_slug:
            candidate_slugs.append(artist_slug)
        else:
            try:
                initial_response = active_client.get(f"/bands/{artist_id}/")
            except TooManyRedirects:
                LOGGER.warning("Too many redirects while resolving slug for artist %s", artist_id)
                return ArtistDiscographyPage(
                    artist_id=artist_id,
                    source_url=urljoin(
                        f"{active_client.base_url.rstrip('/')}/", f"bands/{artist_id}/"
                    ),
                    releases=[],
                    genres=[],
                )

            slug_from_url = _extract_artist_slug(initial_response.url, artist_id)
            if not slug_from_url:
                LOGGER.warning("Artist %s has no slug; discography fetch skipped", artist_id)
                return ArtistDiscographyPage(
                    artist_id=artist_id,
                    source_url=initial_response.url,
                    releases=[],
                    genres=[],
                )
            candidate_slugs.append(slug_from_url)

        last_page: Optional[ArtistDiscographyPage] = None
        for slug in candidate_slugs:
            band_path = f"/band/{_format_band_slug(slug)}"
            try:
                response = active_client.get(band_path)
            except TooManyRedirects:
                LOGGER.debug(
                    "Too many redirects while fetching %s for artist %s", band_path, artist_id
                )
                response = None
            else:
                page = parse_artist_discography(
                    response.text,
                    artist_id=artist_id,
                    source_url=response.url,
                    base_url=active_client.base_url,
                )
                last_page = page
                if page.releases or page.genres:
                    return page

            legacy_path = f"/bands/{slug}/{artist_id}/?page=releases"
            try:
                response = active_client.get(legacy_path)
            except TooManyRedirects:
                LOGGER.debug(
                    "Too many redirects while fetching %s for artist %s", legacy_path, artist_id
                )
                continue

            page = parse_artist_discography(
                response.text,
                artist_id=artist_id,
                source_url=response.url,
                base_url=active_client.base_url,
            )
            last_page = page
            if page.releases or page.genres:
                return page

        LOGGER.debug("No discography parsed for artist %s after all attempts", artist_id)
        if last_page is not None:
            return last_page
        return ArtistDiscographyPage(
            artist_id=artist_id,
            source_url=urljoin(f"{active_client.base_url.rstrip('/')}/", f"bands/{artist_id}/"),
            releases=[],
            genres=[],
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
) -> ArtistDiscographyPage:
    soup = BeautifulSoup(html, "html.parser")
    releases: List[ArtistReleaseEntry] = []
    genres = _extract_artist_genres(soup)

    table = soup.find("table", class_="discog")
    if table is not None:
        releases = _parse_legacy_discography_table(
            table,
            artist_id=artist_id,
            source_url=source_url,
            base_url=base_url,
        )
        return ArtistDiscographyPage(
            artist_id=artist_id,
            source_url=source_url,
            releases=releases,
            genres=genres,
        )

    container = soup.find("table", class_="plaincontentbox")
    if container is not None:
        releases = _parse_modern_discography(
            container,
            artist_id=artist_id,
            source_url=source_url,
            base_url=base_url,
        )
        if releases:
            return ArtistDiscographyPage(
                artist_id=artist_id,
                source_url=source_url,
                releases=releases,
                genres=genres,
            )

    releases = _parse_modern_discography(
        soup,
        artist_id=artist_id,
        source_url=source_url,
        base_url=base_url,
    )

    return ArtistDiscographyPage(
        artist_id=artist_id,
        source_url=source_url,
        releases=releases,
        genres=genres,
    )


def _extract_artist_genres(soup: BeautifulSoup) -> List[str]:
    genres: List[str] = []
    seen: set[str] = set()

    for anchor in soup.select("div.tagwrap ul.tags li.tag a"):
        text = anchor.get_text(strip=True)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        genres.append(text)

    if genres:
        return genres

    container = soup.find("div", class_="tagwrap")
    if container is None:
        return []

    raw_text = container.get_text(" ", strip=True)
    for token in re.split(r"[,/]|\s{2,}|\u2022", raw_text):
        cleaned = token.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        genres.append(cleaned)

    return genres


def _parse_legacy_discography_table(
    table: Tag,
    *,
    artist_id: int,
    source_url: str,
    base_url: str,
) -> List[ArtistReleaseEntry]:
    releases: List[ArtistReleaseEntry] = []
    rows = table.find_all("tr")
    for row in rows:
        columns = row.find_all("td")
        if len(columns) < 3:
            continue

        anchor = columns[0].find("a", href=True)
        if not anchor:
            continue

        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue

        release_id = _extract_release_id(href_value)
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


def _parse_modern_discography(
    container: Tag,
    *,
    artist_id: int,
    source_url: str,
    base_url: str,
) -> List[ArtistReleaseEntry]:
    releases: List[ArtistReleaseEntry] = []
    seen_release_ids: set[int] = set()

    for cover_cell in container.find_all("td"):
        width_attr = cover_cell.get("width")
        if width_attr is None:
            continue
        if isinstance(width_attr, list):
            width_value = " ".join(str(item) for item in width_attr)
        else:
            width_value = str(width_attr)
        if width_value.strip() != "120":
            continue

        anchor = cover_cell.find("a", href=True)
        if not anchor:
            continue

        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue

        release_id = _extract_release_id(href_value)
        if release_id is None or release_id in seen_release_ids:
            continue

        details_cell = cover_cell.find_next_sibling("td")
        if details_cell is None:
            continue

        release_type = _infer_release_type(cover_cell)
        entry = _parse_release_card(
            artist_id=artist_id,
            release_id=release_id,
            cover_cell=cover_cell,
            details_cell=details_cell,
            source_url=source_url,
            base_url=base_url,
            release_type=release_type,
        )
        if entry is None:
            continue

        releases.append(entry)
        seen_release_ids.add(release_id)

    if not releases:
        LOGGER.debug("No discography content parsed for artist %s", artist_id)

    return releases


def _parse_release_card(
    *,
    artist_id: int,
    release_id: int,
    cover_cell: Tag,
    details_cell: Tag,
    source_url: str,
    base_url: str,
    release_type: Optional[str],
) -> Optional[ArtistReleaseEntry]:
    art_url = _resolve_art_url(cover_cell, base_url)

    title_anchor: Optional[Tag] = None
    for candidate in details_cell.find_all("a", href=True):
        href_value = candidate.get("href")
        if not isinstance(href_value, str):
            continue
        if _extract_release_id(href_value) == release_id and candidate.get_text(strip=True):
            title_anchor = candidate
            break

    if title_anchor is None:
        title_anchor = details_cell.find("a", href=True)

    title = title_anchor.get_text(strip=True) if title_anchor else ""
    if not title:
        return None

    year = _extract_year_from_details(details_cell)

    rating_container = details_cell.find("center")
    avg_rating, ratings_count = _extract_rating_info(rating_container)

    return ArtistReleaseEntry(
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


def _extract_year_from_details(details_cell: Tag) -> Optional[int]:
    # Prefer the grey date text shown under each album title.
    for font in details_cell.find_all("font"):
        text = font.get_text(strip=True)
        if not text:
            continue
        color_attr = font.get("color")
        if color_attr:
            if isinstance(color_attr, list):
                color_value = " ".join(str(item) for item in color_attr)
            else:
                color_value = str(color_attr)
        else:
            color_value = ""

        if color_value.lower().startswith("#9999"):
            year = _parse_year_from_text(text)
            if year is not None:
                return year

    # Fall back to scanning any text for a 4-digit year.
    for text in details_cell.stripped_strings:
        year = _parse_year_from_text(text)
        if year is not None:
            return year

    return None


def _parse_year_from_text(value: str) -> Optional[int]:
    match = re.search(r"(19|20)\d{2}", value)
    if not match:
        return None
    return _parse_year(match.group(0))


def _infer_release_type(cover_cell: Tag) -> Optional[str]:
    span = cover_cell.find_previous("span")
    if not span:
        return None
    label = span.get_text(strip=True)
    if not label:
        return None
    normalized = label.strip().lower()
    return _CATEGORY_MAP.get(normalized)


def _extract_release_id(href: str) -> Optional[int]:
    match = _ALBUM_ID_RE.search(href)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_artist_slug(url: str, expected_artist_id: int) -> Optional[str]:
    match = _BAND_URL_RE.search(url)
    if not match:
        return None
    try:
        url_artist_id = int(match.group("artist_id"))
    except (ValueError, KeyError):
        return None
    if url_artist_id != expected_artist_id:
        return None
    return match.group(1)


def _format_band_slug(slug: str) -> str:
    candidate = slug.strip().lower().replace(" ", "+").replace("-", "+")
    return re.sub(r"\++", "+", candidate)


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
    rating_text = cell.get_text(separator=" ", strip=True)
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

    count_match = re.search(r"(\d[\d,]*)\s*(ratings|votes)", rating_text, re.IGNORECASE)
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
    if not candidate or not isinstance(candidate, str):
        return None
    return urljoin(f"{base_url.rstrip('/')}/", candidate.lstrip("/"))
