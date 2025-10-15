from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import cycle
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urljoin

import requests
from requests import RequestException, Response

DEFAULT_BASE_URL = "https://www.sputnikmusic.com"
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_MAX_BACKOFF = 30.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_RATE_LIMIT_JITTER = 0.3
DEFAULT_BURST_SIZE = 1
_JITTER_RANGE = 0.1
DEFAULT_HEADERS: Mapping[str, str] = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
DEFAULT_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 OPR/114.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0",
]


@dataclass(frozen=True)
class RequestAttemptEvent:
    """Runtime telemetry for a single HTTP attempt."""

    method: str
    url: str
    attempt: int
    status_code: Optional[int]
    elapsed: float
    error: Optional[BaseException]
    will_retry: bool
    retry_in: Optional[float]


class SputnikClient:
    """HTTP helper that wraps a :class:`requests.Session` with sensible defaults."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
        headers: Optional[Mapping[str, str]] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        retry_status_codes: Optional[Iterable[int]] = None,
        max_backoff: Optional[float] = DEFAULT_MAX_BACKOFF,
        respect_retry_after: bool = True,
        rate_limit_jitter: float = DEFAULT_RATE_LIMIT_JITTER,
        burst: int = DEFAULT_BURST_SIZE,
        logger: Optional[logging.Logger] = None,
        user_agents: Optional[Iterable[str]] = None,
        rotate_user_agents: bool = True,
        observer: Optional[Callable[[RequestAttemptEvent], None]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = max(0.0, float(backoff_factor))
        self._min_interval = max(0.0, float(min_interval))
        self._retry_status_codes = (
            frozenset(int(code) for code in retry_status_codes)
            if retry_status_codes is not None
            else DEFAULT_RETRY_STATUS_CODES
        )
        self._max_backoff = None if max_backoff is None else max(0.0, float(max_backoff))
        self._respect_retry_after = bool(respect_retry_after)
        self._rate_limit_jitter = max(0.0, float(rate_limit_jitter))
        self._burst_capacity = max(1, int(burst))
        self._tokens = float(self._burst_capacity)
        self._last_refill_ts = time.monotonic()
        self._rate_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._total_attempts = 0
        self._total_retries = 0
        self.logger = logger or logging.getLogger(__name__)
        resolved_agents = tuple(agent for agent in (user_agents or DEFAULT_USER_AGENTS) if agent)
        if not resolved_agents:
            raise ValueError("user_agents must contain at least one non-empty string")
        self.user_agents = list(resolved_agents)
        self._rotate_user_agents = rotate_user_agents
        self._user_agent_cycle = cycle(resolved_agents)
        self._ua_lock = threading.Lock()
        self._observer = observer

        self._ensure_headers(headers)

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> "SputnikClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _absolute_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        url = self._absolute_url(path)
        return self._request_with_retries(
            "GET",
            url,
            params=params,
            data=None,
            timeout=timeout,
            headers=headers,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Any] = None,
        timeout: Optional[float] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        url = self._absolute_url(path)
        return self._request_with_retries(
            method,
            url,
            params=params,
            data=data,
            timeout=timeout,
            headers=headers,
        )

    def _ensure_headers(self, headers: Optional[Mapping[str, str]]) -> None:
        combined_headers = self.session.headers
        for key, value in DEFAULT_HEADERS.items():
            if key not in combined_headers:
                combined_headers[key] = value
        if headers:
            for key, value in headers.items():
                combined_headers[key] = value

    def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]],
        data: Optional[Any],
        timeout: Optional[float],
        headers: Optional[Mapping[str, str]],
    ) -> Response:
        method_upper = method.upper()
        max_attempts = self.max_retries + 1
        last_error: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            self._rate_limit()
            request_headers = self._prepare_headers(headers)
            start_ts = time.monotonic()

            try:
                response = self.session.request(
                    method=method_upper,
                    url=url,
                    params=params,
                    data=data,
                    timeout=timeout or self.timeout,
                    headers=request_headers,
                )
                elapsed = time.monotonic() - start_ts
            except RequestException as exc:
                elapsed = time.monotonic() - start_ts
                last_error = exc
                should_retry = attempt < max_attempts and self._should_retry_exception(exc)
                retry_in = self._compute_retry_delay(attempt, None) if should_retry else None
                self._log_attempt(
                    method_upper,
                    url,
                    attempt,
                    status_code=None,
                    error=exc,
                    will_retry=should_retry,
                    retry_in=retry_in,
                    elapsed=elapsed,
                )
                self._emit_attempt_event(
                    method_upper,
                    url,
                    attempt,
                    status_code=None,
                    elapsed=elapsed,
                    error=exc,
                    will_retry=should_retry,
                    retry_in=retry_in,
                )
                if not should_retry:
                    raise

                if retry_in is not None:
                    self._sleep(retry_in)
                continue

            status_code = response.status_code

            if status_code in self._retry_status_codes and attempt < max_attempts:
                retry_after = self._get_retry_after(response)
                retry_in = self._compute_retry_delay(attempt, retry_after)
                self._log_attempt(
                    method_upper,
                    url,
                    attempt,
                    status_code=status_code,
                    error=None,
                    will_retry=True,
                    retry_in=retry_in,
                    elapsed=elapsed,
                )
                self._emit_attempt_event(
                    method_upper,
                    url,
                    attempt,
                    status_code=status_code,
                    elapsed=elapsed,
                    error=None,
                    will_retry=True,
                    retry_in=retry_in,
                )
                self._sleep(retry_in)
                last_error = RequestException(f"Retryable status {status_code}")
                continue

            try:
                response.raise_for_status()
            except RequestException as exc:
                elapsed = time.monotonic() - start_ts
                last_error = exc
                self._log_attempt(
                    method_upper,
                    url,
                    attempt,
                    status_code=status_code,
                    error=exc,
                    will_retry=False,
                    retry_in=None,
                    elapsed=elapsed,
                )
                self._emit_attempt_event(
                    method_upper,
                    url,
                    attempt,
                    status_code=status_code,
                    elapsed=elapsed,
                    error=exc,
                    will_retry=False,
                    retry_in=None,
                )
                raise

            self._log_attempt(
                method_upper,
                url,
                attempt,
                status_code=status_code,
                error=None,
                will_retry=False,
                retry_in=None,
                elapsed=elapsed,
            )
            self._emit_attempt_event(
                method_upper,
                url,
                attempt,
                status_code=status_code,
                elapsed=elapsed,
                error=None,
                will_retry=False,
                retry_in=None,
            )
            return response

        if last_error is not None:
            raise last_error
        raise RuntimeError("Request retries exhausted without exception.")

    def _rate_limit(self) -> None:
        if self._min_interval <= 0:
            return

        with self._rate_lock:
            now = time.monotonic()
            self._refill_tokens(now)
            if self._tokens >= 1:
                self._tokens -= 1
                return

            deficit = 1 - self._tokens
            wait_for = deficit * self._min_interval
            if self._rate_limit_jitter > 0:
                wait_for += random.uniform(0, self._rate_limit_jitter)

            self._sleep(wait_for)

            now = time.monotonic()
            self._refill_tokens(now)
            if self._tokens >= 1:
                self._tokens -= 1
            else:
                # In pathological cases (e.g., huge jitter) ensure progress.
                self._tokens = max(0.0, self._tokens - 1)

    def _prepare_headers(self, headers: Optional[Mapping[str, str]]) -> Optional[Mapping[str, str]]:
        merged = dict(headers or {})

        if "user-agent" not in {key.lower() for key in merged}:
            user_agent = self._choose_user_agent()
            if user_agent:
                merged["User-Agent"] = user_agent

        if not merged:
            return None
        return merged

    def _choose_user_agent(self) -> Optional[str]:
        if not self.user_agents:
            return None
        if not self._rotate_user_agents:
            return self.user_agents[0]
        with self._ua_lock:
            return next(self._user_agent_cycle)

    @property
    def total_attempts(self) -> int:
        with self._metrics_lock:
            return self._total_attempts

    @property
    def total_retries(self) -> int:
        with self._metrics_lock:
            return self._total_retries

    def _should_retry_exception(self, exc: RequestException) -> bool:
        if isinstance(exc, requests.HTTPError):
            response = exc.response
            return bool(response and response.status_code in self._retry_status_codes)
        return isinstance(exc, (requests.Timeout, requests.ConnectionError))

    def _get_retry_after(self, response: Response) -> Optional[float]:
        if not self._respect_retry_after:
            return None
        retry_after_value = response.headers.get("Retry-After")
        if not retry_after_value:
            return None
        parsed = self._parse_retry_after_header(retry_after_value)
        if parsed is None:
            return None
        return max(0.0, parsed)

    def _parse_retry_after_header(self, value: str) -> Optional[float]:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                retry_dt = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if retry_dt is None:
                return None
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            delta = (retry_dt - datetime.now(timezone.utc)).total_seconds()
            return delta
        else:
            return seconds

    def _compute_retry_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = self.backoff_factor * (2 ** (attempt - 1))
            delay += random.uniform(0, _JITTER_RANGE)
        if self._max_backoff is not None:
            delay = min(delay, self._max_backoff)
        return max(0.0, delay)

    def _sleep(self, seconds: Optional[float]) -> None:
        if not seconds or seconds <= 0:
            return
        time.sleep(seconds)

    def _emit_attempt_event(
        self,
        method: str,
        url: str,
        attempt: int,
        *,
        status_code: Optional[int],
        elapsed: float,
        error: Optional[BaseException],
        will_retry: bool,
        retry_in: Optional[float],
    ) -> None:
        with self._metrics_lock:
            self._total_attempts += 1
            if will_retry:
                self._total_retries += 1

        if self._observer:
            event = RequestAttemptEvent(
                method=method,
                url=url,
                attempt=attempt,
                status_code=status_code,
                elapsed=elapsed,
                error=error,
                will_retry=will_retry,
                retry_in=retry_in,
            )
            try:
                self._observer(event)
            except Exception:  # pragma: no cover - defensive logging
                self.logger.exception("Request observer raised an exception")

    def _log_attempt(
        self,
        method: str,
        url: str,
        attempt: int,
        *,
        status_code: Optional[int],
        error: Optional[BaseException],
        will_retry: bool,
        retry_in: Optional[float],
        elapsed: Optional[float],
    ) -> None:
        if will_retry:
            self.logger.warning(
                "Attempt %s for %s %s failed (%s). Retrying in %.2fs",
                attempt,
                method,
                url,
                f"status={status_code}" if status_code is not None else error,
                retry_in or 0.0,
            )
            return

        if error:
            self.logger.error(
                "Attempt %s for %s %s failed: %s",
                attempt,
                method,
                url,
                error,
                exc_info=error,
            )
        else:
            self.logger.debug(
                "Attempt %s for %s %s succeeded%s",
                attempt,
                method,
                url,
                f" in {elapsed:.3f}s" if elapsed is not None else "",
            )

    def _refill_tokens(self, now: float) -> None:
        if self._tokens >= self._burst_capacity:
            self._last_refill_ts = now
            return
        elapsed = max(0.0, now - self._last_refill_ts)
        if elapsed <= 0:
            return
        tokens_to_add = elapsed / self._min_interval
        self._tokens = min(self._burst_capacity, self._tokens + tokens_to_add)
        self._last_refill_ts = now
