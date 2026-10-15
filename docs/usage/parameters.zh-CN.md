# 媒体参数参考

[English](parameters.md) | **简体中文**

HTTP 与 MCP 使用相同的 `params`。所有能力都可以省略它；下面列出需要调整时可填写的值和组合。JSON 字符串区分大小写，请使用表中的小写拼写。

## 支持范围由什么决定

实际可用范围是 **本服务已适配的功能 ∩ 运行引擎提供的功能**，再根据容器兼容性、输入媒体特征和部署限额校验。

- **服务定义参数范围。** 例如视频只开放 `h264`、`hevc`、`vp9`、`av1`，不能传任意 FFmpeg 编码器名。即使 FFmpeg 支持其他编码，本服务也不会自动开放。
- **FFmpeg 的构建决定底层音视频能力。** 安装同一个 FFmpeg 版本，也可能因为编译选项或链接的库不同而提供不同编码器。服务启动时探测实际编码器和封装器，只公布已适配且可用的组合。[FFmpeg 编码器文档](https://ffmpeg.org/ffmpeg-codecs.html#libsvtav1)说明了外部编码库的构建要求。
- **Docker 部署使用容器内的引擎和库。** 在宿主机另装 FFmpeg 不会改变容器能力；更换镜像、架构对应的镜像构建或容器内依赖后，应重新查询能力。直接本机运行时，则取决于服务实际配置的 FFmpeg 可执行文件和 Python 环境。
- **当前使用软件编码。** 主机 CPU、内存和服务限额影响处理速度与可接受规模；本轮未开放 GPU、10-bit/HDR，不会因为主机有显卡就自动启用硬件编码。
- **图片输出由 Pillow 处理。** JPEG、PNG、WebP 及其相关依赖属于服务运行环境；抽帧先由 FFmpeg 解码，再使用共用图片处理参数输出。FFmpeg 的全部图片编码器列表不等于本服务的图片格式列表。

下面是已适配的参考范围。某个部署是否实际可选，以 HTTP `GET /v1/capabilities` 或 MCP `list_capabilities` 返回为准；必需引擎组件缺失会阻止服务就绪，可选编码器缺失只影响对应选项。

## 编码和格式不是同一个参数

`codec` 指音视频数据的编码方式；`container` 指输出文件的封装格式。例如 MP4 可以装 H.264 视频和 AAC 音轨，MKV 也可以装 H.264。扩展名本身不会改变处理结果。

图片使用 `format`，例如 `"format":"jpeg"`。不要给视频传 `format`，也不要把 `libx264`、`H.265`、`jpg` 当成对应参数的值：本服务使用 `h264`、`hevc`、`jpeg`。

## 视频编码与容器

适用于视频转换的顶层 `codec`、`container`。

| `codec` | 含义 | 可配 `container` | 省略容器时 | 省略质量设置时 |
| --- | --- | --- | --- | --- |
| `h264` | H.264 / AVC，默认编码 | `mp4`、`mkv` | `mp4` | CRF 23 |
| `hevc` | H.265 / HEVC | `mp4`、`mkv` | `mp4` | CRF 23 |
| `vp9` | VP9 | `webm`、`mkv` | `webm` | CRF 32 |
| `av1` | AV1 | `webm`、`mp4`、`mkv` | `webm` | CRF 32 |

当前适配的软件实现分别是 libx264、libx265、libvpx-vp9、libsvtav1；调用方填写左列的公共参数值。当前 AV1 输出要求宽高至少 64 像素。视频输出限定 8-bit SDR、`pixel_format: "yuv420p"`。

视频里的音轨参数放在 `audio` 对象中：

| 视频 `container` | 支持的 `audio.codec` | 省略音轨编码时 |
| --- | --- | --- |
| `mp4` | `aac`、`mp3` | `aac` |
| `mkv` | `aac`、`mp3`、`opus`、`flac`、`pcm` | `aac` |
| `webm` | `opus` | `opus` |

例如 `{"codec":"vp9"}` 会选 WebM、CRF 32 和 Opus 音轨。`{"codec":"h264","container":"webm"}` 是不兼容组合，会被拒绝。表格是本服务开放的组合，并不代表各容器标准的全部能力；输出是否可播放还取决于消费端支持的编码。

## 音频编码与容器

适用于音频转换和音轨提取。视频内部音轨也支持这些编码参数，但容器兼容性以上一张表为准。

| `codec` | 含义 | 可配 `container` | 省略容器时 | 常用设置与约束 |
| --- | --- | --- | --- | --- |
| `aac` | AAC，有损，默认编码 | `m4a`、`mka` | `m4a` | 常用 44100 / 48000 Hz；码率默认 `auto` |
| `mp3` | MP3，有损 | `mp3`、`mka` | `mp3` | 只支持单/双声道；码率取值与采样率有关 |
| `opus` | Opus，有损 | `ogg`、`mka` | `ogg` | 输出采样率固定 48000 Hz；默认 `auto` 可重采样 |
| `flac` | FLAC，无损压缩 | `flac`、`mka` | `flac` | 当前输出 16-bit；`compression_level` 为 0～12，默认 5 |
| `pcm` | PCM，未压缩采样 | `wav`、`mka` | `wav` | `sample_format` 为 `s16`（默认）、`s24`、`f32` |

`mka` 是 Matroska 音频容器，`ogg` 在本服务的音频输出中用于 Opus。FLAC 的“无损”描述编码方式；本服务当前固定的 16-bit 输出不承诺对更高位深源素材逐位保真。PCM 的 `s16` / `s24` / `f32` 分别是 16-bit 整数、24-bit 整数、32-bit 浮点采样。

| 参数 | 可用值与说明 |
| --- | --- |
| `mode` | `encode`（默认，重新编码）、`copy`（复制源音轨编码）；视频内 `audio.mode` 另支持 `drop`（去掉音轨） |
| `sample_rate` | 默认 `auto`，优先源值，不兼容时回退；`source` 严格保持源值，不能满足就失败；也可传兼容的整数 Hz |
| `channels` | 默认 `auto`；`source` 严格保持；也可传 1～8，但仍受编码器限制 |
| `channel_layout` | `mono`、`stereo`、`2.1`、`3.0`、`4.0`、`5.1`、`7.1`；必须和声道数一致 |
| 有损 `rate_control.mode` | `auto`（默认）、`bitrate`；AAC/MP3 另支持 `quality` |
| `rate_control.bitrate_bps` | 单位是 bit/s，例如 128 kbps 写 `128000`；AAC/MP3 还有采样率/声道组合约束 |
| `rate_control.value` | `quality` 模式下 AAC 为 0.1～5，MP3 为 0～9，刻度不通用 |

Opus 支持 1/2/3/6/8 声道以及 `mono`、`stereo`、`3.0`、`5.1`、`7.1` 布局。FLAC/PCM 不接收有损码率或质量控制。

`copy` 无需指定 `codec`，省略容器时使用 `mka`。源音轨编码必须是本服务已支持且目标容器能容纳的类型；不是任意音频的直通模式，也没有视频画面的 copy 模式。不能同时设置编码预设、重采样、声道变换、质量或片段裁剪。完整采样率和 MP3 码率表可查实时能力或[参数合同](../design/media-parameters.md)。

## 图片和抽帧输出

图片转换使用顶层参数；抽帧时放在 `image` 对象内，例如 `{"image":{"format":"png"}}`。

| `format` | 输出 | 可设置的编码参数 |
| --- | --- | --- |
| `jpeg` | JPEG，默认格式；透明区域合成到背景 | `quality` 1～95（默认 90）、`jpeg_subsampling` 0/1/2（默认 2）、`progressive` 布尔值（默认 false） |
| `png` | PNG，可保留透明 | `compression_level` 0～9（默认 6）；不接受 `quality` |
| `webp` | WebP，可保留透明 | `quality` 0～100（默认 85）、`lossless` 布尔值（默认 false）、`method` 0～6（默认 4） |

JPEG/WebP 有损模式的 quality 越高通常越清晰、文件越大。PNG 压缩级别调整压缩工作量；WebP 无损模式的 quality 调整压缩工作量与文件大小，method 调整编码努力程度，都不是另一套画质百分比。JPEG subsampling 的 0/1/2 对应 4:4:4 / 4:2:2 / 4:2:0。详见 [Pillow 图片编码参数](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#webp)。

当前只处理静态、8-bit SDR 图片；只设置 `format` 时默认保留尺寸。

## 其他常用枚举

| 参数位置 | 可用值 | 如何理解 |
| --- | --- | --- |
| 视频 `resize.fit`；图片 `fit` | `contain`（默认）、`cover`、`pad` | 等比装入；填满并裁剪；等比装入并补边 |
| 视频 `speed` | `fast`、`medium`（默认）、`slow` | 编码速度/压缩效率取舍；不是快放/慢放，也不是帧率 |
| 视频 `frame_rate.mode` | `source`（默认）、`constant`、`max` | 保留源帧时间；固定帧率；只限制最高帧率 |
| 视频 `rate_control.mode` | `crf`（默认）、`bitrate` | 按质量控制；按目标码率控制；两者互斥 |
| 图片 `resampling` | `lanczos`（默认）、`bicubic`、`bilinear`、`nearest` | 缩放算法；一般保留默认，nearest 可用于保留像素块边界 |
| 抽帧 `sampling.mode` | `timestamps`（默认）、`interval`、`count` | 指定时间点；按间隔；按数量均匀取样 |
| 抽帧 `frame_selection` | `next`（默认）、`previous`、`nearest` | 不早于目标点；不晚于目标点；最接近，平局取之前 |
| 探测 `sections` 数组 | `format`、`streams`、`tags`、`chapters` | 文件概况、轨道、受控标签、章节；默认前两项 |

`cover`/`pad` 需要宽高，视频目标框还要求偶数尺寸；默认不放大。视频背景默认黑色，图片背景默认白色，使用 `#RRGGBB`。显式帧率可传 `30` 或 `"30000/1001"` 等值；`source` 不同时传 `value`。视频 CRF 范围为 H.264/HEVC 0～51、VP9/AV1 0～63（后两者要求整数），同一编码下越低通常越清晰，但不同编码的 CRF 数字不能直接比较。AV1 当前不支持码率模式的峰值/缓冲参数。

## 可以直接采用的参数示例

以下都是 **`params` 对象**。保存为 JSON 文件后，可使用 README 客户端的 `--params 文件路径`；在 HTTP 中放入对应步骤，在 MCP 中放入对应工具。它们是明确的起点，不保证对所有源素材、目标播放器或部署限额都适用。省略 `params` 始终是最简单的默认调用。

### 视频转换：H.264 / MP4，宽度不超过 1280

用于 `transcode_video`：

```json
{"codec":"h264","container":"mp4","resize":{"width":1280},"rate_control":{"mode":"crf","value":23}}
```

### 视频转换：HEVC / MP4

用于 `transcode_video`：

```json
{"codec":"hevc","container":"mp4"}
```

### 视频转换：VP9 / WebM，自动搭配 Opus

用于 `transcode_video`：

```json
{"codec":"vp9","container":"webm"}
```

### 音频转换或音轨提取：MP3，44.1 kHz 双声道、128 kbps

用于 `transcode_audio` 或 `extract_audio`：

```json
{"codec":"mp3","sample_rate":44100,"channels":2,"rate_control":{"mode":"bitrate","bitrate_bps":128000}}
```

### 音频转换或音轨提取：16 kHz 单声道 WAV

用于 `transcode_audio` 或 `extract_audio`：

```json
{"codec":"pcm","container":"wav","sample_rate":16000,"channels":1,"sample_format":"s16"}
```

### 复制音轨编码到 MKA

用于 `transcode_audio` 或 `extract_audio`，仍须源编码兼容：

```json
{"mode":"copy","container":"mka"}
```

### 图片转换：保留尺寸，输出 PNG

用于 `resize_image`：

```json
{"format":"png"}
```

### 图片转换：WebP，质量 85

用于 `resize_image`：

```json
{"format":"webp","quality":85}
```

### 抽帧：均匀取三张、宽度不超过 320、输出 WebP

用于 `extract_frames`：

```json
{"sampling":{"mode":"count","count":3},"image":{"width":320,"format":"webp"}}
```

### 媒体探测：文件概况、轨道和章节

用于 `probe_media`：

```json
{"sections":["format","streams","chapters"]}
```

## 查询当前部署实际支持的值

HTTP 调用 `GET /v1/capabilities`；MCP 调用 `list_capabilities`，参数为 `{}`。两者返回同一能力信息，鉴权与其他请求相同。

| 返回字段 | 用途 |
| --- | --- |
| `media_parameters.video_codecs` / `audio_codecs` | 当前可选的公共 codec 名及引擎实现映射；请求使用映射的键 |
| `media_parameters.video_containers` / `audio_containers` | 当前容器与对应编码的兼容列表 |
| `media_parameters.video_audio_codecs` | 各视频容器内可用的音频编码 |
| `media_parameters.audio_sample_rates` / `mp3_bitrates_bps` | 编码相关采样率和 MP3 码率表 |
| `media_parameters.pcm_sample_formats` | 当前可用的 PCM 采样格式 |
| `operations[操作标识].params_schema` / `default_params` | 参数形状、枚举/范围与实际规范化默认值；图片枚举也在这里 |
| `limits` | 当前部署的尺寸、帧数、输入输出大小及时间等限制 |

schema 描述整个已适配参数合同；可选编码器不一定在某个部署可用。选择编码时还要检查 `media_parameters`，不要只根据 schema 的枚举判断。`audio_codecs` 表示可编码集合；copy 不需要对应编码器，由服务另行检查源编码与目标封装器，不能仅用该集合判断能否复制。只改容器而省略 codec 时仍会使用该能力的默认编码，不会反向按容器猜测编码；例如音频 `{"container":"mp3"}` 应改为 `{"codec":"mp3"}` 或同时填写两者。

完整字段约束见[参数合同](../design/media-parameters.md)；运行与完整上传/下载示例见[中文 README](../../README.zh-CN.md)。
