# ffmpeg-agent

[English](README.md) | **简体中文**

面向应用程序和远程 AI Agent 的媒体处理服务，支持 HTTP 和 MCP。上传文件、提交异步任务，再下载处理结果。客户端无需共享卷，也可以将共享卷与 HTTP 传输组合使用。

| 你可以做什么 | 操作 |
| --- | --- |
| 探测媒体信息、选择报告内容，可选扫描统计帧数 | `media.probe.v1` |
| 直接转换视频，或自定义编码、尺寸、帧率和质量 | `video.transcode.v1` |
| 直接转换音频，或自定义编码、采样率、声道和码率 | `audio.transcode.v1` |
| 提取并转换指定音轨，或保留原编码复制音轨 | `video.extract_audio.v1` |
| 按时间点、间隔或数量抽帧，并调整图片尺寸和质量 | `video.extract_frames.v1` |
| 缩放、裁剪、补边和转换静态 JPEG、PNG、WebP 图片 | `image.resize.v1` |

任务可以串联操作、复用输入并返回多个输出。省略 `params` 即可使用默认处理，再按需补充参数；HTTP 和 MCP 接受相同参数。通过 `GET /v1/capabilities` 查询可用编码器、参数 schema 和限制。

## 使用 Docker Compose 启动

需要 Docker、Compose 和 Python 3。在仓库根目录运行以下命令；已有配置会保留，可以重复执行：

```sh
if [ ! -f .env ]; then cp .env.example .env; fi
if [ ! -f deploy/local/media-config.json ]; then
  python3 deploy/generate-config.py
fi
docker compose up -d --build --wait
```

Compose 会等待服务就绪。生成器将随机服务 Token 写入 `deploy/local/media-config.json`；保持其父目录仅当前用户可访问（`0700`）。下面的示例会自动读取这个文件，无需将 Token 复制到命令中。

在 `.env` 中设置公开地址、端口和容器资源；在私有 JSON 中设置 Token、公开 URL、存储和服务限额。HTTP、MCP、上传和下载通过相同 Bearer 头校验；将 `service_token` 设为 `null` 即开放这些入口。

| `.env` 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MEDIA_IMAGE` | `ffmpeg-agent:v1-local` | 本地镜像名称 |
| `MEDIA_BIND_ADDRESS` / `MEDIA_PORT` | `127.0.0.1` / `8080` | 宿主机公开地址和端口 |
| `MEDIA_CONFIG_PATH` | `./deploy/local/media-config.json` | 私有服务 JSON 路径，相对于仓库根目录 |
| `MEDIA_CPUS` / `MEDIA_MEMORY` / `MEDIA_PIDS_LIMIT` | `2.0` / `2g` / `128` | 容器 CPU、内存和进程数限制 |

如果使用自定义 `MEDIA_CONFIG_PATH`，生成或保留对应配置，并通过 `--config` 将路径传给示例。修改端口时，同时更新 JSON 中的 `public_base_url`。`worker_concurrency` 设置逻辑任务并发数（1～4，默认 1）。

默认服务仅在宿主机可访问。远程使用时提供 HTTPS 入口，将 `public_base_url` 设为客户端可达地址，并转发正确的公开 Host 头；MCP 按该地址检查 Host 和 Origin。远程客户端只需一份包含 `public_base_url` 和 `service_token` 的私有 JSON，通过 `--config` 指定即可，无需复制服务端的存储或部署配置。Token 不放入命令行参数或 Git。

## 上传、处理和下载

仓库提供完整客户端，负责上传本地文件、等待处理，再下载并验证结果。下面的命令使用已有示例素材和新的临时输出目录，可以直接复制，也可以重复运行。上传期间保持源文件不变。

### HTTP：上传时直接提交处理

这个 Python 3 标准库客户端无需安装额外依赖。默认将视频转为 H.264/MP4，保留源尺寸和帧时间节奏：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4"
```

它向 `POST /v1/jobs/upload` 发送一次 multipart 请求，轮询返回的任务，再保存经过校验的 MP4。无需指定编码参数、手动传递 ID 或挂载共享卷。

### HTTP：先上传，再提交处理

加上 `--upload-first`，即可演示先调用 `POST /v1/files`，再调用 `POST /v1/jobs`。客户端还会打印 `file_id`，上传文件有效期间可以让其他任务复用：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4" \
  --upload-first
```

两种方式都会完成上传 → 处理 → 下载。编写自己的 HTTP 客户端时，multipart 中的 `job` 结构如下，这是请求体说明，不是独立可执行命令：

```json
{
  "inputs": {"source": {"kind": "upload", "part": "source"}},
  "steps": [{
    "id": "process",
    "operation": "video.transcode.v1",
    "inputs": {"video": {"source": "source"}}
  }],
  "outputs": {"result": {"step": "process", "output": "video"}}
}
```

先发送 `job` part，再发送 `source` 文件 part，并设置 `Idempotency-Key` 请求头。先上传后提交时，将输入替换为 `{"kind":"uploaded_file","file_id":"<returned file_id>"}`。接口说明见 [HTTP 合同](docs/api/v1-contract.md)，完整实现见[客户端源码](examples/http-workflow.py)。

## 使用远程 MCP

安装 [uv](https://docs.astral.sh/uv/) 后，以下命令使用项目锁定版本的 SDK 运行完整 MCP 客户端：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
uv run --frozen python examples/mcp-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4"
```

客户端通过 HTTP 上传，调用远程 `transcode_video` 工具，通过 MCP 等待任务，再经 HTTP 下载。两种传输使用相同的私有配置和 Token，无需配置本地 stdio 服务。

接入其他 MCP 客户端时，使用 `public_base_url + /mcp` 的 Streamable HTTP 入口，本机为 `http://localhost:8080/mcp`；在连接设置中配置服务 Bearer 头，文件 HTTP 请求也使用相同的头。**仅有 MCP 不能上传你电脑上的文件路径**：客户端需要具备 HTTP 文件传输能力，或者使用服务可访问的输入 URL／共享文件。

下面仅用于说明 HTTP 上传后 `transcode_video` 的工具参数；调用时替换实际 `file_id` 和本次请求的幂等键：

```json
{
  "source": {"kind": "uploaded_file", "file_id": "<returned file_id>"},
  "idempotency_key": "<key for this request>"
}
```

工具返回 `job_id`。用 `get_job` 等待完成，再将 `outputs.result.artifact_id` 传给 `get_artifact` 获取下载信息。可执行客户端会处理这些步骤。通过 `tools/list` 发现全部工具；六个媒体工具与 HTTP 共用参数模型，其余工具管理任务、文件、产物和租约。详见 [MCP 合同](docs/api/v1-contract.md#远程-mcp)。

## 试用六种能力

两个客户端接受相同的 `--operation` 和 `--params`。默认操作为 `transcode_video`，六种能力都可以省略 `--params`。

| `--operation` | `tests/fixtures/` 下的示例输入 | 目标与默认结果 |
| --- | --- | --- |
| `probe_media` | `ffmpeg_h264_640x360.mkv` | `report.json`：格式和轨道信息 |
| `transcode_video` | `ffmpeg_h264_640x360.mkv` | `result.mp4`：H.264、CRF 23、源尺寸和时间节奏 |
| `transcode_audio` | `222.mp3` | `result.m4a`：AAC，自动选择兼容采样率／声道／码率 |
| `extract_audio` | `ffmpeg_h264_640x360.mkv` | `result.m4a`：提取首条音轨并转为 AAC |
| `extract_frames` | `ffmpeg_h264_640x360.mkv` | `frames/`：首帧 JPEG quality 90，加 `manifest.json` |
| `resize_image` | `haojiao.jpg` | `result.jpg`：原尺寸、JPEG quality 90 |

例如，用一条命令下载抽帧集合：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/frames" \
  --operation extract_frames
```

将 `python3 examples/http-workflow.py` 替换为 `uv run --frozen python examples/mcp-workflow.py`，即可通过 MCP 执行相同操作。

### 只调整需要的参数

参数文件只需包含希望覆盖的值。下面的完整示例将视频等比装入 1920×1080 且不放大，采用 CRF 20，并将音轨转为 44.1 kHz 单声道 AAC：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
cat > "$output_dir/params.json" <<'JSON'
{
  "resize": {"width": 1920, "height": 1080},
  "rate_control": {"mode": "crf", "value": 20},
  "audio": {"sample_rate": 44100, "channels": 1}
}
JSON
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4" \
  --params "$output_dir/params.json"
```

同一个参数文件也适用于 MCP 客户端。编写自己的请求时，将这个对象放入 HTTP 步骤的 `params` 或 MCP 工具的 `params`。

预设是可选快捷设置：`{"preset":"web_720p_v1"}` 选择 720p/30 fps 和双声道 AAC；`aac_stereo_128k_v1` 选择 AAC 128 kbps/48 kHz/双声道；`wav_mono_16k_v1` 选择 16 kHz 单声道 PCM。显式参数覆盖选定预设。未选预设时视频保留源尺寸和时间节奏。音频 `auto` 优先保持源采样率／声道，不兼容时自动调整；`source` 要求严格保持。音频复制模式保留原编码，不能同时调整编码参数或裁剪。

通过 `GET /v1/capabilities` 或 MCP `list_capabilities` 获取可用编码器、schema、默认值和限制。任务的 `steps[].effective_params` 展示接受的设置，产物的 `media.effective_params` 展示解析后的输出设置。编码、尺寸、帧率、码率、采样率和质量的完整控制见[参数合同](docs/design/media-parameters.md)。

### 等待、重试和下载

客户端先打印请求 ID，再打印已接受的 `job_id`。默认每次执行都会创建新请求；只有重试完全相同的请求时才显式复用 `--request-id`，包括源文件、操作、参数及上传方式。改变其中任何一项时使用新 ID。

默认等待 600 秒，可用 `--wait-seconds` 调整。等待超时不会取消后台处理；在相同客户端命令后加 `--job-id` 和已打印的 ID，即可跳过上传与提交，继续等待和下载。恢复时仍需填写 `SOURCE DESTINATION`，但不会读取源文件。`--help` 列出完整选项；`--base-url` 覆盖连接地址，`--config` 指定私有客户端 JSON。

目标文件或抽帧目录不能已存在。下载采用流式传输，大小和 SHA-256 校验通过后才形成完整文件。抽帧集合包含 `manifest.json`，记录成功与失败成员；`partial` 会保存可用成员并以状态码 2 退出。任务失败或传输失败会以非零状态退出；客户端不会覆盖已有文件，也不会将下载不完整的文件当作成功结果。

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

当前处理范围不包含 HDR、10 bit 视频、硬件加速、非方形 SAR、动画或多页图片、HLS 处理及任意 FFmpeg 命令。支持的媒体语义和错误边界见 [API 合同](docs/api/v1-contract.md)。

- [部署与共享卷示例](deploy/README.md)
- [HTTP API 合同](docs/api/v1-contract.md)和 [OpenAPI](docs/api/v1-openapi.json)
- [实现与验证状态](docs/plans/v1-implementation-progress.md)
