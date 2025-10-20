from __future__ import annotations

from scraper.user_ratings import fetch_user_ratings
from scraper.user_ratings import parse_user_ratings_page


SAMPLE_USER_VOTES_HTML = """
<html>
  <body>
    <table class="tableborder">
      <tr class="profilebox">
        <td><b>4.5</b> superb</td>
      </tr>
      <tr class="default">
        <td>
          <a href="/album/123/Foo-Bar/">
            <font class="mediumbright">
              Foo Artist
              <font class="smalloffset">Foo Bar</font>
            </font>
          </a>
        </td>
      </tr>
      <tr class="default2">
        <td>
          <a href="/album/456/Baz-Quux/">
            <font class="mediumbright">
              Baz Collective
              <font class="smalloffset">Baz Quux</font>
            </font>
          </a>
        </td>
      </tr>
      <tr class="alt2">
        <td>Comment we ignore</td>
      </tr>
    </table>
    <table class="tableborder">
      <tr class="profilebox">
        <td><b>3</b> average</td>
      </tr>
      <tr class="default">
        <td>
          <a href="/album/789/Gamma-Release/">
            <font class="mediumbright">
              Gamma Artist
              <font class="smalloffset">Gamma Release</font>
            </font>
          </a>
        </td>
      </tr>
    </table>
    <div class="pagination">
      <a href="uservote.php?memberid=tester&page=1">1</a>
      <a href="uservote.php?memberid=tester&page=2">Next</a>
    </div>
  </body>
</html>
"""

SINGLE_PAGE_USER_VOTES_HTML = """
<html>
  <body>
    <table class="tableborder">
      <tr class="profilebox">
        <td><b>5</b> classic</td>
      </tr>
      <tr class="default">
        <td>
          <a href="/album/999/Alpha-Release/">
            <font class="mediumbright">
              Alpha Artist
              <font class="smalloffset">Alpha Release</font>
            </font>
          </a>
        </td>
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
        SAMPLE_USER_VOTES_HTML,
        user_id="tester",
        source_url="https://example.com/uservote.php?memberid=tester",
    )

    assert has_more is True
    assert len(entries) == 3

    first = entries[0]
    assert first.user_id == "tester"
    assert first.release_id == 123
    assert first.release_title == "Foo Bar"
    assert first.artist_name == "Foo Artist"
    assert first.rating == 4.5
    assert first.rating_date is None
    assert first.url == "https://example.com/uservote.php?memberid=tester"

    second = entries[1]
    assert second.release_id == 456
    assert second.artist_name == "Baz Collective"
    assert second.rating == 4.5
    assert second.rating_date is None

    third = entries[2]
    assert third.release_id == 789
    assert third.artist_name == "Gamma Artist"
    assert third.rating == 3.0
    assert third.rating_date is None


def test_fetch_user_ratings_stops_when_no_more_pages() -> None:
    client = DummyClient()
    client.queue_response(
        "https://example.com/uservote.php?memberid=42&user=tester",
        SAMPLE_USER_VOTES_HTML,
    )
    client.queue_response(
        "https://example.com/uservote.php?memberid=42&user=tester&page=2",
        SINGLE_PAGE_USER_VOTES_HTML,
    )

    entries = fetch_user_ratings("tester", client=client, member_id="42")  # type: ignore[arg-type]
    assert len(entries) == 4
    assert [entry.release_id for entry in entries] == [123, 456, 789, 999]
    assert client.calls == [
        ("/uservote.php", {"memberid": "42", "user": "tester"}),
        ("/uservote.php", {"memberid": "42", "user": "tester", "page": "2"}),
    ]
