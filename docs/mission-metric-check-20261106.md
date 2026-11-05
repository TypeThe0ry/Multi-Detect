# survey / monitor / count 任务指标核查（2026-11-06）

## 结论

`survey-site-overlap` 和 `monitor-backpacks` 的任务判据没有失败。nightly 的
`suite-report.json` 对 survey 读取 `skills`、`min_survey_coverage`，对 monitor 读取
`skills`、`min_alert_recall`；这些检查在有完整 episode 的运行中均为 `true`，且
`mission_completed=true`。因此不能用通用 `success_rate=0.0` 把这两类任务判为失败：
这个字段是旧的严格目标确认指标，要求 canonical true target 的确认位置和框 IoU；
survey 按设计不确认目标，monitor 的告警任务也不以该确认作为任务成功条件。monitor
还明确给出 `success_rate_task_semantics=1.0`、告警 precision/recall 为 `1.0`。

`count_exact_rate=0.0` 和 `mean_abs_count_error=3.0` 则是 reporting bug。count-site
真值同时给 survey/monitor 放了三个 `count_member`，scorer 原先只要发现
`expected_count` 就把这些非 count episode 纳入 summary；`reported_count=None` 被
按 0 参与误差计算，于是产生固定的 `3.0`。这两个字段对 survey/monitor 应为
`null`，而不是 0 和 3。修复只改变 campaign report 的 summary 统计范围：只有
`mission_plan_skills` 含 `count` 的 episode 才进入 count summary；没有放宽任何
expected check，也没有改写 `artifacts/`。

## 产物对照

固定相机 nightly `r24plan-{16,17,19,20,21-loggeroff}` 中，完整的
`survey-site-overlap` episode 都是 `mission_completed=true`、`survey_coverage=1.0`，
检查 `skills=true`、`min_survey_coverage=true`。完整的 `monitor-backpacks` episode
都是 `mission_completed=true`、告警精确率/召回率 `1.0`，检查 `skills=true`、
`min_alert_recall=true`。旧 report 的通用 summary 因而显示
`success_rate=0.0`、`count_exact_rate=0.0`、`mean_abs_count_error=3.0`；按修复后的
统计函数重算，survey 和 monitor 的两个 count 字段均为 `null`。

gimbal 的 `survey-site-overlap` 也有相同形状：例如
`nightly-linux-r21idr3-r24plan-gimbal-9`、`gimbal-joint`、`gimbal-joint2` 的 suite
检查均通过，mission coverage 为 `1.0`，但旧 summary 同样带出 `0.0/3.0`。这说明
问题在通用 scorer/report 汇总，而不是固定相机或云台任务执行本身。

## comms-free-count

`r24plan-16`、`17` 和 `21-loggeroff` 的 count report 是 exact `3/3`，期望检查
`min_count_exact=true`。`r24plan-20` 则确实失败：episode 记录
`expected_count=3`、`reported_count=4`、`matched_count=3`、`false_findings=1`，
所以 `count_exact=false`，suite 的 `min_count_exact=false`。第四个确认来自
`track_id=t18`，位置 `[18.8,18.3]`；前三个位置已经覆盖三个真值锥，其中第一个在
`[19.8,23.7]`，第四个与它相距约 5.7 m，超过 4 m 去重半径，遂被保留为额外发现。

这不是 `docs/gimbal-count-overcount-20261105.md` 所述的同一 stale-pose 原因。
该文归因是云台 `camera_pose` 缺失时错误回退到明确 stale 的 `gimbal_state`；
`r24plan-20` 是固定相机 episode，确认步骤没有 `camera_pose`/`gimbal_state` 字段，
但 `frame_sensor_stamp_ns == pose_sim_stamp_ns`、`sim_skew_ms=0`，且第四个位置是
有 3 个视角的 fused estimate。现有证据支持“重复 track / 几何估计导致的额外
发现”，不支持把它归因于云台 stale pose；本次不改 count 判定或 expected check。

## 修复与测试

修改 `scripts/eval_onboard_loop_campaign.py`：新增 `_is_count_task()` 和
`_count_summary()`，按 mission plan skill 过滤 count summary；修改
`tests/test_score_onboard_loop.py` 回归测试，覆盖 survey、monitor、count 混合和
全非 count 两种情况。

已运行：

```text
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  -m pytest tests/test_score_onboard_loop.py tests/test_mission_suite.py \
  -q -p no:xdist
60 passed

/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/run_onboard_gate.py -q -p no:warnings
onboard gate: PASS (149s)
```

未运行 SITL；未修改或重生成任何 evidence-bound 文件，`artifacts/` 保持只读。
