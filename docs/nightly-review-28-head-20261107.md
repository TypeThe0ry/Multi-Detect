# 夜间回归复核：nightly-linux-r21idr3-r24plan-28-head

输入：nightly-linux-r21idr3-r24plan-27-off45-e2、nightly-linux-r21idr3-r24plan-28-head

## nightly-linux-r21idr3-r24plan-27-off45-e2

完成任务：14/14

| 套件 | 完成 | 任务数 | 预期通过 | 失败类 |
| --- | ---: | ---: | --- | --- |
| main | 14 | 14 | no | 无 |

| 套件/任务 | 预期通过 | 失败类 | 失败文本/事件 | expected/reported | false findings | 时长(s) | RTF | Accel/Gyro/MAG TIMEOUT | stall capture |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |
| budget-reserve-return | yes | none | — | 3/— | 0 | 788.239 | 0.184 | 0/0/0 | 0 |
| comms-free-count | yes | none | — | 3/3 | 0 | 1987.219 | 0.164 | 0/0/0 | 0 |
| comms-free-patrol-timeout | yes | none | — | 3/— | 0 | 780.714 | 0.218 | 0/0/0 | 0 |
| count-cones | no | none | TimeoutError: PX4 pre-arm health checks did not pass: xy_position_control; skills; min_count_exact | —/— | — | 2050.558 | 0.185 | 1/0/0 | 0 |
| count-fire-hydrants | yes | none | — | 3/3 | 0 | 1933.693 | 0.173 | 0/0/0 | 0 |
| english-find-person | yes | none | — | —/— | — | 317.996 | 0.079 | 0/0/0 | 0 |
| find-suv-land | yes | none | — | —/— | — | 254.743 | 0.131 | 0/0/0 | 0 |
| high-risk-monitor-link-loss | yes | none | — | 3/— | 0 | 698.519 | 0.217 | 0/0/0 | 0 |
| inspect-postbox | yes | none | — | —/— | — | 633.744 | 0.154 | 0/0/0 | 0 |
| monitor-backpacks | yes | none | — | 3/— | 0 | 2176.896 | 0.16 | 0/0/0 | 0 |
| operator-abort | yes | none | — | 3/— | 0 | 739.927 | 0.196 | 0/0/0 | 0 |
| operator-pause-resume | yes | none | — | 3/— | 0 | 1135.767 | 0.205 | 0/0/0 | 0 |
| patrol-fence-backpacks | yes | none | — | 3/— | 0 | 2796.697 | 0.173 | 0/0/0 | 0 |
| survey-site-overlap | yes | none | — | 3/— | 0 | 1452.523 | 0.199 | 0/0/0 | 0 |

## nightly-linux-r21idr3-r24plan-28-head

完成任务：17/17

| 套件 | 完成 | 任务数 | 预期通过 | 失败类 |
| --- | ---: | ---: | --- | --- |
| gimbal | 3 | 3 | no | sensor_stream_stalled |
| main | 14 | 14 | no | clock_stalled_alive, px4_alive_other_failure, sensor_stream_stalled |

| 套件/任务 | 预期通过 | 失败类 | 失败文本/事件 | expected/reported | false findings | 时长(s) | RTF | Accel/Gyro/MAG TIMEOUT | stall capture |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |
| budget-reserve-return | yes | none | — | 3/— | 0 | 952.666 | 0.154 | 0/0/0 | 0 |
| comms-free-count | no | sensor_stream_stalled | TimeoutError: sensor stream produced no frame within 15.0s; skills; supervisor.final_decision; supervisor.reason; supervisor.return_home_started; supervisor.onboard_log_exists; supervisor.logged_decisions; supervisor.logged_reasons; supervisor.original_mission_completed; supervisor.ground_event_count; min_count_exact | —/— | — | 1865.596 | 0.153 | 1/0/0 | 0 |
| comms-free-patrol-timeout | yes | none | — | 3/— | 0 | 1068.364 | 0.159 | 0/0/0 | 0 |
| count-cones | no | clock_stalled_alive | TimeoutError: PX4 simulation clock stalled during primitive; step=6; wall_timeout_s=45; skills; min_count_exact | —/— | — | 334.707 | 0.064 | 0/0/0 | 1 |
| count-fire-hydrants | no | none | min_count_exact | 3/4 | 1 | 2348.952 | 0.18 | 0/0/0 | 0 |
| english-find-person | yes | none | — | —/— | — | 331.712 | 0.084 | 0/0/0 | 0 |
| find-suv-land | yes | none | — | —/— | — | 337.382 | 0.144 | 0/0/0 | 0 |
| gimbal/gimbal-count-cones | no | none | min_count_exact | 3/2 | 0 | 4159.53 | 0.107 | 0/0/0 | 0 |
| gimbal/gimbal-inspect-postbox | yes | none | — | —/— | — | 769.769 | 0.113 | 0/0/0 | 0 |
| gimbal/gimbal-survey-site-overlap | no | sensor_stream_stalled | TimeoutError: sensor stream produced no frame within 15.0s; skills; min_survey_coverage | —/— | — | 2260.111 | 0.147 | 0/0/0 | 0 |
| high-risk-monitor-link-loss | yes | none | — | 3/— | 0 | 1025.253 | 0.152 | 0/0/0 | 0 |
| inspect-postbox | yes | none | — | —/— | — | 661.813 | 0.147 | 0/0/0 | 0 |
| monitor-backpacks | yes | none | — | 3/— | 0 | 1912.319 | 0.182 | 0/0/0 | 0 |
| operator-abort | no | px4_alive_other_failure | TimeoutError: episode exceeded its hard wall-clock ceiling (4800s); skills; supervisor.final_decision; supervisor.reason; supervisor.return_home_started; supervisor.onboard_log_exists; supervisor.logged_decisions; supervisor.logged_reasons; supervisor.original_mission_completed; supervisor.abort_hover_before_return | —/— | — | 4933.314 | 0.144 | 1/1/1 | 0 |
| operator-pause-resume | yes | none | — | 3/— | 0 | 1430.063 | 0.167 | 0/0/0 | 0 |
| patrol-fence-backpacks | yes | none | — | 3/— | 0 | 4175.023 | 0.113 | 0/0/0 | 0 |
| survey-site-overlap | yes | none | — | 3/— | 0 | 1810.718 | 0.171 | 0/0/0 | 0 |

## 与上一架次对比

| 任务 | 预期通过变化 | 原因 |
| --- | --- | --- |
| comms-free-count | yes → no | TimeoutError: sensor stream produced no frame within 15.0s; skills; supervisor.final_decision; supervisor.reason; supervisor.return_home_started; supervisor.onboard_log_exists; supervisor.logged_decisions; supervisor.logged_reasons; supervisor.original_mission_completed; supervisor.ground_event_count; min_count_exact |
| count-fire-hydrants | yes → no | min_count_exact |
| gimbal/gimbal-count-cones | — → no | min_count_exact |
| gimbal/gimbal-inspect-postbox | — → yes | — |
| gimbal/gimbal-survey-site-overlap | — → no | TimeoutError: sensor stream produced no frame within 15.0s; skills; min_survey_coverage |
| operator-abort | yes → no | TimeoutError: episode exceeded its hard wall-clock ceiling (4800s); skills; supervisor.final_decision; supervisor.reason; supervisor.return_home_started; supervisor.onboard_log_exists; supervisor.logged_decisions; supervisor.logged_reasons; supervisor.original_mission_completed; supervisor.abort_hover_before_return |
