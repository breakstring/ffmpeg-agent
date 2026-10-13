# ffmpeg-agent

**English** | [简体中文](README.zh-CN.md)

Process media from applications or remote AI agents through HTTP and MCP. Upload a file, submit an asynchronous task, and download the result. Shared volumes are optional for clients; you can combine them with HTTP transfers.

| What you can do | Operation |
| --- | --- |
| Inspect media and download a JSON report | `media.probe.v1` |
| Convert video to web-ready MP4 / H.264, up to 720p at 30 fps | `video.transcode.v1` |
| Convert audio to AAC stereo or 16 kHz mono WAV | `audio.transcode.v1` |
| Extract and convert a selected audio track from video | `video.extract_audio.v1` |
| Extract JPEG or PNG frames at requested timestamps | `video.extract_frames.v1` |
| Resize and convert static JPEG, PNG and WebP images | `image.resize.v1` |

Tasks can chain operations, reuse inputs and return multiple outputs. Query `GET /v1/capabilities` for the running service's presets and limits.

## Start with Docker Compose

You need Docker with Compose and Python 3. Run these commands from the repository root; installing the service's Python dependencies on your host is unnecessary. If `.env` already exists, keep it and skip the copy command.

```sh
cp .env.example .env
python3 deploy/generate-config.py
docker compose up -d --build
docker compose logs -f media
```

Exit the log viewer with Ctrl+C, then check readiness:

```sh
curl --fail http://127.0.0.1:8080/health/ready
```

The generator creates `deploy/local/media-config.json` with a random service Token. It refuses to overwrite existing configuration; skip generation if you already have that file. Keep its parent directory private (`0700`).

Edit `.env` for the image name, host address, port, configuration file location and container resource limits. Edit the private JSON for the Token, public URL, storage and service limits. Tokens belong in the private JSON, never in `.env`, command arguments or Git. A Token protects HTTP, MCP, uploads and downloads through the same `Authorization: Bearer ...` header; setting `service_token` to `null` opens these endpoints.

| `.env` variable | Default | Purpose |
| --- | --- | --- |
| `MEDIA_IMAGE` | `ffmpeg-agent:v1-local` | Local image name |
| `MEDIA_BIND_ADDRESS` / `MEDIA_PORT` | `127.0.0.1` / `8080` | Published host address and port |
| `MEDIA_CONFIG_PATH` | `./deploy/local/media-config.json` | Private JSON path, relative to the repository root |
| `MEDIA_CPUS` / `MEDIA_MEMORY` / `MEDIA_PIDS_LIMIT` | `2.0` / `2g` / `128` | Container CPU, memory and process limits |

In the private JSON, `service_token` controls authentication, `public_base_url` determines client and artifact URLs, and `worker_concurrency` sets logical task concurrency (1–4, default 1). If you change `MEDIA_PORT`, update `public_base_url` and the readiness-check URL too; otherwise the examples and returned artifact URLs still point to port 8080.

The default service is reachable only on the host at port 8080. To connect remotely, provide an HTTPS entry point, set `public_base_url` to its externally reachable URL, and forward the correct public Host header. MCP validates Host and Origin against that address. The client examples below read this JSON; use a private client copy when calling from another machine.

## Upload, process and download

The following Python 3 standard-library example streams the upload and download without loading the whole video into memory. It converts `sample.mp4`, waits for the task and verifies the result's length and SHA-256. Keep the source unchanged while it runs.

Save the code as `example.py`, place your video at `sample.mp4`, and run:

```sh
python3 example.py sample.mp4 result.mp4
```

The destination must not already exist. If you changed `MEDIA_CONFIG_PATH`, update the config path in the example too.

```python
import hashlib
import http.client
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

source, destination = map(Path, sys.argv[1:3])
config = json.loads(Path("deploy/local/media-config.json").read_text())
base = config["public_base_url"].rstrip("/")
token = config["service_token"]
auth = {"Authorization": "Bearer " + token} if token else {}


def api(method, path, body=None, extra=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {**auth, **(extra or {})}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response) if response.status != 204 else None


hasher = hashlib.sha256()
with source.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        hasher.update(chunk)
source_digest = hasher.hexdigest()
url = urllib.parse.urlsplit(base)
connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
connection = connection_type(url.hostname, url.port, timeout=300)
try:
    connection.putrequest("POST", url.path.rstrip("/") + "/v1/files")
    headers = {
        **auth,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(source.stat().st_size),
        "X-File-Sha256": source_digest,
        "Idempotency-Key": "readme-file-" + source_digest,
    }
    for name, value in headers.items():
        connection.putheader(name, value)
    connection.endheaders()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            connection.send(chunk)
    response = connection.getresponse()
    if response.status not in (200, 201):
        raise RuntimeError("Upload HTTP status: " + str(response.status))
    uploaded = json.loads(response.read())
finally:
    connection.close()

job = api("POST", "/v1/jobs", {
    "inputs": {"source": {"kind": "uploaded_file", "file_id": uploaded["file_id"]}},
    "steps": [{
        "id": "encode", "operation": "video.transcode.v1",
        "inputs": {"video": {"source": "source"}},
        "params": {"preset": "web_720p_v1"},
    }],
    "outputs": {"video": {"step": "encode", "output": "video"}},
}, {"Idempotency-Key": "readme-video-" + source_digest})
print("file_id:", uploaded["file_id"], "job_id:", job["job_id"])
deadline = time.monotonic() + 600
while job["status"] not in {"succeeded", "partial", "failed", "canceled"}:
    if time.monotonic() >= deadline:
        raise TimeoutError("Keep polling the same job_id; do not submit a duplicate job")
    time.sleep(1)
    job = api("GET", "/v1/jobs/" + job["job_id"])
if job["status"] != "succeeded":
    raise RuntimeError("Task status: " + job["status"])
artifact_id = job["outputs"]["video"]["artifact_id"]
lease = api("POST", f"/v1/artifacts/{artifact_id}/leases", {"duration_seconds": 900})
try:
    headers = {"X-Artifact-Lease-Id": lease["lease_id"]}
    artifact = api("GET", f"/v1/artifacts/{artifact_id}", extra=headers)
    request = urllib.request.Request(artifact["access"]["http"]["content"], headers={**auth, **headers})
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=300) as response, destination.open("xb") as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        if int(response.headers["Content-Length"]) != size:
            raise RuntimeError("Incomplete HTTP download")
    if size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
        raise RuntimeError("Artifact integrity check failed")
finally:
    api("DELETE", "/v1/leases/" + lease["lease_id"])
print("Download verified")
```

Upload and task submission have separate idempotency keys. This example derives stable keys from the file digest: retries with the same file return the existing input and task rather than processing twice. Use new keys when you intentionally want a new run; an expired/released upload cannot be recreated with its old key. If waiting times out, keep querying the printed `job_id`.

### Upload and submit in one request

`POST /v1/jobs/upload` accepts multipart data, with the `job` JSON part first and its declared file parts afterward. The response returns a `job_id` once upload and task acceptance finish; processing remains asynchronous.

This optional example uses the project's Python 3.14 environment and requires uv: run `uv sync --frozen`, save it as `multipart-example.py`, then execute `.venv/bin/python multipart-example.py` with `sample.mp4` present. Reuse its key for retries with the same request; choose a new key for another task.

```python
import json
from pathlib import Path

import httpx

config = json.loads(Path("deploy/local/media-config.json").read_text())
token = config["service_token"]
headers = {"Authorization": "Bearer " + token} if token else {}
headers["Idempotency-Key"] = "readme-multipart-video-001"
job = {
    "inputs": {"source": {"kind": "upload", "part": "source"}},
    "steps": [{
        "id": "encode", "operation": "video.transcode.v1",
        "inputs": {"video": {"source": "source"}},
        "params": {"preset": "web_720p_v1"},
    }],
    "outputs": {"video": {"step": "encode", "output": "video"}},
}
with Path("sample.mp4").open("rb") as stream, httpx.Client(timeout=300) as client:
    response = client.post(
        config["public_base_url"].rstrip("/") + "/v1/jobs/upload",
        headers=headers,
        files=[
            ("job", (None, json.dumps(job), "application/json")),
            ("source", ("sample.mp4", stream, "application/octet-stream")),
        ],
    )
    response.raise_for_status()
    print(response.json()["job_id"])
```

Query `GET /v1/jobs/{job_id}` and download the output through the artifact endpoints as shown above. You can also upload once with `POST /v1/files`, query `GET /v1/files/{file_id}`, and reuse the input in several tasks.

## Connect a remote MCP client

Use the Streamable HTTP endpoint at `public_base_url + /mcp` (locally, `http://localhost:8080/mcp`). Set the service Bearer header in the client's connection settings, and set the same header separately for file HTTP requests. The Token is never a tool argument or part of a download URL.

Discover the tools with `tools/list`. For example, call `transcode_video` with the following arguments, substituting the real uploaded `file_id`:

```json
{
  "source": {"kind": "uploaded_file", "file_id": "<file_id returned by HTTP upload>"},
  "params": {"preset": "web_720p_v1"},
  "idempotency_key": "readme-mcp-video-001"
}
```

The tool returns `job_id`. Call `get_job` with `{"job_id":"<returned job_id>"}` until it finishes, then `get_artifact` with the returned `artifact_id` to obtain download information. Media tools name their declared output `result`, so its ID is under `outputs.result.artifact_id`.

The six media tools are `probe_media`, `transcode_video`, `transcode_audio`, `extract_audio`, `extract_frames` and `resize_image`. Other tools cover capabilities, task graphs, cancellation, input release, artifacts and leases. See the [MCP contract](docs/api/v1-contract.md#远程-mcp) for their arguments.

File bytes travel through HTTP: **upload → `file_id` → MCP processing/query → `artifact_id` → download**. MCP also accepts the other input types below. Only remote MCP is provided; the server cannot read paths on the client's computer.

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

The current presets do not support HDR, 10-bit video, non-square SAR, animated/multipage images, HLS processing or arbitrary FFmpeg commands. See the [API contract](docs/api/v1-contract.md) for supported media semantics and errors.

- [Deployment and shared-volume examples](deploy/README.md)
- [HTTP API contract](docs/api/v1-contract.md) and [OpenAPI](docs/api/v1-openapi.json)
- [Implementation and verification status](docs/plans/v1-implementation-progress.md)
