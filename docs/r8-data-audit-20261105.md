# r21id-r8a 数据审计（2026-11-05）

本审计按 [r7 数据审计](r7-data-audit-20261105.md) 的口径读取 r8 round-state、episode artifacts、r8 index 与场景 truth/teacher intent，并用 `scripts/audit_r21id_r4_collection.py` 检查每集状态、四视角观察帧、地面距离和 index 短边档。训练标签统计沿用 `.venv-vlm` 的 `scripts/build_slow_system_sft.py`，离线输出写入 `.tmp/r8-audit/`。方向门槛按实际配对类别的 `distractor`/`reject_track` 源帧核算；同一 RGB 上的重复候选标签只算一张独立帧，view-balance 上采样不计入门槛。没有写入 `artifacts/`，没有启动 SITL、Docker、GPU 或训练。

## 结论

r8 的 6 个逻辑架次中，5 个最终状态为 `complete` 且通过几何与四视角审计；1 个逻辑架次失败。合格集为：

- `gen_r21id_r8_w0000:0`
- `gen_r21id_r8_w0000:1`
- `gen_r21id_r8_w0001:0`
- `gen_r21id_r8_w0001:1`
- `gen_r21id_r8_w0002:1`

`gen_r21id_r8_w0002:0` 共尝试 4 次仍失败，必须排除。r8 三个 world 都至少有一个合格架次；但这不代表三个方向均达到专项拒绝帧门槛。

`w0000` 替代了 r7 有缺陷的 `w0013`，person→jersey_barrier 的缺口已补上：排除 w0013 后，r7 与 r8 合并有 **211 张独立、带 view 的拒绝 RGB 帧**，front/oblique/edge_truncated/recovery 分别为 **66/52/46/47**，达到至少 96 张且每视角至少 8 张的门槛。该结论以独立 RGB 源帧计数；r7 原审计的 161 条行记录中，按 RGB 去重后为 154 张，r8 为 57 张，两轮之间 exact RGB 重复为 0。

**car→mailbox 和 suv→postbox 合并 r7 后都未达到门槛。** car→mailbox 没有可计入的配对 mailbox 带 view 拒绝帧；r8 新增的 car world 虽然几何和 episode 合格，但实际训练标签中没有配对 mailbox 拒绝样本。suv→postbox 的 r7 基数为 42 张（11/11/13/7），r8 仅增加 2 张独立带 view 拒绝 RGB（均为 oblique），合计 44 张（11/13/13/7），总量不足 96，recovery 也只有 7 张，低于每视角 8 张。

## 状态、重试与 episode 几何

round-state 共记录 6 个 key、累计 12 次尝试：5 个 complete 逻辑架次最终通过审计；`w0001:0` 经 4 次尝试后成功，`w0002:0` 的 4 次尝试全部失败。artifact 中共 7 个 failed attempt：2 个传感器 wire stream 提前结束、1 个等待起飞超时、4 个 PX4 仿真时钟停滞。采集日志 `runs/queue_r21id_r8_collect.out` 在此 worktree 与主仓库对应路径均不存在，因此无法从原始 queue log 复核调度细节；上面的状态与失败原因来自 round-state 和 episode artifacts。

| 架次 | round-state | 尝试数 | 有效视角帧（front / oblique / edge_truncated / recovery） | 设计距离 / 有效观察均值（m） | 短边档（设计 estimate） | 结果 |
|---|---|---:|---|---:|---|---|
| `w0000:0` | complete | 1 | 8 / 8 / 8 / 9 | 9.93 / 9.97 | 12–16（13 px） | 合格 |
| `w0000:1` | complete | 1 | 8 / 8 / 9 / 8 | 9.93 / 9.95 | 12–16（13 px） | 合格 |
| `w0001:0` | complete | 4 | 8 / 8 / 11 / 8 | 80.09 / 77.24 | 12–16（14 px） | 合格 |
| `w0001:1` | complete | 1 | 8 / 8 / 8 / 9 | 80.09 / 77.00 | 12–16（14 px） | 合格 |
| `w0002:0` | failed | 4 | — | — | 16–24（19 px） | 不合格；最终没有 complete path |
| `w0002:1` | complete | 1 | 8 / 8 / 8 / 8 | 69.99 / 67.43 | 16–24（19 px） | 合格 |

距离列采用既有 smoke audit：各预定视角至少 8 个 `phase=observe`、目标在视野内且 RGB 存在的帧，并逐视角核对目标地面距离设计值 ±25%。短边仅检查 index 中 estimate 是否落在声明档位；采集 steps 没有 bbox 像素值，故不是实测短边验收。表中 world 名省略共同前缀 `gen_r21id_r8_`。

## hard-reject 方向覆盖

统计只包含 train split 中真实配对方向的拒绝标签，按可匹配的 view 计数，并用 RGB 路径去重作为独立帧数。下表的 r7 计数来自 r7 审计；r7 的 `w0013` 不纳入。r8 的拒绝数从 unbalanced 离线构建结果核对，未把重复上采样行计入。

| 方向 | r7 独立带 view 拒绝帧（front / oblique / edge / recovery） | r8 独立带 view 拒绝帧（front / oblique / edge / recovery） | 合并帧数 | Gate |
|---|---:|---:|---:|---|
| person→jersey_barrier | 154（41 / 37 / 39 / 37） | 57（25 / 15 / 7 / 10） | 211（66 / 52 / 46 / 47） | **通过**：总数 ≥96，四视角均 ≥8 |
| car→mailbox | 0（0 / 0 / 0 / 0） | 0（0 / 0 / 0 / 0） | 0（0 / 0 / 0 / 0） | **未通过** |
| suv→postbox | 42（11 / 11 / 13 / 7） | 2（0 / 2 / 0 / 0） | 44（11 / 13 / 13 / 7） | **未通过**：总数不足，recovery 少 1 张 |

r7 原文的 person 方向是 161 条带 view 行（41/37/40/43）；本表按独立 RGB 去重后为 154 张，避免同一源帧多个候选标签重复累计。r8 person 方向按同样方法由 62 条配对拒绝标签行去重为 57 张，其中 edge_truncated 有 7 张、recovery 有 10 张。两轮合计每个视角仍高于配额。car 和 SUV 方向没有将同 world 内的其他类别干扰物误计为 mailbox/postbox。

## 离线 builder 交叉检查

按 r7 同一组 builder 开关，对 r8 最终 complete/失败 artifact 根目录只读扫描；builder 自行跳过 failed episodes，且仅把 round index 中的 train world 写入 train：

- r8：146 个有效 segmentation/RGB 源帧，train 849 行；exact model-input duplicate 去重 42 行，决策冲突组 0。
- r7 复核：868 个有效源帧，train/val 为 2,876/587 行，决策冲突组 0，与 r7 审计记载的构建结果一致。
- 按方向过滤后，r8 person 配对拒绝有 62 行、57 张独立 RGB；SUV→postbox 有 3 行、2 张独立 RGB，且都在 oblique；car→mailbox 为 0 行。任何 view-balance 扩增都未用于上述 gate 数字。

## 下一轮 merged list

下一轮应保留 r7 已有的四个历史 episode roots，并按逻辑 key 筛选追加 28 个 r7 合格架次及以下 5 个 r8 合格架次。r7 清单明确排除缺陷 `gen_r21id_r7_w0013:0`、`:1`；r8 排除失败的 `gen_r21id_r8_w0002:0` 和所有失败 attempt artifact。

既有 r6 输入基线：

```text
artifacts/r21id-r4d/episodes
artifacts/r21id-r4e/episodes
artifacts/r21id-r4g/episodes
artifacts/r21id-r5a/episodes
```

本次合并追加的完整 logical key 清单（r7 qualified + r8 qualified；明确不含 w0013）：

```text
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
gen_r21id_r8_w0000:0, :1
gen_r21id_r8_w0001:0, :1
gen_r21id_r8_w0002:1
```

因此建议清单共 **33 个 r7+r8 episode**：r7 28 个、r8 5 个，此外保留上面的四个历史 roots。按此筛选最终 round-state path，不能把 r7/r8 整个 `episodes` 目录未经筛选作为 extra root，否则会重新纳入 w0013 或失败 attempt。合并后继续执行既有的语义等价过滤、模型输入去重和 view balance；car→mailbox 及 suv→postbox 仍需补采。

## 执行记录

使用的 CPU-only 检查：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/audit_r21id_r4_collection.py \
  --episodes-root artifacts/r21id-r8a/episodes \
  --round-state artifacts/r21id-r8a/pipeline/round-state.json \
  --index configs/scenarios/generated-r21id-r8/index.json \
  --scenarios configs/scenarios/generated-r21id-r8 \
  --minimum-frames 8 --output-json .tmp/r8-audit/collection.json
```

随后用 `scripts/build_slow_system_sft.py` 的 r7 builder 开关分别离线构建 r8 与 r7 到 `.tmp/r8-audit/`，核对有效源帧、训练/验证行数、决策冲突与方向拒绝帧。未运行 pytest 或 onboard gate（本任务为只读数据审计和文档编写，无代码改动）；未启动 SITL、Docker、GPU 训练或新一轮采集，也未写入 `artifacts/`。

