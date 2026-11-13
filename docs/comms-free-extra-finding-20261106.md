# `comms-free-count` 额外 finding 调查（2026-11-06）

## 结论

`nightly-linux-r21idr3-r24plan-20` 的第四个 finding 是 `track_id=t18`。它不是
新的物体，而是 `count_member_0000_01`（由 `t2` 先前计数）的重复观测；这次重复
的直接原因是 `t18` 的融合位置估计偏离了该锥体，超过了计数去重半径。当前证据
支持“一次性的感知/几何位置估计 miss”，没有足够证据证明机上去重或几何融合实现
存在可安全收窄的缺陷，因此没有修改计数判定、expected check 或
`artifacts/`。

## 回放方法

只读以下保存的 episode 和场景 truth，使用
`scripts/replay_count_position_stability.py` 重放记录中的确认事件。回放固定使用
已保存的轨迹、身份判断和位置字段，只重新检查当前相机射线门；不重新运行 detector、
VLM、动作规划或 track association，也没有启动 SITL。

异常 run：

```text
artifacts/benchmarks/nightly-linux-r21idr3-r24plan-20/main/comms-free-count/
  campaign/gen_cnt2_w0000/episodes/episode-0000-0339a6cb
```

对照 exact run：

```text
artifacts/benchmarks/nightly-linux-r21idr3-r24plan-16/main/comms-free-count/
  campaign/gen_cnt2_w0000/episodes/episode-0000-af55c5b6
```

场景真值来自 `configs/scenarios/generated-count-site/gen_cnt2_w0000.truth.json`。
回放命令为：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/replay_count_position_stability.py \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-20/main/comms-free-count/campaign/gen_cnt2_w0000/episodes/episode-0000-0339a6cb \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-16/main/comms-free-count/campaign/gen_cnt2_w0000/episodes/episode-0000-af55c5b6 \
  --truth /data/projects/Multi-Detect-env/Multi-Detect/configs/scenarios/generated-count-site/gen_cnt2_w0000.truth.json
```

两条 episode 的确认记录都通过当前相机 ray 和 position gate；因此额外 finding
不是 replay gate 造成的。

## `t18` 证据

`t18` 的类别是 `traffic_cone`，显示标签为 `traffic cone cone`。身份模型在
以下帧都给出 `match`：

| 帧 | 决策 | identity score |
| --- | --- | ---: |
| `frame_0023.png` | `confirm_target` | 0.6723017 |
| `frame_0024.png` | `approach_track` | 0.6904800 |
| `frame_0025.png` | `confirm_target` | 0.9900372 |

计数发生在 `frame_0025.png`，记录位置为 `[18.768, 18.323]`（N,E）。它的同一
track 地面锚点是 `[19.555, 23.604]`，报告位置相对锚点偏移 `5.340 m`。记录的
位置来源为 `fused`，含 3 个视角、`14.275 m` baseline、`22.615°` ray angle、
`3.092 m` sigma，并标记 `position_consistent=true`。

真值和已经计数的 `t2` 对照如下（单位 m）：

| 对象 | 位置 `[N,E]` | 到 `t18` 报告位置 |
| --- | --- | ---: |
| `target_true_gen_cnt2_w0000` | `[9.195, -15.402]` | 35.057 |
| `count_member_0000_01` | `[19.771, 23.886]` | **5.653** |
| `count_member_0000_02` | `[-21.995, 28.631]` | 42.046 |
| 已计数 `t2` | `[19.849, 23.728]` | **5.512** |

`t2` 的位置距 `count_member_0000_01` 只有 `0.176 m`，而 `t18` 是距离该真值
成员最近的 finding。因此第四条记录与第一个 count member 重合，既不是第三个
count member，也不是另一个场景物体。

## 为什么没有去重或跨 track 融合

计数去重比较的是确认记录里的 `position_estimate_ne`。`t18` 与 `t2` 的报告位置
相距 `5.512 m`，而 `count_dedupe_m=4.0 m`；按当前 sigma/footprint 规则，半径仍
受 4 m 上限约束，所以 `_counted_at` 不把它判为已计数对象。注意两个 track 的
地面锚点其实相距约 `0.319 m`，但设计上融合位置只用于确认报告和计数空间去重，
不会把融合位置写回 track 地面锚点；因此这次几何偏差直接暴露为额外 finding。

几何融合只融合**同一个 track**的地面来源和三角化来源，不能把 `t18` 与 `t2`
跨 track 合并。`t18` 的记录已经通过本次 track 内的融合一致性门，故没有
`fallback_ground` 或 `source_disagreement` 记录。它和 `t2` 也没有互相的共视边：
`t2` 的 `co_visible_with` 为 `t1,t3`，`t18` 的为 `t1,t15,t16,t5,t6`。所以共视
规则既没有证明两者是不同物体，也没有提供跨 track 合并依据；最终只能按两个
报告位置分别计数。

## exact run 对照

`r24plan-16` 的三个确认是 `t1/frame_0003.png`、`t8/frame_0008.png`、
`t3/frame_0014.png`，记录为 `3/3`，`false_findings=0`。同一 run 中 `t18` 只在
`frame_0052.png` 被判断为 `no_match`，score `0.9981988`，没有进入确认或计数。
其三个确认位置分别落在三个真值成员附近，未出现本次 `t18` 的 5.5 m 级位置偏移。

## 缺陷判断和改动

这次异常需要同时满足“身份判断为 match”和“融合位置落在去重圆外”；保存数据只
显示后者是单次几何估计异常，不能证明当前 4 m 去重门或同 track 融合实现错误。
直接扩大去重半径、改用地面锚点去重或强制拒绝这种融合，会改变附近真实目标的
区分语义，且没有足够的跨 run 证据支持。结论是一次 perception/geometry miss，
暂不做产品代码修复。

本次只增加回放诊断字段和回归测试：

- `scripts/replay_count_position_stability.py` 输出身份历史、track 地面锚点、融合
  元数据、真值成员距离、去重距离和共视信息。
- `tests/test_replay_count_position_stability.py` 固定验证 `t18` 被识别为
  `count_member_0000_01`、对应先前 `t2`，且 `5.512 m > 4.0 m` 时不会误判为
  已去重。

验证结果：

```text
pytest tests/test_replay_count_position_stability.py -q -p no:xdist
1 passed

python -m py_compile scripts/replay_count_position_stability.py
passed
```

