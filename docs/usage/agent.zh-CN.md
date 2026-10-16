# 自然语言媒体任务

[English](agent.md) · [README](../../README.zh-CN.md) · [设计合同](../design/media-agent.md)

描述期望结果并提供具名输入，PydanticAI 会使用现有六种媒体操作生成受校验的计划，再由原媒体服务执行。自然语言 HTTP/MCP 和可选 A2A 复用持久化任务、文件服务、取消与认证。

## 开启规划

自然语言规划与 A2A 默认关闭。先运行 `python3 deploy/generate-config.py` 创建或迁移 `.env` 和结构服务 JSON，再编辑 `.env` 中已有的配置项，填写供应商参数：

```dotenv
MEDIA_LLM_ENABLED=true
MEDIA_LLM_API_KEY='<模型服务密钥>'
MEDIA_LLM_MODEL='<模型标识>'
MEDIA_LLM_BASE_URL=https://<供应商地址>/compatible-mode/v1
MEDIA_PLANNING_TIMEOUT_SECONDS=300
MEDIA_A2A_ENABLED=false
```

服务级 Bearer Token 也只在 `.env` 中通过 `MEDIA_SERVICE_TOKEN` 配置，与模型 API Key 分开。生成器会创建随机 Token，只有显式 `MEDIA_SERVICE_TOKEN=null` 才选择服务开放模式。保持 `.env` 权限为 `0600`，不加入 Git。值中包含 `$` 时使用单引号，避免 Compose 插值；不要 `source` 凭据文件、将密钥放入命令参数、打印展开后的 Compose 配置或分享容器环境输出。

供应商需支持配置对应的 OpenAI-compatible API，服务不会注入供应商专属 thinking 参数。规划相关配置统一使用同一份 `.env`：

| `.env` 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `MEDIA_LLM_OUTPUT_MODE` | `prompted` | `native` 要求供应商支持原生结构化输出 |
| `MEDIA_PLANNING_CONCURRENCY` | `2` | 并发模型请求数，允许 1～4 |
| `MEDIA_MAX_AGENT_REQUESTS` | `32` | 全局未完成规划请求上限 |
| `MEDIA_PLANNING_TIMEOUT_SECONDS` | `300` | 规划总预算，单位秒，允许大于 0 且不超过 300 |
| `MEDIA_PLANNING_MAX_OUTPUT_TOKENS` | `4096` | 模型输出 token 上限 |
| `MEDIA_A2A_ENABLED` | `false` | 启用 A2A，要求规划配置完整 |

规划预算包括规划排队、模型请求、受控纠错、计划校验和媒体任务提交。转码、抽帧等媒体执行使用独立的 `task_timeout_seconds`：它配置在结构 JSON 中，默认 `3600` 秒，从媒体任务被接受时开始计时，包含媒体排队、执行和产物发布。长视频处理需要调整这个媒体任务预算，不消耗模型规划预算。

消费者技能 CLI 的 `--wait-seconds` 只限制本次客户端等待；等待超时会保留状态文件，不会取消服务端任务，可用同一状态文件执行 `resume` 继续查询和下载。用法见[消费者技能](consumer-skills.zh-CN.md)。

Compose 会显式传递这些值。直接运行 Python 服务默认读取 `.env`，必要时可用 `MEDIA_ENV_FILE` 指定其他文件；进程环境变量优先于文件值。加载器只解析内容，不执行 shell 或插值。`MEDIA_CONFIG_FILE` 指定的结构 JSON 仍提供适合本机的目录、存储、来源规则与限额，但会拒绝服务 Token、规划参数和功能开关，避免与 `.env` 静默冲突。已有 JSON 中的这些字段由生成器迁移。`.env.local` 不会自动发现，也不会复制进镜像。

升级不会覆盖 `.env` 中已有的值；若仍为 `MEDIA_PLANNING_TIMEOUT_SECONDS=60`，请编辑为 `300`。修改 `.env` 或结构 JSON 后，先确认没有活动规划或媒体任务，再重新创建服务容器以读取新配置：

```sh
docker compose up -d --force-recreate --wait media
```

`GET /v1/capabilities` 和 MCP `list_capabilities` 的 `agent` 字段公开启用的协议与请求限制，不公开密钥或供应商地址。关闭规划后，原结构化 HTTP/MCP 保持可用，已有自然语言任务仍可通过 HTTP 查询和取消。

## 通过 HTTP 提交

先通过 `POST /v1/files` 上传字节，或使用已有的共享文件、HTTP 文件、产物引用。然后向 `POST /v1/agent/tasks` 提交以下 JSON，并携带稳定的 `Idempotency-Key` 头和与其他接口相同的 Bearer 凭据：

```json
{
  "intent": "把 source 转成宽度 1280 的 MP4，保持比例；另导出 MP3，再取第 3 秒的 JPEG 封面。",
  "inputs": {
    "source": {"kind": "uploaded_file", "file_id": "<file_id>"}
  }
}
```

`intent` 长度为 1～4000 字符，`inputs` 为 1～4 个具名引用。`output_policy` 可省略，沿用结构化任务的存储和保留设置。模型只收到描述、输入别名和受支持操作，不收到真实文件引用、来源认证或文件字节。描述应提供全部必要要求；规划器不会在规划前自动探测媒体。

首次接受返回 HTTP `202`、`agent_task_id` 和 `Location` 头。同一规范化请求、同一幂等键重放返回 HTTP `200` 和当前结果；同键不同请求返回 `409 IDEMPOTENCY_CONFLICT`。这是异步接受，不表示媒体已经处理完成。

| 请求 | 结果 |
| --- | --- |
| `GET /v1/agent/tasks/{agent_task_id}` | 规划状态、有效计划，以及已关联的媒体任务 |
| `POST /v1/agent/tasks/{agent_task_id}/cancel` | 请求取消；状态为 `canceling` 时返回 `202`，其他状态返回 `200` |
| `GET /v1/artifacts/{artifact_id}` | 产物元数据和 HTTP 下载地址 |

持续查询至终态。`queued`、`planning`、`planned` 表示规划或提交过程；关联媒体任务后，`queued`、`running`、`canceling` 反映媒体状态。成功结果包含 `decision: "execute"`、`plan`、`job_id` 和 `job`。从 `job.outputs` 取得产物 ID，再通过原产物下载接口携带相同凭据下载。

| 终态 | 调用方处理方式 |
| --- | --- |
| `succeeded` | 声明输出已完成，下载对应产物 |
| `partial` | 检查 `job` 中成功产物与失败输出或成员 |
| `needs_input` | 补全目标格式、处理约束等必要信息后，用新幂等键提交 |
| `unsupported` | 请求超出已支持范围，改为受支持任务后用新键提交 |
| `failed` | 检查稳定 `error`，以及存在时的关联媒体任务 |
| `canceled` | 本地规划或媒体工作已停止；不能撤销远端模型已产生的计费 |

公开 `message` 按规划结论使用固定模板，不返回模型自由文本，也不逐项指出本次请求缺少哪些信息。遇到 `needs_input` 时，应检查原描述，补充选择受支持操作及参数所必需的信息。

请求取消不等于已经停止，`canceling` 期间继续查询。规划本身不会取得上传文件或产物引用；正式媒体接受时输入仍须具备引用资格。在规划期间主动 release 或到期，可能导致媒体提交失败。

## 通过 MCP 提交

启用规划后，`/mcp` 在原 19 个工具之上增加三个工具：

| 工具 | 参数 |
| --- | --- |
| `generate_media` | `request` 为 HTTP 请求字段，另传 `idempotency_key` |
| `get_agent_task` | `agent_task_id` |
| `cancel_agent_task` | `agent_task_id` |

`generate_media` 参数示例：

```json
{
  "request": {
    "intent": "探测 audio 并返回媒体属性。",
    "inputs": {"audio": {"kind": "uploaded_file", "file_id": "<file_id>"}}
  },
  "idempotency_key": "inspect-audio-001"
}
```

三个工具均返回与 HTTP 一致的 `AgentTaskResult`。`generate_media` 会调用外部模型，因此工具标注 `openWorldHint: true`。HTTP 与 MCP 共享同调用方的幂等作用域，相同请求和键可以跨协议重试。上传下载仍走 HTTP，不向 MCP 传递 base64 文件内容。

## 可选 A2A

在 `.env` 中填写完整规划配置，并设置 `MEDIA_A2A_ENABLED=true`。官方 Python A2A SDK 提供 `/a2a` JSON-RPC 入口和 `/.well-known/agent-card.json` 能力卡。包括能力卡在内的入口均沿用 HTTP/MCP 的可选 Bearer 认证。首期支持 A2A 1.0；旧 0.3 客户端需要单独验证兼容性。

消息可包含一个 data part，其内容与 `AgentRequest` 一致；也可用 text 提供 intent，并用 data part 提供 `inputs` 和可选 `output_policy`。重试使用相同 message ID。文件使用现有输入引用，不内嵌字节。返回持久化 A2A Task 后继续查询媒体结果；取消后同样需要持续查询至实际停止。

直接使用 JSON-RPC 时，请向 `/a2a` 发送 `A2A-Version: 1.0` 头和以下请求体：

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "inspect-audio-001",
      "role": "ROLE_USER",
      "parts": [{"data": {
        "intent": "探测 audio 并返回媒体属性。",
        "inputs": {"audio": {"kind": "uploaded_file", "file_id": "<file_id>"}}
      }}]
    },
    "configuration": {"returnImmediately": true}
  }
}
```

`SendMessage` 在 `result.task` 返回 Task。使用其 `id` 调用 `GetTask` 或 `CancelTask`，参数为 `"params": {"id": "<agent_task_id>"}`；这两个方法直接在 `result` 返回 Task。使用官方 SDK 的完整客户端见 [`examples/a2a-workflow.py`](../../examples/a2a-workflow.py)。客户端默认读取仓库 `.env`；可用 `--env-file` 指定只包含 `MEDIA_SERVICE_TOKEN` 和 `MEDIA_PUBLIC_BASE_URL` 的私有客户端文件：

```sh
output_dir=$(mktemp -d)
printf '探测 source 并返回媒体属性。\n' > "$output_dir/intent.txt"
uv run --frozen python examples/a2a-workflow.py \
  tests/fixtures/audio_mp3_mono_22050.mp3 "$output_dir/results" \
  --intent-file "$output_dir/intent.txt" --env-file .env
```

`--config` 只用于补充后备服务 URL，不读取其中的 Token。

A2A JSON-RPC 的 `status.state` 在排队、规划、运行、取消中为 `TASK_STATE_WORKING`，成功为 `TASK_STATE_COMPLETED`，部分成功或失败为 `TASK_STATE_FAILED`，实际取消终态为 `TASK_STATE_CANCELED`。`needs_input` 和 `unsupported` 映射为 `TASK_STATE_REJECTED`，需要补全请求后使用新 message ID 提交。部分成功仍保留成功产物引用。产物 URL 不带 Token，下载时仍需同一服务凭据。

首期不支持 task/context 续写、流式响应、push notification、任意 FFmpeg 命令、字幕、拼接、HDR 或硬件加速。完整公共边界见 [API 合同](../api/v1-contract.md)，已有验证与未验证条件见[实施进度](../plans/v1-implementation-progress.md)。
