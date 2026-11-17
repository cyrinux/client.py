from __future__ import annotations

import datetime as dt

import pytest

from deebot_client.exceptions import ApiError
from deebot_client.live_view import (
    AwsCredentials,
    IceServer,
    KvsViewerConnection,
    LiveViewError,
    LiveViewErrorCode,
    LiveViewSession,
)

_CREDENTIALS = AwsCredentials(
    access_key_id="ASIAFAKEKEYID",
    secret_access_key="fake-secret-access-key",  # noqa: S106
    session_token="fake-session-token",  # noqa: S106
    expiration=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
)


@pytest.mark.parametrize(
    ("code", "is_pin_error"),
    [
        (LiveViewErrorCode.WRONG_PIN, True),
        (LiveViewErrorCode.INCORRECT_PIN, True),
        (LiveViewErrorCode.PIN_LOCKED, True),
        (LiveViewErrorCode.ROBOT_OFFLINE, False),
        (LiveViewErrorCode.SERVER_ERROR, False),
        (LiveViewErrorCode.NOT_SUPPORTED, False),
        (LiveViewErrorCode.PRODUCT_NOT_FOUND, False),
    ],
)
def test_error_code_is_pin_error(code: LiveViewErrorCode, is_pin_error: bool) -> None:
    assert code.is_pin_error is is_pin_error
    error = LiveViewError(int(code), "message")
    assert error.is_pin_error is is_pin_error
    assert error.known_code is code


def test_error_codes_match_the_server() -> None:
    assert {code.value for code in LiveViewErrorCode} == {
        20303,
        30010,
        30011,
        30013,
        30014,
        30017,
        30018,
    }


def test_error_is_an_api_error_with_code_and_message() -> None:
    error = LiveViewError(30013, "this device does not support the request")

    assert isinstance(error, ApiError)
    assert error.code == 30013
    assert error.known_code is LiveViewErrorCode.NOT_SUPPORTED
    assert str(error) == (
        "Live view request failed with code 30013: "
        "this device does not support the request"
    )


@pytest.mark.parametrize("code", [None, 12345])
def test_error_with_unknown_code(code: int | None) -> None:
    error = LiveViewError(code)

    assert error.code == code
    assert error.known_code is None
    assert not error.is_pin_error
    assert str(error) == f"Live view request failed with code {code}"


def test_repr_hides_secrets() -> None:
    session = LiveViewSession(
        track_id="FakeTrack1",
        region="eu-central-1",
        channel="fake-channel-name",
        client_id="fake-client-id",
        session="fake-session-id",
        credentials=_CREDENTIALS,
        expires_at=_CREDENTIALS.expiration,
    )
    connection = KvsViewerConnection(
        wss_url="wss://example.invalid/?X-Amz-Signature=fake-signature",
        client_id="fake-client-id",
        channel_arn="arn:aws:kinesisvideo:eu-central-1:000000000000:channel/fake/1",
        ice_servers=(
            IceServer(
                urls="turn:198.51.100.1:443",
                username="fake-user",
                credential="fake-turn-password",
            ),
        ),
        expires_at=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
    )

    text = repr(session) + repr(connection)

    for secret in (
        "ASIAFAKEKEYID",
        "fake-secret-access-key",
        "fake-session-token",
        "fake-session-id",
        "fake-channel-name",
        "fake-signature",
        "fake-turn-password",
    ):
        assert secret not in text
    assert "fake-client-id" in text


def test_ice_server_as_dict() -> None:
    assert IceServer(urls="stun:stun.example.invalid:443").as_dict() == {
        "urls": "stun:stun.example.invalid:443"
    }
    assert IceServer(
        urls="turns:198.51.100.1:443?transport=tcp",
        username="fake-user",
        credential="fake-turn-password",
    ).as_dict() == {
        "urls": "turns:198.51.100.1:443?transport=tcp",
        "username": "fake-user",
        "credential": "fake-turn-password",
    }
