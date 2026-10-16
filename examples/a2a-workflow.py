"""通过官方 A2A 1.0 客户端提交描述，轮询任务并校验 HTTP 下载。"""

import argparse
import asyncio
import logging
import math
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from client_common import ROOT, Client, ExampleError
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

TERMINAL = {
    pb.TASK_STATE_COMPLETED, pb.TASK_STATE_FAILED, pb.TASK_STATE_CANCELED, pb.TASK_STATE_REJECTED,
}


def parse_args():
    parser = argparse.ArgumentParser(description="A2A 自然语言媒体任务与 HTTP 文件传输")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, help="新建的结果目录，每个命名输出单独保存")
    parser.add_argument("--intent-file", type=Path, help="UTF-8 描述文件；恢复查询时无需提供")
    parser.add_argument("--config", type=Path, default=ROOT / "deploy/local/media-config.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="包含服务 Token 的私有 .env")
    parser.add_argument("--base-url", help="覆盖服务 URL，不含凭据")
    parser.add_argument("--request-id", default=uuid4().hex, help="同一完整请求重试时复用")
    parser.add_argument("--task-id", help="恢复已有 agent task 的查询，不再次上传/规划")
    parser.add_argument("--cancel", action="store_true", help="需同时指定 --task-id，等待实际停止")
    parser.add_argument("--wait-seconds", type=float, default=600)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    if args.destination.exists() or args.destination.is_symlink():
        raise ExampleError("结果目录已存在，请选择新目录")
    if not args.destination.parent.is_dir():
        raise ExampleError("结果目录的父目录不存在")
    if not math.isfinite(args.wait_seconds) or args.wait_seconds <= 0:
        raise ExampleError("--wait-seconds 必须是有限正数")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.request_id):
        raise ExampleError("--request-id 只允许 1～80 个字母、数字、下划线或连字符")
    if args.task_id and not re.fullmatch(r"agent_[a-f0-9]{32}", args.task_id):
        raise ExampleError("--task-id 格式错误")
    if args.cancel and not args.task_id:
        raise ExampleError("取消需要 --task-id")
    if not args.task_id:
        if not args.source.is_file() or not args.intent_file:
            raise ExampleError("新请求需要源文件与 --intent-file")
        intent = args.intent_file.read_text(encoding="utf-8").strip()
        if not 1 <= len(intent) <= 4000:
            raise ExampleError("描述需为 1～4000 字符")
    for name in ("a2a", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
    files = Client(args.config, args.base_url, env_file=args.env_file)
    async with httpx.AsyncClient(headers=files.headers, timeout=30, follow_redirects=False) as http:
        card = await A2ACardResolver(http, files.base).get_agent_card()
        if not any(interface.url == files.base + "/a2a"
                   and interface.protocol_binding == "JSONRPC"
                   and interface.protocol_version == "1.0"
                   for interface in card.supported_interfaces):
            raise ExampleError("Agent Card 未提供预期的同源 A2A 1.0 JSON-RPC 接口")
        # 只使用已验证的同源接口，避免把服务凭据发送到其他主机。
        for index in range(len(card.supported_interfaces) - 1, -1, -1):
            if card.supported_interfaces[index].url != files.base + "/a2a":
                del card.supported_interfaces[index]
        client = ClientFactory(ClientConfig(
            httpx_client=http, streaming=False, polling=True,
            supported_protocol_bindings=["JSONRPC"],
        )).create(card)
        if args.task_id:
            task = (await client.cancel_task(pb.CancelTaskRequest(id=args.task_id)) if args.cancel
                    else await client.get_task(pb.GetTaskRequest(id=args.task_id)))
        else:
            print("request_id:", args.request_id, flush=True)
            uploaded = await asyncio.to_thread(files.upload, args.source, args.request_id)
            body = {"intent": intent, "inputs": {
                "source": {"kind": "uploaded_file", "file_id": uploaded["file_id"]},
            }}
            request = pb.SendMessageRequest(message=pb.Message(
                message_id=args.request_id, role=pb.ROLE_USER,
                parts=[pb.Part(data=ParseDict(body, Value()))],
            ))
            responses = [event async for event in client.send_message(request)]
            if len(responses) != 1 or not responses[0].HasField("task"):
                raise ExampleError("服务未返回异步任务")
            task = responses[0].task
        print("agent_task_id:", task.id, flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while task.status.state not in TERMINAL:
            if time.monotonic() >= deadline:
                raise ExampleError("等待超时，可用 --task-id " + task.id + " 恢复；任务仍可继续运行")
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
            task = await client.get_task(pb.GetTaskRequest(id=task.id))
        print("status:", pb.TaskState.Name(task.status.state), flush=True)
        if task.status.state == pb.TASK_STATE_CANCELED and args.cancel:
            return 0
        if not task.artifacts:
            raise ExampleError("任务已结束，本次没有可下载产物")
        args.destination.mkdir()
        for artifact in task.artifacts:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", artifact.name):
                raise ExampleError("无效的产物名称")
            if not re.fullmatch(r"art_[a-f0-9]{32}", artifact.artifact_id):
                raise ExampleError("无效的产物标识")
            metadata = await asyncio.to_thread(
                files.api, "GET", "/v1/artifacts/" + artifact.artifact_id,
            )
            await asyncio.to_thread(files.save_artifact, metadata, args.destination / artifact.name)
        print("产物已下载并核对大小与 SHA-256", flush=True)
        if task.status.state != pb.TASK_STATE_COMPLETED:
            print("任务未全部成功，已保存可用产物", file=sys.stderr)
            return 2
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ExampleError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (Exception, KeyboardInterrupt) as error:
        print("示例已停止：" + type(error).__name__ + "；已接受的任务可用 --task-id 恢复",
              file=sys.stderr)
        sys.exit(1)
