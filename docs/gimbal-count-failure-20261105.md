# 2026-11-05 gimbal count 任务失败归因

## 结论

`nightly-linux-r21idr3-r24plan-gimbal-8` 的 `gimbal-count-cones` 失败原因是并发运行时 identity VLM 服务在 GPU 0 上 CUDA OOM，策略因此连续进入 `health_hold`，随后触发 `return_home`。count executor 没有获得一次可执行的图像帧，所以没有产生 count 完成事件、结构化 count findings 或 suite 所需的精确计数。suite 的 `skills` 和 `min_count_exact` 断言仍然正确；没有清楚、范围足够小且与产品语义一致的代码修复可在本次任务中安全提交。

## 计划是否不同

不是。两次任务使用相同的英文 prompt 和同一世界：

- gimbal 配置的 `task_text`、`zone:site` 参考和模型见 [`gimbal-count-cones/onboard.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/gimbal/gimbal-count-cones/onboard.json:2)。
- passing main 任务的配置也使用相同 `task_text`、`zone:site` 和相同 planning/identity 模型，见 [`main/count-cones/onboard.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-17/main/count-cones/onboard.json:2)。
- 两个 episode 的 `task_text` 都是同一个中文内部任务 `找到橙色交通锥并记录位置`，world 都是 `gen_cnt2_w0000`；见 gimbal [`episode.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-b61fbf49/episode.json:3) 和 main [`episode.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-17/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-74bc0024/episode.json:3)。
- gimbal 失败架次的第一条 `steps.jsonl` 仍记录了 `mission.plan.skills` 为 `count`, `return_home`；因此 planner 没有看到不同 prompt，也没有生成错误技能序列。suite 配置本身对该任务的期望也是 `count`, `return_home`，见 [`suite-gimbal-r21idr3-r24plan.json`](../configs/onboard/suite-gimbal-r21idr3-r24plan.json:38)。

main 的同世界对照确实完成了 count 和 return-home：其 [`mission_summary.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-17/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-74bc0024/mission_summary.json:1) 记录 `s1=count`、`s2=return_home` 均 succeeded，count 为 3；gimbal summary 则将两个步骤都记为 unknown，count 为 0 的 lower bound，且 `not_done` 为 `no_recorded_completion`，见 [`mission_summary.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-b61fbf49/mission_summary.json:1)。

## gimbal 是否让 executor 停止报告

gimbal 传感器本身不是直接原因。失败 episode 的观测仍为 `sensor_status: live`，有 `segmentation_info` 和 gimbal feedback；但前两步 labels 是 `state: health_hold`，第二步的 health reason 为 `slow_unavailable`，第三步变为 `decision: return_home`，见 [`steps.jsonl`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-b61fbf49/steps.jsonl:1)。这说明 executor 没有进入正常 count 状态，故没有 count report；不是 count executor 在完成后丢失报告。

对应的 identity 服务日志给出了直接故障证据：服务先在 `127.0.0.1:9942` 监听，随后在第一次请求处理时报告 `CUDA error: out of memory`，见 [`llama-server-_vfe2dpq.log`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episode-0000-b61fbf49-onboard-frames/mission_log/llama-server-_vfe2dpq.log:19)。planning 服务则成功完成一次请求，见同目录的 [`llama-server-0l15x8dj.log`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episode-0000-b61fbf49-onboard-frames/mission_log/llama-server-0l15x8dj.log:19)；因此计划已生成，失败发生在执行阶段需要 identity VLM 时。

这也解释了为什么 gimbal 的另外两个任务通过：同一 gimbal nightly 报告中，inspect 和 survey 都是 complete/mission_completed，只有 count 任务失败；见 [`nightly-report.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-8/nightly-report.json:35)。失败任务的 `complete=false`、`mission_completed=false`、失败断言为 `skills` 和 `min_count_exact`，而不是传感器、着陆或 telemetry 类失败，见同文件第 60 行起。

## suite expectation 是否错误

不是。`min_count_exact: 1` 要求至少产生一次 exact count 结果；gimbal summary 明确没有 recorded completion，只有 `value: 0`、`full_search_not_established`。将该结果改判为通过会把“VLM OOM 后返航”当成成功计数，掩盖真正的运行时故障。主 nightly 的同 prompt/同 world 对照也证明该 expectation 在正常运行下可满足：报告记录 `expected_count=3`、`reported_count=3`、`count_exact=true`，见 [`report.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-17/main/count-cones/report.json:39)。

## 修复决定

本次不改代码和 suite 断言。可见修复方向是 nightly 资源编排：避免 gimbal suite 的两个大型 llama 服务在 GPU 0 上与其他 worker 争抢显存，或为 identity 服务提供可验证的 CPU/fallback 路径；这属于运行编排/资源配置问题，不能仅凭一架 OOM 失败在任务范围内安全改变 executor 语义。没有修改 `artifacts/` 共享只读产物。
