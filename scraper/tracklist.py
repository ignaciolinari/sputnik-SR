from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from bs4 import BeautifulSoup

from .http import SputnikClient

LOGGER = logging.getLogger(__name__)

_TRACK_LINE_RE = re.compile(r"^(\d+)\.\s*(.+?)(?:\s+((?:\d+:)?\d{1,2}:\d{2}))?$")


@dataclass(slots=True)
class TrackEntry:
    """Representation of a single track in an album tracklist."""

    position: int
    title: str
    duration_seconds: Optional[int]


def fetch_tracklist(album_id: int, *, client: SputnikClient) -> List[TrackEntry]:
    """Download and parse the tracklist modal for a given album."""

    response = client.get("/tracklist.php", params={"albumid": album_id})
    return parse_tracklist_html(response.text)


def parse_tracklist_html(html: str) -> List[TrackEntry]:
    """Convert the contents of ``tracklist.php`` into structured tracks."""

    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if body is None:
        return []

    text = body.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    tracks: List[TrackEntry] = []
    for line in lines:
        match = _TRACK_LINE_RE.match(line)
        if not match:
            continue
        position_text, title, duration_text = match.groups()
        try:
            position = int(position_text)
        except ValueError:
            continue
        normalized_title = title.strip()
        duration_seconds = _parse_duration(duration_text)
        tracks.append(
            TrackEntry(
                position=position,
                title=normalized_title,
                duration_seconds=duration_seconds,
            )
        )

    return tracks


def _parse_duration(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None

    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0
    elif len(numbers) == 3:
        hours, minutes, seconds = numbers
    else:
        return None

    if not (0 <= seconds < 60 and 0 <= minutes < 60):
        return None

    return hours * 3600 + minutes * 60 + seconds
