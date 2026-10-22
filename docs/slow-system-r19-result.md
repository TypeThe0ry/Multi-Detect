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
  --scenarios D:/Projects/Multi-Detect/artifacts/r19-round/scenarios \
  --r15-index D:/Projects/Multi-Detect/artifacts/r19-round/scenarios/index.json \
  --episodes D:/Projects/Multi-Detect/artifacts/r19-round/episodes \
  --source-root D:/Projects/Multi-Detect/artifacts \
  --holdout-from configs/scenarios/generated-v2/index.json \
  --r12-replay-fraction 1.0 \
  --r12-adapter D:/Projects/Multi-Detect/artifacts/models/slow-system-lora-v2-r12-multi/adapter
sitl_release
```

实际参数以 `python scripts/run_r15_round.py --help` 和 `round-state.json` 为准；训练器会从 checkpoint 自动续跑。训练 recipe 必须保持 r18：r12 adapter 热启动、1 epoch、LoRA rank 16/alpha 32、accumulation 4、learning rate `1e-4`、candidate_zoom_v2、全量 r12 replay；随后合并并量化 `Q5_K_M`，用 `fast_identity`、同一 v1/v3 数据集和设置评测。构建后必须核对 cache lookup 的真实候选数、正负配对数、每类/尺寸分布和 holdout 排除清单。

## 对比表（当前可用证据）

| 模型 | v1 身份准确率 | v1 干扰物拒绝 | v3 身份准确率 | v3 干扰物拒绝 | v1 真目标召回 | r19 状态 |
|---|---:|---:|---:|---:|---:|---|
| r12 | 92.8% | 94.9% | 78.5% | 90.5% | 90.4% | 基线 |
| r18 | 92.2% | 97.4% | 89.3% | 100% | 86.1% | 当前默认 |
| r19 | — | — | — | — | — | 未采集/未训练 |

world 与 task-class×candidate-class 的 r19 表格将在 `eval-v1.json`、`eval-v3.json` 及 mined JSONL 评测完成后补齐；在此之前不作“建议替换”结论。

本轮已完成的 r18 mined 交叉切片（候选正类的 TP/FN；负类的 TN/FP）如下，作为 r19 的 hard-case 验收基线：`背包→背包 44/63`、`交通锥→交通锥 149/13`、`消防栓→消防栓 200/27`、`邮筒→邮筒 24/5`、`SUV→SUV 49/7`；主要负类为 `背包→女性 54 TN`、`消防栓→背包 39 TN`、`消防栓→女性 45 TN`、`交通锥→救护车/纸箱/电箱/掀背车/隔离墩 32/26/10/35/40 TN`、`SUV→公交车 5 TN + 1 FP`。按世界的 mined call 数为 `gen_cnt2_w0000=305`、`gen_cnt2_w0001=311`、`gen_cnt2_w0003=188`、`gen_v2_w0035=80`、`gen_v2_w0038=34`、`gen_v2_w0039=4`；这些世界均是评测证据，r19 additions 必须保持为空集交集。
