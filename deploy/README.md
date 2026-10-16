# 容器运行说明

使用者入门：[English](../README.md) · [简体中文](../README.zh-CN.md)。根目录 `docker-compose.yaml` 复用本目录服务定义；下文保留共享卷、跨 Compose 与验收的进阶说明。

运行基线及已验证范围见 [P0 基线](../docs/verification/runtime-baseline.md)。下面的 Compose 固定一个服务容器和一个 Python API 进程；`worker_concurrency` 是容器内部的逻辑任务槽数，不是 Uvicorn 进程数。

## 准备和运行

以下命令均在仓库根目录执行。

第一次取得项目时：

```sh
git clone https://github.com/breakstring/ffmpeg-agent.git
cd ffmpeg-agent
```

部署需要 Docker Engine/Desktop（含 Compose v2）和 Python 3.10+。Python 仅用于生成本机配置；FFmpeg、服务端 Python 3.14 和服务依赖都在镜像中。只连接别人已部署的服务时，无需 Docker，直接[安装消费者技能](../docs/usage/consumer-skills.zh-CN.md)。

### 首次初始化并启动

首次部署时，先准备配置：

```sh
python3 deploy/generate-config.py
```

脚本只生成或迁移 `.env` 和结构 JSON，不会启动容器。按需编辑 `.env` 后，再构建并启动服务：

```sh
docker compose up -d --build --wait
docker compose ps
curl --fail http://127.0.0.1:8080/health/live
curl --fail http://127.0.0.1:8080/health/ready
```

也可以把构建和启动分开，便于先检查镜像构建结果：

```sh
docker compose build media
docker compose up -d --no-build --wait media
```

### 使用 GHCR 预构建镜像

已发布的 `ghcr.io/breakstring/ffmpeg-agent` 支持匿名拉取，包含 `linux/amd64` 和 `linux/arm64`，Docker 按宿主架构选择镜像。2026-10-16 的[首次成功发布](https://github.com/breakstring/ffmpeg-agent/actions/runs/38847641532)已完成双架构测试；验证摘要和精确 digest 见[发布验收报告](../docs/verification/ghcr-release-validation.json)，后续版本见 [Actions](https://github.com/breakstring/ffmpeg-agent/actions/workflows/container.yml) 和 [Packages](https://github.com/breakstring/ffmpeg-agent/pkgs/container/ffmpeg-agent)。

先运行上述配置生成器，再编辑 `.env` 中已有的 `MEDIA_IMAGE` 行：

```dotenv
MEDIA_IMAGE=ghcr.io/breakstring/ffmpeg-agent:latest
```

随后执行：

```sh
docker compose pull media
docker compose up -d --no-build --wait media
docker compose ps
curl --fail http://127.0.0.1:8080/health/ready
```

`--no-build` 确保运行拉取的镜像；不要给这条路径加 `--build`。无需在本机安装 FFmpeg、uv 或模型 SDK。若需要返回本地源码构建，将 `MEDIA_IMAGE` 改回 `ffmpeg-agent:v1-local`，再运行 `docker compose build media`。

| 标签 | 含义 |
| --- | --- |
| `latest`、`main` | 最近成功发布的 main 构建，会随 main 更新移动 |
| `sha-<完整40位提交SHA>` | 对应提交的多架构镜像，用于定位源码版本 |
| `X.Y.Z` | 推送匹配 `pyproject.toml` 版本的稳定 `vX.Y.Z` Git 标签后发布；不会自动创建 Git 标签 |
| `@sha256:<digest>` | 固定精确发布清单；需要可复现升级与回退时优先使用 |

例如，把 `MEDIA_IMAGE` 设置为从发布摘要读到的 `ghcr.io/breakstring/ffmpeg-agent@sha256:<digest>`，即可固定镜像。`sha-…` 和版本号仍是 Registry 标签，只有 digest 直接绑定不可变内容。

### 更新、停止与查看状态

升级前停止新提交并等待正在处理的任务完成，按下文备份一致的状态、文件与配置。GHCR 部署更新时执行 `docker compose pull media`，再执行 `docker compose up -d --no-build --wait media`；源码部署则重新 `docker compose build media` 后启动。固定 digest 的部署需要先把 `.env` 的 `MEDIA_IMAGE` 改为新 digest。

```sh
docker compose ps
docker compose logs --tail 100 media
docker compose stop media
```

`stop` 保留卷，下次 `docker compose up -d --no-build --wait media` 继续使用已有数据。不要用 `docker compose down -v` 作为日常停止方式。

### 日常启动

配置已初始化且镜像已构建时，直接运行以下命令，无需再次执行 `generate-config.py`：

```sh
docker compose up -d --wait
```

更新源码或 Dockerfile 后需要重新构建时，使用 `docker compose up -d --build --wait`。修改 `.env` 后按下文重新创建容器。

### 配置与旧版本迁移

旧部署仍将凭据或功能开关保存在 JSON 中时，需在升级启动前运行生成器进行迁移；已完成迁移的部署无需每次启动或升级都重新生成配置。

生成器可重复执行：创建 `.env` 和结构 JSON，或将旧 JSON 中的服务 Token、模型 Key、规划参数与功能开关迁入 `.env`，移除重复字段。已有非空模型值和明确的 `false` 开关优先；模板中空的模型字段可从旧 JSON 迁入。旧 `callers.tokens` 按已选服务调用方迁移首个 Token，随后移除全部旧 Token。随机 Token 只在缺少配置或仍为已知占位符时生成，只写入权限为 `0600` 的 `.env`，不打印到终端。结构 JSON 所在目录保持 `0700`，文件为 `0644`，便于绑定后由容器 UID/GID `10001:10001` 读取。凭据与本地配置不得加入 Git、构建上下文或日志。

`.env` 是服务 Bearer Token、模型配置、功能开关与 Compose 参数的统一入口，变量说明见 [`.env.example`](../.env.example)。Compose 显式把对应环境变量传给服务，进程环境变量优先于文件；结构 JSON 仅保留存储、来源规则、任务限额等结构项，不再接受认证或规划配置。需要自定义文件时，使用 `python3 deploy/generate-config.py --env-file <环境文件路径> --output <结构JSON路径>`，并让 `MEDIA_CONFIG_PATH` 对应实际结构 JSON 路径；Compose 通过 `docker compose --env-file <环境文件路径> ...` 选择同一文件。根目录 Compose 的相对路径按仓库解析；单独使用旧 `-f deploy/compose.yaml` 时相对配置路径按 deploy 目录解析，建议外部配置使用绝对路径。

确认没有活动任务后，修改 `.env` 或结构 JSON 并执行下列命令重新创建容器，不能只 restart 沿用旧容器环境：

```sh
docker compose up -d --no-build --force-recreate --wait media
```

不要执行 `source .env`，值中含 `$` 时用单引号避免 Compose 插值。检查 Compose 结构时仅使用 `docker compose config --quiet`；完整展开配置和容器环境检查可能包含凭据，不应输出到终端、日志或报告。直接运行 Python 服务默认读取 `.env`，可用 `MEDIA_ENV_FILE` 指定其他文件；结构 JSON 由 `MEDIA_CONFIG_FILE` 指定。两种启动方式都将进程环境变量置于文件值之前。

服务以非 root 运行，根文件系统只读；`state`、`work`、`shared_input` 和 `shared_output` 是四个独立卷。SQLite 只在 `state` 卷，业务容器不挂载 state/work。服务对共享输入只读，对共享输出可写；业务对输出只读。所有区域不能嵌套。外部 bind mount 需要预先满足 UID/GID 的读写权限，不能启动后自动递归修改既有业务目录所有权。

默认仅向宿主机 `127.0.0.1:8080` 映射端口。跨机部署通过实际受控 HTTPS 入口提供服务，把 `.env` 中的 `MEDIA_PUBLIC_BASE_URL` 改为远端调用方可达地址；监听映射由 `MEDIA_BIND_ADDRESS` 和 `MEDIA_PORT` 配置。实际源服务的 hostname、端口、scheme 和允许目标网段加入对应调用方的 `source_rules`；空列表表示默认禁止 HTTP 来源。不可用全网段通配代替部署时的明确目标。

默认 1 个 Worker、2 CPU、2 GiB 内存、128 PID、64 MiB `/tmp`、60 秒容器停止宽限期。配置中的 `max_work_bytes`、存储 `quota_bytes` 和磁盘余量控制持久工作/输出区域；Compose 的内存限制不构成磁盘配额。`max_frame_count` 默认 100，可按容量调整每次抽帧请求上限；`max_frame_rate` 默认 240 fps，只限制显式 constant/max 帧率，source 模式保留源帧时间节奏。这两项在服务 JSON 中配置，并通过 capabilities 公开。提高 Worker 数量之前先完成目标宿主机实测。构建参数 `BUILD_REVISION` 记录交付版本，不修改冻结操作或预设含义。

模型规划预算在 `.env` 的 `MEDIA_PLANNING_TIMEOUT_SECONDS`，默认 300 秒；媒体任务预算在结构 JSON 的 `task_timeout_seconds`，默认 3600 秒。客户端的 `--wait-seconds` 只影响一次等待，不会取消服务端任务。具体配置见[自然语言与 A2A 指南](../docs/usage/agent.zh-CN.md)。

## GHCR 镜像发布

[容器工作流](../.github/workflows/container.yml)使用官方 Actions 的固定提交 SHA。验证 job 仅有 `contents: read`，只有发布 job 增加 `packages: write`，登录 GHCR 使用该次工作流的 `GITHUB_TOKEN`，无需新建或保存个人 PAT、模型 Key 或服务 Token。源码仓库的 Actions 必须允许这些官方 Actions；仓库默认只读 Token 权限可以保持不变。

| 事件 | 行为 |
| --- | --- |
| Pull request | 两种架构验证，不登录或推送 GHCR |
| main push，或 main 上手动运行 | 两种架构验证成功后发布 `main`、`latest` 与提交 SHA 标签 |
| `vX.Y.Z` tag push | 验证并确认版本与 `pyproject.toml` 相同，发布 `X.Y.Z` 与提交 SHA 标签 |
| Fork 仓库 | 可运行验证，发布 job 固定限定 `breakstring/ffmpeg-agent` |

仅修改 Markdown、`docs/` 或项目跟踪绑定的提交不会触发镜像构建；可以从 main 手动运行。版本标签仍触发验证与发布。

每个架构使用对应的原生 GitHub runner：`ubuntu-24.04`（amd64）和 `ubuntu-24.04-arm`（arm64）。先检查 Ruff、Pyright 和技能生成副本，再构建生产镜像，在精确镜像 ID 内执行完整 pytest，并通过 HTTP/MCP 验证六类真实媒体操作。测试只用项目 fixture、临时状态和模拟模型，不调用付费模型服务。

通过的镜像以 `docker save` 传给发布 job；发布前核对镜像 ID、架构、源码 revision 与测试报告，随后推送并合并清单，不在发布阶段重新构建。Actions 保存分架构测试报告及最终清单 digest；镜像传递文件保留 1 天、验证报告 14 天、发布清单 90 天。报告不能替代两台业务宿主机的网络与权限验收。

本仓库镜像已验证可匿名读取与拉取。迁移到其他仓库或首次创建新包时，仍应独立检查 Package 可见性和关联仓库；源码仓库公开不保证包公开。确认对外分发后设置为 Public，并从未登录的客户端验证拉取。公开后应按 GitHub 当前规则处理可见性，不能假定可以改回 Private。参考 [GitHub GHCR 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)与[包权限说明](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)。

基础镜像 digest、uv、Python 依赖和各架构系统包版本已固定；APT 下载源仍是 Debian 活动仓库。若固定版本不再可取，构建会失败，应核对并重新验证基线，不能为让发布变绿而静默删除版本约束。没有成功的 Actions 结果和清单，不把文档里的目标架构当作已发布或已验证证据。

## 上传与远程 MCP

`.env` 中的 `MEDIA_SERVICE_TOKEN` 统一保护 HTTP、上传、产物下载、`/mcp`、启用后的 `/a2a` 与 Agent Card。Token 至少为 24 个无空格的可打印 ASCII 字符；缺失或空值阻止启动，只有显式 `MEDIA_SERVICE_TOKEN=null` 才开放服务。旧 JSON 的 `service_token` 必须先经生成器迁移；运行时不再回退到 JSON 或 `callers.tokens` 获取服务凭据。结构配置中的 `service_caller` 仅指定现有任务和存储所用的服务空间，不增加用户系统。自然语言与 A2A 默认关闭，设置方法见[使用指南](../docs/usage/agent.zh-CN.md)。

独立上传使用 `POST /v1/files`（原始二进制、`application/octet-stream`）；请求内上传使用 `POST /v1/jobs/upload`（multipart，`job` JSON 在先）。托管输入放在私有 work 卷的 `_uploads`，不额外要求业务共享卷，也不写只读输入卷。默认总上传预算 4 GiB、同时接收 2 个、总时限 300 秒、闲置 30 秒，且受整个 work 卷预算限制；实际配置和能力查询为准。上传完成和任务接受是不同检查点，失败请求中的完整文件按有限 TTL 保留供重试，半文件关闭后定向回收。

远程 MCP 使用 `MEDIA_PUBLIC_BASE_URL` 加 `/mcp`，同进程提供 Streamable HTTP。入口按该公开地址检查 Host 和 Origin；反向代理应保留正确的公开 Host。客户端先用 HTTP 上传取得 `file_id`，再通过 MCP 处理和查询，最后带同一 Token 从 HTTP 下载产物。没有 stdio 桥接或远端客户端路径读取。调用示例见[README](../README.zh-CN.md)。

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

两侧都使用本仓库的 `video_h264_aac_square.mp4`；业务侧受控文件服务必须让媒体节点能够 GET 到同一字节内容。媒体配置的 `source_rules` 为真实来源指定精确 `host`、`ports`、`schemes` 和允许的 `networks`（私网目标需显式列出精确目标网段）。更新配置后重启媒体容器，并确认来源响应的 Content-Length 与仓库素材一致。

在**业务实际宿主机**准备私有配置，不在命令行传 token 或签名 URL：

```sh
mkdir -m 700 -p deploy/local
cp deploy/remote-check.example.json deploy/local/remote-check.json
chmod 600 deploy/local/remote-check.json
```

将媒体宿主机采集的 `media-host.json` 复制到业务宿主机的 `deploy/local/media-host.json`。编辑私有 JSON 副本中的服务 HTTPS 地址、来源 URL/受控头和 fixture 路径；专用服务 Token 只写入权限为 `0600` 的 `.env` 的 `MEDIA_SERVICE_TOKEN`。此验收要求认证，Token 不得为空或 `null`；旧副本中的 `caller_token` 须移出 JSON。部署方确认两台实际宿主机不同后，将 `distinct_physical_hosts_confirmed` 设为 `true`；该确认和主机指纹共同作为验收依据，脚本不能独立证明物理拓扑。不要在容器内或两个 VM 身份下冒充两个实际宿主机。随后执行：

```sh
python3 deploy/verify-http-remote.py run \
  --config deploy/local/remote-check.json --env-file .env \
  --output deploy/local/remote-video.mp4 \
  --report deploy/local/two-host-http.json
```

脚本执行 HTTP 输入转码、同键重放、等待输入释放、申请租约、HEAD、单 Range/If-Range、完整下载校验与真实 ffprobe 校验；成功后释放租约并提前 release 本次自有产物。已存在的下载文件不会被覆盖。报告只有脱敏主机指纹、镜像/引擎、任务/产物 ID、摘要和断言，不含 token、源 URL 或本地路径。`--local-smoke` 的报告固定标为 `single_host_http_smoke`，不能计为本节通过。

## 本地验收与容量实测

在可用 Docker daemon 上执行。脚本创建带随机名称和所有权标签的专用容器、卷及网络，完成后只清理本次创建的资源。仓库 fixture 只读挂载，再复制到专用输入卷；不挂载既有业务目录。每次生成的测试 Token 仅存在本次验收的私有环境文件或进程环境中。

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

六类媒体参数验收使用同一精确镜像，分别覆盖 Token 开/关、HTTP 与远程 MCP 的最简调用和自定义参数：

```sh
.venv/bin/python deploy/verify-parameters.py --image <精确镜像ID> \
  --report deploy/parameters-container-arm64.json
```

该脚本验证六个操作及六个媒体工具的共享参数、默认值、幂等重放、有效参数一致性、文件/抽帧成员的 HEAD/Range/SHA 下载，并解码检查实际编码、尺寸、音频规格和帧时间。只读取本项目 fixture，使用独立容器和私有卷，结束后只清理本次创建的资源；报告不能代替另一架构或两台实际宿主机的验收。执行结果和未验证条件记录在[实施进度](../docs/plans/v1-implementation-progress.md)。

以下为旧素材的历史容量基线；2026-10-15 已迁移到本仓库生成的专用素材，这些吞吐数据不代表新素材。2026-10-13 的原生 Linux arm64 实测如下。每组用 `Feline_Apocalypse.mp4` 执行 8 次 `video.transcode.v1` / `web_720p_v1`，限制为 2 CPU / 2 GiB / 128 PID。完整数据及镜像 ID 在 [`capacity-arm64.json`](capacity-arm64.json)。

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
