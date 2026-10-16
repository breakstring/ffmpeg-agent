from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from .errors import ConsumerError
from .state import Record, read_json
from .transport import Client
from .workflow import Workflow, capabilities, check, input_paths


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ConsumerError("INVALID_COMMAND_ARGUMENTS")


def parser():
    root = Parser(description="ffmpeg-agent 消费者客户端（Python 3.10+，仅标准库）")
    root.add_argument("--env-file", type=Path, required=True, help="私有消费者 .env 路径")
    root.add_argument("--timeout", type=float, default=30, help="每个 HTTP 请求超时秒数")
    commands = root.add_subparsers(dest="command", required=True)
    connect = commands.add_parser("check", help="检查健康、认证和指定协议")
    connect.add_argument("--protocol", choices=["http", "mcp", "a2a", "all"], default="all")
    commands.add_parser("capabilities", help="获取部署实际支持的操作、参数和限额")
    upload = commands.add_parser("upload", help="上传本地文件并保存原生协议可用的引用")
    upload.add_argument("--input", action="append", required=True, metavar="NAME=PATH")
    run = commands.add_parser("run", help="上传、提交、等待和收集一次媒体请求")
    run.add_argument("--input", action="append", required=True, metavar="NAME=PATH")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--operation", help="单个版本化操作，例如 image.resize.v1")
    mode.add_argument("--definition-file", type=Path, help="包含 steps/outputs 的 JSON 文件")
    mode.add_argument("--intent-file", type=Path, help="UTF-8 自然语言请求文件")
    run.add_argument("--params", help="仅用于 --operation 的 JSON 参数对象")
    run.add_argument("--transport", choices=["http", "mcp", "a2a"], default="http")
    run.add_argument("--output-dir", type=Path, required=True, help="本次任务专属的新输出目录")
    resume = commands.add_parser("resume", help="使用原状态恢复，不重新规划已接受任务")
    collect = commands.add_parser("collect", help="接手原生协议返回的任务并收集结果")
    ids = collect.add_mutually_exclusive_group(required=True)
    ids.add_argument("--job-id")
    ids.add_argument("--agent-task-id")
    collect.add_argument("--output-dir", type=Path, required=True)
    status = commands.add_parser("status", help="查询实际任务状态")
    cancel = commands.add_parser("cancel", help="请求取消并等待实际停止")
    release = commands.add_parser("release", help="只释放本状态登记且不再使用的上传")
    for command in (upload, run, resume, collect, status, cancel, release):
        command.add_argument("--state", type=Path, required=True, help="私有任务记录 JSON 路径")
    for command in (run, resume, collect, cancel):
        command.add_argument("--wait-seconds", type=float, default=300,
                             help="本次等待预算，超时保留原状态（默认 300 秒）")
    return root


def specification(args):
    if args.params is not None and args.operation is None:
        raise ConsumerError("PARAMS_REQUIRE_OPERATION")
    if args.transport == "a2a" and args.intent_file is None:
        raise ConsumerError("A2A_REQUIRES_INTENT")
    if args.intent_file:
        try:
            with args.intent_file.open("rb") as stream:
                intent = stream.read(16001).decode("utf-8")
        except (OSError, UnicodeError):
            raise ConsumerError("INVALID_INTENT_FILE") from None
        if not intent.strip() or len(intent) > 4000:
            raise ConsumerError("INVALID_INTENT_FILE")
        return {"mode": "intent", "intent": intent}
    if args.definition_file:
        definition = read_json(args.definition_file)
        if (set(definition) - {"steps", "outputs", "output_policy"}
                or not isinstance(definition.get("steps"), list)
                or not isinstance(definition.get("outputs"), dict)
                or not definition["steps"] or not definition["outputs"]):
            raise ConsumerError("INVALID_DEFINITION_FILE")
        return {"mode": "definition", "definition": definition}
    params = None
    if args.params is not None:
        try:
            params = json.loads(args.params)
            if not isinstance(params, dict):
                raise ValueError
        except (ValueError, RecursionError):
            raise ConsumerError("INVALID_PARAMS_JSON") from None
    return {"mode": "operation", "operation": args.operation, "params": params}


def output_path(path: Path):
    path = path.expanduser().absolute()
    # /tmp 等系统路径可使用；实际落盘位置固定为其父目录的真实路径。
    return path.parent.resolve() / path.name


def execute(args):
    if hasattr(args, "wait_seconds") and (not math.isfinite(args.wait_seconds)
                                           or not 0 <= args.wait_seconds <= 86400):
        raise ConsumerError("INVALID_WAIT_SECONDS")
    client = Client.with_env(args.env_file, args.timeout)
    if args.command == "check":
        result = check(client, args.protocol)
        return result, 0 if result["ok"] else 1
    if args.command == "capabilities":
        return capabilities(client), 0
    with Record(args.state, client.base) as record, Workflow(client, record) as workflow:
        if args.command == "upload":
            workflow.prepare(input_paths(args.input))
            return {"ok": True, **workflow.upload()}, 0
        if args.command == "run":
            workflow.prepare(input_paths(args.input), specification(args),
                             output_path(args.output_dir), args.transport)
            workflow.submit()
        elif args.command == "resume":
            record.require_existing()
            workflow.submit()
        elif args.command == "collect":
            workflow.attach(args.job_id, args.agent_task_id, output_path(args.output_dir))
        elif args.command == "status":
            return {"ok": True, **workflow.query()}, 0
        elif args.command == "release":
            return {"ok": True, **workflow.release()}, 0
        if args.command in {"run", "resume", "collect", "cancel"}:
            return workflow.wait(args.wait_seconds, cancel=args.command == "cancel")
    raise ConsumerError("INVALID_COMMAND")


def main(argv=None):
    try:
        if sys.version_info < (3, 10):  # noqa: UP036 -- 消费者与服务端的 Python 基线不同
            raise ConsumerError("PYTHON_310_REQUIRED")
        if os.name != "nt":
            os.umask(0o077)
        result, code = execute(parser().parse_args(argv))
    except ConsumerError as error:
        result = {"ok": False, "code": error.code}
        if error.status is not None:
            result["http_status"] = error.status
        code = 1
    except KeyboardInterrupt:
        result, code = {"ok": False, "code": "INTERRUPTED", "resumable": True}, 130
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        # 不输出异常正文或 traceback，其中可能含认证、私有路径或模型输入。
        result, code = {"ok": False, "code": "INVALID_LOCAL_OR_REMOTE_DATA"}, 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
