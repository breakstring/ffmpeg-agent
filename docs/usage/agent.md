# Natural-language media tasks

[简体中文](agent.zh-CN.md) · [README](../../README.md) · [Design contract](../design/media-agent.md)

Describe the result you want and provide named input references. PydanticAI creates a validated plan using the same six media operations as the structured API. The existing media service executes that plan. Natural-language HTTP/MCP and optional A2A share persisted task IDs, file services, cancellation and authentication.

## Enable planning

Planning and A2A are disabled by default. Run `python3 deploy/generate-config.py` once to create or migrate `.env` and the structural service JSON, then edit the existing entries in `.env` using your provider settings:

```dotenv
MEDIA_LLM_ENABLED=true
MEDIA_LLM_API_KEY='<provider-api-key>'
MEDIA_LLM_MODEL='<model-id>'
MEDIA_LLM_BASE_URL=https://<provider-host>/compatible-mode/v1
MEDIA_PLANNING_TIMEOUT_SECONDS=300
MEDIA_A2A_ENABLED=false
```

The service Bearer Token is also configured only in `.env` as `MEDIA_SERVICE_TOKEN`; it is separate from the model API key. The generator creates a random Token, and only an explicit `MEDIA_SERVICE_TOKEN=null` selects open service access. Keep `.env` mode `0600` and out of Git. Use single quotes for values containing `$` so Compose preserves them literally. Never `source` a credentials file, put a key on the command line, print resolved Compose configuration, or share container environment output.

The provider must support the configured OpenAI-compatible API. The service does not add provider-specific thinking settings. All planning settings use the same `.env`:

| `.env` variable | Default | Meaning |
| --- | --- | --- |
| `MEDIA_LLM_OUTPUT_MODE` | `prompted` | `native` requires provider support for native structured output |
| `MEDIA_PLANNING_CONCURRENCY` | `2` | Concurrent model requests, from 1 to 4 |
| `MEDIA_MAX_AGENT_REQUESTS` | `32` | Global limit for unfinished planning requests |
| `MEDIA_PLANNING_TIMEOUT_SECONDS` | `300` | Total planning budget in seconds; greater than 0 and at most 300 |
| `MEDIA_PLANNING_MAX_OUTPUT_TOKENS` | `4096` | Maximum model output tokens |
| `MEDIA_A2A_ENABLED` | `false` | Enable the A2A endpoint; requires planning |

The planning budget includes waiting for a planning slot, model requests, controlled correction, plan validation and media job submission. Transcoding, frame extraction and other media work use the independent `task_timeout_seconds` setting in the structural JSON. Its default is `3600` seconds, measured from media job acceptance and including media queueing, execution and artifact publication. Adjust this media job budget for long videos; media execution does not consume the model planning budget.

The consumer skill CLI's `--wait-seconds` only limits how long that client invocation waits. On timeout it preserves the state file without canceling the server task; use `resume` with the same state file to continue querying and downloading. See [consumer skills](consumer-skills.md) for usage.

Compose explicitly forwards these values to the service. A direct Python launch reads `.env` by default; `MEDIA_ENV_FILE` selects another file when needed. Process environment variables override file values. The loader reads values without shell execution or interpolation. The structural JSON selected by `MEDIA_CONFIG_FILE` still supplies directories, storage, source rules and limits appropriate to the machine; it rejects service Token and planning/feature settings so they cannot silently disagree with `.env`. Existing JSON settings are migrated by the generator. `.env.local` is not discovered automatically or copied into the image.

An upgrade does not replace existing `.env` values. If it still contains `MEDIA_PLANNING_TIMEOUT_SECONDS=60`, edit that value to `300`. After changing `.env` or the structural JSON, confirm there are no active planning requests or media jobs, then recreate the service to load the new values:

```sh
docker compose up -d --force-recreate --wait media
```

`GET /v1/capabilities` and MCP `list_capabilities` include an `agent` object with enabled protocols and request limits. They do not expose the model key or provider URL. Disabling planning leaves structured media HTTP/MCP available, and existing agent tasks remain queryable and cancelable over HTTP.

## Submit through HTTP

First upload bytes using `POST /v1/files`, or use an existing shared-file, HTTP-file or artifact reference. Then send this JSON to `POST /v1/agent/tasks` with a stable `Idempotency-Key` header and the same Bearer credential used for other service endpoints:

```json
{
  "intent": "Convert source to an MP4 with width 1280, preserving aspect ratio. Also extract its audio as MP3 and a JPEG frame at 3 seconds.",
  "inputs": {
    "source": {"kind": "uploaded_file", "file_id": "<file_id>"}
  }
}
```

`intent` accepts 1–4000 characters; `inputs` contains 1–4 named references. `output_policy` is optional and uses the same storage and retention settings as structured tasks. The model sees the description, input aliases and supported operations, not the actual file references, source authentication or bytes. Put all required instructions in the description; the planner does not automatically probe media before deciding.

A new request returns HTTP `202`, an `agent_task_id`, and a `Location` header. Replaying the same normalized request and key returns HTTP `200` and the current result; changing the request under that key returns `409 IDEMPOTENCY_CONFLICT`. Acceptance is asynchronous and does not mean a media job has finished.

| Request | Result |
| --- | --- |
| `GET /v1/agent/tasks/{agent_task_id}` | Planning status, verified plan and associated media job, when available |
| `POST /v1/agent/tasks/{agent_task_id}/cancel` | Request cancellation; `202` while `canceling`, otherwise `200` |
| `GET /v1/artifacts/{artifact_id}` | Artifact metadata and HTTP download locations |

Poll until the task reaches a terminal state. `queued`, `planning` and `planned` describe planning/submission; `queued`, `running` and `canceling` also reflect the associated media job. Successful results contain `decision: "execute"`, `plan`, `job_id` and `job`. Read artifacts from `job.outputs`, then use the normal artifact download API with the same credentials.

| Terminal status | How to handle it |
| --- | --- |
| `succeeded` | Declared outputs completed; download the referenced artifacts |
| `partial` | Inspect `job` for successful artifacts and failed outputs/items |
| `needs_input` | Complete the description, including required output format or constraints, and submit with a new key |
| `unsupported` | The requested operation is outside the supported contract; submit a supported request with a new key |
| `failed` | Inspect the stable `error` and associated media job, if present |
| `canceled` | Local planning/media work has stopped; remote provider billing cannot be undone |

The public `message` uses a fixed template for each planning decision. It does not include the model's free-text explanation or identify a request's specific missing details. For `needs_input`, review the original description and supply the information needed to choose a supported operation and its parameters.

A cancellation request is not a stop confirmation. Continue polling while `canceling`. Planning does not reserve uploaded files or artifacts: they must still be eligible when the media job is accepted. Releasing or letting an input expire during planning can therefore fail submission.

## Submit through MCP

When enabled, `/mcp` adds three tools to the original 19:

| Tool | Arguments |
| --- | --- |
| `generate_media` | `request` containing the HTTP request fields, plus `idempotency_key` |
| `get_agent_task` | `agent_task_id` |
| `cancel_agent_task` | `agent_task_id` |

Example `generate_media` arguments:

```json
{
  "request": {
    "intent": "Inspect the audio and provide its properties.",
    "inputs": {"audio": {"kind": "uploaded_file", "file_id": "<file_id>"}}
  },
  "idempotency_key": "inspect-audio-001"
}
```

All three return the same `AgentTaskResult` model as HTTP. `generate_media` calls an external model and is annotated with `openWorldHint: true`. HTTP and MCP share the caller's idempotency scope, so the same request/key can be safely retried across the two transports. Uploads and downloads continue to use HTTP; do not pass base64 file contents to MCP.

## Optional A2A

Set `MEDIA_A2A_ENABLED=true` in `.env` alongside complete planning settings. The official Python A2A SDK exposes JSON-RPC at `/a2a` and the Agent Card at `/.well-known/agent-card.json`. These routes, including the Card, use the same optional Bearer authentication as HTTP/MCP. A2A 1.0 is the supported protocol; clients written for 0.3 require separate compatibility validation.

Send a message with one data part containing the same `AgentRequest`, or text containing the intent plus a data part containing `inputs` and optional `output_policy`. Use a stable message ID for retries. Files are referenced by the existing input model, not embedded bytes. The response is a persisted A2A Task; query it to observe the media result, or request cancellation and keep polling until stopped.

For direct JSON-RPC clients, send `A2A-Version: 1.0` and this body to `/a2a`:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "inspect-audio-001",
      "role": "ROLE_USER",
      "parts": [{"data": {
        "intent": "Inspect the audio and provide its properties.",
        "inputs": {"audio": {"kind": "uploaded_file", "file_id": "<file_id>"}}
      }}]
    },
    "configuration": {"returnImmediately": true}
  }
}
```

`SendMessage` returns the Task in `result.task`. Use its `id` with `GetTask` or `CancelTask`, passing `"params": {"id": "<agent_task_id>"}`; those methods return the Task directly in `result`. An official SDK client is available in [`examples/a2a-workflow.py`](../../examples/a2a-workflow.py). It reads the repository `.env` by default; use `--env-file` for a private client file containing `MEDIA_SERVICE_TOKEN` and `MEDIA_PUBLIC_BASE_URL`:

```sh
output_dir=$(mktemp -d)
printf 'Inspect source and return its media properties.\n' > "$output_dir/intent.txt"
uv run --frozen python examples/a2a-workflow.py \
  tests/fixtures/audio_mp3_mono_22050.mp3 "$output_dir/results" \
  --intent-file "$output_dir/intent.txt" --env-file .env
```

The client does not read Tokens from `--config`; that option only supplies a fallback service URL.

A2A JSON-RPC returns `status.state` as `TASK_STATE_WORKING` while queued/planning/running/canceling, `TASK_STATE_COMPLETED` on success, `TASK_STATE_FAILED` for partial or failed media results, and `TASK_STATE_CANCELED` after actual cancellation. `needs_input` and `unsupported` become `TASK_STATE_REJECTED`: submit a new complete message with a new message ID. Partial results preserve successful artifact references. Artifact URLs require the same service credential and never include a Token in the URL.

This release does not support task/context continuations, streaming, push notifications, arbitrary FFmpeg commands, subtitles, concatenation, HDR or hardware acceleration. See the [API contract](../api/v1-contract.md) for the complete public boundary and the [implementation progress](../plans/v1-implementation-progress.md) for recorded verification and remaining checks.
