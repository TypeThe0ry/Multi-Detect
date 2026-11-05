# 2026-11-05 gimbal joint sidecar 回归归因

## 结论

**现有证据更支持单次运行波动和 GPU/主机并发负载，而不是 joint sidecar 已造成这两项回归。** 两组都是每个场景一次运行，且不是确定性 A/B：生成的 PX4/Gazebo episode、动作数和航迹都不同。joint 组的 joint 状态传输正常，帧率没有明显下降；llama 日志没有 OOM、CUDA 分配失败或请求错误。因此不能把 1/3 归因到 sidecar，也不能据此证明 sidecar 完全无性能或行为影响。用户提到的 GPU1 重叠运行使资源竞争成为合理解释，但产物不含 GPU/主机利用率与显存时序，无法证实是哪种资源竞争或它是否改变了模型判断。

## 对比事实

- nightly 总报告从无 sidecar 的 **3/3 expectations passed** 变为 joint 的 **1/3**。joint 的两个失败是 `gimbal-inspect-postbox` 的 `min_inspect_match_fraction` 和 `gimbal-count-cones` 的 `min_count_exact`；第三个 survey 任务通过。任务均正常结束，没有 campaign 异常。
- `inspect-postbox`：无 sidecar 的 inspect match fraction 为 1.0，最终目标框 IoU 0.916、位置误差 3.17 m；joint 组 inspect `target_not_seen`，所以 match fraction 为 null，最终目标框 IoU 0.898、位置误差 0.65 m。joint 在失败期间仍记录每步 1–3 个 detection，说明失败是 inspect 流程没有取得可匹配的目标观测，不是完全没有检测。它走了 26 步/115.8 m，无 sidecar 是 24 步/105.1 m。
- `count-cones`：无 sidecar 报告计数为 3、exact=true、false findings=0，三个计数位置误差为 0.42、1.08、0.31 m；joint 报告计数为 5、exact=false、false findings=2，真值仍为 3。joint 发现结果的位置误差为 37.41 m；无 sidecar 报告汇总位置误差为 40.25 m（其 count 位置误差仍较小，指标口径不同，不能直接视为同一对象的点对点偏差）。joint 飞行 524.9 m/130 步，无 sidecar 414.1 m/92 步，覆盖和重访轨迹明显不同。
- joint 帧服务端分别同步 3,833/3,834 inspect 帧与 12,876/12,877 count 帧；joint 状态无 invalid/missing/stale 样本。无 sidecar 帧流分别同步 3,671/3,672 与 9,942/9,944 帧。用首帧到 frame-server 结束事件的单调时钟跨度估算，帧流速率约为 inspect 7.7 fps 对 7.9 fps、count 5.9 fps 对 5.8 fps。采样率没有显示 joint 组的帧吞吐退化；frame server 统计不等于 GPU 推理帧率。
- inspect 身份模型 llama 日志的生成速度分别约为无 sidecar 43.47 tokens/s、joint 37.23 tokens/s；count 身份模型约为 46.51 与 43.72 tokens/s。没有 OOM/CUDA/分配错误。这个小幅下降可能符合并发负载，但不足以说明它导致了目标不可见或多计数。两组使用相同身份模型 SHA-256。
- joint episode 的 `propeller_joint_state` 出现在所有 26/26 个 inspect 步和 130/130 个 count 步；现有传输统计没有丢样或校验异常。它只是附加观测元数据。当前证据没有显示它改变了相机图像、检测结果或控制输入。

## 归因边界

较长的 joint count 航迹、多出的 38 个动作和更多重访为额外误检/计数漂移提供了 run-to-run 解释；postbox 的 inspect 失败也发生在 identity llama 请求成功、帧流未见退化的情况下。结合报告中的 GPU1 并发信息，主机/GPU 竞争与不同闭环轨迹比 sidecar 数据损坏更符合现有迹象。不过，单次、不配对的 3 个 episode 无法区分随机策略/场景差异、运行时资源竞争和 sidecar 元数据带来的轻微时序影响。

要证明因果关系，需要固定世界种子、模型/配置和控制条件，在独占 GPU 下交替重复 sidecar 开/关，并采集 GPU1 利用率/显存、主机负载、实际 RGB 推理吞吐及相同决策步的输入输出。现有产物没有这些控制量，也没有无 sidecar 与 sidecar 的逐帧成对比较；OOM 假说在本次日志中没有支持证据。

## 只读产物

- 无 sidecar 汇总：[nightly-report.json](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-9/nightly-report.json)
- joint 汇总：[nightly-report.json](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint/nightly-report.json)
- 两个失败任务的逐 episode 细节位于同目录 `gimbal/gimbal-inspect-postbox/report.json`、`gimbal/gimbal-count-cones/report.json`，事件日志位于各自 `campaign/` 下。`artifacts/` 是只读共享目录，本次没有改动。
