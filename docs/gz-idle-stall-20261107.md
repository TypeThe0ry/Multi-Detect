# Gazebo idle stall 调查（2026-11-07）

## 结论

28-head 当前只发现一次 PX4 clock stall：`count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f`，第 6 步，PX4 clock 停在 49.116 s。该 episode 使用 logger off。此前约 70 个 logger-off episode 未复现。本次证据能确认 PX4 与 Gazebo 进程仍存活、PX4 clock 不再推进，但不能确认 Gazebo 世界是否暂停，也不能区分 Gazebo 在等待渲染/传感器/GPU、lockstep 链路停止或其他内部等待。没有足够证据修复某个根因。

## Episode 证据

- `episode.json` 的 `status=failed`、`failure_class=clock_stalled_alive`；pre-arm `ready=true`，启动于新 episode `episode_index=0`，不是 resume 重跑。provenance 记录 logger `off`、PX4 daemon mode、Gazebo world `gen_cnt2_w0000.sdf`，PX4 host PID 90315。
- `steps.jsonl` 显示 step 0–5 primitive 成功。step 6 是 `translate`（forward 13.28 m）；开始后超过 45 s 未见 primitive 完成，collector 抛出 `PX4 simulation clock stalled`。最后 MAVLink `time_boot_ms=49116`，接收时间约 2026-11-07 03:08:18 UTC，与冻结的 49.116 s 一致。
- `px4-clock-stall-capture.json` 两次采样相隔 15.93 s，`gz topic -e ... /world/default/clock` 均未取得消息，分析结果是 `gz_clock_advanced=unknown`，不是确认 clock 没动。`gz topic -i` 在高负载下运行 3 s 后报无 clock publisher，但同时列出 PX4 subscriber；该探针会注册 transport node，单独不足以判定服务端状态。
- 两次采样间 gz 有 129 个线程，`on_cpu_delta` 合计约 0.105337 s；MAVLink 最后一批 attitude、position 和 servo 消息停在 49.116 s。PX4 的 Gazebo transport reception 线程在 `poll`，与没有 clock 消息相符，但缺少 gz 端栈，因此不能判断 clock 发布端为何不发。
- stall capture 时 host load average 约 `[232, 239, 224]`，load/core 约 2.3–2.5；container snapshot 记录 GPU0 占用约 13.5 GiB。它们证明当时资源竞争背景存在，不证明其他 GPU0 工作导致 Gazebo 卡住。旧 capture 没有 world stats/pause state，也没有 gz gdb。

## 竞争解释

| 假设 | 现有证据 | 判断 |
| --- | --- | --- |
| world 被暂停或 stepping 停止 | `gz sim` 启动参数包含 `-r`；检查到的 collector 控制命令只向 `/world/default/wrench/persistent` 和 `/world/default/wrench/clear` 发布实体 wrench/clear，没有 world pause 或 step 命令。旧 capture 未读取 WorldStatistics。 | 未见本仓库显式 pause/step 命令；无法排除运行时外部暂停或 Gazebo 内部停止更新。 |
| rendering sensor / GPU 等待 | Gazebo 进程使用 `--headless-rendering --render-engine ogre2`；GPU0 同期有占用，gz 几乎无 CPU 增量。 | 合理候选，但进程在 GPU wait、渲染锁或其他等待上的证据缺失。 |
| frame server back-pressure | `LatestCandidate.put()` 只替换候选并通知；请求循环等待新 stamp 后才将 packet 写到 stdout。subscriber callbacks 不写 pipe、不等待 host reader。 | 没有证据支持它能反向阻塞 Gazebo physics。若 stdout 写阻塞，只会阻塞 frame server 请求线程；此次冻结发生于 step 6 的 primitive 等待。 |
| PX4 logger off | 日志确认 `SDLOG_BACKEND=0`；同类 logger-off episode 多次正常，本次也已完成起飞和 6 个 primitive。 | 不能据此认定 logger 设置是原因；未观察到关闭 logger 后直接影响 clock 的代码路径。 |
| pre-arm budget / resume 改动 | 本 episode pre-arm 已就绪，且 `episode_index=0`、无 resume；预算逻辑只影响 pre-arm health wait。resume 改动只挑选中断 episode 重新收集。 | 与本次 stall 路径不符。 |

## 提交范围核查

检查了 `cb8244a3..64bc76d4`。`64bc76d4` 本身只合入 r9t 分析文档。相关运行时代码提交中，logger 默认 off 改变启动环境及 ULog 收集策略；pre-arm 预算改为 Linux PX4 clock 预算并设 wall ceiling；stall capture 增加 transport 证据；resume 改动只影响 campaign 重跑选择。它们可能改变运行环境或 stall 后的观测，但现有证据没有把任一项连到此次 Gazebo clock 发布中断。r9r 提交更新场景和生成世界；此次实际世界是 `gen_cnt2_w0000.sdf`，不能仅凭提交时间排除世界负载的影响。

## 采集改进

`scripts/px4_clock_stall_capture.py` 现增加对 `/world/default/stats` 的只读、限时采样，并解析 `paused` 与 sim time。它还会像 PX4 一样对 `gz sim` 做前后两次 gdb 快照，输出到 `px4-clock-stall-gz-gdb.txt`；两进程共用既有 30 s gdb 总预算，降低每个快照超时以保持总预算。采集不向 world control service 发送请求，不会主动 unpause 或 step。pause 字段或 stats 消息未取得时会记录 unknown/缺失，而不推断状态。

## 28-head 后续状态

本报告收尾时扫描到 8 个 episode.json，其中只有上述 1 个 stall-like failure；其余已落盘 episode 没有 stall failure。gate 运行期间新增的 episode 也已纳入这次扫描。`artifacts/` 是共享只读产物，扫描仅做读取。

## 验证

- `tests/test_px4_clock_stall_capture.py`：14 passed。
- `python scripts/run_onboard_gate.py -q -p no:warnings`：PASS（pytest 通过，176 s）。

## r9r 首次完整 stall capture（2026-11-07）

证据目录：`artifacts/r21id-r9rb/episodes/gen_r21id_r9r_w0003/ep-0/attempt-0/episodes/episode-0000-536f4b08/`。该目录只读。本节以 capture JSON、PX4 GDB 和 Gazebo GDB 两份快照为依据。

### 探针结果与分类

- 两次 `/world/default/clock` 订阅均以 `timeout 5s` 返回 124，stdout 为空；`/world/default/stats` 订阅均以 `timeout 3s` 返回 124，stdout 为空。因此 `gz_clock_advanced=unknown`，stats 中 `paused` 没有输入可解析，`gz_world_paused_samples=[null,null]`。这不是 parser 把已收到的 pause 字段漏掉，也不能解释成 world 未暂停。
- 命令使用的 `/world/default/stats` 是预期 WorldStatistics topic；采集超时不是宿主 `subprocess` 的 4 秒 timeout（命令自己的 3 秒 `timeout` 先到）。当时 load average 约 230–244；高负载可能使探针来不及完成发现/收消息，但单次 capture 不能证明这是唯一原因。更关键的是 gz runner 两次都卡在 transport `PublishStats` 路径，因而 stats 本身也可能无法送出。采集没有取得消息，所以无法用解析器验证这次运行的 pause 字段格式。
- `reception_thread_classification=unknown` 是旧分类器对缺失 clock 样本的保守结果。两份 PX4 GDB 快照都识别到同一 reception thread 在 `poll -> zmq_poll -> NodeShared::RunReceptionTask`；由于无法证明 gz clock 同时前进，不能判定“消息停止到达”。`callback_thread_classification=not_observed` 表示两次栈中都没出现 `px4_clock_settime` 或分类器关注的条件广播栈，不代表 callback 一定正常。
- 修正 capture 顺序：先采集进程/线程和 GDB，再运行会注册 Gazebo transport node 的 `gz topic` 探针，减少探针对 gz transport 锁栈的干扰；接收线程存在而 clock 样本未知时，现在输出 `reception_polling_clock_unknown`，保留观测而不越过证据下结论。WorldStatistics parser 也接受 `pause` 与 `paused` 两种字段名。该修正仅改变诊断采集，不改变仿真。

### 手读 gz GDB 栈

两个快照（间隔约 23.7 秒）中，gz 主线程均在 `pthread_mutex_lock -> NodeShared::CheckSubscriberInfo -> Node::Publisher::Publish -> SimulationRunner::PublishStats -> SimulationRunner::Step -> Run`。这是 runner 在发布 stats 时等待 Gazebo Transport 内部 mutex；栈没有给出锁持有者，因此可确认 mutex 等待，不能确认是死锁、谁持锁或锁竞争来源。runner 位于 `PublishStats`，尚不能说 physics 计算本身阻塞在传感器回调或 PX4 lockstep 同步上。

渲染侧有约 15 个 Ogre `SceneManager::_updateWorkerThread` 在 `pthread_barrier_wait`；CameraStream 的 GStreamer 线程在 `nanosleep`，其余 appsrc/queue/x264 工作线程在条件变量等待。后者与暂时没有新帧相容，但不证明它们阻塞 runner。栈没有 CUDA、EGL 或 GPU 驱动调用，也没有 PX4 lockstep 栈。因而证据指向 gz runner 的 transport 发布 mutex 等待，不能归因于 GPU、渲染传感器、或 PX4 同步。

这份 GDB 是在旧采集顺序下取得：同一 sample 的 `gz topic -i`、clock 和 stats 探针先于 GDB 执行，且 `gz topic -i` 会注册瞬态 discovery node。因此不能排除探针对 transport mutex 状态的影响；此前无法判断这条锁等待在无探针时是否已经存在。新的顺序先取 GDB，再启动 topic probes，后续复现可检验这一点。

### 与 28-head stall 对照

28-head count-cones 的 PX4 capture 也记录到 reception thread 在 `poll`、clock 探针超时、无 callback 栈，故两次在“PX4 未收到可见 clock 更新”的可观测边界相似。28-head 没有 gz GDB，且没有有效 world stats；无法比较 runner 是否也停在 `PublishStats` mutex，不能称 Gazebo 内部状态相同。28-head 本身只证明 PX4/Gazebo 进程存活而 PX4 clock 不推进；它既未确认暂停状态，也未区分 gz 内部停顿位置。

### 假设与可证伪观察

单一工作假设：这次 stall 可能是 gz `PublishStats` 路径上的 Gazebo Transport mutex 长时间无法取得，使 runner 停在 `Step`，进而停止推进仿真时间。现有证据支持“快照时正在等待该 mutex”，但由于 topic 探针先于 GDB、且锁持有者未知，**尚不足以确认它是根因**。若无 topic 探针污染的复现 capture 显示 runner 在两个快照中持续越过 `PublishStats`/`Step`，而 PX4 clock 仍停止，则该假设被反驳。当前证据不支持 GPU 等待、渲染线程反压或 PX4 lockstep 作为直接原因。

### 验证

- 更新 `tests/test_px4_clock_stall_capture.py`，覆盖 `pause` 字段解析、未知 clock 下 reception poll 分类和 topic 探针顺序。
- `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest tests/test_px4_clock_stall_capture.py -q -p no:xdist`：15 passed。
- `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_onboard_gate.py -q -p no:warnings`：PASS（236 s）。
