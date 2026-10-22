# r15 慢模型流水线运行手册

本手册对应 `scripts/run_r15_round.py`。它把仿真采集、YOLOE v3 候选框缓存、SFT 构建、LoRA 训练、合并量化、分层评测和中文报告串成可断点续跑的阶段。每个阶段写入 `round-state.json` 和 `manifests/<stage>.json`，文件使用 SHA-256 校验；采集额外按 `world:episode-index` 记录，单个 episode 完成后立即落盘。

## 一键命令

在 PowerShell 中先设置环境（当前机器的 sandbox 不能创建指定的 `.tmp` 时应先解决权限问题，流水线不会悄悄换盘）：

```powershell
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\r15pipe\.tmp"
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$env:CUDA_VISIBLE_DEVICES="-1"
$env:PYTHONPATH="src"
python scripts/run_r15_round.py --stage all `
  --out D:/Projects/mdwt/r15pipe/artifacts/r15-round `
  --episodes D:/Projects/Multi-Detect/artifacts/r15-episodes `
  --source-root D:/Projects/Multi-Detect `
  --annotate-device cuda:0 `
  --r12-adapter D:/Projects/Multi-Detect/artifacts/models/slow-system-lora-v2-r12-multi/adapter `
  --r12-dataset D:/Projects/Multi-Detect/artifacts/datasets/slow-system-sft-v2-r12-multi `
  --model D:/Projects/Multi-Detect/artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots `
  --r12-baseline D:/Projects/Multi-Detect/artifacts/benchmarks/flight-identity-eval-v2-v3prop-r12-fast.json
```

`--source-root` 是 YOLOE 缓存键的根，必须等于保存 episodes 的仓库根（episodes 在 `D:/Projects/Multi-Detect/artifacts/...` 时就是 `D:/Projects/Multi-Detect`），这样缓存键才与 build_sft 查找的 `artifacts/<campaign>/...` 一致；省略时自动推断，填错（如 `.../artifacts` 或 episodes 根）会在 annotate 前直接报错，不能指向本 worktree。采集的场景、世界、地图和 DEM 必须位于运行用的 checkout 内（例如 `configs/scenarios/generated-r19` 与 `simulation/{gazebo/worlds,maps,terrain}`），collect 开始前会逐个构建 SIM stack 预检。默认候选框设备为 `cuda:0`，可用 `--annotate-device cuda:1` 覆盖。默认启用两个采集 worker、每个 world 两个 episode、每个 episode 400 步；可用 `--workers 1`、`--steps-per-episode N` 调整。

仿真一可用就执行上述命令。当前约束下只允许先跑：

```powershell
python scripts/run_r15_round.py --stage all --dry-run --out .r15-dryrun
```

`--dry-run` 会打印每一个 world/episode 的完整 collector 命令、所有后续命令、耗时和磁盘预算，不调用 Docker/Gazebo、YOLOE、训练器或 llama-server。

## 阶段与断点

| 阶段 | 工作 | 预算估算 | 断点依据 |
|---|---|---:|---|
| collect | 20 个 world × 2 episode，400 steps，expert_search；2 worker，各自独立 container prefix 和端口；总磁盘/内存门控，失败最多重试 3 次 | 约 40–55 分钟：collection plan 要求从 14–16 m 经过 9/6/4.5 m 到 `<4 m` 的连续远近观测，末段至少 80 帧；按 20 FPS 采用 4 个约 80 帧观测区间（共 400 步、约 20 秒），覆盖最远、三个中间距离和末段近距。collector 注释为 0.15–0.17 sim s/墙钟 s，2 worker 并行；约 7.5 GiB | `round-state.json` 中每个 `world:index`；episode.json SHA-256 |
| annotate | YOLOE v3 默认候选框，`cuda:0` cache，跳过已存在 image key | 约 20–35 分钟：`04_r3_cache.log` 6462 帧从 00:46:47 到 00:51:10（约4分23秒），GPU 历史口径约20分钟；按 44,800 帧折算并留 IO/启动余量 | cache JSONL 为追加文件，脚本按 image key 去重 |
| build_sft | 在 r12 train/val/test 上加入新行，candidate_zoom_v2、成对难反例、五种尺寸层；不复制正样本 | 约 1–3 小时；新增 JSONL 通常 <2 GiB | additions manifest；合并前后统计门禁 |
| train | 默认从 r12 LoRA adapter 热启动，r15 新数据 + r12 训练集 25% 分层 replay，1 epoch；rank 16、alpha 32、accumulation 4；已有 checkpoint 自动 `--resume`；训练器负责 OOM 重试 | 约 1.5–3 小时：r12 日志 `8042.187 s / 2696 steps`，r14 日志 `7687.251 s / 2858 steps`，约 2.7–3.0 s/optimizer step；热启动不复用 optimizer 状态 | `adapter/checkpoint/state.json`、阶段 manifest |
| export | HF adapter merge，转换 F16 GGUF，再一次性量化 Q5_K_M；使用 `convert_slow_system_lora.py` 生成/执行本地转换计划 | 约 1–3 小时；至少 15 GiB 临时空间 | conversion plan 和 GGUF SHA-256 |
| eval | v1、v3prop（含 `size_buckets`）GPU `fast_identity`，以及规划评测 | v1+v3 目标 20–50 分钟（1,900+233 行，GPU）；规划约 1–2 小时 | 各 eval JSON 的 SHA-256 |
| report | 生成 `r15-report.md`，按五个尺寸层对比 r12/r15 | <1 分钟 | `report-manifest.json` |

总墙钟粗估为 **约 6–14 小时（通常小于 1 天，保守不超过 2 天）**，主要不确定项是规划评测和 simulator 启动/重试。按 10/25 截止，仍建议在 10/23 前恢复仿真并开始采集；本估算使用 r15 计划、`04_r3_cache.log`、collector lockstep 注释和 r12/r14 训练日志，不是本机本轮实测。

## 门禁与替换规则

SFT 合并会输出类别 × 尺寸 × 正/反例统计，并停止于以下任一异常：正样本占比相对 r12 偏差超过 2 个百分点，或合并后的干扰物负例数量少于 r12。内容指纹忽略 `sample_id`，所以“换 ID 复制正样本”也会被拒绝。

最终报告只有同时满足以下条件才写“建议替换 r12”：v3 集身份准确率不降；v3 的 `<12 px` 真目标召回至少提升 10 个百分点；v3 干扰物拒绝不低于 r12−2 个百分点；v1 整体准确率下降不超过 2 个百分点。原始评测 JSON 的 `size_buckets` 保留五层：`<12`、`12-16`、`16-20`、`20-32`、`>=32`。

## 监控与中断恢复

观察 `round-state.json`、`manifests/`、collector stdout/stderr、剩余磁盘和可用内存。中断后重新执行同一命令即可：已完成 episode、已完成阶段和哈希未变化的输出自动跳过；输出缺失或哈希变化会重新执行该阶段。阶段失败会记录退出码并停止后续阶段，修复原因后从该阶段重跑。不要手工删除 checkpoint；训练器会从 `checkpoint/state.json` 继续。

所有真实阶段都应在有足够空间的非 Docker 环境中执行。当前工作区没有主仓库 `artifacts/datasets` 和 `artifacts/models`，因此 dry-run 可验证命令编排，真实运行前必须通过参数指向只读的 Multi-Detect artifacts 根目录。
