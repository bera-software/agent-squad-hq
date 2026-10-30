"""Shared standard-library HTTP helper for every adapter.

One place for timeouts, bounded retry with exponential backoff (honouring
``Retry-After``), and mapping HTTP/network failures onto the adapter error
hierarchy in :mod:`squadhq.interfaces`:

=====================================  =================================
Condition                              Raised
=====================================  =================================
timeout, connection reset, DNS, OSError ``TransientError``
408, 425, 429, 5xx, 529                ``TransientError``
429/403 with a quota-exhausted body    ``CapacityError``
401, 403, 400, 404, 402, other 4xx     ``FatalError``
billing / credit-balance bodies        ``FatalError`` (``provider_wide``)
=====================================  =================================

``CapacityError`` and ``FatalError`` are never retried here. Callers that run
their own retry policy (the LLM fallback chain) pass ``retries=0``.

Tests never touch the network: pass ``transport=`` to :func:`request` (or to
an adapter's constructor), or use :class:`FakeTransport`.
"""
from __future__ import annotations

import io
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from email.message import Message as _EmailMessage
from typing import Any

from squadhq.interfaces import AdapterError, CapacityError, FatalError, TransientError

__all__ = [
    "HTTPResponse",
    "Transport",
    "FakeTransport",
    "request",
    "request_json",
    "stream_lines",
    "classify_http_error",
    "parse_link_next",
    "DEFAULT_TIMEOUT",
]

DEFAULT_TIMEOUT = 30.0
MAX_BACKOFF = 30.0
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})

_CAPACITY_RE = re.compile(
    r"insufficient_quota|quota (has been )?exceeded|exceeded your current quota|resource_exhausted"
    r"|usage limit|session limit|weekly limit",
    re.I,
)
_BILLING_RE = re.compile(r"credit balance|billing|payment required|plan does not|account is not active", re.I)
_MODEL_RE = re.compile(
    r"model.{0,25}(is )?(overloaded|(currently )?unavailable|not found|does not exist|(has been )?deprecated)"
    r"|model_not_found|invalid model|unknown model|unsupported model|no (longer )?access to (this|that) model"
    r"|deploymentnotfound",
    re.I,
)


@dataclass
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body.strip():
            return None
        return json.loads(self.body)

    def header(self, name: str, default: str | None = None) -> str | None:
        low = name.lower()
        for k, v in self.headers.items():
            if k.lower() == low:
                return v
        return default


# A transport takes a prepared request and a timeout and returns something
# with ``.read()`` (``.status``/``.headers`` optional) or raises like urlopen.
Transport = Callable[[urllib.request.Request, float], Any]


def _default_transport(req: urllib.request.Request, timeout: float) -> Any:
    # Looked up at call time so ``mock.patch("urllib.request.urlopen")`` works.
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - URLs are adapter-built https endpoints


def _headers_to_dict(h: Any) -> dict[str, str]:
    if h is None:
        return {}
    if isinstance(h, _EmailMessage) or hasattr(h, "items"):
        try:
            return {str(k): str(v) for k, v in h.items()}
        except Exception:
            return {}
    return {}


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                return None
    return None


def classify_http_error(status: int, body: str, headers: Mapping[str, str] | None = None) -> AdapterError:
    """Map an HTTP failure to ``TransientError`` / ``CapacityError`` / ``FatalError``."""
    headers = headers or {}
    snippet = body[:2000]
    msg = f"HTTP {status}: {snippet}"
    model_specific = bool(_MODEL_RE.search(body))
    retry_after = _retry_after(headers)
    if status in (429, 403) and _CAPACITY_RE.search(body):
        return CapacityError(msg, status=status, body=snippet, retry_after=retry_after, provider_wide=True)
    if status == 402 or (400 <= status < 500 and status != 429 and _BILLING_RE.search(body)):
        return FatalError(msg, status=status, body=snippet, provider_wide=True)
    if status in TRANSIENT_STATUSES or status >= 500:
        # 529 "overloaded" is per-model capacity on Anthropic: a fallback model may answer.
        model_specific = model_specific or status == 529
        return TransientError(msg, status=status, body=snippet, retry_after=retry_after,
                              model_specific=model_specific)
    if status in (401, 403):
        return FatalError(msg, status=status, body=snippet, provider_wide=True)
    if status == 404 and "model" in body.lower():
        model_specific = True
    return FatalError(msg, status=status, body=snippet, model_specific=model_specific)


def _backoff(attempt: int, base: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, MAX_BACKOFF)
    delay: float = base * (2 ** attempt)
    # Small jitter, bounded; deterministic enough for tests that patch sleep.
    return float(min(delay + random.uniform(0, base * 0.1), MAX_BACKOFF))  # noqa: S311 - jitter, not crypto


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    params: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    backoff: float = 1.0,
    transport: Transport | None = None,
    ok_statuses: tuple[int, ...] = (),
) -> HTTPResponse:
    """Send one HTTP request with bounded retry on transient failures.

    ``ok_statuses`` lists extra non-2xx statuses returned instead of raised
    (e.g. ``(404,)`` for an existence probe, ``(409,)`` for a CAS write).
    """
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        url = url + ("&" if "?" in url else "?") + query
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise FatalError(f"refusing non-http(s) URL: {url[:80]}")
    hdrs = {"User-Agent": "agent-squad-hq", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    send = transport or _default_transport
    last: AdapterError | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())  # noqa: S310 - scheme checked above
        try:
            resp = send(req, timeout)
            try:
                raw = resp.read()
                status = int(getattr(resp, "status", None) or getattr(resp, "code", None) or 200)
                rh = _headers_to_dict(getattr(resp, "headers", None))
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            out = HTTPResponse(status=status, headers=rh, body=raw if isinstance(raw, bytes) else bytes(raw), url=url)
            if 200 <= status < 300 or status in ok_statuses:
                return out
            err = classify_http_error(status, out.text, rh)
        except urllib.error.HTTPError as e:
            try:
                ebody = e.read().decode("utf-8", errors="replace") if e.fp is not None else ""
            except Exception:
                ebody = ""
            eh = _headers_to_dict(e.headers)
            if e.code in ok_statuses:
                return HTTPResponse(status=e.code, headers=eh, body=ebody.encode(), url=url)
            err = classify_http_error(e.code, ebody, eh)
        except AdapterError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            err = TransientError(f"network error: {e!r}")
        last = err
        if not err.transient or isinstance(err, CapacityError) or attempt == retries:
            raise err
        time.sleep(_backoff(attempt, backoff, err.retry_after))
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last  # pragma: no cover


def stream_lines(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
) -> Iterator[str]:
    """Send one request and yield the response body line by line as it arrives (server-sent
    events). No retry: a stream that fails part way cannot be replayed. Errors before the body
    map onto the adapter errors exactly like :func:`request`."""
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise FatalError(f"refusing non-http(s) URL: {url[:80]}")
    hdrs = {"User-Agent": "agent-squad-hq", "Accept": "text/event-stream"}
    if headers:
        hdrs.update(headers)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())  # noqa: S310 - scheme checked above
    send = transport or _default_transport
    try:
        resp = send(req, timeout)
    except urllib.error.HTTPError as e:
        try:
            ebody = e.read().decode("utf-8", errors="replace") if e.fp is not None else ""
        except Exception:
            ebody = ""
        raise classify_http_error(e.code, ebody, _headers_to_dict(e.headers)) from None
    except AdapterError:
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise TransientError(f"network error: {e!r}") from None
    try:
        status = int(getattr(resp, "status", None) or getattr(resp, "code", None) or 200)
        if not 200 <= status < 300:
            raise classify_http_error(status, resp.read().decode("utf-8", errors="replace"),
                                      _headers_to_dict(getattr(resp, "headers", None)))
        try:
            for raw in iter(resp.readline, b""):
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except (TimeoutError, ConnectionError, OSError) as e:
            raise TransientError(f"stream interrupted: {e!r}") from None
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()


def request_json(method: str, url: str, **kwargs: Any) -> Any:
    """:func:`request` and decode the JSON body (``None`` for an empty body)."""
    return request(method, url, **kwargs).json()


_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


def parse_link_next(link_header: str | None) -> str | None:
    """Return the ``rel="next"`` URL of an RFC 8288 ``Link`` header (GitHub/GitLab pagination)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        m = _LINK_NEXT_RE.search(part)
        if m:
            return m.group(1)
    return None


@dataclass
class _FakeResp:
    status: int
    headers: dict[str, str]
    body: bytes

    def read(self) -> bytes:
        return self.body

    def readline(self) -> bytes:
        """Line by line, for :func:`stream_lines` (server-sent events)."""
        cut = self.body.find(b"\n")
        line, self.body = (self.body, b"") if cut < 0 else (self.body[:cut + 1], self.body[cut + 1:])
        return line

    def close(self) -> None:
        return None


@dataclass
class FakeTransport:
    """Scripted transport for tests: no network, records every request.

    ``routes`` maps ``"METHOD substring"`` (or just ``"substring"``) to a
    response spec ``(status, body)`` / ``(status, body, headers)`` or a list
    of specs consumed in order (the last one repeats). ``body`` may be a
    ``dict``/``list`` (JSON-encoded), ``str`` or ``bytes``. A status >= 400
    is raised as ``urllib.error.HTTPError`` like urlopen would; an
    ``Exception`` instance as spec is raised as-is.
    """

    routes: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, route: str, *specs: Any) -> FakeTransport:
        self.routes[route] = list(specs) if len(specs) > 1 else specs[0]
        return self

    def _match(self, method: str, url: str) -> Any:
        best: tuple[int, str] | None = None
        for key in self.routes:
            m, _, sub = key.partition(" ") if " " in key else ("", "", key)
            if m and m.upper() != method:
                continue
            if sub in url and (best is None or len(sub) > best[0]):
                best = (len(sub), key)
        if best is None:
            raise AssertionError(f"FakeTransport: no route for {method} {url}")
        spec = self.routes[best[1]]
        if isinstance(spec, list):
            return spec.pop(0) if len(spec) > 1 else spec[0]
        return spec

    def __call__(self, req: urllib.request.Request, timeout: float) -> Any:
        method = req.get_method()
        url = req.full_url
        raw = req.data if isinstance(req.data, bytes) else None
        try:
            parsed: Any = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw
        self.calls.append({"method": method, "url": url, "headers": dict(req.header_items()),
                           "body": parsed, "raw": raw, "timeout": timeout})
        spec = self._match(method, url)
        if isinstance(spec, BaseException):
            raise spec
        status, body, *rest = spec
        hdrs: dict[str, str] = dict(rest[0]) if rest else {}
        if isinstance(body, (dict, list)):
            b = json.dumps(body).encode()
        elif isinstance(body, str):
            b = body.encode()
        elif body is None:
            b = b""
        else:
            b = bytes(body)
        if status >= 400:
            msg = _EmailMessage()
            for k, v in hdrs.items():
                msg[k] = v
            raise urllib.error.HTTPError(url, status, "fake", msg, io.BytesIO(b))
        return _FakeResp(status=status, headers=hdrs, body=b)
