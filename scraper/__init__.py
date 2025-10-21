"""Sputnikmusic scraping utilities."""

from .charts import ChartEntry
from .charts import fetch_best_albums
from .charts import parse_best_album_chart
from .discography import ArtistDiscographyPage
from .discography import ArtistReleaseEntry
from .discography import fetch_artist_discography
from .discography import parse_artist_discography
from .http import SputnikClient
from .soundoffs import SoundoffEntry
from .soundoffs import fetch_soundoffs
from .soundoffs import parse_soundoff_page
from .tracklist import TrackEntry
from .tracklist import fetch_tracklist
from .tracklist import parse_tracklist_html
from .user_ratings import fetch_user_ratings
from .user_ratings import parse_user_ratings_page
from .users import UserProfile
from .users import fetch_user_profile
from .users import parse_user_profile


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
    "ArtistDiscographyPage",
    "fetch_artist_discography",
    "parse_artist_discography",
]
