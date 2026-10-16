"""固定服务镜像、真容器 KILL/重启和假 planner 的 Agent 持久化恢复验收。"""

import argparse
import hashlib
import importlib.util
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_fault_base", ROOT / "deploy/verify-faults.py")
assert spec and spec.loader
faults = importlib.util.module_from_spec(spec)
spec.loader.exec_module(faults)
docker = faults.docker
MODES = ("planning", "planned", "submitting", "submitted", "cancel_planning", "cancel_submitting")


class Harness(faults.Harness):
    def run_container(self, name: str, command: list[str]):
        path = Path(self.temp.name) / "config.json"
        config = json.loads(path.read_text())
        config.update({
            "llm_enabled": True, "a2a_enabled": False,
            "llm_api_key": "isolated-fake-planner-no-model-access",
            "llm_model": "isolated-fake-planner", "llm_base_url": "https://model.invalid/v1",
            "planning_concurrency": 1, "planning_timeout_seconds": 300,
        })
        faults.env_support.write_runtime_configuration(config, path)
        super().run_container(name, ["python", "/fault-tests/agent_fault_server.py", "serve", self.mode])

    def agent_control(self, *args: str):
        return json.loads(docker(
            "exec", self.name, "python", "/fault-tests/agent_fault_server.py", *args,
        ))

    def submit_agent(self, key):
        fixture = ROOT / "tests/fixtures/video_h264_640x360.mkv"
        body = {
            "intent": "读取测试媒体的属性",
            "inputs": {"source": {
                "kind": "shared_file", "storage_id": "input", "key": fixture.name,
                "expected_size_bytes": fixture.stat().st_size,
                "expected_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            }},
        }
        code, response = self.client.request("POST", "/v1/agent/tasks", body, key)
        assert code in {200, 202}, f"agent_accept_status={code}"
        return code, response["agent_task_id"]

    def agent(self, identifier):
        code, result = self.client.request("GET", f"/v1/agent/tasks/{identifier}")
        assert code == 200
        return result

    def finish_agent(self, identifier):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = self.agent(identifier)
            if result["status"] in faults.base.TERMINAL | {"needs_input", "unsupported"}:
                return result
            time.sleep(0.1)
        raise AssertionError("agent_completion_timeout")


def compact(result):
    return {"agent_task_id": result["agent_task_id"], "status": result["status"],
            "job_id": result["job_id"], "error_code": (result.get("error") or {}).get("code")}


def check_case(image, mode):
    with Harness(image, mode) as h:
        key = uuid.uuid4().hex
        code, identifier = h.submit_agent(key)
        assert code == 202
        checkpoint = h.agent_control("marker", mode)
        cancel_response = None
        if mode.startswith("cancel_"):
            code, cancel_response = h.client.request("POST", f"/v1/agent/tasks/{identifier}/cancel")
            assert code == 202 and cancel_response["status"] == "canceling"
            h.agent_control("marker", "cancel-observed")
        before = h.agent(identifier)
        snapshot_before = h.agent_control("snapshot", identifier)
        agent_before = snapshot_before["agent"]
        assert snapshot_before["model_calls"] == snapshot_before["agent_count"] == 1
        assert snapshot_before["runtime"]["clean"] is False
        expected = "canceling" if mode.startswith("cancel_") else mode
        assert agent_before["state"] == expected
        if mode == "submitted":
            assert before["job_id"] == checkpoint["job_id"]
            assert len(snapshot_before["jobs"]) == 1
            assert snapshot_before["jobs"][0]["status"] == "queued"
            assert snapshot_before["jobs"][0]["attempt_count"] == 0
            assert agent_before["request_cleared"]
        else:
            assert before["job_id"] is None and snapshot_before["jobs"] == []
            assert not agent_before["request_cleared"]
        h.kill()
        h.restart()
        after = h.finish_agent(identifier)
        replay_code, replay_id = h.submit_agent(key)
        assert replay_code == 200 and replay_id == identifier
        snapshot_after = h.agent_control("snapshot", identifier)
        agent_after = snapshot_after["agent"]
        assert snapshot_after["model_calls"] == snapshot_after["agent_count"] == 1
        assert snapshot_before["runtime"]["generation"] != snapshot_after["runtime"]["generation"]
        assert snapshot_after["runtime"]["clean"] is False
        assert agent_after["request_cleared"]
        artifact = None
        if mode == "planning":
            assert after["status"] == "failed" and after["error"]["code"] == "PLANNING_INTERRUPTED"
            assert after["job_id"] is None and snapshot_after["jobs"] == []
        elif mode.startswith("cancel_"):
            assert after["status"] == "canceled" and after["error"]["code"] == "CANCELED"
            assert after["job_id"] is None and snapshot_after["jobs"] == []
            assert agent_after["cancel_requested"] == 1
        else:
            assert after["status"] == "succeeded"
            assert len(snapshot_after["jobs"]) == 1
            assert after["job_id"] == snapshot_after["jobs"][0]["id"]
            assert agent_after["state"] == "submitted"
            assert agent_after["plan_sha256"] == agent_before["plan_sha256"]
            assert agent_after["job_hash"] == agent_before["job_hash"]
            assert after["job"]["inputs_released"] is True
            if mode == "submitted":
                assert after["job_id"] == before["job_id"]
            artifact = h.verify_artifact(after["job"])
        return {
            "checkpoint": mode, "before": compact(before), "after": compact(after),
            "cancel_response": compact(cancel_response) if cancel_response else None,
            "before_snapshot": snapshot_before, "after_snapshot": snapshot_after,
            "replay_status": replay_code, "artifact": artifact, "kills": h.kill_evidence,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--case", choices=MODES)
    args = parser.parse_args()
    image = json.loads(docker("image", "inspect", args.image))[0]
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(), "image_id": image["Id"],
        "architecture": image["Architecture"], "engine_series": "7.1",
        "planner": "injected_fake_no_model_network", "checks": [],
        "stop_method": "whole_container_SIGKILL_then_restart",
        "cleanup": "label_verified_owned_resources_only",
    }
    mode = "setup"
    try:
        for mode in MODES:
            if args.case and args.case != mode:
                continue
            started = time.monotonic()
            # inspect 得到的不可变镜像 ID 贯穿全部容器运行，不继续使用可漂移标签。
            evidence = check_case(image["Id"], mode)
            report["checks"].append({
                "case": mode, "passed": True,
                "duration_seconds": round(time.monotonic() - started, 3), "evidence": evidence,
            })
            print(json.dumps({"case": mode, "passed": True}), flush=True)
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure"] = {"case": mode, "type": type(error).__name__}
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
