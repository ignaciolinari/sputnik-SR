from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest  # type: ignore[import]

from scraper.charts import ChartEntry, fetch_best_albums, parse_best_album_chart
from scraper.http import SputnikClient

SAMPLE_CHART_HTML = """
<table>
<tr class="alt1">
<td class=blackbox style="border-bottom:1px solid #ddd;padding-left:25px;" valign=top>
<a href="/album/1/Foo-Album/"><table cellpadding=0 cellspacing=0><tr><td bgcolor=#666666 style="padding:5px; color:#FFF;" valign=top>01 </td><td><img class="lazy" data-original="/images/albums/1.jpg-thumbl" width=110 height=110 border=0></td></tr></table></a>
</td>
<td class="blackbox" width="40%" style="padding-left: 3px;border-bottom:1px solid #ddd;cursor:pointer;" onclick="window.location.href = '/album/1/Foo-Album'">
<font color="#000000" size="3"><b>Foo Artist</b></font><br>
<font class="darktext" size="2">Foo Album</font><br><br>
<table cellpadding=0 cellspacing=0><tr><td><font color="#333333" size="3"><b><font size="3">4.5</font></b><br><font class="contrasttext" size="2">1,234 votes</font></font></td></tr></table>
</td>
</tr>
<tr class="alt1">
<td class=blackbox style="border-bottom:1px solid #ddd;padding-left:25px;" valign=top>
<a href="/album/2/Bar-Album/"><table cellpadding=0 cellspacing=0><tr><td bgcolor=#666666 style="padding:5px; color:#FFF;" valign=top>02 </td><td><img class="lazy" data-original="/images/albums/2.jpg-thumbl" width=110 height=110 border=0></td></tr></table></a>
</td>
<td class="blackbox" width="40%" style="padding-left: 3px;border-bottom:1px solid #ddd;cursor:pointer;" onclick="window.location.href = '/album/2/Bar-Album'">
<font color="#000000" size="3"><b>Bar Artist</b></font><br>
<font class="darktext" size="2">Bar Album</font><br><br>
<table cellpadding=0 cellspacing=0><tr><td><font color="#333333" size="3"><b><font size="3">3.0</font></b><br><font class="contrasttext" size="2">No votes yet</font></font></td></tr></table>
</td>
</tr>
</table>
"""

SAMPLE_NO_ONCLICK_HTML = """
<table>
<tr>
<td class="blackbox" valign=top>
<a href="/album/5/Baz-Album/"><table><tr><td>05</td><td><img data-src="/images/albums/5.jpg"></td></tr></table></a>
</td>
<td class="blackbox">
<a href="/album/5/Baz-Album/">Baz Artist</a><br>
<font class="darktext">Baz Album</font>
<table><tr><td><font color="#333333"><b><font size="3">4,7</font></b><br><font class="contrasttext">1.234 votos</font></font></td></tr></table>
</td>
</tr>
</table>
"""

SAMPLE_INVALID_HTML = """
<table>
<tr>
<td class="blackbox">
<table><tr><td>01</td></tr></table>
</td>
<td class="blackbox" onclick="javascript:void(0)">
<font class="darktext">Invalid Entry</font>
</td>
</tr>
</table>
"""


def test_parse_best_album_chart_basic() -> None:
    entries = parse_best_album_chart(
        SAMPLE_CHART_HTML,
        year=2024,
        base_url="https://example.com",
        source_url="https://example.com/best/albums/2024/",
    )

    assert len(entries) == 2

    first = entries[0]
    assert isinstance(first, ChartEntry)
    assert first.rank == 1
    assert first.album_id == 1
    assert first.album_url == "https://example.com/album/1/Foo-Album"
    assert first.artist_name == "Foo Artist"
    assert first.album_title == "Foo Album"
    assert first.rating == 4.5
    assert first.votes == 1234
    assert first.art_url == "https://example.com/images/albums/1.jpg-thumbl"
    assert first.source_url == "https://example.com/best/albums/2024/"

    second = entries[1]
    assert second.rank == 2
    assert second.album_id == 2
    assert second.votes is None
    assert second.rating == 3.0


def test_fetch_best_albums_uses_existing_client() -> None:
    class DummyClient:
        def __init__(self) -> None:
            self.base_url = "https://example.com"
            self.calls: list[str] = []
            self.closed = False

        def get(self, path: str) -> SimpleNamespace:
            self.calls.append(path)
            return SimpleNamespace(
                text=SAMPLE_CHART_HTML,
                url=f"{self.base_url}{path}",
            )

        def close(self) -> None:  # pragma: no cover - compatibility only
            self.closed = True

    dummy_client = DummyClient()
    entries = fetch_best_albums(2024, client=cast(SputnikClient, dummy_client))

    assert dummy_client.calls == ["/best/albums/2024/"]
    assert dummy_client.closed is False
    assert len(entries) == 2
    assert entries[0].source_url == "https://example.com/best/albums/2024/"


def test_parse_chart_handles_missing_onclick_and_commas() -> None:
    entries = parse_best_album_chart(
        SAMPLE_NO_ONCLICK_HTML,
        year=2024,
        base_url="https://example.com",
        source_url="https://example.com/best/albums/2024/",
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.album_id == 5
    assert entry.album_url == "https://example.com/album/5/Baz-Album"
    assert entry.art_url == "https://example.com/images/albums/5.jpg"
    assert entry.rating == pytest.approx(4.7)
    assert entry.votes == 1234


def test_parse_chart_skips_rows_without_album_path() -> None:
    entries = parse_best_album_chart(
        SAMPLE_INVALID_HTML,
        year=2024,
        base_url="https://example.com",
    )

    assert entries == []
