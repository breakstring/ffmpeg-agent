# 容器硬停止与发布恢复验收

2026-10-13 05:08:58–05:09:58 UTC，在本机 OrbStack 的 Linux arm64 容器中完成最终镜像的 8 项故障验收，全部通过。共执行 10 次 `docker kill --signal KILL`，每次都以 `docker wait` 和容器状态读回确认退出码为 137、`Running=false`，然后使用 `docker start` 建立新的容器运行代际。

- 镜像：`ffmpeg-agent:v1-local`，执行时固定不可变 ID
- 精确镜像 ID：`sha256:f5a01280a8b1771e80e8df8fbeb3d1136ed95f6dbb6469d187fbf0dd5e2c236b`
- 原始证据：[container-faults-arm64.json](container-faults-arm64.json)，包含 job/artifact ID、进程状态、实例锁继承、attempt 状态、发布目录状态、SHA-256、容器 ID 和停止时间。
- 验收脚本：[verify-faults.py](../../deploy/verify-faults.py)。测试入口位于 `tests/container/`，只读挂入临时容器，不进入生产镜像。

| 场景 | 故障前观察 | 重启后结果 |
| --- | --- | --- |
| 真实 FFmpeg 中断及重试上限 | `video.transcode.v1` 已启动真实 FFmpeg，任务 running，输入未释放；观察后以 SIGSTOP 固定故障时点 | 第一次中断在 attempt 2 成功；另一个任务连续中断两次后以 `EXECUTION_INTERRUPTED` 失败，attempts=2，未创建第三次 attempt；两者输入均释放 |
| 发布 intent | 元数据 publishing；正式目录和 staging 均不存在；HTTP 404，无公开绑定 | 旧 artifact 进入 deleted，目录均不存在，HTTP 410；任务重新执行并产生新 artifact；等待正常后台 GC 周期确认回收 |
| 发布 staged | staging 已存在，正式目录不存在；元数据 publishing，HTTP 404，无公开绑定 | 校验并恢复同一个 artifact ID，正式目录存在，staging 消失，HTTP 200 |
| 发布 renamed | 正式目录已存在，staging 不存在；元数据仍 publishing，HTTP 404，无公开绑定 | 完成提交并保留同一个 artifact ID，HTTP 200 |
| 发布 committed | 正式目录存在，元数据 ready，HTTP 200；步骤已绑定 artifact | 绑定 ID 不变，产物仍 ready，任务成功结束 |
| 取消意图已持久化 | canceling、`cancel_requested=1`、输入未释放；停止门闩中真实 FFmpeg 仍存活 | 只完成取消收尾，canceled、attempts=1、输入释放，没有产物及第二次 attempt |
| 同一活容器热启动 | 测试入口停止调度并显式关闭数据库、释放 flock，但未记为 clean stop；容器 PID 1 保持存活 | 同容器内第二个 Service 被 `runtime_guard` 拒绝，退出 78；整容器 KILL/start 后正常就绪 |
| 孤儿媒体进程阻止接管 | 长存 supervisor 保持容器运行；仅杀 API，API 退出 -9；真实 FFmpeg 仍存活且持有继承的 instance.lock FD | 另一容器以相同状态/工作/产物卷启动，被 `instance_lock` 拒绝，退出 78；停止完整原容器后才可恢复，attempt 2 成功并释放输入 |

发布窗口使用生产 `Publisher.checkpoint_hook`，第一次到达目标窗口时写入并 fsync 持久 marker，然后等待门闩。重启后 marker 使该门闩不再触发。取消场景门闩位于测试入口覆盖的 `_stop_process`，只在已有持久取消意图时拦住真实进程的终止流程。测试未修改生产发布、执行或恢复逻辑。

最终复核中，staged/renamed/committed 三个场景将 `max_attempts=1`。恢复后同一个 artifact ID 保持不变，任务成功且 attempts 仍为1，证明完整检查点的收尾不会误判为重试预算耗尽。intent 场景仍需第二次执行，因为尚无已完成的文件检查点。

全部成功产物通过共享输出目录重新读取，并核对 API 元数据中的 SHA-256。真实视频来自仓库 fixture `Feline_Apocalypse.mp4`，发布窗口使用真实 FFprobe 结果。测试每个场景使用独立随机容器名和三个独立 volume；清理前核对本次 owner label，完成后读回没有 `ffmpeg-agent-fault-` 临时容器或 volume 残留。认证 token 仅在内存和权限隔离的临时配置文件中生成，未写入报告或命令参数。

复现命令：

```sh
docker build --platform linux/arm64 -t ffmpeg-agent:faults-local .
.venv/bin/python deploy/verify-faults.py --image ffmpeg-agent:faults-local
.venv/bin/ruff check deploy/verify-faults.py tests/container
.venv/bin/pyright deploy/verify-faults.py tests/container
```

可用 `--case intent` 等参数定向验收，并通过 `--report /tmp/result.json` 单独保存结果。Ruff、Pyright 均通过。

本记录覆盖该精确 arm64 镜像的单宿主机容器故障；不代表真实主机掉电、磁盘故障、跨宿主机迁移或 amd64 原生故障矩阵已经验收。
