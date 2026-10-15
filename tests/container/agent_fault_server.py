"""Agent 持久化边界故障入口；只由验收挂载，不进入生产镜像。"""

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import uvicorn
from entry import marker

from media_service.agent_models import PlanningOutcome
from media_service.api import create_app
from media_service.config import Settings
from media_service.service import Service

STATE = Path("/var/lib/ffmpeg-agent/state")
MODES = ("planning", "planned", "submitting", "submitted", "cancel_planning", "cancel_submitting")


def model_calls(directory):
    path = directory / "agent-model-calls.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


def record_model_call(directory):
    path = directory / "agent-model-calls.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps({"pid": os.getpid()}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def hold(directory, mode, **values):
    if not marker(directory, "agent-" + mode, **values):
        return
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if mode.startswith("cancel_"):
            # 已收到取消，但故意保持本地调用未停止，以验证 canceling 恢复边界。
            marker(directory, "agent-cancel-observed", boundary=mode)
            await asyncio.Event().wait()
        raise


class FakePlanner:
    def __init__(self, settings, mode):
        self.settings = settings
        self.mode = mode

    async def plan(self, request, capabilities):
        record_model_call(self.settings.state_dir)
        if self.mode in {"planning", "cancel_planning"}:
            await hold(self.settings.state_dir, self.mode)
        return PlanningOutcome.model_validate({
            "decision": "execute", "message": "独立容器探测计划",
            "plan": {
                "steps": [{"id": "probe", "operation": "media.probe.v1",
                           "inputs": {"media": {"source": "source"}}}],
                "outputs": {"result": {"step": "probe", "output": "report"}},
            },
        })

    async def close(self):
        pass


def serve(mode):
    assert mode in MODES
    os.umask(0o077)
    settings = Settings.from_file()
    assert settings.runtime_mode == "container" and settings.planning_concurrency == 1
    service = Service(settings, planner=FakePlanner(settings, mode))

    if mode == "planned":
        save = service.agent_tasks._save

        async def held_save(identifier, outcome, job_hash):
            await save(identifier, outcome, job_hash)
            await hold(settings.state_dir, mode, agent_task_id=identifier)

        service.agent_tasks._save = held_save

    if mode in {"submitting", "submitted", "cancel_submitting"}:
        accept = service.accept

        async def held_accept(caller, key, request, *, agent_request_id=None):
            if agent_request_id and mode != "submitted":
                await hold(settings.state_dir, mode, agent_task_id=agent_request_id)
            result = await accept(caller, key, request, agent_request_id=agent_request_id)
            if agent_request_id and mode == "submitted":
                await hold(settings.state_dir, mode, agent_task_id=agent_request_id,
                           job_id=result[0])
            return result

        service.accept = held_accept

    if mode == "submitted" and not (settings.state_dir / "fault-agent-submitted.json").exists():
        # 第一次启动固定停在已提交且尚未领取；整容器重启后使用真实媒体 Worker。
        async def unclaimed():
            return None

        service.state.claim = unclaimed

    uvicorn.run(create_app(settings, service), host=settings.host, port=settings.port,
                access_log=False, log_level="error")


def snapshot(identifier):
    with sqlite3.connect(f"file:{STATE / 'state.sqlite'}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id,state,cancel_requested,request IS NULL AS request_cleared,"
            "outcome,job_hash,job_id,error FROM agent_requests WHERE id=?", (identifier,),
        ).fetchone()
        assert row is not None
        agent = dict(row)
        outcome = agent.pop("outcome")
        agent["plan_sha256"] = hashlib.sha256(outcome.encode()).hexdigest() if outcome else None
        error = agent.pop("error")
        agent["error_code"] = json.loads(error)["code"] if error else None
        agent["request_cleared"] = bool(agent["request_cleared"])
        jobs = [dict(row) for row in connection.execute(
            "SELECT id,status,cancel_requested,attempt_count,inputs_released FROM jobs ORDER BY id"
        )]
        count = connection.execute("SELECT count(*) FROM agent_requests").fetchone()[0]
        runtime = json.loads(connection.execute(
            "SELECT value FROM meta WHERE key='runtime'"
        ).fetchone()[0])
    return {"agent": agent, "jobs": jobs, "agent_count": count,
            "model_calls": model_calls(STATE), "runtime": runtime}


def main():
    command = sys.argv[1]
    if command == "serve":
        serve(sys.argv[2])
    elif command == "snapshot":
        print(json.dumps(snapshot(sys.argv[2])))
    elif command == "marker":
        path = STATE / f"fault-agent-{sys.argv[2]}.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                print(json.dumps(json.loads(path.read_text())))
                return
            except (FileNotFoundError, ValueError):
                time.sleep(0.05)
        raise SystemExit("agent_fault_marker_not_observed")
    else:
        raise SystemExit("unknown_agent_test_command")


if __name__ == "__main__":
    main()
