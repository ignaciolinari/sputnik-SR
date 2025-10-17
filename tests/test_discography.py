from __future__ import annotations

from scraper.discography import parse_artist_discography


SAMPLE_DISCOGRAPHY_HTML = """
<html>
  <body>
    <table class="plaincontentbox">
      <tr>
        <td colspan="6">
          <span><font>LPs</font></span>
        </td>
      </tr>
      <tr>
        <td width="120" style="padding:12px;">
          <a href="/album/123/Foo-Album/">
            <img src="/images/albums/123.jpg-thumbl" />
          </a>
        </td>
        <td style="padding-top:20px;">
          <font size="2"><b><a href="/album/123/Foo-Album/"><font color="#111111">Foo Album</font></a></b></font><br />
          <font color="#999999" size="1">02/10/2020</font>
          <table><tr><td><center>
            <font color="#FF0000" size="4"><b>4.3</b></font><br />
            <font size="1">120 Votes</font>
          </center></td></tr></table>
        </td>
        <td width="120" style="padding:12px;">
          <a href="/album/456/Bar-Album/">
            <img data-original="/images/albums/456.jpg-thumbl" />
          </a>
        </td>
        <td style="padding-top:20px;">
          <font size="2"><b><a href="/album/456/Bar-Album/">Bar Album</a></b></font><br />
          <font color="#999999" size="1">2018</font>
          <table><tr><td><center>
            <font color="#FF0000" size="4"><b>3.5</b></font><br />
            <font size="1">45 Votes</font>
          </center></td></tr></table>
        </td>
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
    assert first.art_url == "https://example.com/images/albums/123.jpg-thumbl"

    second = entries[1]
    assert second.release_id == 456
    assert second.release_type == "LP"
    assert second.release_year == 2018
    assert second.avg_rating == 3.5
    assert second.ratings_count == 45
    assert second.art_url == "https://example.com/images/albums/456.jpg-thumbl"
