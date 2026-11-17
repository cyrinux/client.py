"""Live view (camera) support.

The Ecovacs api starts a session on an AWS Kinesis Video Streams signaling
channel, where the robot is the master. The calls are kept small, so the caller
decides when to start, reuse and end sessions:

- start_watch: start a session (temporary AWS credentials, channel, client id)
- create_viewer_connection: presigned signaling url and ICE servers for a
  WebRTC viewer; call it again for a fresh presigned url
- end_watch: end the session
"""

from __future__ import annotations

from .api import end_watch, hash_pin, start_watch
from .kvs import PRESIGN_EXPIRES, create_viewer_connection
from .models import (
    AwsCredentials,
    IceServer,
    KvsError,
    KvsViewerConnection,
    LiveViewError,
    LiveViewErrorCode,
    LiveViewSession,
)

__all__ = [
    "PRESIGN_EXPIRES",
    "AwsCredentials",
    "IceServer",
    "KvsError",
    "KvsViewerConnection",
    "LiveViewError",
    "LiveViewErrorCode",
    "LiveViewSession",
    "create_viewer_connection",
    "end_watch",
    "hash_pin",
    "start_watch",
]
