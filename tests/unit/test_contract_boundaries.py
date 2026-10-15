import copy

import pytest
from pydantic import ValidationError

from media_service.config import Settings
from media_service.models import JobCreate


def probe_request():
    return {
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": "video.mp4"}},
        "steps": [{"id": "probe", "operation": "media.probe.v1",
                   "inputs": {"media": {"source": "main"}}}],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    }


def http_request():
    value = probe_request()
    value["inputs"]["main"] = {
        "kind": "http_file",
        "url": "https://source.example.invalid/video.mp4?signature=test-only",
        "headers": {"Authorization": "Bearer test-only-value"},
        "expires_at": "2026-10-14T12:00:00Z",
        "expected_size_bytes": 100,
        "expected_sha256": "a" * 64,
    }
    return value


def test_cycle_is_rejected_after_all_sources_are_used():
    value = probe_request()
    value["steps"].extend([
        {"id": "a", "operation": "video.transcode.v1",
         "inputs": {"video": {"step": "b", "output": "video"}}},
        {"id": "b", "operation": "video.transcode.v1",
         "inputs": {"video": {"step": "a", "output": "video"}}},
    ])
    value["outputs"]["cycle"] = {"step": "a", "output": "video"}
    with pytest.raises(ValidationError, match="dependency cycle"):
        JobCreate.model_validate(value)


def test_self_dependency_is_rejected_after_all_sources_are_used():
    value = probe_request()
    value["steps"].append({
        "id": "self", "operation": "video.transcode.v1",
        "inputs": {"video": {"step": "self", "output": "video"}},
    })
    value["outputs"]["cycle"] = {"step": "self", "output": "video"}
    with pytest.raises(ValidationError, match="dependency cycle"):
        JobCreate.model_validate(value)


@pytest.mark.parametrize(("operation", "input_port", "output", "params"), [
    ("video.extract_frames.v1", "video", "frames", {"sampling": {"mode": "timestamps", "timestamps_ms": [0]}}),
    ("media.probe.v1", "media", "report", {}),
    ("audio.transcode.v1", "audio", "audio", {}),
])
def test_incompatible_step_output_cannot_be_used_as_video(operation, input_port, output, params):
    value = probe_request()
    value["steps"] = [
        {"id": "first", "operation": operation,
         "inputs": {input_port: {"source": "main"}}, "params": params},
        {"id": "second", "operation": "video.transcode.v1",
         "inputs": {"video": {"step": "first", "output": output}}},
    ]
    value["outputs"] = {"result": {"step": "second", "output": "video"}}
    with pytest.raises(ValidationError, match="incompatible step input type"):
        JobCreate.model_validate(value)


def test_unordered_valid_dag_preserves_dependencies():
    value = probe_request()
    value["steps"] = [
        {"id": "probe", "operation": "media.probe.v1",
         "inputs": {"media": {"step": "audio", "output": "audio"}}},
        {"id": "audio", "operation": "video.extract_audio.v1",
         "inputs": {"video": {"step": "video", "output": "video"}}},
        {"id": "video", "operation": "video.transcode.v1",
         "inputs": {"video": {"source": "main"}}},
    ]
    job = JobCreate.model_validate(value)
    assert [step.id for step in job.ordered_steps()] == ["video", "audio", "probe"]


@pytest.mark.parametrize("path", [
    ("inputs", "main"),
    ("steps", 0),
    ("steps", 0, "inputs", "media"),
    ("outputs", "report"),
    ("output_policy",),
])
def test_unknown_nested_fields_are_rejected(path):
    value = probe_request()
    value["output_policy"] = {}
    target = value
    for key in path:
        target = target[key]
    target["unexpected"] = "not-supported"
    with pytest.raises(ValidationError) as error:
        JobCreate.model_validate(value)
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


@pytest.mark.parametrize("key", ["/tmp/source.mp4", "../source.mp4", "a/../b", "a/./b",
                                "a//b", "a/", "a\\b", "a\x00b"])
def test_unsafe_shared_keys_are_rejected(key):
    value = probe_request()
    value["inputs"]["main"]["key"] = key
    with pytest.raises(ValidationError, match="unsafe key"):
        JobCreate.model_validate(value)


@pytest.mark.parametrize("invalid", [True, "100", 100.1, -1])
def test_input_size_requires_nonnegative_integer(invalid):
    value = probe_request()
    value["inputs"]["main"]["expected_size_bytes"] = invalid
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)


@pytest.mark.parametrize("invalid", [True, "100", 100.1, -1])
def test_frame_timestamps_require_nonnegative_integer_milliseconds(invalid):
    value = probe_request()
    value["steps"] = [{
        "id": "frames", "operation": "video.extract_frames.v1",
        "inputs": {"video": {"source": "main"}}, "params": {"sampling": {"mode": "timestamps", "timestamps_ms": [invalid]}},
    }]
    value["outputs"] = {"frames": {"step": "frames", "output": "frames"}}
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)


@pytest.mark.parametrize("changes", [
    {"url": "https://source.example.invalid/video.mp4?signature=changed-test-only"},
    {"headers": {"authorization": "Bearer different-test-only-value"}},
    {"expires_at": "2026-10-14T13:00:00Z"},
    {"expected_size_bytes": 101},
    {"expected_sha256": "b" * 64},
])
def test_every_source_identity_field_affects_idempotency(changes):
    original = http_request()
    changed = copy.deepcopy(original)
    changed["inputs"]["main"].update(changes)
    assert JobCreate.model_validate(original).digest() != JobCreate.model_validate(changed).digest()


def test_source_header_case_normalizes_before_idempotency_hash():
    original = http_request()
    changed = copy.deepcopy(original)
    changed["inputs"]["main"]["headers"] = {"authorization": "Bearer test-only-value"}
    assert JobCreate.model_validate(original).digest() == JobCreate.model_validate(changed).digest()


@pytest.mark.parametrize("headers", [
    {"Host": "different.example.invalid"},
    {"Authorization": "test\r\nInjected: value"},
    {"Authorization": "first-test-value", "authorization": "second-test-value"},
])
def test_source_headers_reject_routing_override_injection_and_case_duplicates(headers):
    value = http_request()
    value["inputs"]["main"]["headers"] = headers
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)


@pytest.mark.parametrize("expires_at", ["tomorrow", "2026-10-14T12:00:00", "2026-02-30T12:00:00Z"])
def test_source_expiry_requires_valid_rfc3339_with_timezone(expires_at):
    value = http_request()
    value["inputs"]["main"]["expires_at"] = expires_at
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)


def settings_data(tmp_path):
    return {
        "state_dir": tmp_path / "state",
        "work_dir": tmp_path / "work",
        "storages": {"output": {"root": tmp_path / "output", "role": "output",
                                  "callers": ["test"]}},
        "callers": {"test": {"tokens": ["test-only-credential-with-no-business-access"],
                               "default_output_storage": "output"}},
    }


@pytest.mark.parametrize("workers", [0, 5])
def test_out_of_range_worker_configuration_is_rejected(tmp_path, workers):
    value = settings_data(tmp_path)
    value["worker_concurrency"] = workers
    with pytest.raises(ValidationError):
        Settings.model_validate(value)


def test_nested_or_shared_storage_roots_are_rejected(tmp_path):
    value = settings_data(tmp_path)
    value["storages"]["output"]["root"] = tmp_path / "work" / "output"
    with pytest.raises(ValidationError, match="storage roots must not overlap"):
        Settings.model_validate(value)


def test_cross_caller_token_reuse_is_rejected(tmp_path):
    value = settings_data(tmp_path)
    value["callers"]["other"] = copy.deepcopy(value["callers"]["test"])
    value["storages"]["output"]["callers"].append("other")
    with pytest.raises(ValidationError, match="credentials must be unique"):
        Settings.model_validate(value)
