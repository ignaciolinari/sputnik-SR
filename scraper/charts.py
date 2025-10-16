from __future__ import annotations

import re
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any
from typing import List
from typing import Optional
from typing import Tuple
from urllib.parse import urljoin
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from .http import DEFAULT_BASE_URL
from .http import SputnikClient


ALBUM_PATH_RE = re.compile(r"['\"](/album/[^'\"]+)['\"]")
ALBUM_ID_RE = re.compile(r"/album/(\d+)/")
VOTES_RE = re.compile(r"([\d.,]+)")


@dataclass
class ChartEntry:
    """Single row from a Sputnikmusic best albums chart."""

    year: int
    rank: int
    album_id: int
    album_title: str
    artist_name: str
    album_url: str
    art_url: Optional[str]
    rating: Optional[float]
    votes: Optional[int]
    source_url: str

    def to_dict(self) -> dict:
        """Return a plain ``dict`` representation of the entry."""

        return asdict(self)


def parse_best_album_chart(
    html: str,
    *,
    year: int,
    base_url: str = DEFAULT_BASE_URL,
    source_url: Optional[str] = None,
) -> List[ChartEntry]:
    """Convert a Sputnikmusic "Best Albums" chart page into structured entries."""

    soup = BeautifulSoup(html, "html.parser")
    entries: List[ChartEntry] = []
    resolved_source = source_url or f"{base_url.rstrip('/')}/best/albums/{year}/"

    target_cells = soup.select("td.blackbox[onclick]") or soup.select("td.blackbox")

    for info_cell in target_cells:
        album_path = _extract_album_path(info_cell)
        if not album_path:
            continue

        album_id = _extract_album_id(album_path)
        rank = _extract_rank(info_cell)
        if album_id is None or rank is None:
            continue

        artist_name = _extract_artist_name(info_cell)
        album_title = _extract_album_title(info_cell)
        art_url = _extract_art_url(info_cell, base_url)
        rating, votes = _extract_rating_votes(info_cell)

        absolute_album_url = _build_absolute_url(base_url, album_path)
        if not absolute_album_url:
            continue

        entries.append(
            ChartEntry(
                year=year,
                rank=rank,
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                album_url=absolute_album_url,
                art_url=art_url,
                rating=rating,
                votes=votes,
                source_url=resolved_source,
            )
        )

    entries.sort(key=lambda entry: entry.rank)
    return entries


def fetch_best_albums(
    year: int,
    *,
    client: Optional[SputnikClient] = None,
) -> List[ChartEntry]:
    """Download and parse the "Best Albums" chart for a specific year."""

    owns_client = client is None
    active_client = client or SputnikClient()
    try:
        response = active_client.get(f"/best/albums/{year}/")
        return parse_best_album_chart(
            response.text,
            year=year,
            base_url=active_client.base_url,
            source_url=response.url,
        )
    finally:
        if owns_client:
            active_client.close()


def _extract_album_path(info_cell: Tag) -> Optional[str]:
    onclick = _as_string(info_cell.get("onclick"))
    candidate = _extract_album_path_from_onclick(onclick)
    if candidate:
        return candidate

    data_path = _as_string(info_cell.get("data-album-path"))
    candidate = _normalize_album_path(data_path)
    if candidate:
        return candidate

    anchor = info_cell.find("a", href=True)
    if anchor:
        candidate = _normalize_album_path(_as_string(anchor["href"]))
        if candidate:
            return candidate

    sibling_anchor = info_cell.find_previous("a", href=True)
    if sibling_anchor:
        candidate = _normalize_album_path(_as_string(sibling_anchor["href"]))
        if candidate:
            return candidate

    return None


def _extract_album_path_from_onclick(onclick: str) -> Optional[str]:
    if not onclick:
        return None
    match = ALBUM_PATH_RE.search(onclick)
    if not match:
        return None
    return _normalize_album_path(match.group(1))


def _normalize_album_path(raw: str) -> Optional[str]:
    if not raw:
        return None
    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme or parsed.netloc else raw
    if not path or "/album/" not in path:
        return None
    normalized = path.split("?")[0].split("#")[0].rstrip("/")
    if not normalized:
        return None
    if not normalized.startswith("/"):
        normalized = f"/{normalized.lstrip('/')}"
    return normalized


def _extract_album_id(album_path: str) -> Optional[int]:
    match = ALBUM_ID_RE.search(f"{album_path}/")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_rank(info_cell: Tag) -> Optional[int]:
    rank_cell = info_cell.find_previous("td", class_="blackbox")
    if rank_cell is None:
        return None
    raw = rank_cell.get_text(strip=True)
    digits = re.findall(r"\d+", raw)
    if not digits:
        return None
    try:
        return int(digits[0])
    except ValueError:
        return None


def _extract_artist_name(info_cell: Tag) -> str:
    artist_tag = info_cell.find("b")
    if artist_tag:
        text = artist_tag.get_text(strip=True)
        if text:
            return text
    anchor = info_cell.find("a")
    if anchor:
        text = anchor.get_text(strip=True)
        if text:
            return text
    return ""


def _extract_album_title(info_cell: Tag) -> str:
    title_tag = info_cell.find("font", class_="darktext")
    return title_tag.get_text(strip=True) if title_tag else ""


def _extract_art_url(info_cell: Tag, base_url: str) -> Optional[str]:
    rank_cell = info_cell.find_previous("td", class_="blackbox")
    if rank_cell is None:
        return None
    img = rank_cell.find("img")
    if not img:
        return None
    candidate = (
        _as_string(img.get("data-original"))
        or _as_string(img.get("data-src"))
        or _as_string(img.get("src"))
    )
    if not candidate:
        return None
    return _build_absolute_url(base_url, candidate)


def _extract_rating_votes(info_cell: Tag) -> Tuple[Optional[float], Optional[int]]:
    rating_block = info_cell.find("font", attrs={"color": "#333333"})
    if rating_block is None:
        return None, None

    rating_value = None
    rating_font = rating_block.find("font")
    if rating_font:
        rating_value = _safe_float(rating_font.get_text(strip=True))

    votes_value = None
    votes_tag = rating_block.find("font", class_="contrasttext")
    if votes_tag:
        votes_value = _parse_votes(votes_tag.get_text())

    return rating_value, votes_value


def _safe_float(value: str) -> Optional[float]:
    try:
        if not value:
            return None
        normalized = value.replace(",", ".")
        return float(normalized)
    except ValueError:
        return None


def _parse_votes(value: str) -> Optional[int]:
    normalized_value = value.replace(",", "").replace(".", "")
    match = VOTES_RE.search(normalized_value)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _as_string(value: Optional[Any]) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value if part is not None)
    return str(value)


def _build_absolute_url(base_url: str, candidate: str) -> Optional[str]:
    if not candidate:
        return None
    url = urljoin(f"{base_url.rstrip('/')}/", candidate.lstrip("/"))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url
