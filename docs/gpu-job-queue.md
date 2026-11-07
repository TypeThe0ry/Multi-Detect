# GPU 小任务队列

`scripts/gpu_job_queue.py` 是 Linux 上的轻量文件队列，只依赖 Python 标准库、
`/proc`、`/bin/bash` 和 `nvidia-smi`。它持续补充空闲 GPU 上的任务，无需在某个
训练脚本结束后手工启动下一份脚本。默认队列目录：

```text
/data/projects/Multi-Detect-env/runs/jobq/
  pending/       等待调度的 JSON
  running/       已领取 GPU 的任务（包括需要人工核查的 lost 任务）
  done/          脚本退出码为 0 的任务
  failed/        非零退出、取消、依赖失败或启动失败的任务
  logs/          每次启动的 stdout/stderr 合并日志
  .control/      监督进程的子进程身份、取消请求和退出回执
  .daemon.lock   守护进程单实例锁（flock）
  .state.lock    提交、调度、取消和查询的短期文件状态锁
```

队列目录是可信的本机控制目录：提交脚本具有运行账户的全部权限。不要让不可信
账户写入，也不要把它放到 NAS 等不保证本地锁/原子重命名语义的文件系统中。
同一主机只部署一个共享队列目录；不同目录之间没有 GPU 互斥。

## 快速使用

以下都是供操作者执行的示例；开发测试不启动真实 GPU 任务。

```bash
cd /data/projects/Multi-Detect-env/Multi-Detect
PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
Q=/data/projects/Multi-Detect-env/runs/jobq

# 前台持续运行；可放到自己的 tmux 会话或进程管理器中。
"$PY" scripts/gpu_job_queue.py --queue-dir "$Q" run \
  --memory-threshold-mib 4000 --poll-seconds 5
```

另一个终端设置同样的 `PY`、`Q` 变量后，提交已准备好的任务 JSON：

```bash
"$PY" scripts/gpu_job_queue.py --queue-dir "$Q" submit /path/to/train.json
"$PY" scripts/gpu_job_queue.py --queue-dir "$Q" list
"$PY" scripts/gpu_job_queue.py --queue-dir "$Q" cancel train-round-a
```

`--queue-dir` 是全局参数，放在 `run/submit/list/cancel` **之前**。不指定时使用默认路径。
`submit` 校验后原子写入 `pending/`，不会执行命令；`list` 输出全部任务的 JSON，
包含 `queue_state`、`runtime` 和尚未完成的依赖 `waiting_for`。取消运行中任务是异步的，
应再次查询确认最终状态。命令失败返回 1，参数解析错误返回 2。

守护进程每轮会回收完成任务、处理依赖并尽量填满空闲卡。第二个 `run` 会因
`.daemon.lock` 退出；不要删除锁文件，删除后会破坏 inode 上的互斥。
SIGINT/SIGTERM 只停止调度。运行中的任务由独立会话内的监督进程继续管理，
重新执行同一 `run` 即可接管。日志可从 `runtime.log` 指向的路径查看。

## 任务格式与 GPU 选择

固定卡任务：

```json
{
  "name": "train-round-a",
  "gpus": [0],
  "command": "/data/projects/Multi-Detect-env/runs/train-round-a.sh",
  "priority": 10,
  "after": []
}
```

在 GPU0、GPU1 中任选一张空闲卡：

```json
{
  "name": "eval-round-a",
  "any_of": [0, 1],
  "command": "/data/projects/Multi-Detect-env/runs/eval-round-a.sh",
  "priority": 0,
  "after": ["train-round-a"]
}
```

- `name` 全队列唯一，保留 `done/failed` 后也不能复用。允许 ASCII 字母、数字、
  点、下划线、连字符，首字符须为字母或数字，长度 1–128。
- `gpus` 与 `any_of` 必须且只能提供一个，数组非空，元素是不重复的非负整数。
  `gpus: [0, 1]` 要同时占用两张卡；`any_of: [0, 1]` 只占一张，按数组顺序选择。
- GPU2/GPU3 **不会被自动补入候选集合**。只有显式写入对应数组才能使用；
  工作站 GPU3 不稳定，日常任务建议只列 0/1。
- `command` 是已经存在的绝对 shell 脚本路径，由 `/bin/bash` 通过参数数组执行，
  不进行字符串拼接或 `shell=True` 求值。不支持内联命令、参数串或自动重试。
  脚本无需 executable 位，运行时仍须可读。工作目录为脚本所在目录，stdin 为 `/dev/null`。
- `priority` 默认 0，整数越大越先调度；同优先级按任务名排序。没有抢占和等待时长加权。
  优先任务资源不足时可先运行其他可执行任务。
- `after` 默认 `[]`，只有全部依赖在 `done/` 且退出码为 0 才能运行。
  未知依赖保持等待，允许之后提交；失败或取消会使下游递归进入 `failed/`。
  `submit` 拒绝自依赖、重复依赖和已知任务间的循环依赖。
- 不接受其他输入字段，尤其不能在提交文件中填入 `runtime`。

每张卡必须同时满足：没有本队列任务预留；`nvidia-smi` 报告的 `memory.used`
**严格小于** `--memory-threshold-mib`（默认 4000 MiB）。GPU0 的桌面常驻显存也计入。
未知卡、N/A、命令失败、解析错误或 10 秒查询超时都不会被当作空闲。
查询整体失败时本轮停止启动新任务，但仍处理完成回执和依赖失败。

队列预留覆盖从启动到退出回执的全过程，即使训练脚本暂时没有分配显存，也不会
在同一张卡上再启动一个队列任务。显存检查只是对队列外程序的瞬时观测，不能防止
外部程序在查询后抢卡；需要持续互斥的作业都应使用同一队列。

也可由外部生产者先在 `pending/` 外写完 JSON，再在同一文件系统原子重命名为
`pending/<name>.json`；文件名必须与 `name` 相同。推荐使用 `submit`，它还会检查
重名和循环依赖。手工输入的非法任务会进入 `failed/`；损坏的运行记录、重复任务名等
涉及预留安全的错误会停止守护进程，先修复记录再重启。不要原地编辑运行中的 JSON。

## 现有 bash 队列的接入

监督进程为脚本设置 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 和已分配物理卡的
`CUDA_VISIBLE_DEVICES`，其余环境继承守护进程。CUDA 程序在脚本内部看到的逻辑卡
会从 0 重新编号，例如物理 GPU1 单卡任务内部的 CUDA 设备号为 0。

现有 `queue_r21id_r5_round.sh` 固定 `CUDA_VISIBLE_DEVICES=0`，
`nightly_r21idr3_r24.sh` 固定为 1，还有额外的模拟器 GPU 参数。
直接接入这些脚本时应申明相同的固定 `gpus`。要改为 `any_of`，需要先移除脚本对
`CUDA_VISIBLE_DEVICES` 的覆盖，并核对脚本、容器、渲染器的设备编号。
这是协作式调度，不是设备隔离；脚本自行覆盖环境或自行开容器仍可能使用未预留的卡。

脚本需以前台方式等待全部工作完成，使用 `set -e` 或显式保存/返回重要命令的退出码，
并用 trap 负责清理自己的子进程。例如旧脚本最后的成功 `echo` 可能掩盖上一条命令的
失败，队列只能依据整个 bash 脚本的退出码判断成功。脚本自己的显存等待环可以删除，
否则它可能一直持有预留但不做计算。任务内现有的 SITL/eval 互斥锁仍需保留。
本实现没有改动或执行上述脚本。

## 进程身份、退出记录与恢复

运行记录的 `runtime` 包含监督进程 `pid`、`identity`（boot ID、`/proc/<pid>/stat`
的启动 ticks、唯一 token）、分配的 `gpus`、领取时间 `start`、日志路径 `log`、
结束时间 `end`、退出码 `exit` 和状态。`start` 是领取资源的时间；
`script_start`/`script_pid` 在子脚本启动后记录在 `.control/<token>.child.json`，
并在收尾时归入最终 `runtime`。退出码保留 Python 的负信号号约定，无法得知则为 null。

启动先把任务移入 `running/`，再创建监督进程；监督进程等待自己的精确身份持久化后
才执行脚本。因此守护进程在启动边界退出不会使同一个任务被重新执行。
尚未提交身份的启动会进入 `failed/`，未获许可的监督进程最多等待 30 秒后退出。
监督进程将实际 bash 退出码和结束时间原子写入退出回执，所以即使守护进程已重启、
无法再 `waitpid` 原来的子进程，也能收集结果。监督进程不能依赖的临时 worktree
删除操作，应等队列清空后再进行。

恢复时只检查持久化的 PID、启动 ticks、boot ID 和 token；不会通过名称或命令行
寻找进程。相同 PID 已被复用也不会被接管或发送信号。若监督进程被 SIGKILL、主机
重启或 `/proc` 不可读取，且没有退出回执，则记录标为 `runtime.status=lost`，
继续保留在 `running/` 并占用原 GPU。这样可避免监督进程死亡后仍存活的脚本或
后代与新任务重叠，不会编造成功退出码或自动重跑。

`lost` 的人工处理：先停止守护进程，核查日志、`.control/` 中的子进程身份、
主机是否已重启及 GPU 实际占用，确认旧任务及其后代均已结束。然后将该任务记录
移至 `failed/`（保留原有身份和未知退出码），再重启守护进程。需要重试时使用新任务名。
不要仅凭显存为零或单个 PID 不存在就释放预留。

## 取消行为与边界

待运行任务直接移入 `failed/`，状态为 `cancelled`。运行中任务只有在监督进程
身份匹配时才写入取消请求；监督进程每 0.2 秒检查一次，仅向它自己 `Popen` 并持有的
bash 子进程 PID 发送 SIGTERM，10 秒未退出则 SIGKILL。脚本尚未启动时直接记录取消。
取消结果仍进入 `failed/`，下游不会继续。

不使用 `pkill`、进程名匹配或进程组群杀，不会向外部训练进程或 PID 复用后的进程
发送信号。为遵守仅结束已启动 PID 的边界，监督进程**不遍历或杀死脚本后代**；
任务必须以前台方式运行，并在收到 TERM 时自行等待/清理其子进程。强制杀死 bash
不能保证清理不配合的后代；这种情况下须人工核查，显存门槛只是额外保护。

队列 JSON、日志、控制文件均不会自动清理。任务名和依赖历史靠这些记录保持，
归档前需确认没有运行中任务或待运行任务引用它们。保留运行脚本及本 Python 文件
所在的 checkout，直到所有监督进程都退出。

## 开发验证

单元测试使用假的 `nvidia-smi`、`Popen` 和进程身份，不启动真实子任务或访问真实 GPU：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_gpu_job_queue.py -q -p no:xdist

# 现有 CPU-only 回归闸门，默认单进程。
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/run_onboard_gate.py -q -p no:warnings -p no:xdist
```
