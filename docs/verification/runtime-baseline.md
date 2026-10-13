# P0 运行与构建基线

验证日期：2026-10-13。此记录仅证明依赖与基础引擎可用，服务行为、恢复、并发和三种部署的验收记录另行维护。

## 已验证环境

宿主为 macOS arm64，Docker daemon 为 OrbStack Linux aarch64，10 个逻辑 CPU、约 7.8 GiB 内存。测试只构建本项目镜像和运行自动移除的本项目容器，未修改已有业务容器。

| 环境 | Python | SQLite | FFmpeg/ffprobe | 结果 |
| --- | --- | --- | --- | --- |
| 本机 `.venv` | 3.14.7，常规 GIL | 3.53.1 | 宿主 9.0.1 | 基础探测脚本退出 0；不充当部署 7.1 验收 |
| Linux arm64 原生容器 | 3.14.8，常规 GIL | 3.46.1 | 7.1.5-0+deb13u1 | 非 root、只读根文件系统下基础探测退出 0 |
| Linux amd64 模拟容器 | 3.14.8，常规 GIL | 3.46.1 | 7.1.5-0+deb13u1 | 同一脚本退出 0；不充当 amd64 原生容量证据 |

两种 Linux 架构分别真实执行了 FastAPI OpenAPI/Pydantic 严格模型、aiosqlite 参数化事务、H.264/libx264 与 AAC 合成/ffprobe 探测、PCM 转换，以及 Pillow JPEG/PNG/WebP 保存后解码。测试脚本为 [`deploy/probe-runtime.py`](../../deploy/probe-runtime.py)，结果为 [`runtime-probe-arm64.json`](../../deploy/runtime-probe-arm64.json) 和 [`runtime-probe-amd64.json`](../../deploy/runtime-probe-amd64.json)。本记录没有用软件包可安装性替代上述执行结果，也没有断言全部媒体预设通过。

本机 uv 0.12.8 提供的最新 3.14 下载项为 3.14.7，`.python-version` 因此固定 3.14.7。Docker 使用官方镜像的 3.14.8；`uv sync --frozen --python /usr/local/bin/python` 明确使用容器解释器。项目要求 `>=3.14,<3.15`。

## 精确版本

| Python 依赖 | 锁定版本 |
| --- | --- |
| FastAPI / Pydantic / pydantic-settings | 0.143.0 / 2.14.1 / 2.15.0 |
| Uvicorn / aiosqlite / aiohttp | 0.54.0 / 0.22.1 / 3.14.5 |
| Pillow | 12.3.0 |
| MCP SDK / python-multipart（P8～P12 增量） | 2.1.0 / 0.0.22 |
| Ruff / pyright | 0.17.0 / 1.1.414 |
| pytest / pytest-asyncio / httpx | 9.1.1 / 1.4.0 / 0.28.1 |

直接依赖在 `pyproject.toml` 使用精确版本；传递依赖、下载位置与摘要在 `uv.lock`。构建仅安装运行依赖。本机安装包含开发依赖。

MCP/上传增量已在本机和原生 arm64 服务镜像验证，见[增量验证记录](upload-mcp-validation.json)。P0 的双架构探测只覆盖当时依赖；尚未把新 MCP 依赖作为 amd64 运行验收通过。

基础镜像已从官方 registry 实时读取清单：

- `python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170`，实际 3.14.8。
- Python amd64 manifest：`sha256:cfe2e24a75302a15934d37c2d86412893c0aa934dc3a97cbb439d04c01890ca9`。
- Python arm64 manifest：`sha256:7f47c8ffd4e70e88f7326b68e7df95109597ef66023a1052b7d1ec18390d2604`。
- `ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a`。
- Dockerfile frontend：`docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e`。

Debian 13 系统包按架构完整固定：[`system-packages-arm64.tsv`](../../deploy/system-packages-arm64.tsv) 有 289 项，[`system-packages-amd64.tsv`](../../deploy/system-packages-amd64.tsv) 有 292 项。Dockerfile 精确安装清单，并用 `dpkg-query` 结果逐行比较；两架构重建已退出 0。包括 FFmpeg `7:7.1.5-0+deb13u1`、ca-certificates `20250419`、libx264 `2:0.164.3108+git31e19f9-2+b1`。旧包被仓库移除时构建会失败，不能自动换版本；届时更新清单、重新验证后再构建。

`ffmpeg -L` 实际显示 `--enable-gpl`、libx264、libx265，并声明 GPL v2 或更新版本；镜像保留 Debian 包自带的 `/usr/share/doc/*/copyright`。本项目没有将该 Debian 构建描述为 LGPL-only。

官方背景材料：[Python 官方镜像](https://hub.docker.com/_/python)、[Debian trixie FFmpeg](https://packages.debian.org/trixie/ffmpeg)、[Pillow 12 发布说明](https://pillow.readthedocs.io/en/stable/releasenotes/12.0.0.html)、[Pydantic 变更记录](https://docs.pydantic.dev/latest/changelog/)。精确可用版本以本次锁文件及真实安装结果为准。

## 可复执行命令

在仓库根目录执行；双架构分别运行，不能用一个架构结果替另一个打勾：

```sh
uv sync --frozen
docker build --target runtime-base --platform linux/arm64 -t ffmpeg-agent:p0-arm64 .
docker build --target runtime-base --platform linux/amd64 -t ffmpeg-agent:p0-amd64 .
docker run --rm --platform linux/arm64 --read-only --user 10001:10001 \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount type=bind,src="$PWD/deploy/probe-runtime.py",dst=/probe-runtime.py,readonly \
  ffmpeg-agent:p0-arm64 python /probe-runtime.py
docker run --rm --platform linux/amd64 --read-only --user 10001:10001 \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount type=bind,src="$PWD/deploy/probe-runtime.py",dst=/probe-runtime.py,readonly \
  ffmpeg-agent:p0-amd64 python /probe-runtime.py
```

`docker compose config --quiet` 已对基础、同 Compose 合并、external 合并及独立业务侧四种配置通过解析；配置 JSON 已经 `Settings.model_validate` 校验。此检查不证明服务就绪、真实卷交接或容器恢复。

## 剩余条件

- 没有第二台真实宿主机，跨宿主机 HTTP 交付仍需独立验收。
- amd64 当前为模拟执行，未取得原生 amd64 的吞吐、RSS、CPU 和取消延迟容量结果。

## 后续服务验收

后续实现已构建 `service` 镜像，并在上述原生 arm64 Linux 环境实际完成以下检查。报告记录各自使用的镜像；重新构建后应按服务变更选择重跑，不把 P0 的双架构依赖探测扩张成双架构服务验收。

- [`tests-arm64.json`](../../deploy/tests-arm64.json)：最终服务镜像 `sha256:f5a01280a8b1771e80e8df8fbeb3d1136ed95f6dbb6469d187fbf0dd5e2c236b`，Python 3.14.8 / FFmpeg 7.1.5、非 root、只读根文件系统中完整测试 `238 passed in 33.39s`。测试依赖按锁文件安装在临时目录，未修改服务镜像。
- [`container-p3-arm64.json`](../../deploy/container-p3-arm64.json)：健康检查、认证、并发幂等提交、共享路径 probe 产物及摘要、排斥第二实例，以及容器 kill/start 后持久记录与产物读回。
- [`deployments-arm64.json`](../../deploy/deployments-arm64.json)：同一 Compose 与两个独立 Compose 项目间的真实卷交接；业务原子写入、申请租约后读产物、摘要校验、输出只读、实际卷身份相同且 state/work 不向业务暴露。
- [`http-script-smoke-arm64.json`](../../deploy/http-script-smoke-arm64.json)：同一宿主机上的受控 HTTP 来源、真实视频转码、租约、HEAD/Range/If-Range、完整下载摘要、ffprobe 与租约释放。与上述完整测试、P3 和 Compose 报告使用同一个最终镜像；此报告明确是 `single_host_http_smoke`，不计为两台实际宿主机验收。
- [`capacity-arm64.json`](../../deploy/capacity-arm64.json)：2 CPU / 2 GiB / 128 PID 下的 Worker 1/2/4 真视频转码，以及观察到实际 FFmpeg 后取消并确认进程停止。每组 8 个任务，详细口径与结果见 [部署说明](../../deploy/README.md#本地验收与容量实测)。
