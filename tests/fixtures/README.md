# 媒体测试素材

六个基础素材由本仓库 `scripts/generate_fixtures.py` 于 2026-10-15 离线合成。图形、文字标记、波形和运动均由 Pillow、Python 标准库及 FFmpeg 生成；不读取其他项目的图片、录音或视频，也不下载素材。原先复制的六个媒体文件已替换并删除。

普通测试直接读取这些固定文件，不在每次测试时重新编码。`checksums.json` 保持 `{name, size_bytes, sha256}` 列表格式；`manifest.json` 记录生成器摘要、精确镜像 ID、引擎/依赖版本、固定种子、生成参数及实测媒体属性。六文件总计 **15,764,123 字节**。

| 文件 | 内容及属性 | 用途 |
| --- | --- | --- |
| `image_jpeg_portrait_odd.jpg` | 1279×1706 RGB JPEG；渐变、八色块、1～4px细线、灰阶、方向标记和裁剪靶 | 纵向/奇数尺寸、缩放、裁剪、填充及画质 |
| `audio_mp3_mono_22050.mp3` | MP3，22.05kHz、单声道；原始波形8秒，编码容器约8.071837秒；440/880Hz、静音、1320Hz四段，每段2秒并带25ms淡入淡出 | 音频裁剪、编码、默认参数和重采样 |
| `video_h264_aac_square.mp4` | H.264/AAC，720×720、30fps、900帧、精确30秒；运动图案、固定种子纹理、0000～0899帧号及时间标记；44.1kHz双声道，左440Hz/右880Hz | probe、转码、提音轨、抽帧、HTTP输入、并发和取消 |
| `video_h264_high_bitrate_1080p.mp4` | H.264/AAC，1920×1080、30fps、36帧、1.2秒；运动图案及固定种子细节纹理；44.1kHz单声道 | 缩放及高码率输入；35M编码目标，短片因VBV缓冲实测视频平均约58.5Mbit/s |
| `video_hevc_640x360.mp4` | HEVC/AAC，640×360、24fps、24帧、1秒；44.1kHz单声道，44,398字节 | HEVC解码、首末帧边界及受限文件输入 |
| `video_h264_640x360.mkv` | H.264/AAC，640×360、24fps、24帧；容器约1.023秒，时间基1/1000；AAC起点为−0.023秒 | Matroska输入、时间戳及音轨直接复制 |

主视频保留30秒运动和纹理，以支撑并发、取消等真实处理测试；无需语音或人物内容。旧素材的性能、吞吐及故障报告保留为历史证据，不能当作新素材的验收结果。

## 重新生成与验证

当前生成使用精确镜像：

```text
sha256:7eeda466adad958b13d4b91a980c9254fcd590b642c879c436c6f009da648a06
FFmpeg/ffprobe 7.1.5-0+deb13u1 / Python 3.14.8 / Pillow 12.3.0 / LittleCMS 2.19
```

在仓库根目录运行下面的隔离生成命令。镜像须已在本地存在；若重新按项目 Dockerfile 和锁文件构建，先读取新镜像的精确 ID，替换 `fixture_image` 并保留新的 `manifest.json`。根文件系统只读、容器禁网，仅允许新临时输出目录和容器内临时源文件可写：

```bash
fixture_image=sha256:7eeda466adad958b13d4b91a980c9254fcd590b642c879c436c6f009da648a06
fixture_output=$(mktemp -d /tmp/ffmpeg-fixtures.XXXXXX)
docker image inspect "$fixture_image" --format '{{.Id}}'
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 0:0 \
  --tmpfs /tmp:rw,nosuid,size=256m \
  --mount "type=bind,src=$PWD/scripts/generate_fixtures.py,dst=/generate_fixtures.py,readonly" \
  --mount "type=bind,src=$fixture_output,dst=/generated" \
  --entrypoint python "$fixture_image" /generate_fixtures.py \
  --output-dir /generated/fixtures --engine-image-id "$fixture_image"
```

生成脚本拒绝已有输出目录和非7.1系列FFmpeg，固定种子为 `20261015`、视频编码线程为1。输出清单前会完整解码JPEG和五个音视频文件，并检查编码、尺寸、帧数、像素格式、音轨布局、时长、MKV时间基、高码率下限、HEVC大小和逐帧时间戳。每次生成后应检查图板、帧号、音轨和 `manifest.json`，复核摘要后再显式替换仓库文件；脚本本身不会覆盖或删除现存素材。

固定种子与参数保证内容可重建，**不承诺不同编码器、CPU架构或依赖构建产生相同字节**；例如Matroska容器标识也可能变化。SHA-256用于确认当前提交的具体输入版本，媒体属性和相应行为断言用于核对功能合同。

核对当前六个文件的大小、摘要与普通文件属性：

```bash
.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

root = Path("tests/fixtures")
for row in json.loads((root / "checksums.json").read_text()):
    path = root / row["name"]
    assert path.is_file() and not path.is_symlink()
    assert path.stat().st_size == row["size_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
print("6 fixtures verified")
PY
```

## 边界素材

使用仓库内脚本，在全新临时目录按需生成，避免把测试运行结果混入版本控制：

```bash
.venv/bin/python scripts/prototype_media.py --output-dir /tmp/media-boundaries --generate-only
.venv/bin/python scripts/prototype_media.py --report /tmp/media-p0-report.json
```

边界脚本要求真实FFmpeg/ffprobe和Pillow，包含已知PTS、非零起点、VFR、旋转、非方形SAR、奇数尺寸、无音轨/多音轨、零字节/截断/损坏媒体、外部网络/本地文件引用、EXIF方向、透明PNG、静态/动画WebP、多页TIFF、CMYK/16bit/ICC及像素上限。像素上限用小图和降低后的测试阈值验收，不默认生成大图。

基础JPEG为普通RGB测试图。有效非sRGB的颜色转换由独立边界用例保留：`generate_fixtures.linear_rgb_icc()` 用Pillow/LittleCMS生成RGB配置，保留sRGB原色与白点并将TRC设为线性，不从旧图片提取配置；测试验证RGB `(128, 64, 32)` 转成sRGB约 `(188, 137, 99)`，区别于仅丢弃ICC元数据。无效ICC拒绝用例另行保留。

[`test_media_semantics.py`](../integration/test_media_semantics.py) 另外在 pytest 临时目录生成三组小素材，验证实际输出内容：分段双声道 WAV 核对裁剪换音点、16kHz 重采样、左右声道及单声道混音；四帧纯色视频将 next/previous/nearest 的时间清单、乱序与重复请求绑定到图片颜色，并核对视频裁剪首末帧；四象限 JPEG 核对 EXIF 2/3/6/8 的镜像和旋转方向。频率与像素断言独立于编码参数和输出元数据，临时素材无需提交。

边界目录的 `generated-checksums.json` 记录本次生成摘要，用于测试输入追溯，不作跨构建字节一致承诺。完整原型额外验证FD路径替换、白名单、预设和精确抽帧；服务集成测试和容器验收独立于P0原型。
