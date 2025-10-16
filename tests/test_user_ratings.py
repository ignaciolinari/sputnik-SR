from __future__ import annotations

from scraper.user_ratings import fetch_user_ratings, parse_user_ratings_page

SAMPLE_RATINGS_HTML = """
<html>
  <body>
    <table class="ratings">
      <tr>
        <td><a href="/album/123/Foo-Bar/">Foo Bar</a></td>
        <td><a href="/bands/foo">Foo Artist</a></td>
        <td>4.5 superb</td>
        <td>05/01/24</td>
      </tr>
      <tr>
        <td><a href="/album/456/Baz-Quux/">Baz Quux</a></td>
        <td>Baz Collective</td>
        <td>3 average</td>
        <td>April 10, 2023</td>
      </tr>
      <tr>
        <td>No album</td>
        <td>Unknown</td>
        <td>2.5 average</td>
        <td>04/12/23</td>
      </tr>
    </table>
    <div class="pagination">
      <a href="?page=1">1</a>
      <a href="?page=2">2</a>
      <a href="?page=3">Next</a>
    </div>
  </body>
</html>
"""

SINGLE_PAGE_RATINGS_HTML = """
<html>
  <body>
    <table class="ratings">
      <tr>
        <td><a href="/album/999/Alpha-Release/">Alpha Release</a></td>
        <td>Alpha Artist</td>
        <td>5 classic</td>
        <td>01/01/22</td>
      </tr>
    </table>
  </body>
</html>
"""


class DummyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.responses: list[tuple[str, str]] = []

    def queue_response(self, url: str, html: str) -> None:
        self.responses.append((url, html))

    def get(self, path: str, params: dict[str, str] | None = None):  # type: ignore[override]
        self.calls.append((path, params))
        try:
            url, html = self.responses.pop(0)
        except IndexError:  # pragma: no cover - defensive
            raise AssertionError("No queued response available") from None
        return type("Response", (), {"text": html, "url": url})


def test_parse_user_ratings_page_extracts_entries_and_pagination() -> None:
    entries, has_more = parse_user_ratings_page(
        SAMPLE_RATINGS_HTML,
        user_id="tester",
        source_url="https://example.com/user/tester/ratings/",
    )

    assert has_more is True
    assert len(entries) == 2

    first = entries[0]
    assert first.user_id == "tester"
    assert first.release_id == 123
    assert first.release_title == "Foo Bar"
    assert first.artist_name == "Foo Artist"
    assert first.rating == 4.5
    assert first.rating_date == "2024-05-01"
    assert first.url == "https://example.com/user/tester/ratings/"

    second = entries[1]
    assert second.release_id == 456
    assert second.artist_name == "Baz Collective"
    assert second.rating == 3.0
    assert second.rating_date == "2023-04-10"


def test_fetch_user_ratings_stops_when_no_more_pages() -> None:
    client = DummyClient()
    client.queue_response(
        "https://example.com/user/tester/ratings/",
        SAMPLE_RATINGS_HTML,
    )
    client.queue_response(
        "https://example.com/user/tester/ratings/?page=2",
        SINGLE_PAGE_RATINGS_HTML,
    )

    entries = fetch_user_ratings("tester", client=client)
    assert len(entries) == 3
    assert [entry.release_id for entry in entries] == [123, 456, 999]
    assert client.calls == [
        ("/user/tester/ratings/", None),
        ("/user/tester/ratings/", {"page": 2}),
    ]
