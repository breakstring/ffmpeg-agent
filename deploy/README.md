# 容器运行说明

使用者入门：[English](../README.md) · [简体中文](../README.zh-CN.md)。根目录 `docker-compose.yaml` 复用本目录服务定义；下文保留共享卷、跨 Compose 与验收的进阶说明。

运行基线及已验证范围见 [P0 基线](../docs/verification/runtime-baseline.md)。下面的 Compose 固定一个服务容器和一个 Python API 进程；`worker_concurrency` 是容器内部的逻辑任务槽数，不是 Uvicorn 进程数。

## 准备和运行

在仓库根目录执行：

```sh
cp .env.example .env
python3 deploy/generate-config.py
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8080/health/live
curl --fail http://127.0.0.1:8080/health/ready
```

生成器拒绝覆盖已有文件，随机 token 只进入 `deploy/local/media-config.json`，不打印到终端。该目录必须为 `0700`；目录内文件 `0644` 便于绑定到容器后由 UID/GID `10001:10001` 读取，宿主机其他用户无法穿过私有目录。请保持配置目录权限，不把配置加入 Git、构建上下文或日志。可以直接编辑该文件设置调用方、存储和来源规则；不要通过命令行参数或环境变量传 token。

已有 `.env` 和私有 JSON 时保留它们，跳过复制和生成。`.env.example` 仅配置 Compose 项目名、镜像、端口、配置文件位置和容器资源，根目录 `MEDIA_CONFIG_PATH` 相对仓库解析。单独使用旧 `-f deploy/compose.yaml` 时其相对配置路径仍按 deploy 目录解析，外部配置使用绝对路径。

服务以非 root 运行，根文件系统只读；`state`、`work`、`shared_input` 和 `shared_output` 是四个独立卷。SQLite 只在 `state` 卷，业务容器不挂载 state/work。服务对共享输入只读，对共享输出可写；业务对输出只读。所有区域不能嵌套。外部 bind mount 需要预先满足 UID/GID 的读写权限，不能启动后自动递归修改既有业务目录所有权。

默认仅向宿主机 `127.0.0.1:8080` 映射端口。跨机部署通过实际受控 HTTPS 入口提供服务，把 `public_base_url` 改为远端调用方可达地址；监听映射由 `MEDIA_BIND_ADDRESS` 和 `MEDIA_PORT` 配置。实际源服务的 hostname、端口、scheme 和允许目标网段加入对应调用方的 `source_rules`；空列表表示默认禁止 HTTP 来源。不可用全网段通配代替部署时的明确目标。

默认 1 个 Worker、2 CPU、2 GiB 内存、128 PID、64 MiB `/tmp`、60 秒容器停止宽限期。配置中的 `max_work_bytes`、存储 `quota_bytes` 和磁盘余量控制持久工作/输出区域；Compose 的内存限制不构成磁盘配额。提高 Worker 数量之前先完成目标宿主机实测。构建参数 `BUILD_REVISION` 记录交付版本，不修改冻结操作或预设含义。

## 上传与远程 MCP

新生成配置使用一个 `service_token`，HTTP、上传、产物下载与 `/mcp` 共用 Bearer 校验。显式设为 `null` 开放服务；已有配置缺少这个字段时继续使用原 `callers.tokens`，升级不会意外关闭认证。配置内保留的 `service_caller` 仅指定现有任务和存储所用的服务空间，不增加用户系统。

独立上传使用 `POST /v1/files`（原始二进制、`application/octet-stream`）；请求内上传使用 `POST /v1/jobs/upload`（multipart，`job` JSON 在先）。托管输入放在私有 work 卷的 `_uploads`，不额外要求业务共享卷，也不写只读输入卷。默认总上传预算 4 GiB、同时接收 2 个、总时限 300 秒、闲置 30 秒，且受整个 work 卷预算限制；实际配置和能力查询为准。上传完成和任务接受是不同检查点，失败请求中的完整文件按有限 TTL 保留供重试，半文件关闭后定向回收。

远程 MCP 使用 `public_base_url` 加 `/mcp`，同进程提供 Streamable HTTP。入口按该公开地址检查 Host 和 Origin；反向代理应保留正确的公开 Host。客户端先用 HTTP 上传取得 `file_id`，再通过 MCP 处理和查询，最后带同一 Token 从 HTTP 下载产物。没有 stdio 桥接或远端客户端路径读取。调用示例见[README](../README.zh-CN.md)。

升级会在单实例锁内把 SQLite schema 1 事务迁移到 2，保留原有任务与产物，新增上传登记和引用表。新版本写入 schema 2 后，旧镜像不能直接读取该数据库；回退应恢复升级前一致性备份。备份现有 state/work/output 已包含托管上传文件。

## 同 Compose

提供实际业务镜像并合并示例：

```sh
BUSINESS_IMAGE=your-internal-business-image \
  docker compose -f deploy/compose.yaml -f deploy/compose.same-project.yaml config --quiet
BUSINESS_IMAGE=your-internal-business-image \
  docker compose -f deploy/compose.yaml -f deploy/compose.same-project.yaml up -d
```

业务在 `/app/media-to-process` 写临时文件，关闭后同文件系统重命名，再提交 `storage_id=input` 与相对 key。媒体服务在 `/media/input` 读同一卷；两个挂载路径可以不同。业务在申请有效产物租约后，从 `/app/media-results` 读取产物，结束释放租约。输入必须保留到任务返回 `inputs_released=true`。

## 跨 Compose、同一 Docker daemon

部署方先明确外部卷和网络的实际名称；以下命令只适合创建新的专用资源，不覆盖或清空任何已有数据：

```sh
docker volume create ffmpeg-agent-shared-input
docker volume create ffmpeg-agent-shared-output
docker network create ffmpeg-agent-shared-network
docker compose -f deploy/compose.yaml -f deploy/compose.external.yaml up -d media
BUSINESS_IMAGE=your-internal-business-image \
  docker compose -f deploy/compose.consumer.yaml up -d
```

媒体服务先初始化其新卷目录的权限，再启动业务。已有卷应预先核对权限和所有权；不应递归 chown 不明数据。两侧用相同的 `MEDIA_INPUT_VOLUME`、`MEDIA_OUTPUT_VOLUME` 和 `MEDIA_SHARED_NETWORK` 值。状态/工作卷仍由媒体 Compose 私有管理。不同 daemon、Docker rootless 实例或不同宿主机不因卷名相同而共享数据。

## 两台真实宿主机 HTTP 验收

需要一台实际远端业务宿主机、一台媒体宿主机、媒体节点可访问的受控 HTTP(S) 源、调用方可访问的服务 HTTPS 地址，以及专用测试凭据。只使用本仓库 fixture，不使用业务录音或文档。

1. 在业务宿主机将 fixture 通过已有受控文件服务提供 GET URL，记录文件 SHA-256；配置媒体调用方的精确来源规则。
2. 从业务宿主机提交 `http_file` 输入的 probe 或转码任务，轮询到终态，断言 `inputs_released=true`。
3. 从同一远端宿主机读取产物元数据，下载内容，核对响应完整性摘要；用真实 ffprobe 校验转码结果。
4. 覆盖 HEAD、单 Range 和携带有效租约的读取；记录两边宿主机身份、网络方向、镜像版本、任务/产物 ID 及脱敏断言。
5. 来源 URL、认证头、token 和配置完整内容不进入验收报告。临时源和产物只清理本用例拥有的数据。

同机两个容器只验证 Docker 网络路径，不计为本节通过。当前可用环境缺少第二台实际宿主机，此项尚待执行。

仓库提供可执行的跨机检查脚本，依赖 Python 3.14 与业务宿主机上的 ffprobe。先在**媒体实际宿主机**使用本机 Docker socket 采集身份和目标镜像证据，再将脱敏 JSON 复制到业务宿主机：

```sh
mkdir -m 700 -p deploy/local
python3 deploy/verify-http-remote.py host --image ffmpeg-agent:v1-local \
  --report deploy/local/media-host.json
```

两侧都使用本仓库的 `Feline_Apocalypse.mp4`；业务侧受控文件服务必须让媒体节点能够 GET 到同一字节内容。媒体配置的 `source_rules` 为真实来源指定精确 `host`、`ports`、`schemes` 和允许的 `networks`（私网目标需显式列出精确目标网段）。更新配置后重启媒体容器，并确认来源响应的 Content-Length 与仓库素材一致。

在**业务实际宿主机**准备私有配置，不在命令行传 token 或签名 URL：

```sh
mkdir -m 700 -p deploy/local
cp deploy/remote-check.example.json deploy/local/remote-check.json
chmod 600 deploy/local/remote-check.json
```

将媒体宿主机采集的 `media-host.json` 复制到业务宿主机的 `deploy/local/media-host.json`。编辑私有配置副本中的服务 HTTPS 地址、专用调用 token、来源 URL/受控头和 fixture 路径。部署方确认两台实际宿主机不同后，将 `distinct_physical_hosts_confirmed` 设为 `true`；该确认和主机指纹共同作为验收依据，脚本不能独立证明物理拓扑。不要在容器内或两个 VM 身份下冒充两个实际宿主机。随后执行：

```sh
python3 deploy/verify-http-remote.py run \
  --config deploy/local/remote-check.json \
  --output deploy/local/remote-video.mp4 \
  --report deploy/local/two-host-http.json
```

脚本执行 HTTP 输入转码、同键重放、等待输入释放、申请租约、HEAD、单 Range/If-Range、完整下载校验与真实 ffprobe 校验；成功后释放租约并提前 release 本次自有产物。已存在的下载文件不会被覆盖。报告只有脱敏主机指纹、镜像/引擎、任务/产物 ID、摘要和断言，不含 token、源 URL 或本地路径。`--local-smoke` 的报告固定标为 `single_host_http_smoke`，不能计为本节通过。

## 本地验收与容量实测

在可用 Docker daemon 上执行。脚本创建带随机名称和所有权标签的专用容器、卷及网络，完成后只清理本次创建的资源。仓库 fixture 只读挂载，再复制到专用输入卷；不挂载既有业务目录。每次生成的测试 token 仅存在临时配置中。

```sh
docker build --build-arg BUILD_REVISION=local-verification -t ffmpeg-agent:v1-local .
.venv/bin/python deploy/verify-tests.py --image ffmpeg-agent:v1-local \
  --report deploy/tests-arm64.json --log /tmp/ffmpeg-agent-tests.log
.venv/bin/python deploy/verify-container.py --image ffmpeg-agent:v1-local \
  --report deploy/container-p3-arm64.json
.venv/bin/python deploy/verify-deployments.py --image ffmpeg-agent:v1-local \
  --report deploy/deployments-arm64.json
.venv/bin/python deploy/benchmark.py --image ffmpeg-agent:v1-local \
  --workers 1 2 4 --jobs 8 --report deploy/capacity-arm64.json
```

完整测试脚本先解析不可变镜像 ID，在非 root、只读根文件系统的容器中运行；锁定的开发依赖只安装到临时 tmpfs。该测试 tmpfs 允许加载 Python 动态扩展，服务部署的 `/tmp` 配置不受影响。传入 amd64 镜像时应使用不同报告文件名，且需区分模拟和原生执行。

上传/MCP 增量验收使用独立报告，保留原 P0～P7 证据。以实际精确镜像 ID 替换下例参数：

```sh
.venv/bin/python deploy/verify-upload-mcp.py --image <精确镜像ID> \
  --report deploy/upload-mcp-container-arm64.json
```

脚本使用两组独立容器和私有卷，分别验证 Token 开/关、原始上传、真实 TCP multipart、远程 MCP、HEAD/Range/SHA 与 ffprobe，并在观察到登记半成品后强停整个测试容器验证恢复；不挂载业务共享输入，只清理本次所有权标签资源。当前结果见[增量进度](../docs/plans/v1-implementation-progress.md)。该单机远程协议验收不计为两台宿主机证据。

2026-10-13 的原生 Linux arm64 实测如下。每组用 `Feline_Apocalypse.mp4` 执行 8 次 `video.transcode.v1` / `web_720p_v1`，限制为 2 CPU / 2 GiB / 128 PID。完整数据及镜像 ID 在 [`capacity-arm64.json`](capacity-arm64.json)。

| Worker | 吞吐 jobs/min | 排队平均 / 最大 s | GET job p95 ms | 取消并确认停止 s | 进程 RSS 峰值 MiB | 容器内存峰值 MiB | 平均 CPU | work 分配峰值 MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 19.54 | 10.68 / 21.30 | 4.15 | 0.85 | 273.64 | 447.42 | 185.36% | 5.24 |
| 2 | 17.30 | 10.49 / 20.91 | 49.79 | 0.82 | 467.57 | 397.06 | 197.32% | 6.98 |
| 4 | 11.66 | 10.28 / 20.59 | 84.34 | 0.83 | 876.46 | 726.99 | 198.46% | 8.00 |

该 fixture 和 CPU 配额下，增加并发降低吞吐并增加 API 延迟，默认保持 1 个 Worker。该建议只覆盖本次短视频转码负载；调整机器规格、输入规模或操作组合后需要重新测量。

排队时长来自持久化的任务创建时间到首次 attempt 开始时间；API 延迟只统计实际轮询的 GET job，包含主机到容器的往返。每组吞吐结束后另起一个任务，观察到真实 FFmpeg 进程后取消，等待 `canceled`、`inputs_released=true` 及 FFmpeg/ffprobe 均消失，报告该全程耗时。

资源约每 0.5 秒采样；RSS 为各进程 `VmRSS` 之和，会重复计算共享页，与 cgroup 内存口径不同。容器内存使用 cgroup `memory.peak`，CPU 为 cgroup 累计用量差值，以一个 CPU 为 100%；短采样区间可能略超 200%。work 磁盘是服务工作卷内文件的实际分配量，不包含输入、产物和 SQLite。采样会带来少量开销，离散采样可能遗漏瞬时 RSS 和磁盘峰值；宿主机其他工作负载未被控制。本报告不是原生 amd64 容量或两台真实宿主机验收。

## 维护边界

禁止 `--workers`、reload、在同一容器热重启 API 或多实例挂载同一状态目录；修改并发配置后重启整个容器。升级先停止新任务并排空；保留旧镜像以处理冻结定义，不悄悄替换预设。同一 SQLite 文件不得放 NFS 或同时被其他服务访问。

备份先停止接收新任务并正常停止容器，保留配套 state/work/output 数据及版本清单；不能只复制数据库后宣称产物可恢复。不要用 `docker compose down -v` 作为媒体清理手段。正常任务与产物回收通过 API 和服务自身的定向回收完成。
