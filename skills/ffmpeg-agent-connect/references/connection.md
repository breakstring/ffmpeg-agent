# 消费者连接与排查

## 私有配置

使用本机私有目录保存 `.env`，只包含以下两项。占位符由用户通过本机编辑器替换；不要要求用户在对话或命令参数里提供 Token。

```dotenv
MEDIA_PUBLIC_BASE_URL=https://media.example.com
MEDIA_SERVICE_TOKEN=<service-token-from-administrator>
```

管理员明确使用开放模式时写 `MEDIA_SERVICE_TOKEN=null`，不能用空值代替。不要复制服务端整份 `.env`，消费者不需要 `MEDIA_LLM_API_KEY`。配置文件不加入 Git，不通过 `source` 执行，不在日志中打印。Unix 上目录设为 `0700`、文件设为 `0600`；其他系统使用等效的用户私有权限。

URL 是消费者可达的服务根地址，不包含 `/mcp` 或 `/a2a` 后缀，也不包含用户名、密码、查询凭据或片段。远程连接使用 HTTPS；本机回环调试可使用 HTTP。进程中的同名环境变量优先于配置文件；若连接目标不符，核对覆盖来源但不要输出凭据。

## 检查入口

下面的 `skill_dir` 指已安装技能的绝对路径，`consumer_env` 指私有配置文件的绝对路径。这两个变量不含凭据：

```sh
skill_dir="/absolute/path/to/ffmpeg-agent-connect"
consumer_env="/absolute/path/to/private/consumer.env"
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" check --protocol all
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" capabilities
```

所有命令返回 JSON。需要独立排查某个入口时选择 `check --protocol http`、`mcp` 或 `a2a`。`all` 根据实际功能开关检查；A2A 关闭时报告 `enabled: false`，不把它当成整个服务失败。

区分四层证据：

1. 健康检查可达且服务就绪。
2. 使用所选认证成功读取业务能力，不能仅以健康端点返回成功推断 Token 有效。
3. MCP 初始化/工具发现或 A2A Agent Card/协议检查成功。
4. 本地文件上传、媒体终态、产物下载及校验成功。

不要把前三层替代第四层，也不要把辅助客户端的 MCP 成功替代某个宿主 Agent 的原生 MCP 配置成功。

## 原生宿主配置

先读取宿主支持的远程协议与安全凭据机制，再按其正式 schema 配置。以下是连接要素，不是任何特定宿主的配置文件：

| 要素 | 值 |
| --- | --- |
| MCP 传输 | Streamable HTTP |
| MCP URL | 服务根地址 + `/mcp` |
| A2A 能力卡 | 服务根地址 + `/.well-known/agent-card.json` |
| A2A 入口/版本 | 以能力卡为准；本服务为 `/a2a`、1.0 |
| 认证 | Token 模式为 `Authorization: Bearer <token>`；开放模式不发送认证头 |
| 文件传输 | 同一根地址的 HTTP API，使用相同认证 |

本服务没有 stdio 桥接，不要求在消费者机运行 Docker 或 FFmpeg。即使原生 MCP 已连接，本机文件仍需 HTTP 上传；原生 MCP 不会让服务器直接读取消费者本地路径。

新增配置只修改明确属于本服务的条目，保留其他连接。宿主不能安全存储或引用 Token 时，先使用私有配置文件配合辅助客户端，不把 Token 写进示例、聊天或可公开文件。配置后的验证应使用宿主真实连接执行能力发现。

## 最小端到端验证

使用用户允许上传的小型本地媒体文件。`consumer_run` 是已经创建的私有工作目录；`probe-results` 必须不存在：

```sh
consumer_run="/absolute/path/to/private/connection-test"
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" run \
  --input "source=/absolute/path/to/sample.mp4" \
  --operation media.probe.v1 --transport http \
  --state "$consumer_run/probe-run.json" \
  --output-dir "$consumer_run/probe-results"
```

检查最终媒体状态和下载校验结果。状态文件保存恢复所需的请求、文件与任务标识，应和配置一样私有保存。等待超时只表示本次等待结束，使用同一个状态文件恢复：

```sh
python3 "$skill_dir/scripts/media_client.py" --env-file "$consumer_env" resume \
  --state "$consumer_run/probe-run.json"
```

若用户要验证 MCP，可在另一个新状态文件和输出目录执行同一操作，并显式选择 `--transport mcp`。A2A 只接受自然语言意图；使用 `--intent-file` 取代 `--operation`，需要服务端已启用规划和 A2A。不要为完成连接检查额外开启服务端模型或改写部署配置。

## 常见问题

| 现象 | 下一步 |
| --- | --- |
| 健康成功，业务请求返回 401 | 核对私有文件和宿主认证是否指向同一服务；不要输出 Token |
| HTTP 成功，MCP 连接被拒绝 | 核对 `/mcp`、Streamable HTTP、代理转发及管理员配置的公开 Host/Origin |
| A2A 显示未启用 | 使用结构化 HTTP/MCP；若任务确实需要 A2A，由管理员决定是否启用 |
| 工具没有自然语言入口 | 以实时能力为准，使用结构化操作或向管理员说明所需能力 |
| 上传成功后提交失败 | 保留状态并查询；不得以换协议或新幂等键掩盖不确定的提交结果 |
| 处理完成但本机没有结果 | 核对任务状态、输出元数据及下载校验，恢复原状态文件的收集流程 |

只报告稳定错误码和必要诊断，不转发可能包含敏感路径或凭据的原始异常全文。
