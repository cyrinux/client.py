"""Kinesis Video Streams signaling, viewer side."""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
import orjson

from deebot_client.logging_filter import get_logger

from .models import IceServer, KvsError, KvsViewerConnection
from .sigv4 import SigningContext, presign_url, sign_headers

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .models import LiveViewSession

_LOGGER = get_logger(__name__)

_SERVICE = "kinesisvideo"
# Validity of the presigned signaling url, as the KVS SDKs use it
PRESIGN_EXPIRES = 299
_TIMEOUT = ClientTimeout(30)
# AWS region names, such as eu-central-1; the region becomes part of host names
_REGION_PATTERN = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")
_INVALID_RESPONSE = "invalid response"


def is_valid_region(region: str) -> bool:
    """Return True if the value looks like an AWS region name."""
    return _REGION_PATTERN.fullmatch(region) is not None


def control_plane_url(region: str) -> str:
    """Return the KVS control plane url of the region."""
    if not is_valid_region(region):
        raise ValueError("Invalid AWS region")
    return f"https://kinesisvideo.{region}.amazonaws.com"


def _signing_context(
    session: LiveViewSession, now: dt.datetime | None
) -> SigningContext:
    return SigningContext(
        credentials=session.credentials,
        region=session.region,
        service=_SERVICE,
        now=now or dt.datetime.now(dt.UTC),
    )


async def _post(
    http_session: ClientSession,
    context: SigningContext,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Perform a signed KVS api call; the operation is the last path segment."""
    operation = url.rpartition("/")[2]
    body = orjson.dumps(payload)
    headers = sign_headers(
        context,
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
    )
    _LOGGER.debug("Calling KVS %s", operation)
    try:
        async with http_session.post(
            url, data=body, headers=headers, timeout=_TIMEOUT
        ) as res:
            status = res.status
            text = await res.read()
    except (TimeoutError, ClientError) as ex:
        raise KvsError(operation, type(ex).__name__) from ex

    data = _loads(text)
    if status != 200:
        # AWS error messages may name the channel: only keep the error type
        error_type = data.get("__type") if isinstance(data, dict) else None
        reason = f"HTTP {status}"
        if isinstance(error_type, str):
            reason += f" {error_type}"
        raise KvsError(operation, reason, status=status)
    if not isinstance(data, dict):
        raise KvsError(operation, _INVALID_RESPONSE, status=status)
    return data


def _loads(text: bytes) -> Any:
    try:
        return orjson.loads(text)
    except orjson.JSONDecodeError:
        return None


async def describe_signaling_channel(
    http_session: ClientSession,
    session: LiveViewSession,
    *,
    control_plane: str | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Return the ARN of the signaling channel of the session."""
    operation = "describeSignalingChannel"
    response = await _post(
        http_session,
        _signing_context(session, now),
        f"{control_plane or control_plane_url(session.region)}/{operation}",
        {"ChannelName": session.channel},
    )
    info = response.get("ChannelInfo")
    arn = info.get("ChannelARN") if isinstance(info, dict) else None
    if not isinstance(arn, str):
        raise KvsError(operation, _INVALID_RESPONSE)
    return arn


async def get_signaling_channel_endpoints(
    http_session: ClientSession,
    session: LiveViewSession,
    channel_arn: str,
    *,
    control_plane: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """Return the viewer endpoints of the channel by protocol (WSS and HTTPS)."""
    operation = "getSignalingChannelEndpoint"
    response = await _post(
        http_session,
        _signing_context(session, now),
        f"{control_plane or control_plane_url(session.region)}/{operation}",
        {
            "ChannelARN": channel_arn,
            "SingleMasterChannelEndpointConfiguration": {
                "Protocols": ["WSS", "HTTPS"],
                "Role": "VIEWER",
            },
        },
    )
    endpoints = response.get("ResourceEndpointList")
    if not isinstance(endpoints, list) or not all(
        isinstance(entry, dict) for entry in endpoints
    ):
        raise KvsError(operation, _INVALID_RESPONSE)
    result = {
        str(entry.get("Protocol")): str(entry.get("ResourceEndpoint"))
        for entry in endpoints
    }
    if not {"WSS", "HTTPS"} <= result.keys():
        raise KvsError(operation, _INVALID_RESPONSE)
    return result


async def get_ice_server_config(
    http_session: ClientSession,
    session: LiveViewSession,
    https_endpoint: str,
    channel_arn: str,
    *,
    now: dt.datetime | None = None,
) -> tuple[IceServer, ...]:
    """Return the ICE servers: the KVS STUN server, then one per TURN uri.

    Each uri gets its own entry, as some viewers (go2rtc) only accept a single
    uri per server.
    """
    operation = "get-ice-server-config"
    response = await _post(
        http_session,
        _signing_context(session, now),
        f"{https_endpoint.rstrip('/')}/v1/{operation}",
        {"ChannelARN": channel_arn, "ClientId": session.client_id, "Service": "TURN"},
    )
    entries = response.get("IceServerList", [])
    if not isinstance(entries, list):
        raise KvsError(operation, _INVALID_RESPONSE)
    stun = IceServer(urls=f"stun:stun.kinesisvideo.{session.region}.amazonaws.com:443")
    return (stun, *(s for e in entries for s in _turn_servers(e, operation)))


def _turn_servers(entry: Any, operation: str) -> list[IceServer]:
    if not isinstance(entry, dict):
        raise KvsError(operation, _INVALID_RESPONSE)
    uris = entry.get("Uris")
    username = entry.get("Username")
    password = entry.get("Password")
    if (
        not isinstance(uris, list)
        or not all(isinstance(uri, str) for uri in uris)
        or not isinstance(username, str)
        or not isinstance(password, str)
    ):
        raise KvsError(operation, _INVALID_RESPONSE)
    return [IceServer(urls=uri, username=username, credential=password) for uri in uris]


async def create_viewer_connection(
    session: LiveViewSession,
    http_session: ClientSession,
    *,
    control_plane: str | None = None,
    now: dt.datetime | None = None,
) -> KvsViewerConnection:
    """Prepare a viewer connection: presigned signaling url and ICE servers.

    Each call makes a fresh presigned url (valid PRESIGN_EXPIRES seconds) and
    fresh TURN credentials from the same live view session.
    """
    context = _signing_context(session, now)
    channel_arn = await describe_signaling_channel(
        http_session, session, control_plane=control_plane, now=context.now
    )
    endpoints = await get_signaling_channel_endpoints(
        http_session,
        session,
        channel_arn,
        control_plane=control_plane,
        now=context.now,
    )
    ice_servers = await get_ice_server_config(
        http_session, session, endpoints["HTTPS"], channel_arn, now=context.now
    )
    wss_url = presign_url(
        context,
        endpoint=endpoints["WSS"],
        query={"X-Amz-ChannelARN": channel_arn, "X-Amz-ClientId": session.client_id},
        expires=PRESIGN_EXPIRES,
    )
    return KvsViewerConnection(
        wss_url=wss_url,
        client_id=session.client_id,
        channel_arn=channel_arn,
        ice_servers=ice_servers,
        expires_at=context.now + dt.timedelta(seconds=PRESIGN_EXPIRES),
    )
