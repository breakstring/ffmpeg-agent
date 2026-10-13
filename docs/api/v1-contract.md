# 首版 API 与媒体边界合同

状态：P0 实施合同；实现和验证结论见[实施进度](../plans/v1-implementation-progress.md)。

本文细化[首版设计](../design/media-service-v1.md)、[技术选型](../design/technology-selection-v1.md)与[实施计划](../plans/v1-implementation-plan.md)中的首版边界。请求模型以 [`models.py`](../../src/media_service/models.py) 为实现入口，部署限制以 [`config.py`](../../src/media_service/config.py) 为入口。后续由实际 API 导出 OpenAPI 与 `GET /v1/capabilities`，文档中的字段列表不代替运行时 schema 或验收证据。

## 请求与幂等

请求字段采用 `snake_case`，不接受未知字段或未知操作版本。整数和布尔参数不互相转换，也不接受数字字符串。命名输入、步骤、输出及存储标识匹配 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`；ID 由服务生成，调用方不依赖其中的编码规则。

任务包含 1～4 个命名输入、1～16 个步骤和 1～16 个命名输出。步骤输入使用 `{"source":"输入名"}` 或 `{"step":"步骤名","output":"端口名"}`。每个首版操作恰好使用一个指定输入端口。重复步骤 ID、未知引用、错误端口、类型不兼容、循环、未使用输入和无用途步骤在接受前拒绝。依赖深度上限为 16；请求中的步骤顺序不必为拓扑顺序。

声明输出必须来自步骤输出。未声明的中间结果仅供本任务使用；每个步骤必须直接或间接服务于至少一个声明输出。下游默认不接受部分成功结果；首版六个操作均不接受集合输入，也不将 probe 的 JSON 报告作为媒体输入。

幂等键在调用方作用域内生效。规范化请求补齐默认值、排序对象键，保留数组顺序；源请求头名称先转为小写。URL、认证头、获取期限、预期字节数和预期 SHA-256 均参与摘要，相同键但不同规范化请求返回 `409 IDEMPOTENCY_CONFLICT`。摘要不代表可以公开或记录原始敏感字段。重放返回原任务和当前状态，不重新执行媒体操作。

## 输入引用

| 类型 | 必填字段 | 可选字段与限制 |
| --- | --- | --- |
| `shared_file` | `storage_id`、`key` | `expected_size_bytes` 为非负整数；`expected_sha256` 为 64 位小写十六进制 |
| `http_file` | `url` | `headers`、`expires_at`、`expected_size_bytes`、`expected_sha256` |
| `artifact` | `artifact_id` | `member_key` 选择集合内已发布的单个成员 |

`key` 是存储区域内相对路径，最多 1024 字符；拒绝绝对路径、反斜线、NUL、空路径段、`.` 和 `..`。实际读取仍须使用安全目录 FD 和已验证的普通文件描述符，字符串检查不能替代文件系统边界。共享输入由调用方保持不变，只读借用，服务不删除它。

HTTP URL 最长 8192 字符，来源的协议、主机、端口、实际连接地址及重定向受调用方策略限制。受控头仅接受 `authorization` 和 `x-api-key`；名称大小写不敏感，不接受重复名称、CR/LF 或超过 4096 字符的值。`expires_at` 使用有效 UTC RFC 3339 时间，属于获取期限提示，来源的真实响应仍需验证；不能根据单个 `403` 判定 URL 过期。

HTTP 输入一旦完成获取和校验，后续步骤及恢复使用固定副本。请求不接受任意引擎参数、本地文件协议或媒体内引用的外部资源。来源凭据和签名 URL 不出现在公开查询、普通日志或公共错误中。

## 操作端口与参数

| 操作 | 输入端口 → 输出端口 | 参数 |
| --- | --- | --- |
| `media.probe.v1` | `media` → `report` | 空对象；返回 JSON 报告文件 |
| `video.transcode.v1` | `video` → `video` | `preset` 默认且仅支持 `web_720p_v1` |
| `audio.transcode.v1` | `audio` → `audio` | `preset` 默认 `aac_stereo_128k_v1`，另支持 `wav_mono_16k_v1`；`audio_stream` 默认 0，范围 0～15 |
| `video.extract_audio.v1` | `video` → `audio` | 与音频转码相同；无所选音轨时明确失败 |
| `video.extract_frames.v1` | `video` → `frames` | `timestamps_ms` 为 1～100 个整数，逐项范围 0～86400000；`format` 为 `jpeg` 或 `png`，默认 `jpeg` |
| `image.resize.v1` | `image` → `image` | `width`、`height` 均必填且为 1～8192 的整数；`fit` 为 `contain` 或 `cover`，默认 `contain`；`allow_upscale` 默认 `false`；`format` 为 `jpeg`、`png` 或 `webp`，默认 `jpeg`；`background` 默认为 `#ffffff`，接受六位十六进制 RGB |

`audio_stream` 是输入媒体内从 0 开始的音频轨道序号，不是所有流的全局序号。操作最终还受部署的输入大小、媒体时长、像素、输出空间和总时限约束。创建期可判定的不支持版本、端口和参数返回 `422`；真实媒体不满足要求时写入执行结果。

## 固定预设与媒体语义

| 预设 | 固定行为 |
| --- | --- |
| `web_720p_v1` | MP4、H.264/libx264、yuv420p；等比装入 1280×720，不放大，采用有效偶数尺寸；CFR 30fps、CRF 23、medium、faststart；有音频时转 AAC 128k、48kHz、双声道 |
| `aac_stereo_128k_v1` | M4A、AAC 128k、48kHz、双声道 |
| `wav_mono_16k_v1` | WAV、PCM s16le、16kHz、单声道 |

同名预设不随升级改变语义。任务接受时冻结操作、预设与执行版本要求，能力不足或不兼容时明确失败；不静默换编码器、copy 音轨或更改画面策略。

视频首版不支持 HDR 或 10 bit 处理，不做隐式色调映射；非方形 SAR 输入拒绝。旋转按 90° 整数倍元数据自动校正画面方向；不能按该规则处理的方向明确拒绝。输出预设的尺寸约束作用于规范化后的画面。每种边界须有真实媒体证据，probe 对源媒体的描述不等于已支持其转换。

抽帧以第一张可解码视频帧的 PTS 为零点。用原始 PTS 与 time base 的有理数计算规范化播放时间，选择第一张实际时间不早于请求毫秒时间的可解码帧；不能先舍入到毫秒再比较。缺失、不可解析或无法构成有序展示时间轴的 PTS 明确拒绝。返回 `actual_time_ms` 时四舍五入到整数毫秒，恰好半毫秒向上取整。

抽帧结果按请求顺序保存，重复请求点也有独立索引。无命中或越界点返回逐项错误；有可用帧时可发布部分成功集合，全部失败不创建伪可用集合。文件名或序号不作为实际时间证据。已发布集合和成员清单不可原地补写。

图片仅支持静态 JPEG、PNG 和 WebP，拒绝动画、多页和超限像素。先应用 EXIF 方向，再执行缩放；`contain` 等比装入目标尺寸，`cover` 等比填满并裁剪。禁止放大时，无法满足 `cover` 所需尺寸则拒绝。有效 RGB ICC 转换至 sRGB；无效 ICC 或不支持的颜色空间明确失败。透明图转 JPEG 先按 `background` 合成。输出去除 EXIF/GPS 等无关元数据，不复制未经处理的源色彩描述。

## 产物消费、保留与租约

`output_policy.storage_id` 省略时使用调用方的默认输出区域；指定时校验授权。`retention_seconds` 默认 86400 秒，schema 范围为 1～604800 秒，同时受部署允许的最大值约束。创建时不能静默缩短保留承诺，保留期从正式发布开始。

显式租约的 `duration_seconds` 默认 900 秒，schema 范围为 1～3600 秒，实际期限还受部署配置和累计租约上限约束。产物进入 `retiring` 后拒绝新引用及续租，原有效租约仍可在范围内读取；读取请求通过 `X-Artifact-Lease-Id` 携带租约 ID。幂等释放与提前 release 不强制中断已经开始的有效读取。

单文件内容和集合成员内容支持 GET、HEAD、单区间 Range、ETag 与 If-Range。合法多区间 Range 回退完整 `200`，不实现 multipart/byteranges。非法或不满足的区间按首版错误规则返回，不暴露容器路径。集合不自动打包，成员分页按不可变清单顺序返回，逐项索引不随分页变化。

HTTP 发送取得内部读取引用，保护持续到真实发送结束、断开或超时后的收尾完成；仅释放响应等待协程不代表发送已停止。内部任务引用和显式租约遵守同一回收门槛。只有服务登记且保留期、发布状态和所有有效引用均允许回收的文件，才能进入定向清理。

## 状态与错误

任务状态为 `queued`、`running`、`canceling`、`succeeded`、`partial`、`failed`、`canceled`；运行阶段为 `acquiring_inputs`、`processing`、`publishing`。失败依赖跳过，独立分支继续；按声明输出计算终态。无法可靠计算百分比时 `progress` 为空。

取消先持久化意图，再停止并回收进程组、等待实际文件工作结束并关闭句柄，最后一致提交终态、`inputs_released=true` 和内部输入引用释放。执行租约过期不能证明旧进程已停止。发布检查点、已发布 `artifact_id` 与任务输出绑定不可由恢复重试替换。

公共错误结构为 `code`、脱敏 `message`、`retryable`、`request_id` 和可空的 `details`，HTTP 响应统一包含外层 `error`。抽帧全部失败时 `details.items` 保留逐项错误。HTTP 状态与稳定错误目录遵循[首版设计](../design/media-service-v1.md#错误语义)。实际合同见 [OpenAPI](v1-openapi.json)、[请求模型](JobCreate.json)、[任务响应](JobResult.json)、[产物响应](ArtifactResult.json)、[成员响应](MembersResult.json) 和 [错误响应](ErrorEnvelope.json)。重新导出：`PYTHONPATH=src .venv/bin/python scripts/export_contract.py`。验收状态以实施进度为准。
