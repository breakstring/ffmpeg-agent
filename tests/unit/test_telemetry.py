import io
import json
import logging

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
