# Media parameter reference

**English** | [简体中文](parameters.zh-CN.md)

HTTP and MCP use the same `params`. You can omit it for every operation; the tables below list values and combinations to use when you need to customize the output. JSON strings are case-sensitive, so use the lowercase spellings shown here.

## What determines support?

The available features are the **intersection of what this service implements and what its runtime engines provide**, subject to container compatibility, input media properties, and deployment limits.

- **The service defines the parameter choices.** For example, video encoding accepts only `h264`, `hevc`, `vp9`, and `av1`, rather than arbitrary FFmpeg encoder names. Other codecs do not become available automatically just because FFmpeg supports them.
- **The FFmpeg build determines the underlying audio and video capabilities.** Two installations of the same FFmpeg version can provide different encoders because of their build options or linked libraries. At startup, the service detects the available encoders and muxers, then advertises only the combinations it implements and can use. The [FFmpeg encoder documentation](https://ffmpeg.org/ffmpeg-codecs.html#libsvtav1) describes the build requirements for external encoding libraries.
- **Docker deployments use the engines and libraries inside the container.** Installing FFmpeg on the host does not change the container's capabilities. Query capabilities again after changing the image, its architecture-specific build, or dependencies inside the container. When running directly on a host, support depends on the FFmpeg executable configured for the service and its Python environment.
- **Encoding currently runs in software.** Host CPU, memory, and service limits affect processing speed and acceptable workloads. GPU acceleration and 10-bit/HDR are not exposed in this release; a host GPU does not automatically enable hardware encoding.
- **Pillow handles image output.** JPEG, PNG, WebP, and their dependencies are part of the service runtime. Frame extraction first decodes with FFmpeg, then uses the shared image parameters to produce output. FFmpeg's full list of image encoders is not the service's list of supported image formats.

The following tables describe the implemented choices. Check HTTP `GET /v1/capabilities` or MCP `list_capabilities` for availability in a particular deployment. Missing required engine components prevent the service from becoming ready; missing optional encoders affect only their corresponding choices.

## Codecs and formats are different parameters

`codec` specifies how audio or video data is encoded; `container` specifies the output file's container format. For example, MP4 can contain H.264 video and AAC audio, while MKV can also contain H.264. A filename extension alone does not change the output.

Images use `format`, such as `"format":"jpeg"`. Do not pass `format` for video, or use `libx264`, `H.265`, or `jpg` as the corresponding parameter values: the service uses `h264`, `hevc`, and `jpeg`.

## Video codecs and containers

These values apply to the top-level `codec` and `container` fields for video conversion.

| `codec` | Meaning | Compatible `container` values | Default container | Default quality setting |
| --- | --- | --- | --- | --- |
| `h264` | H.264 / AVC; the default codec | `mp4`, `mkv` | `mp4` | CRF 23 |
| `hevc` | H.265 / HEVC | `mp4`, `mkv` | `mp4` | CRF 23 |
| `vp9` | VP9 | `webm`, `mkv` | `webm` | CRF 32 |
| `av1` | AV1 | `webm`, `mp4`, `mkv` | `webm` | CRF 32 |

The currently implemented software encoders are libx264, libx265, libvpx-vp9, and libsvtav1, respectively. Callers use the public values in the left column. AV1 output currently requires both dimensions to be at least 64 pixels. Video output is limited to 8-bit SDR with `pixel_format: "yuv420p"`.

Place the video's audio track parameters inside the `audio` object:

| Video `container` | Supported `audio.codec` values | Default audio codec |
| --- | --- | --- |
| `mp4` | `aac`, `mp3` | `aac` |
| `mkv` | `aac`, `mp3`, `opus`, `flac`, `pcm` | `aac` |
| `webm` | `opus` | `opus` |

For example, `{"codec":"vp9"}` selects WebM, CRF 32, and Opus audio. `{"codec":"h264","container":"webm"}` is incompatible and will be rejected. These are the combinations exposed by this service, rather than every combination allowed by each container standard. Playback also depends on the codecs supported by the consuming application.

## Audio codecs and containers

These values apply to audio conversion and audio extraction. Audio tracks within video also support these encoding parameters, subject to the video container compatibility table above.

| `codec` | Meaning | Compatible `container` values | Default container | Common settings and constraints |
| --- | --- | --- | --- | --- |
| `aac` | AAC; lossy; the default codec | `m4a`, `mka` | `m4a` | Common sample rates: 44100 / 48000 Hz; rate control defaults to `auto` |
| `mp3` | MP3; lossy | `mp3`, `mka` | `mp3` | Mono or stereo only; available bitrates depend on the sample rate |
| `opus` | Opus; lossy | `ogg`, `mka` | `ogg` | Output sample rate is fixed at 48000 Hz; the default `auto` can resample |
| `flac` | FLAC; lossless compression | `flac`, `mka` | `flac` | Output is currently 16-bit; `compression_level` is 0–12, default 5 |
| `pcm` | PCM; uncompressed samples | `wav`, `mka` | `wav` | `sample_format` is `s16` (default), `s24`, or `f32` |

`mka` is a Matroska audio container; this service uses `ogg` for Opus audio output. “Lossless” describes FLAC encoding: the service's currently fixed 16-bit output does not promise bit-for-bit preservation of sources with a higher bit depth. PCM formats `s16`, `s24`, and `f32` mean 16-bit integer, 24-bit integer, and 32-bit floating-point samples, respectively.

| Parameter | Values and behavior |
| --- | --- |
| `mode` | `encode` (default; re-encode) or `copy` (copy the source audio encoding); a video's `audio.mode` also accepts `drop` to remove audio |
| `sample_rate` | Defaults to `auto`: prefer the source value and fall back if incompatible. `source` requires the source value and fails if it cannot be preserved. You can also supply a compatible integer rate in Hz |
| `channels` | Defaults to `auto`; `source` requires preservation; integers 1–8 are also accepted, subject to codec limits |
| `channel_layout` | `mono`, `stereo`, `2.1`, `3.0`, `4.0`, `5.1`, `7.1`; must match the channel count |
| Lossy `rate_control.mode` | `auto` (default) or `bitrate`; AAC/MP3 also support `quality` |
| `rate_control.bitrate_bps` | In bit/s: write `128000` for 128 kbps. AAC/MP3 also have constraints based on the sample rate and channel count |
| `rate_control.value` | In `quality` mode: 0.1–5 for AAC, or 0–9 for MP3. These scales are not interchangeable |

Opus supports 1/2/3/6/8 channels and the `mono`, `stereo`, `3.0`, `5.1`, and `7.1` layouts. FLAC/PCM do not accept lossy bitrate or quality controls.

`copy` does not require a `codec`; its default container is `mka`. The source audio codec must be supported by the service and compatible with the target container. This is not unrestricted audio passthrough, and there is no video stream copy mode. Do not combine it with an encoding preset, resampling, channel changes, quality settings, or clipping. See live capabilities or the [parameter contract](../design/media-parameters.md) for the complete sample rate and MP3 bitrate tables.

## Image and extracted-frame output

Image conversion uses top-level parameters. For frame extraction, place them inside the `image` object, for example `{"image":{"format":"png"}}`.

| `format` | Output | Encoding parameters |
| --- | --- | --- |
| `jpeg` | JPEG; the default format; transparent areas are composited onto the background | `quality` 1–95 (default 90), `jpeg_subsampling` 0/1/2 (default 2), `progressive` boolean (default false) |
| `png` | PNG; can preserve transparency | `compression_level` 0–9 (default 6); does not accept `quality` |
| `webp` | WebP; can preserve transparency | `quality` 0–100 (default 85), `lossless` boolean (default false), `method` 0–6 (default 4) |

In lossy JPEG/WebP modes, higher quality usually produces clearer images and larger files. PNG compression level controls compression effort. In lossless WebP mode, quality controls compression effort and file size, while method controls encoding effort; these are not alternative image quality percentages. JPEG subsampling values 0/1/2 correspond to 4:4:4 / 4:2:2 / 4:2:0. See [Pillow image encoding parameters](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#webp).

Only static, 8-bit SDR images are currently supported. Setting only `format` preserves the dimensions by default.

## Other common enum values

| Parameter location | Values | Meaning |
| --- | --- | --- |
| Video `resize.fit`; image `fit` | `contain` (default), `cover`, `pad` | Fit within the box while preserving aspect ratio; fill and crop; fit and pad |
| Video `speed` | `fast`, `medium` (default), `slow` | Encoding speed versus compression efficiency; not playback speed or frame rate |
| Video `frame_rate.mode` | `source` (default), `constant`, `max` | Preserve source frame timing; use a constant frame rate; limit only the maximum frame rate |
| Video `rate_control.mode` | `crf` (default), `bitrate` | Control quality or target bitrate; the two modes are mutually exclusive |
| Image `resampling` | `lanczos` (default), `bicubic`, `bilinear`, `nearest` | Resize filter; generally keep the default. Nearest can preserve hard pixel boundaries |
| Frame extraction `sampling.mode` | `timestamps` (default), `interval`, `count` | Select timestamps; sample at intervals; sample evenly by count |
| Frame extraction `frame_selection` | `next` (default), `previous`, `nearest` | At or after the target; at or before the target; closest, choosing the earlier frame on a tie |
| Probe `sections` array | `format`, `streams`, `tags`, `chapters` | File overview, streams, allowlisted tags, chapters; defaults to the first two |

`cover`/`pad` require both width and height; video target boxes also require even dimensions. Upscaling is disabled by default. The background defaults to black for video and white for images, using `#RRGGBB`. Explicit frame rates can be values such as `30` or `"30000/1001"`; do not supply `value` with `source`. Video CRF ranges are 0–51 for H.264/HEVC and 0–63 for VP9/AV1 (the latter two require integers). Within the same codec, lower values usually mean higher quality, but CRF numbers cannot be compared directly across codecs. AV1 does not currently support peak bitrate or buffer parameters in bitrate mode.

## Ready-to-use parameter examples

Each example below is a **`params` object**. Save it as a JSON file and pass `--params path/to/file` to the README client, or place it in the appropriate HTTP step or MCP tool call. These are concrete starting points; suitability still depends on the source media, target player, and deployment limits. Omitting `params` remains the simplest way to use the defaults.

### Video conversion: H.264 / MP4, at most 1280 pixels wide

For `transcode_video`:

```json
{"codec":"h264","container":"mp4","resize":{"width":1280},"rate_control":{"mode":"crf","value":23}}
```

### Video conversion: HEVC / MP4

For `transcode_video`:

```json
{"codec":"hevc","container":"mp4"}
```

### Video conversion: VP9 / WebM, with Opus selected automatically

For `transcode_video`:

```json
{"codec":"vp9","container":"webm"}
```

### Audio conversion or extraction: MP3, 44.1 kHz stereo, 128 kbps

For `transcode_audio` or `extract_audio`:

```json
{"codec":"mp3","sample_rate":44100,"channels":2,"rate_control":{"mode":"bitrate","bitrate_bps":128000}}
```

### Audio conversion or extraction: 16 kHz mono WAV

For `transcode_audio` or `extract_audio`:

```json
{"codec":"pcm","container":"wav","sample_rate":16000,"channels":1,"sample_format":"s16"}
```

### Copy the audio encoding into MKA

For `transcode_audio` or `extract_audio`; the source codec must still be compatible:

```json
{"mode":"copy","container":"mka"}
```

### Image conversion: preserve dimensions and output PNG

For `resize_image`:

```json
{"format":"png"}
```

### Image conversion: WebP, quality 85

For `resize_image`:

```json
{"format":"webp","quality":85}
```

### Frame extraction: three evenly sampled frames, at most 320 pixels wide, as WebP

For `extract_frames`:

```json
{"sampling":{"mode":"count","count":3},"image":{"width":320,"format":"webp"}}
```

### Media probe: file overview, streams, and chapters

For `probe_media`:

```json
{"sections":["format","streams","chapters"]}
```

## Query the values available in your deployment

Use HTTP `GET /v1/capabilities` or MCP `list_capabilities` with `{}` as its arguments. Both return the same capability information and use the same authentication as other requests.

| Response field | Purpose |
| --- | --- |
| `media_parameters.video_codecs` / `audio_codecs` | Available public codec names mapped to engine implementations; use the mapping's keys in requests |
| `media_parameters.video_containers` / `audio_containers` | Available containers and their compatible codecs |
| `media_parameters.video_audio_codecs` | Audio codecs available within each video container |
| `media_parameters.audio_sample_rates` / `mp3_bitrates_bps` | Codec-specific sample rate and MP3 bitrate tables |
| `media_parameters.pcm_sample_formats` | Available PCM sample formats |
| `operations[operation identifier].params_schema` / `default_params` | Parameter structure, enums/ranges, and actual normalized defaults; image enums are also here |
| `limits` | Deployment limits for dimensions, frame counts, input/output sizes, time, and more |

The schema describes the full implemented parameter contract; an optional encoder may be unavailable in a particular deployment. Check `media_parameters` when choosing a codec, rather than relying only on schema enums. `audio_codecs` lists codecs available for encoding. Copying does not require the corresponding encoder: the service checks the source codec and target muxer separately, so that list alone cannot determine whether copying will work. Changing only the container while omitting the codec still uses the operation's default codec; the service does not infer a codec from the container. For example, replace audio `{"container":"mp3"}` with `{"codec":"mp3"}`, or supply both fields.

See the [parameter contract](../design/media-parameters.md) for complete field constraints, and the [README](../../README.md) for setup and complete upload/download examples.
