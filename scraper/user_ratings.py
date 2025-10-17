from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List
from typing import Optional

from bs4 import BeautifulSoup
from bs4 import Tag

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
        params = {"memberid": user_id}
        if page > 1:
            params["page"] = str(page)

        response = client.get("/uservote.php", params=params)
        page_entries, has_more = parse_user_ratings_page(
            response.text,
            user_id=user_id,
            source_url=response.url,
            current_page=page,
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
    current_page: int = 1,
) -> tuple[List[UserRatingEntry], bool]:
    soup = BeautifulSoup(html, "html.parser")
    entries: List[UserRatingEntry] = []

    rating_tables = soup.find_all("table", class_="tableborder")
    if not rating_tables:
        LOGGER.debug("No rating blocks found for user %s", user_id)
        return [], False

    for table in rating_tables:
        group_rating = _extract_group_rating(table)
        if group_rating is None:
            continue

        for row in table.find_all("tr"):
            if not _is_rating_row(row):
                continue

            release_anchor = row.find("a", href=True)
            if not release_anchor:
                continue

            href_value = release_anchor.get("href")
            if not isinstance(href_value, str):
                href_value = ""
            release_id = _extract_release_id(href_value)
            if release_id is None:
                continue

            artist_name, release_title = _extract_artist_and_title(release_anchor)

            entries.append(
                UserRatingEntry(
                    user_id=user_id,
                    release_id=release_id,
                    release_title=release_title,
                    artist_name=artist_name,
                    rating=group_rating,
                    rating_date=None,
                    url=source_url,
                )
            )

    has_more = _has_next_page(soup, current_page)
    return entries, has_more


def _extract_group_rating(table: Tag) -> Optional[float]:
    header = table.find("tr", class_="profilebox")
    if header is None:
        return None

    header_text = header.get_text(" ", strip=True)
    return _extract_rating(header_text)


def _is_rating_row(row: Tag) -> bool:
    classes = {value.lower() for value in (row.get("class") or [])}
    return "default" in classes or "default2" in classes


def _extract_artist_and_title(anchor: Tag) -> tuple[str, str]:
    title_node = anchor.find("font", class_="smalloffset")
    release_title = (
        _normalize_whitespace(title_node.get_text(" ", strip=True)) if title_node else ""
    )

    artist_node = anchor.find("font", class_="mediumbright")
    artist_name = ""
    if artist_node:
        artist_name = _normalize_whitespace(artist_node.get_text(" ", strip=True))
        if release_title:
            artist_name = _normalize_whitespace(artist_name.replace(release_title, ""))

    combined = _normalize_whitespace(anchor.get_text(" ", strip=True))

    if not release_title:
        fallback_title = anchor.find("font", class_="smalloffset")
        if fallback_title:
            release_title = _normalize_whitespace(fallback_title.get_text(" ", strip=True))
        elif " - " in combined:
            _, possible_title = combined.split(" - ", 1)
            release_title = _normalize_whitespace(possible_title)
        else:
            release_title = combined

    if not artist_name:
        if release_title and combined.endswith(release_title):
            candidate = combined[: -len(release_title)].rstrip("- ")
            artist_name = _normalize_whitespace(candidate)
        elif " - " in combined:
            candidate, _ = combined.split(" - ", 1)
            artist_name = _normalize_whitespace(candidate)
        else:
            artist_name = combined if combined != release_title else ""

    if not artist_name:
        artist_name = "Unknown Artist"
    if not release_title:
        release_title = "Unknown Release"

    return artist_name, release_title


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


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


def _has_next_page(soup: BeautifulSoup, current_page: int) -> bool:
    pager = soup.find("div", class_="pagination")
    if pager is None:
        return False

    for link in pager.find_all("a", href=True):
        if link.get_text(strip=True).lower() in {"next", ">"}:
            return True

        raw_href = link.get("href")
        href = raw_href if isinstance(raw_href, str) else ""
        match = _PAGE_LINK_RE.search(href)
        if not match:
            continue

        try:
            page_number = int(match.group(1))
        except (TypeError, ValueError):
            continue

        if page_number >= current_page + 1:
            return True

    return False
