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

## Start with Docker Compose

You need Docker with Compose and Python 3. Run these commands from the repository root. Existing configuration is kept, so you can run them again:

```sh
if [ ! -f .env ]; then cp .env.example .env; fi
if [ ! -f deploy/local/media-config.json ]; then
  python3 deploy/generate-config.py
fi
docker compose up -d --build --wait
```

Compose waits for the service to become ready. The generator creates a random service Token in `deploy/local/media-config.json`. Keep its parent directory private (`0700`). The examples below use that file automatically; you do not need to copy the Token into commands.

Edit `.env` for the published address, port and container resources. Edit the private JSON for the Token, public URL, storage and service limits. A Token protects HTTP, MCP, uploads and downloads through the same Bearer header; setting `service_token` to `null` opens these endpoints.

| `.env` variable | Default | Purpose |
| --- | --- | --- |
| `MEDIA_IMAGE` | `ffmpeg-agent:v1-local` | Local image name |
| `MEDIA_BIND_ADDRESS` / `MEDIA_PORT` | `127.0.0.1` / `8080` | Published host address and port |
| `MEDIA_CONFIG_PATH` | `./deploy/local/media-config.json` | Private service JSON path, relative to the repository root |
| `MEDIA_CPUS` / `MEDIA_MEMORY` / `MEDIA_PIDS_LIMIT` | `2.0` / `2g` / `128` | Container CPU, memory and process limits |

If you use a custom `MEDIA_CONFIG_PATH`, generate or keep that configuration and pass its path to examples with `--config`. If you change the port, update `public_base_url` in the JSON too. `worker_concurrency` sets logical task concurrency (1–4, default 1).

The default service is reachable only on the host. For remote use, provide an HTTPS entry point, set `public_base_url` to the externally reachable URL, and forward the correct public Host header. MCP validates Host and Origin against that address. A remote client needs only a private JSON containing `public_base_url` and `service_token`, passed with `--config`; it does not need the server's storage or deployment configuration. Keep Tokens out of command arguments and Git.

## Upload, process and download

The repository includes complete clients that upload a local file, wait for processing, and download and verify the result. All commands below use existing sample files and a new temporary output directory, so they can be copied and run again. Keep the source unchanged while uploading.

### HTTP: upload and submit together

This Python 3 standard-library client needs no extra packages. By default, it converts video to H.264/MP4, preserving source dimensions and frame timing:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4"
```

It sends one multipart request to `POST /v1/jobs/upload`, polls the returned task, and saves the verified MP4. You do not need to supply encoding parameters, handle IDs manually, or mount a shared volume.

### HTTP: upload first, then submit

Add `--upload-first` to use `POST /v1/files` followed by `POST /v1/jobs`. The client also prints the `file_id`, which can be reused in other tasks while the upload is available:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4" \
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
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4"
```

The client uploads over HTTP, calls the remote `transcode_video` tool, waits through MCP, and downloads over HTTP. It uses the same private configuration and Token for both transports. There is no local stdio server to configure.

To connect another MCP client, use the Streamable HTTP endpoint at `public_base_url + /mcp` (locally `http://localhost:8080/mcp`) and configure the service Bearer header in its connection settings. File HTTP requests need the same header. **MCP alone cannot upload a path from your computer:** the client needs HTTP file-transfer capability, or an input URL/shared file the service can access.

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
| `probe_media` | `ffmpeg_h264_640x360.mkv` | `report.json`: format and stream information |
| `transcode_video` | `ffmpeg_h264_640x360.mkv` | `result.mp4`: H.264, CRF 23, source dimensions and timing |
| `transcode_audio` | `222.mp3` | `result.m4a`: AAC, compatible sample rate/channels/bitrate chosen automatically |
| `extract_audio` | `ffmpeg_h264_640x360.mkv` | `result.m4a`: first audio track, converted to AAC |
| `extract_frames` | `ffmpeg_h264_640x360.mkv` | `frames/`: first frame as JPEG quality 90, plus `manifest.json` |
| `resize_image` | `haojiao.jpg` | `result.jpg`: original dimensions, JPEG quality 90 |

For example, download a frame collection with one command:

```sh
output_dir=$(mktemp -d)
printf 'Output directory: %s\n' "$output_dir"
python3 examples/http-workflow.py \
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/frames" \
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
  tests/fixtures/ffmpeg_h264_640x360.mkv "$output_dir/result.mp4" \
  --params "$output_dir/params.json"
```

The same file works with the MCP client. In your own requests, put this object in the HTTP step's `params` or the MCP tool's `params`.

Presets are optional shortcuts: `{"preset":"web_720p_v1"}` selects 720p/30 fps with stereo AAC; `aac_stereo_128k_v1` selects AAC 128 kbps/48 kHz/stereo; `wav_mono_16k_v1` selects 16 kHz mono PCM. Explicit settings override the selected preset. Without a preset, video preserves source dimensions and timing. Audio `auto` prefers source sample rate/channels and falls back to compatible values; `source` requires exact preservation. Audio copy mode preserves the original encoding and cannot be combined with encoding settings or trimming.

Query `GET /v1/capabilities` or MCP `list_capabilities` for available encoders, schemas, defaults and limits. Accepted settings appear in task `steps[].effective_params`; resolved output settings appear in artifact `media.effective_params`. Full codec, size, frame-rate, bitrate, sample-rate and quality controls are documented in the [parameter contract](docs/design/media-parameters.md).

### Waiting, retries and downloads

The clients print a request ID and then the accepted `job_id`. Each invocation creates a new request by default. Reuse an explicit `--request-id` only to retry the same complete request, including source, operation, parameters and upload mode. Use a new ID when changing any of them.

The default wait is 600 seconds; change it with `--wait-seconds`. A wait timeout leaves processing running. To resume, run the same client with `--job-id` and the printed ID; it skips upload and submission. `SOURCE DESTINATION` are still required, but the source is not opened during resume. Use `--help` for all options. `--base-url` overrides the connection URL; `--config` selects a private client JSON.

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

`docker compose stop` preserves volumes. Before backups or upgrades, stop new submissions, let tasks drain, then stop the service. Back up **state, work, output, private configuration and image version together**; your application owns shared-input backups. Avoid `docker compose down -v` for routine stops or cleanup.

Run one service instance with its own state and work volumes. The default is one logical Worker, configurable from 1 to 4; increasing it requires workload and resource testing. Do not run several containers against the same state directory.

The current processing scope excludes HDR, 10-bit video, hardware acceleration, non-square SAR, animated/multipage images, HLS processing and arbitrary FFmpeg commands. See the [API contract](docs/api/v1-contract.md) for supported media semantics and errors.

- [Deployment and shared-volume examples](deploy/README.md)
- [HTTP API contract](docs/api/v1-contract.md) and [OpenAPI](docs/api/v1-openapi.json)
- [Implementation and verification status](docs/plans/v1-implementation-progress.md)
