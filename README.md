# ffmpeg-agent

**English** | [简体中文](README.zh-CN.md)

Process media from applications or remote AI agents through HTTP and MCP. Upload a file, submit an asynchronous task, and download the result. Shared volumes are optional for clients; you can combine them with HTTP transfers.

| Capability | What you can do |
| --- | --- |
| Media inspection | Inspect media, select report sections and optionally count frames |
| Video conversion | Convert video with defaults or select encoding, size, frame rate and quality |
| Audio conversion | Convert audio with defaults or select codec, sample rate, channels and bitrate |
| Audio extraction | Extract and convert a selected audio track, or copy its original encoding |
| Frame extraction | Extract frames by timestamps, interval or count, with image size and quality controls |
| Image processing | Resize, crop, pad and convert static JPEG, PNG and WebP images |

Tasks can chain operations, reuse inputs and return multiple outputs. Start with the defaults by omitting `params`, then add only the settings you need. HTTP and MCP accept the same parameters. Query `GET /v1/capabilities` for available encoders, parameter schemas and limits.

Optional natural-language tasks turn a description into a validated media plan using PydanticAI. Enable the official A2A endpoint to expose the same task service to other agents. Both features are off by default. See the [natural-language and A2A guide](docs/usage/agent.md) / [自然语言与 A2A 使用指南](docs/usage/agent.zh-CN.md).

## Connect your Agent to an existing service

Consumers do not need Docker, FFmpeg or server source. Install [`ffmpeg-agent-connect`](skills/ffmpeg-agent-connect/SKILL.md) for connection and capability checks, and use [`ffmpeg-agent-media`](skills/ffmpeg-agent-media/SKILL.md) for uploads, processing, recovery and downloads. Each skill bundles the same independent client, requiring only Python 3.10+, a service URL and service Token. The model key stays on the server.

Agents already connected through remote MCP can use native tools with HTTP file transfers. Natural-language A2A delegation and plain HTTP are also supported. See the [consumer skills guide](docs/usage/consumer-skills.md) for installation, private configuration, native MCP handoff and complete examples.

## Start with Docker Compose

You need Docker with Compose and Python 3. Run the commands below from the repository root.

### First-time setup and startup

Initialize the configuration before starting the service for the first time:

```sh
python3 deploy/generate-config.py
```

The generator only prepares `.env` and `deploy/local/media-config.json`; it does not start the service. It fills missing settings, preserves your existing valid Token and configuration, and migrates legacy JSON credentials and feature switches into `.env`. The random service Token is stored only in `.env`, with mode `0600`. Keep `deploy/local` private (`0700`); the examples read `.env` automatically, so Tokens do not need to appear in commands.

Review `.env` and adjust any settings you need (see below), then build the image and start the service:

```sh
docker compose up -d --build --wait
```

Compose waits for the service to become ready.

### Routine startup

After initialization, start the service with the existing configuration:

```sh
docker compose up -d --wait
```

You do not need to run `generate-config.py` again for routine startup. When source code, dependencies or the Dockerfile change, add `--build` to rebuild the image.

### Configuration and legacy migration

Configure the service Bearer Token, model key, model settings and feature switches in `.env`. Docker Compose explicitly forwards these settings to the service. The structural JSON contains storage, source rules and service limits; it must not contain `service_token`, model credentials or feature switches. If an existing installation still stores those settings in JSON, run the generator once before upgrading to migrate them. A process environment value overrides the corresponding `.env` setting; an empty Token is rejected. Keep credentials out of Git, shell commands and logs. Do not run `source .env`, print resolved Compose configuration, or share container environment inspection output.

| `.env` variable | Default | Purpose |
| --- | --- | --- |
| `MEDIA_SERVICE_TOKEN` | Generated randomly | Bearer Token shared by HTTP, MCP, A2A, uploads and downloads; explicit `null` opens the service |
| `MEDIA_LLM_ENABLED` / `MEDIA_A2A_ENABLED` | `false` / `false` | Enable natural-language planning and optional A2A |
| `MEDIA_LLM_API_KEY` / `MEDIA_LLM_MODEL` / `MEDIA_LLM_BASE_URL` | Empty | Provider key, model ID and OpenAI-compatible API address |
| `MEDIA_PUBLIC_BASE_URL` | `http://localhost:8080` | Address reachable by clients; also used for MCP Host/Origin validation |
| `MEDIA_IMAGE` | `ffmpeg-agent:v1-local` | Local image name |
| `MEDIA_BIND_ADDRESS` / `MEDIA_PORT` | `127.0.0.1` / `8080` | Published host address and port |
| `MEDIA_CONFIG_PATH` | `./deploy/local/media-config.json` | Structural service JSON path, relative to the repository root |
| `MEDIA_CPUS` / `MEDIA_MEMORY` / `MEDIA_PIDS_LIMIT` | `2.0` / `2g` / `128` | Container CPU, memory and process limits |

A Token must have at least 24 printable ASCII characters without spaces. Missing or empty Tokens stop startup; only `MEDIA_SERVICE_TOKEN=null` selects open access. To enable natural-language tasks or A2A, follow the [enablement guide](docs/usage/agent.md#enable-planning). After editing `.env`, recreate the container so the new values take effect:

```sh
docker compose up -d --force-recreate --wait media
```

If you change the port, update `MEDIA_PUBLIC_BASE_URL` in `.env` too. `worker_concurrency` in the structural JSON sets logical task concurrency (1–4, default 1). If you use a custom JSON path, select it with `MEDIA_CONFIG_PATH`; examples can read its address with `--config` when no environment URL or `--base-url` is provided.

The default service is reachable only on the host. For remote use, provide an HTTPS entry point, set `MEDIA_PUBLIC_BASE_URL` to the externally reachable URL, and forward the correct public Host header. MCP validates Host and Origin against that address. A remote example client needs only a private `.env` with `MEDIA_PUBLIC_BASE_URL` and `MEDIA_SERVICE_TOKEN`, selected with `--env-file`; it does not need the server's storage or deployment JSON. Process environment settings override this file, and `--base-url` overrides the connection address. Tokens are never read from JSON by the example clients.

## Upload, process and download

The repository includes complete clients that upload a local file, wait for processing, and download and verify the result. All commands below use existing sample files and a new temporary output directory, so they can be copied and run again. Keep the source unchanged while uploading.

### HTTP: upload and submit together

This Python 3 standard-library client needs no extra packages. By default, it converts video to H.264/MP4, preserving source dimensions and frame timing:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4"
```

It sends one multipart request to `POST /v1/jobs/upload`, polls the returned task, and saves the verified MP4. You do not need to supply encoding parameters, handle IDs manually, or mount a shared volume.

### HTTP: upload first, then submit

Add `--upload-first` to use `POST /v1/files` followed by `POST /v1/jobs`. The client also prints the `file_id`, which can be reused in other tasks while the upload is available:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4" \
  --upload-first
```

Both modes complete the same upload → process → download workflow. When building your own HTTP client, the multipart `job` part has this shape (a request-body reference, not a standalone command):

```json
{
  "inputs": {"source": {"kind": "upload", "part": "source"}},
  "steps": [{
    "id": "process",
    "operation": "video.transcode.v1",
    "inputs": {"video": {"source": "source"}}
  }],
  "outputs": {"result": {"step": "process", "output": "video"}}
}
```

Send `job` first, then the `source` file part, with an `Idempotency-Key` header. For upload-first submission, replace the input with `{"kind":"uploaded_file","file_id":"<returned file_id>"}`. See the [HTTP contract](docs/api/v1-contract.md) for the endpoints and [client source](examples/http-workflow.py) for a complete implementation.

## Use remote MCP

With [uv](https://docs.astral.sh/uv/) installed, this command runs the complete MCP client with the SDK version pinned by the project:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
uv run --frozen python examples/mcp-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4"
```

The client uploads over HTTP, calls the remote `transcode_video` tool, waits through MCP, and downloads over HTTP. It reads the same `.env` Token for both transports. There is no local stdio server to configure.

To connect another MCP client, use the Streamable HTTP endpoint at `MEDIA_PUBLIC_BASE_URL + /mcp` (locally `http://localhost:8080/mcp`) and configure the service Bearer header in its connection settings. File HTTP requests need the same header. **MCP alone cannot upload a path from your computer:** the client needs HTTP file-transfer capability, or an input URL/shared file the service can access.

For reference, these are the arguments to `transcode_video` after HTTP upload; replace the placeholders with the returned `file_id` and a key for this request:

```json
{
  "source": {"kind": "uploaded_file", "file_id": "<returned file_id>"},
  "idempotency_key": "<key for this request>"
}
```

The tool returns `job_id`. Call `get_job` until it finishes, then use `outputs.result.artifact_id` with `get_artifact` to obtain download information. The runnable client handles these steps for you. Discover all tools through `tools/list`; the six media tools share their parameter models with HTTP, and the remaining tools manage tasks, files, artifacts and leases. See the [MCP contract](docs/api/v1-contract.md#远程-mcp).

## Try the six operations

Both clients accept the same `--operation` and `--params` options. The default operation is `transcode_video`; all six work without `--params`.

| `--operation` | Sample source under `tests/fixtures/` | Destination and default result |
| --- | --- | --- |
| `probe_media` | `video_h264_640x360.mkv` | `report.json`: format and stream information |
| `transcode_video` | `video_h264_640x360.mkv` | `result.mp4`: H.264, CRF 23, source dimensions and timing |
| `transcode_audio` | `audio_mp3_mono_22050.mp3` | `result.m4a`: AAC, compatible sample rate/channels/bitrate chosen automatically |
| `extract_audio` | `video_h264_640x360.mkv` | `result.m4a`: first audio track, converted to AAC |
| `extract_frames` | `video_h264_640x360.mkv` | `frames/`: first frame as JPEG quality 90, plus `manifest.json` |
| `resize_image` | `image_jpeg_portrait_odd.jpg` | `result.jpg`: original dimensions, JPEG quality 90 |

For example, download a frame collection with one command:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/frames" \
  --operation extract_frames
```

Use `uv run --frozen python examples/mcp-workflow.py` instead of `python3 examples/http-workflow.py` to run the same operation through MCP.

### Adjust only what you need

A parameters file contains just the values to override. This complete example fits the video within 1920×1080 without enlarging it, uses CRF 20, and encodes mono AAC at 44.1 kHz:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
cat > "$output_dir/params.json" <<'JSON'
{
  "resize": {"width": 1920, "height": 1080},
  "rate_control": {"mode": "crf", "value": 20},
  "audio": {"sample_rate": 44100, "channels": 1}
}
JSON
python3 examples/http-workflow.py \
  tests/fixtures/video_h264_640x360.mkv "$output_dir/result.mp4" \
  --params "$output_dir/params.json"
```

The same file works with the MCP client. In your own requests, put this object in the HTTP step's `params` or the MCP tool's `params`.

### Available codecs, formats and common values

These values are supported by the service's parameter contract; use lowercase strings. `codec` selects the encoding, `container` selects the file container, and images use `format`.

| Parameter | Values |
| --- | --- |
| Video `codec` | `h264` (default), `hevc`, `vp9`, `av1` |
| Video `container` | `mp4`, `mkv`, `webm` |
| Audio `codec` | `aac` (default), `mp3`, `opus`, `flac`, `pcm` |
| Audio `container` | `m4a`, `mp3`, `ogg`, `flac`, `wav`, `mka` |
| Image `format` | `jpeg` (default; not `jpg`), `png`, `webp` |
| Video `speed` | `fast`, `medium` (default), `slow`; encoding speed and compression efficiency |
| Video `resize.fit` / image `fit` | `contain` (default, fit inside), `cover` (crop to fill), `pad` (add borders) |
| Video `frame_rate.mode` | `source` (default), `constant`, `max` |

Common combinations are H.264 + MP4, HEVC + MP4, VP9/AV1 + WebM, AAC + M4A, MP3 + MP3, Opus + Ogg, and PCM + WAV. Changing only `codec` selects a compatible default container. Explicit combinations must match; H.264 + WebM, for example, is rejected.

Actual availability is the intersection of service support and runtime support, subject to combination rules and deployment limits. Docker uses FFmpeg/Pillow **inside the container**; installing codecs on the host does not add service capabilities. The current implementation uses software encoding. Query the capabilities endpoint below for your deployment. See the [media parameter reference](docs/usage/parameters.md) for complete compatibility tables, enum meanings and 10 ready-to-use JSON parameter examples.

Presets are optional shortcuts: `{"preset":"web_720p_v1"}` selects 720p/30 fps with stereo AAC; `aac_stereo_128k_v1` selects AAC 128 kbps/48 kHz/stereo; `wav_mono_16k_v1` selects 16 kHz mono PCM. Explicit settings override the selected preset. Without a preset, video preserves source dimensions and timing. Audio `auto` prefers source sample rate/channels and falls back to compatible values; `source` requires exact preservation. Audio copy mode preserves the original encoding and cannot be combined with encoding settings or trimming.

Query `GET /v1/capabilities` or MCP `list_capabilities` for available encoders, schemas, defaults and limits. Accepted settings appear in task `steps[].effective_params`; resolved output settings appear in artifact `media.effective_params`. Full codec, size, frame-rate, bitrate, sample-rate and quality controls are documented in the [parameter contract](docs/design/media-parameters.md).

### Waiting, retries and downloads

The clients print a request ID and then the accepted `job_id`. Each invocation creates a new request by default. Reuse an explicit `--request-id` only to retry the same complete request, including source, operation, parameters and upload mode. Use a new ID when changing any of them.

The default wait is 600 seconds; change it with `--wait-seconds`. A wait timeout leaves processing running. To resume, run the same client with `--job-id` and the printed ID; it skips upload and submission. `SOURCE DESTINATION` are still required, but the source is not opened during resume. Use `--help` for all options. `--env-file` selects the private environment file (default: repository `.env`); `--base-url` overrides the connection URL. `--config` only supplies a fallback `public_base_url`, never a Token.

A destination file or frame directory must not already exist. Downloads are streamed and checked against the advertised size and SHA-256 before a completed file appears. Frame collections include `manifest.json` with successful and failed members. A `partial` result saves available members and exits with status 2; a failed task or transfer exits nonzero. The clients do not overwrite existing files or treat an incomplete transfer as a complete result.

## Choose an input and consume results

| Input kind | Use when |
| --- | --- |
| `uploaded_file` | You uploaded bytes through `POST /v1/files` and have a `file_id` |
| `http_file` | The service can fetch your file URL; its source must match configured allow rules |
| `shared_file` | The service and your application share an input volume; pass `storage_id` and relative `key` |
| `artifact` | You want to reuse an existing result; optionally select a collection `member_key` |

HTTP sources are denied by default until configured. Shared input files are borrowed read-only and never deleted by the service. Keep them unchanged until every consuming task reports `inputs_released=true`. Shared volumes do not disable uploads, HTTP downloads or MCP.

Tasks finish as `succeeded`, `partial`, `failed` or `canceled`; inspect each declared output before using it. `POST /v1/jobs/{job_id}/cancel` requests cancellation. Wait for the terminal state and `inputs_released=true` before changing or deleting shared inputs.

Single-file artifacts expose `GET/HEAD /v1/artifacts/{artifact_id}/content`, including Range and ETag support. Frame collections expose a paginated member list and individual file downloads; collections are not automatically zipped.

| Storage and cleanup | Default behavior |
| --- | --- |
| Uploaded inputs | Retained for 24 hours after upload; accepted tasks protect them through queuing, execution and retries |
| Task working files | Reclaimed after the task finishes and actual input/file use ends |
| Published artifacts | Retained for 24 hours after publication; active reads, task references and leases delay deletion |
| Shared source files | Managed by your application, not deleted by this service |

Use `POST /v1/files/{file_id}/release` or `POST /v1/artifacts/{artifact_id}/release` for early reclamation. Release stops new references and waits for existing users. For shared-output reads, obtain a lease before opening the file and release it afterward. HTTP transfers protect their active sends; an explicit lease also protects the interval between metadata retrieval, download and later consumption. Retention and lease periods are configurable; exact expiration times are returned by the API.

## Operate and configure

`docker compose stop` preserves volumes. Before backups or upgrades, stop new submissions, let tasks drain, then stop the service. Back up **state, work, output, private `.env`, structural JSON and image version together**; your application owns shared-input backups. Avoid `docker compose down -v` for routine stops or cleanup.

Run one service instance with its own state and work volumes. The default is one logical Worker, configurable from 1 to 4; increasing it requires workload and resource testing. Do not run several containers against the same state directory.

The current processing scope excludes HDR, 10-bit video, hardware acceleration, non-square SAR, animated/multipage images, HLS processing and arbitrary FFmpeg commands. See the [API contract](docs/api/v1-contract.md) for supported media semantics and errors.

- [Deployment and shared-volume examples](deploy/README.md)
- [HTTP API contract](docs/api/v1-contract.md) and [OpenAPI](docs/api/v1-openapi.json)
- [Implementation and verification status](docs/plans/v1-implementation-progress.md)
