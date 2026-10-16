import io
import json
import logging

import pytest

from media_service.telemetry import SafeExceptionFilter, event


def test_event_allowlist_and_unexpected_exception_do_not_expose_source(caplog):
    with caplog.at_level(logging.INFO, logger="media_service.events"):
        event("job_started", job_id="job_test", url="https://private.example/?signature=secret",
              authorization="secret", path="/private/source", request_body={"secret": "value"})
    payload = json.loads(caplog.records[-1].message)
    assert payload["job_id"] == "job_test"
    assert not {"url", "authorization", "path", "request_body"} & payload.keys()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SafeExceptionFilter())
    logger = logging.Logger("test-isolated-server")
    logger.addHandler(handler)
    try:
        raise RuntimeError("https://private.example/?signature=secret /private/source")
    except RuntimeError:
        logger.exception("unexpected %s", "secret")
    assert "secret" not in stream.getvalue() and "/private" not in stream.getvalue()
    assert "runtime_error" in stream.getvalue()


def test_event_levels_filter_polling_and_keep_failures(caplog):
    with caplog.at_level(logging.INFO, logger="media_service.events"):
        event("request_finished", status=200)
        event("job_accepted", job_id="job_test")
        event("request_finished", status=401)
        event("request_finished", status=503)
    rows = [json.loads(record.message) for record in caplog.records]
    assert [row["level"] for row in rows] == ["INFO", "WARNING", "ERROR"]
    assert rows[0]["event"] == "job_accepted"
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="media_service.events"):
        event("request_finished", status=200)
    assert json.loads(caplog.records[0].message)["level"] == "DEBUG"


def test_model_content_redacts_nested_credentials_and_limits_output(caplog):
    from media_service.model_log_content import MAX_CONTENT_BYTES, log_model_content

    content = json.dumps({
        "authorization": "unknown-private-auth",
        "messages": [{"content": json.dumps({"api_key": "unknown-private-key",
                                               "steps": [{"id": "convert"}]})}],
        "error": "Bearer private-auth sk-private-key https://private.example?signature=secret",
        "intent": "known-service-token /private/source.wav user@example.com",
        "other": "/Volumes/Private/input.mov /data/customer/input.wav",
        "metadata": "\ud800",
    }).encode()
    with caplog.at_level(logging.DEBUG, logger="media_service.events"):
        log_model_content("response", content, secrets=("known-service-token",),
                          agent_task_id="agent_test", model_request=2, http_status=200)
    text = caplog.text
    for value in ("private-auth", "private-key", "private.example", "known-service-token",
                  "/private/source", "user@example.com", "/Volumes/", "/data/"):
        assert value not in text
    assert "convert" in text
    row = json.loads(caplog.records[-1].message)
    assert row["content_format"] == "json" and not row["content_truncated"]
    caplog.clear()
    secret = "secret-across-boundary"
    body = b"x" * (MAX_CONTENT_BYTES - 6) + secret.encode()
    with caplog.at_level(logging.DEBUG, logger="media_service.events"):
        log_model_content("request", body, secrets=(secret,),
                          agent_task_id="agent_test", model_request=1)
    row = json.loads(caplog.records[-1].message)
    assert row["content_truncated"] and len(row["content"].encode()) <= MAX_CONTENT_BYTES
    assert "secret" not in row["content"]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="media_service.events"):
        log_model_content("request", b"never-log-at-info", secrets=(),
                          agent_task_id="agent_test", model_request=1)
    assert not caplog.records


@pytest.mark.parametrize("key", [
    "authorization", "cookie", "refresh_token", "service_token", "signature", "token",
])
@pytest.mark.parametrize("form", ["oversized", "fenced", "malformed"])
def test_model_content_redaction_fails_closed_for_unstructured_private_fields(caplog, key, form):
    from media_service.model_log_content import MAX_CONTENT_BYTES, log_model_content

    content = json.dumps({key: "opaque-private-value", "padding": "x"})
    if form == "oversized":
        content = content[:-2] + "x" * MAX_CONTENT_BYTES + '"}'
    elif form == "fenced":
        content = "```json\n" + content + "\n```"
    else:
        content = content[:-1]
    with caplog.at_level(logging.DEBUG, logger="media_service.events"):
        log_model_content("response", content.encode(), secrets=(),
                          agent_task_id="agent_test", model_request=1)
    assert "opaque-private-value" not in caplog.text
    assert "REDACTED" in caplog.text


def test_model_content_omits_oversized_escaped_secret(caplog):
    from media_service.model_log_content import MAX_CONTENT_BYTES, log_model_content

    secret = 'arbitrary-token-with-quote"and-backslash\\end'
    content = json.dumps({"intent": secret, "padding": "x" * MAX_CONTENT_BYTES}).encode()
    with caplog.at_level(logging.DEBUG, logger="media_service.events"):
        log_model_content("request", content, secrets=(secret,),
                          agent_task_id="agent_test", model_request=1)
    row = json.loads(caplog.records[-1].message)
    assert row["content"] == "[REDACTED_OVERSIZED_CONTENT]"
    assert "arbitrary-token" not in caplog.text
