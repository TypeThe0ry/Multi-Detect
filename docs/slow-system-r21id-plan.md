# 慢模型 r21-identity 难例补采计划

## 数据集布局

`r21-identity` 沿用 `scripts/generate_r15_worlds.py` 的 r20 profile 入口，生成 24 个世界：18 个 train、3 个 val、3 个 test。三类身份各 8 个世界；每类 6 train、1 val、1 test。test 世界和 test 类别组合只用于最终评测，不进入采集训练清单。每个世界固定一个 truth `target_id`，index 的 `view_geometry` 记录高度、地面距离、目标短边像素层、可见比例、遮挡类型和视角序列。

每类 8 个世界轮换以下几何层：相机高度 12/18/24/32 m，地面距离 12/20/30/40 m，短边 8--12、13--20、21--32 px。每个实例按 front、oblique、edge_truncated、recovery 四个观察位执行同一 target_id 的多视角序列；其中 edge_truncated 允许一次拒绝，recovery 必须重新观察同一实例。建筑、护栏和管线是遮挡标注类型，`electrical_box`/`cardboard_box`/`jersey_barrier` 为同世界近邻干扰物。

正负比例按调用帧计为约 3:1：每个世界四个序列观察位中三个位为正目标观察，一个位为拒绝或遮挡难例；每个世界同时保留一个近邻干扰物框。因此负例约 25%，并且正负来自同一世界和相近视角，避免模型学成过度拒绝。训练 split 只使用 18 个 train 世界；val/test 只在对应评测阶段加载。

## 生成与采集

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

## 评测门槛

* 实例级跨视角一致率：按 `target_id` 聚合四视角 verdict，>= 0.90；同一实例不得因一次 no_match 永久终止。
* 拒绝后恢复率：edge_truncated 后 recovery 再次接受同一实例的比例 >= 0.85，且平均恢复观察次数 <= 2。
* 真目标拒绝率：所有正目标调用的 false-reject <= 0.08；消防栓小目标层单独 <= 0.12。
* 干扰物接受率：显式 distractor 的 false-accept <= 0.026（拒绝率 >= 0.974），保持 r18 identity 门禁。
* test 世界只报告上述四项，不参与阈值调参；val 仅用于选择候选检查点。

## 与 r18 identity 对比

在相同的 r18 identity 评测脚本、相同正负判定和置信度阈值下，先冻结 r18 的 test 输出，再对 r21id test 世界按 `target_id` 对齐。报告总体和三类分层的跨视角一致率、恢复率、真目标拒绝率、干扰物接受率，并同时给出 95% bootstrap 置信区间。只有在干扰物拒绝率不低于 r18、且恢复率和一致率达到门槛时，才接受 r21id 的正例提升；不通过时保留 r18 作为发布基线。
