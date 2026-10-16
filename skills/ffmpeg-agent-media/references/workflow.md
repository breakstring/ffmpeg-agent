# 媒体工作流与恢复

## 配置与调用

安装时保留完整技能目录；`scripts/media_client.py` 和相邻 `ffmpeg_agent_client` 包必须一起复制。客户端只需要 Python 3.10+，没有额外 Python 包依赖。

消费者私有 `.env` 只包含 `MEDIA_PUBLIC_BASE_URL` 与 `MEDIA_SERVICE_TOKEN`，例如：

```dotenv
MEDIA_PUBLIC_BASE_URL=https://media.example.com
MEDIA_SERVICE_TOKEN=<service-token-from-administrator>
```

由用户在本机私有编辑器替换占位符，不在对话中索取 Token。开放服务使用明确的 `null`，不能留空。URL 是服务根地址，远程连接使用 HTTPS。本技能不需要服务端模型 Key，不读取部署 JSON。同名进程环境变量优先于配置文件，使用前核对目标。不要 `source` 配置文件；配置和状态文件使用用户私有权限，不加入 Git。

以下命令中的目录和文件名都是示例。将它们替换为用户选择的绝对路径；先创建私有工作目录，输出子目录应不存在：

```sh
skill_dir="/absolute/path/to/ffmpeg-agent-media"
consumer_env="/absolute/path/to/private/consumer.env"
consumer_run="/absolute/path/to/private/media-run"
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" capabilities
```

所有命令返回 JSON。详细选项用 `--help` 或 `<command> --help` 查询；不要在报告中展示状态文件全文或认证材料。

## 明确操作：HTTP 或 MCP

把本地视频转换为默认 MP4；省略参数时优先保留源尺寸和时间节奏：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --operation video.transcode.v1 --transport http \
  --state "$consumer_run/video-run.json" \
  --output-dir "$consumer_run/video-results"
```

消费者 CLI 使用版本化操作标识：`media.probe.v1`、`video.transcode.v1`、`audio.transcode.v1`、`video.extract_audio.v1`、`video.extract_frames.v1`、`image.resize.v1`。`--params` 接收内联 JSON 参数对象，例如 `--params '{"resize":{"width":1280}}'`；只填写需要覆盖的设置，不传参数文件路径。MCP 传输把 `--transport http` 改为 `mcp`，并使用新的状态文件与输出目录进行新任务。

多输入可重复 `--input "名称=绝对路径"`，最多四项。多步任务使用 `--definition-file definition.json`，内容为 `steps`、`outputs` 和可选 `output_policy`；输入由上传自动填入，不在定义中填本机路径。步骤的输入名必须与 `--input` 名称一致。定义示例：

```json
{
  "steps": [{
    "id": "convert",
    "operation": "video.transcode.v1",
    "inputs": {"video": {"source": "source"}}
  }],
  "outputs": {"video": {"step": "convert", "output": "video"}}
}
```

## 原生 MCP 加 HTTP 文件传输

宿主已有原生 MCP 连接时优先使用它，不为了文件传输重配宿主：

1. 运行 `upload`；读取返回的输入引用和 `request_id`。
2. 通过原生 MCP 发现工具 schema，然后提交结构化任务或 `generate_media`。使用上传结果中的引用；把 `request_id` 作为 `idempotency_key`。保存返回的任务 ID。
3. 用 `collect` 绑定任务并等待、下载。任务 ID 与本次上传必须属于同一请求。若提交结果不确定，用相同键和相同请求恢复确认，不能创建新键重发。

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" upload \
  --input "source=/absolute/path/to/input.mp4" \
  --state "$consumer_run/native-mcp-run.json"

# 在宿主的原生 MCP 中提交后，替换返回的任务 ID。
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" collect \
  --state "$consumer_run/native-mcp-run.json" --job-id "<returned-job-id>" \
  --output-dir "$consumer_run/native-mcp-results"
```

自然语言工具返回规划任务时，使用 `--agent-task-id "<returned-agent-task-id>"` 代替 `--job-id`。`collect` 不重新上传或提交任务。MCP/A2A 中不传 base64 文件字节。

## 自然语言：HTTP、MCP 或 A2A

先确认服务启用了自然语言能力；A2A 另需开启 A2A。将用户意图保存为 UTF-8 文本文件，明确使用上传输入的名字。例如 `intent.txt`：

```text
将 source 转为宽度不超过 1280 的 H.264 MP4，并提取 MP3 音轨；分别返回两个输出。
```

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/input.mp4" \
  --intent-file "$consumer_run/intent.txt" --transport a2a \
  --state "$consumer_run/a2a-run.json" \
  --output-dir "$consumer_run/a2a-results"
```

自然语言也支持 `--transport http` 或 `mcp`。A2A 只接受意图模式，不能与 `--operation` 或 `--definition-file` 搭配。服务使用 A2A 1.0；发现成功不证明旧版 0.3 宿主兼容。Task 的协议状态需结合底层媒体状态判断：部分成功仍应保留可用产物，拒绝可能对应 `needs_input` 或 `unsupported`。

不要在消费者配置模型 Key，不把不受支持的任意 FFmpeg 命令交给服务执行。

## 恢复、取消与清理

`run` 和 `collect` 的等待超时不会取消服务器任务。为当前操作指定 `--wait-seconds` 可以限制本次等待；下一次使用原状态恢复：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" status \
  --state "$consumer_run/video-run.json"
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" resume \
  --state "$consumer_run/video-run.json" --wait-seconds 600
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" cancel \
  --state "$consumer_run/video-run.json" --wait-seconds 600
```

每个状态文件绑定稳定请求标识及原始提交内容。网络不确定时保留它；任务内容改变时使用新的状态文件。不要编辑文件中的 ID、输入引用或请求体来重试，不要并发运行操作同一状态文件的客户端。

取消仍需等待实际终态和 `inputs_released=true`。只有确认本次托管输入已不再使用后才释放；活动任务期间客户端会拒绝 release：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" release \
  --state "$consumer_run/video-run.json"
```

成功或部分成功的收集流程会在 `inputs_released=true` 后自动 release 本次上传。Release 只作用于这个状态登记的服务托管文件，不删除本机源文件，也不释放结果产物。已发布产物仍受服务的保留期限约束，应及时下载。

结果写入新目录，下载按大小和 SHA-256 校验；已有文件不会被覆盖。输出按任务声明的名称落盘，单操作默认是 `result`，当前不自动追加扩展名；真实格式以产物元数据为准，实际路径见返回 JSON 的 `downloads`。集合对应同名目录，包含成员和 `manifest.json`，必须检查失败成员。`partial` 不是全部成功，失败也可能保留部分可用结果。只以实际任务终态和已校验文件报告交付。
