# 媒体测试素材

六个基础素材于 2026-10-13 从已授权的外部内部测试素材目录**复制**，来源只读；目标均为普通文件，不使用软链接。仅由用户授权内部测试复用，未确认公开再分发许可，不能据此称为开放授权素材。未复制会议录音、PDF 或 DOCX。

测试按仓库根目录相对寻址，运行时不依赖原仓库。`checksums.json` 是可机读清单，复制前后逐项核对 SHA-256，六文件总计 8,444,238 字节。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `Feline_Apocalypse.mp4` | 4223114 | `c29e17b6fa7293a62dddc2e3a7201eaccc43964b8f0cb6699a5dec4785e26527` |
| `222.mp3` | 122971 | `aa85a226fc65477708df129ab5a5c5f7c48550fb53674a804c8e3ac851a7d225` |
| `haojiao.jpg` | 95104 | `e4e7a92a96f96c377e5bebe17e4dcc110c10c4b31301825bb384f76cde64831e` |
| `ffmpeg_high_bitrate_1080p.mp4` | 3888663 | `eb6592ff21d662fb17c8fa9e6c97b146d974ef4a810fade9dad57ee4937ee499` |
| `ffmpeg_hevc_640x360.mp4` | 51551 | `c264958bd0e4329acf3fc42005bb4a8e1b100b254feaa407f66fb8dca20754d2` |
| `ffmpeg_h264_640x360.mkv` | 62835 | `16af66744b6aaafdd4c4eec194d518b224d26d0db88c7fc697f4066b67152340` |

- `Feline_Apocalypse.mp4`：H.264/AAC，720×720，30fps，约30.834秒；probe、转码、音轨提取、抽帧与 HTTP 输入。
- `222.mp3`：MP3，22.05kHz、单声道、约7.68秒；音频转码与重采样。
- `haojiao.jpg`：JPEG，1279×1706；纵向与奇数尺寸图片。
- `ffmpeg_high_bitrate_1080p.mp4`：1920×1080、H.264/AAC、30fps、1.2秒；缩放及高码率输入。
- `ffmpeg_hevc_640x360.mp4`：640×360、HEVC/AAC、24fps、1秒；HEVC 解码。
- `ffmpeg_h264_640x360.mkv`：640×360、H.264/AAC、24fps、约1.023秒；Matroska 输入。

## 原仓库保存的合成命令

仅三个 `ffmpeg_*` 文件有已知合成命令；另外三个文件的原始生成方式未提供。以下照录源 README，编码构建不同不保证生成字节与现存素材相同。

```bash
ffmpeg -y \
  -f lavfi -i testsrc2=size=1920x1080:rate=30:duration=1.2 \
  -f lavfi -i sine=frequency=1000:duration=1.2 \
  -c:v libx264 -b:v 35M -minrate 35M -maxrate 35M -bufsize 70M \
  -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart -shortest \
  tests/fixtures/ffmpeg_high_bitrate_1080p.mp4

ffmpeg -y \
  -f lavfi -i testsrc2=size=640x360:rate=24:duration=1 \
  -f lavfi -i sine=frequency=880:duration=1 \
  -c:v libx265 -preset ultrafast -x265-params log-level=error -crf 32 \
  -pix_fmt yuv420p -c:a aac -b:a 96k -movflags +faststart -shortest \
  tests/fixtures/ffmpeg_hevc_640x360.mp4

ffmpeg -y \
  -f lavfi -i testsrc2=size=640x360:rate=24:duration=1 \
  -f lavfi -i sine=frequency=660:duration=1 \
  -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 96k -shortest \
  tests/fixtures/ffmpeg_h264_640x360.mkv
```

## 边界素材

使用仓库内脚本，在全新临时目录按需生成，避免把测试运行结果混入版本控制：

```bash
.venv/bin/python scripts/prototype_media.py --output-dir /tmp/media-boundaries --generate-only
.venv/bin/python scripts/prototype_media.py --report /tmp/media-p0-report.json
```

脚本要求真实 FFmpeg/ffprobe 和 Pillow，包含：已知 PTS、非零起点、VFR、旋转、非方形 SAR、奇数尺寸、无音轨/多音轨、零字节/截断/损坏媒体、外部网络/本地文件引用、EXIF 方向、透明 PNG、静态/动画 WebP、多页 TIFF、CMYK/16bit/ICC 及像素上限。像素上限用小图和降低后的测试阈值验收，不默认生成大图。

生成目录的 `generated-checksums.json` 记录本次生成摘要；这些摘要用于测试输入追溯，不作跨 FFmpeg 构建字节一致承诺。完整原型额外验证 FD 路径替换、白名单、预设和精确抽帧，临时文件默认自动清理。服务集成测试和容器验收独立于这个 P0 原型。
