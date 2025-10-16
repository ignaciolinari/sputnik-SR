from __future__ import annotations

from scraper.discography import parse_artist_discography

SAMPLE_DISCOGRAPHY_HTML = """
<html>
  <body>
    <table class="discog">
      <tr>
        <td><a href="/album/123/Foo-Album/"><img src="/images/123.jpg" />Foo Album</a></td>
        <td>LP</td>
        <td>2020</td>
        <td>4.3 (120 ratings)</td>
      </tr>
      <tr>
        <td><a href="/album/456/Bar-Album/"><img data-original="/images/456.jpg" />Bar Album</a></td>
        <td>EP</td>
        <td>2018</td>
        <td>3.5 (45 ratings)</td>
      </tr>
      <tr>
        <td>No link</td>
        <td>LP</td>
        <td>2015</td>
        <td>4.0 (10 ratings)</td>
      </tr>
    </table>
  </body>
</html>
"""


def test_parse_artist_discography_extracts_releases() -> None:
    entries = parse_artist_discography(
        SAMPLE_DISCOGRAPHY_HTML,
        artist_id=1,
        source_url="https://example.com/bands/1/",
        base_url="https://example.com",
    )

    assert len(entries) == 2

    first = entries[0]
    assert first.release_id == 123
    assert first.title == "Foo Album"
    assert first.release_type == "LP"
    assert first.release_year == 2020
    assert first.avg_rating == 4.3
    assert first.ratings_count == 120
    assert first.art_url == "https://example.com/images/123.jpg"

    second = entries[1]
    assert second.release_id == 456
    assert second.release_type == "EP"
    assert second.release_year == 2018
    assert second.avg_rating == 3.5
    assert second.ratings_count == 45
    assert second.art_url == "https://example.com/images/456.jpg"
