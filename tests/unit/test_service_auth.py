from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from starlette.datastructures import Headers

from media_service.api import create_app
from media_service.auth import authorize
from media_service.errors import ServiceError


def test_single_token_uses_service_context_and_null_opens(settings):
    token = "isolated-single-service-token-for-tests"
    config = settings.model_copy(update={"service_token": SecretStr(token)})
    assert authorize(config, Headers({"authorization": f"Bearer {token}"})) == "test"
    for headers in (Headers(), Headers({"authorization": "Bearer wrong"}),
                    Headers(raw=[(b"authorization", f"Bearer {token}".encode())] * 2)):
        with pytest.raises(ServiceError, match="UNAUTHORIZED"):
            authorize(config, headers)
    config = settings.model_copy(update={"service_token": None})
    assert authorize(config, Headers()) == "test"
    assert authorize(config, Headers({"authorization": "unused"})) == "test"


async def test_upload_auth_rejects_before_reading_body(settings):
    app = create_app(service=SimpleNamespace(settings=settings))
    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"not-read", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": "POST", "scheme": "http", "path": "/v1/files",
               "raw_path": b"/v1/files", "query_string": b"", "headers": [],
               "server": ("test", 80), "client": ("test", 123)}, receive, send)
    assert reads == 0
    assert sent[0]["status"] == 401
    assert b"UNAUTHORIZED" in sent[1]["body"]
