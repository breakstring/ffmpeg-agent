import errno
import hashlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4


def main() -> None:
    base = os.environ.get("MEDIA_SERVICE_URL", "http://media:8080").rstrip("/")
    token = Path("/run/secrets/caller-token").read_text().strip()

    def request(method, path, body=None, key=None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        call = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(call, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            status = response.getcode()
            assert isinstance(status, int)
            return status, json.loads(content) if content else None

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if request("GET", "/health/ready")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        raise AssertionError("媒体服务未就绪")

    name = f"client-{uuid4().hex}.mp3"
    fixture = Path("/fixtures/222.mp3")
    source = Path("/business/input") / name
    staging = source.with_suffix(".tmp")
    with fixture.open("rb") as incoming, staging.open("xb") as output:
        shutil.copyfileobj(incoming, output)
        output.flush()
        os.fsync(output.fileno())
    staging.rename(source)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    body = {
        "inputs": {
            "main": {
                "kind": "shared_file",
                "storage_id": "input",
                "key": name,
                "expected_size_bytes": source.stat().st_size,
                "expected_sha256": checksum,
            }
        },
        "steps": [
            {"id": "probe", "operation": "media.probe.v1", "inputs": {"media": {"source": "main"}}}
        ],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    }
    status, job = request("POST", "/v1/jobs", body, name)
    assert status == 202, f"接受任务返回 {status}"
    job_id = job["job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, job = request("GET", f"/v1/jobs/{job_id}")
        assert status == 200
        if job["status"] in {"succeeded", "partial", "failed", "canceled"}:
            break
        time.sleep(0.1)
    assert job["status"] == "succeeded" and job["inputs_released"] is True
    artifact_id = job["outputs"]["report"]["artifact_id"]
    status, artifact = request("GET", f"/v1/artifacts/{artifact_id}")
    assert status == 200 and artifact["state"] == "ready"
    status, lease = request("POST", f"/v1/artifacts/{artifact_id}/leases", {"duration_seconds": 60})
    assert status == 201
    lease_id = lease["lease_id"]
    try:
        key = PurePosixPath(artifact["access"]["shared"]["key"])
        assert not key.is_absolute() and ".." not in key.parts
        result = Path("/business/output") / key
        contents = result.read_bytes()
        assert hashlib.sha256(contents).hexdigest() == artifact["sha256"]
        assert isinstance(json.loads(contents), dict)
        try:
            (Path("/business/output") / f"write-must-fail-{uuid4().hex}").write_text("test")
        except OSError as error:
            assert error.errno == errno.EROFS
        else:
            raise AssertionError("业务侧输出卷未只读挂载")
    finally:
        assert request("DELETE", f"/v1/leases/{lease_id}")[0] == 204
    assert source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == checksum
    print(
        json.dumps(
            {
                "status": "passed",
                "job_id": job_id,
                "artifact_id": artifact_id,
                "checks": [
                    "atomic_input_handoff",
                    "real_probe",
                    "distinct_mount_paths",
                    "lease_protected_shared_read",
                    "output_readonly",
                    "source_preserved",
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
