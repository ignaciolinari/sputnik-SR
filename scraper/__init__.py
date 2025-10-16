"""Sputnikmusic scraping utilities."""

from .charts import ChartEntry, fetch_best_albums, parse_best_album_chart
from .discography import (
    ArtistReleaseEntry,
    fetch_artist_discography,
    parse_artist_discography,
)
from .http import SputnikClient
from .soundoffs import SoundoffEntry, fetch_soundoffs, parse_soundoff_page
from .tracklist import TrackEntry, fetch_tracklist, parse_tracklist_html
from .user_ratings import fetch_user_ratings, parse_user_ratings_page
from .users import UserProfile, fetch_user_profile, parse_user_profile

__all__ = [
    "SputnikClient",
    "ChartEntry",
    "fetch_best_albums",
    "parse_best_album_chart",
    "TrackEntry",
    "fetch_tracklist",
    "parse_tracklist_html",
    "SoundoffEntry",
    "fetch_soundoffs",
    "parse_soundoff_page",
    "UserProfile",
    "fetch_user_profile",
    "parse_user_profile",
    "fetch_user_ratings",
    "parse_user_ratings_page",
    "ArtistReleaseEntry",
    "fetch_artist_discography",
    "parse_artist_discography",
]
