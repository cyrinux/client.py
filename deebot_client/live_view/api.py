"""Ecovacs live view session api (start and end watching)."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import string
import time
from typing import TYPE_CHECKING, Any

from deebot_client.const import (
    PATH_API_APPSVR_AKVS_END_WATCH,
    PATH_API_APPSVR_AKVS_START_WATCH,
    PATH_API_APPSVR_AKVS_START_WATCH_V2,
)
from deebot_client.logging_filter import get_logger

from .kvs import is_valid_region
from .models import AwsCredentials, LiveViewError, LiveViewSession

if TYPE_CHECKING:
    from deebot_client.authentication import Authenticator
    from deebot_client.models import ApiDeviceInfo

_LOGGER = get_logger(__name__)

# Values sent by the app version the requests were taken from
_PLATFORM = "Android"
_AV = "2.1.0"
_APP_CHANNEL = "google_play"
_TRACK_ID_ALPHABET = string.ascii_letters + string.digits
_TRACK_ID_LENGTH = 10
_PIN_PATTERN = re.compile(r"[0-9]{4,16}")
_INVALID_RESPONSE = "Invalid live view response"
# Expirations above this are in milliseconds (year 2286 in seconds)
_MAX_EPOCH_SECONDS = 10_000_000_000


def hash_pin(pin: str) -> str:
    """Return the PIN as the server expects it (never send the PIN itself)."""
    return hashlib.md5(  # format of the api
        f"eco_{pin}".encode(), usedforsecurity=False
    ).hexdigest()


def _new_track_id() -> str:
    return "".join(secrets.choice(_TRACK_ID_ALPHABET) for _ in range(_TRACK_ID_LENGTH))


def _base_params(track_id: str, lang: str) -> dict[str, str]:
    return {
        "videoTrackId": track_id,
        "lang": lang,
        "plat": _PLATFORM,
        "av": _AV,
    }


async def start_watch(
    authenticator: Authenticator,
    device_info: ApiDeviceInfo,
    pin: str | None,
    *,
    lang: str = "en",
) -> LiveViewSession:
    """Start a live view session; the robot starts its camera.

    Devices protected by a PIN need it; without a PIN the no-PIN endpoint is
    called. Raise LiveViewError if the server refuses (see is_pin_error: do
    not retry a wrong PIN, the server locks it after 5 attempts).
    """
    if pin is not None and _PIN_PATTERN.fullmatch(pin) is None:
        raise ValueError("The PIN must be 4 to 16 digits")

    track_id = _new_track_id()
    params = {
        **_base_params(track_id, lang),
        "did": device_info["did"],
        "mid": device_info["class"],
        "res": device_info["resource"],
    }
    path = PATH_API_APPSVR_AKVS_START_WATCH
    if pin is not None:
        params["pwd"] = hash_pin(pin)
        path = PATH_API_APPSVR_AKVS_START_WATCH_V2
    params["channel"] = _APP_CHANNEL

    _LOGGER.debug("Starting live view of a %s device", device_info["class"])
    response = await authenticator.get_authenticated(path, params)
    _raise_on_failure(response)
    return _parse_session(response, track_id)


async def end_watch(
    authenticator: Authenticator,
    session: LiveViewSession,
    *,
    lang: str = "en",
) -> None:
    """End a live view session."""
    params = {
        **_base_params(session.track_id, lang),
        "sid": session.session,
        "clientId": session.client_id,
        "channel": _APP_CHANNEL,
    }
    _LOGGER.debug("Ending live view session")
    response = await authenticator.get_authenticated(
        PATH_API_APPSVR_AKVS_END_WATCH, params
    )
    _raise_on_failure(response)


def _raise_on_failure(response: dict[str, Any]) -> None:
    if response.get("ret") == "ok":
        return
    try:
        code: int | None = int(response.get("code"))  # type: ignore[arg-type]
    except TypeError, ValueError:
        code = None
    message = response.get("message") or response.get("error")
    _LOGGER.debug("Live view request refused with code %s", code)
    raise LiveViewError(code, str(message) if message else None)


def _str_field(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise LiveViewError(None, _INVALID_RESPONSE)
    return value


def _parse_session(response: dict[str, Any], track_id: str) -> LiveViewSession:
    credentials = response.get("credentials")
    if not isinstance(credentials, dict):
        raise LiveViewError(None, _INVALID_RESPONSE)
    region = _str_field(response, "region")
    if not is_valid_region(region):
        raise LiveViewError(None, _INVALID_RESPONSE)

    expires_at = _parse_expiration(credentials)
    return LiveViewSession(
        track_id=track_id,
        region=region,
        channel=_str_field(response, "channel"),
        client_id=_str_field(response, "client_id"),
        session=_str_field(response, "session"),
        credentials=AwsCredentials(
            access_key_id=_str_field(credentials, "AccessKeyId"),
            secret_access_key=_str_field(credentials, "SecretAccessKey"),
            session_token=_str_field(credentials, "SessionToken"),
            expiration=expires_at,
        ),
        expires_at=expires_at,
    )


def _parse_expiration(credentials: dict[str, Any]) -> dt.datetime | None:
    """Return when the credentials expire, from Expiration or else TtlSec."""
    expiration = _parse_timestamp(credentials.get("Expiration"))
    if expiration is not None:
        return expiration

    try:
        ttl = int(credentials.get("TtlSec"))  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return dt.datetime.fromtimestamp(time.time() + ttl, dt.UTC)


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        seconds = value / 1000 if value > _MAX_EPOCH_SECONDS else value
        return dt.datetime.fromtimestamp(seconds, dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    return None
