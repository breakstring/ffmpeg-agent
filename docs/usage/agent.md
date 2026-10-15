# Natural-language media tasks

[简体中文](agent.zh-CN.md) · [README](../../README.md) · [Design contract](../design/media-agent.md)

Describe the result you want and provide named input references. PydanticAI creates a validated plan using the same six media operations as the structured API. The existing media service executes that plan. Natural-language HTTP/MCP and optional A2A share persisted task IDs, file services, cancellation and authentication.

## Enable planning

Planning and A2A are disabled by default. Add the following fields to the private service JSON, substituting your provider settings privately:

```json
{
  "llm_enabled": true,
  "llm_api_key": "<provider-api-key>",
  "llm_model": "<model-id>",
  "llm_base_url": "https://<provider-host>/compatible-mode/v1",
  "a2a_enabled": false
}
```

These are additional configuration fields, not a complete service configuration. Keep the file and credentials out of Git. The provider must support the configured OpenAI-compatible API. The default `llm_output_mode` is `prompted`; `native` requires provider support for native structured output. The service does not add provider-specific thinking settings.

| Setting | Default | Meaning |
| --- | --- | --- |
| `planning_concurrency` | `2` | Concurrent model requests, from 1 to 4 |
| `max_agent_requests` | `32` | Global limit for unfinished planning requests |
| `planning_timeout_seconds` | `60` | Total planning budget, including any controlled correction |
| `planning_max_output_tokens` | `4096` | Maximum model output tokens |
| `a2a_enabled` | `false` | Enable the A2A endpoint; requires planning |

For explicit local use, `MEDIA_ENV_FILE=.env.local` tells the launcher to load that file. It accepts the existing `llm_api_key`, `llm_default_model`, `llm_base_url` names as well as `MEDIA_LLM_*` settings. The service does not automatically discover `.env.local` in its working directory. Docker deployments use their private JSON configuration; the local file is not copied into the image. See [`.env.example`](../../.env.example) for placeholders. Never `source` a credentials file or put a key on the command line.

When using the local file, set `MEDIA_LLM_ENABLED=true`; set `MEDIA_A2A_ENABLED=true` to enable A2A as well. The example keeps both switches off, and the three compatibility aliases only configure the model. A direct Python launch still needs `MEDIA_CONFIG_FILE` to point to a complete service JSON with directories, runtime mode and engine version appropriate to that machine.

With the repository's Compose setup, edit the private JSON selected by `MEDIA_CONFIG_PATH` (default `deploy/local/media-config.json`). Compose uses `.env` for its own substitutions and does not forward `MEDIA_LLM_*`, `MEDIA_A2A_ENABLED` or `MEDIA_ENV_FILE` to the container. After changing the private JSON, recreate the service so it reads the new configuration:

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

Set `a2a_enabled: true` alongside complete planning settings. The official Python A2A SDK exposes JSON-RPC at `/a2a` and the Agent Card at `/.well-known/agent-card.json`. These routes, including the Card, use the same optional Bearer authentication as HTTP/MCP. A2A 1.0 is the supported protocol; clients written for 0.3 require separate compatibility validation.

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

`SendMessage` returns the Task in `result.task`. Use its `id` with `GetTask` or `CancelTask`, passing `"params": {"id": "<agent_task_id>"}`; those methods return the Task directly in `result`. An official SDK client is available in [`examples/a2a-workflow.py`](../../examples/a2a-workflow.py).

A2A JSON-RPC returns `status.state` as `TASK_STATE_WORKING` while queued/planning/running/canceling, `TASK_STATE_COMPLETED` on success, `TASK_STATE_FAILED` for partial or failed media results, and `TASK_STATE_CANCELED` after actual cancellation. `needs_input` and `unsupported` become `TASK_STATE_REJECTED`: submit a new complete message with a new message ID. Partial results preserve successful artifact references. Artifact URLs require the same service credential and never include a Token in the URL.

This release does not support task/context continuations, streaming, push notifications, arbitrary FFmpeg commands, subtitles, concatenation, HDR or hardware acceleration. See the [API contract](../api/v1-contract.md) for the complete public boundary and the [implementation progress](../plans/v1-implementation-progress.md) for recorded verification and remaining checks.
