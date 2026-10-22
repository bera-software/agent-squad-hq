"""squadhq.http: retries, error mapping, pagination helper, fake transport."""
from __future__ import annotations

import io
import urllib.error
from unittest import mock

import pytest

from squadhq import http
from squadhq.http import FakeTransport, classify_http_error, parse_link_next, request, request_json
from squadhq.interfaces import CapacityError, FatalError, TransientError


def test_get_json_with_params_and_headers() -> None:
    t = FakeTransport().add("GET /items", (200, [{"id": 1}], {"Link": '<https://x/items?page=2>; rel="next"'}))
    resp = request("GET", "https://x/items", params={"page": 1, "skip": None, "l": ["a", "b"]},
                   headers={"Authorization": "Bearer t"}, transport=t)
    assert resp.json() == [{"id": 1}]
    assert resp.header("link") == '<https://x/items?page=2>; rel="next"'
    assert resp.header("missing", "d") == "d"
    assert t.calls[0]["url"] == "https://x/items?page=1&l=a&l=b"
    assert t.calls[0]["headers"]["Authorization"] == "Bearer t"
    assert t.calls[0]["timeout"] == http.DEFAULT_TIMEOUT
    assert parse_link_next(resp.header("Link")) == "https://x/items?page=2"


def test_post_json_body_and_text() -> None:
    t = FakeTransport().add("POST /x", (201, "created"))
    r = request("POST", "https://h/x?a=1", json_body={"k": "v"}, params={"b": 2}, transport=t)
    assert r.text == "created" and r.status == 201
    assert t.calls[0]["body"] == {"k": "v"} and t.calls[0]["url"] == "https://h/x?a=1&b=2"
    assert t.calls[0]["headers"]["Content-type"] == "application/json"
    assert request_json("POST", "https://h/x", data=b"raw", transport=FakeTransport().add("/x", (200, b""))) is None


def test_retries_transient_then_succeeds() -> None:
    t = FakeTransport().add("/x", (503, "busy"), (502, "bad gw"), (200, {"ok": True}))
    with mock.patch("squadhq.http.time.sleep") as sleep:
        assert request_json("GET", "https://h/x", transport=t, retries=2) == {"ok": True}
    assert len(t.calls) == 3 and sleep.call_count == 2


def test_retry_after_header_and_exhaustion() -> None:
    t = FakeTransport().add("/x", (429, "slow down", {"Retry-After": "3"}))
    with mock.patch("squadhq.http.time.sleep") as sleep, pytest.raises(TransientError) as ei:
        request("GET", "https://h/x", transport=t, retries=1)
    sleep.assert_called_once_with(3.0)
    assert ei.value.status == 429 and ei.value.retry_after == 3.0


def test_fatal_and_capacity_not_retried() -> None:
    for spec, cls in (((401, "bad creds"), FatalError), ((429, "insufficient_quota"), CapacityError)):
        t = FakeTransport().add("/x", spec)
        with mock.patch("squadhq.http.time.sleep") as sleep, pytest.raises(cls):
            request("GET", "https://h/x", transport=t, retries=3)
        assert len(t.calls) == 1
        sleep.assert_not_called()


def test_network_errors_are_transient() -> None:
    for exc in (urllib.error.URLError("dns"), TimeoutError("t"), ConnectionResetError("r")):
        t = FakeTransport().add("/x", exc)
        with pytest.raises(TransientError):
            request("GET", "https://h/x", transport=t, retries=0)


def test_ok_statuses_returned() -> None:
    t = FakeTransport().add("/x", (404, {"message": "Not Found"}))
    assert request("GET", "https://h/x", transport=t, ok_statuses=(404,)).status == 404


def test_non_2xx_without_httperror() -> None:
    class Resp:
        status = 500
        headers = {"X": "1"}

        def read(self) -> bytes:
            return b"err"

    with pytest.raises(TransientError):
        request("GET", "https://h/x", transport=lambda req, timeout: Resp(), retries=0)

    class Odd:
        status = 302
        headers = None

        def read(self) -> bytes:
            return b""

    assert request("GET", "https://h/x", transport=lambda r, t: Odd(), ok_statuses=(302,)).headers == {}


def test_refuses_non_http_scheme() -> None:
    with pytest.raises(FatalError):
        request("GET", "file:///etc/passwd")


def test_default_transport_uses_urlopen() -> None:
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"a": 1}')) as uo:
        assert request_json("GET", "https://h/x") == {"a": 1}
    assert uo.call_args.kwargs["timeout"] == http.DEFAULT_TIMEOUT
    err = urllib.error.HTTPError("https://h", 500, "x", None, None)  # type: ignore[arg-type]
    with mock.patch("urllib.request.urlopen", side_effect=err), pytest.raises(TransientError):
        request("GET", "https://h/x", retries=0)


@pytest.mark.parametrize(
    "status,body,cls,flags",
    [
        (500, "", TransientError, {}),
        (529, "overloaded", TransientError, {"model_specific": True}),
        (503, "The model is currently unavailable", TransientError, {"model_specific": True}),
        (402, "", FatalError, {"provider_wide": True}),
        (403, "Forbidden", FatalError, {"provider_wide": True}),
        (403, "RESOURCE_EXHAUSTED quota exceeded", CapacityError, {"provider_wide": True}),
        (404, "model gpt-9 does not exist", FatalError, {"model_specific": True}),
        (422, "validation failed", FatalError, {}),
    ],
)
def test_classify(status: int, body: str, cls: type, flags: dict[str, bool]) -> None:
    e = classify_http_error(status, body)
    assert type(e) is cls
    for k, v in flags.items():
        assert getattr(e, k) is v


def test_link_header_edge_cases() -> None:
    assert parse_link_next(None) is None
    assert parse_link_next('<https://a?p=1>; rel="prev"') is None


def test_fake_transport_errors() -> None:
    t = FakeTransport()
    with pytest.raises(AssertionError):
        request("GET", "https://h/none", transport=t)
    t.add("PUT /a", (200, None))
    with pytest.raises(AssertionError):
        request("GET", "https://h/a", transport=t)
    assert request("PUT", "https://h/a", data=b"not json", transport=t).body == b""
    assert t.calls[-1]["body"] == b"not json"
    t.add("/b", (200, bytearray(b"xy")))
    assert request("GET", "https://h/b", transport=t).body == b"xy"
