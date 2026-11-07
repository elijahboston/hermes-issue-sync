"""Tiny stdlib JSON-over-HTTP client shared by the tracker adapters.

The network goes through an injectable ``transport(method, url, headers, body) -> (status,
headers, body_bytes)`` so tests never touch it. Errors become ``AdapterError`` subclasses whose
messages are safe to show users: status and a short body excerpt, never request headers.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Container, Mapping

from .adapters.base import AdapterError, RateLimited

Transport = Callable[[str, str, Mapping[str, str], "bytes | None"], "tuple[int, Mapping[str, str], bytes]"]

TIMEOUT_SECONDS = 20.0
DEFAULT_RETRY_AFTER = 60.0
EXCERPT_CHARS = 200
# Checked in order after ``Retry-After``: generic, then Linear's request/complexity budgets.
RESET_HEADERS = ("x-ratelimit-reset", "x-ratelimit-requests-reset", "x-ratelimit-complexity-reset")


class HttpError(AdapterError):
    """A non-2xx response. ``status``/``headers``/``body`` let adapters inspect tracker-specific errors."""

    def __init__(self, message: str, status: int, headers: Mapping[str, str], body: bytes) -> None:
        super().__init__(message)
        self.status = status
        self.headers = dict(headers)
        self.body = body


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lower-cased names
    data: Any  # parsed JSON; None for an empty body


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                     timeout: float = TIMEOUT_SECONDS) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


def excerpt(body: bytes, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join(body.decode("utf-8", errors="replace").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _seconds_until(value: str, now: float) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        pass
    else:
        if number > 1e12:  # epoch milliseconds (Linear)
            return number / 1000.0 - now
        if number > 1e9:  # epoch seconds
            return number - now
        return number  # delta seconds
    try:
        moment = parsedate_to_datetime(value)  # HTTP-date
    except (TypeError, ValueError):
        moment = None
    if moment is None:
        from datetime import datetime
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))  # ISO-8601 (Jira)
        except ValueError:
            return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp() - now


def retry_after(headers: Mapping[str, str], now: float | None = None) -> float:
    """Seconds to wait, from ``Retry-After`` or a rate-limit reset header; ``DEFAULT_RETRY_AFTER`` if none."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    now = time.time() if now is None else now
    for name in ("retry-after", *RESET_HEADERS):
        if name in lowered:
            seconds = _seconds_until(lowered[name], now)
            if seconds is not None:
                return max(0.0, seconds)
    return DEFAULT_RETRY_AFTER


class JsonClient:
    def __init__(self, service: str, transport: Transport | None = None, *,
                 timeout: float = TIMEOUT_SECONDS, clock: Callable[[], float] = time.time) -> None:
        self.service = service
        self.timeout = timeout
        self.clock = clock
        self._transport = transport

    def _send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        if self._transport is not None:
            return self._transport(method, url, headers, body)
        return urllib_transport(method, url, headers, body, self.timeout)

    def request(self, method: str, url: str, *, json_body: Any = None, headers: Mapping[str, str] | None = None,
                allow_statuses: Container[int] = ()) -> Response:
        """Send one request. Non-2xx raises unless listed in ``allow_statuses``."""
        sent = {"Accept": "application/json", **(headers or {})}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        try:
            status, raw_headers, raw = self._send(method, url, sent, body)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise AdapterError(f"{self.service}: request failed: {reason}") from None
        resp_headers = {str(k).lower(): str(v) for k, v in (raw_headers or {}).items()}
        raw = raw or b""
        if not 200 <= status < 300 and status not in allow_statuses:
            self._raise_for(status, resp_headers, raw)
        return Response(status, resp_headers, self._parse(status, raw))

    def _raise_for(self, status: int, headers: Mapping[str, str], raw: bytes) -> None:
        if status == 429:
            wait = retry_after(headers, self.clock())
            raise RateLimited(wait, f"{self.service}: rate limited (HTTP 429); retry after {wait:g}s")
        if status in (401, 403):
            raise HttpError(f"{self.service}: HTTP {status}: not authorized; check your API key/token",
                            status, headers, raw)
        detail = excerpt(raw)
        raise HttpError(f"{self.service}: HTTP {status}" + (f": {detail}" if detail else ""), status, headers, raw)

    def _parse(self, status: int, raw: bytes) -> Any:
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise AdapterError(f"{self.service}: HTTP {status} returned invalid JSON: {excerpt(raw)}") from None
