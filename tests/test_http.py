from __future__ import annotations

import types
from collections import deque
from typing import Any, Deque, Iterable, List, Mapping, Optional, Tuple

import pytest  # type: ignore[import]
import requests

from scraper.http import DEFAULT_BASE_URL, DEFAULT_USER_AGENTS, SputnikClient


class DummyResponse(requests.Response):
    def __init__(self, url: str, text: str = "", status_code: int = 200) -> None:
        super().__init__()
        self.status_code = status_code
        self._content = text.encode("utf-8")
        self.encoding = "utf-8"
        self.url = url


def _make_client(
    outcomes: Iterable[Any],
    *,
    base_url: str = DEFAULT_BASE_URL,
    **client_kwargs: Any,
) -> Tuple[SputnikClient, List[dict[str, Any]]]:
    client = SputnikClient(base_url=base_url, **client_kwargs)
    queue: Deque[Any] = deque(outcomes)
    calls: List[dict[str, Any]] = []

    def fake_request(
        self,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Any] = None,
        timeout: Optional[float] = None,
        headers: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> DummyResponse:
        if not queue:
            raise AssertionError("No more responses configured for DummyResponse queue")
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "timeout": timeout,
                "headers": headers,
                "extras": kwargs,
            }
        )
        outcome = queue.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client.session.request = types.MethodType(fake_request, client.session)  # type: ignore[assignment]
    return client, calls


def test_get_builds_absolute_url_and_forwards_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = DummyResponse(url="https://example.com/best/albums/2024/", text="<html></html>")
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)
    client, calls = _make_client([response], base_url="https://example.com", min_interval=0)

    result = client.get("/best/albums/2024/", params={"page": 1}, timeout=5)

    assert result is response
    assert calls == [
        {
            "method": "GET",
            "url": "https://example.com/best/albums/2024/",
            "params": {"page": 1},
            "data": None,
            "timeout": 5,
            "headers": {"User-Agent": DEFAULT_USER_AGENTS[0]},
            "extras": {},
        }
    ]


def test_request_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [
        requests.exceptions.Timeout("boom"),
        DummyResponse(url=f"{DEFAULT_BASE_URL}/foo", text="<html></html>"),
    ]

    sleep_calls: List[float] = []
    monkeypatch.setattr("scraper.http.time.sleep", sleep_calls.append)
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)

    client, calls = _make_client(outcomes, min_interval=0, backoff_factor=0.1, max_retries=1)

    result = client.request("GET", "/foo")

    assert result.url == f"{DEFAULT_BASE_URL}/foo"
    assert len(calls) == 2
    assert sleep_calls == [pytest.approx(0.1)]
    assert client.total_retries == 1


def test_rate_limiting_enforces_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [
        DummyResponse(url=f"{DEFAULT_BASE_URL}/first"),
        DummyResponse(url=f"{DEFAULT_BASE_URL}/second"),
    ]

    sleep_calls: List[float] = []
    monkeypatch.setattr("scraper.http.time.sleep", sleep_calls.append)
    monotonic_values = iter([0.0, 0.0, 0.1, 0.15, 0.2, 1.2, 1.25, 1.3])
    monkeypatch.setattr("scraper.http.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)

    client, calls = _make_client(outcomes, min_interval=1.0)

    client.request("GET", "/first")
    client.request("GET", "/second")

    assert sleep_calls == pytest.approx([0.8])
    assert [entry["url"] for entry in calls] == [
        f"{DEFAULT_BASE_URL}/first",
        f"{DEFAULT_BASE_URL}/second",
    ]
    assert [entry["headers"]["User-Agent"] for entry in calls] == [
        DEFAULT_USER_AGENTS[0],
        DEFAULT_USER_AGENTS[1],
    ]


def test_retry_after_header_overrides_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    retry_response = DummyResponse(url=f"{DEFAULT_BASE_URL}/foo", status_code=429)
    retry_response.headers["Retry-After"] = "2"
    success_response = DummyResponse(url=f"{DEFAULT_BASE_URL}/foo", text="ok")

    sleep_calls: List[float] = []
    monkeypatch.setattr("scraper.http.time.sleep", sleep_calls.append)
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)

    client, _ = _make_client(
        [retry_response, success_response],
        min_interval=0,
        max_retries=1,
        backoff_factor=0.1,
    )

    result = client.request("GET", "/foo")

    assert result is success_response
    assert sleep_calls == [pytest.approx(2.0)]
    assert client.total_retries == 1


def test_http_error_not_in_retry_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    failing_response = DummyResponse(url=f"{DEFAULT_BASE_URL}/foo", status_code=404)

    sleep_calls: List[float] = []
    monkeypatch.setattr("scraper.http.time.sleep", sleep_calls.append)
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)

    client, _ = _make_client([failing_response], min_interval=0, max_retries=2)

    with pytest.raises(requests.HTTPError):
        client.request("GET", "/foo")

    assert sleep_calls == []
    assert client.total_retries == 0


def test_rate_limiter_allows_burst_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        DummyResponse(url=f"{DEFAULT_BASE_URL}/one"),
        DummyResponse(url=f"{DEFAULT_BASE_URL}/two"),
        DummyResponse(url=f"{DEFAULT_BASE_URL}/three"),
    ]

    sleep_calls: List[float] = []
    monkeypatch.setattr("scraper.http.time.sleep", sleep_calls.append)
    monotonic_values = iter([0.0, 0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 1.3, 1.35, 1.4])
    monkeypatch.setattr("scraper.http.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)

    client, calls = _make_client(responses, min_interval=1.0, burst=2)

    client.request("GET", "/one")
    client.request("GET", "/two")
    client.request("GET", "/three")

    assert sleep_calls == [pytest.approx(0.7)]
    assert [entry["url"] for entry in calls] == [
        f"{DEFAULT_BASE_URL}/one",
        f"{DEFAULT_BASE_URL}/two",
        f"{DEFAULT_BASE_URL}/three",
    ]


def test_observer_receives_attempt_events(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [
        requests.exceptions.Timeout("boom"),
        DummyResponse(url=f"{DEFAULT_BASE_URL}/foo", text="<html></html>"),
    ]

    sleep_calls: List[float] = []
    monkeypatch.setattr("scraper.http.time.sleep", sleep_calls.append)
    monkeypatch.setattr("scraper.http.random.uniform", lambda *_: 0.0)

    events: List[Any] = []

    def observer(event: Any) -> None:
        events.append(event)

    client, _ = _make_client(
        outcomes,
        min_interval=0,
        backoff_factor=0.1,
        max_retries=1,
        observer=observer,
    )

    client.request("GET", "/foo")

    assert len(events) == 2
    first, second = events
    assert first.will_retry is True and first.retry_in == pytest.approx(0.1)
    assert second.will_retry is False and second.status_code == 200
