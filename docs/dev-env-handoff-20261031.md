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

运行脚本时：`PYTHONPATH="src;."`（Linux 用 `src:.`），`PYTHONUTF8=1`。

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
