# 自然语言媒体任务

[English](agent.md) · [README](../../README.zh-CN.md) · [设计合同](../design/media-agent.md)

描述期望结果并提供具名输入，PydanticAI 会使用现有六种媒体操作生成受校验的计划，再由原媒体服务执行。自然语言 HTTP/MCP 和可选 A2A 复用持久化任务、文件服务、取消与认证。

## 开启规划

自然语言规划与 A2A 默认关闭。在私有服务 JSON 中加入以下字段，使用自己的供应商配置：

```json
{
  "llm_enabled": true,
  "llm_api_key": "<模型服务密钥>",
  "llm_model": "<模型标识>",
  "llm_base_url": "https://<供应商地址>/compatible-mode/v1",
  "a2a_enabled": false
}
```

这只是新增字段，不能替代完整服务配置。配置文件和凭据不能加入 Git。供应商需支持配置对应的 OpenAI-compatible API。`llm_output_mode` 默认 `prompted`；选用 `native` 需要供应商支持原生结构化输出。服务不会注入供应商专属 thinking 参数。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `planning_concurrency` | `2` | 并发模型请求数，允许 1～4 |
| `max_agent_requests` | `32` | 全局未完成规划请求上限 |
| `planning_timeout_seconds` | `60` | 包含受控纠错的规划总预算，单位秒 |
| `planning_max_output_tokens` | `4096` | 模型输出 token 上限 |
| `a2a_enabled` | `false` | 启用 A2A，要求规划配置完整 |

显式本地使用时，设置 `MEDIA_ENV_FILE=.env.local` 才会让启动器读取该文件。支持已有的 `llm_api_key`、`llm_default_model`、`llm_base_url` 命名及 `MEDIA_LLM_*` 设置；服务不会自动搜索当前目录的 `.env.local`。Docker 部署使用私有 JSON 配置，本地凭据文件不复制进镜像。占位值见 [`.env.example`](../../.env.example)。不要用 shell `source` 加载凭据文件，也不要将密钥放到命令参数中。

使用本地文件时，需将 `MEDIA_LLM_ENABLED` 设为 `true`；需要 A2A 时再将 `MEDIA_A2A_ENABLED` 设为 `true`。模板中的两个开关均为关闭，三个兼容别名只配置模型。直接运行 Python 服务还需用 `MEDIA_CONFIG_FILE` 指向完整服务 JSON，其中目录、运行模式和引擎版本应匹配本机环境。

使用仓库的 Compose 配置时，应修改 `MEDIA_CONFIG_PATH` 指向的私有 JSON，默认是 `deploy/local/media-config.json`。Compose 仅用 `.env` 替换自身配置中的变量，不会向容器传递 `MEDIA_LLM_*`、`MEDIA_A2A_ENABLED` 或 `MEDIA_ENV_FILE`。修改私有 JSON 后，重新创建服务容器以读取新配置：

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

在完整规划配置之外设置 `a2a_enabled: true`。官方 Python A2A SDK 提供 `/a2a` JSON-RPC 入口和 `/.well-known/agent-card.json` 能力卡。包括能力卡在内的入口均沿用 HTTP/MCP 的可选 Bearer 认证。首期支持 A2A 1.0；旧 0.3 客户端需要单独验证兼容性。

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

`SendMessage` 在 `result.task` 返回 Task。使用其 `id` 调用 `GetTask` 或 `CancelTask`，参数为 `"params": {"id": "<agent_task_id>"}`；这两个方法直接在 `result` 返回 Task。使用官方 SDK 的完整客户端见 [`examples/a2a-workflow.py`](../../examples/a2a-workflow.py)。

A2A JSON-RPC 的 `status.state` 在排队、规划、运行、取消中为 `TASK_STATE_WORKING`，成功为 `TASK_STATE_COMPLETED`，部分成功或失败为 `TASK_STATE_FAILED`，实际取消终态为 `TASK_STATE_CANCELED`。`needs_input` 和 `unsupported` 映射为 `TASK_STATE_REJECTED`，需要补全请求后使用新 message ID 提交。部分成功仍保留成功产物引用。产物 URL 不带 Token，下载时仍需同一服务凭据。

首期不支持 task/context 续写、流式响应、push notification、任意 FFmpeg 命令、字幕、拼接、HDR 或硬件加速。完整公共边界见 [API 合同](../api/v1-contract.md)，已有验证与未验证条件见[实施进度](../plans/v1-implementation-progress.md)。
