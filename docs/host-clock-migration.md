# 主机高分辨率时钟迁移

日期：2026-09-08。当前状态：共享QPC实现、像素生产/消费、父子Mission身份传递已完成；显式QPC单任务外部camera C/D已运行。默认旧时钟路径保留。源时间同步和训练标签资格尚未证明。

## 当前结论：接收时钟统一不等于源时间同步

### 固定偏移之外的候选：仿射速率差

最新F外部验证通过并精确清理：602个采集状态样本全有效，三holdout位于ARM后33.7790..33.7850秒、任务接收窗口总长102.8616秒内，sidecar与报告同值、同Clock身份。独立仿射审计相容但不改变端点拟合；不证明源同步、物理时间误差或训练标签资格。证据docs/evidence/qpc-camera-f-holdout-20260908.json。独立能量重审尚未执行。

E失败的已有报告不能恢复首个无效样本：观察器异常时原flight结果停留在初始值，不能据此推断从未固定翼或未起飞。已新增独立有界flight_capture_diagnostics，保留样本计数、首个无效样本、最后样本、首错计时摘要及marker错误，即使observer未正常返回也由外层报告保留。冻结遥测用例证明“状态位仍有效但消息年龄超限”可被明确记录，408项相关回归通过。F未复现E，不称为E根因修复；阈值/拒绝条件未变。

中段验证采集已接线：父进程在像素完成标记之后、等待Mission子进程之前执行一次三探针批次，不占用子进程遥测循环。六个端点探针仍单独拟合；holdout验证仅检查联合约束相容，不更新端点拟合。时间顺序/同容器/主机身份需通过校验；实际是否位于Mission飞行窗口内仍需另验，不把调用位置当作时间证据。

新camera E实跑失败并完成精确清理：Mission拒绝采集期间离开FW/armed/in-air/AUTO.MISSION状态的完成标记，未放宽门槛。失败路径暴露holdout仅存内存；已改为采样后立即独占写入host-container-holdout.json，包含起始和holdout原探针，避免后续Mission失败丢失诊断。E发生在此修复之前，不能补造其holdout数据；修复后248项相关测试通过，尚未再次外部运行。见docs/evidence/qpc-camera-e-holdout-attempt-20260908.json。

`scripts/audit_affine_clock_rate.py`独立检验`host_ns = rate * container_ns + offset`且rate>0的假设。它不修改原固定偏移审计，不产生校准参数，不接入RuntimeBackend或训练晋升。输入外层报告必须匹配调用方SHA，重算原探针摘要并核对容器/主机身份；本脚本不重验Mission事务或Clock流。

对每个因果探针，令主机区间为[L_i,U_i]、容器时间为C_i。对于i<j，精确约束为`(L_j-U_i)/(C_j-C_i) <= rate <= (U_j-L_i)/(C_j-C_i)`。取所有探针对约束的交集，再与正数相交；用Fraction保留整数精度，不用最小二乘把矛盾数据平均成映射。若交集为空，conditional_rate_interval为null。交集非空只表示假设与这些样本相容，不证明采样间恒速、传输时延、时间戳准确度或源同步，也不允许外推。

C与D各六个原始探针的SHA绑定复核分别见`artifacts/evaluation/qpc-camera-c-affine-rate-20260908.json`、`artifacts/evaluation/qpc-camera-d-affine-rate-20260908.json`：固定偏移均不相容，正仿射假设均相容。host/container条件速率差C约-52.3281..-38.6432ppm，D约-67.5420..-41.4210ppm。区间有交集，但不能据此宣称同一速率长期成立。38项新旧探针测试通过，覆盖非仿射矛盾、巨大整数原点、严格正速率边界和SHA/身份拒绝。

下一步应使用独立的任务中段探针检验开始/结束探针推导的约束，并分别核验PX4启动时钟与Gazebo源时钟的关系。不得直接将此候选应用于未验证的图像/飞控时间标签。

接收连续性现在由`audit_mission_clock_receipts.py --max-receipt-gap-ns N`显式检验，N为1..3600000000000的整数纳秒。未提供预算不返回合格；缺少严格边界返回insufficient_coverage；只检查任务窗口及严格边界相邻记录，窗口外大间隔不污染窗口结论。即使passed也不声明观测新鲜度、源对齐或训练资格。CLI成功退出表示审计完成，必须读取receipt_continuity.status/qualified判断连续性。

D的200ms诊断预算复核见`artifacts/evaluation/qpc-camera-d-continuity-200ms-20260908.json`：886个窗口间隔，最大173185800ns，连续性通过。全采集最大986702600ns位于该窗口之外。200ms只用于本次显式敏感性检查，不是新生产标准或飞行控制阈值。46项时钟审计/身份绑定/采集回归通过。

camera D独立审计文件为`artifacts/evaluation/qpc-camera-d-clock-independent-20260908.json`，绑定外层报告SHA `3bdb4eabe7c03dcfe03a59b24a2eb5820d125c90544274c25d68cf65f0073370`和Mission SHA `9b78cf7275960ca1117bf48e0c39eff5f250edf41ed231c2986d12292a8b4b3f`。994条Clock记录严格包围ARM与完成，接收包围间隔分别102.3884ms、115.6029ms，最大主机接收采样间隔986.7026ms。

这些是接收侧关联，不是曝光、飞控启动时间或源事件的误差界。`training_label_eligible=false`，独立审计也没有重新验证Mission事务日志。D的能量接收窗口覆盖和当前像素采集门通过，不改变这一结论。

下一步设计应分别解决：任务决策所需的观测新鲜度、同一来源的时间排序、跨来源训练标签对齐。不得预设单一固定偏移必然适合全部来源，也不得通过放宽超时把未对齐数据标成合格。

## 历史阶段记录

下方记录保留各阶段当时的限制与待办；“尚未接线/尚未运行”等只描述当时状态，不覆盖上方当前结论。

## Campaign入口接线（2026-09-08）

独立复核camera C：Clock审计新增单任务初验/终验双哈希绑定，成功复核1429条Clock接收记录，ARM与完成严格包围间隔89.114ms和108.8499ms；不证明源事件时间。能量审计在24.165s因left heartbeat/throttle age=1.55554/1.51664s超过1.5s停止，结束窗口缺失。像素门不被重写为失败，但完整能量/同步训练资格明确为false。下一步应针对该运行的帧哈希路由记录定位接收缺口，不放宽阈值或外推缺失能量。

最新外部结果：qpc-camera-20260908-c在固定镜像中实际完成Mission与3帧采集，所有当前外层门通过并清理，父/子/像素QPC session为b2b017fb3d0a4423995c36d7e3bcd84d。B无合格续航而失败；C使用100Wh容量/36W航电/324W最大推进功率/100Wh初始余量的合成配置（非物理标定）。Clock探针constant_offset_compatible=false，仍不可声称源同步。完整报告SHA、失败报告SHA及容器ID已记录current-progress.json；此处只通过单任务像素路径，不覆盖能量窗口、训练有效性或全模式验收。

当前入口状态（覆盖下方旧阶段描述）：QPC单任务图像路径已开放，父进程--host-clock perf_counter创建一次Clock并传播session，子进程保留ready/complete文件参数。单任务--capture-clock可选；不开启不产生Gazebo Clock诊断或同步声明。QPC campaign仍要求--capture-clock。445项相关测试含完整外层旧/QPC生命周期替身通过；真实observer和像素生产链另有前轮单元证据，尚未新外部Gazebo端到端验收。下一步应运行独立、固定镜像、可清理的外部单任务图像验收，失败记录不可用本地测试替代。

外层增量：等待器接收调用方身份；selected_clock传入sensor_capture，起止、complete/timing使用同源，初验和清理后ready/sensor/Mission复验均传identity。sensor验收重新读取磁盘manifest，将其声明（缺省旧域）及逐帧provenance与父身份绑定。241项相关测试通过，旧生命周期保持，QPC父验收正常/错父/缺父均覆盖。尚未解除_process_arguments、外层run及Mission CLI的QPC图像保护，下一步先增加完整QPC生命周期测试再联动解除限制。

生产链增量：_capture_gazebo_sensor_transport新增可选QPC Clock，传入真实backend；_write_frame_set_manifest从调用方接收完整身份，在计算manifest哈希前核对全部帧。错误身份/混域拒绝时保留已有像素供诊断但不写manifest。325项相关测试通过，测试backend实际读取内存wire fixture并落盘，未运行外部Gazebo。下一步外层需统一_wait_for_flight_ready的截止时间及身份、sensor_capture(clock=selected_clock)、起止读数、complete/timing标签与身份，并把身份传入validate_sensor_capture/validate_mission_evidence；不得仅解除CLI限制。

像素消费者增量：检测/定位入口验证manifest哈希后共用validate_frame_set_clock，顶层声明时钟域，逐帧核对身份，不能每帧自选域。旧manifest无顶层声明时仍解释为旧域；QPC要求顶层和全部帧完整身份一致。89项相关测试通过，正常QPC检测/定位fixture保持真值隔离。此处不比较外部主机时间，不声称哈希等于来源认证；外层父Clock绑定及seal manifest生产者接线是下一阶段，CLI限制不变。

像素生产者增量：GazeboSimFrameSource.read在实际Clock为HighResolutionSystemClock时输出host_perf_counter及完整身份。内部身份保存为不可变标量键值元组，to_mapping导出新dict；旧无身份格式不改变。captured_at_s仍在wire读取/校验后由同一Clock读取，与sensor_stamp_ns不等价。50项backend测试通过；尚未修改检测/定位消费者，QPC文件仍被旧消费者拒绝，外层入口保护保留。核心指纹已更新，历史E7对照另存而非修改原报告。

消费者增量：observer complete校验使用Collector实际QPC身份，父Mission验收新增调用方身份参数，对ready、complete和每条interval sample同域验证；424项相关测试通过。实际observer函数的两域测试覆盖ready文件、complete消费和区间资格，而非仅手工构造终态报告。剩余像素链路包括`backends/gazebo_sim.py`的provenance生成、`candidate_detector_controls.py`和`gazebo_target_geolocation_acceptance.py`的旧标签消费者。外层等待/采集尚未切换，CLI保护仍保留；无新外部运行。

图像屏障API增量：`validate_flight_capture_ready`与`validate_flight_capture_complete_marker`新增调用方`host_clock_identity`参数，共用`capture_clock_matches`。显式QPC必须标签/完整身份与调用方一致；不传身份时只接受旧域（允许旧报告缺少身份，不允许显式null或矛盾身份）。416项相关测试通过，包括两端身份篡改及QPC下原有安全拒绝条件。检查结果键`host_monotonic_timestamp`/`host_monotonic_clock`暂保留历史名称，实际语义为调用方选择的单调时钟域校验，不表示QPC与旧域等价。生产者、父等待器和区间/provenance消费者尚未全部迁移，因此图像CLI限制保持；本轮无新外部运行，旧全量证据不覆盖本轮改动。

最新C实跑：双任务完成、原外层gate为true，但独立审计发现第二任务能量窗口末端缺失，现已补每sortie父能量验收，重审原报告失败；不覆盖原历史结果。12571路由记录无丢弃，累计11593唯一帧匹配、767歧义；最大路由解码至Collector处理约1.659s，不等同网络时延或已证明调度根因。两个Clock接收窗口均有前后记录；跨289.376s六个host/container探针区间不相交，固定偏移假设不成立，训练标签仍不可用。406相关测试通过，见`docs/evidence/qpc-receipt-campaign-20260908.json`。

接收缺口诊断已接线（尚未新外部运行）：QPC父进程把同一Clock传给router；`router.result.telemetry_receipts`记录0/2/33/74消息的原始帧SHA256、用户态解码/转发时间和源身份，上限131072条，丢弃数明确。Collector在energy_input_history.samples中记录同一帧哈希、pump进入和上次退出时间，接收/解码后处理时刻仍是origin+at_s。400项回归含本机UDP转发和ManualClock时序验证通过。帧哈希缺失为null，重复哈希在后续关联时应判为歧义；两端用户态观测不能证明内核收包、发送端时刻或真实网络时延。

外部验收现状：A/B两次新隔离运行均失败且精确清理，不能声称完整QPC campaign通过。A发现实际flight返回漏host_clock_identity，已修复并补真实observer返回值测试；B身份验证通过，但首任务21.585s处心跳/油门年龄1.533/1.529s超过1.5s，累计覆盖不完整使第二任务创建事务前拒绝。下一步需要发送/路由/主机调度诊断证据。403项相关回归通过，原始失败记录及哈希见`docs/evidence/mission-clock-cli-20260908.json`。

外层现在可显式选择`--host-clock perf_counter`，目前要求同时提供`--campaign-file`与`--capture-clock`。父进程创建一次HighResolutionSystemClock，将源与session_id传给campaign子进程；Clock JSONL和探针复用父时钟。每个sortie的window/flight/observation/energy history必须与父身份一致，完成时及清理后都重验。默认仍是monotonic，不自动更改旧命令的时间域。

Mission及campaign子进程入口支持`--host-clock perf_counter --host-clock-session-id <parent-run-id>`。缺失/非法session、旧源搭配session及QPC搭配未迁移的图像屏障均在打开遥测连接前拒绝。独立子进程仍要求明确的disposable SITL acknowledgement，不创建容器或扩大控制范围。

这一入口只打通无图像屏障的campaign。单任务camera ready/complete仍待迁移，不能据此声称像素、深度、PX4 boot和Gazebo源时间已同步。实际新campaign的结果另记进度与证据；下方“外层尚未接线”段落是前一阶段历史，不代表本段新增入口。

在线累计能量消费者阶段已完成：`CumulativeSimulationEnergy.observe`验证QPC接收身份、锁定会话内源/身份/原点并保持append-only及失败原子性。积分仍用PX4 boot时间。`validate_host_identity`已进入共享Clock层，探针保留兼容导入，窗口审计直接复用共享校验。132项相关与212项集成回归通过；核心指纹同步更新。下面“累计消费者待迁移”为前一阶段记录，当前剩余边界是外层进程传播和图像屏障；尚无新完整QPC Mission证据。

能量窗口审计阶段已完成：`audit_mission_energy_window.py`要求QPC window、flight、post_mission_observation和energy_input_history四方身份严格相同，沿用探针身份格式验证。旧无身份报告仍按monotonic域读取，显式矛盾身份不会被忽略。114项相关测试通过，旧campaign-clock-c/mission-001重新审计通过；未产生新的QPC Mission证据。累计能量的`CumulativeSimulationEnergy.observe`仍只支持旧域，应在父子进程和图像屏障接线前迁移。原文“能量历史消费者待迁移”现指这个在线消费者，不再指独立窗口审计。

父进程采集API阶段已完成：`MissionClockCapture(..., clock=HighResolutionSystemClock(...))`与`sample_probes(..., clock=...)`共用同一读数/身份。JSONL每行带host_clock_identity；高分辨率wait_after只接受同源同session的HostClockStamp，不能传裸整数。独立审计要求Mission window/flight、capture和每行身份一致，并重算host_container_probes摘要。旧无身份路径保持旧域，不能自动升级为QPC。235项相关回归及实际6次QPC容器探针通过：区间0.593672ms，但跨度仅4.5581ms，不验证长时漂移。

外层运行器尚未创建和传播这个Clock，默认CLI仍是旧源；能量历史消费者和图像屏障也不能提前接收新域。必须完成这些接线后再执行新完整Mission验收。下面“父进程探针待迁移”指外层接线，不再指采集API实现。

Mission内部阶段已完成：事件/等待统一调用Collector的now_s；上传/读回协议显式传入Collector用于截止时间，终态尾段读取同源观测。MissionTelemetrySession可接收Clock，初始化失败关闭连接；高分辨率history要求相同源身份，完成报告/窗口携带身份。默认旧适配器保留单一legacy fallback，AST回归约束直接OS读数只存在该处。370项相关回归通过（8项新增）。

外部边界仍未完成：高分辨率Session与图像采集ready/complete标记组合会在控制动作前拒绝，不能把旧host_monotonic标记当作QPC时间。父进程/子进程session传播、探针/JSONL与采集标记生产及校验必须一起迁移；目前不开放CLI开关。后文“Mission上层仍有直接调用”是前一阶段记录，当前已按本段替代。

Collector阶段已完成：`TelemetryCollector(..., clock=...)`显式注入，初始化/消息接收/elapsed/心跳及所有等待截止时间共用`now_s()`，拒绝非法值和回退；默认SystemClock不变。高分辨率`post_mission_observation()`必须读取自身时钟，拒绝裸外部now_s；报告及能量/电池历史包含源身份。自定义测试时钟标为injected_clock，不能冒充host_monotonic。362项相关回归通过，新增9项验证含类内禁止直接time读数的AST检查。

Mission上层仍有直接monotonic调用和裸now_s传参，尚未完成整体迁移。此时显式注入高分辨率Collector不能当作完整高分辨率Mission支持；应先完成上层调用及报告契约，再开启外部选项。当前没有新增CLI切换开关。

## 目标与事实

目标是让任务事件、遥测接收和时钟探针可在同一明确的主机时间域中比较，不是让报告数字显示更多小数位。本机Python3.12的monotonic为GetTickCount64，分辨率15.625ms；perf_counter为QueryPerformanceCounter，报告分辨率100ns。分辨率不是精度或跨设备同步保证。

## 已实现的共享接口

`multidetect.clock.HighResolutionSystemClock`实现原有Clock协议，可通过已有`clock=`入口注入。`monotonic()`和`monotonic_s()`均从`perf_counter_ns()`换算，`read()`保留整数纳秒，UTC记录仍单独提供。`identity()`保留时间源、会话、平台实现及分辨率，明确不等价于旧monotonic域、不认证准确度。

`HostClockStamp.elapsed_since()`仅允许同源、同会话、非倒退时间相减，先整数相减再转秒。独立时钟实例默认生成独立会话ID。父运行器需显式分配并传播同一session_id；相同标签本身不证明不同主机、不同开机周期的时钟相同，不能据此绕过外部运行身份校验。

```python
from multidetect.clock import HighResolutionSystemClock

clock = HighResolutionSystemClock(session_id="explicit-owned-run-id")
started = clock.read()
# All participating event/observation producers must receive this clock.
completed = clock.read()
elapsed_s = completed.elapsed_since(started)
```

旧SystemClock和ManualClock保持原行为。没有更改全局time函数、添加恒定epoch偏移、替换历史时间戳，或将确定性Replay切成实时时钟。

## 当前源码中的迁移边界

| 位置 | 当前时间来源 | 必须同时处理的内容 |
|---|---|---|
| runtime_launcher.py | 支持注入Clock，缺省SystemClock | 同一运行的provider和消费者共用注入源 |
| vtol_sitl_scenario_acceptance.py / TelemetryCollector | 已改显式Clock，默认SystemClock | 本层已统一；上层需要注入相同源并读取其身份 |
| vtol_sitl_mission_acceptance.py | 已统一Collector读数，旧适配器单点fallback | 内部迁移完成；图像屏障与跨进程身份待迁移 |
| mission_clock_capture.py | 主机monotonic_ns | JSONL域身份、前后探针、wait_after阈值 |
| run_vtol_mission_sensor_capture.py | 主机monotonic/ns | 父子进程选择及session传播、等待与报告身份 |
| clock_offset_probe.py | 主机monotonic_ns与容器monotonic_ns | host源声明、分辨率与条件区间，不能单点换QPC |

## 迁移顺序与验收

1. 先把Mission/Collector所有事件和freshness消费者改为显式注入，仍用旧默认源跑回归，防止遗漏直接调用。
2. 父运行器显式选择高分辨率源并将会话ID传递给子进程；新报告保留源身份，原格式按旧域读取，不自动转换。
3. 探针和JSONL切入相同主机域，拒绝跨源/跨运行绑定；修改独立审计器以校验身份。
4. 新隔离完整Mission验证报告/原始记录/探针全窗口、结束清理、失败恢复及journal关联；旧报告不算新路径验收。
5. 另行评估跨主机时钟速率/漂移、Gazebo发布到回调延迟及PX4 boot来源关系。QPC更细并不自动解决这些问题。

不推荐全局替换time.monotonic，也不推荐只在探针里做QPC映射后与旧事件直接相减。当前选择是有身份的注入迁移；若实际回归暴露共享Clock抽象不足，允许修改抽象，而非不断增加隐式偏移补丁。
