# ffmpeg-agent 项目约定

适用全仓，确认日期：2026-10-15。

- 实施合同是 `docs/design/media-service-v1.md`、`docs/design/technology-selection-v1.md` 和 `docs/plans/v1-implementation-plan.md`。按 P0 → P1 → P2 → P3 → P4/P5 → P6 → P7 → P8 → P9 → P10/P11 → P12 推进；保留 P0～P7 既有证据，P8～P12 补齐两种 HTTP 上传、统一可选 Token 和远程 MCP。中间里程碑不代表首版完成。
- 执行跟踪采用 cfKanban KZ / ffmpeg-agent，机器关联在 `.cfkanban-scope.json`；沿用该工具，不接入 Linear。阶段证据和未验证条件记录于 `docs/plans/v1-implementation-progress.md`，不另建 Markdown backlog 或历史任务库。项目治理、执行工具同步和合同调整按项目管理治理技能处理；日常编码直接推进。
- Python 3.14、FastAPI、单进程、单私有 SQLite/aiosqlite 连接；逻辑 Worker 默认 1、允许 1～4。禁止 Uvicorn 多进程/reload、独立数据库、消息队列、分布式 Worker。
- 公共模型、数据库与状态转换由主代理统一负责；并行工作必须划定文件所有权。契约变更先核对所有调用方；不要静默改动版本化预设。
- 参数化增量合同为 `docs/design/media-parameters.md`，按 P13～P17 推进。新项目统一六个 `.v1` 操作，HTTP/MCP 复用同一模型，params 可省略；通用默认优先保留源特征，具名预设作为可选捷径，公开有效参数；10-bit/HDR、硬件加速保留后续 backlog。
- 自然语言与 A2A 增量合同为 `docs/design/media-agent.md`，按 P18～P21 推进。PydanticAI 仅生成受限计划，官方 A2A SDK 复用原任务/文件服务；模型默认关闭，单库持久化计划与媒体关联，不能用模型规划完成或收到取消请求代替媒体完成/停止。P22 统一以 `.env` 配置服务 Token、模型与功能开关，JSON 只保存结构配置；凭据严禁提交及泄漏。
- 消费者技能增量合同为 `docs/design/consumer-skills.md`（P23）。connect/media 两技能独立分发、共用 `consumer/ffmpeg_agent_client/` 维护源，通过 `scripts/sync-consumer-skills.py` 同步；消费者仅需 Python 3.10+ 标准库、服务地址与 Token，不依赖服务端源码或模型 Key。交付须验证生成副本一致和独立消费者真实闭环。
- 所有数据库访问通过统一事务入口，取消后确认回滚完成；事务内不做媒体、文件或网络 I/O。
- 取消以进程组停止/reap、文件工作完成和句柄关闭为准。执行租约到期不证明进程停止。发布检查点、清单与产物绑定不可变。
- 接受和引用资格使用事务开始后的当前时间。恢复时已完整提交的步骤直接收尾，不消耗新的执行尝试；尚未发布结果跨过总时限只能撤销，不能导致正常恢复永久不就绪。
- 首版必须交付远程 Streamable HTTP MCP，不提供 stdio 桥接；MCP 复用同进程任务/文件/产物服务，文件字节通过 HTTP 上传下载。`MEDIA_SERVICE_TOKEN` 为 Token 时所有入口统一校验 Bearer，显式小写 `null` 时开放；部署缺失或空值拒绝启动，旧 JSON 先用生成器迁移。直接构造 Settings 保留旧调用方凭据兼容，不增加用户/角色/OAuth 系统。
- HTTP 托管上传位于私有 `work_dir/_uploads`，与任务目录分开；先登记所有权，完整上传后才能引用。任务接受原子取得 `file_id` 引用，覆盖排队、执行和重试；到期/release 只阻止新引用，已有任务继续。
- 共享输入只读借用。安全目录 FD 打开并把验证过的文件描述符传给媒体引擎；清理只能针对数据库登记且满足回收条件的服务所有文件。HTTP 发送、内部引用和显式租约必须保护文件直到实际停止使用。
- 测试只用本项目生成的 fixture 和独立临时状态/工作/输出目录。六个基础媒体由 `scripts/generate_fixtures.py` 在固定镜像中生成，生成方法、媒体属性和 SHA-256 见 `tests/fixtures/`；不依赖外部项目素材或软链接。边界媒体按需生成，历史验收仍绑定当时素材，不能以新素材替换旧报告中的身份。真实媒体、容器强停、并发、两宿主机验收分别记录，不能互相替代。
- 使用 `uv run ruff check .`、`uv run pyright`、`uv run pytest` 及对应真实集成脚本；依赖与运行基线固定在锁文件和 Dockerfile。只把有实际证据的验收勾为完成。
- 正式验收固定精确镜像 ID；容器内测试用 `MEDIA_TEST_ENGINE_SERIES=7.1`。故障测试入口只放 `tests/container/`，不进入生产镜像。日志只记录已批准的语义字段，不能输出原始外部异常正文。
- 凭据、签名 URL、认证头和敏感路径不进入日志、公开查询、测试报告或提交。部署配置与生成数据不提交。
- 已授权本地实现、测试、隔离容器验收、Git 初始化及当前实现与文档的本地提交；未另行授权不得 push、merge、tag、生产部署或破坏性操作。双宿主机验收保留待办，后续具备环境后补验。交付前核对文件与证据，不把未验证内容写为完成。
