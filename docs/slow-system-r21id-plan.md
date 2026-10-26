# 慢模型 r21-identity 难例补采计划

## 数据集布局

`r21-identity` 沿用 `scripts/generate_r15_worlds.py` 的 r20 profile 入口，生成 24 个世界：18 个 train、3 个 val、3 个 test。三类身份各 8 个世界；每类 6 train、1 val、1 test。test 世界和 test 类别组合只用于最终评测，不进入采集训练清单。每个世界固定一个 truth `target_id`，index 的 `view_geometry` 记录高度、地面距离、目标短边像素层、可见比例、遮挡类型和视角序列。

每类 8 个世界轮换以下几何层：相机高度 12/18/24/32 m，地面距离 12/20/30/40 m，短边 8--12、13--20、21--32 px。每个实例按 front、oblique、edge_truncated、recovery 四个观察位执行同一 target_id 的多视角序列；其中 edge_truncated 允许一次拒绝，recovery 必须重新观察同一实例。建筑、护栏和管线是遮挡标注类型，`electrical_box`/`cardboard_box`/`jersey_barrier` 为同世界近邻干扰物。

正负比例按调用帧计为约 3:1：每个世界四个序列观察位中三个位为正目标观察，一个位为拒绝或遮挡难例；每个世界同时保留一个近邻干扰物框。因此负例约 25%，并且正负来自同一世界和相近视角，避免模型学成过度拒绝。训练 split 只使用 18 个 train 世界；val/test 只在对应评测阶段加载。

## 生成与采集

`view_geometry` 描述相机观察目标所需的高度、距离、像素层和视角序列，不是 vehicle spawn pose。所有多旋翼在地面以约 0.2 m、pitch 0 起飞；任何相机俯仰、高度/巡航高度和观察航点都应通过任务/teacher 参数表达并被实际消费。若生成 fixed-wing 场景，必须在 scenario/runtime 显式声明可解析的 airframe 与 profile 对应起飞方式；RGB、depth 和 segmentation overlay 要符合该 runtime model 的 suite 合约。Round collect 在启动 worker 前对完整场景集做静态校验，配置错误汇总失败且不重试；重试只用于瞬时运行故障。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/generate_r15_worlds.py --profile r21-identity `
  --out-worlds simulation/gazebo/worlds `
  --out-scenarios configs/scenarios/generated-r21id
```

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

先用 `scripts/build_slow_system_sft.py` 生成 r21id 数据集，评测只读取其中的 `test.jsonl`。分别用冻结的 r18 与候选模型运行 `scripts/eval_slow_system.py`，保留其 JSON 输出（顶层 `rows`，每行含 `sample_id`、`expected`、`decision`）。评测脚本不加载模型、不做推理；它把预测行按 `sample_id` 与 test 行合并，从数据行读取 `view` 和 `target_id`。若这些字段放在 `teacher_intent.view`、`metadata.target_id` 中也可读取。缺少任一字段会报错，不能用猜测补齐；当前通用 SFT builder 生成的普通行不自动推导四视角，正式评测数据需在行中携带采集侧标注的视角和真目标 ID。

```powershell
python scripts/eval_r21id_identity.py `
  --dataset artifacts/r21id-round/pipeline/r15-additions/test.jsonl `
  --baseline artifacts/r21id-round/eval-v3-r18.json `
  --candidate artifacts/r21id-round/eval-v3-candidate.json `
  --output-json artifacts/r21id-round/identity-report.json `
  --output-md artifacts/r21id-round/identity-report.md
```

脚本按四视角完整的 `target_id` 统计实例级 verdict 一致率，按 `edge_truncated` 拒绝且 `recovery` 标注为正目标的实例统计恢复率和恢复观察次数；同时统计正目标总体/消防栓小目标 false-reject、显式 distractor false-accept。各比例提供以 `target_id` 为抽样单位的 95% bootstrap 区间，避免同一实例多行被当成独立样本。真目标拒绝率越低越好；候选干扰物接受率除满足绝对门槛外，也必须不高于 r18。r18 与候选必须覆盖相同 `target_id` 集合。JSON 与中文 Markdown 同时输出；候选触发任一门槛失败时退出码为 1，并在报告中明确“保留 r18”。test split 只用于最终报告，不应用于阈值调参。
