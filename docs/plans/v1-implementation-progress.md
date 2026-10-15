# 首版实施进度与验收证据

日期：2026-10-15（Asia/Shanghai）。执行项目：cfKanban **KZ / ffmpeg-agent**，关联记录为 `.cfkanban-scope.json`。

P0～P12 既有交付证据保留；P13～P17 通用媒体参数及简单默认调用的当前环境实现和验收已完成。**P7/P12 的真实双宿主机验收按用户要求保留待办，后续具备环境后补验。** 同机容器和单机 HTTP/MCP 结果未计为跨宿主机通过。

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
| P13 统一参数合同 | CFK-794 | 完成 | 六个操作同一模型、可省略参数、可选预设、规范化/幂等/持久化及能力发现 |
| P14 音视频参数化 | CFK-795 | 完成 | 四种视频/五种音频编码、auto/source、质量/码率、尺寸/帧率/片段及 copy 包摘要 |
| P15 图片/抽帧/探测 | CFK-796 | 完成 | 格式专属质量、单边尺寸/补边/裁剪、三种采样/取帧、标签过滤/统计及取消 |
| P16 HTTP/MCP 一致 | CFK-797 | 完成 | 六能力最简及自定义调用，multipart、错误、有效参数、集合成员及机器合同 |
| P17 参数化验收 | CFK-798 | 完成当前环境范围 | 本机/精确镜像各 456 项、48 个生产 HTTP/MCP 任务、8 项故障矩阵通过 |

## 调用方体验与可运行示例

2026-10-15 从上传、处理到下载重新核对 HTTP 与远程 MCP。服务端接口已能完成闭环，本次补充完整客户端与使用说明：

- `examples/http-workflow.py` 默认一次 multipart 上传并提交；`--upload-first` 演示独立上传得到 `file_id` 后提交。仅使用 Python 标准库，实际以系统 Python 3.9 运行通过。
- `examples/mcp-workflow.py` 使用锁定 SDK，完成 HTTP 上传、远程 MCP 初始化/媒体工具/任务和产物查询、HTTP 下载。六种能力均可省略参数，两个客户端共用文件传输和输出处理。
- 自动轮询、校验大小/SHA-256、下载集合分页与部分成功；下载验证通过后才发布单文件，不覆盖已有目标，失败清理本次临时文件。每次默认使用新请求 ID；显式同键重试要求完整请求相同；超时可用 `--job-id` 恢复且不再读取源文件。
- 中英文 README 改为仓库素材加独立临时输出目录的可执行命令，覆盖两种 HTTP、MCP、抽帧和自定义参数。远程客户端配置只需地址和可选 Token；普通立即 HTTP 下载无需显式租约，共享或延迟读取仍按原租约合同。
- Compose 健康检查改用 `/health/ready`，使 README 的 `up --wait` 等待服务就绪；重复启动保留已有 `.env` 与私有配置。

实际验证：

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 本机完整检查 | Ruff/Pyright/根 Compose/diff 通过；**477 passed in 57.30s**，含新增 21 项下载和目标保护测试 | [本机记录](../verification/caller-validation.json) |
| 脚本直接调用生产容器 | 六能力 × 三通路 × Token 开/关，共 **36 个默认流程**；6 次直接恢复、6 组同键重试、4 个 partial 流程通过 | [示例验收](../verification/caller-examples-arm64.json)；可用 `deploy/verify-examples.py` 复验 |
| README 原样命令 | 六个 shell 块通过，含 Compose 构建/就绪及五个处理示例；重复启动保留配置；三通路等待超时后从同一任务恢复下载 | [README 实跑](../verification/caller-readme-smoke.json) |
| 双语说明 | 8 个代码块完全一致；20 个本地链接、shell 语法与请求模型检查通过 | [本机记录](../verification/caller-validation.json) |

示例矩阵固定镜像 `sha256:7eeda466adad958b13d4b91a980c9254fcd590b642c879c436c6f009da648a06`。README 原样构建得到 `sha256:1784b32994b5c901ef2179822851efd0454038f4d7e0ee842f72d710a3bf495d`，其 27 份服务源码摘要与前述验收一致；分别记录各自镜像证据，不假设重建后的 ID 必然相同。服务源码和媒体参数合同未改动，本轮不重复计算先前容器媒体/故障矩阵为新结果。

所有隔离验收的自有容器、卷已清理，Compose 网络也无残留。真实双宿主机、原生 amd64 等外部验收条件仍保留。关联 CFK-798 的使用交付补充，不新增公共接口或参数版本。

2026-10-15 补充面向调用方的[英文参数参考](../usage/parameters.md)与[中文参数参考](../usage/parameters.zh-CN.md)，两份 README 增加编码、格式及常用枚举简表。说明服务适配范围与运行引擎能力的交集、Docker 与宿主机的区别、编码/容器兼容规则及实时能力查询。10 个 JSON 示例双语一致并通过共享参数模型校验；两份参考各 12 行编码/容器兼容数据与实现一致；README 的 8 个代码块保持双语一致且未变动，四份说明的 30 个本地链接有效，`git diff --check` 通过。本次只补文档，不变更参数合同、默认值或运行代码，也不将模型校验计作新的媒体执行验收。

2026-10-15 进一步补齐接口自描述：六个 MCP 媒体工具直接说明默认输出、可选编码/格式及组合约束，共享参数 schema 提供字段说明、单位、范围、示例和条件默认。JPEG/WebP 质量、按编码推导容器等条件使用 `x-default-by-*`，不再用 `default: null` 冒充省略后的实际默认；仍要求结合预设和父对象理解。HTTP 普通任务及 MCP `submit_job` 的步骤按 `operation` 提供对应 `params` schema，multipart 的 `job` JSON 内容模型保留上传占位输入并复用同一组步骤。OpenAPI 保留共享 schema 中有意义的 null 值，避免框架序列化造成两端条件默认不同。请求接受、参数归一化和媒体执行逻辑未变；接口描述参与既有合同摘要，摘要随文档元数据更新。

本次本机验证：Ruff 通过，Pyright **0 errors / 0 warnings**，完整 pytest **501 passed in 58.17s**。新增[参数发现单测](../../tests/unit/test_parameter_discovery.py)及[真实 TCP 发现检查](../../tests/integration/test_parameter_service.py)覆盖有效示例、条件默认与实际归一化一致、六能力在 HTTP/MCP/任务图/multipart 的字段级 schema 一致、引用可解析及发现查询不创建任务。8 份机器合同重新导出，其中 [OpenAPI](../api/v1-openapi.json)、[JobCreate](../api/JobCreate.json) 和[操作合同](../api/media-operations.json)更新；四份说明的 32 个本地链接有效、原代码示例保持不变，diff 检查通过。本次未重建容器或重跑容器故障/跨宿主机验收，原有环境范围与待办保持。

## P13～P17 通用参数证据

先完善[参数合同](../design/media-parameters.md)及 cfKanban CFK-794～CFK-800，再实施共享参数与六类能力。按新项目的使用目标，六个现有 `.v1` 操作直接支持扩展参数；MCP 仍是 19 个工具，其中六个媒体工具一一对应六个媒体操作，不增加额外参数版本选择。

- HTTP 与 MCP 均可省略 `params`。视频默认 H.264/MP4、CRF 23，保留源尺寸与帧时间；音频默认 AAC，auto 优先保留采样率与声道，按编码和实际声道选兼容码率。图片默认保持尺寸转 JPEG quality 90；抽帧默认首帧；探测默认 format/streams。
- 三个具名预设保留为可选快捷设置，显式参数可覆盖。音频 `source` 严格保持，`auto` 才允许兼容回退；copy 拒绝编码变换、编码预设和精确裁剪。
- HTTP、multipart、MCP 复用同一严格模型。`operations[操作名].default_params` 给出实际默认，任务和产物公开安全有效参数，帧成员公开实际媒体信息；不公开 source、路径或凭据。
- 四种软件视频编码、五种音频编码及实际可用组合可查询；质量/码率、帧率、尺寸、采样率、声道、格式与预设不再写死为唯一规格。抽帧数量和显式帧率上限独立作为部署限制，可配置。
- 真实媒体校验覆盖 VFR 源/输出精确 PTS、copy 音频逐包 SHA-256、禁止放大时奇数尺寸 cover、像素/透明/背景/质量、部分成功、扫描统计及脱敏；复用原进程组取消、FD、发布和回收。

最终验收固定精确镜像：

```text
sha256:7eeda466adad958b13d4b91a980c9254fcd590b642c879c436c6f009da648a06
```

原生 Linux arm64、Python 3.14.8、FFmpeg/ffprobe 7.1.5、Pillow 12.3.0。镜像内 **27 份服务源码 SHA-256** 与工作区、最终本机验证记录逐项相等，六个原始 fixture 的长度与摘要保持不变。详见[镜像核对](../verification/parameters-image.json)。

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| Ruff / Pyright / diff / 根 Compose 配置 | 全部退出 0；Pyright 0 errors / 0 warnings | [本机验证](../verification/parameters-validation.json) |
| 本机完整 pytest | **456 passed in 59.54s**；Python 3.14.7、FFmpeg 9.0.1 | 同上 |
| 精确镜像完整 pytest | **456 passed in 110.23s**；锁定依赖、非 root、只读根 | [容器测试](../verification/parameters-tests-arm64.json) |
| 生产 HTTP/MCP 全六能力 | **48 个任务通过**：六能力 × 默认/自定义 × HTTP/MCP × Token 开/关；幂等、下载、HEAD/Range/SHA、成员与有效参数一致 | [生产闭环](../verification/parameters-smoke-arm64.json) |
| 精确镜像故障矩阵 | **8/8 通过**；执行强停、发布四窗口、取消恢复、热重启拒绝、孤儿锁 | [故障矩阵](../verification/parameters-faults-arm64.json) |
| 双语文档与机器合同 | 代码示例一致，模型/JSON/Python 示例校验与 67 个本地链接通过；导出八份合同 | [README](../../README.md)、[中文说明](../../README.zh-CN.md)、[操作 schema](../api/media-operations.json) |

生产闭环两组所有权标签容器/卷残留均为 0；测试与故障容器完成后清理各自资源。部署示例增加 `max_frame_count` / `max_frame_rate`，根 Compose 与 `.env.example` 仍解析通过。本地提交归档实现、文档与证据，未推送或生产部署。

10-bit/HDR 与硬件加速分别保留 **CFK-799、CFK-800**，本轮未开放占位开关。实际双宿主机、增量原生 amd64 与新默认参数下的容量尚未验证；旧容量报告只代表当时的固定预设与精确镜像，不扩张到本轮全部编码或默认行为。

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
