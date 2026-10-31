# 开发环境迁移交接（2026-10-31）

旧机器：Windows 11，RTX 4060 8 GB，系统内存长期被其它项目挤占，训练经常排队等资源。
新环境目标：更多显卡、更大内存，接着跑模型轮次和 Gazebo SITL。

## 1. 代码

- 仓库：`https://github.com/TypeThe0ry/Multi-Detect`，`main` 已快进到集成分支最新提交（本文所在提交）。
- 旧机器上的集成分支是 `wt/integration`，推送后同名内容即 `main`；另保留 `release/pre9`。
- 未合入集成线、仅作存档的分支：`archive/wt-health`（运行时健康监控早期版本）、`archive/wt-geofence`（主机侧统一安全包络）。
- `feat/sim-teacher-data` 的 5 个提交已以等价补丁形式在 `main` 中，不再单独推送。

## 2. Python 环境

| venv | Python | 用途 |
|---|---|---|
| `.venv` | 3.12.7 | 主环境：`pip install -e .`（见 `pyproject.toml` / `uv.lock`） |
| `.venv-vlm` | 3.12.7 | 慢模型训练/评测：torch 2.6.0+cu124、transformers 4.57.6、peft 0.17.1，另装 `requirements-vlm-*.txt` |

运行脚本时：Windows 使用 `PYTHONPATH="src;."`，Linux 使用 `PYTHONPATH="src:."`，并设置 `PYTHONUTF8=1`。

## 3. 外部依赖（不在 Git 里，需要在新环境重新下载）

- 基座模型：`Qwen/Qwen3-VL-2B-Instruct`（HF snapshot `89644892e4d85e24eaac8bacfd4f463576704203`），放到 `artifacts/cache/huggingface-vlm/`。
- mmproj：`Qwen/Qwen3-VL-2B-Instruct-GGUF` 的 `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf`，放到 `artifacts/cache/hf-vlm-gguf/`。
- llama.cpp：`b11552`（CUDA 12.4 构建），`llama-server` 放到 `artifacts/cache/llama-cpp/b11552/`，或用 `LLAMA_SERVER` 环境变量指定。
- Docker 镜像：`px4io/px4-sitl-gazebo:latest`，以及由 `simulation/` 下 Dockerfile 构建的 `multidetect/px4-gazebo-viewer`。

## 4. 模型与数据包（GitHub Release `handoff-20261031`）

| 文件 | 解压/放置位置 | 说明 |
|---|---|---|
| `slow-r18-Q5_K_M.gguf` | `artifacts/r18-round/merged-gguf/slow-system-Q5_K_M.gguf` | 当前机载慢模型（身份确认）基线 r18 |
| `planner-r14-Q5_K_M.gguf` | `artifacts/models/slow-system-lora-v2-r14-multi-merged-gguf/slow-system-v2r14multi-merged-Q5_K_M.gguf` | 当前规划模型基线 r14 |
| `md-handoff-small.tar.gz` | 在仓库根目录解压 | r18/r14 LoRA adapter、YOLOE 机载检测权重、r18 训练集与 v1/v3 基线评测、plan-sft-v4 数据、旧机器排队脚本 |
| `r21id-r3-data.tar.gz` | 在仓库根目录解压 | r21id-r3 已采集的 episode（72 个世界 × 2）与 build_sft 产物（合并训练集 22843 行、测试 2204 行） |
| `SHA256SUMS` | — | 校验用 |

当前推荐的机载组合配置：`configs/onboard/sim-loop-v3-r18id-r14plan-defaults.json`（r18 慢模型 + r14 规划 + YOLOE v3 1280）。
配置里的 `gguf` 写的是旧机器绝对路径，新环境改成相对路径或本机路径即可。

大体量数据（SITL 采集的 episode、各轮中间产物，合计数百 GB）没有上传；需要时用下面的流水线在新环境重新采集，新环境更多 GPU 可以开多个 worker。

## 5. 当前模型结论

- 慢模型：r18 仍是基线。r21id（v3 干扰物拒绝 0.672）、r21id-r2（小目标 <12 px 召回 0.622）都未替换，详见 `docs/slow-system-r21id-plan.md`。
- 规划模型：r14 仍是基线。r22 未替换（v3 0.588 vs 0.662），详见 `docs/planner-round.md`。
- 崩溃恢复：真实 SITL 5 个场景全部通过，见 `docs/crash-resume.md`。

## 6. 迁移时未完成的两轮

两轮都只完成了数据准备，训练尚未开始（旧机器 GPU/内存一直被占用）。

### r23 规划轮

```bash
python scripts/run_planner_round.py --stage all --out <out>/r23 --eval-root artifacts/evaluation/r23-planner-round \
  --rows 2000 --val-rows 100 --intent-count 80 \
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl \
  --negative-target-mode rejected_candidate --r23-recipe --r23-v3-count 600 --r23-deliver-count 80 \
  --learning-rate 2.5e-5 --min-free-disk-gb 12 --min-memory-gb 8
```

r14 热启动；判定看 v3/v4 与 r14、r22 对比。

### r21id-r3 慢模型轮（已取消类别留出）

用户决定：主门 = r3 冻结测试世界（`configs/scenarios/generated-r21id-r3` 的 12 个 test 世界，r18 与候选同一套）+ 真实飞行集 v1；v3 仅作参考。

```bash
python scripts/run_r15_round.py --stage train --low-disk --out artifacts/r21id-r3/pipeline \
  --scenarios configs/scenarios/generated-r21id-r3 --r15-index configs/scenarios/generated-r21id-r3/index.json \
  --episodes artifacts/r21id-r3/episodes --r12-replay-fraction 1.0 --min-free-gb 12 \
  --no-category-holdout --workers 1 --episodes-per-world 2 \
  --r12-dataset artifacts/r18-round/r15-sft --r21id-r2-filter --r21id-keep-small-true-targets \
  --learning-rate 5e-5 --r12-adapter artifacts/r18-round/adapter/adapter \
  --r12-baseline artifacts/r18-round/eval-v3.json --r12-v1-baseline artifacts/r18-round/eval-v1.json \
  --baseline-label r18 --candidate-label r21id-r3
```

完整参数以 `_scratch/queue_r21id_r3b.sh`（在 `md-handoff-small.tar.gz` 里）为准。解压 `r21id-r3-data.tar.gz` 后 collect/annotate/build_sft 三个阶段已完成，从 `--stage train` 接着跑即可。
跑完后用 `scripts/run_r21id_view_eval.py` 让 r18 与 r21id-r3 在同一 test 集上做四视角评测。

## 7. 约定（沿用）

- 提交署名 `TypeThe0ry <hahatt0902@gmail.com>`，不加 AI 署名；分支不以 `claude` 开头。
- 受证据约束的文件只经生成器或真实运行修改：`configs/release-parity.json`、`docs/current-progress.json`、`configs/schemas/*.json`、`configs/product-version.json`。
- 文档用中文；commit message 用小写英文 Conventional Commits。
- 联合 QGC 验收与 HIL/真机飞行由负责人执行。

## Linux 仿真环境

Linux 端的 SITL 入口已经保留 Windows 参数语义，并把宿主机相关路径和进程探测改成平台实现。Docker 仍使用 bridge 网络：PX4 容器通过 `host.docker.internal` 回传宿主遥测，命令带有 `--add-host=host.docker.internal:host-gateway`；只有控制 UDP 端口发布到 `127.0.0.1`，遥测端口保持容器主动发出，避免 Docker 端口 hairpin。

迁移前后的 Windows 专属写法盘点如下：

| 位置 | 问题 | 修法 |
| --- | --- | --- |
| `scripts/run_simulator_docker.py` | Docker Desktop 路径和 `host.docker.internal` 假设 | Docker 可执行文件按平台发现；Linux 使用原生 Docker，保留 `host-gateway` 映射和现有 UDP 端口设计 |
| `scripts/collect_sim_teacher_episodes.py` | 固定 `docker.exe`，独立采集没有互斥 | Linux 优先 `PATH` 中的 `docker`；采集阶段接入 `scripts/sitl_lock.py` |
| `scripts/run_r15_round.py` | `.exe` llama-server、`D:/Projects/...` 默认值，collect 没有统一 SITL 锁 | 按平台选择 `llama-server`，默认路径改为仓库相对路径；collect 使用同一把锁 |
| `scripts/run_nightly_regression.py` | `D:\...` 锁路径、PowerShell 内存计数器、仅 PID 存活判断 | 复用跨平台 Python 锁；Linux 使用 `psutil`/`/proc/meminfo`，owner 同时记录 PID 和进程启动时间 |
| `scripts/run_sim_session.py` | `.exe` 默认名和 junction 错误提示 | `LLAMA_SERVER`/平台文件名解析，资产缓存按普通目录准备 |
| `scripts/run_crash_resume_sitl.py` | `PYTHONPATH` 使用 `;` | 使用 `os.pathsep`，Linux 生成 `src:.` |
| `scripts/generate_r15_worlds.py`、`src/multidetect/simulator/world_gen.py` | 未发现 Windows 专属运行时写法；依赖缺失 Fuel 缓存 | 保持生成逻辑，增加 `scripts/fetch_sim_assets.py` 负责下载、哈希校验和 resolved 构建 |

准备镜像和资产（不会由脚本自动联网）：

```bash
export PYTHONPATH=src:.
export PYTHONUTF8=1
export TMPDIR="$PWD/.tmp"
mkdir -p "$TMPDIR"

docker pull px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8
python scripts/fetch_sim_assets.py \
  --catalog configs/sim-assets/fuel-catalog.json \
  --raw-root artifacts/cache/gz-fuel/OpenRobotics \
  --resolved-root artifacts/cache/gz-fuel/resolved
```

锁默认位于 `/data/scratch/multidetect-sitl.lock`，也可以用 `MULTIDETECT_SITL_LOCK` 或 `--sitl-lock-path` 指定。它会等待没有 `md-*`/`r15-worker*` 运行容器且可用内存至少 5 GiB；owner 进程死亡并且锁超过 120 秒才回收。llama.cpp 可执行文件默认查找 `artifacts/cache/llama-cpp/b11552/llama-server`，也可设置 `LLAMA_SERVER`。

一次无头教师采集冒烟（真实 Docker/SITL 由负责人执行）：

```bash
python scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/urban_inspection_multirotor_001.scenario.json \
  --teacher expert_search --episodes 1 --steps-per-episode 20 \
  --rgb-size 1280x960 --depth-size 1280x960 \
  --out artifacts/smoke/sim-teacher-linux \
  --control-port 14540 --telemetry-port 14541
```

资产缺失、Docker daemon 不可用或内存门禁未满足时，先修复环境再重试；不要把 `artifacts/` 软链接指向的共享缓存提交到 Git。

### 不改防火墙：宿主网络模式

Linux 上如果 ufw 的 `INPUT` 默认策略阻止了 bridge 网关发往宿主的遥测，可以把 SITL
容器切到宿主网络：

```bash
export MULTIDETECT_SIM_NETWORK=host
python scripts/collect_sim_teacher_episodes.py ...
```

也可以在单次运行中传 `--sim-network host`；命令行参数优先于环境变量。该模式只在 Linux
可用，Windows 会在生成 Docker 计划时失败。容器命令使用 `--network host`，因此不会再带
`--publish` 或 `--add-host`；PX4 的 MAVLink 目标和宿主采集监听均为 `127.0.0.1`。

宿主网络共享 UDP 端口和 Gazebo transport。每个容器设置唯一的 `GZ_PARTITION`（容器名），
避免不同 worker 互相发现 Gazebo 话题；同时给 PX4 传入 worker 对应的 `-i` 实例号。r15
默认 worker 0/1 使用控制端口 `14560/14570`、遥测端口 `14561/14571`，端口段相差 10；
显式改写的端口就是每个 worker 在宿主上占用的 MAVLink 控制输入和遥测回环端口。PX4
`simulator_mavlink`、uXRCE-DDS 以及 rc 文件中的其它默认 UDP 端口由 `-i` 实例号按 PX4
启动脚本的实例规则区分，不能把多个 worker 手工改回同一个实例号；保护端口 `14550`
仍不使用。

`mavlink start -t 127.0.0.1` 指定的是发送目标，不等于自动限制本地 bind 地址。启动包装器
在 host 模式设置 `PX4_NET_INTERFACE=lo`，让 PX4 rc 的 `-f` 参数选择 loopback；使用其它
PX4 镜像时仍应检查 rc 文件是否支持该参数，并用 `ss -lunp` 确认 MAVLink/uXRCE-DDS 只在
回环上监听。若镜像忽略接口参数，宿主网络下的 PX4 UDP socket 可能仍绑定 `0.0.0.0`，这是
该模式的残余暴露风险。
