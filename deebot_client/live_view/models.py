"""Live view models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, unique
from typing import TYPE_CHECKING

from deebot_client.exceptions import ApiError

if TYPE_CHECKING:
    import datetime as dt


@unique
class LiveViewErrorCode(IntEnum):
    """Error codes returned by the live view endpoints."""

    ROBOT_OFFLINE = 20303
    WRONG_PIN = 30010
    SERVER_ERROR = 30011
    NOT_SUPPORTED = 30013
    PIN_LOCKED = 30014
    INCORRECT_PIN = 30017
    PRODUCT_NOT_FOUND = 30018

    @property
    def is_pin_error(self) -> bool:
        """Return True if retrying with the same PIN cannot succeed."""
        return self in _PIN_ERRORS


_PIN_ERRORS = frozenset(
    {
        LiveViewErrorCode.WRONG_PIN,
        LiveViewErrorCode.INCORRECT_PIN,
        LiveViewErrorCode.PIN_LOCKED,
    }
)


class LiveViewError(ApiError):
    """Live view request refused by the server."""

    def __init__(self, code: int | None, message: str | None = None) -> None:
        text = f"Live view request failed with code {code}"
        if message:
            text += f": {message}"
        super().__init__(text)
        self.code = code

    @property
    def known_code(self) -> LiveViewErrorCode | None:
        """Return the error code as enum, if it is a known one."""
        if self.code is None or self.code not in LiveViewErrorCode:
            return None
        return LiveViewErrorCode(self.code)

    @property
    def is_pin_error(self) -> bool:
        """Return True if the PIN is wrong or locked."""
        return (code := self.known_code) is not None and code.is_pin_error


class KvsError(ApiError):
    """Kinesis Video Streams request failed."""

    def __init__(
        self, operation: str, reason: str, *, status: int | None = None
    ) -> None:
        super().__init__(f"{operation} failed: {reason}")
        self.operation = operation
        self.status = status


@dataclass(frozen=True, kw_only=True)
class AwsCredentials:
    """Temporary AWS credentials."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    expiration: dt.datetime | None = None


@dataclass(frozen=True, kw_only=True)
class LiveViewSession:
    """Live view session, as started by the Ecovacs api."""

    track_id: str
    region: str
    channel: str = field(repr=False)
    client_id: str
    session: str = field(repr=False)
    credentials: AwsCredentials
    expires_at: dt.datetime | None


@dataclass(frozen=True, kw_only=True)
class IceServer:
    """ICE server with a single uri."""

    urls: str
    username: str | None = None
    credential: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, str]:
        """Return the server in the WebRTC configuration format."""
        server = {"urls": self.urls}
        if self.username is not None:
            server["username"] = self.username
        if self.credential is not None:
            server["credential"] = self.credential
        return server


@dataclass(frozen=True, kw_only=True)
class KvsViewerConnection:
    """Everything a WebRTC viewer needs to join the signaling channel."""

    wss_url: str = field(repr=False)
    client_id: str
    channel_arn: str
    ice_servers: tuple[IceServer, ...]
    expires_at: dt.datetime
