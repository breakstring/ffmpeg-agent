# 首版实施进度与验收证据

日期：2026-10-13（Asia/Shanghai）。执行项目：cfKanban **KZ / ffmpeg-agent**，关联记录为 `.cfkanban-scope.json`。

P0～P7 原范围及本轮 P8～P12 当前环境可执行的实现和验收已完成。**P7/P12 的真实双宿主机验收按用户要求保留待办，后续具备环境后补验。** 同机容器和单机 HTTP/MCP 结果未计为跨宿主机通过。

## 阶段状态

| 阶段 | cfKanban | 实现状态 | 验证状态 |
| --- | --- | --- | --- |
| P0 契约与运行基线 | CFK-768 | 完成 | 六素材摘要、真实原型、arm64 原生与 amd64 模拟基线通过 |
| P1 控制面 | CFK-769 | 完成 | 认证、严格 schema、事务、幂等、配额、队列与 Worker 1/2/4 通过 |
| P2 文件与执行底座 | CFK-770 | 完成 | 安全 FD、真实进程组停止/reap、文件线程取消和资源边界通过 |
| P3 probe 闭环 | CFK-771 | 完成 | 共享输入→probe→发布→查询、持久重放及整容器重启通过 |
| P4 六操作与组合 | CFK-772 | 完成 | 六操作真实媒体、DAG、独立分支、部分成功与时间/色彩边界通过 |
| P5 生命周期 | CFK-773 | 完成 | 租约/引用/定向 GC、取消、检查点恢复及最终镜像故障矩阵通过 |
| P6 HTTP 通路 | CFK-774 | 完成 | 27项来源、22项内容与2项真实 TCP 测试通过；容器 HTTP 转码消费通过 |
| P7 集成交付 | CFK-775 | 完成代码与交付材料 | 本机与 arm64 容器全部通过；真实双宿主机验收保留待办，待环境补验 |
| P8 合同与单 Token | CFK-781 | 完成 | Token/显式 null 开放/旧配置兼容；各入口统一校验，上传读 body 前鉴权 |
| P9 托管上传 | CFK-782 | 完成 | 20 项管理/并发/取消/引用/回收测试；schema 1→2 迁移；生产上传强停恢复 |
| P10 请求内上传 | CFK-783 | 完成 | 严格绑定、完整边界后接受、输入删除后幂等重放；两 Token 模式真实 TCP probe 下载 |
| P11 远程 MCP | CFK-784 | 完成 | 19 工具；6 项 TCP 测试、2025 握手/2026 发现、协议错误脱敏；仅 Streamable HTTP |
| P12 增量交付 | CFK-785 | 完成当前环境范围 | 本机/容器各 275 项、8 项故障矩阵、生产上传/MCP/下载、两 Compose 模式通过；双宿主机仍待 |

## P8～P12 增量证据

本轮把直接 HTTP 上传、远程 MCP 和一个可选服务 Token 纳入首版必交付。参考项目的工具语义、明确参数和流式接收体验已吸收，继续复用原持久任务、不可变产物和引用回收；backlog 新增 CFK-781～CFK-785。MCP 不包含 stdio 桥接、客户端本地路径或大文件 base64 参数。

- 独立上传返回 `file_id`；请求内 job-first multipart 上传返回异步任务 ID。共用私有 `_uploads`、幂等、配额、总/闲置超时与回收，上传不占媒体 Worker 槽。
- 接受事务按当前时间验证文件资格并取得引用，覆盖排队和重试；实际进程/文件停止后释放。中断文件关闭后回收；完整但未接受的文件按有限 TTL 保留供重试；删除后幂等墓碑满配置窗口再清除。
- 显式 `service_token` 为 Token 时统一 Bearer 校验，为 null 时开放，缺字段继续旧认证。schema 1→2 短事务迁移保留原记录；回退旧镜像须使用升级前一致性备份。
- 官方 MCP SDK 2.1.0 同进程直接复用 Service，19 工具提供严格输入和结构化输出。未知协议方法/非法参数返回固定 -32601/-32602，不回显原输入；工具错误沿用脱敏语义目录。

增量正式验收固定精确镜像：

```text
sha256:9c66f952922fb6d0e126bfcab2e2f4591e041aef4abedbcee480b46b38ac8da4
```

原生 Linux arm64、Python 3.14.8、FFmpeg/ffprobe 7.1.5；镜像内 24 份源码 SHA-256 已与工作区逐项核对一致。旧容量报告和 P0 双架构基线保留原镜像范围，不作为增量 amd64 或新容量证据。

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| `uv run ruff check .` / `uv run pyright` | 退出 0；0 errors / 0 warnings | [增量验证记录](../verification/upload-mcp-validation.json) |
| `uv run pytest -q` | **275 passed in 26.96s**；本机 Python 3.14.7 / FFmpeg 9.0.1 | 同上 |
| 精确镜像完整 pytest | **275 passed in 43.01s**；7.1 引擎、非 root、只读根 | [容器测试](../../deploy/upload-mcp-tests-arm64.json) |
| 生产镜像 HTTP/MCP 闭环 | 两 Token 模式通过；转码/探测、HEAD/Range/SHA/ffprobe；两模式真实 TCP multipart probe | [闭环报告](../../deploy/upload-mcp-container-arm64.json) |
| 上传强停恢复 | 观察登记 uploading 与实际 partial 后 KILL/退出 137；恢复删除半成品，原完整输入仍可复用 | 同上；测试入口仅在 `tests/container/upload_control.py` |
| 既有故障矩阵复验 | **8/8 通过**：执行、发布、取消、热重启及孤儿锁 | [故障矩阵](../../deploy/upload-mcp-faults-arm64.json) |
| 同/跨 Compose 复验 | 两组通过；新 Token 配置、共享输入保持、输出只读、租约保护 | [部署复验](../../deploy/upload-mcp-deployments-arm64.json) |

闭环报告两组随机 owner 的容器/卷残留均为 0；按脚本名称前缀读回，包括调试尝试也无残留。私有配置、Token、原始异常和生成媒体未进入提交；机器合同已重新导出（含 FileResult）。

剩余条件：实际双宿主机的上传/MCP/下载、增量原生 amd64 运行与容量未验证。客户端证据为官方 SDK 的真实 TCP 请求，不声称所有第三方客户端均兼容。CFK-785 保留未完成环境项。

## 使用者交付入口补充

2026-10-13 补充根目录 `docker-compose.yaml` 与 `.env.example`；根入口通过 extends 复用既有服务配置，提供镜像、端口、配置位置及资源设置，Token 仍保存在私有 JSON。旧 deploy 入口和共享卷组合继续可用。

默认英文 `README.md` 与简体中文 `README.zh-CN.md` 顶部互链，面向使用者说明启动、上传、异步处理、远程 MCP、下载与保留/取消；两份代码相同。已有配置不覆盖，改端口须同步公开 URL。

定向验证：根入口、`.env.example`、同 Compose/外部卷组合及旧入口解析通过；Ruff 退出 0，Pyright 0 错误/警告；Python/JSON 示例与本地链接通过。随机独立 Compose 项目使用上述精确镜像实际启动，原样运行 README 标准库上传/转码/租约下载、同键重放、multipart 上传及 MCP 参数示例；长度与 SHA 校验通过，fixture 未改变。只移除本次项目，容器/卷/网络残留均为 0。证据见[Compose 与 README 实跑](../verification/compose-readme-smoke.json)。本轮未修改服务 Python 源码，媒体运行基线沿用前述证据。

## P0～P7 既有交付与关键决定

- 初始仓库只有三份基线文档。按其范围实现，保留单服务容器、单 API 进程、私有 SQLite/aiosqlite、默认1个逻辑 Worker（允许1～4）。无独立数据库、队列或分布式执行器。
- 三类输入、六个版本化操作、有限 DAG、部分成功、不可变发布、取消、恢复、租约、HTTP 发送保护及短期清理均已实现。实际 schema 与预设见 [API 合同](../api/v1-contract.md) 和 [OpenAPI](../api/v1-openapi.json)。
- 接受事务先完成持久化才返回202；同键重放不重新执行，不受新任务磁盘预检影响。资格与过期判定使用事务内当前时间，避免 preflight 或锁等待跨过保留期限后误获新引用。
- 取消先保存意图，确认进程组、文件线程与发送实际停止，再提交终态、输入释放和引用释放。HTTP 引用没有仅按 TTL 强行回收的路径。
- 恢复前以容器代际及单实例锁确认执行者停止；锁 FD 传给受管进程，孤儿 FFmpeg 仍活着时阻止新实例接管。全部步骤已提交时直接收尾，不消耗新尝试；哈希期间过期的未发布结果安全撤销，不阻断启动。
- 固定 HTTP 副本完成校验并持久登记后复用；实际 DNS/连接 IP、逐跳重定向、凭据剥离、超时与整个 work 卷并发预算均受控。敏感来源定义终态后清除，日志不输出 URL、认证头、私有路径或原始外部异常正文。
- 六个基础 fixture 为实际复制、非软链接，8,444,238字节，SHA-256与授权来源一致；原目录未修改。来源与已有合成方法见 [fixture说明](../../tests/fixtures/README.md)。边界媒体由本仓库脚本在临时目录生成。
- 已创建并随实现更新项目 [AGENTS.md](../../AGENTS.md)。所有文档已去除旧组织/项目名称及来源绝对路径，保留文件名、摘要和技术合同。
- Git已初始化为 `main`；按用户授权将当前实现、文档、测试与验收记录归档为本地首次提交，提交信息以 Git 记录为准。未 push、merge、tag 或生产部署。部署私有配置、临时数据和测试日志在忽略范围内。

## P0～P7 既有验证

最终服务镜像为 `ffmpeg-agent:v1-local`，不可变 ID：

```text
sha256:f5a01280a8b1771e80e8df8fbeb3d1136ed95f6dbb6469d187fbf0dd5e2c236b
```

正式镜像使用 Python3.14.8、FFmpeg/ffprobe7.1.5、Pillow12.3.0。本机 Python3.14.7 / FFmpeg9.0.1 的结果单独记录，不冒充部署引擎结果。精确依赖、镜像摘要、系统包与架构见 [运行基线](../verification/runtime-baseline.md)。

| 验证 | 实际结果 | 可复核证据 |
| --- | --- | --- |
| `.venv/bin/ruff check .` | 退出0 | [最终检查记录](../verification/final-validation.json) |
| `.venv/bin/pyright` | 0 errors / 0 warnings | 同上 |
| `.venv/bin/pytest -q` | **238 passed in 20.43s** | 同上；记录全部21个源文件摘要 |
| 最终镜像完整 pytest | **238 passed in 33.39s** | [tests-arm64.json](../../deploy/tests-arm64.json)，`deploy/verify-tests.py`可复执行 |
| 最终镜像 P3 端到端 | 全部通过 | [container-p3-arm64.json](../../deploy/container-p3-arm64.json) |
| 最终镜像故障矩阵 | **8/8，通过10次整容器 KILL，退出均137** | [故障说明](../verification/container-faults.md)、[原始结果](../verification/container-faults-arm64.json) |
| 同 Compose 与跨 Compose | 两组通过 | [deployments-arm64.json](../../deploy/deployments-arm64.json) |
| 容器 HTTP 输入与消费 | 单机通过 | [http-script-smoke-arm64.json](../../deploy/http-script-smoke-arm64.json)，明确标记 single_host_http_smoke |
| 原生 arm64 Worker 1/2/4 容量 | 三组通过 | [capacity-arm64.json](../../deploy/capacity-arm64.json) |

完整测试包含真实媒体、SQLite写锁/事务取消、20并发同键、全工作卷下载竞争、输入不变性、真实FFmpeg取消、发布四窗口、租约与清理竞争、时间/色彩边界及错误脱敏。真实 TCP 用64MiB文件制造背压，确认发送未停时仍有引用、文件仍存在；客户端断开后收尾释放并回收。此项不使用假 ASGI 发送替代网络证据。

故障矩阵使用同一最终镜像，包含正在运行的真实 FFmpeg 硬停恢复与重试耗尽、intent/staged/renamed/committed、持久取消、同活容器热重启拒绝和孤儿进程锁隔离。后三个完整发布窗口将 `max_attempts=1`，证明最后一次允许尝试完成后能保持同 artifact ID 并直接收尾，attempts仍为1。

容量每组8个 `Feline_Apocalypse.mp4` 转码，在2 CPU / 2 GiB / 128 PID下，Worker=1/2/4吞吐分别 **19.54 / 17.30 / 11.66 jobs/min**，GET job p95为 **4.15 / 49.79 / 84.34ms**，取消并确认停止约 **0.82～0.85s**。资源口径与限制见[部署说明](../../deploy/README.md#本地验收与容量实测)。默认1有实测支持；容量报告保留其当时精确镜像，随后修改未涉及编码器/预设/并行槽实现，不将数据扩张成所有输入的性能保证。

所有隔离验收创建的容器/卷均按所有权标签清理，最终只读检查无本次验证资源残留。曾出现的验证环境问题（测试 venv 位于 noexec tmpfs、代码更新时旧镜像与新测试不匹配）已修正并以最终镜像重跑通过，不列为当前失败。

## 运行、调用与剩余条件

运行与四种输入调用示例见[README](../../README.md)，同/跨 Compose、租约消费、配置、备份、排空升级及验证脚本见[部署说明](../../deploy/README.md)。配置生成器不打印 token，真实配置仅存私有目录；共享输入须保持到 `inputs_released=true`。

真实双宿主机验收先保留待办，后续有**第二台实际宿主机**、两侧受控网络可达的服务/来源地址及专用测试凭据后，再执行 `deploy/verify-http-remote.py host` 和 `run`。详细步骤、私有配置模板、主机身份核对及结果校验已备齐；单机脚本自验不能将此项打勾。P7保持未完成。

其他明确限制：amd64仅完成模拟环境运行基线，未测原生amd64容量；视频不支持HDR/10bit/非方形SAR，图片不支持动画/多页；集合不自动打包，不支持任意引擎参数、跨容器Worker或多实例共享SQLite。保持这些首版边界，不以缺少第二宿主机为由扩大架构。
