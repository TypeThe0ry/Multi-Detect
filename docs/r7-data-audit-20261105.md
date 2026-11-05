# r21id-r7a 数据审计（2026-11-05）

本审计读取 `docs/slow-system-r21id-plan.md`、`artifacts/r21id-r7a`、`configs/scenarios/generated-r21id-r7`，并将 r7 与 `artifacts/r21id-r4d`、`artifacts/r21id-r4e`、`artifacts/r21id-r4g`、`artifacts/r21id-r5a` 的 episode RGB 做交叉检查。采集质量沿用 `scripts/audit_r21id_r4_collection.py` 的口径：每个视角至少 8 个 `phase=observe`、`target_in_fov=true` 且 RGB 存在的帧，目标地面距离在 index 设计值 ±25%，短边只核对 index 的设计 estimate。SFT 标签检查用 `.venv-vlm` 的 `scripts/build_slow_system_sft.py` 离线构建到临时目录，没有写入 `artifacts/`，没有启动训练、GPU、Docker、SITL 或新一轮采集。

## 结论

r7 可以作为下一轮的**补充数据**，不能作为完整采集轮次直接放行。30 个逻辑架次全部标记为 `complete`，但只有 28 个架次合格，覆盖 14/15 个 world。`gen_r21id_r7_w0013` 的两个架次都应排除：front、oblique、edge_truncated 没有有效观察，目标距离实测约 1.26 m，而 index 设计值为 17.0 m；其 `target_short_side_px_estimate=13.77` 也不在声明的 `24-32` 档位内。

r7 的三类 hard-reject 方向质量不均：person→jersey_barrier 的 train 拒绝帧已达到量级要求，SUV→postbox 仍缺 recovery 视角配额，car→mailbox 几乎没有带视角的有效拒绝帧。因此下一轮可以合并合格 r7 架次来增加场景和视角变化，但不能宣称三个方向都已完成专向覆盖，也不能用复制或无 view 行补足缺口。

## 采集质量与视角

| 方向 | 计划 world | 合格 world | 合格 episode | 合格短边档 episode 数（8-12 / 12-16 / 16-24 / 24-32） |
|---|---:|---:|---:|---|
| SUV / postbox | 5 | 5 | 10 | 2 / 4 / 2 / 2 |
| car / mailbox | 5 | 5 | 10 | 2 / 4 / 2 / 2 |
| person / jersey_barrier | 5 | 4 | 8 | 2 / 4 / 2 / 0 |
| 合计 | 15 | 14 | 28 | 6 / 12 / 6 / 4 |

合格的 28 个架次均有四个有效视角，单视角计数为 8 帧或更多；额外帧来自 teacher 在视角停留期间的重复观察。唯一的视角缺失集中在 `w0013:0`、`w0013:1`，两者均只有 recovery（8、9 帧）。round-state 没有逻辑失败项，但 episode artifact 中有 69 个失败尝试，另有 3 个失败 artifact 缺少 failure 字段；这些尝试不进入合并。失败以 PX4 simulation-clock/启动超时和 Docker 端口冲突为主，不能把它们当成有效标签数据。

## 身份平衡与子类型覆盖

r7 index 的 15 个 world 是三类 × 五个几何/外观档位。30 个 complete 架次保持每 world 两个架次，但剔除 w0013 后，`male_visitor` 子类型只剩 1 个 world、2 个 episode；`walking_person` 有 3 个 world、6 个 episode；SUV 有 5 个 world、10 个 episode；car 家族为 `hatchback` 2 个 world/4 个 episode、`hatchback_blue` 2 个 world/4 个 episode、`hatchback_red` 1 个 world/2 个 episode。

离线 builder（含 counterfactual、confusable 和 task-text variant 检查）处理了 868 个按 world 去重后的有效源帧，train/val 输出分别为 2,876/587 行，`conflicting_decision_groups=0`。视角元数据能可靠匹配的行仍不完整：train canonical `true_target`/`distractor` 行中 156 行没有 view，val 中 42 行没有 view；这些行可作为普通身份监督，但不能计入四视角配额。

| canonical hard reject | train 拒绝帧（已匹配 view） | train 缺 view | val 拒绝帧（已匹配 view） | 判断 |
|---|---:|---:|---:|---|
| SUV→postbox | 42（front 11 / oblique 11 / edge 13 / recovery 7） | 23 | 1 | recovery 少于每视角 8，未达专向门槛 |
| car→mailbox | 0 | 2 | 0 | 有效覆盖不足，必须补采 |
| person→jersey_barrier | 161（41 / 37 / 40 / 43） | 5 | 37 | train 方向已具备可用四视角覆盖 |

按计划的专向验收，每个方向需要至少 96 个独立拒绝帧，并且每个视角至少 8 个带 view 的帧。r7 只满足 person→jersey_barrier 的 train 方向；SUV→postbox 和 car→mailbox 应在后续补采/重建任务中继续排队。

## 标签缺陷检查

对 30 个 complete episode 的步骤和 sidecar 做了结构检查：

- episode task text 与 r7 index 一致：0 个不一致。
- 每个 truth 恰好一个 `true_target`：0 个异常。
- RGB、segmentation 引用缺失：0 个。
- labels 缺失：0 行；`labels.observation_view` 与步骤 `observation_view` 不一致：0 行。
- step index 重复：0 个 episode。
- 语义等价子类型没有生成错误拒绝：`walking_person/person`、`hatchback/car` 只保留正例或被等价性规则丢弃，没有生成 alias `confusable_negative`。builder 记录 `dropped_confusable_task_equivalent=743`，这是过滤数，不是可用标签数。
- 同一模型输入的决策冲突为 0。builder 内部去重删除 267 行重复模型输入，其中包括 variant/counterfactual 与原始 true-target 的重复，不应把这些行重新复制回来。

唯一需要修复的 r7 数据缺陷是 `w0013` 的几何/观察 intent 与实际采集不一致；它不是标签文本问题。该 world 的两个完整 artifact 也不能只截取 recovery 当作完整四视角 episode。

## 与历史 episode 的重复

对 r7 合格架次的 3,918 个 RGB 文件，与 r4d/r4e/r4g/r5a 的 11,342 个 RGB 文件逐字节 SHA-256 比较，跨轮 exact RGB duplicate 为 **0**；r7 内部路径重复也为 0。r7 world 前缀、seed 和场景 truth 均为新生成值，因此没有发现应从下一轮合并列表中按 exact frame 删除的历史重叠。感知 hash 的近似候选没有作为重复证据，因为天空、地面和遮挡背景会产生非独立的低信息碰撞；合并门禁仍以模型输入/源帧 exact 去重为准。

## 下一轮 merged list

下一轮保留既有 r6 输入基线，并追加 r7 的合格逻辑架次：

```text
artifacts/r21id-r4d/episodes
artifacts/r21id-r4e/episodes
artifacts/r21id-r4g/episodes
artifacts/r21id-r5a/episodes

r7 additions（round-state 的最终 complete path）：
gen_r21id_r7_w0000:0, :1
gen_r21id_r7_w0001:0, :1
gen_r21id_r7_w0002:0, :1
gen_r21id_r7_w0003:0, :1
gen_r21id_r7_w0004:0, :1
gen_r21id_r7_w0005:0, :1
gen_r21id_r7_w0006:0, :1
gen_r21id_r7_w0007:0, :1
gen_r21id_r7_w0008:0, :1
gen_r21id_r7_w0009:0, :1
gen_r21id_r7_w0010:0, :1
gen_r21id_r7_w0011:0, :1
gen_r21id_r7_w0012:0, :1
gen_r21id_r7_w0014:0, :1
```

也就是 r7 追加 14 个 world、28 个 complete episode。明确排除 `gen_r21id_r7_w0013:0` 和 `gen_r21id_r7_w0013:1`，以及所有 `attempt-*` 下的失败 artifact。不能直接把整个 `artifacts/r21id-r7a/episodes` 作为未过滤的 extra root 交给下一轮，否则 w0013 会被重新纳入；应按上面的 logical key/最终 round-state path 建立只读筛选清单，再与四个历史根目录合并。

合并后继续执行现有的语义等价过滤、exact model-input 去重和 view balance 检查。下一轮专向补采优先级为：car→mailbox（先保证 confuser 能入框并带 view）、SUV→postbox recovery、以及补齐 `male_visitor` 的有效四视角 world；w0013 需要先修正生成的 geometry/teacher intent，再重新采集。

## 执行记录

使用的 CPU-only 命令为：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/audit_r21id_r4_collection.py \
  --episodes-root artifacts/r21id-r7a/episodes \
  --round-state artifacts/r21id-r7a/pipeline/round-state.json \
  --index configs/scenarios/generated-r21id-r7/index.json \
  --scenarios configs/scenarios/generated-r21id-r7 \
  --minimum-frames 8
```

并用同一 venv 离线运行 `scripts/build_slow_system_sft.py` 做 segmentation/标签冲突、子类型等价性和内部重复检查；输出写在 `.tmp/`，没有修改只读 `artifacts/`。没有启动 round、训练、GPU、SITL、Docker 或联网下载。
