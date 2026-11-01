# 慢模型 r21-identity 难例补采计划

## 数据集布局

`r21-identity` 沿用 `scripts/generate_r15_worlds.py` 的 r20 profile 入口，生成 24 个世界：18 个 train、3 个 val、3 个 test。三类身份各 8 个世界；每类 6 train、1 val、1 test。test 世界和 test 类别组合只用于最终评测，不进入采集训练清单。每个世界固定一个 truth `target_id`，index 的 `view_geometry` 记录高度、地面距离、目标短边像素层、可见比例、遮挡类型和视角序列。

每类 8 个世界轮换以下几何层：相机高度 12/18/24/32 m，地面距离 12/20/30/40 m，短边 8--12、13--20、21--32 px。每个实例按 front、oblique、edge_truncated、recovery 四个观察位执行同一 target_id 的多视角序列；其中 edge_truncated 允许一次拒绝，recovery 必须重新观察同一实例。建筑、护栏和管线是遮挡标注类型，`electrical_box`/`cardboard_box`/`jersey_barrier` 为同世界近邻干扰物。

正负比例按调用帧计为约 3:1：每个世界四个序列观察位中三个位为正目标观察，一个位为拒绝或遮挡难例；每个世界同时保留一个近邻干扰物框。因此负例约 25%，并且正负来自同一世界和相近视角，避免模型学成过度拒绝。训练 split 只使用 18 个 train 世界；val/test 只在对应评测阶段加载。

## 生成与采集

`view_geometry` 描述相机观察目标所需的高度、距离、像素层和视角序列，不是 vehicle spawn pose。所有多旋翼在地面以约 0.2 m、pitch 0 起飞；任何相机俯仰、高度/巡航高度和观察航点都应通过任务/teacher 参数表达并被实际消费。若生成 fixed-wing 场景，必须在 scenario/runtime 显式声明可解析的 airframe 与 profile 对应起飞方式；RGB、depth 和 segmentation overlay 要符合该 runtime model 的 suite 合约。Round collect 在启动 worker 前对完整场景集做静态校验，配置错误汇总失败且不重试；重试只用于瞬时运行故障。

Docker daemon 不可用属于基础设施故障，不计入 episode 重试次数。collect 每次尝试前和失败后探测 daemon，默认最多等待 20 分钟、每 30 秒复查；可用 `--docker-wait-minutes` 调整等待时间。超时会将阶段记为 `docker_unavailable`、当前 episode 保持 `pending` 并以非零退出，恢复 Docker 后重跑 collect 会自动继续；采集器不会自动重启 Docker Desktop。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/generate_r15_worlds.py --profile r21-identity `
  --out-worlds simulation/gazebo/worlds `
  --out-scenarios configs/scenarios/generated-r21id
```

## r21id 结果与回退分析

本节依据 2026-10-27 的已落盘输出完成；未启动训练、GPU 推理、llama-server、SITL 或 Docker。逐行分析产物在 `artifacts/evaluation/r21id-analysis-20261027/analysis.json`。

| v3 指标 | r18 | r21id | 变化 |
|---|---:|---:|---:|
| identity accuracy | 0.893 | 0.785 | -0.108 |
| 正例身份召回（`confirm_target` 或 `approach_track`） | 0.786（92/117） | 0.897（105/117） | +13 行 |
| <12 px 正例召回 | 0.703（52/74） | 0.892（66/74） | +14 行 |
| distractor rejection | 1.000（116/116） | 0.672（78/116） | -38 行 |

逐行按 `task_query × truth_category × candidate_size_bucket` 对齐后，r18 拒绝而 r21id 接受的负例共 38 行，其中 35 行变为 `approach_track`、3 行变为 `confirm_target`。主组是 `Find the pickup` 对 SUV：18 行回退（16 行 approach、2 行 confirm）；另有 pickup 对 postbox 5 行、person 对 jersey_barrier 4 行、person 对 postbox 2 行、car 对 postbox 3 行和 car 对 None 2 行。另有一条正例（pickup、真值也是 pickup、20-32 px）从 confirm 降为 approach。

正例提升集中在小人物和小 pickup：`person` <12 px 从 14/20 提升至 20/20，20-32 px 从 1/6 到 5/6；pickup <12 px 从 5/6 到 6/6。SUV 大目标在 `Find the car` 的 >=32 桶从 3/6 提升到 5/6。提升以更愿意继续跟踪/approach 为主，并非更高的最终 confirm 能力；同时负例拒绝明显退化。

### 新增训练数据证据与判断

`r15-additions/train.jsonl` 有 622 条 `counterfactual` / `proposal_counterfactual` 正例：308 + 198 条 `approach_track`，59 + 57 条 `confirm_target`。506 条 approach 正例按视角为 front 178、edge_truncated 139、recovery 99、oblique 76、view 缺失 14；edge/recovery 合计占 approach 反事实正例的 47%。这确实反复强化了小/截断候选“先接近再看”，但不能单独证明模型对所有不确定目标一律接受，因为生成器同时支持 `--counterfactual-negatives` 配对拒绝标签。

可核查样例：`030364e2242ae11dade2edd9`（Find the dumpster，proposal_counterfactual/approach_track，edge_truncated，`...gen_r21id_w0017.../rgb/000033_before.jpg`）；`02bd92b4e19b46ecfb89570b`（Find the jersey barrier，proposal_counterfactual/approach_track，recovery，`...gen_r21id_w0017.../rgb/000048_after.jpg`）；`01312ac92bcd73d73596e1a6`（Find the person，proposal_counterfactual/approach_track，view 缺失，`...gen_r21id_w0015.../rgb/000001_before.jpg`）。

代码没有把 `suv` 和 `pickup` 声明为同类：`task_equivalent` 只比较类别相等或语义首词重合，`CATEGORY_ZH` 将二者分列；`_counterfactual_task` 遇到另一可见对象也满足任务时会放弃该反事实任务。因此 SUV→pickup 错误不是近义表显式合并导致。更合理的根因是有限新增集上的 1 epoch、`lr=1e-4` 热启动微调把决策边界推向正向/approach：新增集反事实正例中 81%（506/622）是 approach，并有 47% 来自 edge/recovery；这与 38 个负例回退相符，但不是消融实验已证明的唯一因果。

### r21id-r2 数据方案

1. 训练前剔除没有同帧同 task 明确身份拒绝配对的 `counterfactual` / `proposal_counterfactual` approach 正例；保留有严格配对的正例和尺寸足以确认的正例，不把重新命名后确实匹配的候选粗暴改成拒绝。
2. 增加同图成对难例：task=pickup、可见 SUV；task=SUV、可见 pickup；task=person、可见 jersey_barrier/postbox；以及 car/person/pickup 对 postbox 和空候选。错配候选标 `reject_track`，同任务真目标另给正例。
3. edge_truncated 正例 approach 占比限制在 20% 以下；recovery 只有同一真目标才允许 approach/confirm。edge 截断且类别不符或证据不足的候选必须保留 reject。
4. 保持完整 r18 replay，r21id 新增数据权重约 0.25，学习率由 `1e-4` 降至 `2e-5`，最多 1 epoch；val 同时监控小目标正例召回和 v3 distractor rejection，test 只做冻结后的最终评测。

本轮无需修改生成器或合并逻辑：已有 `counterfactual_negatives` 配对开关、去重和正负比例门禁；问题是错配类别覆盖不足和新增 approach 分布偏斜。

### r21id 四视角专项评测

`eval_slow_system.py --dataset` 接受 JSONL 文件路径并按行读取，不接受目录；`eval_r21id_identity.py` 用预测 `rows[].sample_id` 对齐并读取 `view` / `target_id`。已新增 `scripts/run_r21id_view_eval.py`，串行运行 r18、r21id 和汇总，端口分别为 9933、9934。先检查命令：

```powershell
python scripts/run_r21id_view_eval.py --dry-run
```

正式运行（本轮未执行）：

```powershell
python scripts/run_r21id_view_eval.py
```

结果写入 `artifacts/evaluation/r21id-analysis-20261027/view-eval/`，包括两个逐行预测 JSON 和 `identity-report.json/.md`。模型、mmproj、llama-server、`--identity-only --fast-identity --crop-policy candidate_zoom_v2` 参数与 r15 eval 阶段一致。

实际采集时，每个世界四个位姿序列各采 3 次，单次只运行一个 SITL 进程，并由锁脚本包裹：

```powershell
bash _scratch/sitl_lock.sh -- bash -lc "PYTHONPATH=src:. PYTHONUTF8=1 python scripts/run_r15_round.py collect --scenarios configs/scenarios/generated-r21id --r15-index configs/scenarios/generated-r21id/index.json --out <episodes> --world <world_id>"
```

24 世界 × 4 视角 × 3 重复约 288 个 episode。按约 90 秒/episode 的短难例架次估计，纯飞行与保存约 4.5--5 小时；考虑启动、校验和低速磁盘写入，预留 6 小时。该计划不要求在本轮启动 SITL、Docker、llama-server 或训练。

修复说明：先前生成器把相机观察高度/俯仰误写入 runtime spawn pose，令多旋翼出生在空中并倾斜，PX4 拒绝解锁；固定翼采集还会因缺少显式 airframe 而落到 x500 默认机型。重新生成时保留既有 world id、目标布局、holdout 与 seed，只调整出生配方、airframe 绑定和任务/teacher 观察意图。collector/teacher 必须读取这些观察意图，不允许只落入元数据。

每个多旋翼架次按 sidecar 的四个位姿序列执行 front、oblique、edge_truncated、recovery；每个观察位至少采 8 帧有效观察，edge_truncated 可记录遮挡/截断难例，recovery 必须回到同一实例。观察位高度须落在意图高度 ±3 m，目标 ground distance 落在对应 index `view_geometry` 档位 ±25%。`observation_waypoint` 到真目标 truth 坐标的水平距离是设计 ground distance；采集器不能在起飞点因为 `target_in_fov` 直接结束。

离线检查每个烟测 episode：状态 complete、每个位姿序列 ≥8 个有效帧、高度误差 ≤3 m、水平目标距离误差 ≤25%。有效帧要求有 `target_in_fov=true`、RGB 文件和同步姿态。执行示例：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/check_sim_teacher_smoke.py <episode-dir> --intent <world>.teacher_intent.json --truth <world>.truth.json --minimum-frames 8
```

启动 SITL 前的静态预检还必须确认每个出生点 15 m 半径无碰撞物，并且从贴地出生高度爬升到意图观察高度的垂直路径无障碍物。该检查只描述几何，不替代飞行后的高度、距离和帧数验收。

单架次 SITL 烟测命令（由后续排队者运行；一个多旋翼和一个固定翼各采 1 架次）：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 bash _scratch/sitl_lock.sh -- bash -lc 'python scripts/collect_sim_teacher_episodes.py --scenario configs/scenarios/generated-r21id/gen_r21id_w0000.scenario.json --airframe x500_mono_cam --episodes 1 --steps-per-episode 120 --out artifacts/smoke/r21id-multirotor'
PYTHONPATH=src:. PYTHONUTF8=1 bash _scratch/sitl_lock.sh -- bash -lc 'python scripts/collect_sim_teacher_episodes.py --scenario configs/scenarios/generated-r20/gen_r20_w0005.scenario.json --airframe rc_cessna_runway --takeoff-method runway --episodes 1 --steps-per-episode 120 --out artifacts/smoke/r21id-fixed-wing'
```

## 评测门槛

每个 r21id 世界的一个架次依次访问 front、oblique、edge_truncated、recovery 四个
观察位，每个位至少 8 帧；观察高度误差不超过 3 m，目标地面距离相对设计值不超过
25%。起飞前预检同时检查 15 m 出生净空、爬升廊道，以及观察位和相邻观察航段的
碰撞 footprint；观察位不能落在建筑、护栏、管线或干扰物内。

本轮冒烟命令（不并行运行 SITL）：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/run_r15_round.py collect --scenarios configs/scenarios/generated-r21id --r15-index configs/scenarios/generated-r21id/index.json --airframe-category multirotor --out D:/Projects/Multi-Detect/artifacts/smoke/r21id-multirotor --episodes D:/Projects/Multi-Detect/artifacts/r21id-episodes
python scripts/run_r15_round.py collect --scenarios configs/scenarios/generated-r20 --r15-index configs/scenarios/generated-r20/index.json --airframe-category fixed_wing --out D:/Projects/Multi-Detect/artifacts/smoke/r20-fixed-wing --episodes D:/Projects/Multi-Detect/artifacts/r20-episodes
```

冒烟时将 `--r15-index` 指向只含两个 r21id 世界（必须含 `gen_r21id_w0000`）的临时
index，固定翼 index 只含一个世界；两次 collect 共用 round-state.json，按 world key
合并完成状态。固定翼烟测通过 `scripts/check_sim_teacher_smoke.py` 时使用
`--minimum-frames 8`，其距离档位为 80 m orbit 对应的 55 m 目标视线设计值。

* 实例级跨视角一致率：按 `target_id` 聚合四视角 verdict，>= 0.90；同一实例不得因一次 no_match 永久终止。
* 拒绝后恢复率：edge_truncated 后 recovery 再次接受同一实例的比例 >= 0.85，且平均恢复观察次数 <= 2。
* 真目标拒绝率：所有正目标调用的 false-reject <= 0.08；消防栓小目标层单独 <= 0.12。
* 干扰物接受率：显式 distractor 的 false-accept <= 0.026（拒绝率 >= 0.974），保持 r18 identity 门禁。
* test 世界只报告上述四项，不参与阈值调参；val 仅用于选择候选检查点。

## 与 r18 identity 对比

在相同的 r18 identity 评测脚本、相同正负判定和置信度阈值下，先冻结 r18 的 test 输出，再对 r21id test 世界按 `target_id` 对齐。报告总体和三类分层的跨视角一致率、恢复率、真目标拒绝率、干扰物接受率，并同时给出 95% bootstrap 置信区间。只有在干扰物拒绝率不低于 r18、且恢复率和一致率达到门槛时，才接受 r21id 的正例提升；不通过时保留 r18 作为发布基线。

## r21id identity 离线评测用法

先用 `scripts/build_slow_system_sft.py` 生成 r21id 数据集，评测只读取其中的 `test.jsonl`。分别用冻结的 r18 与候选模型运行 `scripts/eval_slow_system.py`，保留其 JSON 输出（顶层 `rows`，每行含 `sample_id`、`expected`、`decision`）。评测脚本不加载模型、不做推理；它把预测行按 `sample_id` 与 test 行合并，从数据行读取 `view` 和 `target_id`。若这些字段放在 `teacher_intent.view`、`metadata.target_id` 中也可读取。缺少 `target_id` 会报错；`view: null` 的行会跳过并计数，四视角不全的实例也会在报告中单独计数。正式评测数据需在行中携带采集侧标注的视角和真目标 ID。

```powershell
python scripts/eval_r21id_identity.py `
  --dataset artifacts/r21id-round/pipeline/r15-additions/test.jsonl `
  --baseline artifacts/r21id-round/eval-v3-r18.json `
  --candidate artifacts/r21id-round/eval-v3-candidate.json `
  --output-json artifacts/r21id-round/identity-report.json `
  --output-md artifacts/r21id-round/identity-report.md
```

脚本按四视角完整的 `target_id` 统计实例级 verdict 一致率，按 `edge_truncated` 拒绝且 `recovery` 标注为正目标的实例统计恢复率和恢复观察次数；同时统计正目标总体/消防栓小目标 false-reject、显式 distractor false-accept。各比例提供以 `target_id` 为抽样单位的 95% bootstrap 区间，避免同一实例多行被当成独立样本。真目标拒绝率越低越好；候选干扰物接受率除满足绝对门槛外，也必须不高于 r18。r18 与候选必须覆盖相同 `target_id` 集合。JSON 与中文 Markdown 同时输出；候选触发任一门槛失败时退出码为 1，并在报告中明确“保留 r18”。test split 只用于最终报告，不应用于阈值调参。

## r21id-r2 可直接排队的执行方案

本轮使用 `artifacts/r18-round/r15-sft` 做完整 r18 replay，新增数据来自
`artifacts/r21id-r2/r15-additions-r2-final`。合并时显式打开 `--r21id-r2-filter`：
反事实 `approach_track` 必须有同一 `world + image_path/frame_id + task_text` 的
`reject_track` 配对；`edge_truncated` 的 approach 正例最多占该视角正例的 20%；
`recovery` 的 approach/confirm 只保留同一 `target_id` 的 true-target 配对；新增正例按
`world/label_type/decision/view` 分层约四分之一抽样，所有新增 reject 保留。该开关默认关闭，
所以旧轮次命令不变。合并统计和门禁结果落在 `artifacts/r21id-r2/merged/manifest.json`。

SFT 行本身不重复写 `truth_category`，但 builder 从 r21id truth 文件的 projected targets
读取类别，新增 `--confusable-negatives` 只处理 train/val，并保留 holdout 与去重逻辑。当前
落盘 additions 已生成 `confusable_negative`/`confusable_positive`。已有帧的方向计数如下（每个
方向目标为 30 才认为覆盖足够；低于 30 的方向仍需补采）：

| 混淆方向 | reject/paired 计数 | 状态 |
|---|---:|---|
| suv→car、pickup→car、postbox→car、mailbox→car、person→jersey_barrier、person→postbox、person→mailbox、jersey_barrier→person、postbox→person、mailbox→person | 30（builder 上限） | 达到当前覆盖目标 |
| person→mailbox、car→mailbox | 2 | 不足，需补采 |
| pickup↔suv、suv→pickup | 0 | 不足，需补采 |

hard-pair 统计严格排除 `flight-identity-eval-v2-v3prop` 和 generated-v2 holdout；当前 manifest
记录 `hard_pairs.available_pairs=3`（已有同帧正负配对），并将上述低覆盖方向标记为 supplementary
collection required。补采方案是生成 24 个 train 世界（pickup、SUV、person、postbox 四类各 6 个，另含
car 交叉干扰），沿用 `generate_r15_worlds.py --profile r21-identity` 的几何层；新增模板需
记录 `truth_category`、`candidate_category`、`target_id` 和同帧 `frame_id`。生成命令（只生成，
不采集）：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/generate_r15_worlds.py --profile r21-identity --count 24 `
  --out-worlds simulation/gazebo/worlds --out-scenarios configs/scenarios/generated-r21id-r2
```

采集排队命令（本轮不执行）应只使用 train index，按 24 世界各 4 视角、每视角 3 次收集，
并保持 `--holdout-from configs/scenarios/generated-v2/index.json`：

```powershell
python scripts/run_r15_round.py collect --scenarios configs/scenarios/generated-r21id-r2 `
  --r15-index configs/scenarios/generated-r21id-r2/index.json `
  --out artifacts/r21id-r2/episodes --episodes artifacts/r21id-r2/episodes
```

完整训练→导出→评测→报告队列（CPU 合并已经完成，训练命令仅供后续有 GPU 时执行；每条命令可续跑）：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/run_r15_round.py --stage train --out artifacts/r21id-r2 `
  --r12-dataset artifacts/r18-round/r15-sft --additions artifacts/r21id-r2/r15-additions-r2-final `
  --r12-replay-fraction 1.0 --r21id-r2-filter --learning-rate 2e-5 `
  --hot-start --r12-adapter artifacts/r18-round/adapter/adapter `
  --baseline-label r18 --candidate-label r21id-r2 `
  --holdout-from configs/scenarios/generated-v2/index.json
python scripts/run_r15_round.py --stage export --out artifacts/r21id-r2 --baseline-label r18 --candidate-label r21id-r2
python scripts/run_r15_round.py --stage eval --out artifacts/r21id-r2 --baseline-label r18 --candidate-label r21id-r2
python scripts/run_r15_round.py --stage report --out artifacts/r21id-r2 --baseline-label r18 --candidate-label r21id-r2
```

验收条件：v3 distractor rejection ≥ 0.97；pickup/SUV 成对拒绝 ≥ 0.97；v1 不低于 r18；
同时尽量保留小目标召回提升。任一条件失败则保留 r18。

## r21id-r2 结果（2026-10-28）

r21id-r2：r18 热启动，lr 2e-5，1 epoch；训练集 = 完整 r18 replay + 经 `--r21id-r2-filter` 过滤的 r21id additions + 228 条易混类别拒绝难例（`--confusable-negatives`）。报告 `artifacts/r21id-r2/r15-report.md`。

| 指标 | r18（机载） | r21id | r21id-r2 |
|---|---:|---:|---:|
| v1 身份准确率 | 0.922 | 0.951 | 0.932 |
| v1 干扰物拒绝 | 0.974 | 0.983 | 0.986 |
| v3 身份准确率 | 0.893 | 0.785 | 0.867 |
| v3 <12 px 召回 | 0.703 | 0.892 | 0.622 |
| v3 干扰物拒绝 | 1.000 | 0.672 | 1.000 |

结论：**不替换 r18**。易混难例与过滤修复了 r21id 的干扰物回退（v3 拒绝回到 1.0，v1 两项均优于 r18），但低学习率加 0.25 降权把小目标召回压到 r18 以下（-8.1 pp），v3 身份准确率 -2.6 pp。r21id 与 r21id-r2 分别站在“召回”和“拒绝”两端，说明新增数据的有效信号（小目标正例）与易混拒绝需要同时保留：下一轮应保留全部易混难例与过滤规则，但不再对 r21id 小目标真目标正例（`true_target`/`proposal_true`，<16 px）降权，学习率取 5e-5，并补采 pickup/SUV、person/postbox 真目标以形成严格配对（当前仅 3 对）。

## r21id 四视角专项评测（2026-10-28）

数据：`artifacts/r21id-round/pipeline/r15-additions/test.jsonl`（3 个 test 世界，358 行；49 行无 target_id 不计分，14 行 view=null，四视角齐全的实例仅 3 个，置信区间很宽）。输出：`artifacts/evaluation/r21id-analysis-20261027/view-eval/`。

| 指标 | r18 | r21id | r21id-r2 | 门槛 |
|---|---:|---:|---:|---:|
| 实例级跨视角一致率 | 0.50 | 0.25 | 0.50 | ≥ 0.90 |
| 拒绝后恢复率 | 0.67 | 1.00 | 1.00 | ≥ 0.85 |
| 真目标总体拒绝率 | 0.544 | 0.349 | 0.343 | ≤ 0.08 |
| 显式干扰物接受率 | 0.000 | 0.008 | 0.000 | ≤ 0.026 |

三个模型均未过门槛；现役 r18 在 edge_truncated/recovery 等难视角上拒绝了一半以上真目标，是当前身份确认最大的短板。r21id-r2 在四视角集上全面不劣于 r18（真目标拒绝 -20 pp，干扰物接受持平），但 v3 小目标召回下降，仍不替换。下一轮需要：更多 test 世界（≥ 8）使四视角结论具备统计意义；保持易混难例；不对小目标真目标正例降权。

## r21id-r3 worlds（生成与冒烟）

r21id-r3 使用 `scripts/generate_r15_worlds.py --profile r21id-r3`，固定 seed `20261028`，生成 72 个 world：56 train、4 val、12 test。真目标覆盖 pickup、suv、walking_person（person）、postbox、mailbox、hatchback（car）、fire_hydrant、backpack、construction_cone（traffic_cone）；每个 world 都带 `front/oblique/edge_truncated/recovery` 四视角 sidecar，edge/recovery 在几何层中占两档，测试目标只来自 `generated-v2/index.json` 的 frozen held-out combo。

| 项目 | 数值 |
|---|---:|
| worlds | 72 |
| train / val / test | 56 / 4 / 12 |
| 每类 true target | 8 |
| 四视角完整 sidecar | 72 / 72 |
| 严格配对方向（每方向 world 数） | 8 |
| 四视角 pair 估计（每方向） | 32 |

生成命令：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/generate_r15_worlds.py --profile r21id-r3 `
  --out-worlds simulation/gazebo/worlds `
  --out-scenarios configs/scenarios/generated-r21id-r3
```

静态检查使用 `run_r15_round.py --stage collect --dry-run` 的同一场景解析/preflight 路径；所有生成场景解析为 `x500_mono_cam`，出生高度 `<1 m`、pitch=0，teacher intent 观察高度和 waypoint 保持在 sidecar，未改动 release parity、schema 或 product version 文件。

合并过滤新增 `--r21id-keep-small-true-targets`。该选项只在 `--r21id-r2-filter` 下生效，保留短边 `<16 px` 的 `true_target`/`proposal_true` 正例；默认关闭，旧轮次行为不变。单测覆盖默认抽样和显式保留两条路径。

本轮 smoke 采用 SITL 锁串行执行，目标为一个 test pickup/SUV pair world 与一个 train world：

```bash
. /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire cxr3smoke
trap sitl_release EXIT
PYTHONPATH=src:. PYTHONUTF8=1 python scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/generated-r21id-r3/gen_r21id_r3_w0000.scenario.json \
  --airframe x500_mono_cam --episodes 1 --steps-per-episode 120 \
  --out artifacts/smoke/r21id-r3-test-pair
PYTHONPATH=src:. PYTHONUTF8=1 python scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/generated-r21id-r3/gen_r21id_r3_w0003.scenario.json \
  --airframe x500_mono_cam --episodes 1 --steps-per-episode 120 \
  --out artifacts/smoke/r21id-r3-train
sitl_release
```

每个完成 episode 用 `scripts/check_sim_teacher_smoke.py`，要求状态 complete、每个 view 至少 8 帧、记录 `observation_view` 且四个 view 全部出现。验收门槛沿用 r21id：四视角一致率 ≥0.90、edge 后 recovery 恢复率 ≥0.85、真目标 false-reject ≤0.08、显式 distractor false-accept ≤0.026。

完整采集后队列（本轮只生成与 smoke，不执行）：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/run_r15_round.py --stage collect --out artifacts/r21id-r3 --scenarios configs/scenarios/generated-r21id-r3 --r15-index configs/scenarios/generated-r21id-r3/index.json
python scripts/run_r15_round.py --stage annotate --out artifacts/r21id-r3 --r15-index configs/scenarios/generated-r21id-r3/index.json
python scripts/run_r15_round.py --stage build_sft --out artifacts/r21id-r3 --additions artifacts/r21id-r3/r15-additions --confusable-negatives
python scripts/run_r15_round.py --merge-only --out artifacts/r21id-r3 --r12-dataset artifacts/r18-round/r15-sft --additions artifacts/r21id-r3/r15-additions --merged artifacts/r21id-r3/merged --r21id-r2-filter --r21id-keep-small-true-targets
python scripts/run_r15_round.py --stage train --out artifacts/r21id-r3 --r12-dataset artifacts/r18-round/r15-sft --additions artifacts/r21id-r3/r15-additions --r12-replay-fraction 1.0 --r21id-r2-filter --r21id-keep-small-true-targets --learning-rate 5e-5 --hot-start --r12-adapter artifacts/r18-round/adapter/adapter
python scripts/run_r15_round.py --stage export --out artifacts/r21id-r3 --baseline-label r18 --candidate-label r21id-r3
python scripts/run_r15_round.py --stage eval --out artifacts/r21id-r3 --baseline-label r18 --candidate-label r21id-r3 --r12-baseline artifacts/r18-round/eval-v3.json --r12-v1-baseline artifacts/r18-round/eval-v1.json
python scripts/run_r15_round.py --stage report --out artifacts/r21id-r3 --baseline-label r18 --candidate-label r21id-r3 --r12-baseline artifacts/r18-round/eval-v3.json --r12-v1-baseline artifacts/r18-round/eval-v1.json
```

接受条件：test 四视角实例完整率 100%；四视角一致率 ≥0.90；recovery ≥0.85；真目标 false-reject ≤0.08；distractor false-accept ≤0.026；pickup↔suv、person↔postbox、car↔postbox 每方向至少 30 个四视角 pair 估计；任一门槛失败则保留 r18。

## r21id-r3 结果（2026-10-31，新开发机）

在新开发机上从 `--stage train` 接着跑：r18 热启动，lr 5e-5，1 epoch（5,711 步，GPU2 约 2.6 小时），类别留出已取消，小目标正样本保持全权重。导出 Q5_K_M 正常。

**主门第一部分：r3 冻结测试世界四视角评测**（12 个 test 世界，r18 与候选同一套，`artifacts/evaluation/r21id-r3-view-eval/identity-report.md`）：

| 指标 | r18 | r21id-r3 | 门槛 |
|---|---:|---:|---:|
| 实例级跨视角一致率 | 0.433 [0.267, 0.600] | 0.733 [0.567, 0.900] | ≥ 0.90 |
| 拒绝后恢复率 | 0.50 [0.17, 0.83] | 1.00 [1.00, 1.00] | ≥ 0.85 |
| 真目标总体拒绝率 | 0.346 [0.151, 0.572] | 0.086 [0.036, 0.157] | ≤ 0.08 |
| 显式干扰物接受率 | 0.130 [0.077, 0.183] | 0.094 [0.050, 0.145] | ≤ 0.026 |

r21id-r3 在每一项上都明显好于 r18（真目标误拒从 34.6% 降到 8.6%，跨视角一致率从 0.43 升到 0.73），但按本计划的绝对门槛，一致率、真目标拒绝率（差 0.6 个点）和干扰物接受率仍不达标。

**主门第二部分：真实飞行集 v1**。v1 评测集引用的 1,619 张图片（以及 v3 参考集的 94 张）原本只在旧 Windows 机器上，已由负责人复制到 NAS（`10_Projects/Drone/Multi-Detect-handoff-20261031/eval-images/`）并拉回，随后补跑 eval/report（`artifacts/r21id-r3/pipeline/r15-report.md`）。为排除新机器（CUDA 13.1 编译的 llama.cpp）带来的偏差，r18 在新机器上也重跑了 v1（`artifacts/evaluation/r21id-r3-view-eval/r18-v1-recheck.json`）：

| v1 指标 | r18（旧机器记录） | r18（新机器重跑） | r21id-r3 |
|---|---:|---:|---:|
| 身份准确率 | 0.922 | 0.913 | 0.946 |
| 干扰物拒绝率 | 0.974 | 0.958 | 0.993 |
| 确认召回 | 0.458 | 0.462 | 0.716 |
| 确认精度 | 0.950 | 0.928 | 0.990 |

r18 在新机器上比旧记录低约 1 个点（推理环境差异），同机对比时 r21id-r3 的优势更明显。v3（参考）：身份 0.892→0.918，干扰物拒绝都是 1.0，但 <12 px 小目标召回 0.703→0.622 下降。

结论：r21id-r3 在主门两部分都明显好于 r18，v1 没有退化；但四视角绝对门槛（一致率 ≥0.90、真目标误拒 ≤0.08、干扰物误收 ≤0.026）仍未全部通过，流水线报告也因 v3 小目标召回未提升而判“暂不替换”。**是否替换由负责人决定**（门槛是否改为“相对 r18 显著改进且 v1 不退化”）。下一轮方向：干扰物误收离门槛最远（0.094 对 0.026），继续补易混负例；同时盯住 v3 小目标召回。

## r21id-r4 设计

本节的拆分直接按 `eval_r21id_identity.py` 的计分口径重算：只保留有 `target_id` 且 `view` 属于四视角的行；目标框短边用测试 JSON 的归一化框乘以 1280×960 图像尺寸，尺寸档为 `<8`、`8-12`、`12-16`、`16-24`、`24-32`、`>=32` px。候选类别从对应 world 的 `truth.json` 通过 `target_id` 反查，避免把任务类别当成候选类别。

### 1. r21id-r3 错误集中

计分分母为显式干扰物 1,166 行、真目标 838 行、完整四视角实例 30 个。r18 与 r3 的错误数如下：

| 错误 | r18 | r21id-r3 | r3 变化 |
|---|---:|---:|---:|
| 干扰物误收 | 152/1166 (0.1304) | 110/1166 (0.0943) | -42 |
| 真目标误拒 | 290/838 (0.3461) | 72/838 (0.0859) | -218 |
| 四视角不一致实例 | 17/30 | 8/30 | -9 |

r3 的干扰物误收主要集中在以下任务类别→候选类别（括号内为 r3，后为 r18）：`pickup→suv` 42（41）、`suv→pickup` 38（25）、`postbox→car` 11（37）、`person→postbox` 7（46）、`suv→ambulance` 7（0）、`pickup→car` 5（3）。其中 `pickup→suv` 的 42 条按视角为 front 19、edge 13、oblique 7、recovery 3；`suv→pickup` 的 38 条为 oblique 13、front 11、edge 9、recovery 5。两组主要是大框：`pickup→suv` 的 42 条、`suv→pickup` 的 24 条落在 `>=32 px`；其余 suv→pickup 错误集中在 8-12、12-16、16-24 px 各 5、5、4 条。`postbox→car` 的 6 条在 24-32 px、5 条在 `>=32 px`；`person→postbox` 为 12-16 px 4 条、16-24 px 3 条。按 world，错误最高的是 w0002 的 pickup→suv 20 条、w0010 的 suv→pickup 19 条、w0001 的 pickup→suv 14 条、w0009 的 suv→pickup 14 条、w0026 的 postbox→car 10 条。

r3 真目标误拒的 72 条中，pickup 占 52 条、suv 13 条、postbox 5 条、person 2 条。pickup 误拒按视角为 edge 23、front 14、oblique 10、recovery 5；suv 为 edge 8、recovery 3，其余视角各 1。pickup 误拒有 46 条为 `>=32 px`，另有 5 条为 24-32 px；因此这不是只有小目标的问题，尤其是 w0000/w0001/w0002 三个 pickup world 合计 52 条。四视角不一致从 r18 的 17/30 降到 r3 的 8/30；逐实例不一致为：w0000 pickup 真目标 `>=32`（edge 被拒，其余接受）、w0001 pickup 真目标 `>=32`（oblique 被拒）、w0002 pickup 真目标 `>=32`（front/recovery 相反）、w0001 pickup→suv `>=32`（front/oblique 接受、edge/recovery 拒绝）、w0000 pickup→suv `>=32`（仅 oblique 误收）、w0010 suv→pickup `>=32`（仅 oblique 误收）、w0010 suv 真目标 `>=32`（仅 oblique 接受）、w0026 postbox 真目标 8-12 px（front 接受、edge 拒绝、其余 approach）。主要错误转移点是 edge 对真 pickup 的误拒和 oblique 对近邻 SUV/pickup 的误收。

### 2. 训练集对照

`r21id-r3/pipeline/r15-additions/train.jsonl` 有 19,381 行；合并后的 `r15-sft/train.jsonl` 有 22,843 行，其中 r18 replay 6,418 行、经过 `r21id-r2-filter` 的 additions 16,425 行。合并 manifest 记录的过滤为：无配对 659、恢复不匹配 939、edge approach 760、权重抽样 598；r21id-r3 另显式保留小真目标正例。

训练 JSONL 没有为所有历史行保存显式候选类别字段，因此不把从 task 文本/target_id 推断出来的全量正负数冒充精确配对统计。可审计的 `confusable_pair_type` 定向配对统计是：`pickup→suv` hard reject 0、`suv→pickup` hard reject 30、`postbox→car` hard reject 30、`person→postbox` hard reject 22；相同 pair family 的配对正例分别为 0、0、206、162。生成器的配对正例与拒绝行来自同一帧，说明目前最明确的缺口是 `pickup→suv` 拒绝方向；另外 person→postbox 拒绝量仅为 22。普通 distractor 与 counterfactual negative 仍有拒绝标签，但无法仅凭旧训练字段可靠地按具体候选类别计数。

`--confusable-negatives` 的拒绝行在部分视角偏斜；addition train 的 true/proposal true 行为 edge 951、recovery 701、front 642、oblique 522；distractor/proposal distractor 为 edge 1,676、front 1,409、oblique 1,013、recovery 1,009；confusable negative 为 edge 148、front 88、oblique 53、recovery 33。现有数据可以补齐视角和生成器标记的配对方向，但不能凭空增加某个 world 的新几何实例。

### 3. 只用现有采集数据的 r4 配方

1. **重采样现有定向 hard reject。** 保留配对正例，不做全 additions 降权；对现有的 `suv→pickup`（30）、`postbox→car`（30）、`person→postbox`（22）hard-negative 家族在四视角内重采样，目标每方向最多 60 条、每视角 15 条，视角不足时只在该视角内重复已有行。依据是 r3 的 `pickup↔suv` 合计 80/110 条误收，且 `postbox→car`、`person→postbox` 仍有 18 条误收。`pickup→suv` hard reject 当前为 0，不能从 JSONL 凭空构造，列入 SITL 新采集。
2. **按任务类别×正负决策做四视角平衡。** 使用新增的 `--balance-identity-views --max-upsample 2`，只复制已有 JSONL 行，不复制图片、不改变 test/val。实际在当前工作树重建的 train 为 13,344 行，新增 2,033 行；front 1,780→2,136、oblique 1,285→2,144、edge 2,089→2,120、recovery 1,377→2,164。该配方针对剩余 8/30 跨视角不一致和 edge/recovery 的真目标误拒，不会改变默认构建器输出。
3. **保留小目标正例全权重，避免再使用 r21id-r2 的整体 additions 降权。** r3 真目标误拒虽主要发生在大 pickup 框，但 postbox 的 5 条误拒包含 12-16/16-24 px，且 r21id-r2 已证明整体 down-weight 会把小目标召回降到 0.62；r3 的 `--r21id-keep-small-true-targets` 行为应继续保留。
4. **训练前的审计门。** 对生成 manifest 逐项检查四视角的正/负计数、`suv→pickup`、`postbox→car`、`person→postbox` 的拒绝行是否存在，确认新采集补上 `pickup→suv`，以及 `<16 px` true/proposal_true 是否没有被 merge 过滤掉；不满足则不进入训练。当前实际构建输出在 `.tmp/r21id-r4-sft/`，不写 artifacts。

### 4. 需要新 SITL 采集的数据

现有数据的 residual 主要来自少数固定 world，不能只靠复制同一帧证明跨视角稳定。建议新增 24 个 episode：`pickup` 6 个 world、`suv` 6 个、`postbox` 4 个、`person` 4 个、`ambulance` 4 个；每个 world 采集 front/oblique/edge_truncated/recovery 四视角，合计 96 组视角。pickup 与 SUV 各 6 个 world 强制同框出现两类候选并交换任务文本，优先覆盖目前缺失的 `pickup→suv` 拒绝方向；postbox 4 个 world 同时放置 car，person 4 个 world 同时放置 postbox，ambulance 4 个 world 作为 r3 新增 suv→ambulance 误收的对照负例。其中至少 4 个 pickup、4 个 suv、2 个 postbox world 把目标短边固定在 8-12/12-16 px，余下保持 24-32/`>=32 px`，以覆盖 r3 的小框边界和大框误收。新增 world 应优先复刻错误最高的几何条件：w0002/w0001 的 pickup、w0010/w0009 的 suv、w0026 的 postbox；每类至少两个新 world，避免只重复原 world 的纹理。

采集验收要求每个 episode 实际产生四个 view sidecar、观察帧数非零、目标距离/短边落在 index 设计档；不能以 episode `status=complete` 代替这些检查。新采集只用于 train/val，保留一组未见 world 作为 12 个 test world 的同类扩展评测，不能把新增视角直接混入冻结测试。

### 5. 构建器变更与实际构建

`scripts/build_slow_system_sft.py` 新增两个 opt-in 选项：`--balance-identity-views` 按 `task_category × (positive|negative) × view` 平衡，`--balance-confusable-negative-views` 按已有 hard-pair × view 平衡；默认均关闭，因此已有命令和输出保持不变。两者都在 train 选择完成后做确定性上采样，并在 manifest 写入每个 strata 的 original/added/final 数量；`tests/test_slow_system_sft.py` 增加了稀疏视角补齐、hard reject 专项补齐、忽略 ask_clarify 以及保留显式平衡副本的测试。实际命令使用 r21id-r3 已有 episodes、proposal cache、`--counterfactual-tasks --counterfactual-negatives --confusable-negatives --balance-identity-views --balance-confusable-negative-views --max-upsample 2`，输出 `.tmp/r21id-r4-sft/`：train 15,505 行、val 1,163 行、test 1,350 行；通用视角平衡新增 2,033 行，hard-pair 视角平衡新增 128 行。本次重建虽传入 proposal cache，但没有生成 `proposal_*` 标签行；因此行数只作为当前工作树可重放结果，不能替代 r21id-r3 原始 manifest。

最终分布：train 正例（confirm+approach）7,065、reject 8,046、search/clarify 394；核心标签为 distractor 4,554、true_target 2,131、counterfactual 3,198、counterfactual_negative 2,794、confusable_positive 1,736、confusable_negative 499。val 为正例 561、reject 594、其他 8；test 为正例 518、reject 777、其他 55。val/test 未执行两种平衡。

## r21id-r4 worlds

新增 24 个仿真世界，全部属于 train/val，不新增或改写 r21id-r3 的冻结 test。pickup/SUV 各 6 个世界同时放置两类车辆，并在 `task_text_variants` 记录交换后的两种任务文本；postbox 世界同框放 hatchback（car），person 世界同框放 postbox，ambulance 世界以“SUV”任务文本请求并放置真实 ambulance，形成 suv→ambulance 拒绝对照。像素短边按 catalog 的目标水平短边、相机内参 `fx=539.936` 和相机至目标斜距计算，估算值与档位均写入 index。

| 世界 ID | 类别/数量 | 同框干扰物 | 短边档位 | split |
|---|---|---|---|---|
| `gen_r21id_r4_w0000`–`w0005` | pickup ×6 | suv | 8–12 ×2、12–16 ×2、24–32 ×1、≥32 ×1 | train 5、val 1 |
| `gen_r21id_r4_w0006`–`w0011` | suv ×6 | pickup | 8–12 ×2、12–16 ×2、24–32 ×1、≥32 ×1 | train 4、val 2 |
| `gen_r21id_r4_w0012`–`w0015` | postbox ×4 | hatchback (car) | 8–12 ×1、12–16 ×1、24–32 ×1、≥32 ×1 | train 3、val 1 |
| `gen_r21id_r4_w0016`–`w0019` | person ×4 | postbox | 24–32 ×2、≥32 ×2 | train 3、val 1 |
| `gen_r21id_r4_w0020`–`w0023` | ambulance ×4 | suv；任务请求 SUV | 24–32 ×2、≥32 ×2 | train 3、val 1 |

pickup/SUV 各有 4 个小目标世界，postbox 有 2 个；pickup/SUV 几何参考 r3 的 w0002/w0001、w0010/w0009，高度配置沿用其视角档并使用不同外观；postbox 使用 w0026 的 morning-low-sun 外观条件，并按指定像素档反解距离。所有多旋翼出生点固定为 z=0.2 m、pitch=0；本组没有固定翼世界。

生成命令（离线，不启动 SITL）：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
python scripts/generate_r15_worlds.py --profile r21id-r4 \
  --out-worlds simulation/gazebo/worlds \
  --out-scenarios configs/scenarios/generated-r21id-r4
```

之后进行单世界 SITL 冒烟时可运行一个小目标 pickup 世界：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
python scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/generated-r21id-r4/gen_r21id_r4_w0000.scenario.json \
  --airframe x500_mono_cam --episodes 1 --steps-per-episode 120 \
  --out artifacts/smoke/r21id-r4-w0000
```

## r21id-r3 替换决定（2026-11-01）

负责人把决定交给开发侧。决定：**r21id-r3 替换 r18 成为机载身份模型**。理由：主门两部分（r3 冻结测试世界四视角、真实飞行集 v1）r21id-r3 都明显好于 r18，v1 在同一台机器上对比没有任何退化（身份 0.913→0.946、干扰物拒绝 0.958→0.993、确认召回 0.462→0.716）；四视角绝对门槛 r18 自己差得更远（真目标误拒 34.6%），继续以 r18 为现役等于保留更差的模型。已知短板照实记录：四视角干扰物误收 9.4%（门槛 2.6%）、v3 参考集 <12 px 小目标召回 0.703→0.622，由 r21id-r4 继续改进（新增 24 个世界的采集已在跑）。

模型：`artifacts/models/slow-system-lora-v2-r21id-r3-merged-gguf/slow-system-v2r21idr3-merged-Q5_K_M.gguf`（sha256 `4392eab6…`），adapter 在 `artifacts/models/slow-system-lora-v2-r21id-r3/`。推荐机载组合：`configs/onboard/sim-loop-v3-r21idr3-r24plan-defaults.json`（r21id-r3 身份 + r24 规划 + YOLOE v3 1280）。各回归套件配置等 Linux 上 SITL 任务套件跑通后再切换。下一轮 r21id-r4 以 r21id-r3 为热启动和基线。

## r21id-r4 流水线

`run_r15_round.py --stage build_sft` 先生成本轮 additions，再自动调用自身的 `--merge-only`：`--r12-dataset` 是上一轮合并集，作为 replay；本轮 additions 来自 `$OUT/r15-additions`；合并输出是 `$OUT/r15-sft`。因此 r4 使用 `--r12-dataset artifacts/r21id-r3/pipeline/r15-sft --r12-replay-fraction 1.0`，保留 r3 全部 replay。`--r12-baseline` 与 `--r12-v1-baseline` 只在 report 阶段读取，分别与本轮 v3/v1 结果比较；本轮标签显式设为 `r21id-r3` 和 `r21id-r4`。`--stage train` 通过 `--hot-start --r12-adapter` 从 r3 adapter 初始化。

runner 新增 `--confusable-negatives`、`--balance-identity-views`、`--balance-confusable-negative-views`、`--max-upsample`，仅在显式传入时透传给 `build_slow_system_sft.py`，默认行为不变。r4 显式打开 hard-negative 构建，但不启用会降权 additions 的 `--r21id-r2-filter`。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
OUT=artifacts/r21id-r4/pipeline
EPISODES=artifacts/r21id-r4c/episodes
INDEX=configs/scenarios/generated-r21id-r4/index.json
SCENARIOS=configs/scenarios/generated-r21id-r4
R3_DATA=artifacts/r21id-r3/pipeline/r15-sft
R3_ADAPTER=artifacts/models/slow-system-lora-v2-r21id-r3/adapter
BASE_MODEL=artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203
R3_V3=artifacts/r21id-r3/pipeline/eval-v3.json
R3_V1=artifacts/r21id-r3/pipeline/eval-v1.json

$PY scripts/run_r15_round.py --stage annotate --out "$OUT" --episodes "$EPISODES" \
  --scenarios "$SCENARIOS" --r15-index "$INDEX"
$PY scripts/run_r15_round.py --stage build_sft --out "$OUT" --episodes "$EPISODES" \
  --scenarios "$SCENARIOS" --r15-index "$INDEX" --r12-dataset "$R3_DATA" \
  --r12-replay-fraction 1.0 --confusable-negatives --balance-identity-views \
  --balance-confusable-negative-views --max-upsample 2
# build_sft 已自动合并；仅在 additions 已存在、需要单独重建合并集时运行：
$PY scripts/run_r15_round.py --merge-only --r12-dataset "$R3_DATA" \
  --additions "$OUT/r15-additions" --merged "$OUT/r15-sft" --r12-replay-fraction 1.0
$PY scripts/run_r15_round.py --stage train --out "$OUT" --model "$BASE_MODEL" \
  --r12-adapter "$R3_ADAPTER" --r12-replay-fraction 1.0 --hot-start --learning-rate 5e-5
$PY scripts/run_r15_round.py --stage export --out "$OUT" --model "$BASE_MODEL"
$PY scripts/run_r15_round.py --stage eval --out "$OUT" --data-root "$PWD" \
  --r12-baseline "$R3_V3" --r12-v1-baseline "$R3_V1" \
  --baseline-label r21id-r3 --candidate-label r21id-r4
$PY scripts/run_r15_round.py --stage report --out "$OUT" \
  --r12-baseline "$R3_V3" --r12-v1-baseline "$R3_V1" \
  --baseline-label r21id-r3 --candidate-label r21id-r4
```

四视角评测固定使用 r3 冻结测试世界 `artifacts/r21id-r3/pipeline/r15-additions/test.jsonl`。先用同一测试目录分别生成 r3/r4 预测，再用 `eval_r21id_identity.py` 按 `sample_id` 对齐，比较跨视角一致率、恢复率、真目标误拒和干扰物误收：

```bash
TEST_DIR=artifacts/r21id-r3/pipeline/r15-additions
TEST_JSONL=$TEST_DIR/test.jsonl
R3_GGUF=artifacts/models/slow-system-lora-v2-r21id-r3-merged-gguf/slow-system-v2r21idr3-merged-Q5_K_M.gguf
R4_GGUF=$OUT/merged-gguf/slow-system-Q5_K_M.gguf
MMPROJ=artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf
LLAMA_SERVER=artifacts/cache/llama-cpp/b11552/llama-server
VIEW_OUT=$OUT/four-view; mkdir -p "$VIEW_OUT"
for pair in "r21id-r3:$R3_GGUF" "r21id-r4:$R4_GGUF"; do
  label=${pair%%:*}; model=${pair#*:}
  $PY scripts/eval_slow_system.py --dataset "$TEST_DIR" --data-root "$PWD" \
    --backend gguf --device cuda:0 --identity-only --fast-identity \
    --crop-policy candidate_zoom_v2 --gguf-model "$model" --mmproj "$MMPROJ" \
    --llama-server "$LLAMA_SERVER" --output "$VIEW_OUT/$label-predictions.json"
done
$PY scripts/eval_r21id_identity.py --dataset "$TEST_JSONL" \
  --baseline "$VIEW_OUT/r21id-r3-predictions.json" \
  --candidate "$VIEW_OUT/r21id-r4-predictions.json" \
  --output-json "$VIEW_OUT/identity-report.json" \
  --output-md "$VIEW_OUT/identity-report.md"
```

2026-11-01 对现有采集目录做了不写入 artifacts 的 dry-run：

```bash
$PY scripts/run_r15_round.py --stage annotate --dry-run \
  --out .tmp/r21id-r4-pipeline-dryrun --episodes artifacts/r21id-r4c/episodes \
  --scenarios configs/scenarios/generated-r21id-r4 \
  --r15-index configs/scenarios/generated-r21id-r4/index.json
$PY scripts/run_r15_round.py --stage build_sft --dry-run \
  --out .tmp/r21id-r4-pipeline-dryrun --episodes artifacts/r21id-r4c/episodes \
  --scenarios configs/scenarios/generated-r21id-r4 \
  --r15-index configs/scenarios/generated-r21id-r4/index.json \
  --r12-dataset artifacts/r21id-r3/pipeline/r15-sft --r12-replay-fraction 1.0 \
  --confusable-negatives --balance-identity-views \
  --balance-confusable-negative-views --max-upsample 2
```

输出要点：annotate 发现现有 campaign `gen_r21id_r4_w0000`、`w0001`、`w0002`、`w0003`、`w0004`、`w0006`、`w0007`、`w0008`、`w0009`、`w0010`、`w0012`、`w0013`、`w0020`、`w0021`、`w0022`、`w0023`，并把 proposal cache 规划到 `.tmp/r21id-r4-pipeline-dryrun/yoloe-v3-cache.jsonl`；build 命令包含 `--confusable-negatives --balance-identity-views --balance-confusable-negative-views --max-upsample 2`，merge 命令包含 `--r12-dataset artifacts/r21id-r3/pipeline/r15-sft --r12-replay-fraction 1.0`。一次性 `--stage all --dry-run` 会在 train 前因临时目录没有真实 `r15-sft/manifest.json` 停止，这是 runner 对 train 输入的预期检查；dry-run 本身不会执行阶段或写入 `artifacts/r21id-r4c`。当前 `round-state.json` 仍有失败/未完成 episode，采集完成后再执行真实 build/train。

## 错误分层报告工具

`scripts/report_identity_errors.py` 读取 `eval_slow_system.py` 格式的预测 JSON（顶层 `rows`，也接受行数组），按 `sample_id` 与 test JSONL 对齐；既可只分析候选模型，也可同时比较基线与候选。报告按任务类别、候选类别、视角、候选框短边尺寸档和 world 分层，分别列出干扰物误收、真目标误拒的计分数、错误数和比率，并附候选相对基线的差值及 Top-N 错误组合。候选类别优先从 world 对应的 `truth.json` 按行 `target_id` 反查；任务类别从 truth 中的 `true_target` 获取，若数据行已有 `task_category`/`task_query` 则优先使用。真实飞行 v1 没有视角时会归入 `unknown`。r21id 四视角数据仅计入带 `target_id` 且 `view` 属于四视角的正例和显式干扰物行，与 `eval_r21id_identity.py` 的分母口径一致。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
python scripts/report_identity_errors.py \
  --dataset artifacts/r21id-r3/pipeline/r15-additions/test.jsonl \
  --baseline artifacts/evaluation/r21id-r3-view-eval/r18-predictions.json \
  --candidate artifacts/evaluation/r21id-r3-view-eval/r21id-r3-predictions.json \
  --output-json .tmp/r21id-r3-errors.json --output-md .tmp/r21id-r3-errors.md \
  --top-n 20

python scripts/report_identity_errors.py \
  --dataset artifacts/datasets/flight-identity-eval-v1/test.jsonl \
  --baseline artifacts/evaluation/r21id-r3-view-eval/r18-v1-recheck.json \
  --candidate artifacts/r21id-r3/pipeline/eval-v1.json \
  --output-json .tmp/v1-errors.json --output-md .tmp/v1-errors.md
```

world truth 默认从 `configs/scenarios/*/<world>.truth.json` 查找；输出目录由调用者指定，推荐使用 `.tmp/`，避免覆盖评测产物。测试：`python -m pytest tests/test_report_identity_errors.py -q -p no:xdist`。
