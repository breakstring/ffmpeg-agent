# 六类媒体能力参数合同

状态：Frozen（当前实施合同；实现与验收状态分别以 cfKanban 和[实施进度](../plans/v1-implementation-progress.md)为准）\
日期：2026-10-15\
适用项目：`ffmpeg-agent`\
关联：[首版设计](media-service-v1.md) · [技术选型](technology-selection-v1.md) · [实施计划 P13～P17](../plans/v1-implementation-plan.md)

本项目提供六类通用媒体能力。最简单的调用只传输入和幂等键，服务使用通用默认值；需要指定编码、尺寸、帧率、采样率或质量时再传 `params`。HTTP 与远程 MCP 共用同一参数模型、默认解析、校验和执行逻辑。10-bit、HDR、色调映射、硬件编解码及硬件调度留待后续。

## 接口与默认值

HTTP 只提供以下六个操作：`media.probe.v1`、`video.transcode.v1`、`audio.transcode.v1`、`video.extract_audio.v1`、`video.extract_frames.v1`、`image.resize.v1`，均直接支持本文参数。HTTP 路径为 `/v1`。MCP 对应 `probe_media`、`transcode_video`、`transcode_audio`、`extract_audio`、`extract_frames`、`resize_image`，使用相同 `params`；参数可省略，不需要额外版本选择。通用任务图工具也复用这六个操作。

| 能力 | 省略 `params` 时的行为 |
| --- | --- |
| 探测 | 返回 format、streams 基础报告，不扫描统计帧数 |
| 视频转码 | H.264/MP4、CRF 23、medium，保留源尺寸和帧时间节奏；音轨转 AAC，采样率/声道/码率自动选兼容值 |
| 音频转码/音轨提取 | AAC/M4A，采样率和声道优先保持源值，必要时选兼容值；码率根据实际声道及编码器限制自动选择 |
| 抽帧 | 取归一化时间 0ms 的首帧，保持尺寸，JPEG quality 90 |
| 图片转换 | 保持源尺寸，JPEG quality 90 |

`preset` 默认 `null`。`web_720p_v1`、`aac_stereo_128k_v1`、`wav_mono_16k_v1` 作为可选快捷设置保留：选择后应用该预设的默认规格，再由显式参数覆盖。预设名中的版本用于固定该组快捷设置，不是 API 或 MCP 的另一套参数协议。

- 省略字段使用通用默认；选定预设后，未显式覆盖的字段使用预设默认。调用方只需提供希望改变的参数。
- 音频 `auto` 优先保持源采样率或声道，源值不适用于目标编码时选兼容值；`source` 表示严格保持，无法支持就明确失败。显式数字同样严格校验。自动采样率在源值未知或不兼容时回落到 48000 Hz；自动声道在源值未知、超出支持范围或不适用于 MP3/Opus 时回落到双声道。
- 切换编码器时重新解析该编码器的专属默认，不遗留另一编码器的 CRF、速度或码率设置。HEVC 默认 MP4，VP9/AV1 默认 WebM；WebM 默认音频为 Opus。显式不兼容组合拒绝，不静默改写。
- `rate_control` 按整个对象替换，CRF 与目标码率互斥。不接受未知字段、任意 FFmpeg 参数或自定义滤镜字符串。
- 接受任务时冻结操作、预设及规范化有效参数，保存在现有任务定义，不增加数据库迁移、进程或服务。公开参数不包含输入 source、路径、URL、认证头或凭据。
- 依赖输入的 `auto`、`source`、轨道和 copy 兼容性在安全探测后解析，实际值写入产物；恢复使用同一已保存策略和不可变发布检查点。

最简 MCP 视频转码参数如下，`file_id` 来自 HTTP 上传：

```json
{
  "source": {"kind": "uploaded_file", "file_id": "<file_id>"},
  "idempotency_key": "convert-001"
}
```

HTTP 对应 `POST /v1/jobs`，通过请求头提供幂等键：

```json
{
  "inputs": {"source": {"kind": "uploaded_file", "file_id": "<file_id>"}},
  "steps": [{
    "id": "convert",
    "operation": "video.transcode.v1",
    "inputs": {"video": {"source": "source"}}
  }],
  "outputs": {"video": {"step": "convert", "output": "video"}}
}
```

## 六类能力

### 媒体探测

`media.probe.v1` 开放 `sections`、`stream_indices`、`count_frames`：

| 字段 | 语义与默认 |
| --- | --- |
| `sections` | 从 `format`、`streams`、`tags`、`chapters` 中选择，默认 `format` 和 `streams` |
| `stream_indices` | 可选的容器流索引列表；省略表示所有允许探测的流，未知索引明确报错 |
| `count_frames` | 默认 `false`；启用时实际扫描统计帧数，计入任务总时限和资源预算 |

输出仍为 `report` JSON 单文件。报告必须结构化脱敏，不包含输入文件名、绝对路径、来源信息或原始 ffprobe 正文。标签只返回经过格式校验的 `language` 和 `creation_time`；禁止透传任意 `title`、`comment` 等可能包含敏感信息的内容。未知标签不能因为用户选择了 `tags` 就全部返回。章节同样按受控字段输出，不透传任意标签。

### 视频转码

`video.transcode.v1` 使用 `video` 单文件输出；默认 `preset=null`，采用上述通用设置。

| 字段 | 开放范围与默认 |
| --- | --- |
| `container` | `mp4`、`mkv`、`webm`；通用默认 MP4，切换编码器时推导兼容容器 |
| `codec` | `h264`、`hevc`、`vp9`、`av1`；只有已经适配且运行时探测可用的软件编码器才能被宣告支持 |
| `video_stream` | 按视频轨道顺序选择，从 0 开始，默认 0 |
| `resize` | 默认 `{}` 保持源尺寸，`contain`、不放大；可只指定宽或高 |
| `frame_rate` | 默认 `{"mode":"source"}` 保持源帧时间节奏；支持 `source`、`constant`、`max`，后两者使用正有理数 `value` |
| `rate_control` | `{"mode":"crf","value":...}` 或 `{"mode":"bitrate","bitrate_bps":...,"maxrate_bps":...,"buffer_size_bits":...}`；后两项为可选 |
| `speed` | `fast`、`medium`、`slow`，按编码器适配到具体设置；默认 `medium` |
| `audio` | `drop`、`copy`、`encode` 三种模式；默认选择首条音轨转 AAC，采样率、声道和码率为 auto；编码模式复用音频参数 |
| `range` | `start_ms` 默认 0，`end_ms` 可省略；用于截取完整媒体时间段，省略则处理全部 |

`resize` 使用 `width`、`height`、`fit`（`contain`/`cover`/`pad`）、`allow_upscale`、`background`、`crop_x`、`crop_y`；焦点范围为 0～1，默认居中 0.5。`contain` 等比装入、不补边；`cover` 等比填满后按焦点裁剪；`pad` 等比装入并补背景。单边尺寸等比计算；需要目标框的 `cover`/`pad` 必须给出宽高。禁用放大时不偷偷拉伸源图像；不能满足目标框及编码尺寸约束的组合明确拒绝。编码要求的偶数尺寸调整必须有稳定规则并在结果中返回实际尺寸。

`source` 帧率保留输入时间节奏，`constant` 输出指定固定帧率，`max` 只限制最高帧率而不主动补帧。帧率支持 `30000/1001` 等精确分数，不以浮点近似作为公开合同。字符串只接受有界正整数、十进制小数或整数分数，每段最多 9 位且不接受指数记法；规范化后的分子、分母均不超过 999999999，显式 constant/max 值另受部署 `max_frame_rate` 约束（默认 240 fps，可配置）。source 模式不受该参数限制，保留源帧时间节奏。数值输入必须为有限正值；先限制表示范围再执行分数解析，防止微小请求触发无界大整数计算。质量取值范围按编码器声明；VP9/AV1 的 CRF 必须为整数。码率、峰值码率、缓冲区按明确单位校验；峰值码率与缓冲区必须成对提供，峰值不得低于目标码率；本轮 AV1 目标码率模式不支持峰值/缓冲组合，明确拒绝。

产物的 `media.streams[].frame_rate` 是容器报告的标称帧率，并不表示逐帧测得的时间间隔。尤其 WebM 的 `max` 模式可能保留源标称值，不能仅据该字段判定限帧策略失效或实际帧率超限；请求和最终生效的策略见 `effective_params.frame_rate`，需要验证实际时间节奏时应检查解码帧时间戳。

本轮视频输出固定 8-bit `yuv420p`，保持现有 SDR 输入边界。AV1/libsvtav1 要求至少 64×64 输出像素，低于该尺寸明确拒绝，不偷偷放大。profile、其他像素格式、位深和色彩转换不在本轮范围。容器、视频编码和音轨编码组合由明确兼容表校验；更换编码器不意味着所有容器自动可用，不能静默换容器或丢音轨。

### 音频转码与提取音轨

`audio.transcode.v1` 与 `video.extract_audio.v1` 共用音频参数和 `audio` 单文件输出；提取视频音轨仍要求存在符合条件的音轨。

| 字段 | 开放范围与默认 |
| --- | --- |
| `preset` | 默认 `null`；可选 `aac_stereo_128k_v1` 或 `wav_mono_16k_v1` 快捷设置 |
| `audio_stream` | 按音轨顺序选择，从 0 开始，默认 0 |
| `mode` | `encode`（默认）或 `copy` |
| `container` | `m4a`、`mp3`、`ogg`、`flac`、`wav`、`mka`；`copy` 未显式指定时默认 `mka` |
| `codec` | `aac`、`mp3`、`opus`、`flac`、`pcm`，以实际适配且可用的编码器为准 |
| `sample_rate` | 默认 `auto`，也可传正整数或 `source`；Opus 目标为 48000 Hz，auto 可重采样，source 必须原本兼容 |
| `channels` | 默认 `auto`，也可传声道数或 `source`；与显式布局的声道数必须一致 |
| `channel_layout` | 可选的受控布局枚举；不接受任意滤镜或表达式 |
| `sample_format` | PCM 支持 `s16`、`s24`、`f32`；其他编码不接受不适用的显式值 |
| `rate_control` | 有损编码使用 `bitrate`、已适配的 `quality` 或待探测后解析的 `auto`；无损编码为 `null`，不接受虚假的目标有损码率 |
| `compression_level` | FLAC 编码压缩级别；与有损画质分开 |
| `range` | encode 模式可选 `start_ms` / `end_ms`，省略处理完整音轨 |

有损码率还须与采样率及声道兼容。AAC 的 `bitrate_bps` 不超过 `6 × sample_rate × channels`。MP3 使用以下受控码率表，单位为 kbps（接口仍传 bps）：

| MP3 采样率 | 合法目标码率（kbps） |
| --- | --- |
| 8000、11025、12000 Hz | 8、16、24、32、40、48、56、64 |
| 16000、22050、24000 Hz | 8、16、24、32、40、48、56、64、80、96、112、128、144、160 |
| 32000、44100、48000 Hz | 32、40、48、56、64、80、96、112、128、160、192、224、256、320 |

有损音频的通用码率控制为 `{"mode":"auto"}`。安全探测并解析目标采样率和声道后，按以下首选值计算，再按编码器合法范围选择兼容码率：

| 编码 | auto 首选目标码率 |
| --- | --- |
| AAC | 64000 × 实际输出声道数 bps，再应用 AAC 上限 |
| MP3 | 96000 × min(实际输出声道数, 2) bps，再向下选该采样率支持的合法码率 |
| Opus | 单声道 64000、双声道 96000、多声道 64000 × 声道数 bps，再应用编码器限制 |

实际目标码率写入产物 `media.effective_params.resolved.rate_control`；视频内音轨位于 `media.effective_params.resolved.audio.rate_control`。它是编码目标，不保证 AAC 成品的平均码率精确相等。具名预设可提供明确码率；显式参数覆盖预设，省略预设时使用通用 auto。

显式 `bitrate` 必须按同一组合规则校验；不兼容就拒绝，不能改成 auto 或依赖引擎自行调整后仍报告成功。`source` 值由执行时解析，不能绕过兼容规则。能力接口公开 MP3 采样率对应的码率表与 AAC 上限公式。

Opus 本轮固定使用编码器支持的 48 kHz，允许 1/2/3/6/8 声道及 mono/stereo/3.0/5.1/7.1 布局。`source` 必须在探测后满足同一兼容范围；不是绕过编码器约束的开关。

`copy` 直接复制所选压缩音轨，不重新编码；拒绝显式编码、质量、重采样、声道变换和裁剪参数。不得执行与 copy 冲突的预设编码变换。执行时探测源音轨编码并验证目标容器兼容性，失败返回稳定错误。`copy` 不承诺未经实现和验证的所有音频编码都能封装。视频转码内嵌音轨的 copy 同样必须通过目标视频容器兼容校验。

### 视频抽帧

`video.extract_frames.v1` 使用 `frames` 集合输出，开放视频轨道、采样方式、帧选择策略和图片输出参数。

| 字段 | 语义 |
| --- | --- |
| `video_stream` | 默认 0，按视频轨道顺序选择 |
| `sampling` | 默认 `{"mode":"timestamps","timestamps_ms":[0]}`；支持以下三个互斥对象之一：`timestamps` + `timestamps_ms`；`interval` + `start_ms`/`end_ms`/`interval_ms`；`count` + `start_ms`/`end_ms`/`count` |
| `frame_selection` | `next`（默认）、`previous`、`nearest` |
| `image` | 复用下一节的图片参数，默认不改尺寸；JPEG 默认质量 90，可覆盖 |

`start_ms` 默认 0，`end_ms` 可省略，默认取最后一张可解码帧的归一化时间。interval 从开始点按间隔生成不超过 end 的请求点；count 在指定区间均匀生成指定数量，包含首末端点，count=1 取开始点。请求点先生成再进行帧选择，不按成功张数反向补齐。所有帧选择策略对落在 `[0, 最后一帧时间]` 之外的请求都返回逐项越界，不用最接近边界的帧掩盖越界。端点和整数毫秒取整规则进入确定性测试，不能用输出文件数代替时间语义验证。

时间轴以首张可解码视频帧归零。`next` 选不早于请求点的第一帧，`previous` 选不晚于请求点的最后一帧；`nearest` 距离相等时选之前的帧。保留请求顺序和重复项，每项返回 `requested_time_ms` 与 `actual_time_ms` 或稳定错误；部分成功、不可变集合、禁止补写已发布成员等规则沿用。

抽帧上限由部署参数 `max_frame_count` 控制，默认 100，可调整。生成请求总数在执行输出前核对，超过限额明确拒绝，不能截断列表。图片格式与质量使用共用图片参数，默认 JPEG quality 90。

### 图片缩放与格式转换

`image.resize.v1` 使用 `image` 单文件输出，参数也供抽帧复用。

| 字段 | 开放范围与默认 |
| --- | --- |
| `width` / `height` | 可单边指定，另一边等比计算；都省略则保留尺寸，仅转换格式或编码质量 |
| `fit` | `contain`（默认）、`cover`、`pad`；双边框语义与视频一致 |
| `allow_upscale` | 默认 `false` |
| `background` | 受控颜色值，用于补边及透明转 JPEG |
| `crop_x` / `crop_y` | 裁剪焦点 0～1，默认 0.5 |
| `resampling` | `lanczos`（默认）、`bicubic`、`bilinear`、`nearest` |
| `format` | `jpeg`、`png`、`webp` |
| `quality` | JPEG 1～95、默认 90；WebP 0～100、默认 85；PNG 不接受有损质量 |
| `compression_level` | PNG 0～9、默认 6 |
| `jpeg_subsampling` | 0、1、2，默认 2 |
| `progressive` | JPEG 渐进输出，默认 `false` |
| `lossless` / `method` | WebP 无损开关默认 `false`，压缩效率档位默认 4 |

按输出格式校验字段适用性，显式给了不适用的参数就报错。PNG 压缩级别和 WebP method 影响压缩效率，不伪装为统一画质刻度。图片仍限定静态、8-bit、SDR；保留 EXIF 方向修正、透明语义与元数据清除，不透传任意标签。像素数、输出量和处理时间继续受服务限额约束。

## 能力发现、执行与兼容

MCP `tools/list` 的六个媒体工具描述直接说明通用默认、主要可选值和组合约束，`inputSchema` 内的 `params` 提供字段说明、枚举、单位、范围、示例及默认规则。HTTP OpenAPI 和能力接口复用同一参数模型；调用方可直接从工具描述和 schema 选择参数，无需先阅读 README。通用任务图的 `steps` schema 通过 `oneOf` 按 `operation` 关联对应的完整参数模型，适用于 HTTP 普通任务、multipart 内任务 JSON 与 MCP `submit_job`。

schema 的 `default` 表示字段的普通默认，需结合预设、父对象及字段说明理解；依赖编码、格式、模式、容器或其他字段的默认分别使用 `x-default-by-codec`、`x-default-by-format`、`x-default-by-mode`、`x-default-by-container`、`x-default-by-value` 描述。视频 `audio` 的 `x-default-by-container` 给出各容器对应的完整规范化音频默认。条件默认不使用误导性的 `default: null`。这些是供调用方理解的默认规则，不要求客户端自动补齐；省略字段与显式 `null` 不可互换，仍以各字段说明和校验为准。只发送需要改变的字段，避免把另一编码或格式的默认值带入请求。

`capabilities` 同时给出六个操作的说明、参数 schema、预设、软件编码器可用性、容器兼容组合、质量/采样率等取值边界和部署限额。`operations` 每项提供 `description`、`params_schema` 和 `default_params`；后者是整个 `params` 省略、未选择预设时的规范化默认值，例如 JPEG quality 90，不是可套用于所有参数组合的模板。schema 枚举描述已适配范围；实际支持集合取“已实现适配 ∩ 运行时探测可用”，选择可选编码器还须检查 `media_parameters`，不能把 FFmpeg 列出的所有编码器直接当作可调用能力。

基础能力所需引擎缺失影响就绪；可选编码器缺失只使相应能力不可用，并明确拒绝选择该编码器。不得改用另一编码器、自动降低分辨率、忽略显式质量参数或减少抽帧数来伪造成功。软件编码器设置由结构化参数生成无 shell 的命令数组，继续使用安全 FD、单媒体进程槽、超时取消及有界输出。

HTTP 普通 JSON、multipart 内任务 JSON、MCP 六个语义工具及通用任务图工具共享同一参数解析。应分别验证默认省略、显式默认、组合覆盖和非法参数的等价性；不能只比较成功状态。公开任务结果返回规范化有效参数和实际媒体信息；不得为了展示参数而公开原始任务 source。

## 实施拆分与验收要求

动态 backlog 位于 cfKanban KZ / ffmpeg-agent，本文件只保存合同，不复制任务状态。P13～P17 的详细工作包见[实施计划](../plans/v1-implementation-plan.md)。

- P13（CFK-794）：共享模型、规范化、兼容规则、有效参数保存及能力发现；验证六个操作的通用默认、可选预设及省略参数调用。
- P14（CFK-795）：音视频参数化与 copy；验证编码、尺寸、帧率、码率/质量、采样率、声道及容器兼容错误。
- P15（CFK-796）：图片参数化、三种抽帧采样与帧选择、探测范围和扫描；验证时间、像素、透明、标签过滤及限额。
- P16（CFK-797）：HTTP/MCP 参数与错误一致，更新机器合同和中英文使用说明。
- P17（CFK-798）：真实媒体矩阵、重启/取消及精确镜像闭环；只据实际证据声明支持，不将本地结果扩大为双宿主机或另一架构通过。

后续单列两个方向：10-bit/HDR/色彩转换完整链路（CFK-799），以及硬件编解码与设备资源管理（CFK-800）。本轮既不开放未经验证的开关，也不把它们列入 P17 必须完成项。现有真实两宿主机及原生架构验收的未验证条件继续保留。
