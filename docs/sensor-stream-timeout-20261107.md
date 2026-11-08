# Sensor stream 首帧等待超时调查（2026-11-07）

## 结论

`nightly-linux-r21idr3-r24plan-28-head` 的 `comms-free-count` 在第 81 步失败。失败发生于转向 primitive 已成功结束之后，collector 等待 `obs_after` 的新相机帧时。该等待以主机单调时钟计时，15.0 秒是墙钟预算。

这次不能证明 PX4 仿真时钟在这 15 秒内只是缓慢前进。最后一条保存的 PX4 `LOCAL_POSITION_NED` 遥测在 `316.484 s`，接近等待开始；它之后到失败快照之间没有保存到新的 PX4 时间。因此不满足“证据显示仿真时钟移动但只是较慢”这一改码前提。本次只补充调查文档，没有修改等待逻辑或增加测试。

## 证据与时间线

证据目录：`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/comms-free-count/campaign/gen_cnt2_w0000/episodes/episode-0000-33a49796/`。

- [collect_sim_teacher_episodes.py](../scripts/collect_sim_teacher_episodes.py) 的 `_fresh_sensor_snapshot` 先以 `time.monotonic() + timeout_s` 建立截止时间，循环中再次比较 `time.monotonic()`，超时后抛出本次异常。它没有读取 PX4 或 Gazebo 仿真时钟；所以 15.0 秒明确是墙钟。
- 调用点在 primitive 成功结束后，用 `primitive_end_ns` 作为主机接收时间水位，并用当前 pose 的仿真时间戳作为最低传感器时间戳，等待新的 `obs_after` 帧。Campaign 日志记录第 81 步 primitive 于单调时钟 `23423.345 s` 成功，随后进入该等待；异常栈确认抛出位置是 `after_sample = _fresh_sensor_snapshot(...)`。
- 第 80 步是已写入 `steps.jsonl` 的最后一步。其 after observation 的 PX4 `time_boot_ms=315912`，相机帧时间戳为 `315912000000 ns`；RGB、depth、segmentation 文件分别为 `rgb/000080_after.png`、`depth/000080_after.npy` 和 `seg/000080_after.png`。失败的第 81 步没有 after observation，因此没有对应步骤记录。
- `mavlink.jsonl` 最后一条 `LOCAL_POSITION_NED` 是 `time_boot_ms=316484`，接收墙钟 `1793996056.1216`。失败快照墙钟是 `1793996071.2056`，距末条遥测约 15.08 秒。记录显示 PX4 时间从最后一帧的 `315.912 s` 到最后一条遥测的 `316.484 s` 曾前进约 0.572 秒；但这段增长发生在等待期间的可观测窗口开始附近，之后约 15 秒没有新的 PX4 时间样本。故不能据此称时钟在整个等待期持续慢速前进。
- 失败快照时容器仍运行且 PX4 进程存活；当时主机 load average 为 `265.71, 274.75, 275.98`。这与机器负载很高一致，但不能单独证明负载造成了仿真冻结或相机停流。

## 帧流证据

帧服务端启动事件记录 RGB topic 为 `/world/default/model/x500_mono_cam_0/link/camera_link/sensor/camera/image`，depth topic 为 `/world/default/model/x500_mono_cam_0/link/depth_camera_link/sensor/depth_camera/depth_image`。启动日志有 `sensor_stream_first_frame` 事件，帧数为 1。episode 中各有 162 个 RGB、depth、segmentation 输出文件；最后一组是第 80 步的 after 帧，传感器时间为 `315.912 s`。

因此可以确认该 episode 曾经从相机帧流收到并解码帧，且 frame server 的响应流持续支撑了前 80 步。能确认的最后一帧是上述 `315.912 s` 的 after 帧。之后没有更晚帧文件。等待条件要求一帧同时满足“主机接收时间晚于 primitive 结束”和“传感器时间不早于 pose 水位”；现有记录不足以区分 frame server 没收到新相机消息、消息没有满足时间戳门槛、frame server/pipe 未送达，或仿真与渲染都停滞。

## 历史出现次数

对只读 `artifacts/benchmarks/` 按完整异常文本 `sensor stream produced no frame within 15.0s` 搜索：共找到 **2 个 episode 事件**。本次 28-head episode 是其中一个；此前的 nightly 产物中找到 **1 个**，位于 `nightly-linux-r21idr3-r24plan-8` 的 `patrol-fence-backpacks`、`gen_cnt2_w0003` episode（该 episode 记录 43 步、86 个 RGB/depth/segmentation 文件）。Campaign 日志和 episode.json 会重复同一事件，因此按 episode.json 事件计数，不把重复日志行算作独立故障。

这是共享 artifacts 当前可读内容中、按此精确异常文本得到的次数；它不证明更早但未保留、未命中此错误文本或没有 episode.json 的运行不存在同类故障。

## 已证明与未证明

已证明：超时预算是墙钟；故障发生在第 81 步 primitive 成功后的 `obs_after` 取帧等待；该 episode 的 RGB 相机帧流曾经产出帧，最后保存帧时间为 `315.912 s`；末条 PX4 遥测到 `316.484 s` 后，在失败快照前约 15 秒没有后续 PX4 时间记录；失败时 PX4 仍存活；历史精确文本命中 1 个更早 nightly episode。

未证明：PX4 时钟在等待期内持续慢速移动；相机 topic 在这段等待期是否继续发布；frame server 是否收到而丢弃了不满足时间戳条件的图像；高主机负载是否为直接根因；精确文本搜索未覆盖到的历史运行是否发生过同类故障。

由于决定性条件“仿真时钟在等待期间确实移动、只是较慢”没有得到支持，本次没有调整超时实现，也没有新增测试。
