# ffmpeg-agent

独立的单容器媒体处理服务。业务提交声明式任务图，服务持久化任务、调度 FFmpeg/Pillow 处理并发布不可变产物；业务通过共享卷或 HTTP 消费结果。首版使用 Python 3.14、FastAPI、单 API 进程、SQLite/aiosqlite，逻辑 Worker 默认 1、允许 1～4。

实现范围和验收状态以 [API 合同](docs/api/v1-contract.md) 与 [实施进度](docs/plans/v1-implementation-progress.md) 为准。当前真实容器和容量记录覆盖原生 Linux arm64；amd64 已完成模拟环境的依赖/引擎探测，原生 amd64 容量与两台实际宿主机交付仍未验证。

## 支持范围

| 操作 | 用途 |
| --- | --- |
| `media.probe.v1` | 生成媒体 JSON 探测报告 |
| `video.transcode.v1` | 固定 `web_720p_v1`，MP4 / H.264 / 最高 1280×720 / 30fps |
| `audio.transcode.v1` | AAC 双声道 128k 或 PCM 单声道 16kHz |
| `video.extract_audio.v1` | 按音轨序号提取并转换音频 |
| `video.extract_frames.v1` | 按精确播放时间抽帧，保留逐项结果与实际时间 |
| `image.resize.v1` | 静态 JPEG / PNG / WebP 的方向、色彩、缩放与格式转换 |

任务最多 4 个输入、16 个步骤、16 个声明输出；步骤顺序不必为拓扑顺序。输入可以是 `shared_file`、受控来源的 `http_file` 或已有 `artifact`。参数、端口和版本严格校验，不接受任意 FFmpeg 命令或未知字段。运行中的准确能力与限制由 `GET /v1/capabilities` 返回。

## 启动

需要 Docker Compose。开发检查使用 uv；所有 Python 依赖、基础镜像和 Linux 系统包有精确版本及摘要记录。

```sh
uv sync --frozen
python3 deploy/generate-config.py
docker compose -f deploy/compose.yaml build media
docker compose -f deploy/compose.yaml up -d media
curl --fail http://127.0.0.1:8080/health/ready
```

配置生成到 Git 忽略的 `deploy/local/media-config.json`，token 不输出到终端。目录必须保持 `0700`；配置作为只读 secret 挂载。请在私有文件中编辑调用方、授权存储、HTTP 来源规则和 `public_base_url`，不要把凭据放入 shell 命令参数、日志或提交。默认 HTTP 来源规则为空，默认禁止外部下载。

服务以 UID/GID `10001:10001` 运行，根文件系统只读，`state`、`work`、输入和输出各自独立。默认资源为 2 CPU、2 GiB、128 PID，端口仅映射到宿主机 `127.0.0.1:8080`。跨机调用通过受控 HTTPS 入口，配置可达的 `public_base_url`。同 Compose、跨 Compose 外部卷与网络配置见 [部署说明](deploy/README.md)。

## 提交和查询任务

所有 `/v1` 请求携带 `Authorization: Bearer <token>`；创建任务还必须携带调用方内稳定的 `Idempotency-Key`。以下请求假设业务已把 `sample.mp4` 以“写临时文件、关闭、同文件系统重命名”的方式交到授权输入卷：

```json
{
  "inputs": {
    "source": {"kind": "shared_file", "storage_id": "input", "key": "sample.mp4"}
  },
  "steps": [
    {
      "id": "encode",
      "operation": "video.transcode.v1",
      "inputs": {"video": {"source": "source"}},
      "params": {"preset": "web_720p_v1"}
    }
  ],
  "outputs": {"video": {"step": "encode", "output": "video"}},
  "output_policy": {"retention_seconds": 86400}
}
```

将 JSON 保存为任务文件，用下例从私有配置读取 token 并提交；只打印服务生成的任务 ID：

```python
import json
import urllib.request
from pathlib import Path

config = json.loads(Path("deploy/local/media-config.json").read_text())
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/jobs",
    data=Path("job.json").read_bytes(),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + config["callers"]["demo"]["tokens"][0],
        "Idempotency-Key": "example-video-001",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    print(json.load(response)["job_id"])
```

首次接受返回 `202`，同键同规范化请求重放返回 `200` 和原任务；同键不同请求返回 `409 IDEMPOTENCY_CONFLICT`。用 `GET /v1/jobs/{job_id}` 轮询。终态为 `succeeded`、`partial`、`failed` 或 `canceled`；声明输出中的成功项提供 `artifact_id`。独立分支可以继续运行，任务的部分成功不意味着每个声明输出可用。

幂等记录在任务终态后至少保留 24 小时，准确截止时间见任务的 `idempotency_expires_at`；这与产物保留期分别计算。客户端重试应复用原键和原请求，不能因等待超时就生成新键重复处理。

HTTP 输入时，将完整示例中的 `inputs` 替换为下列对象；实际来源必须满足调用方来源规则，签名 URL 和受控认证头仅保存在私有请求文件中：

```json
{
  "source": {
    "kind": "http_file",
    "url": "https://controlled-source.example.internal/sample.mp4",
    "headers": {}
  }
}
```

已有产物输入时，使用同调用方可引用的真实视频产物 ID 替换下例 ID，仍替换完整示例的 `inputs`。如果引用集合中的单个成员，再提供对应的 `member_key`，并确保其媒体类型与下游操作端口相符：

```json
{
  "source": {
    "kind": "artifact",
    "artifact_id": "art_0123456789abcdef0123456789abcdef"
  }
}
```

共享输入必须保持不变，直到 `inputs_released=true`。`POST /v1/jobs/{job_id}/cancel` 只接受取消意图；业务等待终态和输入释放，不能把返回 `202` 当作 FFmpeg 已停止。HTTP 来源须满足调用方的精确协议、hostname、端口和网络规则，建议附带预期大小与 SHA-256；完整获取后服务使用固定副本。

## 读取和保留产物

| 请求 | 行为 |
| --- | --- |
| `GET /v1/artifacts/{id}` | 元数据、大小、SHA-256 与授权访问方式 |
| `GET/HEAD /v1/artifacts/{id}/content` | 单文件内容 |
| `GET /v1/artifacts/{id}/members` | 集合成员和逐项结果分页 |
| `GET/HEAD /v1/artifacts/{id}/files/{member_key}` | 集合中一个已发布文件 |
| `POST /v1/artifacts/{id}/leases` | 用 `{"duration_seconds":900}` 申请租约 |
| `PATCH /v1/leases/{lease_id}` | 在允许期限内续租 |
| `DELETE /v1/leases/{lease_id}` | 幂等释放租约 |
| `PATCH /v1/artifacts/{id}/retention` | 用 `{"retention_seconds":86400}` 延长保留 |
| `POST /v1/artifacts/{id}/release` | 提前进入回收流程 |

共享卷消费必须先申请租约，再按元数据中的 `storage_id` 与相对 `key` 读取，结束后释放。两侧容器挂载路径可以不同，实际卷必须相同；业务只能只读挂载输出，不能挂载服务 state/work。远端下载通过 `X-Artifact-Lease-Id` 携带有效租约，校验响应长度和 SHA-256。HTTP 读取另有内部引用保护实际发送；显式租约适合保护元数据获取、分段读取和业务处理之间的整个窗口。

产物支持 HEAD、单 Range、ETag 和 If-Range；合法多 Range 回退完整 `200`。集合不自动打包。进入 `retiring` 后拒绝新引用和续租，已有有效租约仍可读；release 不强制打断已有发送。删除只由服务对已登记且满足保留期与引用条件的自有文件执行。

## 验证

本机测试需要可执行的 FFmpeg/ffprobe；当前开发测试默认期望 9.0 系列，使用 7.1 时设置 `MEDIA_TEST_ENGINE_SERIES=7.1`。服务容器固定 7.1，推荐直接使用部署说明中的 `deploy/verify-tests.py --image <精确镜像ID>` 在相同运行基线内执行完整测试，避免本机引擎与安装差异。

```sh
uv run ruff check .
uv run pyright
uv run pytest
```

机器可读合同为 [OpenAPI](docs/api/v1-openapi.json)、[创建任务](docs/api/JobCreate.json)、[任务结果](docs/api/JobResult.json)、[产物元数据](docs/api/ArtifactResult.json)、[成员分页](docs/api/MembersResult.json) 和 [统一错误](docs/api/ErrorEnvelope.json)。从当前实现重新导出：

```sh
PYTHONPATH=src uv run python scripts/export_contract.py
```

真实媒体测试使用仓库内隔离 fixture 与临时目录。[fixture 来源](tests/fixtures/README.md) 仅确认内部测试复用，未确认公开再分发许可。容器验收、同/跨 Compose 交接和 Worker 1/2/4 容量脚本见 [部署验证命令](deploy/README.md#本地验收与容量实测)；[运行基线](docs/verification/runtime-baseline.md) 记录版本与架构范围。

两台实际宿主机的 HTTP 获取、转码、租约、HEAD/Range、下载摘要及 ffprobe 验收可用 [`deploy/verify-http-remote.py`](deploy/verify-http-remote.py)。其 `--local-smoke` 只验证脚本，不计为真实跨机证据。

## 升级、备份和限制

升级先在业务入口停止新提交并排空任务，再正常停止整个媒体容器；不要在容器内热重启 API。保留原镜像、精确配置与版本清单，使用同一组数据卷启动兼容镜像。任务接受时冻结引擎与版本要求；恢复发现不兼容会明确失败，不把旧任务悄悄换成新预设。

备份时停止新任务并正常停止容器，作为一组保存 state、work、output、配置与镜像版本；共享输入由业务另行保留。恢复时先在独立副本验证完整性与版本兼容。不能只复制 SQLite 就声称产物已备份，也不能把 `docker compose down -v` 当作正常清理命令。

首版是单实例服务：禁止 Uvicorn 多进程/reload、多副本共享状态、SQLite 放 NFS、跨容器分布式 Worker。同一 daemon 的外部卷能共享数据，不同宿主机或 Docker daemon 的同名卷不能。默认 Worker 1 的容量建议只来自当前 fixture 和 2 CPU 实测，生产规格需按实际输入重新测量。

首版视频拒绝 HDR、10 bit 和非方形 SAR，图片拒绝动画、多页与不支持的色彩模式；不解析媒体内部外部网络资源，不接受任意引擎参数。详细语义与错误边界见 [API 合同](docs/api/v1-contract.md)。镜像中的 Debian FFmpeg 启用了 GPL 组件，保留其软件包许可证；分发镜像前按实际使用与分发方式核对依赖许可。
