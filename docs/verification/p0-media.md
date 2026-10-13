# P0 真实媒体原型验证

日期：2026-10-13。此文仅记录素材与媒体/文件访问原型，不代表服务完成或容器生命周期验收通过。六个已授权的外部内部测试素材已复制到本仓库，文件名、大小、SHA-256、用途及已知合成命令见 `tests/fixtures/README.md` 和 `checksums.json`，不依赖外部仓库运行。

## 实际执行

```bash
.venv/bin/python scripts/prototype_media.py --report /tmp/media-p0-native.json
docker run --rm --platform linux/arm64 \
  --mount type=bind,src="$PWD",dst=/workspace,readonly \
  --entrypoint python ffmpeg-agent:p0-arm64 \
  /workspace/scripts/prototype_media.py > /tmp/media-p0-linux-arm64.json
docker run --rm --init --platform linux/amd64 \
  --mount type=bind,src="$PWD",dst=/workspace,readonly \
  --entrypoint python ffmpeg-agent:p0-amd64 \
  /workspace/scripts/prototype_media.py > /tmp/media-p0-linux-amd64.json
.venv/bin/ruff check scripts/prototype_media.py
.venv/bin/ruff format --check scripts/prototype_media.py
```

上述原型运行退出码均为 `0`。执行环境：

| 环境 | Python | Pillow | FFmpeg / ffprobe | 范围 |
| --- | --- | --- | --- | --- |
| macOS arm64 | 3.14.7 | 12.3.0 | 9.0.1 / 9.0.1 | 本机功能对照，不替代选型基线 |
| Linux aarch64 容器 | 3.14.8 | 12.3.0 | 7.1.5-0+deb13u1 / 同构建 | 首版目标系列真实媒体原型 |
| Linux x86_64 容器 | 3.14.8 | 12.3.0 | 7.1.5-0+deb13u1 / 同构建 | 同一arm64宿主的amd64架构模拟，完整原型通过 |

镜像精确基线见运行基线验证文档。amd64结果不代表原生amd64宿主性能或两宿主机联通。本次未声称容器硬停止恢复、跨宿主机或容量验收。

## 结果与冻结建议

- 六文件摘要与大小全部匹配，均可通过 `fd:` 和显式 `-fd N` 传给真实 ffprobe。目录逐级 `openat`、`O_NOFOLLOW`，最后打开时加 `O_NONBLOCK` 并 `fstat` 限定普通文件。7 个恶意或不支持路径（上级、绝对、符号链接、目录、FIFO、点和空串）拒绝；打开后重命名并替换路径，引擎仍读取原已验证 inode。
- 输入协议仅 `fd`；格式白名单为 `mov,matroska,mp3,wav,flac,ogg,aac,jpeg_pipe,png_pipe,webp_pipe`。HLS/concat、零字节、截断及损坏文件失败。额外故意允许 concat demuxer，仍证明其 `file:` 外部引用被协议白名单拒绝。正式服务不得因文件扩展名放宽白名单。
- `web_720p_v1` 对四个基础视频均成功：方形视频720×720，1080p输入1280×720，另两个640×360；均H.264/yuv420p/CFR30、AAC/48kHz/双声道。CRF23、medium、faststart，等比装入且不放大，偶数输出尺寸。
- 音频两个预设对MP3和多音轨视频的第一音轨真实成功：AAC M4A/48kHz/双声道和PCM s16le WAV/16kHz/单声道。无音轨样例确实无音轨；正式服务应在指定音轨不存在时返回稳定执行错误，不静默回退。
- 已知CFR时间为 `[0,200,400,600]ms`，非零起点样例从绝对2秒起但归一化同上；VFR为 `[0,200,600,800]ms`。请求 `[0,201,600,601,800,201]` 的结果逐项按原序保留，CFR得 `[0,400,600,无命中,无命中,400]`，VFR得 `[0,600,600,800,800,600]`。命中来源是解码器 `showinfo` 的实际PTS，另以ffprobe逐帧PTS交叉核对，未从文件名或帧数推算时间。
- 抽帧时间定义建议冻结为第一可解码帧的展示PTS为零；精确比较原始有理数PTS，响应 `actual_time_ms` 四舍五入到整数毫秒。缺失或非严格递增PTS拒绝精确抽帧；不靠nominal fps补造时间。重复时间点保持独立索引。
- 旋转元数据为90度的64×48视频，默认自动旋转实际抽得48×64图片。构造样例必须使用 `-display_rotation 90`，旧 `-metadata rotate=90` 在这两个构建未写入有效矩阵，不能拿文件名冒充旋转覆盖。非方形SAR样例探得2:1、奇数样例为65×49，建议首版分别明确拒绝非方形SAR，及对转码采用向下偶数尺寸；任意非90倍数旋转、HDR/10bit等不支持组合拒绝，勿静默转换。
- 图片先处理EXIF方向，orientation=6的40×20原图输出20×40；contain默认不放大，cover居中裁剪且需要放大但未授权放大时失败。透明图片转JPEG白底；动画WebP、多页TIFF、CMYK、16bit、无效ICC和超限像素均拒绝。静态WebP正常缩放、允许放大分支、有效cover均通过。
- 基础 `haojiao.jpg` 带 **Display P3 RGB ICC**，实测不能全面拒绝ICC。用Pillow ImageCms将有效RGB ICC转换到sRGB后缩放，输出480×640并清除ICC、EXIF。带alpha图片转换RGB色彩后保留alpha；不带ICC按sRGB解释；非法或非RGB ICC拒绝。CMYK、16bit仍拒绝。

边界生成命令：

```bash
.venv/bin/python scripts/prototype_media.py --generate-only --output-dir /tmp/media-boundaries-new
```

目标目录须为空。生成媒体和摘要留在指定目录，默认完整原型用自动清理的临时目录。大输入/大图/磁盘压力不属于普通原型，后续资源层独立生成。缺失/非单调PTS、HDR拒绝、正式API错误映射和完整并发/生命周期仍由后续实现测试验收；以上建议需与最终schema和能力接口保持一致。
