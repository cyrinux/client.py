from __future__ import annotations

import datetime as dt
import logging
import string
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from deebot_client.const import (
    PATH_API_APPSVR_AKVS_END_WATCH,
    PATH_API_APPSVR_AKVS_START_WATCH,
    PATH_API_APPSVR_AKVS_START_WATCH_V2,
)
from deebot_client.live_view import (
    AwsCredentials,
    LiveViewError,
    LiveViewErrorCode,
    LiveViewSession,
    end_watch,
    hash_pin,
    start_watch,
)

if TYPE_CHECKING:
    from unittest.mock import AsyncMock, Mock

    from deebot_client.models import ApiDeviceInfo

_PIN = "1234"
_PIN_HASH = "adc0bb4a8dc02386529095fa7275f781"
_NOW = 1_900_000_000.0

_DEVICE: ApiDeviceInfo = {
    "class": "fake01",
    "company": "eco-ng",
    "did": "fake-device-id",
    "name": "fake-name",
    "resource": "fake-resource",
}


def _ok_response(**credentials: Any) -> dict[str, Any]:
    return {
        "ret": "ok",
        "credentials": {
            "AccessKeyId": "ASIAFAKEKEYID",
            "SecretAccessKey": "fake-secret-access-key",
            "SessionToken": "fake-session-token",
            **credentials,
        },
        "region": "eu-central-1",
        "channel": "fake-channel-name",
        "client_id": "fake-client-id",
        "session": "fake-session-id",
        "ts": 1_900_000_000_000,
    }


def _get_mock(authenticator: Mock) -> AsyncMock:
    mock: AsyncMock = authenticator.get_authenticated
    return mock


def _awaited_args(authenticator: Mock) -> tuple[str, dict[str, str]]:
    await_args = _get_mock(authenticator).await_args
    assert await_args is not None
    path, params = await_args.args
    return path, params


async def test_start_watch_with_pin(authenticator: Mock) -> None:
    _get_mock(authenticator).return_value = _ok_response(
        Expiration="2030-01-02T03:04:05Z", TtlSec=3600
    )

    session = await start_watch(authenticator, _DEVICE, _PIN)

    _get_mock(authenticator).assert_awaited_once()
    path, params = _awaited_args(authenticator)
    assert path == PATH_API_APPSVR_AKVS_START_WATCH_V2
    track_id = params.pop("videoTrackId")
    assert len(track_id) == 10
    assert set(track_id) <= set(string.ascii_letters + string.digits)
    assert params == {
        "lang": "EN",
        "plat": "Android",
        "av": "2.1.0",
        "did": "fake-device-id",
        "mid": "fake01",
        "res": "fake-resource",
        "pwd": _PIN_HASH,
        "channel": "google_play",
    }
    expires_at = dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert session == LiveViewSession(
        track_id=track_id,
        region="eu-central-1",
        channel="fake-channel-name",
        client_id="fake-client-id",
        session="fake-session-id",
        credentials=AwsCredentials(
            access_key_id="ASIAFAKEKEYID",
            secret_access_key="fake-secret-access-key",  # noqa: S106
            session_token="fake-session-token",  # noqa: S106
            expiration=expires_at,
        ),
        expires_at=expires_at,
    )


async def test_start_watch_without_pin(authenticator: Mock) -> None:
    _get_mock(authenticator).return_value = _ok_response()

    session = await start_watch(authenticator, _DEVICE, None, lang="DE")

    path, params = _awaited_args(authenticator)
    assert path == PATH_API_APPSVR_AKVS_START_WATCH
    assert "pwd" not in params
    assert params["lang"] == "DE"
    assert session.expires_at is None
    assert session.credentials.expiration is None


async def test_start_watch_uses_a_new_track_id_each_time(authenticator: Mock) -> None:
    _get_mock(authenticator).return_value = _ok_response()

    first = await start_watch(authenticator, _DEVICE, _PIN)
    second = await start_watch(authenticator, _DEVICE, _PIN)

    assert first.track_id != second.track_id


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        (
            {"Expiration": "2030-01-02T03:04:05Z"},
            dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        ),
        (
            {"Expiration": "2030-01-02T05:04:05+02:00"},
            dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        ),
        (
            {"Expiration": 1_900_000_100},
            dt.datetime(2030, 3, 17, 17, 48, 20, tzinfo=dt.UTC),
        ),
        (
            {"Expiration": 1_900_000_100_000},
            dt.datetime(2030, 3, 17, 17, 48, 20, tzinfo=dt.UTC),
        ),
        (
            {"Expiration": "1900000100"},
            dt.datetime(2030, 3, 17, 17, 48, 20, tzinfo=dt.UTC),
        ),
        ({"TtlSec": 100}, dt.datetime(2030, 3, 17, 17, 48, 20, tzinfo=dt.UTC)),
        ({"TtlSec": "100"}, dt.datetime(2030, 3, 17, 17, 48, 20, tzinfo=dt.UTC)),
        (
            {"Expiration": "garbage", "TtlSec": 100},
            dt.datetime(2030, 3, 17, 17, 48, 20, tzinfo=dt.UTC),
        ),
        (
            {"Expiration": "2030-01-02T03:04:05"},
            dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        ),
        ({"Expiration": None, "TtlSec": "soon"}, None),
        ({"Expiration": True}, None),
    ],
)
async def test_start_watch_expiration(
    authenticator: Mock, credentials: dict[str, Any], expected: dt.datetime | None
) -> None:
    _get_mock(authenticator).return_value = _ok_response(**credentials)

    with patch("deebot_client.live_view.api.time.time", return_value=_NOW):
        session = await start_watch(authenticator, _DEVICE, _PIN)

    assert session.expires_at == expected
    assert session.credentials.expiration == expected


@pytest.mark.parametrize(
    ("response", "code", "is_pin_error"),
    [
        ({"ret": "fail", "code": 30010, "message": "wrong pin"}, 30010, True),
        ({"ret": "fail", "code": "30017", "error": "wrong pin"}, 30017, True),
        ({"ret": "fail", "code": 30014}, 30014, True),
        ({"ret": "fail", "code": 20303, "message": "offline"}, 20303, False),
        ({"ret": "fail", "code": "not-a-number"}, None, False),
        ({"code": 30011}, 30011, False),
    ],
)
async def test_start_watch_failure(
    authenticator: Mock,
    response: dict[str, Any],
    code: int | None,
    is_pin_error: bool,
) -> None:
    _get_mock(authenticator).return_value = response

    with pytest.raises(LiveViewError) as exc_info:
        await start_watch(authenticator, _DEVICE, _PIN)

    assert exc_info.value.code == code
    assert exc_info.value.is_pin_error is is_pin_error
    assert _PIN_HASH not in str(exc_info.value)


@pytest.mark.parametrize(
    "response",
    [
        {"ret": "ok"},
        {**_ok_response(), "region": 1},
        {**_ok_response(), "credentials": "nope"},
        {**_ok_response(), "credentials": {"AccessKeyId": "x"}},
        {**_ok_response(), "region": "evil.example.com/x"},
    ],
)
async def test_start_watch_invalid_success_response(
    authenticator: Mock, response: dict[str, Any]
) -> None:
    _get_mock(authenticator).return_value = response

    with pytest.raises(LiveViewError, match="Invalid live view response") as exc_info:
        await start_watch(authenticator, _DEVICE, _PIN)

    assert exc_info.value.code is None


@pytest.mark.parametrize("pin", ["", "123", "12a4", " 1234", "1234 ", "١٢٣٤", "1" * 17])
async def test_start_watch_rejects_invalid_pin(authenticator: Mock, pin: str) -> None:
    with pytest.raises(ValueError, match="PIN"):
        await start_watch(authenticator, _DEVICE, pin)

    _get_mock(authenticator).assert_not_called()


async def test_start_watch_does_not_log_secrets(
    authenticator: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    _get_mock(authenticator).return_value = {"ret": "fail", "code": 30010}

    with caplog.at_level(logging.DEBUG), pytest.raises(LiveViewError):
        await start_watch(authenticator, _DEVICE, _PIN)

    assert _PIN_HASH not in caplog.text
    assert "fake-device-id" not in caplog.text


@pytest.mark.parametrize(
    ("pin", "expected"),
    [("1234", _PIN_HASH), ("0042", "1229af426be48db3e0db5abc672ad17f")],
)
def test_hash_pin(pin: str, expected: str) -> None:
    assert hash_pin(pin) == expected


async def test_end_watch(authenticator: Mock) -> None:
    _get_mock(authenticator).return_value = _ok_response()
    session = await start_watch(authenticator, _DEVICE, _PIN)
    _get_mock(authenticator).reset_mock()
    _get_mock(authenticator).return_value = {
        "ret": "ok",
        "code": 0,
        "message": "success",
    }

    await end_watch(authenticator, session)

    _get_mock(authenticator).assert_awaited_once_with(
        PATH_API_APPSVR_AKVS_END_WATCH,
        {
            "videoTrackId": session.track_id,
            "lang": "EN",
            "plat": "Android",
            "av": "2.1.0",
            "sid": "fake-session-id",
            "clientId": "fake-client-id",
            "channel": "google_play",
        },
    )


async def test_end_watch_failure(authenticator: Mock) -> None:
    _get_mock(authenticator).return_value = _ok_response()
    session = await start_watch(authenticator, _DEVICE, _PIN)
    _get_mock(authenticator).return_value = {"ret": "fail", "code": 30011}

    with pytest.raises(LiveViewError) as exc_info:
        await end_watch(authenticator, session, lang="FR")

    assert exc_info.value.known_code is LiveViewErrorCode.SERVER_ERROR
    assert _awaited_args(authenticator)[1]["lang"] == "FR"
