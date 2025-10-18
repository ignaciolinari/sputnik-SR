from __future__ import annotations

from scraper.tracklist import parse_tracklist_html


def test_parse_tracklist_ignores_non_positive_positions() -> None:
    html = """
    <html>
      <body>
        0. Hidden Intro 1:23
        1. Awake 3:45
        2. Beyond 4:10
      </body>
    </html>
    """

    tracks = parse_tracklist_html(html)

    assert [track.position for track in tracks] == [1, 2]
    assert [track.title for track in tracks] == ["Awake", "Beyond"]
