# ffmpeg-agent

[English](README.md) | **简体中文**

面向应用程序和远程 AI Agent 的媒体处理服务，支持 HTTP 和 MCP。上传文件、提交异步任务，再下载处理结果。客户端无需共享卷，也可以将共享卷与 HTTP 传输组合使用。

**已有服务，想让自己的 Agent 处理媒体？** 从[技能安装与使用](#让你的-agent-使用已部署的服务)开始。**需要自行部署？** 查看 [Docker 构建与启动](#使用-docker-compose-启动)。

| 能力 | 用途 |
| --- | --- |
| 媒体探测 | 探测媒体信息、选择报告内容，可选扫描统计帧数 |
| 视频转换 | 直接转换视频，或自定义编码、尺寸、帧率和质量 |
| 音频转换 | 直接转换音频，或自定义编码、采样率、声道和码率 |
| 音轨提取 | 提取并转换指定音轨，或保留原编码复制音轨 |
| 视频抽帧 | 按时间点、间隔或数量抽帧，并调整图片尺寸和质量 |
| 图片处理 | 缩放、裁剪、补边和转换静态 JPEG、PNG、WebP 图片 |

任务可以串联操作、复用输入并返回多个输出。省略 `params` 即可使用默认处理，再按需补充参数；HTTP 和 MCP 接受相同参数。通过 `GET /v1/capabilities` 查询可用编码器、参数 schema 和限制。

可选自然语言入口使用 PydanticAI 将描述转换为受校验的媒体计划，也可开启官方 A2A 入口供其他 Agent 调用同一任务服务。两项能力默认关闭，配置和示例见[自然语言与 A2A 使用指南](docs/usage/agent.zh-CN.md) / [English guide](docs/usage/agent.md)。

## 让你的 Agent 使用已部署的服务

消费者只需 **Python 3.10+**、可达的服务地址和服务 Token。运行时无需额外 Python 包、Docker、FFmpeg、服务端源码或模型 Key。两个技能都附带同一套独立的标准库客户端，可分别安装。

| 技能 | 用途 |
| --- | --- |
| [`ffmpeg-agent-connect`](skills/ffmpeg-agent-connect/SKILL.md) | 首次连接、认证与能力检查、连接排障 |
| [`ffmpeg-agent-media`](skills/ffmpeg-agent-media/SKILL.md) | 上传、处理、恢复任务、下载与结果校验 |

**安装并连接：** 将下面的话复制给你的 Agent，把地址替换为从 Agent 实际执行环境可访问的服务地址：

```text
请从 https://github.com/breakstring/ffmpeg-agent/tree/main/skills 分别安装 ffmpeg-agent-connect 和 ffmpeg-agent-media 技能。
请按当前宿主的技能安装方式安装两个完整目录，保留 scripts、references 和 agents 元数据。
然后使用 ffmpeg-agent-connect 连接 https://media.example.com。复用我的私有消费者配置；尚未配置时，请指导我在本机私有文件中设置服务地址和 Token，不要让我把 Token 粘贴到对话中。
检查可用协议和媒体能力，优先复用宿主已有的原生远程 MCP，本地文件通过 HTTP 传输。用我选定的小文件完成上传、媒体处理和下载校验。
```

手工安装见[完整命令和宿主目录表](docs/usage/consumer-skills.zh-CN.md#安装完整技能)。需要安装整个技能目录，不能只复制 `SKILL.md`；安装后按宿主要求刷新技能或开启新会话。

**开始处理：** 连接成功后，把文件和目标交给 Agent，例如：

```text
请使用 ffmpeg-agent-media，把这段视频转成宽度不超过 1280 的 H.264 MP4，保留源帧时间节奏。保存到新的输出目录，完成下载校验，并保留任务状态，等待超时后可以继续恢复。
```

结构化 HTTP/MCP 操作无需服务端模型，当前 Agent 可以直接将需求转成支持的参数；服务端自然语言规划和 A2A 是可选功能。原生 MCP 工具负责提交和查询任务，**本地文件字节仍通过 HTTP 上传下载**。安装技能本身不会自动注册宿主原生 MCP。私有配置、原生 MCP 衔接和可运行示例见[消费者技能使用指南](docs/usage/consumer-skills.zh-CN.md)。

## 使用 Docker Compose 启动

宿主机需要 Git、Docker Compose v2 和 Python 3.10+（用于配置与客户端脚本）。Python 3.14、FFmpeg 和媒体依赖运行在镜像内部，无需在宿主机另外安装。

```sh
git clone https://github.com/breakstring/ffmpeg-agent.git
cd ffmpeg-agent
```

以下部署命令均在此仓库根目录运行。

### 首次生成配置

首次部署时，先生成配置：

```sh
python3 deploy/generate-config.py
```

脚本准备 `.env` 和 `deploy/local/media-config.json`，不会启动容器。随机服务 Token 只写入权限为 `0600` 的 `.env`；保持 `deploy/local` 目录权限为 `0700`。

### 从源码构建镜像

按需编辑 `.env` 中的服务地址、模型配置和功能开关，再构建并启动：

```sh
docker compose up -d --build --wait
```

Compose 构建本地镜像 `ffmpeg-agent:v1-local`，启动单个服务实例并等待就绪。也可以分开构建和启动：

```sh
docker build -t ffmpeg-agent:v1-local .
docker compose up -d --no-build --wait
```

以上命令使用 `.env` 中默认的 `MEDIA_IMAGE`；如果已修改，请让构建和启动使用同一镜像名。容器以非 root 用户运行、根文件系统只读，Compose 提供持久化状态、工作和输出卷。不要扩容为多个服务实例，也不要让多个实例共用 SQLite 状态。

检查容器状态和带认证的服务连接，然后运行[上传 → 处理 → 下载示例](#上传处理和下载)：

```sh
docker compose ps
python3 skills/ffmpeg-agent-connect/scripts/media_client.py --env-file .env check --protocol all
```

默认地址为宿主机上的 `http://localhost:8080`，未主动开启时 A2A 显示未启用是正常的。连接检查不能代替实际媒体处理验证。下面的客户端示例自动读取 `.env`，无需将 Token 复制到命令中。

### 使用 GHCR 预构建镜像

已配置的镜像仓库为 `ghcr.io/breakstring/ffmpeg-agent`，首次成功发布后提供 Linux AMD64 和 ARM64 镜像。Package 可见性与代码仓库独立，设为 Public 后才能匿名拉取；详见[镜像发布与可见性设置](deploy/README.md#ghcr-镜像发布)。

此方式与上面的本地构建二选一。先完成首次生成配置，再将 `.env` 中的非敏感镜像配置改为：

```dotenv
MEDIA_IMAGE=ghcr.io/breakstring/ffmpeg-agent:latest
```

镜像发布完成后，拉取并直接启动：

```sh
docker compose pull media
docker compose up -d --no-build --wait media
```

更新镜像时，先等待活动任务结束并备份状态，再执行同样的两条命令。使用 GHCR 镜像时保留 `--no-build`；`--build` 用于自己的本地源码镜像。

| 镜像引用 | 含义 |
| --- | --- |
| `:main` / `:latest` | `main` 最近一次成功发布的镜像，标签会移动 |
| `:sha-<完整40位提交ID>` | 关联指定源码提交的镜像 |
| `:X.Y.Z` | 对应 `vX.Y.Z` Git 标签发布的版本，需等该版本实际发布 |
| `@sha256:<digest>` | 不可变的清单引用，适合需要可复现性的部署 |

发布流程先验证两个架构，再发布已经测试的镜像。此表说明标签规则，不表示某个版本已经存在；请从[镜像包页面](https://github.com/breakstring/ffmpeg-agent/pkgs/container/ffmpeg-agent)选择实际已发布的标签或 digest。

### 日常启动

配置已初始化且镜像已构建或拉取时，直接运行：

```sh
docker compose up -d --no-build --wait
```

**日常启动无需再次运行 `generate-config.py`。** 本地源码镜像更新代码、依赖或 Dockerfile 后，使用 `docker compose up -d --build --wait` 重新构建；仓库镜像按上面的拉取/启动命令更新。修改 `.env` 后的生效命令见下文。

### 配置与旧版本迁移

服务级 Bearer Token、模型 Key、模型参数和功能开关统一在 `.env` 中配置，Docker Compose 会显式传给服务。结构 JSON 只保留存储、来源规则和服务限额等设置，不再接受 `service_token`、模型凭据或功能开关。只有旧部署仍将这些字段保存在 JSON 中时，才需要在升级启动前运行生成器进行迁移。生成器可重复执行，会保留已有有效 Token 和明确配置，补齐缺项并迁移旧字段；它不是每次启动的必需步骤。进程环境变量优先于 `.env`，空 Token 会被拒绝。凭据不能进入 Git、命令参数或日志；不要 `source .env`、打印展开后的 Compose 配置或分享容器环境检查输出。

| `.env` 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MEDIA_SERVICE_TOKEN` | 自动随机生成 | HTTP、MCP、A2A、上传和下载共用的 Bearer Token；显式 `null` 开放服务 |
| `MEDIA_LLM_ENABLED` / `MEDIA_A2A_ENABLED` | `false` / `false` | 开启自然语言规划和可选 A2A |
| `MEDIA_LLM_API_KEY` / `MEDIA_LLM_MODEL` / `MEDIA_LLM_BASE_URL` | 空 | 供应商 Key、模型标识与 OpenAI-compatible API 地址 |
| `MEDIA_PUBLIC_BASE_URL` | `http://localhost:8080` | 客户端可达地址，也用于 MCP Host/Origin 校验 |
| `MEDIA_IMAGE` | `ffmpeg-agent:v1-local` | 本地镜像名或已发布的仓库镜像引用 |
| `MEDIA_PLANNING_TIMEOUT_SECONDS` | `300` | 自然语言规划预算；媒体执行另用 JSON `task_timeout_seconds`（默认 `3600`） |
| `MEDIA_BIND_ADDRESS` / `MEDIA_PORT` | `127.0.0.1` / `8080` | 宿主机公开地址和端口 |
| `MEDIA_CONFIG_PATH` | `./deploy/local/media-config.json` | 结构服务 JSON 路径，相对于仓库根目录 |
| `MEDIA_CPUS` / `MEDIA_MEMORY` / `MEDIA_PIDS_LIMIT` | `2.0` / `2g` / `128` | 容器 CPU、内存和进程数限制 |

Token 至少为 24 个无空格的可打印 ASCII 字符；缺失或空值均阻止启动，只有 `MEDIA_SERVICE_TOKEN=null` 才选择开放模式。自然语言和 A2A 的具体设置见[启用方法](docs/usage/agent.zh-CN.md#开启规划)。修改 `.env` 后重新创建容器以生效：

```sh
docker compose up -d --force-recreate --wait media
```

修改端口时，同时更新 `.env` 中的 `MEDIA_PUBLIC_BASE_URL`。结构 JSON 的 `worker_concurrency` 设置逻辑任务并发数（1～4，默认 1）。自定义 JSON 通过 `MEDIA_CONFIG_PATH` 选择；示例可用 `--config` 读取其中的地址作为没有环境 URL 和 `--base-url` 时的后备。

默认服务仅在宿主机可访问。远程使用时提供 HTTPS 入口，将 `MEDIA_PUBLIC_BASE_URL` 设为客户端可达地址，并转发正确的公开 Host 头；MCP 按该地址检查 Host 和 Origin。远程示例客户端只需一份包含 `MEDIA_PUBLIC_BASE_URL` 与 `MEDIA_SERVICE_TOKEN` 的私有 `.env`，通过 `--env-file` 指定，无需复制服务端存储或部署 JSON。进程环境变量优先于该文件，`--base-url` 优先指定连接地址；示例客户端绝不从 JSON 读取 Token。

## 上传、处理和下载

仓库提供完整客户端，负责上传本地文件、等待处理，再下载并验证结果。下面的命令使用已有示例素材和新的临时输出目录，可以直接复制，也可以重复运行。上传期间保持源文件不变。

### HTTP：上传时直接提交处理

这个 Python 3 标准库客户端无需安装额外依赖。默认将视频转为 H.264/MP4，保留源尺寸和帧时间节奏：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4"
```

它向 `POST /v1/jobs/upload` 发送一次 multipart 请求，轮询返回的任务，再保存经过校验的 MP4。无需指定编码参数、手动传递 ID 或挂载共享卷。

### HTTP：先上传，再提交处理

加上 `--upload-first`，即可演示先调用 `POST /v1/files`，再调用 `POST /v1/jobs`。客户端还会打印 `file_id`，上传文件有效期间可以让其他任务复用：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4" \
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
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4"
```

客户端通过 HTTP 上传，调用远程 `transcode_video` 工具，通过 MCP 等待任务，再经 HTTP 下载。两种传输读取相同的 `.env` Token，无需配置本地 stdio 服务。

接入其他 MCP 客户端时，使用 `MEDIA_PUBLIC_BASE_URL + /mcp` 的 Streamable HTTP 入口，本机为 `http://localhost:8080/mcp`；在连接设置中配置服务 Bearer 头，文件 HTTP 请求也使用相同的头。**仅有 MCP 不能上传你电脑上的文件路径**：客户端需要具备 HTTP 文件传输能力，或者使用服务可访问的输入 URL／共享文件。

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
| `probe_media` | `video_h264_640x360.mkv` | `report.json`：格式和轨道信息 |
| `transcode_video` | `video_h264_640x360.mkv` | `result.mp4`：H.264、CRF 23、源尺寸和时间节奏 |
| `transcode_audio` | `audio_mp3_mono_22050.mp3` | `result.m4a`：AAC，自动选择兼容采样率／声道／码率 |
| `extract_audio` | `video_h264_640x360.mkv` | `result.m4a`：提取首条音轨并转为 AAC |
| `extract_frames` | `video_h264_640x360.mkv` | `frames/`：首帧 JPEG quality 90，加 `manifest.json` |
| `resize_image` | `image_jpeg_portrait_odd.jpg` | `result.jpg`：原尺寸、JPEG quality 90 |

例如，用一条命令下载抽帧集合：

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/frames" \
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
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4" \
  --params "$output_dir/params.json"
```

同一个参数文件也适用于 MCP 客户端。编写自己的请求时，将这个对象放入 HTTP 步骤的 `params` 或 MCP 工具的 `params`。

### 编码、格式有哪些可选值

下面是服务已经适配的参数值；字符串使用小写。`codec` 是编码方式，`container` 是封装格式，图片则使用 `format`。

| 参数 | 可用值 |
| --- | --- |
| 视频 `codec` | `h264`（默认）、`hevc`、`vp9`、`av1` |
| 视频 `container` | `mp4`、`mkv`、`webm` |
| 音频 `codec` | `aac`（默认）、`mp3`、`opus`、`flac`、`pcm` |
| 音频 `container` | `m4a`、`mp3`、`ogg`、`flac`、`wav`、`mka` |
| 图片 `format` | `jpeg`（默认，不能写成 `jpg`）、`png`、`webp` |
| 视频 `speed` | `fast`、`medium`（默认）、`slow`，控制编码速度与压缩效率 |
| 视频 `resize.fit`／图片 `fit` | `contain`（默认，等比装入）、`cover`（裁剪填满）、`pad`（补边） |
| 视频 `frame_rate.mode` | `source`（默认）、`constant`、`max` |

常见组合是 H.264 + MP4、HEVC + MP4、VP9/AV1 + WebM、AAC + M4A、MP3 + MP3、Opus + Ogg、PCM + WAV。只改 `codec` 就会推导兼容的默认容器；显式填写时必须匹配，例如 H.264 + WebM 会被拒绝。

实际可用范围是“服务已适配 ∩ 运行环境支持”，还受组合和部署限额约束。Docker 使用**容器内**的 FFmpeg/Pillow，宿主机另装编码器不会增加服务能力；当前只使用软件编码。具体部署可通过下方能力接口查询。完整兼容表、非数字参数含义及 10 组可直接使用的 JSON 参数见[媒体参数参考](docs/usage/parameters.zh-CN.md)。

预设是可选快捷设置：`{"preset":"web_720p_v1"}` 选择 720p/30 fps 和双声道 AAC；`aac_stereo_128k_v1` 选择 AAC 128 kbps/48 kHz/双声道；`wav_mono_16k_v1` 选择 16 kHz 单声道 PCM。显式参数覆盖选定预设。未选预设时视频保留源尺寸和时间节奏。音频 `auto` 优先保持源采样率／声道，不兼容时自动调整；`source` 要求严格保持。音频复制模式保留原编码，不能同时调整编码参数或裁剪。

通过 `GET /v1/capabilities` 或 MCP `list_capabilities` 获取可用编码器、schema、默认值和限制。任务的 `steps[].effective_params` 展示接受的设置，产物的 `media.effective_params` 展示解析后的输出设置。编码、尺寸、帧率、码率、采样率和质量的完整控制见[参数合同](docs/design/media-parameters.md)。

### 等待、重试和下载

客户端先打印请求 ID，再打印已接受的 `job_id`。默认每次执行都会创建新请求；只有重试完全相同的请求时才显式复用 `--request-id`，包括源文件、操作、参数及上传方式。改变其中任何一项时使用新 ID。

默认等待 600 秒，可用 `--wait-seconds` 调整。等待超时不会取消后台处理；在相同客户端命令后加 `--job-id` 和已打印的 ID，即可跳过上传与提交，继续等待和下载。恢复时仍需填写 `SOURCE DESTINATION`，但不会读取源文件。`--help` 列出完整选项；`--env-file` 指定私有环境文件（默认仓库根目录 `.env`），`--base-url` 覆盖连接地址；`--config` 只补充后备 `public_base_url`，不读取 Token。

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

`docker compose stop` 会保留卷。备份或升级前停止新提交、等待任务完成，再停止服务。将 **state、work、output、私有 `.env`、结构 JSON 和镜像版本一起备份**；共享输入由应用负责备份。日常停止或清理不要使用 `docker compose down -v`。

每个服务实例独占自己的状态和工作卷。默认一个逻辑 Worker，可配置为 1～4 个；增加前按实际负载和资源测试。不要让多个容器同时访问同一状态目录。

当前处理范围不包含 HDR、10 bit 视频、硬件加速、非方形 SAR、动画或多页图片、HLS 处理及任意 FFmpeg 命令。支持的媒体语义和错误边界见 [API 合同](docs/api/v1-contract.md)。

- [部署与共享卷示例](deploy/README.md)
- [HTTP API 合同](docs/api/v1-contract.md)和 [OpenAPI](docs/api/v1-openapi.json)
- [实现与验证状态](docs/plans/v1-implementation-progress.md)
