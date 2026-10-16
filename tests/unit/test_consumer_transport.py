import hashlib
import io
import json
import sys
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "consumer"))

from ffmpeg_agent_client.artifacts import save_artifact  # noqa: E402
from ffmpeg_agent_client.config import Connection, load_connection  # noqa: E402
from ffmpeg_agent_client.errors import ConsumerError  # noqa: E402
from ffmpeg_agent_client.protocols import A2A, MCP, MCP_VERSION  # noqa: E402
from ffmpeg_agent_client.transport import JSON_LIMIT, Client  # noqa: E402

TOKEN = "isolated-consumer-test-token-12345"
FILE_ID = "file_" + "a" * 32
ARTIFACT_ID = "art_" + "b" * 32
TASK_ID = "agent_" + "c" * 32


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        raw = json.dumps(data).encode() if isinstance(data, dict) else data
        super().__init__(raw)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        if "Content-Type" not in self.headers:
            self.headers["Content-Type"] = ("application/json" if isinstance(data, dict)
                                            else "application/octet-stream")
        if "Content-Length" not in self.headers:
            self.headers["Content-Length"] = str(len(raw))


@pytest.fixture
def wire(monkeypatch):
    calls, connections = [], []
    handlers = []

    class HTTP:
        def __init__(self, host, port, timeout):
            self.address, self.timeout, self.closed = (host, port), timeout, False
            connections.append(self)

        def request(self, method, path, body, headers):
            call = {"method": method, "path": path, "body": body, "headers": headers}
            calls.append(call)
            self.response = handlers.pop(0)(call)

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr("ffmpeg_agent_client.transport.http.client.HTTPConnection", HTTP)
    monkeypatch.setattr("ffmpeg_agent_client.transport.http.client.HTTPSConnection", HTTP)
    return handlers, calls, connections


@pytest.fixture
def client():
    return Client(Connection("https://media.example.invalid/prefix", TOKEN))


def _artifact(data=b"media", kind="file", **extra):
    return {"artifact_id": ARTIFACT_ID, "kind": kind, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), **extra}


def test_configuration_no_interpolation_and_process_precedence(tmp_path):
    env = tmp_path / "consumer.env"
    env.write_text("MEDIA_PUBLIC_BASE_URL='https://example.invalid/media'\n"
                   f"MEDIA_SERVICE_TOKEN='{TOKEN}-$SECRET'\n")
    connection = load_connection(env, {})
    assert connection.base == "https://example.invalid/media"
    assert connection.token == TOKEN + "-$SECRET"
    assert TOKEN not in repr(connection)
    assert load_connection(env, {"MEDIA_SERVICE_TOKEN": "null"}).token is None
    with pytest.raises(ConsumerError, match="INVALID_SERVICE_TOKEN"):
        load_connection(env, {"MEDIA_SERVICE_TOKEN": ""})
    with pytest.raises(ConsumerError, match="MISSING_SERVICE_TOKEN"):
        load_connection(tmp_path / "missing", {"MEDIA_PUBLIC_BASE_URL": "https://example.invalid"})


@pytest.mark.parametrize("url", [
    "https://secret@example.invalid", "https://example.invalid?a=secret",
    "https://example.invalid#secret", "https://example.invalid?", "https://example.invalid:99999",
    "https://example.invalid\n", "file:///tmp/media", "https://example.invalid/a/../b",
    "https://example.invalid/a/%2e%2e/b", "https://example.invalid//prefix",
])
def test_configuration_rejects_unsafe_service_urls(url):
    with pytest.raises(ConsumerError, match="INVALID_SERVICE_URL") as captured:
        Connection(url, TOKEN)
    assert url not in str(captured.value)
    assert TOKEN not in repr(captured.value)


def test_redirect_refused_without_followup_or_secret_error(client, wire):
    handlers, calls, connections = wire
    handlers.append(lambda _: Response(b"secret response", 302, {
        "Location": "https://third-party.invalid/?token=secret"}))
    with pytest.raises(ConsumerError, match="REDIRECT_REFUSED") as captured:
        client.api("GET", "/v1/capabilities")
    assert len(calls) == len(connections) == 1
    assert connections[0].address == ("media.example.invalid", None)
    assert calls[0]["path"] == "/prefix/v1/capabilities"
    assert captured.value.status == 302
    assert connections[0].closed
    assert "secret" not in str(captured.value)


def test_remote_error_codes_are_allowlisted(client, wire):
    handlers, _, _ = wire
    for code, expected in [("TOKEN_IS_SECRET", "HTTP_ERROR"),
                           ("IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_CONFLICT")]:
        handlers.append(lambda _, code=code: Response({"error": {
            "code": code, "message": TOKEN, "details": {"token": TOKEN}}}, 409))
        with pytest.raises(ConsumerError) as captured:
            client.api("GET", "/v1/capabilities")
        assert captured.value.code == expected
        assert TOKEN not in repr(captured.value)
    handlers.append(lambda _: (_ for _ in ()).throw(OSError(TOKEN)))
    with pytest.raises(ConsumerError, match="CONNECTION_FAILED") as captured:
        client.api("GET", "/v1/capabilities")
    assert captured.value.__suppress_context__


def test_planning_message_redacts_token_and_control_characters(client):
    message = client.safe_message("请补充尺寸\n" + TOKEN + "\x1b\r\t\u202e结束")
    assert message == "请补充尺寸\n[redacted]结束"
    assert TOKEN not in message
    assert client.safe_message(TOKEN * 100) is None
    assert client.safe_message({"token": TOKEN}) is None
    assert client.safe_message("\r\n\t") is None


def test_response_size_bounded_and_paths_cannot_escape(client, wire):
    handlers, calls, _ = wire
    handlers.append(lambda _: Response(b" " * (JSON_LIMIT + 2)))
    with pytest.raises(ConsumerError, match="RESPONSE_TOO_LARGE"):
        client.api("GET", "/v1/capabilities")
    for path in ["https://evil.invalid", "//evil.invalid", "/../health", "/%2e%2e/health"]:
        with pytest.raises(ConsumerError, match="INVALID_SERVICE_PATH"):
            client.api("GET", path)
    with pytest.raises(ConsumerError, match="INVALID_HTTP_HEADER"):
        client.api("GET", "/health/live", headers={"Authorization": "Bearer other"})
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b'{"key": 1, "key": 2}', b'{"value": NaN}'])
def test_ambiguous_json_response_rejected(client, wire, body):
    wire[0].append(lambda _: Response(body, headers={"Content-Type": "application/json"}))
    with pytest.raises(ConsumerError, match="INVALID_JSON_RESPONSE"):
        client.api("GET", "/v1/capabilities")


def test_upload_streams_checks_hash_and_server_identity(client, wire, tmp_path):
    handlers, _, _ = wire
    data = b"fixture\0" * 400000
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    def upload(call):
        assert not isinstance(call["body"], bytes)
        chunks = list(call["body"])
        assert len(chunks) > 1 and max(map(len, chunks)) <= 1024 * 1024
        assert b"".join(chunks) == data
        assert call["headers"]["X-File-Sha256"] == digest
        assert call["headers"]["Idempotency-Key"] == "consumer-upload"
        return Response({"file_id": FILE_ID, "state": "ready",
                         "size_bytes": len(data), "sha256": digest}, 201)

    handlers.append(upload)
    assert client.upload(source, "consumer-upload", digest)["file_id"] == FILE_ID
    handlers.append(lambda call: Response({"file_id": "wrong", "state": "ready",
        "size_bytes": len(b"".join(call["body"])), "sha256": digest}, 201))
    with pytest.raises(ConsumerError, match="INVALID_UPLOAD_RESPONSE"):
        client.upload(source, "consumer-upload")


def test_upload_rejects_local_mutation_before_send(client, wire, tmp_path):
    handlers, _, _ = wire
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")

    def mutate(call):
        source.write_bytes(b"changed!")
        list(call["body"])
        pytest.fail("the changed source must not be accepted")

    handlers.append(mutate)
    with pytest.raises(ConsumerError, match="INPUT_CHANGED"):
        client.upload(source, "consumer-upload")


def test_file_download_verified_reused_never_overwritten(client, wire, tmp_path):
    handlers, calls, _ = wire
    artifact = _artifact(access={"http": {"content": "https://evil.invalid/"}})
    destination = tmp_path / "result.bin"
    handlers.append(lambda _: Response(b"media"))
    lease_id = "lease_" + "d" * 32
    result = save_artifact(client, artifact, destination, lease_id)
    assert destination.read_bytes() == b"media"
    assert result["files"][0]["reused"] is False
    assert calls[0]["path"] == f"/prefix/v1/artifacts/{ARTIFACT_ID}/content"
    assert calls[0]["headers"]["X-Artifact-Lease-ID"] == lease_id
    assert save_artifact(client, artifact, destination)["files"][0]["reused"] is True
    assert len(calls) == 1
    destination.write_bytes(b"user content")
    with pytest.raises(ConsumerError, match="OUTPUT_EXISTS"):
        save_artifact(client, artifact, destination)
    assert destination.read_bytes() == b"user content"
    assert len(calls) == 1


@pytest.mark.parametrize("data,headers,code", [
    (b"wrong", {}, "DOWNLOAD_HASH_MISMATCH"),
    (b"media", {"Content-Length": "4"}, "DOWNLOAD_SIZE_MISMATCH"),
])
def test_bad_download_never_published(client, wire, tmp_path, data, headers, code):
    handlers, _, _ = wire
    handlers.append(lambda _: Response(data, headers=headers))
    destination = tmp_path / "result.bin"
    with pytest.raises(ConsumerError, match=code):
        save_artifact(client, _artifact(), destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_symlink_file_and_directory(client, wire, tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"media")
    linked = tmp_path / "linked"
    linked.symlink_to(original)
    with pytest.raises(ConsumerError, match="UNSAFE_OUTPUT_PATH"):
        save_artifact(client, _artifact(), linked)
    folder = tmp_path / "folder"
    folder.mkdir()
    directory_link = tmp_path / "folder-link"
    directory_link.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ConsumerError, match="UNSAFE_OUTPUT_PATH"):
        save_artifact(client, _artifact(), directory_link / "result.bin")
    assert original.read_bytes() == b"media" and not list(folder.iterdir())
    assert wire[1] == []


def test_collection_paginates_preserves_partial_failures_and_resumes(client, wire, tmp_path):
    handlers, calls, _ = wire
    first = {"index": 0, "member_key": "frame_000.png", "size_bytes": 5,
             "sha256": hashlib.sha256(b"media").hexdigest()}
    failed = {"index": 1, "error": {"code": "TIMESTAMP_OUT_OF_RANGE", "message": TOKEN}}
    artifact = _artifact(kind="collection", item_count=2, member_count=1)
    handlers.extend([
        lambda _: Response({"items": [first], "next_cursor": 1}),
        lambda _: Response({"items": [failed], "next_cursor": None}),
        lambda _: Response(b"media"),
    ])
    destination = tmp_path / "frames"
    result = save_artifact(client, artifact, destination)
    assert result["partial"] is True
    assert result["failures"] == [{"index": 1, "code": "TIMESTAMP_OUT_OF_RANGE"}]
    assert "cursor=1" in calls[1]["path"]
    assert TOKEN not in (destination / "manifest.json").read_text()
    handlers.append(lambda _: Response({"items": [first, failed], "next_cursor": None}))
    resumed = save_artifact(client, artifact, destination)
    assert resumed["files"][0]["reused"] and len(calls) == 4


@pytest.mark.parametrize("name", ["../escape", "/absolute", "..", "a\\b", "manifest.json"])
def test_collection_member_traversal_rejected_before_writes(client, wire, tmp_path, name):
    handlers, _, _ = wire
    handlers.append(lambda _: Response({"items": [{"index": 0, "member_key": name,
        "size_bytes": 5, "sha256": hashlib.sha256(b"media").hexdigest()}], "next_cursor": None}))
    with pytest.raises(ConsumerError, match="UNSAFE_COLLECTION_MEMBER"):
        save_artifact(client, _artifact(kind="collection", item_count=1, member_count=1),
                      tmp_path / "frames")
    assert not list(tmp_path.iterdir())


def _rpc(result):
    def handler(call):
        request = json.loads(call["body"])
        return Response({"jsonrpc": "2.0", "id": request["id"], "result": result})
    return handler


def _mcp_initialize(handlers, version=MCP_VERSION):
    handlers.append(_rpc({"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "ffmpeg-agent", "version": "1"}}))
    if version == MCP_VERSION:
        handlers.append(lambda _: Response(b"", 202))


def test_mcp_initialization_and_structured_calls(client, wire):
    handlers, calls, _ = wire
    _mcp_initialize(handlers)
    handlers.extend([_rpc({"tools": [{"name": "get_capabilities", "inputSchema": {}}]}),
                     _rpc({"structuredContent": {"operations": {}}, "content": []})])
    with MCP(client) as mcp:
        assert mcp.list_tools()[0]["name"] == "get_capabilities"
        assert mcp.call("get_capabilities", {}) == {"operations": {}}
    assert [json.loads(call["body"])["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert all(call["headers"]["MCP-Protocol-Version"] == MCP_VERSION for call in calls)
    with pytest.raises(ConsumerError, match="MCP_NOT_INITIALIZED"):
        mcp.list_tools()


def test_mcp_wrong_version_id_and_secret_error_rejected(client, wire):
    handlers, _, _ = wire
    _mcp_initialize(handlers, "unknown-version")
    with pytest.raises(ConsumerError, match="MCP_VERSION_MISMATCH"), MCP(client):
        pass
    handlers.append(lambda _: Response({"jsonrpc": "2.0", "id": "wrong-id", "result": {}}))
    with pytest.raises(ConsumerError, match="MCP_INVALID_RESPONSE"), MCP(client):
        pass
    _mcp_initialize(handlers)
    handlers.append(_rpc({"isError": True, "content": [{"type": "text", "text": TOKEN}],
                          "structuredContent": {"error": {"code": "TOKEN_IS_SECRET"}}}))
    with MCP(client) as mcp, pytest.raises(ConsumerError, match="REMOTE_ERROR") as captured:
        mcp.call("get_capabilities", {})
    assert TOKEN not in str(captured.value)


@pytest.mark.parametrize("domain_error", [None, {"code": "INVALID_MEDIA"}])
def test_mcp_domain_error_field_is_not_tool_protocol_failure(client, wire, domain_error):
    handlers, _, _ = wire
    _mcp_initialize(handlers)
    payload = {"job_id": "job_" + "d" * 32,
               "status": "succeeded" if domain_error is None else "failed", "error": domain_error}
    handlers.append(_rpc({"isError": False, "content": [], "structuredContent": payload}))
    with MCP(client) as mcp:
        assert mcp.call("get_job", {"job_id": payload["job_id"]}) == payload


def _card(base):
    return {"supportedInterfaces": [{"url": base + "/a2a", "protocolBinding": "JSONRPC",
                                      "protocolVersion": "1.0"}],
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
            "securityRequirements": [{"schemes": {"bearer": {}}}]}


def _task():
    return {"id": TASK_ID, "status": {"state": "TASK_STATE_WORKING"},
            "metadata": {"agent_task_id": TASK_ID, "status": "running"}}


def test_a2a_card_send_get_cancel_use_v1_jsonrpc(client, wire):
    handlers, calls, _ = wire
    handlers.extend([lambda _: Response(_card(client.base)), _rpc({"task": _task()}),
                     _rpc(_task()), _rpc(_task())])
    a2a = A2A(client)
    assert a2a.send({"intent": "检查媒体"}, "request-key")["id"] == TASK_ID
    assert a2a.get(TASK_ID)["id"] == TASK_ID
    assert a2a.cancel(TASK_ID)["status"]["state"] == "TASK_STATE_WORKING"
    send = json.loads(calls[1]["body"])
    assert send["method"] == "SendMessage"
    assert send["params"]["message"]["messageId"] == "request-key"
    assert send["params"]["message"]["role"] == "ROLE_USER"
    assert send["params"]["configuration"]["returnImmediately"] is True
    assert all(call["headers"]["A2A-Version"] == "1.0" for call in calls)


def test_a2a_refuses_third_party_card_and_rpc_errors(client, wire):
    handlers, calls, _ = wire
    handlers.append(lambda _: Response(_card("https://evil.invalid")))
    with pytest.raises(ConsumerError, match="A2A_UNSUPPORTED_INTERFACE"):
        A2A(client).send({}, "request-key")
    assert len(calls) == 1
    handlers.extend([lambda _: Response(_card(client.base)), lambda call: Response({
        "jsonrpc": "2.0", "id": json.loads(call["body"])["id"],
        "error": {"code": -32603, "message": TOKEN}})])
    with pytest.raises(ConsumerError, match="A2A_RPC_ERROR") as captured:
        A2A(client).get(TASK_ID)
    assert TOKEN not in str(captured.value)


def test_a2a_malformed_nested_values_become_safe_errors(client, wire):
    handlers, _, _ = wire
    card = _card(client.base)
    card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] = {"secret": TOKEN}
    handlers.append(lambda _: Response(card))
    with pytest.raises(ConsumerError, match="A2A_AUTH_MISMATCH"):
        A2A(client).card()
    task = _task()
    task["status"]["state"] = {"secret": TOKEN}
    handlers.extend([lambda _: Response(_card(client.base)), _rpc(task)])
    with pytest.raises(ConsumerError, match="A2A_INVALID_TASK"):
        A2A(client).get(TASK_ID)
