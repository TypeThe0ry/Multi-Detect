# 慢模型 r19 轮次结果与续跑记录

## 当前结论

本轮尚未切换机上默认模型。r19 的采集、训练和 GGUF 评测没有在本次会话中完成，因此没有证据支持替换 r18。现有接受规则要求 v1 身份准确率相对 r12 不下降超过 2 个百分点、干扰物拒绝不下降、并同时检查 v3 小框召回和闭环结果；在 r19 结果补齐前按规则保留 r18。

## SITL hard-example mining

脚本 `scripts/mine_slow_hard_examples.py` 对 `mission-suite-*2026102*` 做只读扫描，输出在 `D:/Projects/Multi-Detect/artifacts/r19-round/sitl-hard-mining{,.jsonl,.md}`。共 12 个 benchmark 目录、6 个能由当前 truth 索引解析的世界、922 次 slow call；每条记录保留任务 query、verdict/decision、track bbox、原始 RGB frame 路径和 bbox crop 描述。truth 通过 `score_onboard_loop.truth_for_episode` 加载，候选类别按 `ground_ne` 最近目标匹配，计数成员视为目标。

| 分组 | n | TP | TN | FP | FN |
|---|---:|---:|---:|---:|---:|
| 总计 | 922 | 466 | 336 | 5 | 115 |
| 消防栓 | 227 | 200 | 0 | 0 | 27 |
| 交通锥 | 162 | 149 | 0 | 0 | 13 |
| 背包 | 146 | 44 | 39 | 0 | 63 |
| 邮筒 | 37 | 24 | 8 | 0 | 5 |
| SUV | 56 | 49 | 0 | 0 | 7 |

按距离：`<=8m` 为 59/12/0/5，`8-12m` 为 105/22/3/9，`12-17m` 为 78/33/0/35，`17-25m` 为 89/45/0/39，`>25m` 为 135/224/2/27（顺序均为 TP/TN/FP/FN）。按框短边：`<12px` 为 116/85/0/25，`12-16px` 为 137/74/3/22，`16-24px` 为 133/99/0/13，`>=24px` 为 80/78/2/55。主要缺陷与已知现场一致：12–25 m 的小消防栓/背包漏判，近距离大框也有漏判；误接受仅 5 次。

这些 mission-suite 世界（包括 `gen_cnt2_w0000..w0003`、`gen_v2_w0035/0038/0039`）只用于挖掘和评测，绝不进入 r19 训练。

## r19 数据与续跑命令

已生成隔离的 `gen_r19_w0000..w0019`（seed `20261022`，16 train/4 val/0 test）到 `D:/Projects/Multi-Detect/artifacts/r19-round/scenarios`，没有覆盖 `generated-r15`。生成器新增 `--world-prefix/--seed/--count/--resolved-root`，流水线采集阶段也按 index 的 world id 工作。新索引自身 `held_out_target_combos` 为空，构建 SFT 时必须继承 `configs/scenarios/generated-v2/index.json` 的 holdout。

采集尚未启动；dry-run 已验证 20 世界 × 4 episode × 120 step 的命令编排。续跑前先检查内存并取得共享锁：

```bash
. /d/Projects/mdwt/_scratch/sitl_lock.sh; sitl_acquire r19
export PYTHONPATH="src;."
python scripts/run_r15_round.py --stage all \
  --out D:/Projects/Multi-Detect/artifacts/r19-round/pipeline \
  --scenarios configs/scenarios/generated-r19 \
  --r15-index configs/scenarios/generated-r19/index.json \
  --episodes D:/Projects/Multi-Detect/artifacts/r19-round/episodes \
  --source-root D:/Projects/Multi-Detect \
  --holdout-from configs/scenarios/generated-v2/index.json \
  --r12-replay-fraction 1.0 \
  --r12-adapter D:/Projects/Multi-Detect/artifacts/models/slow-system-lora-v2-r12-multi/adapter
sitl_release
```

2026-10-23：首次流水线内采集失败（`scenario manifest escapes repository root`）。SIM stack 只接受运行 checkout 内的场景清单，且世界/地图/DEM 固定从 `simulation/gazebo/worlds`、`simulation/maps`、`simulation/terrain` 解析，而本轮场景在 `artifacts/r19-round/scenarios`、世界在 `artifacts/r19-round/worlds`（地图/DEM 被生成器写到了主仓库根的 `maps/`、`terrain/`）。现已把这 20 个世界按原字节提交为 `configs/scenarios/generated-r19` 与 `simulation/{gazebo/worlds,maps,terrain}/gen_r19_w*`，命令改用仓库内路径；collect 开始前逐个世界构建 SIM stack 预检。同时修正 annotate 只认 `gen_r15_w*` 目录、build_sft 只在历史 generated-* 目录找 truth 两处会让 r19 静默产出空数据的问题；`--source-root` 必须是保存 episodes 的仓库根 `D:/Projects/Multi-Detect`（填 `.../artifacts` 时缓存键与 build_sft 的 `artifacts/...` 帧路径对不上），现在填错会直接报错。

实际参数以 `python scripts/run_r15_round.py --help` 和 `round-state.json` 为准；训练器会从 checkpoint 自动续跑。训练 recipe 必须保持 r18：r12 adapter 热启动、1 epoch、LoRA rank 16/alpha 32、accumulation 4、learning rate `1e-4`、candidate_zoom_v2、全量 r12 replay；随后合并并量化 `Q5_K_M`，用 `fast_identity`、同一 v1/v3 数据集和设置评测。构建后必须核对 cache lookup 的真实候选数、正负配对数、每类/尺寸分布和 holdout 排除清单。

## 对比表（当前可用证据）

| 模型 | v1 身份准确率 | v1 干扰物拒绝 | v3 身份准确率 | v3 干扰物拒绝 | v1 真目标召回 | r19 状态 |
|---|---:|---:|---:|---:|---:|---|
| r12 | 92.8% | 94.9% | 78.5% | 90.5% | 90.4% | 基线 |
| r18 | 92.2% | 97.4% | 89.3% | 100% | 86.1% | 当前默认 |
| r19 | — | — | — | — | — | 未采集/未训练 |

world 与 task-class×candidate-class 的 r19 表格将在 `eval-v1.json`、`eval-v3.json` 及 mined JSONL 评测完成后补齐；在此之前不作“建议替换”结论。

本轮已完成的 r18 mined 交叉切片（候选正类的 TP/FN；负类的 TN/FP）如下，作为 r19 的 hard-case 验收基线：`背包→背包 44/63`、`交通锥→交通锥 149/13`、`消防栓→消防栓 200/27`、`邮筒→邮筒 24/5`、`SUV→SUV 49/7`；主要负类为 `背包→女性 54 TN`、`消防栓→背包 39 TN`、`消防栓→女性 45 TN`、`交通锥→救护车/纸箱/电箱/掀背车/隔离墩 32/26/10/35/40 TN`、`SUV→公交车 5 TN + 1 FP`。按世界的 mined call 数为 `gen_cnt2_w0000=305`、`gen_cnt2_w0001=311`、`gen_cnt2_w0003=188`、`gen_v2_w0035=80`、`gen_v2_w0038=34`、`gen_v2_w0039=4`；这些世界均是评测证据，r19 additions 必须保持为空集交集。

## 2026-10-23 训练与评测结果

流水线（低磁盘，`f4c6f31` 起续跑）：collect/annotate/build_sft 约 3 小时；merge_sft 门禁因 additions 中无配对反事实正样本偏多失败，改为分层剔除 114 条无配对反事实正样本后通过（正比例 0.410，基线 0.400）；训练 2 h 40 min（4685 行，1172 optimizer step，`.venv-vlm`）；导出 Q5_K_M 1.26 GB。评测前两次因 llama-server 默认 8 GiB 主机提示缓存把内存吃光后 CUDA 报 OOM，`--cache-ram 0` 后重跑（0 次重启、0 无效行）。

| 指标 | r12 | r18（机载身份模型） | r19 |
|---|---:|---:|---:|
| v1 身份准确率 | 0.928 | 0.922 | 0.891 |
| v1 干扰物拒绝率 | 0.950 | 0.974 | 0.881 |
| v1 match 精度 | 0.939 | 0.965 | 0.866 |
| v1 <12 px 真目标召回 | – | 0.598 | 0.683 |
| v3 身份准确率 | 0.785 | 0.893 | 0.931 |
| v3 <12 px 真目标召回 | 0.459 | 0.703 | 0.757 |
| planning-v3 完整序列正确率 | 0.4375 | 0.6125 | 0.6875（r14 机载规划 0.60） |

结论：**不替换机载身份模型 r18**。r19 在仿真集 v3 和小目标召回上更好，但真实飞行集 v1 的干扰物拒绝下降 9.3 个百分点（pickup 任务把 SUV 当目标 184/209→107/209 拒绝；锥桶任务把 jersey barrier 当目标 40/41→23/41；16–20 px 层 0.927→0.696），机载误确认风险上升。r19 的规划指标优于当前规划模型 r14，可作为规划候选，需先做 SIM 闭环验证。下一轮补 pickup/SUV、锥桶/路障的成对拒绝样本，以及固定翼倾斜高空视角下救护车/大巴/SUV/消防车的正负样本（见 fixed-wing-vtol-sim-flight.md）。

报告：`artifacts/r19-round/pipeline/r15-report.md`、`r19-vs-r18-r12.md`；评测 `eval-v1.json`、`eval-v3.json`（OOM 那次保留为 `eval-v1.oom-20261023.json`）。

### r19 作为规划模型的离线对比（2026-10-23）

用机载同一规划入口（`build_mission_from_text` + `labelled_refs`，b11552，temperature 0，strict json_schema，`--cache-ram 0`）对回归套件中实际走规划模型的 8 条任务和 3 条中文任务包 prompt 各跑 3 次（输出逐字节一致）：r14 与 r19 技能序列都是 11/11 正确，JSON 有效率 100%，单次规划 0.74–1.25 s。差异只在参数（inspect standoff 10/8 m、r14 多加 `max_count: 10`、find-suv-land 失败分支 r14 返航/r19 原地降落）。

15 条“返航”说法探测两者都对 12/15，但 r19 会把“到大门看一下，然后返航”和“到大门检查位置，然后返回起飞前指定的 home”规划成 `goto → land`（落在目的地），r14 在这两条上是多余的 inspect/area_search 后仍 `return_home`。planning-v3 中 r19 也有 3 例该返航未返航（r14 1 例），并接受了地图上不存在的地点。**结论：维持 r14 为机载规划模型。**下一轮规划数据补“到 X 做事然后返航/回 home/回起飞点”与误规划 `goto→land` 的配对拒绝样本，以及未知地点必须拒绝的样本。对比脚本与结果在 `_scratch/plannercmp/`。
