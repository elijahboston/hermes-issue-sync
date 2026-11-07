import json
import urllib.error

import pytest

from ticket_sync.adapters.base import AdapterError, RateLimited
from ticket_sync.http import DEFAULT_RETRY_AFTER, HttpError, JsonClient, retry_after

NOW = 1_700_000_000.0


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, dict(headers), body))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def client(*responses):
    transport = Transport(*responses)
    return JsonClient("svc", transport, clock=lambda: NOW), transport


def test_json_body_is_encoded_and_response_parsed():
    c, t = client((200, {"Content-Type": "application/json", "X-Thing": "1"}, b'{"ok": true}'))
    resp = c.request("POST", "https://x.invalid/api", json_body={"a": 1}, headers={"Authorization": "k"})
    method, url, headers, body = t.requests[0]
    assert (method, url) == ("POST", "https://x.invalid/api")
    assert json.loads(body) == {"a": 1}
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "k"
    assert resp.status == 200 and resp.data == {"ok": True}
    assert resp.headers["x-thing"] == "1"  # names lower-cased


def test_empty_body_parses_to_none():
    c, t = client((204, {}, b""))
    assert c.request("POST", "https://x.invalid").data is None
    assert t.requests[0][3] is None


def test_429_uses_retry_after_seconds():
    c, _ = client((429, {"Retry-After": "12"}, b"slow down"))
    with pytest.raises(RateLimited) as info:
        c.request("GET", "https://x.invalid")
    assert info.value.retry_after == 12


def test_429_uses_epoch_ms_reset_header():
    c, _ = client((429, {"X-RateLimit-Requests-Reset": str(int((NOW + 30) * 1000))}, b""))
    with pytest.raises(RateLimited) as info:
        c.request("GET", "https://x.invalid")
    assert info.value.retry_after == pytest.approx(30)


def test_429_without_headers_defaults_to_60():
    c, _ = client((429, {}, b""))
    with pytest.raises(RateLimited) as info:
        c.request("GET", "https://x.invalid")
    assert info.value.retry_after == DEFAULT_RETRY_AFTER == 60


@pytest.mark.parametrize("value, expected", [
    ("Wed, 15 Nov 2023 22:14:20 GMT", 0.0 + (1700086460 - NOW)),
    (str(int(NOW + 5)), 5.0),
    ("2023-11-14T22:13:30Z", 10.0),
    ("garbage", DEFAULT_RETRY_AFTER),
])
def test_retry_after_formats(value, expected):
    assert retry_after({"Retry-After": value}, NOW) == pytest.approx(expected)


def test_retry_after_in_the_past_is_zero():
    assert retry_after({"x-ratelimit-reset": str(int(NOW - 100))}, NOW) == 0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_name_the_credential(status):
    c, _ = client((status, {"WWW-Authenticate": "Basic"}, b'{"message": "nope"}'))
    with pytest.raises(AdapterError) as info:
        c.request("GET", "https://x.invalid")
    assert "check your API key/token" in str(info.value)
    assert f"HTTP {status}" in str(info.value)
    assert not isinstance(info.value, RateLimited)


def test_other_error_has_status_and_short_excerpt_but_no_headers():
    body = b'{"errorMessages": ["Issue does not exist"]}' + b" " * 10 + b"x" * 1000
    c, _ = client((404, {"X-Secret-Header": "s3cr3t"}, body))
    with pytest.raises(HttpError) as info:
        c.request("GET", "https://x.invalid", headers={"Authorization": "Basic s3cr3t"})
    text = str(info.value)
    assert "HTTP 404" in text and "Issue does not exist" in text
    assert "s3cr3t" not in text
    assert len(text) < 260
    assert info.value.status == 404


def test_allow_statuses_returns_instead_of_raising():
    c, _ = client((400, {}, b'{"errors": [{"message": "bad"}]}'))
    resp = c.request("POST", "https://x.invalid", allow_statuses=(400,))
    assert resp.status == 400 and resp.data["errors"][0]["message"] == "bad"


def test_invalid_json_is_an_adapter_error():
    c, _ = client((200, {}, b"<html>proxy</html>"))
    with pytest.raises(AdapterError, match="invalid JSON"):
        c.request("GET", "https://x.invalid")


def test_network_failure_is_an_adapter_error():
    c, _ = client(urllib.error.URLError("connection refused"))
    with pytest.raises(AdapterError, match="request failed: connection refused"):
        c.request("GET", "https://x.invalid")


def test_default_timeout_is_20_seconds():
    assert JsonClient("svc").timeout == 20
