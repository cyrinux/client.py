from __future__ import annotations

from collections import defaultdict
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from aiohttp import ClientConnectionError, ClientSession, web
from aiohttp.test_utils import TestServer
import orjson
import pytest

from deebot_client.live_view import (
    AwsCredentials,
    IceServer,
    KvsError,
    LiveViewSession,
    create_viewer_connection,
)
from deebot_client.live_view.kvs import (
    control_plane_url,
    describe_signaling_channel,
    get_ice_server_config,
    get_signaling_channel_endpoints,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_NOW = dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC)
_REGION = "eu-central-1"
_CHANNEL_ARN = "arn:aws:kinesisvideo:eu-central-1:000000000000:channel/fake/1"
_WSS_ENDPOINT = "wss://v-fake.kinesisvideo.eu-central-1.amazonaws.com"
_CREDENTIALS = AwsCredentials(
    access_key_id="ASIAFAKEKEYID",
    secret_access_key="fake-secret-access-key",  # noqa: S106
    session_token="fake-session-token",  # noqa: S106
)
_SESSION = LiveViewSession(
    track_id="FakeTrack1",
    region=_REGION,
    channel="fake-channel-name",
    client_id="fake-client-id",
    session="fake-session-id",
    credentials=_CREDENTIALS,
    expires_at=None,
)
_ICE_SERVERS = [
    {
        "Uris": [
            "turn:198.51.100.1:443?transport=udp",
            "turns:198.51.100.1:443?transport=tcp",
        ],
        "Username": "fake-turn-user-1",
        "Password": "fake-turn-password-1",
        "Ttl": 300,
    },
    {
        "Uris": ["turn:198.51.100.2:443?transport=udp"],
        "Username": "fake-turn-user-2",
        "Password": "fake-turn-password-2",
        "Ttl": 300,
    },
]
_SECRETS = (
    "fake-secret-access-key",
    "fake-session-token",
    "fake-turn-password-1",
    "fake-turn-password-2",
    "X-Amz-Signature",
)


class _FakeKvs:
    """Fake KVS control plane and signaling HTTPS endpoint."""

    def __init__(self) -> None:
        self.url = ""
        self.responses: dict[str, tuple[int, Any]] = {}
        self.requests: defaultdict[str, list[tuple[dict[str, str], Any]]] = defaultdict(
            list
        )
        self._server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/{path:.*}", self._handle)
        self._server = TestServer(app)
        await self._server.start_server(access_log=None)
        self.url = str(self._server.make_url("")).rstrip("/")
        self.responses = {
            "describeSignalingChannel": (
                200,
                {"ChannelInfo": {"ChannelARN": _CHANNEL_ARN, "ChannelName": "x"}},
            ),
            "getSignalingChannelEndpoint": (
                200,
                {
                    "ResourceEndpointList": [
                        {"Protocol": "WSS", "ResourceEndpoint": _WSS_ENDPOINT},
                        {"Protocol": "HTTPS", "ResourceEndpoint": self.url + "/"},
                    ]
                },
            ),
            "get-ice-server-config": (200, {"IceServerList": _ICE_SERVERS}),
        }

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        operation = request.path.rpartition("/")[2]
        self.requests[operation].append(
            (dict(request.headers), orjson.loads(await request.read()))
        )
        status, body = self.responses[operation]
        if isinstance(body, bytes):
            return web.Response(status=status, body=body)
        return web.json_response(
            body, status=status, dumps=lambda o: orjson.dumps(o).decode()
        )


@pytest.fixture
async def kvs() -> AsyncGenerator[_FakeKvs]:
    fake = _FakeKvs()
    await fake.start()
    yield fake
    await fake.stop()


async def test_create_viewer_connection(
    kvs: _FakeKvs, session: ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        connection = await create_viewer_connection(
            _SESSION, session, control_plane=kvs.url, now=_NOW
        )

    assert connection.client_id == "fake-client-id"
    assert connection.channel_arn == _CHANNEL_ARN
    assert connection.expires_at == _NOW + dt.timedelta(seconds=299)
    assert connection.ice_servers == (
        IceServer(urls="stun:stun.kinesisvideo.eu-central-1.amazonaws.com:443"),
        IceServer(
            urls="turn:198.51.100.1:443?transport=udp",
            username="fake-turn-user-1",
            credential="fake-turn-password-1",
        ),
        IceServer(
            urls="turns:198.51.100.1:443?transport=tcp",
            username="fake-turn-user-1",
            credential="fake-turn-password-1",
        ),
        IceServer(
            urls="turn:198.51.100.2:443?transport=udp",
            username="fake-turn-user-2",
            credential="fake-turn-password-2",
        ),
    )

    wss = urlsplit(connection.wss_url)
    assert f"{wss.scheme}://{wss.netloc}" == _WSS_ENDPOINT
    query = dict(parse_qsl(wss.query))
    assert query["X-Amz-ChannelARN"] == _CHANNEL_ARN
    assert query["X-Amz-ClientId"] == "fake-client-id"
    assert query["X-Amz-Expires"] == "299"
    assert query["X-Amz-Security-Token"] == "fake-session-token"
    assert query["X-Amz-Credential"] == (
        "ASIAFAKEKEYID/20150830/eu-central-1/kinesisvideo/aws4_request"
    )

    assert [body for _, body in kvs.requests["describeSignalingChannel"]] == [
        {"ChannelName": "fake-channel-name"}
    ]
    assert [body for _, body in kvs.requests["getSignalingChannelEndpoint"]] == [
        {
            "ChannelARN": _CHANNEL_ARN,
            "SingleMasterChannelEndpointConfiguration": {
                "Protocols": ["WSS", "HTTPS"],
                "Role": "VIEWER",
            },
        }
    ]
    assert [body for _, body in kvs.requests["get-ice-server-config"]] == [
        {"ChannelARN": _CHANNEL_ARN, "ClientId": "fake-client-id", "Service": "TURN"}
    ]
    for requests in kvs.requests.values():
        headers, _ = requests[0]
        assert headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=ASIAFAKEKEYID/20150830/eu-central-1/"
            "kinesisvideo/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token, "
        )
        assert headers["X-Amz-Date"] == "20150830T123600Z"
        assert headers["X-Amz-Security-Token"] == "fake-session-token"
        assert headers["Content-Type"] == "application/json"

    for secret in _SECRETS:
        assert secret not in caplog.text
        assert secret not in repr(connection)


async def test_create_viewer_connection_uses_the_current_time(
    kvs: _FakeKvs, session: ClientSession
) -> None:
    before = dt.datetime.now(dt.UTC)
    connection = await create_viewer_connection(
        _SESSION, session, control_plane=kvs.url
    )

    assert connection.expires_at >= before + dt.timedelta(seconds=299)
    assert connection.expires_at <= dt.datetime.now(dt.UTC) + dt.timedelta(seconds=299)


def test_control_plane_url() -> None:
    assert control_plane_url("eu-central-1") == (
        "https://kinesisvideo.eu-central-1.amazonaws.com"
    )


@pytest.mark.parametrize("region", ["", "evil.example.com/x", "eu-central-1.x", "EU"])
def test_control_plane_url_rejects_invalid_region(region: str) -> None:
    with pytest.raises(ValueError, match="region"):
        control_plane_url(region)


async def test_http_error_hides_the_aws_message(
    kvs: _FakeKvs, session: ClientSession
) -> None:
    kvs.responses["describeSignalingChannel"] = (
        403,
        {"__type": "AccessDeniedException", "message": "fake-channel-name denied"},
    )

    with pytest.raises(KvsError) as exc_info:
        await describe_signaling_channel(session, _SESSION, control_plane=kvs.url)

    assert exc_info.value.status == 403
    assert exc_info.value.operation == "describeSignalingChannel"
    assert str(exc_info.value) == (
        "describeSignalingChannel failed: HTTP 403 AccessDeniedException"
    )


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"__type": 1}'],
)
async def test_http_error_without_aws_type(
    kvs: _FakeKvs, session: ClientSession, body: bytes
) -> None:
    kvs.responses["describeSignalingChannel"] = (500, body)

    with pytest.raises(KvsError, match=r"^describeSignalingChannel failed: HTTP 500$"):
        await describe_signaling_channel(session, _SESSION, control_plane=kvs.url)


@pytest.mark.parametrize(
    ("operation", "body"),
    [
        ("describeSignalingChannel", b"not json"),
        ("describeSignalingChannel", {"ChannelInfo": {}}),
        ("describeSignalingChannel", []),
        ("getSignalingChannelEndpoint", {"ResourceEndpointList": "x"}),
        (
            "getSignalingChannelEndpoint",
            {"ResourceEndpointList": [{"Protocol": "WSS", "ResourceEndpoint": "x"}]},
        ),
        ("getSignalingChannelEndpoint", {"ResourceEndpointList": ["x"]}),
        ("get-ice-server-config", {"IceServerList": "x"}),
        ("get-ice-server-config", {"IceServerList": [{"Uris": "x"}]}),
        (
            "get-ice-server-config",
            {"IceServerList": [{"Uris": ["turn:x"], "Username": 1, "Password": "p"}]},
        ),
        ("get-ice-server-config", {"IceServerList": ["x"]}),
    ],
)
async def test_invalid_response(
    kvs: _FakeKvs, session: ClientSession, operation: str, body: Any
) -> None:
    kvs.responses[operation] = (200, body)

    with pytest.raises(KvsError, match=f"^{operation} failed: invalid response$"):
        await create_viewer_connection(_SESSION, session, control_plane=kvs.url)


async def test_ice_server_list_may_be_missing(
    kvs: _FakeKvs, session: ClientSession
) -> None:
    kvs.responses["get-ice-server-config"] = (200, {})

    servers = await get_ice_server_config(session, _SESSION, kvs.url, _CHANNEL_ARN)

    assert servers == (
        IceServer(urls="stun:stun.kinesisvideo.eu-central-1.amazonaws.com:443"),
    )


async def test_endpoints(kvs: _FakeKvs, session: ClientSession) -> None:
    endpoints = await get_signaling_channel_endpoints(
        session, _SESSION, _CHANNEL_ARN, control_plane=kvs.url
    )

    assert endpoints == {"WSS": _WSS_ENDPOINT, "HTTPS": kvs.url + "/"}


@pytest.mark.parametrize("error", [TimeoutError(), ClientConnectionError("boom")])
async def test_transport_error(session: ClientSession, error: Exception) -> None:
    with (
        patch.object(session, "post", side_effect=error),
        pytest.raises(KvsError, match=r"^describeSignalingChannel failed: ") as exc,
    ):
        await describe_signaling_channel(session, _SESSION)

    assert exc.value.status is None
    assert exc.value.__cause__ is error
