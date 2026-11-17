"""SigV4 checks with fake keys only.

The expected values come from the AWS SigV4 test suite (``get-vanilla``,
``get-vanilla-query-order-key-case``, ``post-vanilla``) and from botocore
(``SigV4Auth`` / ``SigV4QueryAuth``) run once on the same fake inputs.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qsl, urlsplit

import pytest

from deebot_client.live_view import AwsCredentials
from deebot_client.live_view.sigv4 import SigningContext, presign_url, sign_headers

# Example credentials of the AWS documentation
_AWS_EXAMPLE = AwsCredentials(
    access_key_id="AKIDEXAMPLE",
    secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",  # noqa: S106
)
_FAKE_WITH_TOKEN = AwsCredentials(
    access_key_id="ASIAFAKEFAKEFAKE",
    secret_access_key="fakeSecret/with+chars=",  # noqa: S106
    session_token="IQoJb3JpZ2luX2Vj//////////fake+token/with=chars",  # noqa: S106
)
_NOW = dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC)
_WSS_ENDPOINT = "wss://v-1a2b3c4d.kinesisvideo.eu-central-1.amazonaws.com"
_CHANNEL_ARN = (
    "arn:aws:kinesisvideo:eu-central-1:123456789012:channel/fake-channel/1234567890123"
)
_CET = dt.timezone(dt.timedelta(hours=1))


def _context(
    credentials: AwsCredentials, region: str, service: str, tz: dt.tzinfo = dt.UTC
) -> SigningContext:
    return SigningContext(
        credentials=credentials, region=region, service=service, now=_NOW.astimezone(tz)
    )


@pytest.mark.parametrize(
    ("method", "url", "signature"),
    [
        (
            "GET",
            "https://example.amazonaws.com/",
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        ),
        (
            "GET",
            "https://example.amazonaws.com/?Param2=value2&Param1=value1",
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
        ),
        (
            "POST",
            "https://example.amazonaws.com/",
            "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b",
        ),
    ],
)
def test_aws_test_suite_vectors(method: str, url: str, signature: str) -> None:
    headers = sign_headers(
        _context(_AWS_EXAMPLE, "us-east-1", "service"),
        method=method,
        url=url,
        headers={},
        body=b"",
    )

    assert headers == {
        "X-Amz-Date": "20150830T123600Z",
        "Authorization": (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
            f"aws4_request, SignedHeaders=host;x-amz-date, Signature={signature}"
        ),
    }


def test_header_signing_matches_botocore() -> None:
    given = {"Content-Type": "application/json"}

    headers = sign_headers(
        SigningContext(
            credentials=_FAKE_WITH_TOKEN,
            region="eu-central-1",
            service="kinesisvideo",
            # a naive time is taken as UTC
            now=_NOW.replace(tzinfo=None),
        ),
        method="POST",
        url="https://kinesisvideo.eu-central-1.amazonaws.com/describeSignalingChannel",
        headers=given,
        body=b'{"ChannelName":"fake-channel"}',
    )

    assert headers == {
        "Content-Type": "application/json",
        "X-Amz-Date": "20150830T123600Z",
        "X-Amz-Security-Token": "IQoJb3JpZ2luX2Vj//////////fake+token/with=chars",
        "Authorization": (
            "AWS4-HMAC-SHA256 Credential=ASIAFAKEFAKEFAKE/20150830/eu-central-1/"
            "kinesisvideo/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token, "
            "Signature=408ede9fa9a40427533b5a609e84ddbc65fb861617ca825c25fb849d4de24964"
        ),
    }
    # the given headers are not modified
    assert given == {"Content-Type": "application/json"}


@pytest.mark.parametrize(
    ("credentials", "signature"),
    [
        (
            _FAKE_WITH_TOKEN,
            "8121864b829c97b43a84a743776db525955c53c27c14f9cb2ccdb0641d1c5eb5",
        ),
        (
            _AWS_EXAMPLE,
            "dd80c216824c4f297f6dafdc4612502348eb7eb58eb50f01902a0a164b6de753",
        ),
    ],
)
def test_presign_matches_botocore(credentials: AwsCredentials, signature: str) -> None:
    url = presign_url(
        # the time zone does not change the signature
        _context(credentials, "eu-central-1", "kinesisvideo", tz=_CET),
        endpoint=_WSS_ENDPOINT,
        query={"X-Amz-ChannelARN": _CHANNEL_ARN, "X-Amz-ClientId": "fake-client-id"},
        expires=299,
    )

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == _WSS_ENDPOINT
    assert parts.path == "/"
    expected = {
        "X-Amz-ChannelARN": _CHANNEL_ARN,
        "X-Amz-ClientId": "fake-client-id",
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": (
            f"{credentials.access_key_id}/20150830/eu-central-1/kinesisvideo/aws4_request"
        ),
        "X-Amz-Date": "20150830T123600Z",
        "X-Amz-Expires": "299",
        "X-Amz-SignedHeaders": "host",
        "X-Amz-Signature": signature,
    }
    if credentials.session_token:
        expected["X-Amz-Security-Token"] = credentials.session_token
    assert dict(parse_qsl(parts.query)) == expected
    # The signature is last, as the KVS SDKs emit it
    assert parts.query.rsplit("&", 1)[1] == f"X-Amz-Signature={signature}"


def test_presign_keeps_the_endpoint_path() -> None:
    url = presign_url(
        _context(_AWS_EXAMPLE, "us-east-1", "service"),
        endpoint="wss://example.amazonaws.com/some path",
        query={},
    )

    parts = urlsplit(url)
    assert parts.path == "/some%20path"
    assert dict(parse_qsl(parts.query))["X-Amz-Expires"] == "299"
