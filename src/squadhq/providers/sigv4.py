"""AWS Signature Version 4 request signing (standard library only).

Implements the header-based variant for non-S3 services: each path segment
of the canonical URI is URI-encoded twice (the request path is sent encoded
once), query parameters are sorted and encoded, and the payload hash is
SHA-256 of the body.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping

ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="-_.~")


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def canonical_uri(path: str, *, double_encode: bool = True) -> str:
    if not path:
        return "/"
    segs = path.split("/")
    if double_encode:
        segs = [_enc(s) for s in segs]
    return "/".join(segs)


def canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(pairs))


def sign(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    session_token: str | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, str]:
    """Return the headers to add (``Authorization``, ``X-Amz-Date``, maybe ``X-Amz-Security-Token``).

    Every header in ``headers`` plus ``host`` and ``x-amz-date`` is signed.
    """
    t = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date = t.strftime("%Y%m%d")
    parts = urllib.parse.urlsplit(url)
    to_sign: dict[str, str] = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    to_sign["host"] = parts.netloc
    to_sign["x-amz-date"] = amz_date
    out = {"X-Amz-Date": amz_date}
    if session_token:
        to_sign["x-amz-security-token"] = session_token
        out["X-Amz-Security-Token"] = session_token
    names = sorted(to_sign)
    canonical_headers = "".join(f"{n}:{to_sign[n]}\n" for n in names)
    signed_headers = ";".join(names)
    payload_hash = hashlib.sha256(body).hexdigest()
    creq = "\n".join([
        method.upper(),
        canonical_uri(parts.path, double_encode=service != "s3"),
        canonical_query(parts.query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date}/{region}/{service}/aws4_request"
    sts = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(creq.encode("utf-8")).hexdigest()])
    sig = hmac.new(signing_key(secret_key, date, region, service), sts.encode("utf-8"), hashlib.sha256).hexdigest()
    out["Authorization"] = f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={sig}"
    return out
