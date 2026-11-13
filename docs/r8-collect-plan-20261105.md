# r21id-r8 数据采集计划（2026-11-05）

## 目的与边界

依据 [r7 数据审计](r7-data-audit-20261105.md)，r7 的 30 个逻辑架次中有 28 个合格，覆盖 14 个合格 world。`gen_r21id_r7_w0013` 的两个架次有几何和视角缺陷，不能合并。本计划选出一个替补 world，并为 `car->mailbox` 与 `suv->postbox` 各增加一个配对 world。清单只表示计划；本次没有生成场景、启动采集或写入 `artifacts/`。

r8 复用 `scripts/generate_r15_worlds.py` 的 `r21id-r8` profile 和现有两架次/world 的 episode key 格式。选择器为 `scripts/select_r21id_r8_collection.py`，会检查新 world ID 不与 r7 index 或仓库中 identity evaluation index 的 test world ID 重叠。固定 seed 为 `20261122`，world 前缀为 `gen_r21id_r8_w`。

## World 与 Episode 清单

| World | Seed | 目标 / 配对干扰物 | class pair | Episode keys | 用途 |
|---|---:|---|---|---|---|
| `gen_r21id_r8_w0000` | 20261122 | `male_visitor` / `jersey_barrier` | `person->jersey_barrier` | `:0`, `:1` | 替换缺陷的 w0013，补回一个有效 person world |
| `gen_r21id_r8_w0001` | 20261123 | `hatchback` / `mailbox` | `car->mailbox` | `:0`, `:1` | 补采缺少有效带视角拒绝帧的方向 |
| `gen_r21id_r8_w0002` | 20261124 | `suv` / `postbox` | `suv->postbox` | `:0`, `:1` | 增加该方向的配对样本及 recovery 视角机会 |

完整 episode key 列表：

```text
gen_r21id_r8_w0000:0
gen_r21id_r8_w0000:1
gen_r21id_r8_w0001:0
gen_r21id_r8_w0001:1
gen_r21id_r8_w0002:0
gen_r21id_r8_w0002:1
```

预期 episode 数按 class pair 统计：

| Class pair | 新增 world | 预期 episode |
|---|---:|---:|
| `person->jersey_barrier`（w0013 替补） | 1 | 2 |
| `car->mailbox` | 1 | 2 |
| `suv->postbox` | 1 | 2 |
| 合计 | 3 | 6 |

新增样本不代表专项门槛已满足。每方向仍须以实际采集审计确认至少 96 个独立拒绝帧及每个视角至少 8 个带 view 帧；特别是 `suv->postbox` 的 recovery 覆盖不能仅凭计划推定合格。

## 几何与不重叠约束

三个 world 均用生成器的 `identity_multiview` 路径、配对干扰物、设计短边档和四个观察视角（front、oblique、edge_truncated、recovery）。替补项采用与缺陷 `w0013` 相同的 `male_visitor` / `jersey_barrier` 方向，但使用新 ID、seed 和设计几何；car 项将目标设为 `hatchback`、配对物设为 `mailbox`；SUV 项使用 `suv` / `postbox`。三个条目均为 train split。

r8 使用新的 `gen_r21id_r8_w` 前缀，seed 区间为 `20261122-20261124`，不复用 r7 的任何 world ID。选择器还将已有 `generated-r21id*/index.json` 中标为 test 的 world 与 `generated-v2/index.json` 的 test world 纳入 ID 冲突检查。held-out 约束在此处针对 world/episode 身份，不改变本任务要求补足的 hard-reject class pair。

## 复现

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/select_r21id_r8_collection.py
```

命令输出包含 world、episode、固定 seed 和 class-pair 计数；可用 `--output PATH` 写出 JSON 计划文件。只检查生成器模板时，可运行以下 dry-run；不要将其误认为场景生成或采集：

```bash
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/generate_r15_worlds.py --profile r21id-r8 --dry-run
```

实际场景生成和 collection 不属于本计划执行范围。未来执行时，应为 r8 指定独立的场景/world 输出目录，并沿用每个 world 两个 episode 的现有 round 格式。
