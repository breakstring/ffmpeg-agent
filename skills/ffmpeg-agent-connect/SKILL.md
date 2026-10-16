---
name: ffmpeg-agent-connect
description: 连接已部署的 ffmpeg-agent 媒体服务，配置消费者端地址与认证，验证 HTTP、远程 MCP 或 A2A 接入；用于首次连接、更换服务或排查连接问题。
---

# 连接 ffmpeg-agent

帮助用户的 Agent 接入已有服务，交付经过验证的连接方式和可用能力。这个技能只配置消费者端；服务端部署、模型 Key 和功能开关由服务管理员管理。

## 确认连接条件

- 找到用户选定的服务地址、宿主 Agent 和私有配置文件。消费者只需要 `MEDIA_PUBLIC_BASE_URL` 与 `MEDIA_SERVICE_TOKEN`。让用户在本机私有文件或宿主安全凭据设置中填写 Token，不要求其粘贴到对话中。
- 先检查宿主是否已经连接这个远程 MCP 服务；已有连接优先复用。只有需要新增或修复连接时才检查宿主支持的配置方式。未知宿主的路径、命令和配置 schema 必须查证，不能猜测，也不能覆盖其他服务条目。
- 本目录的 `scripts/media_client.py` 是 Python 3.10+ 标准库客户端。无需 FFmpeg、Docker、服务端源码或服务端模型凭据。具体配置和排查步骤见 [references/connection.md](references/connection.md)。

## 验证并选择入口

先运行连接检查和能力查询；命令中只传配置文件路径：

```sh
python3 "<本技能目录>/scripts/media_client.py" --env-file "<私有配置文件>" check --protocol all
python3 "<本技能目录>/scripts/media_client.py" --env-file "<私有配置文件>" capabilities
```

`check` 验证协议接入，不能单独证明文件上传、媒体处理和下载都正常。A2A 未开启时，应报告未启用，不能替用户开启服务端开关。根据实际宿主能力选择：

| 宿主能力 | 接入方式 |
| --- | --- |
| 已连接远程 MCP | 使用原生 MCP 的 `tools/list` 和 `list_capabilities`；本地文件通过 HTTP 客户端上传下载 |
| 支持远程 MCP，但尚未配置 | 配置服务地址加 `/mcp` 的 Streamable HTTP 入口及相同的 Bearer 认证；没有 stdio 入口 |
| 支持 A2A，需委托自然语言任务 | 读取认证后的 `/.well-known/agent-card.json`，按实际 Agent Card 接入 A2A 1.0 `/a2a` |
| 能运行 Python，无原生协议集成 | 使用随技能提供的客户端，通过 HTTP 完成处理；也可显式选择其 MCP/A2A 传输 |

自然语言规划和 A2A 是可选能力。结构化 HTTP/MCP 操作不依赖服务端模型；不要因模型关闭而判定整个服务不可用，也不要在请求失败后静默更换协议或反复调用模型。

## 完成接入

用户已要求端到端接入验证且提供可用本地素材时，用媒体探测等轻量操作验证“上传 → 接受 → 实际媒体终态 → 下载校验”。选用本次独立状态文件和新输出目录，参照 [references/connection.md](references/connection.md#最小端到端验证)。本地路径不是服务器输入引用，先上传取得 `file_id`；不要把消费者路径交给远程服务尝试读取。

交付时说明实际验证过的协议、是否启用自然语言/A2A、结果保存位置，以及尚未验证的宿主集成或文件传输环节。只展示脱敏结果；不打印配置文件、认证头或包含凭据的 URL。

日常媒体处理可使用已安装的 `ffmpeg-agent-media`；本技能自身也附带完整客户端，连接验证不依赖安装另一个技能。
