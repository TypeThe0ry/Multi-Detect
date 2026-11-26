# Altair / X1 在途决策接口 v1

适用范围为 `delta_tailsitter_gimbal_cam` 的观察与测绘仿真。起飞前保留现有任务包验签、能力协商和标准 `MissionPlan` 编译流程。起飞后，机载 Altair 在收到新事件时生成单个决策。PX4 按自身配置运行原生保底。X1 执行侧验证 Altair 提案，并在准入后分派该动作。

## 输入

`X1StateBuilder` 接收主机单调毫秒时钟上的观测和事件。`snapshot()` 生成 `x1.contingency.state.v1`，`X1ContingencyExecutor.open_request()` 包装为 `x1.contingency.request.v1`。以下是删减未列出遥测组的示例。

```json
{
  "schema": "x1.contingency.request.v1",
  "request_id": "sortie7:1",
  "event_id": 12,
  "deadline_ms": 43000,
  "remaining_plan_sha256": null,
  "native_owner": null,
  "hold_refs": [],
  "capabilities": ["airframe:x1", "gimbal_control"],
  "actions": ["abort-transition", "continue", "gimbal-retarget", "land-now", "loiter", "retask", "return"],
  "state": {
    "schema": "x1.contingency.state.v1",
    "vehicle": "delta_tailsitter_gimbal_cam",
    "mission_id": "survey-7",
    "revision": 1,
    "t_ms": 40080,
    "latest_event_id": 12,
    "telemetry": {
      "link": {"gcs":"down","heartbeat_age_ms":5300,"age_ms":80,"source":"link_monitor"},
      "battery": {"pct":42,"charge_state":1,"age_ms":300,"source":"mavlink"},
      "vtol": {"state":"fw","landed_state":2,"age_ms":85,"source":"mavlink"}
    },
    "host_leg": {"step_id":"survey","progress":0.48,"age_ms":120},
    "events": [{"id":12,"type":"link_loss","t_ms":40000,"source":"link_monitor","severity":"warning","confidence":0.98}]
  }
}
```

| 遥测组 | 字段与单位 | 新鲜度上限 |
| --- | --- | ---: |
| `battery` | `pct` 0–100、`charge_state` MAVLink 枚举 | 5000 ms |
| `wind` | `north_mps`、`east_mps`、`down_mps` 为 NED 分量 | 5000 ms |
| `link` | `gcs` 为 `up/down`、`heartbeat_age_ms` 是 GCS 心跳年龄 | 3000 ms |
| `fc` / `health` | `armed` 与 FC `system_status` 枚举，`health.system_status` 为 `SYS_STATUS` 传感器健康位掩码 | 3000 ms |
| `gps` / `ekf` | `fix_type`、`satellites`，`flags` 与 `horizontal_error_m` | 3000 / 5000 ms |
| `agl` / `terrain` | 高度 `m`，前向 `clearance_m` 有符号、`conflict` 布尔值 | 2000 ms |
| `vtol` / `air` | `state` 为 `mc/fw/front_transition/back_transition`、`landed_state`，`airspeed_mps` | 1500 / 2000 ms |
| `target` / `obstacle` | `visible`、`track_state`、`track_ref`、`identity_confidence`，`conflict` 与 `clearance_m` | 2000 ms |
| `navigation` | `home_distance_m`、`remaining_time_s`、`remaining_energy_wh` 由机载估计器供给 | 2000 ms |
| `leg` / `host_leg` | MAVLink `seq/total`，执行器 `step_id/progress`。`progress` 为 0–1 | 5000 ms |

数据组过期后为 `null`，单字段未知也为 `null`。同组一条观测产生一个来源与年龄。需要不同时间基准的量分别放在各自组内。PX4 心跳用于 `fc`，GCS 状态由单独的地面链路监测器供给。目标、障碍、前向地形与导航估计从相应机载候选源接入。`fault` 是 CPU 回放的合成来源。事件保留最近八条且最多 15 秒，事件序号在同一 run 内递增，`severity` 和 `confidence` 可缺省为 `null`。事件本身只描述观察。

X1 采集器优先使用 1500 ms 内的有效 `WIND_COV`。否则，仅在明确处于空中固定翼模式且空速至少 12 m/s 时，用新鲜的 `LOCAL_POSITION_NED`（无可用配对时用 `GLOBAL_POSITION_INT`）水平地速减去空速沿机头方向的水平投影。机头方向取自 `ATTITUDE`（X1 尾座机的固定翼前向对应 PX4 报告机体系的 `−Z`，不把 HUD 整度航向当机头方向）。假定空速近似真空速、侧滑和迎角较小；只估计水平风，`down_mps` 为 `null`，模式或输入失效时整组为 `null`。输入年龄不超过 1000 ms，配对接收时差不超过 250 ms；2 秒窗口内须有至少 5 个独立内点且占比至少 75%。`age_ms` 取最新内点各运动输入中最旧的接收时间；`source` 仍为 `mavlink`。`wind_step` 仍须在原窗口内观测到相对配置基线至少 2 m/s 的变化，只接受注入后收到的证据。水平估计只能证明水平变化，事件 `confidence` 为内点比例，不是校准概率。观测不读取 Gazebo 实时真值，也不选择或改写动作。

Gazebo Harmonic 的 X1 风情景可通过 `run_x1_autonomy_demo.py --wind-airspeed-plugin /绝对路径/libx1_wind_airspeed.so` 显式启用风相对空速；仅在选中 `wind_step` 时可用，默认关闭。启动器在该作业的 server config 中替换 stock AirSpeed，而不是另起一个发布者。替代系统把世界风投影到皮托管正向，从 stock 轴向地速中减去该投影；仍使用原传感器的压差模型、更新频率和 PX4 订阅话题。零风时使用原速度输入。世界风只在 Gazebo 传感器模型内部使用；Altair 仍从 PX4 遥测估风并决定动作，执行器只校验包线并接受或拒绝，PX4 内置安全行为不变。源码和构建入口见 `simulation/gazebo/plugins/x1_wind_airspeed/`。

`wind_step.values.ramp_ms` 可选，默认 `0`，表示窗口开始和结束时分别立即切换到目标风和 SDF 基线风。正值为每段斜坡的仿真毫秒数，必须为有限数，范围为 `0..duration_ms / 2`。注入器在窗口开头按 10 Hz 的 PX4 boot-time 网格线性插值 ENU 风矢量，达到目标后保持，并在窗口最后 `ramp_ms` 内回到基线。独立线程串行发布，慢命令期间跳过过时样本，窗口结束时补发精确基线。异常退出时先等待在途发布结束，再发精确基线，恢复命令失败时重试一次。`scenario_injected`、`scenario_restored` 和发布失败标记的 `applied` 记录斜坡时长、采样间隔、基线和目标矢量，以及最后一次成功发布的 `enu_mps`。发布确认的 `readback` 为 `publish_only`，遥测触发以独立的 `scenario_observed` 为准。CPU 合成回放仍注入窗口目标值。

给推理器的固定上下文还应带原始任务句子、当前计划摘要和已授权引用。`remaining_plan_sha256` 绑定宿主所提供的摘要来源。将事实快照作为单独的紧凑 JSON，编译器已有的消息模板继续承载原始任务和静态上下文。`actions` 是可序列化的动作词表，当前阶段和证据决定每个动作能否准入。保留约束、候选和数据缺口供模型判断。以实测分词器记录输入/输出 token 和首动作延迟。3 秒事件截止与 500 毫秒执行帧新鲜度是 v1 软件上限，具体场景可有更早的物理截止。

## 输出

只接受一个 UTF-8 JSON 对象，最多 6144 字节。回显 `request_id`、`event_id`、`mission_id` 和当前 `revision`。以下消息在对应请求还有效时，提议继续当前计划。

```json
{"schema":"x1.contingency.decision.v1","request_id":"sortie7:1","event_id":12,"mission_id":"survey-7","revision":1,"action":"continue","params":{}}
```

| `action` | `params` | 准入后的执行 |
| --- | --- | --- |
| `continue` | `{}` | 对当前活动轨迹重新验包，通过后保留计划。此调用发出 0 条新指令 |
| `retask` | `{"plan": <MissionPlan v1>}` | 按原任务包引用边界验证，机载串行 tick 原子替换剩余计划。`revision` 加一 |
| `loiter` | `{"hold_ref":"hold:a"}` | 只接受请求中的引用。MC 进入当前位置 Hold（`DO_SET_MODE` AUTO.LOITER），FW 用已证明的圆心、半径、高度和速度发 `DO_ORBIT` |
| `return` | `{}` | 分派 `rtl()`（`NAV_RETURN_TO_LAUNCH`），按本轮 PX4 路径、过渡与返航走廊证明准入 |
| `land-now` | `{}` | MC 稳态下分派 `land()`（`DO_SET_MODE` AUTO.LAND，原地降落），需当下着陆区及下滑净空证据 |
| `abort-transition` | `{}` | 在已验证支持的前过渡场景发出 `vtol_transition("mc")`。后过渡问题保留独立事件和评估，当前 v1 对未验证的中止路径拒绝 |
| `gimbal-retarget` | `{"pitch_deg":-20,"yaw_deg":5}` | 云台直接接口，机型俯仰、偏航及默认滚转须在角限内，需稳态且云台控制权可用 |

`retask` 的 `plan` 保持 `{plan_version:1,goal,steps,report}`。每步仍使用 `{id,skill,params,on_success,on_fail}`。外层 `revision` 是运行版本，不受模型控制。挂接 `X1AirbornePlanAdapter` 时，FW 接受最多四步 X1 巡航或观察原语后接 `back_transition → return_home`，也接受仅后两步。MC 接受 `return_home`。边必须显式、有限并终止。空中 failure edge 需要由 X1 原语执行器在遇到异常时上报新事件，并等待已通过校验的 Altair 决策后再确定后续动作。计算线的 `replace_plan` 映射为 `retask`。`clarify/safe_stop` 不应转译成执行器自行挑选的命令。需要恢复动作时由 Altair 显式产出 `loiter`、`return` 或 `land-now` 等具体动作。

## 运行时联接

1. **消息身份与截止**。一个事件至多开一张票。更新的事件淘汰旧票。执行端使用与事件相同的单调时钟，确认事件、任务、版本和最新帧。票据匹配的有效决定最多消费一次。重启用新的 `run_id`，并重建活动计划与控制权。
2. **可信证据**。执行端用精度未截断的 `RuntimeFrame` 及 `MotionProof`，将路径、转弯包络、剩余时间与电量、过渡 AGL、围栏、地形和障碍裕度绑定到 `frame_id` 与规范 JSON SHA-256。前过渡离场条件、返回路径和着陆走廊需由机型与实际 PX4 配置建模。保护预算与已授权任务包由宿主提供。输入 JSON 的显示字段以及模型解释不参与证明。
3. **提交**。在现有串行控制 tick 验包并提交。X1 原语执行器应提供 `replace_remaining(plan, expected_version)`，将已证明的同一套原语原子替换，失败保留旧版本。`claim_native()` 必须在同一控制权域完成交接，防止普通任务发令与原生模式争用。订阅真实原生模式、ACK 与动作进度。链路允许恢复和任务重新接收前需独立确认原生控制权释放。
4. **回读**。记录关联 ID、决策摘要、动作、`Admission.status/reason`、PX4 ACK、模式与实际结果。`installed/continued` 只说明本地状态改变，`accepted_unconfirmed/sent_unconfirmed` 表示需回读。返回 `CommandReceipt` 的端口按 ACK 映射：接受为 `accepted_unconfirmed`，拒绝为 `transport_denied`，超时为 `transport_uncertain`；只发不收的端口仍为 `sent_unconfirmed`。`transport_denied/transport_uncertain/executor_failed/rejected` 进入宿主诊断。新鲜事件、模型超时、非法计划、传输失败均使用同一验收日志。执行器不从这些信号派生另一条动作。原生保底、Altair 动作和不明归因分别评分。

### 逐决策运动证明

`X1MotionProofBuilder` 绑定通过起飞前检查的 X1 单航点降级结果、原任务包、DEM 和机型。串行 tick 先核对票据、版本及帧时间，再用解码后的决策计算证明。证明与宿主提供的 `ExecutionSweep` 都绑定当前 `frame_id` 和决策摘要。扫掠段使用 DEM 本地北／东坐标和海拔（AMSL）；每段包括进入、退出、转弯与跟踪误差的空间包络，以及有证据支持的耗时和峰值功率上界。规划器只提出动作和引用，不提供这些上界。

扫掠体须落在原任务包运行区域与走廊内，符合任务半径和禁飞区；`continue` 和 `return` 还须留在已验证的单航点走廊及过渡延伸区内。用 DEM 相交网格单元的顶点和已知高程误差界算净空；前向地形冲突可否决。DEM 不覆盖、无可信地形误差界、障碍覆盖缺失或终端着陆区未经确认均不能算作净空。地面接触只在已确认可着陆的覆盖区允许。按各段功率与耗时上界、机型功率和过渡能耗下限算需求；以当前帧的剩余可用能量和剩余任务时间下界校验原有储备。电池百分比不能单独当作剩余 Wh，机型上限也不能代替预计动力学。

| 动作 | 证明器支持范围 |
| --- | --- |
| `continue` | FW 单航点、后过渡进入前、MC 原航线回家；须覆盖终端与所有可执行路径。若 FW 异常会走不同分支，宿主须先停在原证明边界并重新准入，不可沿未证明分支继续。 |
| `loiter` | 使用请求中已提供且当前帧未改变的 Hold 站点。MC 原地保持；FW 须覆盖接入和整个盘旋圆盘（含跟踪范围），按现有包络核对指令速度对应的最小转弯半径，以及实测空速、预报空速和坡度上界，并验证地形、障碍与原任务走廊。宿主须能在有界停留期结束前强制重新验证。 |
| `return` | 须证明实际 PX4 RTL 扫掠体：从当前点经已验证的原航线走廊（FW 含后过渡），到编译出的回家高度和位置，再垂直降落。原任务的 MC 回家航段不能直接代替 RTL。含着陆的能耗上界须在可用 Wh 下界内，并留出任务包与机型两种储备中的较大值；全程耗时上界不得超出剩余时间或任务预算。 |
| `land-now` | 只允许 MC 在当前水平位置下降。各段都须通过 DEM 和障碍检查，并位于可信着陆区覆盖范围内；只有最后一段允许接地，且须从地形误差上界以上下降。立即着陆可动用能量储备，但仍须证明下降能耗和耗时。 |
| `retask` | MC 回家、FW 后过渡回家，或 FW 一处任务包已授权航点后过渡回家。新路径使用同一份任务包围栏／走廊及 DEM；尚不支持多航点、观察原语和未验证分支。 |
| `abort-transition` / `gimbal-retarget` | 前者暂无运动证明；后者仍用原有云台准入，不使用 MotionProof。 |

CPU 回放使用 `recorded_decisions(...)`、`DecisionTick(..., gimbal_port=None, cpu_replay=True, build_proof=...)`。回放后端只把记录的动作和参数绑定到当前请求；扫掠证据由宿主单独提供，并按本次解码的摘要与当前帧编号核对。回放只向 `replay_commands` 写入通过准入的原始动作对应的命令。FW 盘旋的模拟 ACK 仅用于测试代码路径，不是飞控读回，也不能证明实际运动。普通 collector 不启用该选项，且回放模式拒绝接收真实端口。

### SITL collector 的实时 `continue` 证据

收到 Altair 回包的串行 tick 读取同一个 PX4 快照。当前位置使用来自目标对端、接收不超过 500 ms 的 `GLOBAL_POSITION_INT` 和 `LOCAL_POSITION_NED`。两者启动时钟相差不得超过 500 ms。经纬度与海拔转换到 runner 已加载的 DEM 坐标系，并核对当前编译步骤、入口位置和本地速度。证明从该位置起步，逐段验证扫掠体到回家着陆，包括当前位置、前方地形、DEM 高程误差和障碍覆盖。运动证明继续使用当下的帧编号和已解码决策摘要。成功的回执是 `action=continue / status=continued / reason=no_new_command`。

SIM 任务包可以由任务方提供 `x1_live_evidence`。这是宿主证据，不进入 Altair 的规划上下文。缺失、结构不正确或绑定不符会使实时 `continue` 保持拒绝。字段如下。

| 字段 | 证据契约 |
| --- | --- |
| `scope_sha256` | 取同一任务在 `--plan-only` 下的 `.contingency_scenario.motion_scope_sha256`。摘要覆盖已编译步骤、选中的故障、机型文件 SHA-256、世界 SDF SHA-256、镜像标签和可选风速插件 SHA-256。证据方须核对实际执行版本和镜像。 |
| `dem_sha256`, `dem_error_m` | 已加载 DEM 的内容摘要和经证实的高程绝对误差硬界。DEM 文件的标准差与编译阶段的相对地形范围不能直接作为硬界。 |
| `obstacle_coverage`, `obstacles` | 已核验覆盖盒及覆盖区内保守占据盒的清单。盒的顺序为 `[北下界, 北上界, 东下界, 东上界, AMSL下界, AMSL上界]`，单位为米。空清单表示整盒经核验无障碍物。扫掠体离开覆盖范围则未知，接触占据盒则拒绝。 |
| `landing_pad` | 以任务包 home 和该处 DEM 海拔为中心，提供 `radius_m` 和 `elevation_error_m`，涵盖整块可接地的区域。 |
| `battery` | `max_voltage_v` 为电池包电压上界，`soc_error_pct` 为荷电状态与剩余可用能量比例的误差上界，`consumption_error_wh` 为累计耗能折算的误差上界，须覆盖计量误差及最多 500 ms 的采样延迟。`budget.battery_capacity_wh` 必须是可用容量下界。 |
| `suffixes` | 键 `"1"` 为 FW 航点、`"2"` 为后过渡前的 FW 入口、`"3"` 为 MC 回家。每项给出 `entry` 盒、`max_entry_speed_mps`、`legs`、`dynamics`、`airspeed` 和 `failure_revalidation_enforced`。未提供的步骤拒绝。 |

`legs[]` 字段为 `phase`、`end`（北、东、AMSL）、`padding_ne_m`、`padding_alt_m`、`duration_s`、`peak_power_w`。首段起点取当前帧，后续段首尾相接。`dynamics` 提供 `max_bank_deg`、`max_climb_mps`、`max_descent_mps`、`max_acceleration_mps2` 和 `max_angular_rate_dps`。`airspeed` 为 `[最小, 最大]`，MC 可用 `null`。证据须涵盖入口盒内满足门限的实际轨迹及相关误差、选中场景中的风况、转弯和可执行分支。只有宿主已强制约束 FW 失败分支时才能声明 `failure_revalidation_enforced: true`。实时遥测的瞬时值不能证明未来的动力学或占据范围。

`BATTERY_STATUS` 同样要求目标对端且接收不超过 500 ms。能量同时受任务能量预算减去累计耗能上界、以及经校准的荷电状态乘以容量下界约束。累计耗能用 `energy_consumed`（hJ）或正的 `current_consumed`（mAh）乘电压上界换算。无有效计量、计数回退、状态故障或缺少标定时，剩余能量保持未知。任务时间由新启动 PX4 的鲜活启动时钟计算，保守扣除起飞前及航线开始前的所有时间。沿用原有能量与时间储备检查。

### 从 demo runner 生成实时证据

`run_x1_autonomy_demo.py --generate-live-evidence` 默认为关闭。启用时，传入尚未包含 `x1_live_evidence` 的 SIM 任务包，以及选定故障的 `--contingency-scenario`。runner 在编译和站点检查后将带证据的任务包写到 `--output` 下的 `mission-package.json`。同一份包用于实时准入及 `compiled_mission_sha256` 的计算。`--plan-only` 也能生成该文件。`result.json` 的 `.contingency_scenario.live_evidence` 记录证据中已生成和缺失的字段，以及加入证据前的 `base_mission_sha256`。

当前 Iowa 部署可生成 `scope_sha256` 和 `dem_sha256`。DEM 发布 `vertical_sigma_m`。运行地图的 GeoJSON 提供场地边界。离线路线预检使用碰撞网格、建筑和树木等数据来检查它自己的路线与中心垂直起降区。为任意新编译路线签发硬界，仍需覆盖扫掠范围的误差上界、完备的保守占据清单、对应任务 home 的可接地范围，以及分步骤和故障窗口验证的实际轨迹与动力学界。任务包容量也须经校准，并有有效的机载累计耗能计量。缺证字段在生成结果中保持缺失。

宿主验收并取得这些硬界后，可传入 `--live-evidence-bounds /path/to/verified-bounds.json`。文件由 `scope_sha256`、`dem_sha256`、`base_mission_sha256` 和上表的可选证据字段构成。先用同一站点、计划、预算及故障执行一次 `--plan-only --generate-live-evidence` 获取前三个绑定值。runner 检查三者完全匹配。哈希用于适用性校验，硬界的真实性由提供该本地文件的宿主负责。`obstacle_coverage` 和 `obstacles` 一起提供。`landing_pad` 需附 DEM 硬界。生成 `battery` 需要任务包中已有经校准的 `budget.battery_capacity_wh`。只允许为已经验证的飞行阶段提供对应 `suffixes` 条目。

CPU 回放覆盖实时采样、同一执行器的准入和零新增指令。实际 SITL 轨迹需要同一场景的可信机载和任务包证据。collector 现在为 `loiter`、`return`、`land-now` 提供原生接管（见下节），仍不提供换计划（`retask`）执行权限。PX4 原生保底保持原行为。

### SITL collector 的原生接管

`CollectorScenario` 绑定 collector 的遥测连接后创建 `NativeHandoffPort`，并把它作为 `native_port` 传给 `DecisionTick`。没有该端口的实时 tick 仍以 `native_handoff_not_available` 拒绝所有原生动作。

1. **单一写入方**。collector 把路线执行器的 MAVLink 连接包进 `RouteWriteGate`。执行器准入原生动作后调用 `claim_native()`，即 `route_gate.claim()`：先等正在发送的一条路线指令写完，再拒绝之后所有路线写入（`mav.*_send`、`set_mode` 等，记入 `blocked`），然后停止已登记的 OFFBOARD 设定点流。控制权在本架次内不再释放。主循环在下一 tick 或当前原语线程上离开路线循环，记录 `collector_route_superseded`。被放弃的原语线程只剩遥测等待，不能再发指令。
2. **原样下发**。端口只在已认领后发送，且上一条原生指令未定论时拒绝再发。`return` 发 `MAV_CMD_NAV_RETURN_TO_LAUNCH`；`land-now` 发 `MAV_CMD_DO_SET_MODE(1, 4, 6)`；MC `loiter` 发 `MAV_CMD_DO_SET_MODE(1, 4, 3)`；FW `loiter` 用已准入 HoldSite 的半径、切向速度、经纬度和海拔发 `MAV_CMD_DO_ORBIT`。不重试，不替换，不改参数。
3. **ACK 与遥测确认**。ACK 从 reader 的逐命令缓存读取，默认 3 s。确认要求目标对端在 ACK 之后的 `HEARTBEAT` 报告期望模式：RTL 为 (4,5)，LAND 为 (4,6)，Hold 为 (4,3)。Hold 还要求 ACK 之后的 `POSITION_TARGET_GLOBAL_INT` 落在保持点上（FW 为圆心，2 m；MC 为下令时位置，按该 Hold 的水平余量）。发令前已处于期望模式且无其他证据时不算确认。默认 10 s 内未确认、NACK、ACK 超时都记为 `scenario_native_handoff` 的 `unconfirmed` 及原因（`ack_denied`、`ack_timeout`、`mode_not_observed`、`hold_target_not_observed`、`mode_unchanged_unverifiable`）。原生控制权不释放，collector 结束本架次并报告；飞机交给 PX4，PX4 自身保底是最后一道防线。
4. **Hold 驻留**。Hold 的证明耗时上界即驻留上限。到期前 10 s，collector 产生一次 `hold_revalidation` 事件（来源 `executor`）并向 Altair 重新请求。到期仍无新的已分派原生动作时记 `scenario_native_hold_expired`，本架次失败关闭。执行端不会替 Altair 选择后续动作。
5. **结束架次**。认领之后由 `finish_native()` 持续回读并处理新决策，直到确认的 `return`/`land-now` 在 ACK 之后报告 `landed_state=1`。`collector_landing_complete.method` 为 `native_return` 或 `native_land-now`。demo runner 的 `flight.native_handoff` 记录动作、确认状态、被替代的编译步骤和落点到首个本地位置样本的距离。只有确认接管、PX4 原生落地、已执行步骤全部确认，且 `return` 落在任务包着陆圆内时才判 `pass`。
6. **电池恢复检查**。`battery_drop` 窗口结束时仍先恢复 drain 与下限参数并等待 SOC 回升。只有确认的原生 `return`/`land-now` 已接管飞机时，未观察到回升才记为 `recovery.status=not_applicable`，原因 `vehicle_under_native_<action>`；其余情况仍报错。PX4 带容量参数时的 SOC 融合（`battery.cpp` `estimateStateOfCharge`）在武装期间回升极慢，这是归档中检查失败的原因。

任务包 `x1_live_evidence.native` 为可选字段，缺失的部分对应动作保持拒绝。

| 字段 | 证据契约 |
| --- | --- |
| `return` | 键只能是 `"3"`（MC 回家步骤），结构同 `suffixes`，阶段为 `mc → landing`。PX4 VTOL 在 FW 中的 RTL 会先绕 home 盘旋降高再后过渡（`rtl_direct.cpp` 的 MOVE_TO_LOITER/LOITER_DOWN/MOVE_TO_LAND），离开已验证的原航线走廊，本版本不建模，FW `return` 拒绝。 |
| `land-now` | 键只能是 `"3"`。给出 `max_entry_speed_mps`、`padding_ne_m`、`padding_alt_m`、`duration_s`、`peak_power_w`、`dynamics`。只在当前位置位于 home 着陆圆内、且剩余半径大于水平余量时，用该圆内的子圆作为接地区。 |
| `holds` | 至多 8 个 `hold:*` 引用。均含 `mode`、`steps`、`entry`、`max_entry_speed_mps`、`padding_ne_m`、`padding_alt_m`、`duration_s`（进入加驻留的上界）、`peak_power_w`、`dynamics`。FW 另含 `centre`（北、东、AMSL）、`radius_m`、`tangential_speed_mps`、`airspeed`；水平余量须覆盖整个圆。圆心按 DEM 原点转为 WGS-84 再转回，证明锚定在指令实际表示的点上。只有当前步骤与稳态模式匹配的 Hold 才在请求中提供。 |

每个入口门限只对自己的动作生效：Hold 的入口不放宽 `continue` 的入口速度或位置。

字段对照基于 `/data/scratch/cx/altair/autonomy/AUTONOMY-GAP.md`。该文的 `timestamp_monotonic_ms` 对应 `events[].t_ms`，`operator_link` 对应 `telemetry.link.gcs`，剩余计划 hash 和可执行技能分别对应 `remaining_plan_sha256` 和 `capabilities`。回归样本保持起飞前规划与 identity 的原有门禁，飞行中主验收使用 [闭环矩阵](x1-contingency-acceptance.md)。

## ASSUMED_SIM，仅限 SITL，禁止用于实体飞行器

Iowa Clear Noon 的 `configs/simulation/x1-assumed-sim-bounds-iowa.json` 只适用于该固定任务和 `wind_step`、`link_loss`、`battery_drop` 三个独立场景。使用 `scripts/generate_x1_assumed_sim_bounds.py --mode sim --world rw_farm_iowa_w6km__clear_noon --image multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4 --output PATH` 重新生成。生成器读取固定的 4051 机型配置。模板中的 `basis` 和 `justification` 随每组数值一同审阅。

SITL runner 需要显式传 `--generate-live-evidence --live-evidence-bounds CASE.site-bounds.json --assumed-sim-bounds PATH`。地形误差、着陆区、电池及后缀使用假设值。障碍覆盖仍从独立校验的 `.site-bounds.json` 获取。runner 绑定 SIM 任务模式、基础任务和计划、故障窗口、插件、SDF、DEM、机型和镜像。`--plan-only` 核对声明的镜像，启动前再核对不可变镜像 ID。真实、HIL 和无显式运行模式的加载入口拒绝此模板及带标记的任务包。通用 SIM 任务包入口也拒绝带标记的任务包。`mission-package.json` 的 `x1_live_evidence`、`result.json`、决策回执及生成的 `MotionProof` 记录 `basis: ASSUMED_SIM`。归档和评分保留这一标记。

SIM 采用 12 m DEM 假设、16 m 着陆半径和 13 m 着陆高程误差。72 Wh 由 5 Ah × 14.4 V 得到。该验收场景在预武装时设置并读回 `SIM_BAT_DRAIN=1800` 和 `SIM_BAT_MIN_PCT=50`，使正常场景在短任务内有足够的模拟 SOC 区分度。电池故障使用 1 s 和 17% 的现有注入。假定从 PX4 生成到决策至多 500 ms，正常和故障的 SOC 下界分别扣 4 和 54 个百分点，包含模型斜率、滤波与量化余量。接收时钟和 PX4 接收侧时钟两项门限必须同时通过。只有固定 SIM 模型的无计量组合 `energy_consumed=-1`、`current_consumed=0` 可以用带标记的 SOC 下界。时间、能量储备、机型、围栏、地形和障碍检查仍适用。该假设不提供实体电池校准。

后缀只覆盖已编译的 `continue` 路径。受控的试验 runner 在路线步骤失败时停止后续路线写入，PX4 保持原生保底。

原生动作的假设单列在 `native` 与 `px4_native_parameters` 中，各带 `basis` 与 `justification`。预武装时设置并读回 `RTL_TYPE=0`、`RTL_CONE_ANG=0`、`RTL_LAND_DELAY=0`、`MPC_XY_CRUISE=5`、`MPC_Z_V_AUTO_DN=1.5`、`MPC_LAND_SPEED=0.7`、`NAV_ACC_RAD=2`，与电池参数一起核对。

| 假设 | 内容 |
| --- | --- |
| MC `return`（步骤 3） | PX4 MC RTL 按 5 m/s 直飞 home 并保持当前高度，到点后原地 LAND。440 m 留 110 s，末段 45 m 留 20 s，下降 85 s，按机型最高功率计。 |
| MC `land-now`（步骤 3） | 只在 home 着陆圆内且留出 5 m 余量时可用，85 s，最高功率。 |
| `hold:yard-orbit`（FW，步骤 1） | 航点 (320, 0) 上空巡航高度，半径 80 m，15 m/s 的 `DO_ORBIT`。水平余量 120 m，垂向 12 m，进入加驻留 180 s，两倍盘旋功率。 |
| `hold:mc-here`（MC，步骤 3） | 原地 AUTO.LOITER，水平 20 m、垂向 5 m，120 s，最高功率。 |

`configs/simulation/x1-native-handoff-faults-v1.json` 在 MC 回家段放一个 50–58 s 的 `link_loss` 窗口，范围摘要以 `link_loss:mc_return` 登记，用于原生 `return` 的验收。固定任务里 FW 段还没有可准入的原生退出：FW `return` 没有建模，`land-now` 需要 MC，接管后不能 `continue`/`retask`。所以 FW Hold 驻留到期仍会失败关闭。17% 的 `battery_drop` 发生在 FW 段，`return` 因无 FW RTL 证明而以 `unverified_clearance` 拒绝。即使有证明，17% × 72 Wh 也低于 14.8 Wh 的返航储备，仍会因 `energy_or_reserve_limit` 拒绝。
