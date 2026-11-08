# 多目标搜索与计数覆盖（2026-11-08）

## 结论

新增专项 suite 并完成静态配置核查、dry-run、相关 pytest 与 CPU-only onboard gate。SITL 未能启动：默认共享锁目录只读；改用本任务 `.tmp` 锁后，`docker ps` 因无法访问 Docker API 返回权限拒绝，SITL 锁按规则将容器状态视为未知并等待。为避免与可能存在的 SITL 作业并行，停止了本任务等待进程。没有产生可评分 episode。

因此本次只证明输入任务可解析、计数 truth/expected 通道相符、计划构造首步是 count，以及现有 scorer 按任务 skill 隔离 count 汇总。**不宣称已覆盖运行时遮挡/重访、track association、漏检重获或 exact count 验收。** `.tmp/multitarget-count-coverage-20261108/sitl/suite-report.json` 保留了两项基础设施失败记录，没有伪造 episode 或改写 expected。

## 配置与输入

专项配置为 `configs/onboard/suite-multitarget-count-coverage-20261108.json`，SHA-256：`10eb5565abbf798f0a0c2dc562566f91a929dbfc1337e414efbdd7cec829bbd2`。配置只选两个 count world，各 1 个 episode；未修改既有 suite、scenario、truth 或 expected。

| World / split | 任务 | expected | truth SHA-256 | expectations SHA-256 | runtime input SHA-256 | 最近异类目标 |
| --- | --- | ---: | --- | --- | --- | --- |
| `gen_cnt2_w0000` / test | 橙色交通锥计数 | 3 | `654930c6a756b551b2a980abf7cbc85d3823263c31e3d4d1a12a61942f4950ad` | `85ad897bea4cfd3b33818dae9d0d316b695b0a64685196a7914b8fa360f3818d` | `94710023e9c0b358475a501c3ad8032ab8880aedd6cdd23a59f16b65854fe261` | `distractor_0000_04` jersey barrier，距 `count_member_0000_02` 7.900 m |
| `gen_cfs_w0011` / train | 橙色交通锥计数 | 3 | `6ab8819b81106aaa020eb6fd58c50c1441b62a2d0e405ea98c40b04c96c41760` | `94d8c4c97ca076a33e8ede396a6fcc47f6d8f0753b9592d838c0244c691908db` | `d226cbcbfd58e846225e03a644d9d1b918b95c70e680a1370794397bb06f1838` | `distractor_0011_01` person，距 `target_true_gen_cfs_w0011` 5.807 m |

两组 scenario manifest 均通过项目 `load_scenario_index` / `load_verified_channel` 校验，manifest 中的 truth、expectations、runtime_input 引用 hash 均匹配。source manifest canonical hash 分别为 `fa50c55ebbe14fc63a6bd83ec7405534fa1653ce996152e9cf6340dbf13669bb` 与 `2dafd74f9e70bee73735fce08a11c858efd451bde69874adc696259d92e8165d`。两个 world 的三成员及 truth 位置（N,E，米）如下：

| World | Truth target IDs 与位置 |
| --- | --- |
| `gen_cnt2_w0000` | `target_true_gen_cnt2_w0000` `(9.195,-15.402)`；`count_member_0000_01` `(19.771,23.886)`；`count_member_0000_02` `(-21.995,28.631)` |
| `gen_cfs_w0011` | `target_true_gen_cfs_w0011` `(-7.298,10.684)`；`count_member_0011_01` `(16.199,-41.178)`；`count_member_0011_02` `(-4.109,-21.849)` |

任务预算为每 world `steps=150`、`max_episode_s=3600`，区域参考点 `(0,0)`、半径 55 m。采集器配置使用前向 RGB 相机 `1280x960` 和向下深度相机；预期技能为 `count → return_home`，只要求原有 `min_count_exact=1`。静态 `plan_for_count_task("orange traffic cone", region="zone:site")` 输出首步 `count`、末步 `return_home`。相机实际姿态、目标是否被部分遮挡及重访事件需 SITL episode 才能确认。

## 执行与逐目标状态

| World | episode | 逐目标 track / confirmation / fused location | 最终 count 与分类 |
| --- | --- | --- | --- |
| `gen_cnt2_w0000` | 未启动 | 无原始 episode，无法关联 track、confirmation 或 fused location | 未评分；duplicate、false finding、漏检/重获均未知 |
| `gen_cfs_w0011` | 未启动 | 无原始 episode，无法关联 track、confirmation 或 fused location | 未评分；duplicate、false finding、漏检/重获均未知 |

未观察到的 partial occlusion/revisit 不归类为感知或关联失败；这是**基础设施阻塞**，不能从静态几何推断运行时漏检。无 lower-bound episode 可保留；后续运行若出现 lower bound，应保留原始 episode 并按规划、感知、关联、几何或基础设施证据分类。

## 验证

- 静态配置验证：两条 suite mission 与对应 source index/world/split/task kind 对齐；各 truth 有 3 个 `count_member`；truth 与 expected 共享正确 scenario ID，所有通道 hash 验证通过。机器可读记录：`.tmp/multitarget-count-coverage-20261108/static-validation.jsonl`。
- dry-run：通过，输出两条单 episode 命令；SITL 脚本解释器均为 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`。记录：`.tmp/multitarget-count-coverage-20261108/dry-run.jsonl`。
- scorer 语义：`scripts/eval_onboard_loop_campaign.py::_is_count_task` 仅将含 `count` mission skill 的行纳入 count 汇总；survey/monitor 即便 truth 有 count members 也不计入。`tests/test_mission_suite.py` 等既有测试通过。
- 相关 pytest：`tests/test_mission_suite.py`、`tests/test_count_coverage_completion.py`、`tests/test_count_revisit_priority.py`、`tests/test_replay_track_association.py`、`tests/test_onboard_ground_reid.py`、`tests/test_score_onboard_loop.py`、`tests/test_onboard_loop.py`：通过，187 passed。
- CPU-only onboard gate：`/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_onboard_gate.py -q -p no:warnings`：PASS，116 秒。
- SITL：`MULTIDETECT_SITL_SLOTS=3`、`--workers 1`。第一次因 `/data/scratch/multidetect-sitl.lock.queue` 只读而未启动；第二次指定 `.tmp/multitarget-count-coverage-20261108/sitl.lock`，等待前置条件约 60 秒后因 Docker API permission denied 中止。没有访问槽位 3/4，没有完成 SITL episode；尝试与 dry-run 证据保留在 `.tmp/multitarget-count-coverage-20261108/`。

## 剩余缺口

需要在 Docker API 可用且 SITL 锁门禁确认空闲的环境中，依次运行两个配置各 1 个 episode，再检查 mission 首步、逐目标轨迹关联、confirmation/fused location、重复计数、近邻干扰误收、遮挡后重获与 exact/lower-bound。若两组配置没有产生可证实的部分遮挡和重访，需新增最小场景支持并重新 hash 输入。当前没有运行证据支撑目标级验收结论。

## 建议的下一个任务

1. **目标：**完成多目标搜索计数 SITL 覆盖。**证据路径：**`configs/onboard/suite-multitarget-count-coverage-20261108.json`、`configs/scenarios/generated-count-site/`、`configs/scenarios/generated-confusers/`、`.tmp/multitarget-count-coverage-20261108/`、`scripts/run_mission_suite.py`、`scripts/score_onboard_loop.py`。确认 Docker API 可用和项目 SITL 槽位空闲后，设置 `MULTIDETECT_SITL_SLOTS=3`，只用一个 worker、每次一个 episode，依次运行两个 world；禁止复跑 nightly、follow/track、climb-fault，禁止槽位 3/4、GPU 训练、模型服务、HIL、真机、联网下载和写 `artifacts/`。保留原始 episode，逐目标列出 truth/track/confirmation/fused position/final finding，对 duplicate、误收干扰、漏检/重获、exact/lower-bound 做证据分类。**写入：**必要的最小 suite/scenario/test 支持和中文验证报告；不得放宽 scorer/expected。**报告路径：**更新 `docs/multi-target-count-coverage-20261108.md`。

2. **目标：**若上述 SITL 证据未能在现有两个 world 中证明部分遮挡后的重访，构造一个最小的第三个生成场景来确定性地产生 3 个同类目标、近邻异类干扰物和一处建筑/植被部分遮挡路线。**证据路径：**`src/multidetect/simulator/world_gen.py`、`configs/scenarios/generated-confusers/`、`configs/scenarios/generated-confusers-2/`、`tests/test_mission_suite.py` 与 SITL 目标级报告。不要改既有 expected、scorer 判定、`configs/release-parity.json`、`docs/current-progress.json`、`configs/schemas/*.json`、`configs/product-version.json`，不要触碰 `artifacts/`。为新 world 增加真值/expected/hash 一致性和遮挡几何测试，并用一个 SITL worker/episode 验证重访。**写入：**最小 scenario/suite/test 支持及中文结果。**报告路径：**更新 `docs/multi-target-count-coverage-20261108.md`。
