from __future__ import annotations

import copy
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "consumer"))

from ffmpeg_agent_client.errors import ConsumerError  # noqa: E402
from ffmpeg_agent_client.state import Record  # noqa: E402
from ffmpeg_agent_client.workflow import Workflow  # noqa: E402

FILE_ID = "file_" + "a" * 32
JOB_ID = "job_" + "b" * 32
AGENT_ID = "agent_" + "c" * 32
ARTIFACT_ID = "art_" + "d" * 32
LEASE_ID = "lease_" + "e" * 32
OPERATION = {"mode": "operation", "operation": "image.resize.v1", "params": {"width": 80}}
INTENT = {"mode": "intent", "intent": "将输入图片缩放到宽度 80 像素"}


def _job(status="running", released=False, outputs=None):
    return {"job_id": JOB_ID, "status": status, "inputs_released": released,
            "outputs": {} if outputs is None else outputs}


class FakeClient:
    base = "https://consumer-test.example.invalid"
    authenticated = True

    def __init__(self):
        self.calls = []
        self.uploads = []
        self.submit_error = None
        self.document = {
            "operations": {"image.resize.v1": {"input_ports": ["image"], "output_port": "image"}},
            "limits": {}, "agent": {"enabled": True, "a2a_enabled": True},
        }
        self.job = _job()
        self.agent = {"agent_task_id": AGENT_ID, "status": "planning", "job": None}
        self.next_jobs = []
        self.cancel_response = _job("canceling")
        self.artifact = {"artifact_id": ARTIFACT_ID, "kind": "file", "size_bytes": 5,
                         "sha256": hashlib.sha256(b"media").hexdigest()}

    def upload(self, path, key, expected_sha256):
        data = path.read_bytes()
        self.uploads.append((str(path), key, expected_sha256))
        return {"file_id": FILE_ID, "state": "ready", "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "retain_until": "2099-01-01T00:00:00Z"}

    @staticmethod
    def safe_message(value):
        return value if isinstance(value, str) else None

    def api(self, method, path, payload=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(payload), copy.deepcopy(headers)))
        if (method, path) == ("GET", "/v1/capabilities"):
            return copy.deepcopy(self.document)
        if method == "POST" and path in {"/v1/jobs", "/v1/agent/tasks"}:
            if self.submit_error is not None:
                error, self.submit_error = self.submit_error, None
                raise error
            return {"agent_task_id": AGENT_ID} if path.endswith("tasks") else {"job_id": JOB_ID}
        if (method, path) == ("GET", "/v1/jobs/" + JOB_ID):
            if self.next_jobs:
                self.job = self.next_jobs.pop(0)
            return copy.deepcopy(self.job)
        if (method, path) == ("GET", "/v1/agent/tasks/" + AGENT_ID):
            return copy.deepcopy(self.agent)
        if (method, path) == ("POST", "/v1/jobs/" + JOB_ID + "/cancel"):
            return copy.deepcopy(self.cancel_response)
        if (method, path) == ("POST", "/v1/files/" + FILE_ID + "/release"):
            return {"state": "retiring"}
        if (method, path) == ("POST", "/v1/artifacts/" + ARTIFACT_ID + "/leases"):
            return {"lease_id": LEASE_ID}
        if (method, path) == ("GET", "/v1/artifacts/" + ARTIFACT_ID):
            return copy.deepcopy(self.artifact)
        if (method, path) == ("DELETE", "/v1/leases/" + LEASE_ID):
            return {}
        raise AssertionError(f"unexpected fake client request: {method} {path}")


@pytest.fixture
def request_files(tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"isolated consumer fixture")
    return {"source": source}, tmp_path / "private" / "state.json", tmp_path / "outputs"


def _calls(client, method, path):
    return [call for call in client.calls if call[:2] == (method, path)]


def test_unknown_submission_resumes_same_key_and_frozen_request(request_files):
    paths, state, output = request_files
    client = FakeClient()
    client.submit_error = ConsumerError("CONNECTION_FAILED")
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, INTENT, output)
        with pytest.raises(ConsumerError, match="CONNECTION_FAILED"):
            workflow.submit()
        assert record.data["phase"] == "submitting"
        assert "agent_task_id" not in record.data
        first_request = copy.deepcopy(record.data["request"])
        key = record.data["request_id"]
        with pytest.raises(ConsumerError, match="SUBMISSION_UNCONFIRMED"):
            workflow.release()
    client.document["agent"]["enabled"] = False
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.submit()
        assert record.data["agent_task_id"] == AGENT_ID
        assert record.data["request"] == first_request
    submissions = _calls(client, "POST", "/v1/agent/tasks")
    assert len(submissions) == 2
    assert all(call[2] == first_request and call[3] == {"Idempotency-Key": key}
               for call in submissions)
    assert len(client.uploads) == 1
    assert len(_calls(client, "GET", "/v1/capabilities")) == 1
    assert not _calls(client, "POST", "/v1/files/" + FILE_ID + "/release")


def test_known_task_resume_does_not_upload_replan_or_require_original_file(request_files):
    paths, state, output = request_files
    client = FakeClient()
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, INTENT, output)
        workflow.submit()
    paths["source"].unlink()
    client.document["agent"]["enabled"] = False
    before = copy.deepcopy(client.calls)
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.submit()
        assert record.data["agent_task_id"] == AGENT_ID
    assert client.calls == before
    assert len(client.uploads) == 1


@pytest.mark.parametrize("retry_prepare", [False, True])
def test_input_changes_are_rejected_before_upload(request_files, retry_prepare):
    paths, state, output = request_files
    client = FakeClient()
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
    paths["source"].write_bytes(b"modified consumer fixture")
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        with pytest.raises(ConsumerError, match="INPUT_CHANGED"):
            if retry_prepare:
                workflow.prepare(paths, OPERATION, output)
            else:
                workflow.submit()
    assert client.calls == client.uploads == []


def test_same_record_rejects_changed_request(request_files):
    paths, state, output = request_files
    client = FakeClient()
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        with pytest.raises(ConsumerError, match="STATE_REQUEST_MISMATCH"):
            workflow.prepare(paths, {**OPERATION, "params": {"width": 160}}, output)
    assert client.calls == client.uploads == []


@pytest.mark.parametrize("status", ["succeeded", "canceled", "failed"])
def test_terminal_status_does_not_finish_or_release_before_inputs_stop(request_files, status):
    paths, state, output = request_files
    client = FakeClient()
    client.job = _job(status, released=False)
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        workflow.submit()
        result, code = workflow.wait(0)
        assert code == 3 and result["resumable"] and not result["terminal"]
        assert result["code"] == "WAIT_TIMEOUT" and not result["ok"]
        with pytest.raises(ConsumerError, match="TASK_STILL_ACTIVE"):
            workflow.release()
    assert not output.exists()
    assert not _calls(client, "POST", "/v1/files/" + FILE_ID + "/release")


def test_cancel_waits_past_canceling_response_until_actual_stop(request_files, monkeypatch):
    paths, state, output = request_files
    client = FakeClient()
    client.next_jobs = [_job("canceling"), _job("canceled", released=True)]
    monkeypatch.setattr("ffmpeg_agent_client.workflow.time.sleep", lambda _: None)
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        workflow.submit()
        result, code = workflow.wait(10, cancel=True)
        assert code == 0 and result["ok"] and result["status"] == "canceled"
        assert result["terminal"] and result["inputs_released"]
    assert len(_calls(client, "POST", "/v1/jobs/" + JOB_ID + "/cancel")) == 1
    assert len(_calls(client, "GET", "/v1/jobs/" + JOB_ID)) == 2
    assert not output.exists()


def test_needs_input_is_not_media_completion(request_files):
    paths, state, output = request_files
    client = FakeClient()
    client.agent = {"agent_task_id": AGENT_ID, "status": "needs_input", "job": None,
                    "decision": "needs_input", "message": "请指定输出尺寸"}
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, INTENT, output)
        workflow.submit()
        result, code = workflow.wait(0)
    assert code == 1 and not result["ok"]
    assert result["decision"] == "needs_input"
    assert result["message"] == "请指定输出尺寸"
    assert "downloads" not in result and not output.exists()
    assert not _calls(client, "POST", "/v1/files/" + FILE_ID + "/release")


def test_failed_output_is_reported_as_partial_exit_two(request_files):
    paths, state, output = request_files
    client = FakeClient()
    client.job = _job("partial", released=True, outputs={
        "result": {"status": "failed", "error": {"code": "NO_AUDIO_TRACK"}},
    })
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        workflow.submit()
        result, code = workflow.wait(0)
    assert code == 2 and not result["ok"] and result["uploads_released"]
    assert result["downloads"]["result"] == {"partial": True, "code": "NO_AUDIO_TRACK"}
    assert len(_calls(client, "POST", "/v1/files/" + FILE_ID + "/release")) == 1


@pytest.mark.parametrize("download_failure", ["exception", "partial_collection"])
def test_download_failure_returns_partial_and_releases_lease(request_files, monkeypatch,
                                                            download_failure):
    paths, state, output = request_files
    client = FakeClient()
    client.job = _job("succeeded", released=True, outputs={
        "result": {"status": "succeeded", "artifact_id": ARTIFACT_ID},
    })

    def incomplete_download(*_):
        if download_failure == "exception":
            raise ConsumerError("DOWNLOAD_HASH_MISMATCH")
        return {"partial": True, "files": [], "failures": [{"code": "TIMESTAMP_OUT_OF_RANGE"}]}

    monkeypatch.setattr("ffmpeg_agent_client.workflow.save_artifact", incomplete_download)
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        workflow.submit()
        result, code = workflow.wait(0)
    assert code == 2 and not result["ok"]
    assert result["downloads"]["result"]["partial"] and result["uploads_released"]
    assert len(_calls(client, "DELETE", "/v1/leases/" + LEASE_ID)) == 1


def test_state_lock_and_private_permissions(request_files):
    paths, state, output = request_files
    client = FakeClient()
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        with pytest.raises(ConsumerError, match="STATE_BUSY_OR_UNWRITABLE"), Record(
            state, client.base
        ):
            pytest.fail("same state record was concurrently locked")
        if os.name != "nt":
            assert stat.S_IMODE(state.stat().st_mode) == 0o600
            assert stat.S_IMODE(state.with_name(state.name + ".lock").stat().st_mode) == 0o600
    with Record(state, client.base) as record:
        assert record.data["inputs"]["source"]["size_bytes"] == paths["source"].stat().st_size
    with pytest.raises(ConsumerError, match="STATE_TARGET_OR_FORMAT_MISMATCH"), Record(
        state, "https://different.example.invalid"
    ):
        pytest.fail("record from another service was accepted")


def test_existing_output_directory_is_never_adopted(request_files):
    paths, state, output = request_files
    output.mkdir()
    original = output / "user-file.txt"
    original.write_text("keep this file")
    client = FakeClient()
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        with pytest.raises(ConsumerError, match="OUTPUT_EXISTS_OR_UNSAFE"):
            workflow.prepare(paths, OPERATION, output)
    assert original.read_text() == "keep this file"
    assert not state.exists() and not client.calls and not client.uploads


def test_existing_output_content_is_not_overwritten_during_collection(request_files):
    paths, state, output = request_files
    client = FakeClient()
    client.job = _job("succeeded", released=True, outputs={
        "result": {"status": "succeeded", "artifact_id": ARTIFACT_ID},
    })
    with Record(state, client.base) as record, Workflow(client, record) as workflow:
        workflow.prepare(paths, OPERATION, output)
        workflow.submit()
        output.mkdir()
        (output / ".ffmpeg-agent-owner").write_text(record.data["request_id"])
        (output / "result").write_bytes(b"user content")
        result, code = workflow.wait(0)
    assert code == 2 and not result["ok"]
    assert result["downloads"]["result"] == {"partial": True, "code": "OUTPUT_EXISTS"}
    assert (output / "result").read_bytes() == b"user content"
