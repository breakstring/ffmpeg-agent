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
