# Consumer Agent connection and media processing

**English** | [简体中文](consumer-skills.zh-CN.md)

When an administrator has already deployed the service, consumers only need the skills and connection settings. They do not need another server. This project provides two independently installable Agent Skills:

| Skill | When to use it | Outcome |
| --- | --- | --- |
| [`ffmpeg-agent-connect`](../../skills/ffmpeg-agent-connect/SKILL.md) | First connection, changing services, connection failures | Verify authentication, protocols and capabilities, then file processing when requested |
| [`ffmpeg-agent-media`](../../skills/ffmpeg-agent-media/SKILL.md) | Everyday media work | Upload, submit, wait or resume, download and verify, release this run's uploads |

Both skills include the same standalone client. Either complete directory works without the other skill, server source, Docker, FFmpeg or extra Python packages. Python 3.10 or newer is required.

## Install the complete skills

You can ask your Agent to install both skills using the [copyable README prompt](../../README.md#connect-your-agent-to-an-existing-service), or install them manually. Obtain `skills/ffmpeg-agent-connect/` and `skills/ffmpeg-agent-media/` from this repository. Keep `SKILL.md`, `references/`, `agents/` and all of `scripts/`; copying only `SKILL.md` does not install the client.

| Local host | Personal skill directory | Project skill directory |
| --- | --- | --- |
| Codex | `~/.agents/skills/` | `.agents/skills/` in your working project |
| Claude Code | `~/.claude/skills/` | `.claude/skills/` in your working project |
| Other Agent Skills hosts | Use the host's documented location | Use the host's documented location |

The paths above describe local skill discovery; cloud hosts can have different installation mechanisms. Sources: [Codex skills](https://learn.chatgpt.com/docs/build-skills) and [Claude Code skills](https://code.claude.com/docs/en/skills), checked on 2026-10-16.

On macOS/Linux, these commands download a copy and install both complete directories for Codex. They require Git and Python 3.10+ for installation, and refuse to overwrite existing skills. For Claude Code, change the first line to `skills_root="$HOME/.claude/skills"`; for a project installation, use its absolute skill-directory path.

```sh
skills_root="$HOME/.agents/skills"
skill_source=$(mktemp -d)
git clone --depth 1 https://github.com/breakstring/ffmpeg-agent.git "$skill_source/ffmpeg-agent"
python3 - "$skill_source/ffmpeg-agent/skills" "$skills_root" <<'PYTHON'
from pathlib import Path
import shutil
import sys

source, target = map(Path, sys.argv[1:])
names = ("ffmpeg-agent-connect", "ffmpeg-agent-media")
if any((target / name).exists() or (target / name).is_symlink() for name in names):
    raise SystemExit("Skill already exists; review the installed version before updating.")
target.mkdir(parents=True, exist_ok=True)
for name in names:
    shutil.copytree(source / name, target / name)
PYTHON
python3 "$skills_root/ffmpeg-agent-connect/scripts/media_client.py" --help
```

The expected layout is `skills_root/ffmpeg-agent-media/SKILL.md` and `skills_root/ffmpeg-agent-media/scripts/media_client.py`, with the same structure for connect. Refresh skills or restart the host if needed. Review local changes before updating an existing installation; do not merge a new client into an old skill blindly. The installed skills do not depend on the downloaded repository, so you do not need to build the server or run `uv sync`. A host without skill loading can still run the CLI directly.

## Configure a private service connection

The service administrator supplies the root URL and service Token. Create a private file on the machine where the client will run. The following macOS/Linux commands create a template and refuse to overwrite an existing configuration:

```sh
consumer_private="$HOME/.config/ffmpeg-agent"
mkdir -p "$consumer_private"
chmod 700 "$consumer_private"
consumer_env="$consumer_private/consumer.env"
(
  umask 077
  set -C
  cat > "$consumer_env" <<'ENV'
MEDIA_PUBLIC_BASE_URL=https://media.example.com
MEDIA_SERVICE_TOKEN=<service-token-from-administrator>
ENV
)
```

**Before connecting, open this file in a private local editor and replace both placeholders.** Do not paste the Token into Agent chat or command arguments. If you already have a private configuration, reuse its path instead. On other systems, create the same two settings and apply equivalent private file permissions.

Use the service root URL, without `/mcp` or `/a2a`. The URL must be reachable from the Agent's execution environment: `127.0.0.1` inside a remote Agent or container refers to that environment, not your computer. Use HTTPS for remote services; HTTP is suitable for local loopback testing. If the administrator explicitly selected open access, set `MEDIA_SERVICE_TOKEN=null`; do not leave it empty.

Consumers do not need the model key or deployment JSON. Do not copy the server's entire `.env`, print the consumer file or `source` it. Process environment variables with the same names override this file; check for another environment's overrides before connecting. State files contain local paths, file references and task IDs, so keep them private and out of Git too.

Continue in the same shell after installation and configuration:

```sh
skill_dir="$skills_root/ffmpeg-agent-media"
consumer_run=$(mktemp -d "$consumer_private/run.XXXXXX")
```

If using an existing installation or a new shell, set `skill_dir`, `consumer_env` and `consumer_run` to the corresponding absolute paths first. Create a new private run directory for new tasks; output subdirectories used below must not exist. You can point `skill_dir` to `ffmpeg-agent-connect` instead, whose client behaves identically. Commands pass the configuration file path, never the Token. Full options are available with `python3 "$skill_dir/scripts/media_client.py" --help`.

## Connect for the first time

Ask a host that supports the skill:

> Use ffmpeg-agent-connect to connect to the service in my private configuration, check the available protocols, then verify the complete workflow with the small file I choose.

The initial checks are:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" check --protocol all
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" capabilities
```

`check` returns JSON with HTTP, MCP and A2A results. With `all`, disabled A2A is reported as `enabled: false` without treating the whole service as failed. Use `--protocol http`, `mcp` or `a2a` for a specific protocol. This check does not upload media or prove processing works; complete an end-to-end run below for that evidence.

| Host support | Suggested connection |
| --- | --- |
| Already connected to remote MCP | Prefer the host's native MCP; use the client for local file transfer, recovery and downloads |
| Supports remote MCP, not yet configured | Service root + `/mcp`, Streamable HTTP; use the host's documented authentication configuration |
| Supports A2A and needs natural-language delegation | Read `/.well-known/agent-card.json`; this service currently provides A2A 1.0 at `/a2a` |
| Can execute Python, no native protocol support | Use the bundled client with an explicit HTTP, MCP or A2A transport |

All endpoints and file transfers use the same Bearer authentication in Token mode; open mode sends no authentication header. The skills do not guess host configuration paths, replace other server entries or enable server features. There is no stdio MCP endpoint. A2A 0.3 hosts need separate compatibility verification. Successful protocol checks in the helper client do not prove the host's native integration works.

Structured operations do not require a server model. Natural-language planning and A2A are optional; inspect current capabilities. Use capability results and MCP `tools/list` to choose encoders, formats and parameters rather than assuming every deployment supports all documented options.

## Defined operations: HTTP or MCP

Default video conversion preserves source dimensions and frame timing. This command uploads a local file, waits for media completion, then downloads and verifies the result:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --operation video.transcode.v1 --transport http \
  --state "$consumer_run/http-run.json" \
  --output-dir "$consumer_run/http-results"
```

Submit the same operation through remote MCP using a new state file and output directory:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --operation video.transcode.v1 --transport mcp \
  --state "$consumer_run/mcp-run.json" \
  --output-dir "$consumer_run/mcp-results"
```

The CLI uses six versioned operations: `media.probe.v1`, `video.transcode.v1`, `audio.transcode.v1`, `video.extract_audio.v1`, `video.extract_frames.v1` and `image.resize.v1`. Omit `--params` for defaults. To change settings, pass an inline JSON parameter object, overriding only what is needed. For example, `--params '{"resize":{"width":1280}}'` limits video width without separately imposing frame rate or audio settings. This consumer CLI's `--params` does not accept a file path. See the [parameter guide](parameters.md), subject to the current service's capabilities.

For multiple steps, use `--definition-file` instead of `--operation`. Its JSON contains `steps`, `outputs` and optional `output_policy`; uploads supply `inputs`. Repeat `--input "name=/absolute/path"` for up to four named inputs. A definition example is included in the skill's [workflow reference](../../skills/ffmpeg-agent-media/references/workflow.md#明确操作http-或-mcp).

## Use native MCP tools

Remote MCP cannot read paths on your computer. First upload through HTTP and pass the returned references to the already connected MCP tools:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" upload \
  --input "source=/absolute/path/to/input.mp4" \
  --state "$consumer_run/native-mcp-run.json"
```

The result includes input references and a stable `request_id`. Discover tool schemas with `tools/list`, build the request using those references, and use `request_id` as `idempotency_key`. Do not send local paths or base64 file bytes to MCP. After receiving `job_id`, hand off waiting and downloads:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" collect \
  --state "$consumer_run/native-mcp-run.json" \
  --job-id "<returned-job-id>" \
  --output-dir "$consumer_run/native-mcp-results"
```

For a natural-language tool returning `agent_task_id`, use `--agent-task-id "<returned-agent-task-id>"` instead. `collect` does not upload or submit again. Bind only the task returned for this request. If the submission result is uncertain, recover using the same request and idempotency key rather than creating another request.

## Natural-language tasks and A2A

When server-side planning is desired, save the user's intent in a UTF-8 `intent.txt`, for example:

```text
Convert source to an H.264 MP4 no wider than 1280 pixels and extract an MP3 audio track. Return both outputs.
```

After the server has enabled planning and A2A:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --intent-file "$consumer_run/intent.txt" --transport a2a \
  --state "$consumer_run/a2a-run.json" \
  --output-dir "$consumer_run/a2a-results"
```

Natural-language tasks also support explicit `--transport http` or `mcp`. A2A only accepts intent mode, not `--operation` or `--definition-file`. The model key stays on the server. The client does not enable server features, silently switch protocols after failure or repeatedly call the model.

A completed plan or returned A2A Task is not media completion. The client follows the associated media task, then downloads and verifies artifacts. For `needs_input`, add information necessary to express a supported task; for `unsupported`, adjust the request to supported capabilities. Submit changed requests using a new state file. A `partial` result preserves usable outputs, but failed outputs or collection members must be reported.

## Wait, recover, cancel and release

Use a new state file for each new task. It persistently records stable request and task references. `run`, `collect`, `resume` and `cancel` accept `--wait-seconds` to limit that invocation's wait. A timeout neither cancels the server task nor means processing failed:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" status \
  --state "$consumer_run/http-run.json"
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" resume \
  --state "$consumer_run/http-run.json" --wait-seconds 600
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" cancel \
  --state "$consumer_run/http-run.json" --wait-seconds 600
```

After a network interruption or uncertain submission, preserve the original state. Use `resume` for CLI submissions. For a native MCP submission not yet bound to state, recover the task with the same request and idempotency key, then `collect`. Do not upload again, replace the key or create another request. Use a new state only for a new or changed request. Do not edit saved requests, IDs or references, or run concurrent commands against one state file.

Accepted cancellation is not proof that work stopped. Wait for an actual terminal state and `inputs_released=true`; if completion races with cancellation, use the returned terminal state. Successful or partial collection automatically releases this run's managed uploads after they are no longer in use. To explicitly clean up a failed, canceled or unsubmitted upload:

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" release \
  --state "$consumer_run/http-run.json"
```

The client rejects release while its task is active. Release affects only uploads recorded by that state, not local source files or published result artifacts. Published artifacts remain subject to the server's retention policy; download them promptly.

Downloads use new destinations, verify size and SHA-256, and do not overwrite existing files. Outputs use their declared names; a single operation defaults to `result`, without an automatically added extension. Use artifact metadata for the actual format and the returned JSON's `downloads` for local paths. A collection uses a directory of the same name containing members and `manifest.json`.

Commands return machine-readable JSON. Callers can also distinguish results by exit code:

| Exit code | Meaning |
| --- | --- |
| `0` | Command succeeded; use the JSON status for the actual media or cancellation outcome |
| `1` | Request/processing failure, or a result such as `needs_input` or `unsupported` requiring action |
| `2` | Partial media success or incomplete downloads; inspect `downloads` and output states |
| `3` | This wait timed out; resume the same state |
| `130` | Client interrupted; retain and resume the original state |

Report actual media state and verified files without exposing configuration, raw state, authentication headers or sensitive paths.

## Maintainers: shared client

The canonical client logic is in [`consumer/ffmpeg_agent_client/`](../../consumer/ffmpeg_agent_client/); its `envfile.py` is generated from the shared [`src/media_service/envfile.py`](../../src/media_service/envfile.py). Client entrypoints and packages inside the skills are distribution copies too. Make fixes in the canonical source, not generated copies, then run:

```sh
python3 scripts/sync-consumer-skills.py
python3 scripts/sync-consumer-skills.py --check
```

This generates each skill's `scripts/media_client.py` and `scripts/ffmpeg_agent_client/`, and checks the distributed contents. Each skill remains independently installable while client logic has one maintained source.
