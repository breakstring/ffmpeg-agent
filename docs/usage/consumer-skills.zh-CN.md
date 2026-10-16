# 消费者 Agent 接入与媒体处理

[English](consumer-skills.md) | **简体中文**

服务已由管理员部署时，消费者安装技能并配置连接信息即可使用，不需要部署另一个服务。本项目提供两个可单独安装的 Agent Skills：

| 技能 | 使用时机 | 交付 |
| --- | --- | --- |
| [`ffmpeg-agent-connect`](../../skills/ffmpeg-agent-connect/SKILL.md) | 首次连接、更换服务、连接失败 | 验证认证、可用协议和能力，按需完成文件处理验证 |
| [`ffmpeg-agent-media`](../../skills/ffmpeg-agent-media/SKILL.md) | 日常媒体处理 | 上传、提交、等待或恢复、下载校验和本次上传释放 |

两个技能都自带相同的独立客户端。安装任意一个完整目录即可运行，不依赖另一个技能、服务端源码、Docker、FFmpeg 或额外 Python 包；客户端最低 Python 3.10。

## 安装完整技能

可以直接将 [README 中的安装话术](../../README.zh-CN.md#让你的-agent-使用已部署的服务)交给 Agent，也可以手工安装。从本仓库取得 `skills/ffmpeg-agent-connect/` 和 `skills/ffmpeg-agent-media/`，保留 `SKILL.md`、`references/`、`agents/` 和完整 `scripts/`；只复制 `SKILL.md` 不能运行客户端。

| 本地宿主 | 个人技能目录 | 项目技能目录 |
| --- | --- | --- |
| Codex | `~/.agents/skills/` | 用户工作项目中的 `.agents/skills/` |
| Claude Code | `~/.claude/skills/` | 用户工作项目中的 `.claude/skills/` |
| 其他 Agent Skills 宿主 | 以宿主文档为准 | 以宿主文档为准 |

上述路径用于本机技能发现；云端宿主的安装方式可能不同。依据：[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills)，核对日期为 2026-10-16。

macOS/Linux 可使用以下命令下载并将两个完整目录安装到 Codex；安装时需要 Git 和 Python 3.10+，已有同名技能时会停止，不覆盖。Claude Code 将第一行改成 `skills_root="$HOME/.claude/skills"`；项目级安装则改成该项目技能目录的绝对路径。

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

安装后应存在 `skills_root/ffmpeg-agent-media/SKILL.md` 和 `skills_root/ffmpeg-agent-media/scripts/media_client.py`，connect 的结构相同。按宿主要求刷新技能或重新启动会话。更新已有技能前先检查版本和本地修改，不要将新客户端直接混入旧版本目录。安装后的技能不依赖下载的仓库，无需构建服务端或运行 `uv sync`；不支持技能加载的宿主仍可直接执行 CLI。

## 配置私有服务连接

由服务管理员提供服务根地址和服务 Token，在客户端实际运行的机器上创建私有配置文件。以下 macOS/Linux 命令创建模板，已有同名配置时拒绝覆盖：

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

**连接前，通过本机私有编辑器打开此文件，替换两项占位值。** 不要把 Token 粘贴到 Agent 对话或命令参数。已有私有配置时复用其路径即可；其他系统填写同样的两项配置，并设置等效私有权限。

URL 是服务根地址，不加 `/mcp` 或 `/a2a`，且必须从 Agent 的实际执行环境可达。远程 Agent 或容器中的 `127.0.0.1` 指向它自己，不是用户电脑。远程连接使用 HTTPS，本机回环调试可用 HTTP。管理员明确部署为开放模式时填 `MEDIA_SERVICE_TOKEN=null`，不要留空。

消费者不需要服务端模型 Key 或部署 JSON；不要复制服务端整份 `.env`，不要打印消费者配置或 `source` 此文件。进程中同名环境变量优先于文件，连接前应核对是否存在其他环境配置的覆盖。状态文件也包含本地路径、文件引用和任务标识，需私有保存且不提交 Git。

完成安装和配置后，在同一终端继续设置：

```sh
skill_dir="$skills_root/ffmpeg-agent-media"
consumer_run=$(mktemp -d "$consumer_private/run.XXXXXX")
```

如果复用已有安装或打开了新终端，先将 `skill_dir`、`consumer_env` 和 `consumer_run` 设为对应绝对路径。新任务创建新的私有工作目录，下文的各个输出子目录必须不存在。也可以把 `skill_dir` 指向 `ffmpeg-agent-connect`，客户端行为相同。命令只传配置文件路径，不传 Token；完整选项见 `python3 "$skill_dir/scripts/media_client.py" --help`。

## 首次连接

可对支持该技能的 Agent 说：

> 使用 ffmpeg-agent-connect，连接我私有配置文件中的媒体服务，检查当前可用协议，再用我选定的小文件验证完整处理流程。

对应的基础检查为：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" check --protocol all
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" capabilities
```

`check` 返回 JSON，区分 HTTP、MCP 与 A2A 检查；使用 `all` 时，A2A 未开启报告 `enabled: false`，不把它当成整个服务失败。可用 `--protocol http`、`mcp` 或 `a2a` 单独检查。连接检查不上传媒体，也不代表媒体处理已验证；用下文的处理命令补齐端到端验证。

| 宿主条件 | 建议接入 |
| --- | --- |
| 已连接远程 MCP | 优先调用宿主原生 MCP，附带客户端负责本地文件上传、恢复和下载 |
| 支持远程 MCP，尚未配置 | 使用服务根地址 + `/mcp`，传输为 Streamable HTTP；按宿主正式配置方式设置认证 |
| 支持 A2A，需委托自然语言任务 | 读取 `/.well-known/agent-card.json`；当前服务提供 A2A 1.0 `/a2a` |
| 可执行 Python，无原生协议能力 | 使用附带客户端，显式选择 HTTP、MCP 或 A2A 传输 |

Token 模式下，各入口和文件传输使用相同的 Bearer 认证；开放模式不发送认证头。技能不会猜测宿主配置路径、覆盖其他服务器条目或开启服务端功能。MCP 没有 stdio 入口，A2A 0.3 宿主的兼容性需要另行验证。辅助客户端的协议检查成功，也不能替代宿主原生连接的验证。

结构化操作不依赖服务端模型。自然语言规划和 A2A 是可选功能，以实时能力为准。选择编码器、格式和参数时，读取能力结果及 MCP `tools/list`，不要把文档中的可选值当成每个部署都支持。

## 明确需求：结构化 HTTP 或 MCP

默认视频转换保留源尺寸和时间节奏；下面的命令上传本机文件，等待媒体任务完成并下载校验结果：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --operation video.transcode.v1 --transport http \
  --state "$consumer_run/http-run.json" \
  --output-dir "$consumer_run/http-results"
```

同一操作也能显式通过远程 MCP 提交，使用新的状态和输出目录：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --operation video.transcode.v1 --transport mcp \
  --state "$consumer_run/mcp-run.json" \
  --output-dir "$consumer_run/mcp-results"
```

客户端使用六个版本化操作：`media.probe.v1`、`video.transcode.v1`、`audio.transcode.v1`、`video.extract_audio.v1`、`video.extract_frames.v1`、`image.resize.v1`。省略 `--params` 使用默认行为；需要修改时直接传内联 JSON 参数对象，只写必要覆盖项。例如 `--params '{"resize":{"width":1280}}'` 限制视频宽度，而不另外强制帧率或音频参数。此消费者 CLI 的 `--params` 不接受文件路径。完整参数说明见[媒体参数参考](parameters.zh-CN.md)，可用范围仍以当前服务为准。

多步任务使用 `--definition-file` 代替 `--operation`，文件包含 `steps`、`outputs` 与可选 `output_policy`，`inputs` 由上传补齐。可重复 `--input "name=/absolute/path"` 提供最多四个具名输入。示例结构见技能的[工作流参考](../../skills/ffmpeg-agent-media/references/workflow.md#明确操作http-或-mcp)。

## 复用宿主原生 MCP

远程 MCP 不能直接读取你电脑上的路径。先通过 HTTP 上传取得文件引用，再交给已连接的 MCP 工具：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" upload \
  --input "source=/absolute/path/to/input.mp4" \
  --state "$consumer_run/native-mcp-run.json"
```

上传结果提供输入引用和稳定 `request_id`。宿主通过 `tools/list` 发现工具，用该文件引用构造请求，并使用 `request_id` 作为 `idempotency_key`。不要向 MCP 传本机路径或 base64 文件字节。返回 `job_id` 后接手等待和下载：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" collect \
  --state "$consumer_run/native-mcp-run.json" \
  --job-id "<returned-job-id>" \
  --output-dir "$consumer_run/native-mcp-results"
```

自然语言工具返回 `agent_task_id` 时，改用 `--agent-task-id "<returned-agent-task-id>"`。`collect` 不会重新上传或提交。只绑定本次请求返回的任务；提交响应不确定时，以相同请求和幂等键恢复确认，不用新键重复执行。

## 自然语言与 A2A

需要服务端规划时，把用户意图保存为 UTF-8 `intent.txt`，例如：

```text
将 source 转为宽度不超过 1280 的 H.264 MP4，并提取 MP3 音轨；分别返回两个输出。
```

服务已开启规划和 A2A 后：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --intent-file "$consumer_run/intent.txt" --transport a2a \
  --state "$consumer_run/a2a-run.json" \
  --output-dir "$consumer_run/a2a-results"
```

自然语言也可显式选择 `--transport http` 或 `mcp`。A2A 只接受意图模式，不能配 `--operation` 或 `--definition-file`。模型 Key 留在服务端；客户端不会开启服务端开关，也不会在失败后静默切换协议或重复调用模型。

规划完成和 A2A 返回 Task 都不代表媒体处理完成。客户端持续查询关联媒体任务，再下载并校验产物。`needs_input` 需要补充构造受支持任务所必需的信息；`unsupported` 需要调整到支持范围，修改后使用新状态文件提交。`partial` 会保留可用结果，但应报告失败的输出或集合成员，不能当作全部成功。

## 等待、恢复、取消与释放

每次新任务用新的状态文件；状态持久保存稳定请求标识和任务引用。`run`、`collect`、`resume` 和 `cancel` 可用 `--wait-seconds` 限制本次等待。超时不取消后台任务，也不等于处理失败：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" status \
  --state "$consumer_run/http-run.json"
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" resume \
  --state "$consumer_run/http-run.json" --wait-seconds 600
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" cancel \
  --state "$consumer_run/http-run.json" --wait-seconds 600
```

网络中断或提交结果不确定时，保留原状态并恢复；CLI 提交使用 `resume`，尚未绑定到状态的原生 MCP 提交用相同请求和幂等键恢复确认后再 `collect`。不要重新上传、改幂等键或直接创建新请求。请求内容改变时才使用新状态。不要手动修改状态文件中的请求、ID 或引用，也不要并发操作同一个状态文件。

取消请求被接受不等于工作已停止，需要等实际终态和 `inputs_released=true`；与处理完成同时发生时，以返回的实际终态为准。成功或部分成功的收集流程会在输入不再使用后自动 release 本次托管上传。失败、取消或未提交上传需要显式清理时：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" release \
  --state "$consumer_run/http-run.json"
```

活动任务期间客户端拒绝 release。释放只作用于该状态记录的本次上传，不删除用户本地源文件，也不释放已发布结果产物。产物仍受服务保留期限约束，应及时下载。

下载只写新目的位置，按大小与 SHA-256 校验，不覆盖已有文件。输出按任务声明的名称落盘，单操作默认是 `result`，当前不自动追加扩展名；真实格式以产物元数据为准，实际路径见返回 JSON 的 `downloads`。集合输出为同名目录，内有成员和 `manifest.json`。

命令输出机器可读 JSON，调用方还可以按退出码区分结果：

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功；媒体或取消结果仍以 JSON 中的实际状态为准 |
| `1` | 请求/处理失败，或 `needs_input`、`unsupported` 等需处理结果 |
| `2` | 媒体部分成功或部分下载未完成，检查 `downloads` 和输出状态 |
| `3` | 本次等待超时，用同一状态恢复 |
| `130` | 客户端被中断，保留原状态恢复 |

对外报告实际媒体状态和已校验文件，不公开配置、状态全文、认证头或敏感路径。

## 项目维护者：共享客户端

客户端主逻辑的权威源码位于 [`consumer/ffmpeg_agent_client/`](../../consumer/ffmpeg_agent_client/)；其中 `envfile.py` 从服务共用的 [`src/media_service/envfile.py`](../../src/media_service/envfile.py) 生成。两个技能内的入口和包也是分发副本，不在生成副本上直接修复。修改权威源码后运行：

```sh
python3 scripts/sync-consumer-skills.py
python3 scripts/sync-consumer-skills.py --check
```

同步生成两个技能的 `scripts/media_client.py` 与 `scripts/ffmpeg_agent_client/`，并校验分发内容。这样每个技能仍可独立安装，共享逻辑只维护一份。
