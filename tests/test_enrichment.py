from __future__ import annotations

from scraper.soundoffs import parse_soundoff_page
from scraper.tracklist import parse_tracklist_html
from scraper.users import parse_user_profile


SAMPLE_SOUNDOFF_HTML = """
<table style="border-top:8px solid #444;">
  <tr>
    <td>
      <table style="border-bottom:1px dotted #888;">
        <tr>
          <td><font class="reviewheading"><b>4.5</b></font> <font class="mediumtext">superb</font></td>
          <td align="right"><font class="mediumtext"><b><a href="/user/alpha">Alpha User</a></b> | May 3rd 24</font></td>
        </tr>
      </table>
      <table style="border-bottom:1px dotted #888;">
        <tr>
          <td><font class="reviewheading"><b>3.0</b></font> <font class="mediumtext">good</font></td>
          <td align="right"><font class="mediumtext"><b><a href="/user/beta">beta</a></b> | December 27th 11</font></td>
        </tr>
      </table>
    </td>
  </tr>
</table>
"""

SOUNDOFF_WITH_ROLE_HTML = """
<table style="border-top:8px solid #444;">
  <tr>
    <td>
      <table style="border-bottom:1px dotted #888;">
        <tr>
          <td><font class="reviewheading"><b>4.3</b></font> <font class="mediumtext">superb</font></td>
          <td align="right"><font class="mediumtext"><b><a href="/user/fripp">Frippertronics</a> <font class="brighttext" size="1">EMERITUS</font></b> | March 1st 20</font></td>
        </tr>
      </table>
    </td>
  </tr>
</table>
"""


SAMPLE_TRACKLIST_HTML = """
<html><body>
<table><tr><td>Tracklist for <b>Example</b>:<br>
01. First Song 3:30 <br>
02. Second Song 5:15<br>
03. Outro 1:02
</td></tr></table>
</body></html>
"""


SAMPLE_USER_HTML = """
<html><body>
<font size="6">Alpha User</font>
<table>
  <tr>
    <td><font class="category">Soundoffs</font> <font class="normal">12</font></td>
  </tr>
  <tr>
    <td><font class="category">Album Ratings</font> <font class="normal">34</font></td>
  </tr>
  <tr>
    <td><font class="category">Last Active</font> <font class="normal">05-03-24 11:45 pm</font></td>
  </tr>
  <tr>
    <td><font class="category">Joined</font> <font class="normal">04-01-10</font></td>
  </tr>
</table>
</body></html>
"""


SAMPLE_USER_HTML_WITH_MEMBER_ID = """
<html><body>
<font size="6">Beta User</font>
<a href="/uservote.php?memberid=98765">Ratings</a>
<table>
  <tr>
    <td><font class="category">Album Ratings</font> <font class="normal">12</font></td>
  </tr>
</table>
</body></html>
"""


def test_parse_soundoff_page_extracts_entries() -> None:
    entries = parse_soundoff_page(
        SAMPLE_SOUNDOFF_HTML,
        album_id=123,
        source_url="https://example.com/soundoff.php?albumid=123",
    )
    assert len(entries) == 2

    first = entries[0]
    assert first.user_id == "alpha"
    assert first.rating == 4.5
    assert first.rating_label == "superb"
    assert first.rating_date == "2024-05-03"
    assert first.user_role is None

    second = entries[1]
    assert second.user_id == "beta"
    assert second.rating_date == "2011-12-27"
    assert second.user_role is None


def test_parse_tracklist_html_returns_tracks() -> None:
    tracks = parse_tracklist_html(SAMPLE_TRACKLIST_HTML)
    assert [(track.position, track.title, track.duration_seconds) for track in tracks] == [
        (1, "First Song", 210),
        (2, "Second Song", 315),
        (3, "Outro", 62),
    ]


def test_parse_user_profile_normalizes_fields() -> None:
    profile = parse_user_profile(SAMPLE_USER_HTML, user_id="alpha")
    assert profile is not None
    assert profile.display_name == "Alpha User"
    assert profile.soundoffs == 12
    assert profile.ratings_count == 34
    assert profile.join_date == "2010-04-01"
    assert profile.last_active == "2024-05-03T23:45:00"
    assert profile.role is None
    assert profile.member_id is None


def test_parse_soundoff_page_detects_user_role() -> None:
    entries = parse_soundoff_page(
        SOUNDOFF_WITH_ROLE_HTML,
        album_id=42,
        source_url="https://example.com/soundoff.php?albumid=42",
    )
    assert len(entries) == 1
    assert entries[0].user_role == "EMERITUS"
    assert entries[0].user_display == "Frippertronics"


def test_parse_user_profile_extracts_member_id() -> None:
    profile = parse_user_profile(SAMPLE_USER_HTML_WITH_MEMBER_ID, user_id="beta")
    assert profile is not None
    assert profile.member_id == "98765"
    assert profile.ratings_count == 12
