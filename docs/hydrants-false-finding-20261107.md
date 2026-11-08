# 28-head 消防栓过计数复核（2026-11-07）

## 结论

`nightly-linux-r21idr3-r24plan-28-head/main/count-fire-hydrants` 的真值为 3，报告为 4，匹配 3，误检 1。评分中的误检是 **第三个 finding**：`frame_0088.png`、`track t14`、位置约 `(N 37.9 m, E -19.3 m)`。它与第一个 finding `frame_0016.png`、`track t4`、位置约 `(N 39.2 m, E -20.8 m)` 是同一个真值消防栓（真值 `count_member_0001_01`，约 `(37.883,-19.555)`），不是不同物体。第四个 finding `frame_0117.png`、`track t23`、约 `(-35.6,-31.6)` 才是第三枚真值消防栓。

根因是 **轨迹断裂后的空间去重半径略小，未能把同一目标的两个融合位置合并**。这不是慢模型把不同物体判成同类：`t4` 和 `t14` 都得到 `match`，而是 `t4` 在长时间离开视野后没有被重新识别为同一 `track_id`，随后新轨迹 `t14` 的位置估计与已计数位置相距略超过当前不确定度门限。

## 逐帧证据

真值来自只读场景文件 `configs/scenarios/generated-count-site/gen_cnt2_w0001.truth.json`；运行证据来自：

`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-fire-hydrants/campaign/gen_cnt2_w0001/episodes/episode-0000-6931667c/`

| finding | 轨迹与身份输出 | 融合位置 | 去重结果 |
| --- | --- | --- | --- |
| `frame_0016.png` | `t4`；step 13 `match/confirm_target`，置信度 `0.5411`；step 16 再次 `match/confirm_target`，`0.9472` | `(39.167,-20.818)`，σ `1.233 m`，5 视图 | 首次计数，`s1=1` |
| `frame_0024.png` | `t6`；step 19 `match/confirm_target`，`0.5559`；step 24 `match/confirm_target`，`0.8260` | `(8.0,-15.5)`，σ `1.230 m` | 另一枚目标，`s1=2` |
| `frame_0088.png` | 新轨迹 `t14`；step 61 `match/confirm_target`，`0.5585`；step 83 `match/approach_track`，`0.6015`；step 86 `0.6437`；step 87 `match/confirm_target`，`0.9834`；step 88 `0.9675` | `(37.936,-19.307)`，σ `1.229 m` | **未标记 `duplicate_of_counted`，错误新增，`s1=3`** |
| `frame_0117.png` | `t23`；step 115、116、117 均为 `match/approach_track`，置信度 `0.6388/0.5493/0.6334` | `(-35.581,-31.578)`（报告四舍五入为 `(-35.6,-31.6)`） | 新目标，`s1=4` |

`t4` 与 `t14` 的融合位置距离为约 **1.949 m**，两者合成 σ 为约 **1.741 m**。当时的 `count_dedupe_m=4.0` 仍受不确定度分支约束，实际比较半径为 `max(1.5 m, 1.5×footprint, min(4.0 m, 1.741 m)) = 1.741 m`；因此没有进入 `duplicate_of_counted` 分支。两条轨迹之间也没有保留下可用的共视关系来证明它们是不同物体。

从帧图可见，`frame_0016.png` 和 `frame_0088.png` 中都是场景右侧、建筑附近的同一红色消防栓；`frame_0117.png` 的目标位于场景西南侧，位置与第二个 count member `(-35.442,-31.800)` 一致。

## 是否为重访后的重识别失败

是，但表现分两层：

1. `t4` 在 step 16 后离开视野，后续再看到同一消防栓时建立了新轨迹 `t14`。这说明轨迹级 re-identification 没有把重访接回 `t4`。
2. 新轨迹的身份模型本身连续给出 `match`，所以最终问题不是“把目标拒成 `no_match`”，而是新轨迹确认后空间去重没有吸收它。换言之，这是“重识别失败后去重补偿不足”，不是一个不同对象被身份模型误认。

## 其他 nightly 对照

使用 `scripts/compare_nightly_arms.py` 对同一 `count-fire-hydrants` mission 汇总，并核对历史 `docs/nightly-review-28-head-20261107.md`：

| nightly | 真值 / 报告 / 匹配 | 误检 | 结果 |
| --- | ---: | ---: | --- |
| `r21idr3-r24plan-16` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-17` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-20` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-21-loggeroff` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-22-off45` | 3 / 2 / 2 | 0 | 早期少计；第三枚目标在回看中经历 `approach → no_match`，见 `docs/count-lower-bound-20261106.md` |
| `r21idr3-r24plan-23-def180` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-26-off45` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-27-off45-e2` | 3 / 3 / 3 | 0 | 精确计数 |
| `r21idr3-r24plan-28-head` | 3 / 4 / 3 | 1 | 本次过计 |
| `r21idr3-r24plan-29-head` | 尚未完成 | — | 没有可比的 mission report |

历史 review 将 28-head 的变化归为 `count-fire-hydrants: yes → no`、原因 `min_count_exact`；本次逐帧证据进一步确定了误检来自 `t14` 与 `t4` 的同物体重复计数。对照结果没有显示此前的 22-head 少计是由去重合并造成的；它是拒绝后身份恢复失败，故修复不能通过放宽拒绝来换取召回。

## 修复

在 `src/multidetect/autonomy/onboard_loop.py` 的计数去重半径中加入可配置的小校准余量：

- 新增 `LoopConfig.count_dedupe_sigma_factor`，默认 `1.15`；
- 不确定度分支由 `min(count_dedupe_m, sigma)` 改为 `min(count_dedupe_m, factor × sigma)`；
- 只影响已有确认之间的计数去重，不改变慢模型身份 verdict、轨迹关联、拒绝恢复或候选召回；
- 28-head 这两个估计的有效半径变为约 `2.00 m`，因此会合并为同一 finding；
- `count_dedupe_m=4.0` 仍是上限，且共视几何仍优先证明两个目标不同。

为避免把相邻真目标合并，新增回归测试覆盖 28-head 的 `(1.949 m, σ=1.233/1.229 m)` 情形；原有“相距 1.7 m 但 σ 很小的精确估计仍算两个”的测试继续通过。因此该改动不会降低早期少计 episode 的身份召回路径，也不会把已有的精确近邻双目标测试合并。

## 验证

已运行：

- `.venv/bin/python -m pytest tests/test_onboard_loop.py -q -p no:xdist`：**77 passed**；
- `.venv/bin/python -m pytest tests/test_onboard_ground_reid.py tests/test_score_onboard_loop.py tests/test_replay_count_position_stability.py -q -p no:xdist`：**55 passed**。

没有修改 `artifacts/` 共享产物，也没有重跑 SITL/nightly；本次结论基于只读证据和纯逻辑回归测试。
