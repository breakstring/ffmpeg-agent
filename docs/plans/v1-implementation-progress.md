# 首版实施进度与验收证据

日期：2026-10-16（Asia/Shanghai）。执行项目：cfKanban **KZ / ffmpeg-agent**，关联记录为 `.cfkanban-scope.json`。

P0～P17 既有交付证据保留；P18～P21 自然语言规划与官方 A2A 的当前环境实现和验收已完成。**P7/P12 的真实双宿主机验收按用户要求保留待办，后续具备环境后补验。** 同机容器和单机 HTTP/MCP/A2A 结果未计为跨宿主机通过。

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
| P18 自然语言合同与依赖 | CFK-808 | 完成 | Python 3.14、PydanticAI slim 2.56.0、A2A SDK 1.2.2 解析、导入与协议基线 |
| P19 持久规划与 HTTP/MCP | CFK-809 | 完成 | 同库 schema 3、规划幂等/有界并发/取消/恢复、输入资格；启用时 22 个 MCP 工具 |
| P20 官方 A2A | CFK-810 | 完成 | A2A 1.0 JSON-RPC、官方客户端、统一鉴权、异步查询/取消、HTTP 产物引用 |
| P21 自然语言增量验收 | CFK-811 | 完成当前环境范围 | 本机/精确镜像各 635 项、百炼 6 场景、6 个整容器强停恢复场景通过 |

## 专用素材迁移与内容回归验证

2026-10-15，CFK-807：按用户确认方案，以[本仓库生成器](../../scripts/generate_fixtures.py)离线生成六个专用媒体，删除原复制的六个文件。基础素材包含奇数宽度纵向图板、分段单声道 MP3、30 秒带帧号及左右声道标记的主视频、高码率 1080p 视频、HEVC 与 Matroska 视频，共 **15,764,123 字节**。方法、用途、属性、大小和 SHA-256 见 [素材说明](../../tests/fixtures/README.md)、[摘要清单](../../tests/fixtures/checksums.json)及[生成记录](../../tests/fixtures/manifest.json)。

素材使用精确镜像 `sha256:7eeda466adad958b13d4b91a980c9254fcd590b642c879c436c6f009da648a06` 生成，FFmpeg 7.1.5、Python 3.14.8、Pillow 12.3.0；完整解码全部六文件，并核对编码、尺寸、帧数、声道、时长、时间基、HEVC PTS 和高码率下限。固定参数及种子用于重新生成相同用途的输入，不承诺编码文件跨构建或重建逐字节一致。

同步测试、部署验收脚本、双语 README 和当前素材约定。远程验收脚本改为从当前摘要清单读取输入身份，实测接受当前文件、拒绝同尺寸篡改与错误长度。旧报告里的文件名、摘要和容量数据保留历史含义，不改写为新素材验收。

新增 **11 项真实内容回归**，边界输入只在独立临时目录生成：

- 合法线性 RGB ICC 的像素 `(128,64,32)` 实际转换为 sRGB `(188,137,99)`，并清除输出元数据。
- 分段双声道 PCM 验证裁剪后的换音点、16kHz 重采样、左右声道内容及单声道混音。
- 四帧纯色视频将 next/previous/nearest 的抽帧时间绑定到实际颜色，覆盖乱序、重复和平局；另核对视频裁剪首末帧内容。
- 四象限 JPEG 验证 EXIF 2/3/6/8 的镜像、旋转方向、尺寸与元数据清除。详见[内容测试](../../tests/integration/test_media_semantics.py)。

本轮验收固定精确镜像 `sha256:fc108171c589ad80d5376fcb9bc6e5ef42c83eb2a18e43d6692b6b58002f9373`，原生 Linux arm64、FFmpeg 7.1.5。镜像内 27 份服务源码摘要与工作区逐项相等；本轮未修改生产服务代码，见[镜像核对](../verification/fixtures-image.json)。

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| Ruff / Pyright / diff / 素材身份与文档 | 全部通过；101 个本地链接、19 个 shell 块及双语 8 个相同代码块检查通过 | [本机检查](../verification/fixtures-validation.json) |
| 本机完整 pytest | **512 passed in 73.14s**；FFmpeg 9.0.1 | 同上 |
| 固定镜像完整 pytest | **512 passed in 135.55s**；`MEDIA_TEST_ENGINE_SERIES=7.1`、非 root、只读根 | [容器测试](../verification/fixtures-tests-arm64.json) |
| 真实媒体原型 | 六素材摘要、FD/路径边界、抽帧时间、音视频预设、图片与 ICC 转换通过 | [原型结果](../verification/fixtures-prototype.json) |
| 六能力 HTTP/MCP | **48 个任务通过**，Token 开/关、默认/自定义参数、实际产物、HEAD/Range/SHA 与幂等 | [参数闭环](../verification/fixtures-parameters-arm64.json) |
| 三通路调用示例 | **36 个默认流程**、6 次恢复、6 组同键重试（12 次执行）、4 个 partial 流程通过 | [示例实跑](../verification/fixtures-examples-arm64.json) |
| 容器故障与恢复 | **8/8 通过**，含执行强停、发布四窗口、取消恢复、热重启拒绝及孤儿进程保护 | [故障矩阵](../verification/fixtures-faults-arm64.json) |

容器完整套件首轮暴露验收脚本未挂载 `examples/`，导致 21 项已有客户端测试在准备阶段缺文件；补上只读挂载后重跑全部 512 项通过。所有本轮验收容器与卷均已清理，标签核对残留为 0。新素材容量基准、原生 amd64 和真实两宿主机验收未运行；旧容量数字只作为历史基线，不扩张为新输入的性能保证。

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


## 2026-10-16：冻结自然语言与 A2A 增量（P18）

按用户授权，新增 [自然语言与 A2A 合同](../design/media-agent.md)，同步既有服务设计、技术选型和 P18～P21 实施计划。cfKanban KZ / ffmpeg-agent 已创建并读回 CFK-808～811；合同区分规划接受、有效计划与媒体执行，冻结单库幂等/取消/恢复、文件引用资格、A2A 1.0 和统一可选 Token。

Python 3.14.7 / macOS arm64 隔离解析和安装 `pydantic-ai-slim[openai]==2.56.0`、`a2a-sdk[http-server]==1.2.2` 通过，既有固定依赖版本未变；主项目 uv lock/sync 也已通过。无外部模型请求的 MockTransport 验证结构化输出与官方 SDK 协议入口，结果见 [依赖 preflight](../verification/agent-dependency-preflight.json)。其中 SDK 0.3/SSE 是依赖探测，不是本服务提供这些能力的证据。

`.env.local` 已存在且被 `.env.*` 忽略，未被 Git 跟踪；只核对键名存在，不输出值。重新提供无秘密 `.env.example`。此记录仅证明合同与依赖基线，不代表实现、真实百炼或容器增量验收完成；后续结果分别追加，既有双宿主机等未验证条件保持不变。

## 2026-10-16：自然语言与 A2A 实现和验收（P19～P21）

使用 PydanticAI 生成受限 `MediaPlan`，由原媒体服务绑定输入、校验并执行；新增 HTTP 自然语言任务和 MCP 三工具，以及官方 A2A 1.0 JSON-RPC、Agent Card 与[可运行客户端](../../examples/a2a-workflow.py)。配置默认关闭，启用方式见[中文指南](../usage/agent.zh-CN.md)及[英文指南](../usage/agent.md)。Compose 使用私有服务 JSON，不自动传递 `.env.example` 的模型字段。

schema 3 在同一 SQLite 增加短期规划记录，保存计划后恢复直接提交，原子关联唯一媒体 job；未保存计划时的在途模型调用中断返回 `PLANNING_INTERRUPTED`，不自动再次请求模型。取消遵循实际停止，提交期间到期/release 的输入不会复活。独立复核发现并修复取消撞上错误收尾、计划刚提交即关闭、模型初始化失败遗留后台任务、模型说明复述私有描述四类边界，均已固化回归。公开说明只用受控模板，终态清除原始描述与输入绑定。

最终精确镜像为 Linux arm64、Python 3.14.8、FFmpeg/ffprobe 7.1.5，32 份服务源码 SHA-256 与工作区逐项一致：

```text
sha256:680953c259022ca9d1ad9e703021e67a1ad431eab04be6189da0a0e6f663a0c4
```

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| Ruff / Pyright / diff / 机器合同 | 退出 0，Pyright 0 errors / 0 warnings；导出 11 份合同 | [本机与源码记录](../verification/agent-local-validation.json) |
| 本机完整 pytest | **635 passed in 85.25s**；Python 3.14.7、FFmpeg 9.0.1 | 同上 |
| 精确镜像完整 pytest | **635 passed in 150.34s**；FFmpeg 7.1、非 root、只读根 | [容器完整测试](../verification/agent-tests-arm64.json) |
| 百炼真实模型与三通路 | `qwen3.8-flash`、默认 60 秒规划预算，**6/6 场景通过**；幂等、真实媒体、重启查询与公开响应/日志凭据扫描通过 | [百炼验收](../verification/agent-validation.json) |
| 整容器故障恢复 | **6/6 通过**，6 次 KILL 退出均为 137；planning/planned/submitting/submitted 与两个取消窗口，恢复不重复调用模型或创建 job | [故障报告](../verification/agent-faults-arm64.json) |
| 文档与示例 | 10 份文档的 150 个本地链接/锚点、6 个 JSON 示例及 CLI 参数检查通过 | [使用指南](../usage/agent.zh-CN.md)、本机记录 |

百炼实际输出：HTTP 图片为 PNG 80×107；MCP 音频为 FLAC 48 kHz 单声道；A2A 一次请求得到 H.264 MP4 320×180、MP3，以及 0/500 ms 的两张 PNG 80×45；HTTP 双输入探测得到音频和视频两份报告。不支持的字幕任务返回 `unsupported`，缺少目标格式返回 `needs_input`，均不创建媒体 job。下载核对字节数/SHA-256、HEAD/Range，图片解码和 ffprobe 属性均符合要求；成功结果重启后保持同一 agent task、job、计划及输出。

[首次 60 秒验收](../verification/agent-validation-default60.json)保留 5/6 通过及 A2A 组合规划超时的事实。后续仅删除模型能力提示中与输出 schema 完全重复的参数 schema，六操作约束、说明、条件默认和端口保持一致；离线请求体由 118,161 降至 71,873 字节。最终镜像复验仍使用默认 60 秒，组合请求在 43.814 秒完成规划、执行和校验。该次成功不代表延迟保证；`planning_timeout_seconds` 可在 (0,300] 秒内配置，验收脚本也支持显式预算与单场景选择，不自动重试已中断的规划。

`.env.local` 未修改、未跟踪且继续被忽略；模型 Key 未出现在变更文件、公开响应、容器参数/环境或日志中。验收只在进程中读取配置，以 0700 临时父目录中的只读文件挂载到测试容器，不将 Key 放入 Docker 环境或命令行。百炼和故障测试的自有容器/卷残留为 0，生成媒体与日志不提交。

本轮仅本地提交，未 push、merge、tag 或生产部署。真实双宿主机、自然语言增量原生 amd64、规划容量与长期延迟分布未验证；P7/P12 环境待办、HDR/10-bit（CFK-799）、硬件加速（CFK-800）保持原范围。A2A 首期仅单次请求、查询、取消及 HTTP 产物，不声明多轮、SSE、push 或 0.3 客户端兼容。

## 2026-10-16：统一 `.env` 配置（P22 / CFK-812）

服务 Bearer Token、模型 Key/标识/地址、自然语言与 A2A 开关、规划预算统一由 `.env` 提供；服务默认读取当前目录文件，进程环境优先，Compose 按白名单传入。结构 JSON 不再接受这些字段，缺失或空 Token 拒绝启动，只有显式小写 `null` 开放服务。生成器支持初始化、原子迁移与幂等重跑；先保存 `.env` 再移除 JSON 的旧凭据/模型字段。同步双语说明、三种示例客户端及隔离验收脚本。

迁移独立复核发现并修复两处边界：复制模板后的空模型字段不得覆盖并删除旧 Key；合法 `replace…` 前缀 Token 不得被误识别成占位符轮换。已有明确 `false` 开关继续保留，空服务 Token 在写入前拒绝。单/双引号、美元符号和反斜杠经过 15 组本地与 Compose 字面解析对照；模型 Key 按不透明字符串处理，不能再次当 JSON 解码。

最终精确镜像为 `sha256:ea09dd8e2d0ec68f792bd4caf75c7dae4a9e9a57b122f8ea591f4e9b6ef215b5`（Linux arm64），33 个服务源码摘要与工作区一致。

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| Ruff / Pyright / diff | 全部通过；0 errors / 0 warnings | [本机与配置验证](../verification/env-local-validation.json) |
| 本机完整 pytest | **702 passed in 83.63s** | 同上 |
| 最终镜像完整 pytest | **702 passed in 151.27s**；非 root、只读根、FFmpeg 7.1 | [精确镜像测试](../verification/env-tests-arm64.json) |
| 三阶段 Compose 重建 | Token+关闭、Token+开启、null+关闭均通过；HTTP/MCP/A2A/Card 鉴权一致 | [Compose 验收](../verification/env-compose-validation.json) |
| 模型配置与脱敏 | 本机模拟 OpenAI 收到正确 Key/model/输出预算，HTTP 任务到 `needs_input`，A2A 查询同任务；37 次公开响应、3 次日志扫描无测试凭据 | 同上 |
| 文档与 CLI | 79 个本地链接、4 个锚点、3 个示例 `--help` 通过 | 本机记录 |

本机原有模型的三个字段已无损迁入权限为 `0600` 的 `.env`，确认加载成功后从旧 `.env.local` 移除对应字段；服务 Token 已生成，LLM/A2A 仍默认关闭。实际 `.env` 与 Compose 读取值一致，私有文件保持 ignored/untracked，交付文件凭据匹配扫描为零；真实 Key 未进入工具参数、日志、报告或 Git。修改后须重新创建容器，操作见[部署说明](../../deploy/README.md)。

本轮仅验证配置，模拟模型不作为真实供应商或媒体质量的新证据；P21 的百炼及真实媒体报告保持原镜像与原数据。隔离 Compose 的自有容器、卷和网络已清理；仅本地提交，未 push、merge、tag 或生产部署。双宿主机、原生 amd64 等既有未验证条件不变。

## 2026-10-16：统一配置后的真实模型补验（CFK-812）

按用户要求，使用原 `.env.local` 已迁入 `.env` 的真实模型配置重新执行六场景验收；读取时确认旧文件不再包含模型字段，规范文件具备真实 Key、模型与地址。没有使用模拟模型，也没有将凭据复制回旧文件。当前精确镜像仍为 `sha256:ea09dd8e2d0ec68f792bd4caf75c7dae4a9e9a57b122f8ea591f4e9b6ef215b5`，使用生产启动入口、PydanticAI/AsyncOpenAI、`qwen3.8-flash` 和现有默认 **60 秒**规划时限，未覆盖时限。

`deploy/verify-agent.py --env-file .env` 完整执行结果为 **6/6 通过**，脱敏结果见[真实模型报告](../verification/env-real-model-validation.json)。

| 场景 | 实际结果 | 端到端时长 |
| --- | --- | --- |
| HTTP 图片 | PNG 80×107，完整解码和下载校验通过 | 21.464 秒 |
| MCP 音频 | FLAC、48 kHz、单声道 | 12.952 秒 |
| A2A 视频组合 | H.264 MP4 320×180、MP3，以及 0/500 ms 两张 PNG 80×45 | 34.052 秒 |
| HTTP 双输入探测 | 音频和视频两份独立报告 | 15.057 秒 |
| 不支持的字幕请求 | `unsupported`，未创建媒体任务 | 6.898 秒 |
| 缺少目标格式 | `needs_input`，未创建媒体任务 | 9.856 秒 |

成功场景均等待媒体终态与输入释放，再实际下载，核对字节数/SHA-256、HEAD/Range、图片解码与音视频属性；幂等重放保持相同任务，重启后任务、计划、媒体 job 和产物保持一致。HTTP/MCP/A2A/Card 鉴权、官方 A2A 发现、MCP 22 工具及 fixture 未变检查通过。凭据只进入受保护临时文件挂载，Docker 参数、环境、公开响应、日志和报告的敏感值扫描全部通过；自有容器和卷剩余均为零。

本次仅新增验收证据，生产代码与本机默认功能开关未修改，不重跑未变化的 702 项回归。上述时长为本次端到端观测，不作为供应商延迟保证。既有模拟配置测试与历史真实模型报告均保留，真实双宿主机等待办不变；仅本地提交，不推送或部署。

## 2026-10-16：独立消费者技能（P23 / CFK-813）

交付 [`ffmpeg-agent-connect`](../../skills/ffmpeg-agent-connect/SKILL.md) 与 [`ffmpeg-agent-media`](../../skills/ffmpeg-agent-media/SKILL.md)。两个目录分别自包含 Python 3.10+ 标准库客户端，共用 `consumer/ffmpeg_agent_client/` 维护源，通过生成器同步 11 个运行文件及摘要；不依赖服务端源码、Docker、FFmpeg 或模型 Key。双语[接入指南](../usage/consumer-skills.zh-CN.md)覆盖宿主原生 MCP 与辅助 CLI、私有配置、上传、三协议提交、恢复、取消、集合/部分结果下载和定向 release。

任务记录保存冻结请求与稳定幂等键，接受结果未知时复用原请求，已接受后只查询；媒体终态且输入停止使用后才收集。输出目录受所有权标记保护，已有文件仅在大小与 SHA-256 一致时复用；集合保存清单与失败项。只 release 本次上传，保留本地源文件和服务产物。私有配置仅含服务地址/Token，不复制服务端模型配置。

本次复用健康的现有 arm64 服务，精确镜像为 `sha256:754a08caaf17fe025c8f925606683f475d28df15695498ee8868806589ef3b1b`。没有修改服务源码、部署配置或凭据，也未重建/停止容器；实际模型与 A2A 继续开启。最终技能运行包 manifest SHA-256 为 `cb06cda3a6b12ceee4ec55eaafc0044005e278649e8ab3caf1110ad88adc8794`。

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| Ruff / Pyright / 完整 pytest | 全部退出 0；Pyright 0 errors / 0 warnings；**762 passed in 83.02s**，含新增 60 项消费者测试 | [本机验证](../verification/consumer-skills-local-validation.json) |
| 独立目录真实服务 | **16/16 场景通过**；六操作、HTTP/MCP、双输入双输出、部分抽帧、失败、等待恢复、取消、上传后独立 MCP 提交再收集 | [消费者闭环](../verification/consumer-skills-live-validation.json) |
| 真实模型 | HTTP PNG 80×107、MCP FLAC 48 kHz 单声道；需补充格式返回 `needs_input`、有脱敏说明且无媒体 job | 同上 |
| 仅按技能执行的 A2A 前向验证 | 0 秒等待后用原状态恢复；两张 PNG 80×45，大小/SHA-256 与 PNG 头检查通过；最终包再次恢复保持 ID 并复用文件 | [前向验证](../verification/consumer-skills-forward.json) |
| 实际最低 Python 基线 | CPython **3.10.21**，单技能、`-S -B`、清除相关进程环境；连接、HTTP 探测恢复及新 MCP 探测完整通过 | [最低版本](../verification/consumer-skills-python310.json) |
| 技能与文档 | 两个技能结构验证、生成副本一致；8 份使用/技能文档链接与 shell 语法、37 个示例参数解析通过 | 本机验证 |
| 凭据与输入保护 | 变更文件、消费者输出/私有状态及当前服务 4 小时日志无实际 Token/模型 Key 匹配；模型 Key 未进入消费者目录，fixture 摘要未变 | 本机验证、消费者闭环 |

真实验收发现并修复 MCP 响应解析错误：`structuredContent.error` 是任务领域字段，`null` 或任务失败都不能当作 MCP 工具调用失败；现在仅以协议 `isError` 判定，补充两项回归。原有接受请求通过同一状态/幂等键恢复，未重复模型规划。验收脚本也修正了探测结果的 `codec` 字段断言及基础视频实际含音轨的假设。最终报告中的恢复耗时是当次 CLI 收集耗时，不是模型规划延迟。

原生宿主 MCP 的配置和发现遵循宿主文档；本次验证了独立 MCP 客户端及上传/独立提交/收集组合，没有把它表述为所有宿主连接器兼容。A2A 前向验证只统计消费者观察到的唯一任务 ID，未查询服务端全库任务数，也未独立解码源视频核对帧时间点。实际双宿主机、Windows、HTTPS/代理部署、容量和新容器强停未在本轮验证，既有待办与历史证据保留。本轮仅本地提交，不 push、merge、tag 或部署。

## 2026-10-16：本机 Codex 安装与原生 MCP 补验（P23 / CFK-813）

按用户追加授权，将两个完整消费者技能安装到本机 Codex 用户技能目录，逐文件与已提交包核对一致。Codex `skills/list` 实际发现两技能且均为 enabled；持久 Python 3.12.13 可运行客户端。新增 `ffmpeg_agent` Streamable HTTP 配置，使用官方 `http_headers_helper` 从权限为 `0600` 的私有消费者文件读取服务 Token，全局配置不含该 Token；消费者不持有模型 Key。原有配置保留并已私有备份，未重启 Codex 或媒体服务。

运行中的 Codex App Server 0.162.1 重载后，当前线程连接状态为 `connected`，发现 **22 个工具**。本轮实际调用会话的 **`mcp__ffmpeg_agent` 原生工具**：`list_capabilities`、`submit_job`、`get_job`、`get_artifact` 和 `list_artifact_members`；辅助客户端仅负责 HTTP 上传和下载收集。

自有 `video_h264_640x360.mkv` 上传后，原生 MCP 提交一个三步骤任务，实际到达 `succeeded` 且 `inputs_released=true`。下载 H.264 MP4 **320×180**、0/500 ms 的两张 PNG **80×45** 和 JSON 探测报告，共四个文件；独立大小/SHA-256、ffprobe 属性、PNG 完整解码及报告解析通过。仅 release 本次上传，fixture 与本地源副本摘要未变。详见[原生 MCP 验收报告](../verification/codex-native-mcp-validation.json)。

本次补齐本机 Codex 的安装发现、原生 MCP 与文件传输闭环；不扩展为所有宿主、真实双宿主机、Windows 或 HTTPS/代理兼容保证。帧时间按服务原生元数据核对，未在本轮独立解码源时间线。没有修改服务实现或技能运行代码，沿用前轮 762 项回归证据；只验证本轮安装和实际闭环，并本地提交新增报告。

## 2026-10-16：原生 MCP 自然语言链路补验（P23 / CFK-813）

使用已安装的 `ffmpeg-agent-media` 技能与既有连接，文件由技能客户端 HTTP 上传/下载，规划与执行查询实际经过会话的 `mcp__ffmpeg_agent.generate_media`、`get_agent_task`、`get_job`、`get_artifact`、`list_artifact_members`。本次未修改服务配置、技能运行代码或模型开关，也未重启服务。

首个「转码 320 宽 H.264 MP4，加原视频 0/500 ms 两张 160 宽 PNG」组合请求在当前 **60 秒**规划期限内失败，稳定错误为 `PLANNING_TIMEOUT`，没有计划或媒体任务。保留该失败证据并释放其上传；向用户说明后，以新状态和新请求验证单项「将视频首帧保存为 160 宽等比 PNG」，未重发原请求，也未切换协议。

单项请求实际返回 `decision=execute` 和受限 `video.extract_frames.v1` 计划，关联媒体任务 **succeeded**、执行尝试 1、`inputs_released=true`。服务接受到媒体任务创建为 **8.222 秒**，任务创建到产物发布为 **0.624 秒**；前者包含排队、规划、校验与提交，不是独立模型耗时。技能下载 **160×90 PNG、6939 字节**，随后自动释放本次上传。

独立验证 **17/17** 通过：实际文件大小/SHA-256 与原生成员及消费者清单一致，Pillow 和 FFmpeg 完整解码，源 fixture 前后 SHA 不变；另以本机 FFmpeg 9.0.1 解码首帧并用 Pillow Lanczos 重建，平均 RGB 差 **0.671/255**、最大通道差 **3**，不宣称逐像素相等。集合摘要仅与原生元数据核对，实际 PNG 摘要独立重算。消费者未持有模型 Key，运行文本及服务近 10 分钟 stdout/stderr 的已知凭据匹配为 0。

详见[原生 MCP 自然语言验收报告](../verification/codex-native-mcp-natural-language-validation.json)。本次证明单项自然语言的模型规划、实际执行与下载闭环；组合规划仍保留 60 秒超时限制，不能扩大为复杂请求稳定性保证。仅归档证据并本地提交，不重复无代码变更的全量回归；跨宿主机等既有待验边界保留。

## 2026-10-16：规划预算调整与原组合请求补验（CFK-814）

排查确认，前次 `PLANNING_TIMEOUT` 发生在媒体任务建立前：接受后 60 秒仍无计划或媒体 job。该预算包括规划排队、模型请求、受控纠错、校验和提交；媒体执行另用结构 JSON 的 `task_timeout_seconds`，从媒体任务接受时起计时，默认 **3600 秒**。模型客户端已经跟随规划配置设置超时，没有另一层写死的 60 秒上限；既有失败证据不能进一步定位供应商或模型内部慢阶段。

将服务默认值、`.env.example` 和 Compose 缺省值统一改为 **300 秒**，保留原 `0 < seconds <= 300` 范围。按授权把本机 `.env` 的 `MEDIA_PLANNING_TIMEOUT_SECONDS` 从 60 调为 300，其余字段和凭据保持不变；本地服务重建、重新创建后健康，原生 MCP 读回 300。旧 `.env` 的显式值不会被生成器覆盖，双语[使用说明](../usage/agent.zh-CN.md)补充升级操作，并区分规划预算、媒体任务预算和消费者 `--wait-seconds`。

沿用原请求全文和同一源 fixture，通过当前会话原生 `generate_media` 新提交一次，获得包含 `video.transcode.v1` 与 `video.extract_frames.v1` 的两步计划。两个步骤都引用原始 source，任务实际到达 `succeeded`、执行尝试 1、`inputs_released=true`；安装的消费者技能完成 HTTP 下载并释放本次上传。精确镜像为 `sha256:c23c8b4c22d32bd564be488045d31dffe35f94032d80f9d14a08d686cba6d085`。

| 验证 | 实际结果 |
| --- | --- |
| 原生 MCP 规划与执行 | 接受到媒体 job 创建 **56.486 秒**；job 创建到最后产物发布 **1.328 秒** |
| 三个实际下载文件 | H.264 MP4 **320×180**、0/500 ms 的两张 PNG **160×90** |
| 独立媒体验证 | **32/32 通过**；大小/SHA-256、完整解码、指定源帧比对、24 帧及相对 PTS、音轨属性通过 |
| 本机回归 | Ruff / Pyright 通过；**772 passed in 83.85s** |
| 新增边界覆盖 | 180/300 秒传入模型 HTTP 超时；已建立媒体 job 不被规划期限到期终止；新旧 `.env` 值处理 |
| 凭据与输入保护 | 实际凭据在运行记录及服务 stdout/stderr 中匹配为 0；私有配置未跟踪，源 fixture 摘要未变 |

前后证据见[规划预算与组合请求报告](../verification/planning-timeout-300-validation.json)，原 60 秒失败报告原样保留并记录 SHA-256。56.486 秒包含排队、规划、校验和提交，不是独立模型耗时；它小于旧上限，不能声称本次成功必然依赖超过 60 秒，或保证未来模型延迟。300 秒期限和两类预算隔离有实际读回与定向回归覆盖。

媒体验证保留差异：MP4 标称 24 fps、全部帧的相对 PTS 一致，但平均帧率字段为 `8000/333`，视频绝对起点后移 23 ms；AAC 重编码后容器时长增加 20.991 ms，不能表述为音轨字节或绝对时序原样复制。两张 PNG 与独立源帧重建的平均 RGB 差分别为 0.671/255、0.680/255，最大通道差均为 3。

完整测试首轮因沙箱禁止绑定 loopback 端口而中断，确认原因后在获准权限下重跑全部通过。本轮全量测试在本机运行，真实媒体在上述精确镜像运行；不替代双宿主机等既有待验项。按本轮用户授权配置 GitHub origin，保留并合并远端仅含 MIT LICENSE 的初始化提交，随后正常推送 main；最终提交和远端读回记录于交付及 CFK-814 完成证据。

## 2026-10-16：技能安装、Docker 使用与 GHCR 发布准备（CFK-815）

参考同属 `breakstring` 的 echarts-agent README，将中英文入口分为“连接已有服务”和“自行部署”，补齐双技能完整目录安装、Python 3.10+ 前提、私有连接配置、独立客户端验证，以及源码构建、GHCR 拉取、更新、停止和配置生效步骤。消费者只持有服务地址与 Token；原生 MCP 管理任务，文件字节仍通过 HTTP 传输。[部署说明](../../deploy/README.md#ghcr-镜像发布)记录镜像标签、digest 固定、首次可见性及发布边界。

新增固定官方 Action SHA 的双架构原生工作流：先进行 Ruff、Pyright、技能副本校验、精确生产镜像全量测试和六操作 HTTP/MCP 真实产物检查，再将同一已测镜像保存并交给发布 job。发布时再次校验镜像 ID、架构和源码 revision，不重新构建；只为发布 job 授予 `packages: write`，使用 `GITHUB_TOKEN`。main 发布 `main/latest/sha-完整SHA`，稳定 `vX.Y.Z` 须匹配项目版本且只发布版本与 SHA 标签；PR 不发布，纯文档变更不触发构建，本轮不创建 Git tag。

发现并修复容器测试脚本在消费者技能交付后漏挂载 `consumer/` 与 `skills/` 的问题；打包测试需要的源码视图从精确镜像复制到临时文件系统，pytest 仍显式导入 `/app/src`，没有用宿主源码替代镜像实现。复用现有精确 arm64 镜像，**772 passed in 153.06s**；服务代码和当前运行实例未改变。

首次推送前，本地验证通过 Ruff、Pyright、actionlint 1.7.12、diff 检查；50 个 README/消费者 shell 块、8 个新增部署 shell 块、24 条 CLI 参数、60 个本地链接以及本地/GHCR 两种 Compose 配置均通过。两个技能复制到独立目录后通过 Python `-S` 启动，重复安装和配置覆盖被拒绝，私有权限为 0700/0600；变更文件实际凭据匹配为 0。详见[本地验证报告](../verification/ghcr-local-validation.json)。

GitHub 只读 preflight 确认仓库 Public、Actions 已启用、当前账号 ADMIN、目标 GHCR 包查询 404。此段仅记录发布准备，不将工作流文件存在当作镜像已经可用；远端运行、原生双架构、Package 可见性与匿名拉取结果在实际执行后追加。双宿主机等既有待验项保留。

### GHCR 首次发布与匿名拉取验收

首次 Actions 运行 `38847505294` 在宿主检查环境安装阶段失败：锁定的 uv 0.12.8 尚不提供 managed Python 3.14.8。修正提交 `95020d760de6b866fe2abad8f63887b3829e431e` 仅将 CI 宿主工具环境设为 Python 3.14.7，生产镜像继续使用原定 Python 3.14.8。[后续运行 38847641532](https://github.com/breakstring/ffmpeg-agent/actions/runs/38847641532)三个 job 全部成功。

原生 amd64 镜像 **772 passed in 261.25s**，原生 arm64 镜像 **772 passed in 244.25s**；各自另完成 48 个真实媒体任务（六操作 × 默认/自定义参数 × HTTP/MCP × Token 开/关），合计 96 个唯一任务、112 个下载校验文件。每个架构的 pytest 和媒体报告绑定相同精确镜像 ID，自有容器和卷残留均为 0。验证后发布同一镜像，未重建。

已发布 `ghcr.io/breakstring/ffmpeg-agent` 的 `main`、`latest`、`sha-95020d760de6b866fe2abad8f63887b3829e431e`；多架构清单 digest 为 `sha256:618d759fd0727d6979bc4928b6777a69b5e0996ec69ee866acc4b92037c1fa9a`。匿名 registry GET 返回 200，两个平台 manifest 中的 config digest 均与 CI 已测 image ID 一致。用隔离的空 Docker 配置完整拉取 arm64 成功，源码 revision 一致；本机镜像存储的 inspect ID 是索引 digest，不能直接与 CI config digest 比较。

中英文 README 和部署指南已更新为镜像实际可用。详细摘要、原始报告 SHA-256、发布清单及匿名拉取读回见[发布验收报告](../verification/ghcr-release-validation.json)。完整匿名下载仅在 arm64 执行，amd64 已匿名核对 manifest；本轮不覆盖真实模型、A2A 端到端、HTTPS/代理或双实际业务宿主机验收，不修改现有运行实例或凭据，不创建 Git 版本标签。

### 本地既有服务切换到 GHCR（CFK-815 后续验收）

按用户后续明确授权，将本地 `ffmpeg-agent` Compose 服务切换到上述 GHCR 精确 digest，平台为原生 arm64。先确认媒体、规划、上传、下载引用与发布均无活动项，正常停止后完整备份配置和四个实际数据卷；归档内文件内容、UID/GID、权限及目录结构逐项与停止后的源快照一致，数据库 schema 3、完整性和外键检查通过。私有备份保存在忽略目录 `deploy/local/ghcr-switch-20261016/backup-verified`，未提交配置或数据。

`.env` 仅修改 `MEDIA_IMAGE`；Token、模型配置、300 秒规划预算、功能开关、结构 JSON、端口和原四个卷均保留。镜像自带的 `MEDIA_IMAGE_VERSION` 从 `local` 改为发布提交，其余容器环境逐项摘要相同。服务实现摘要一致；启动后 23 个历史任务、23 个执行尝试、27 个产物、29 个步骤、26 个上传及 9 个规划请求均无缺失或变化，59 个历史输出文件内容及元数据一致。仅运行代际标记随重启改变。

已安装的连接技能验证 HTTP、MCP 22 工具和 A2A 接入正常。媒体技能通过 HTTP 上传本项目 62,056 字节测试视频，宿主原生 MCP 提交 `video.transcode.v1` 缩放至宽 320，任务 `job_f9e10dade3fe48cbb0a28acfd261b2bb` 一次执行成功；技能通过 HTTP 下载 30,736 字节 H.264/AAC MP4，独立验证 320×180、大小和 SHA-256 匹配，并完整解码通过。本次上传已释放，源素材未变，验证后所有旧记录及旧文件仍一致，服务健康、无活动任务，日志中实际凭据匹配为 0。

验收摘要及停机/切换检查调整记录见[本地 GHCR 切换报告](../verification/local-ghcr-switch-validation.json)。本轮验证一个结构化真实媒体闭环；保留自然语言/A2A 配置但不再次调用模型，不代替双实际宿主机验收，也未执行备份恢复演练。

### GHCR 原生 MCP 自然语言组合处理与日志排查

按用户后续要求，通过已安装媒体技能上传同一项目 fixture，使用当前 GHCR 服务的宿主原生 `generate_media` 提交一次自然语言组合请求：H.264 MP4 等比缩放至宽 320、保留源帧节奏与音频内容，并从原始视频 0/500 ms 各提取一张宽 160 的 PNG。`agent_f025340f436e4495a1118d07c9507abe` 于 2026-10-16 05:36:11.644 UTC 接受，300.009 秒后以 `PLANNING_TIMEOUT` 失败，未持久化计划、未创建媒体任务，下载产物为 0；本次闭环验收失败。本次上传已通过技能释放，源 fixture 未变，没有重试模型。

用户要求只读排查 Docker 日志后，核对该时间窗口内 322 条结构化事件：仅有 MCP 工具完成和 HTTP 请求完成，上传为 201，其他接口均为 2xx，26 次健康检查正常；容器无 OOM、无重启，无其他重叠规划请求。诊断时容器内 DNS、TCP/TLS 以及模型列表 API 均成功，模型列表 HTTP 200 且包含所配置模型；这不能证明先前生成请求已正常返回。

代码核对确认：SDK 原始日志静默，服务没有规划阶段及模型请求日志；`generate_media` 的 `mcp_tool_finished/succeeded` 只表示请求已接受。总规划期限覆盖模型调用、可选纠错、本地验证和保存，现有日志不能区分首次模型请求、第二轮纠错或保存等待。建议补任务 ID、阶段、请求序号、耗时、HTTP 状态和固定错误码等受控事件，同时保持请求/响应正文、URL、凭据和原始异常不入日志。本轮未修改服务代码、配置或再次部署，详见[失败验收与诊断报告](../verification/ghcr-native-mcp-natural-language-validation.json)。

## 2026-10-16：分级规划诊断与关闭 Qwen 思考（CFK-816）

按用户后续授权补充规划阶段和模型请求日志，提交 `4070229`。`MEDIA_LOG_LEVEL` 默认 `INFO`，支持 DEBUG/INFO/WARNING/ERROR/CRITICAL；DEBUG 增加状态读取、模型等待响应头/响应体、解析、输出校验、计划保存和媒体接受的阶段、请求序号及耗时。超时、取消、错误使用固定类别，记录最近检查点；`http_headers` 包括建连和发送，不能证明供应商已经收到请求。用户另要求诊断正文，因此新增默认关闭的 `MEDIA_LOG_MODEL_CONTENT`，仅显式开启且处于 DEBUG 时输出经凭据、敏感字段、URL、路径、邮箱和嵌套 JSON 脱敏的正文；超过 128 KiB 整段省略。SDK 原始日志保持关闭。

该精确 arm64 诊断镜像 `sha256:cf077387f6d7e5ca5b404f7b0a059ae3f806f4df53c3ba4a39e2ab1812d7cdca` 内 **820 项测试通过**。同一 fixture 和自然语言组合请求成功：首次模型请求 94.315517 秒，无纠错，计划校验、保存、媒体接受均为毫秒级，媒体执行 1.333 秒；三份产物完整下载并独立验证。实际请求、响应及配置模型均为 `qwen3.8-flash`，未传入 `enable_thinking`，响应含 13,577 字符思考内容及供应商报告的 3,709 reasoning tokens。此次慢点位于模型请求，但无法拆分网络、供应商排队与生成耗时，也不能还原旧 300 秒失败的精确原因。证据见[规划日志诊断报告](../verification/planning-logs-validation.json)，原失败报告保留。

按用户补充要求，提交 `ef3dbc6` 加入 `.env` 配置 `MEDIA_LLM_ENABLE_THINKING=false`；`true` 开启，显式小写 `null` 省略扩展，空值和 `NULL` 拒绝。核对[百炼思考参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)与[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)：SDK 的 `extra_body` 应映射为 HTTP 顶层参数；本轮保持合规的 `prompted/json_object`、含 JSON 关键词和完整 Schema 的提示词及本地 Pydantic/图校验。当前 native 路径实际 `strict:false` 且有两处 `patternProperties`；强制 OpenAI strict 会破坏动态输入/输出和参数，不能仅切开关声称百炼严格 Schema 兼容。保留现有 4096 输出预算及截断输出校验，供应商原生 Schema 适配留待独立实施与验收。

| 同一自然语言组合请求 | 省略思考开关 | 显式关闭思考 |
| --- | --- | --- |
| 模型调用次数 / 纠错 | 1 / 0 | 1 / 0 |
| 模型请求耗时 | 94.315517 秒 | 7.515839 秒 |
| 响应思考正文 | 13,577 字符 | 未返回该字段 |
| 媒体执行耗时 | 1.333 秒 | 3.06474 秒 |
| 三个下载文件 | 独立验证通过 | 独立验证通过，大小与 SHA-256 均与前次相同 |

关闭思考后实测模型耗时减少 92.03%，为各一次观测，不是延迟承诺。脱敏请求副本除新增 `enable_thinking:false` 外一致；实际响应 HTTP 200、finish_reason=stop，生成 H.264/AAC MP4 320×180 与 0/500 ms 两张 PNG 160×90。完整解码、24 帧相对 PTS、音频内容对齐及 PNG 独立重建误差检查通过。最终源码在宿主和精确镜像 `sha256:8e3e3ebcbc4a8e78f1d650367b930ab5ad001a2fccc89744e80a5ed8a8ec4244` 内均 **843 项测试通过**；Ruff、Pyright、消费者副本同步检查通过。报告记录实际阈值和边界，见[关闭思考验收与对比](../verification/qwen-thinking-off-validation.json)。

两次本地更新前均确认无活动工作，正常停止并逐项校验四卷备份，再复用原卷、端口和安全配置。第二次备份首次误用系统 Python 3.9，脚本自动恢复原容器；确认缺少 `hashlib.file_digest` 后使用项目 Python 3.14 重做，完整备份通过才切换。最终 25 个历史任务、25 个尝试、30 个产物、32 个步骤、29 个上传和 11 个规划请求均未改变，96 个历史输出目录/文件条目保持一致；新增一项成功组合任务。本次上传已释放，服务健康、无活动工作，INFO 日志和正文关闭已恢复，思考保持关闭；其他配置和凭据不变。仅本地提交和部署，未 push 或发布新 GHCR 镜像；双实际宿主机及备份恢复演练等待办不变。
