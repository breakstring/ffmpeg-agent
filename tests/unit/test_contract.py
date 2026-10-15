import copy

import pytest
from pydantic import ValidationError

from media_service.models import JobCreate


def request():
    return {
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": "test.mp4"}},
        "steps": [{"id": "probe", "operation": "media.probe.v1",
                   "inputs": {"media": {"source": "main"}}}],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    }


def test_canonical_defaults_and_mapping_order():
    original = request()
    a = JobCreate.model_validate(original)
    b = JobCreate.model_validate(dict(reversed(list(original.items()))))
    assert a.digest() == b.digest()
    assert a.digest() == JobCreate.model_validate(a.model_dump()).digest()


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(extra=True),
    lambda r: r["inputs"]["main"].update(key="a/../b"),
    lambda r: r["steps"][0].update(operation="media.probe.v999"),
    lambda r: r["steps"][0].update(params={"shell": "true"}),
    lambda r: r["steps"][0].update(inputs={"wrong": {"source": "main"}}),
    lambda r: r["outputs"]["report"].update(output="wrong"),
    lambda r: r["steps"].append(copy.deepcopy(r["steps"][0])),
])
def test_invalid_contract(mutation):
    value = request()
    mutation(value)
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)


def test_dag_rejects_cycle_unused_and_partial_collection_consumption():
    value = request()
    value["steps"] = [
        {"id": "a", "operation": "video.transcode.v1", "inputs": {"video": {"step": "b", "output": "video"}}},
        {"id": "b", "operation": "video.transcode.v1", "inputs": {"video": {"step": "a", "output": "video"}}},
    ]
    value["outputs"] = {"video": {"step": "a", "output": "video"}}
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)
    value = request()
    value["steps"].append({"id": "unused", "operation": "media.probe.v1", "inputs": {"media": {"source": "main"}}})
    with pytest.raises(ValidationError):
        JobCreate.model_validate(value)


def test_sensitive_source_fields_affect_digest():
    value = request()
    value["inputs"]["main"] = {"kind": "http_file", "url": "https://example.invalid/a?sig=one"}
    first = JobCreate.model_validate(value).digest()
    value["inputs"]["main"]["url"] = "https://example.invalid/a?sig=two"
    assert first != JobCreate.model_validate(value).digest()


def test_frame_requests_keep_duplicates_and_order():
    value = request()
    value["steps"] = [{"id": "f", "operation": "video.extract_frames.v1",
                       "inputs": {"video": {"source": "main"}},
                       "params": {"sampling": {"mode": "timestamps", "timestamps_ms": [100, 0, 100]}}}]
    value["outputs"] = {"frames": {"step": "f", "output": "frames"}}
    assert JobCreate.model_validate(value).steps[0].params["sampling"]["timestamps_ms"] == [100, 0, 100]
