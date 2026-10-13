# ffmpeg-agent

[English](README.md) | **简体中文**

面向应用程序和远程 AI Agent 的媒体处理服务，支持 HTTP 和 MCP。上传文件、提交异步任务，再下载处理结果。客户端无需共享卷，也可以将共享卷与 HTTP 传输组合使用。

| 你可以做什么 | 操作 |
| --- | --- |
| 探测媒体信息，下载 JSON 报告 | `media.probe.v1` |
| 将视频转为适合网页播放的 MP4 / H.264，最高 720p、30 fps | `video.transcode.v1` |
| 将音频转为 AAC 双声道或 16 kHz 单声道 WAV | `audio.transcode.v1` |
| 从视频中提取指定音轨并转换格式 | `video.extract_audio.v1` |
| 按指定时间抽取 JPEG 或 PNG 视频帧 | `video.extract_frames.v1` |
| 缩放、转换静态 JPEG、PNG、WebP 图片 | `image.resize.v1` |

任务可以串联操作、复用输入并返回多个输出。通过 `GET /v1/capabilities` 查询运行中服务的预设与限制。

## 使用 Docker Compose 启动

需要 Docker、Compose 和 Python 3。在仓库根目录运行以下命令，无需在宿主机安装服务的 Python 依赖。已有 `.env` 时保留它，跳过复制命令。

```sh
cp .env.example .env
python3 deploy/generate-config.py
docker compose up -d --build
docker compose logs -f media
```

用 Ctrl+C 退出日志查看，再检查服务是否就绪：

```sh
curl --fail http://127.0.0.1:8080/health/ready
```

生成器将随机服务 Token 写入 `deploy/local/media-config.json`，拒绝覆盖已有配置；已有该文件时跳过生成即可。保持其父目录仅当前用户可访问（`0700`）。

在 `.env` 中设置镜像名称、宿主机监听地址、端口、配置文件位置和容器资源限制；在私有 JSON 中设置 Token、公开地址、存储和服务限额。Token 只保存在私有 JSON，不放入 `.env`、命令行参数或 Git。HTTP、MCP、上传和下载统一通过 `Authorization: Bearer ...` 校验；将 `service_token` 显式设为 `null` 即开放这些入口。

| `.env` 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MEDIA_IMAGE` | `ffmpeg-agent:v1-local` | 本地镜像名称 |
| `MEDIA_BIND_ADDRESS` / `MEDIA_PORT` | `127.0.0.1` / `8080` | 宿主机公开地址和端口 |
| `MEDIA_CONFIG_PATH` | `./deploy/local/media-config.json` | 私有 JSON 路径，相对于仓库根目录 |
| `MEDIA_CPUS` / `MEDIA_MEMORY` / `MEDIA_PIDS_LIMIT` | `2.0` / `2g` / `128` | 容器 CPU、内存和进程数限制 |

私有 JSON 中，`service_token` 控制鉴权，`public_base_url` 决定客户端和产物 URL，`worker_concurrency` 设置逻辑任务并发数（1～4，默认 1）。修改 `MEDIA_PORT` 时，同时更新 `public_base_url` 和就绪检查 URL，否则示例与返回的产物 URL 仍指向 8080 端口。

默认服务仅在宿主机的 8080 端口可访问。远程使用时提供 HTTPS 入口，将 `public_base_url` 设为客户端可达的地址，并转发正确的公开 Host 头。MCP 按该地址检查 Host 和 Origin。以下客户端示例读取此 JSON；从另一台机器调用时，准备私有的客户端配置副本。

## 上传、处理和下载

下面的 Python 3 标准库示例以分块方式上传和下载，不将整个视频读入内存。它会转换 `sample.mp4`，等待任务完成，再验证结果长度与 SHA-256。运行期间保持输入文件不变。

将代码保存为 `example.py`，准备好 `sample.mp4`，执行：

```sh
python3 example.py sample.mp4 result.mp4
```

输出文件不能已存在。如果修改了 `MEDIA_CONFIG_PATH`，同时修改示例中的配置路径。

```python
import hashlib
import http.client
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

source, destination = map(Path, sys.argv[1:3])
config = json.loads(Path("deploy/local/media-config.json").read_text())
base = config["public_base_url"].rstrip("/")
token = config["service_token"]
auth = {"Authorization": "Bearer " + token} if token else {}


def api(method, path, body=None, extra=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {**auth, **(extra or {})}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response) if response.status != 204 else None


hasher = hashlib.sha256()
with source.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        hasher.update(chunk)
source_digest = hasher.hexdigest()
url = urllib.parse.urlsplit(base)
connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
connection = connection_type(url.hostname, url.port, timeout=300)
try:
    connection.putrequest("POST", url.path.rstrip("/") + "/v1/files")
    headers = {
        **auth,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(source.stat().st_size),
        "X-File-Sha256": source_digest,
        "Idempotency-Key": "readme-file-" + source_digest,
    }
    for name, value in headers.items():
        connection.putheader(name, value)
    connection.endheaders()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            connection.send(chunk)
    response = connection.getresponse()
    if response.status not in (200, 201):
        raise RuntimeError("Upload HTTP status: " + str(response.status))
    uploaded = json.loads(response.read())
finally:
    connection.close()

job = api("POST", "/v1/jobs", {
    "inputs": {"source": {"kind": "uploaded_file", "file_id": uploaded["file_id"]}},
    "steps": [{
        "id": "encode", "operation": "video.transcode.v1",
        "inputs": {"video": {"source": "source"}},
        "params": {"preset": "web_720p_v1"},
    }],
    "outputs": {"video": {"step": "encode", "output": "video"}},
}, {"Idempotency-Key": "readme-video-" + source_digest})
print("file_id:", uploaded["file_id"], "job_id:", job["job_id"])
deadline = time.monotonic() + 600
while job["status"] not in {"succeeded", "partial", "failed", "canceled"}:
    if time.monotonic() >= deadline:
        raise TimeoutError("Keep polling the same job_id; do not submit a duplicate job")
    time.sleep(1)
    job = api("GET", "/v1/jobs/" + job["job_id"])
if job["status"] != "succeeded":
    raise RuntimeError("Task status: " + job["status"])
artifact_id = job["outputs"]["video"]["artifact_id"]
lease = api("POST", f"/v1/artifacts/{artifact_id}/leases", {"duration_seconds": 900})
try:
    headers = {"X-Artifact-Lease-Id": lease["lease_id"]}
    artifact = api("GET", f"/v1/artifacts/{artifact_id}", extra=headers)
    request = urllib.request.Request(artifact["access"]["http"]["content"], headers={**auth, **headers})
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=300) as response, destination.open("xb") as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        if int(response.headers["Content-Length"]) != size:
            raise RuntimeError("Incomplete HTTP download")
    if size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
        raise RuntimeError("Artifact integrity check failed")
finally:
    api("DELETE", "/v1/leases/" + lease["lease_id"])
print("Download verified")
```

上传和任务提交使用各自的幂等键。此示例以文件摘要生成稳定键，同一文件重试会返回已有输入与任务，避免重复处理。需要主动发起新处理时更换键；已到期或已 release 的上传文件不能通过旧键重新创建。等待超时后，继续查询打印出的 `job_id`。

### 一个请求上传并提交任务

`POST /v1/jobs/upload` 接收 multipart 数据，第一个 part 必须是 `job` JSON，后面才是它声明的文件 part。上传完成、任务接受后返回 `job_id`，媒体处理仍异步执行。

以下可选示例使用项目的 Python 3.14 环境，需要 uv：先执行 `uv sync --frozen`，将代码保存为 `multipart-example.py`，准备好 `sample.mp4` 后执行 `.venv/bin/python multipart-example.py`。相同请求重试复用原键；另一项任务使用新键。

```python
import json
from pathlib import Path

import httpx

config = json.loads(Path("deploy/local/media-config.json").read_text())
token = config["service_token"]
headers = {"Authorization": "Bearer " + token} if token else {}
headers["Idempotency-Key"] = "readme-multipart-video-001"
job = {
    "inputs": {"source": {"kind": "upload", "part": "source"}},
    "steps": [{
        "id": "encode", "operation": "video.transcode.v1",
        "inputs": {"video": {"source": "source"}},
        "params": {"preset": "web_720p_v1"},
    }],
    "outputs": {"video": {"step": "encode", "output": "video"}},
}
with Path("sample.mp4").open("rb") as stream, httpx.Client(timeout=300) as client:
    response = client.post(
        config["public_base_url"].rstrip("/") + "/v1/jobs/upload",
        headers=headers,
        files=[
            ("job", (None, json.dumps(job), "application/json")),
            ("source", ("sample.mp4", stream, "application/octet-stream")),
        ],
    )
    response.raise_for_status()
    print(response.json()["job_id"])
```

通过 `GET /v1/jobs/{job_id}` 查询状态，再按上一示例通过产物接口下载。也可以先用 `POST /v1/files` 上传一次，通过 `GET /v1/files/{file_id}` 查询，再让多个任务复用该输入。

## 连接远程 MCP 客户端

使用 `public_base_url + /mcp` 的 Streamable HTTP 入口，本机为 `http://localhost:8080/mcp`。在客户端连接设置中配置服务 Bearer 头，同时为文件 HTTP 请求单独配置相同头。Token 不作为工具参数，也不进入下载 URL。

通过 `tools/list` 发现工具。例如，使用下列参数调用 `transcode_video`，将 `file_id` 替换为实际上传返回的值：

```json
{
  "source": {"kind": "uploaded_file", "file_id": "<file_id returned by HTTP upload>"},
  "params": {"preset": "web_720p_v1"},
  "idempotency_key": "readme-mcp-video-001"
}
```

工具返回 `job_id`。用 `{"job_id":"<returned job_id>"}` 调用 `get_job` 等待完成，再用返回的 `artifact_id` 调用 `get_artifact` 获取下载信息。媒体工具声明的输出名固定为 `result`，因此产物 ID 位于 `outputs.result.artifact_id`。

六个媒体工具为 `probe_media`、`transcode_video`、`transcode_audio`、`extract_audio`、`extract_frames` 和 `resize_image`。其他工具提供能力查询、任务图、取消、输入释放、产物和租约管理。参数见 [MCP 合同](docs/api/v1-contract.md#远程-mcp)。

文件字节通过 HTTP 传输：**上传 → `file_id` → MCP 处理与查询 → `artifact_id` → 下载**。MCP 也接受下表的其他输入类型。仅提供远程 MCP，服务不能读取客户端电脑上的文件路径。

## 选择输入和使用结果

| 输入类型 | 适用情况 |
| --- | --- |
| `uploaded_file` | 已通过 `POST /v1/files` 上传字节并取得 `file_id` |
| `http_file` | 服务能获取你的文件 URL，且来源符合已配置的允许规则 |
| `shared_file` | 服务和应用共享输入卷，传入 `storage_id` 与相对 `key` |
| `artifact` | 复用已有处理结果，可通过 `member_key` 选择集合成员 |

HTTP 来源默认禁止，配置规则后才能获取。共享输入只读借用，服务不会删除；所有使用它的任务返回 `inputs_released=true` 前，保持文件不变。配置共享卷不会关闭上传、HTTP 下载或 MCP。

任务终态为 `succeeded`、`partial`、`failed` 或 `canceled`，使用结果前检查每个声明输出。`POST /v1/jobs/{job_id}/cancel` 只请求取消；等待终态与 `inputs_released=true` 后，再修改或删除共享输入。

单文件产物使用 `GET/HEAD /v1/artifacts/{artifact_id}/content`，支持 Range 和 ETag。抽帧集合提供分页成员列表和逐文件下载，不自动打包为 ZIP。

| 存储与清理 | 默认行为 |
| --- | --- |
| 上传输入 | 上传完成后保留 24 小时，已接受任务在排队、执行和重试期间保护文件 |
| 任务工作文件 | 任务结束且实际输入使用、文件操作停止后回收 |
| 已发布产物 | 发布后保留 24 小时，正在读取、任务引用和有效租约会延迟删除 |
| 共享源文件 | 由你的应用管理，服务不删除 |

通过 `POST /v1/files/{file_id}/release` 或 `POST /v1/artifacts/{artifact_id}/release` 提前回收。Release 阻止新引用，等待已有使用者结束。读取共享输出前先申请租约，读完释放。HTTP 传输会保护正在发送的文件；显式租约还可保护元数据查询、下载和后续消费之间的完整时间窗口。保留期和租约时长可配置，准确到期时间由 API 返回。

## 运行和配置

`docker compose stop` 会保留卷。备份或升级前停止新提交、等待任务完成，再停止服务。将 **state、work、output、私有配置和镜像版本一起备份**；共享输入由应用负责备份。日常停止或清理不要使用 `docker compose down -v`。

每个服务实例独占自己的状态和工作卷。默认一个逻辑 Worker，可配置为 1～4 个；增加前按实际负载和资源测试。不要让多个容器同时访问同一状态目录。

当前预设不支持 HDR、10 bit 视频、非方形 SAR、动画或多页图片、HLS 处理及任意 FFmpeg 命令。支持的媒体语义和错误边界见 [API 合同](docs/api/v1-contract.md)。

- [部署与共享卷示例](deploy/README.md)
- [HTTP API 合同](docs/api/v1-contract.md)和 [OpenAPI](docs/api/v1-openapi.json)
- [实现与验证状态](docs/plans/v1-implementation-progress.md)
