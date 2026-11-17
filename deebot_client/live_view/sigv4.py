"""Minimal AWS Signature Version 4 (header signing and query presigning)."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import hmac
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .models import AwsCredentials

_ALGORITHM = "AWS4-HMAC-SHA256"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, kw_only=True)
class SigningContext:
    """Who signs, for which region and service, and when (a naive time is UTC)."""

    credentials: AwsCredentials
    region: str
    service: str
    now: dt.datetime

    @property
    def amz_date(self) -> str:
        """Return the request timestamp."""
        return self._utc_now.strftime("%Y%m%dT%H%M%SZ")

    @property
    def scope(self) -> str:
        """Return the credential scope."""
        date = self._utc_now.strftime("%Y%m%d")
        return f"{date}/{self.region}/{self.service}/aws4_request"

    @property
    def _utc_now(self) -> dt.datetime:
        if self.now.tzinfo is None:
            return self.now.replace(tzinfo=dt.UTC)
        return self.now.astimezone(dt.UTC)

    def sign(self, canonical_request: str) -> str:
        """Return the signature of the canonical request."""
        string_to_sign = "\n".join(
            (
                _ALGORITHM,
                self.amz_date,
                self.scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            )
        )
        key = ("AWS4" + self.credentials.secret_access_key).encode()
        for part in self.scope.split("/"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def _uri_encode(value: str) -> str:
    # RFC 3986 unreserved characters stay as they are
    return quote(value, safe="-_.~")


def _uri_encode_path(path: str) -> str:
    return "/".join(_uri_encode(segment) for segment in path.split("/"))


def _canonical_query(items: Iterable[tuple[str, str]]) -> str:
    encoded = sorted((_uri_encode(k), _uri_encode(v)) for k, v in items)
    return "&".join(f"{k}={v}" for k, v in encoded)


def sign_headers(
    context: SigningContext,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
) -> dict[str, str]:
    """Return new headers with the date, the session token and the signature.

    The given headers and the host are signed.
    """
    parts = urlsplit(url)
    credentials = context.credentials
    signed = {k.lower(): " ".join(v.strip().split()) for k, v in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = context.amz_date
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token

    names = sorted(signed)
    signed_headers = ";".join(names)
    signature = context.sign(
        "\n".join(
            (
                method,
                _uri_encode_path(parts.path or "/"),
                _canonical_query(parse_qsl(parts.query, keep_blank_values=True)),
                "".join(f"{name}:{signed[name]}\n" for name in names),
                signed_headers,
                hashlib.sha256(body).hexdigest(),
            )
        )
    )
    result = {
        **headers,
        "X-Amz-Date": context.amz_date,
        "Authorization": (
            f"{_ALGORITHM} Credential={credentials.access_key_id}/{context.scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if credentials.session_token:
        result["X-Amz-Security-Token"] = credentials.session_token
    return result


def presign_url(
    context: SigningContext,
    *,
    endpoint: str,
    query: Mapping[str, str],
    expires: int = 299,
) -> str:
    """Presign a GET on the endpoint (scheme://host[/path]), signing only the host.

    The session token is part of the signed query.
    """
    parts = urlsplit(endpoint)
    path = _uri_encode_path(parts.path or "/")
    credentials = context.credentials
    params = {
        **query,
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{credentials.access_key_id}/{context.scope}",
        "X-Amz-Date": context.amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if credentials.session_token:
        params["X-Amz-Security-Token"] = credentials.session_token
    canonical_query = _canonical_query(params.items())
    signature = context.sign(
        "\n".join(
            (
                "GET",
                path,
                canonical_query,
                f"host:{parts.netloc}\n",
                "host",
                _EMPTY_SHA256,
            )
        )
    )
    return f"{parts.scheme}://{parts.netloc}{path}?{canonical_query}&X-Amz-Signature={signature}"
