# 自然语言与 A2A 增量合同

状态：Frozen。确认日期：2026-10-16。执行关联：P18～P21 / CFK-808～CFK-811。

本合同扩展 [媒体服务](media-service-v1.md) 与 [媒体参数](media-parameters.md)，不修改六个 `.v1` 操作的语义。实施步骤沿用 [现有实施计划](../plans/v1-implementation-plan.md)，结果证据写入 [现有进度记录](../plans/v1-implementation-progress.md)，动态执行状态以 cfKanban KZ / ffmpeg-agent 为准。

## 目标与范围

在原 HTTP/MCP 结构化接口之上增加自然语言任务入口。PydanticAI 只生成受约束媒体计划；官方 Python A2A SDK 只负责协议适配。媒体执行、文件引用、发布、取消和恢复仍归原 Service、SQLite 与 Worker 管理。同一进程、单个私有 SQLite/aiosqlite 连接；不引入独立数据库、消息队列、AgentOS 或第二套媒体执行器。

首期支持单次描述生成并提交任务、查询、取消及 HTTP 下载。计划结论区分可执行、需补充信息和当前不支持。需补充信息的请求结束本次规划，调用方使用新的幂等键提交完整描述；不承诺多轮上下文续接、SSE、push notification、任意 shell/filtergraph、字幕、拼接、HDR 或硬件加速。规划不读取媒体字节，也不自动执行 probe；动态探测驱动的二次决策另行设计。

## 依赖与配置

- 使用 `pydantic-ai-slim[openai]==2.56.0` 和 `a2a-sdk[http-server]==1.2.2`，具体传递依赖锁在 `uv.lock`。Python 3.14 与原固定 FastAPI/Pydantic/MCP 组合须实际解析、导入和协议验证。
- 模型默认关闭；A2A 独立开关，启用 A2A 要求自然语言规划配置完整。关闭模型时原结构化 HTTP/MCP 功能保持可用。
- 使用可配置 OpenAI-compatible base URL/model/API key；key 为 `SecretStr`。不硬编码供应商的 thinking 参数。只开启有界模型输出与显式请求预算，不启用 trace、原始 HTTP/body 日志或 SDK 隐式 HTTP 重试。
- 根目录 `.env` 统一保存服务 Token、模型 Key/model/base URL、LLM/A2A 开关和规划预算；结构 JSON 不接受这些字段。服务默认读取当前目录 `.env`，可用 `MEDIA_ENV_FILE` 显式选文件；不自动读取 `.env.local`。Compose 从 `.env` 按白名单传入环境，进程 `MEDIA_*` 优先于文件；修改后必须重新创建容器。`.env` 权限为 0600，不能提交或复制进镜像，不公开展开的 Compose 配置、inspect 或凭据值。显式本地验收仍兼容 `llm_provider/llm_default_model/llm_api_key/llm_base_url`，解析不执行 shell、不展开变量。旧 JSON 通过 `deploy/generate-config.py` 原子迁移，先保存 `.env` 再移除重复字段；已有 `.env` 明确值优先。
- 规划并发默认 2（1～4），全局未完成请求上限默认 32，规划总期限默认 60 秒，输出 token 上限默认 4096。模型网络 I/O 始终在数据库事务外，与媒体 Worker 槽分开计数。

## 公共请求与计划

HTTP 新增 `POST /v1/agent/tasks`，使用既有 `Idempotency-Key`；`GET /v1/agent/tasks/{id}` 和 `POST /v1/agent/tasks/{id}/cancel` 查询/取消。MCP 在模型启用时增加 `generate_media/get_agent_task/cancel_agent_task`，与 HTTP 复用模型与服务。提交后返回持久化的 agent task ID，明确这是规划请求接受，不能声称媒体执行已完成。

请求包含 `intent`（1～4000 字符）、1～4 个具名 `inputs`（复用四种现有引用）及可选 `output_policy`。模型仅收到描述、输入别名、现有操作的 schema/defaults 与运行时可用能力；不收到真实文件路径、file_id、签名 URL、源认证头或模型凭据。用户文本与模型文本均不作为代码或权限来源。

`MediaPlan` 只含现有操作构成的 `steps` 和命名 `outputs`，不允许生成新的来源、存储策略或执行命令。模型输出经过 Pydantic 校验后，服务端绑定请求输入，构建 `JobCreate`，执行现有图/参数/运行时能力校验。输出策略及默认存储在规划请求接受时冻结。非法计划只允许在同次规划预算内进行一次受控纠错；耗尽预算返回稳定错误。

公开结果包括 agent task ID、状态、计划结论、有限说明、已验证计划、关联媒体 job ID 和安全 job 结果。说明由服务按结论生成固定模板，不持久化或回传模型自由文本，避免复述原始描述中的敏感信息；模板不承诺逐项指出缺失信息。不得回传原始 intent、输入来源或 provider 原始响应/异常。能力发现只说明启用状态、限制和协议，不公开密钥或 provider 地址。

## 持久化、恢复与取消

schema 2→3 增加短期 `agent_requests` 表，保存 caller/幂等键/摘要、私有原始请求、状态与期限、不可变有效计划、关联 job、脱敏错误和终态时间。公共媒体 job 状态机不变。所有新增访问经同一个 `Database.run` 入口；迁移覆盖新库及 schema 1/2。

内部状态为 `queued → planning → planned → submitting → submitted`，submitting 仅用于串行领取已保存计划，对外仍为 planned；未提交的终态为 `needs_input/unsupported/failed/canceled`。取消在途模型先进入 `canceling`，等待本地请求任务实际停止才成为 canceled。submitted 的公开状态由原媒体 job 派生，包括 queued/running/canceling/succeeded/partial/failed/canceled，不能把 partial 隐藏为完全成功。

同 caller 同幂等键同摘要返回同请求；不同摘要返回 409。规划领取先持久化 planning，再发网络请求。重启时：queued 继续首次规划；planning 且无已保存计划终结为 `PLANNING_INTERRUPTED`，不自动重问模型；planned/submitting 直接提交已保存计划；submitted 读取原 job。远端模型调用与 SQLite 不可能跨系统原子提交，因此不承诺远端恰好一次计费。

媒体接受使用稳定内部幂等键。在现有接受事务中复查规划状态、caller、取消意图和当时期限，创建/读回媒体 job 并原子关联 agent request。取消与接受必须序列化：取消先提交则不能创建 job；接受先提交则取消同一个 job。数据库事务内不执行模型、磁盘或媒体 I/O。

规划期间不取得媒体文件引用。上传文件/已有产物必须在正式媒体接受事务中仍具引用资格；期间过期或 release 就明确失败，不续期或复活。任务接受成功后继续沿用现有引用保护。模型只看到别名，规划等待不代表文件保留承诺。

正常关闭先停止规划领取并等待模型/提交操作收束，再关闭媒体 Worker 和数据库。规划 worker 意外失败纳入 readiness。提交成功或未提交终态及时清除原始描述与输入绑定；保留安全计划及至少既有幂等期限的短期记录。GC 先清理到期规划记录，关联记录存在时保护 job 元数据免被删除；不因此延长产物字节保留期。

## A2A 协议

使用官方 SDK 的 JSON-RPC routes，提供 `/a2a` 和 `/.well-known/agent-card.json`。首期协议为 A2A 1.0；不以 SDK 的 0.3 兼容能力代替互操作验收，也不声称能直接运行参考项目的旧客户端。Card 关闭 streaming/push；只声明实际支持的输入/输出与自然语言媒体能力。

支持发送消息、查询任务和取消；消息使用一个包含公共请求的数据 part，或 text intent + 数据 part 的 inputs/output_policy。`messageId` 映射稳定幂等键。首期拒绝 task/context 续写与引用未提供的文件；不接受内嵌文件 bytes。对外 Task 从持久化 agent request 与媒体 job 投影，自定义 SDK request handler，避免默认内存任务或默认 cancel 把已请求取消误报为已停止。

A2A 状态：规划/排队/运行/取消中映射 working；成功映射 completed；partial 映射 failed 并保留成功产物与明确部分失败说明；失败映射 failed；实际取消终态映射 canceled；需补充信息和不支持映射 rejected 并说明需新消息提交，避免承诺本轮没有实现的 input-required 续写。产物返回 HTTP 文件引用与安全元数据，大文件不进入 base64 结果。

所有新 HTTP/A2A/Card 路径沿用统一可选 Bearer Token；`.env` 的 `MEDIA_SERVICE_TOKEN` 显式小写 `null` 开放，Token 统一校验，部署缺失或空值拒绝启动。仅直接构造 Settings 保留旧 callers 兼容。caller 从可信中间件传入，不由模型或请求 metadata 指定。下载 URL 不包含 Token；同一调用方凭据继续用于下载。SDK 原始 body/debug/异常日志必须关闭或安全过滤。

## 验证与交付

1. 定向单测覆盖计划校验、默认与参数绑定、需澄清/不支持、超时、并发、幂等、caller 隔离、外部错误脱敏及普通测试无模型网络请求。
2. 覆盖规划开始/返回/保存、媒体接受前后、取消竞争、重启恢复、文件到期/release、GC、迁移和敏感字段清除；保存计划后恢复不再请求模型。
3. 官方 A2A 客户端及真实 TCP HTTP/MCP 覆盖发现、提交、查询、取消、文件上传下载和鉴权；分别验证 submitted 与最终媒体结果，不用协议接受代替内容正确。
4. 用户授权的百炼真实测试仅显式运行，使用本仓库合成 fixture 和独立状态/输出目录；报告只记模型标识、状态、延迟、操作和媒体属性/哈希，不记凭据、prompt 原文或原始异常。
5. `uv run ruff check .`、`uv run pyright`、`uv run pytest` 与精确镜像隔离验收；容器内 `MEDIA_TEST_ENGINE_SERIES=7.1`。故障入口仅放 `tests/container/`。既有双宿主机与原生 amd64 未验证条件保持独立。

参考：[PydanticAI 输出](https://pydantic.dev/docs/ai/core-concepts/output/)、[官方 A2A Python SDK](https://github.com/a2aproject/a2a-python)、[A2A 1.0](https://a2a-protocol.org/v1.0.0/specification/)。具体 API 以锁定依赖的实际源码和测试为准。
