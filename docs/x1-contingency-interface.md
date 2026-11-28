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

## 感知工具（可选）

`x1.contingency.tool.v1` 只读，不调用飞控、任务写口或云台控制口，也不运行新的模型。Altair 仍然选择动作；执行端只准入或拒绝原动作，PX4 原生安全行为保持不变。工具结果只供 Altair 参考，不构成 `MotionProof`，也不提供执行权限。

宿主以 `DecisionTick(..., tool_runner=...)` 为事件和节拍票据开启工具回合。默认保留单轮行为。X1 demo 同时指定 `--decision-port PORT --perception-tools`，并用 `--contingency-scenario`、`--decision-cadence-s` 或两者启用请求入口。工具回合共用原决策工作线程以及原节奏、票据截止和动作准入规则。

1. **等待模型**：同一张 `x1.contingency.request.v1` 票据增加 `perception`：`schema`、`state_sha256`（`state` 的规范 JSON SHA-256）、`call_index`（从 0 起）、`calls_remaining`、`tools` 和 `results`（初始为空）。模型返回一个正常动作对象，或一个工具请求，不得混合：

   ```json
   {"schema":"x1.contingency.tool.v1","tool":"gimbal_state","args":{},"request_id":"sortie7:1","call_index":0}
   ```

2. **读工具**：严格校验字段、白名单、空参数和回显的请求 ID／索引。一次只读一个既有快照。结果是 `{schema, tool, ok, data, source, age_ms, error}`，其中 `schema` 同为 `x1.contingency.tool.v1`。成功时 `data` 为工具专属对象、`error` 为 `null`；失败时 `data` 为 `null`、`error` 为稳定错误码。`age_ms` 以宿主同一单调时钟计算，取本结果所依赖读数的最大年龄；没有可用时间戳时为 `null`。`source` 标识适配器来源，失败时不证明该来源实际产出了数据。
3. **继续同一票据**：宿主重发原 `state`、哈希、ID 和截止时间；递增 `call_index`，按调用顺序附上累计结果，并重算年龄、隐藏过期数据。默认最多 3 次（包括失败读取），每次最多 150 ms，全窗口含模型推理最多 2500 ms，且不延长原票据。用完次数时 `tools=[]`，只允许最终动作。默认拒绝超过 1000 ms 或未来的读数；最终推理期间其看到的成功读数如过期，整轮失败。非法 JSON、身份不符、超额请求或窗口超时都不产生宿主替代动作。迟到的调用结果被丢弃，尚未返回的生产者不会被无限并发调用。

| 工具 | 成功数据与限制 |
| --- | --- |
| `look` | `detections[]`：`class`、`confidence`、归一化 `bbox=[x1,y1,x2,y2]`、`bearing_deg`（机体前向起算，右正）、`elevation_deg`（上正）；`truncated` 标识最多 12 个框。使用已校正内参、同帧 RGB 检测和 100 ms 内的机体云台读回。无图像像素、掩膜或检测器附加元数据。 |
| `target_status` | `track_id`、`visible`（仅当前视觉确认）、`last_seen_age_ms_at_snapshot`（相对 tracker 快照，不是相对当前回合）、`position_wgs84_amsl=[lat,lon,alt]`。位置必须由已有定位来源提供并绑定同一 track；不可用时整条失败。 |
| `gimbal_state` | `angles_deg`、`limits_deg`、`frame`、`device_id`；角度为实测读回（俯仰上正），限位来自机型，坐标系显式；不使用命令目标或 ACK 代替角度。 |
| `terrain_ahead` | `profile[]`，每点为 `distance_m/elevation_amsl_m/clearance_m`；以及 `min_sample_margin_m/minimum_clearance_m`。沿活动 FW 直线连接段在同一 `LocalDemGrid` 采 9 点（含两端）；越界或没有直线段时失败。不代表全走廊净空、DEM 误差界或障碍间隔。 |
| `route_status` | `steps[]`，每项为 `step_id/primitive/endpoint_distance_m`。距离是当前点到首个显式端点、后续端点间的水平直线距离，不是实际飞行长度；无显式端点则为 `null`。原生接管后不展示已失效的原任务路径。 |
| `battery_status` | `percent/remaining_wh/absolute_error_wh`；必须同时具有有效遥测和既有估算器明确发布的绝对误差界，不以任务预算或单边续航下界冒充估计。 |

collector 准入前复查工具窗口和最终模型轮次看到的成功观测的新鲜度。工具协议错误记入 `scenario_decision_error`，其中 `reason` 保存错误码，同一张票据另有一条拒绝回执。实际可读的数据依赖快照来源。demo 当前接入 `gimbal_state`、`terrain_ahead` 和 `route_status`。

训练样本格式、标签归属与反例见 [Altair SFT 工具回合](x1-perception-tools-sft.md)。

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
| `retask` | 默认回放证明支持 MC 回家或 FW 单航点后过渡回家。Iowa `ASSUMED_SIM` live adapter 可验证有界的多航点与观察后缀。两者均使用原任务包围栏、走廊及 DEM。 |
| `abort-transition` / `gimbal-retarget` | 前者暂无运动证明；后者仍用原有云台准入，不使用 MotionProof。 |

CPU 回放使用 `recorded_decisions(...)`、`DecisionTick(..., gimbal_port=None, cpu_replay=True, build_proof=...)`。回放后端只把记录的动作和参数绑定到当前请求；扫掠证据由宿主单独提供，并按本次解码的摘要与当前帧编号核对。回放只向 `replay_commands` 写入通过准入的原始动作对应的命令。FW 盘旋的模拟 ACK 仅用于测试代码路径，不是飞控读回，也不能证明实际运动。普通 collector 不启用该选项，且回放模式拒绝接收真实端口。

### SITL collector 的实时 `continue` 证据

收到 Altair 回包的串行 tick 读取同一个 PX4 快照。当前位置使用来自目标对端、接收不超过 500 ms 的 `GLOBAL_POSITION_INT` 和 `LOCAL_POSITION_NED`。两者启动时钟相差不得超过 500 ms。经纬度与海拔转换到 runner 已加载的 DEM 坐标系，并核对当前编译步骤、入口位置和本地速度。证明从该位置起步，逐段验证扫掠体到回家着陆，包括当前位置、前方地形、DEM 高程误差和障碍覆盖。运动证明继续使用当下的帧编号和已解码决策摘要。成功的回执是 `action=continue / status=continued / reason=no_new_command`。

在已授权的 Iowa `ASSUMED_SIM` 路径成功安装 `retask` 后，原路线的 `suffixes` 几何不再用于 `continue`。collector 从串行路线槽读取当前原语和剩余的已安装指令，保留安装时解析出的目标、找点误差和限时，并从新遥测位置构建证明，不重新降低计划或移动航点。盘旋同时覆盖进入段与带跟踪余量的完整圆盘；后过渡覆盖所有退出航向的有界位移；正向路径证明到经核验的回家着陆点。扫掠仍须通过任务包运行区、走廊、禁飞区、DEM 与误差上界、障碍覆盖及终端着陆检查，能量与时间下界仍取自当前遥测。`MotionProof` 另外绑定已安装计划的 SHA-256 和运行修订号，与宿主当前状态任一不符即拒绝；这不是 Altair 请求中的剩余进度摘要。未能定位当前已安装原语、遥测过期、覆盖缺失或盘旋越出走廊时不发新指令。该路径不扩大 `ASSUMED_SIM` 的适用范围，不更改 FW 空速门槛，也不将 `continue` 变成备用动作。

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

CPU 回放覆盖实时采样、同一执行器的准入和零新增指令。实际 SITL 轨迹需要同一场景的可信机载和任务包证据。collector 为 `loiter`、`return`、`land-now` 提供原生接管，并在限定的 SITL 场景提供 `retask`（见后文）。PX4 原生保底保持原行为。

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
| `return` | 键只能是 `"3"`（MC 回家步骤），结构同 `suffixes`，阶段为 `mc → landing`。FW 不复用此后缀；其专用证明和候选镜像要求见下文。 |
| `land-now` | 键只能是 `"3"`。给出 `max_entry_speed_mps`、`padding_ne_m`、`padding_alt_m`、`duration_s`、`peak_power_w`、`dynamics`。只在当前位置位于 home 着陆圆内、且剩余半径大于水平余量时，用该圆内的子圆作为接地区。 |
| `holds` | 至多 8 个 `hold:*` 引用。均含 `mode`、`steps`、`entry`、`max_entry_speed_mps`、`padding_ne_m`、`padding_alt_m`、`duration_s`（进入加驻留的上界）、`peak_power_w`、`dynamics`。FW 另含 `centre`（北、东、AMSL）、`radius_m`、`tangential_speed_mps`、`airspeed`；水平余量须覆盖整个圆。圆心按 DEM 原点转为 WGS-84 再转回，证明锚定在指令实际表示的点上。只有当前步骤与稳态模式匹配的 Hold 才在请求中提供。 |

每个入口门限只对自己的动作生效：Hold 的入口不放宽 `continue` 的入口速度或位置。

字段对照基于 `/data/scratch/cx/altair/autonomy/AUTONOMY-GAP.md`。该文的 `timestamp_monotonic_ms` 对应 `events[].t_ms`，`operator_link` 对应 `telemetry.link.gcs`，剩余计划 hash 和可执行技能分别对应 `remaining_plan_sha256` 和 `capabilities`。回归样本保持起飞前规划与 identity 的原有门禁，飞行中主验收使用 [闭环矩阵](x1-contingency-acceptance.md)。

## ASSUMED_SIM，仅限 SITL，禁止用于实体飞行器

Iowa Clear Noon 的 `configs/simulation/x1-assumed-sim-bounds-iowa.json` 只适用于该固定任务与已登记的 `wind_step`、`link_loss`、`battery_drop`、`target_found` 单故障作用域。使用 `scripts/generate_x1_assumed_sim_bounds.py --mode sim --world rw_farm_iowa_w6km__clear_noon --image multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4 --output PATH` 重新生成。生成器读取固定的 4051 机型配置。模板中的 `basis` 和 `justification` 随每组数值一同审阅。

SITL runner 需要显式传 `--generate-live-evidence --live-evidence-bounds CASE.site-bounds.json --assumed-sim-bounds PATH`。地形误差、着陆区、电池及后缀使用假设值。障碍覆盖仍从独立校验的 `.site-bounds.json` 获取。runner 绑定 SIM 任务模式、完整基础任务包和规范化计划、故障窗口、插件、SDF、DEM、机型和镜像。只规范化模型的 `goal` 文案和步骤 ID（含分支引用），不改变任务图或动作。原始计划仍记在 `result.json` 的 `plan_json`。`--plan-only` 核对声明的镜像，启动前再核对不可变镜像 ID。真实、HIL 和无显式运行模式的加载入口拒绝此模板及带标记的任务包。通用 SIM 任务包入口也拒绝带标记的任务包。`mission-package.json` 的 `x1_live_evidence`、`result.json`、决策回执及生成的 `MotionProof` 记录 `basis: ASSUMED_SIM`。归档和评分保留这一标记。

SIM 采用 12 m DEM 假设、16 m 着陆半径和 13 m 着陆高程误差。72 Wh 由 5 Ah × 14.4 V 得到。该验收场景在预武装时设置并读回 `SIM_BAT_DRAIN=1800` 和 `SIM_BAT_MIN_PCT=50`，使正常场景在短任务内有足够的模拟 SOC 区分度。电池故障使用 1 s 和 17% 的现有注入。假定从 PX4 生成到决策至多 500 ms，正常和故障的 SOC 下界分别扣 4 和 54 个百分点，包含模型斜率、滤波与量化余量。接收时钟和 PX4 接收侧时钟两项门限必须同时通过。只有固定 SIM 模型的无计量组合 `energy_consumed=-1`、`current_consumed=0` 可以用带标记的 SOC 下界。时间、能量储备、机型、围栏、地形和障碍检查仍适用。该假设不提供实体电池校准。

后缀只覆盖已编译的 `continue` 路径。受控的试验 runner 在路线步骤失败时停止后续路线写入，PX4 保持原生保底。

原生动作的假设单列在 `native` 与 `px4_native_parameters` 中，各带 `basis` 与 `justification`。预武装时设置并读回 `RTL_TYPE=0`、`RTL_CONE_ANG=0`、`RTL_LAND_DELAY=0`、`RTL_DESCEND_ALT=73`、`FW_AIRSPD_MIN=12`、`FW_AIRSPD_TRIM=15`、`FW_AIRSPD_MAX=18`、`MPC_XY_CRUISE=5`、`MPC_Z_V_AUTO_DN=1.5`、`MPC_LAND_SPEED=0.7`、`NAV_ACC_RAD=2`，与电池参数一起核对。

| 假设 | 内容 |
| --- | --- |
| MC `return`（步骤 3） | PX4 MC RTL 按 5 m/s 直飞 home 并保持当前高度，到点后原地 LAND。440 m 留 110 s，末段 45 m 留 20 s，下降 85 s，按机型最高功率计。 |
| MC `land-now`（步骤 3） | 只在 home 着陆圆内且留出 5 m 余量时可用，85 s，最高功率。 |
| `hold:yard-orbit`（FW，步骤 1） | 航点 (320, 0) 上空巡航高度，以 `DO_ORBIT` 设置 80 m 半径；15 m/s 是机型的校准配平空速，不是固定翼 orbit 命令的速度覆盖。水平余量 120 m，垂向 12 m，进入加驻留 180 s，两倍盘旋功率。 |
| `hold:mc-here`（MC，步骤 3） | 原地 AUTO.LOITER，水平 20 m、垂向 5 m，120 s，最高功率。 |

`configs/simulation/x1-native-handoff-faults-v1.json` 在 MC 回家段放一个 50–58 s 的 `link_loss` 窗口，范围摘要以 `link_loss:mc_return` 登记，用于原生 `return` 的验收。`configs/simulation/x1-mc-battery-faults-v1.json` 另登记同一航线的 50–62 s 电池窗口；此绑定不提供低电量放行。17% 的 `battery_drop` 仍不是固定翼返航特例：未建立证明仍拒绝；即使有证明，17% × 72 Wh 也低于 14.8 Wh 的返航储备。接管后仍不能 `continue`/`retask`。

### 多段固定翼走廊

`configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json` 是宿主校验的 `ASSUMED_SIM`、`SITL-only` 模板。它只在显式允许 `ASSUMED_SIM` 的 SIM 入口与无签名任务包绑定，同时校验任务图、运动作用域和场地证据。模型侧沿用现有计划和动作格式。当前绑定的任务是 K 短折线：75 m AGL，`home (0,0)`、`poi:first-beacon (60,0)`、`poi:second-beacon (30,52)`，三条 FW 腿合计 180.07 m；home、走廊矩形和预算不变。`continue` 的 MotionProof 从当前遥测位置逐腿重建扫掠，覆盖当前剩余腿、后续 FW 腿（含计划内飞回 home）、后过渡跑出区、MC 返航和着陆。静态准入保留原有规划检查，并以 `X1_ENERGY_PROOF` 提前拒绝证明层预算不足的走廊。

K 的净空结果为：最坏 DEM 余量 +8.024 m（后过渡末片），最低预测过渡 AGL 48.024 m，围栏最小余量 +48.384 m，FW 巡航最小障碍余量 +59.844 m，T2/T3 最小 DEM 余量 +10.754 m；旧 47 m 几何在同一后过渡扫掠上为 −23.95 m，继续作为拒绝对照。

**覆盖边界与验收口径**：正式验收只要求 FW 巡航阶段的 `continue` 被接纳。起飞/MC、前过渡/MC、`back_transition`、MC 返航/approach 没有进入 `_corridor_continue_sweep` 的证明入口，如实记为“走廊证明器未覆盖”；这些阶段被拒绝不表示 K 几何越界，也不作为通过条件。为它们增加走廊 `continue` 证明的设计选项 B 不做。宿主仍按原样拒绝这些请求，不改动模式门、判定或余量常数。

**能量警示**：K 在请求 #12 的模型能量余量只有 0.085 Wh（已扣 14.8 Wh 储备）。它只用于机制与净空验证，不能作为可泛化航程、其他风况或真实机体的余量依据。所有数值均为 SITL/`ASSUMED_SIM` 假设。

当前 OP-A 采用 500 W 的 FW 腿证明功率和 59.2 Wh 的名义可用能量（74 Wh 扣除 14.8 Wh 储备）。首个 FW tick 的可用量还要扣除爬升、前过渡，以及任务包额外要求的储备。500 W 是模板中的仿真假设，依据推力与转速反推。下表沿用 `REPORT-power500.md` 的证明层估算，standoff 门关闭，MC 返航距离项取 505 m，FW 按 12 m/s 对称去回。

| H = AGL + 地形起伏 | 最大可证明单程 FW 距离（约） |
| ---: | ---: |
| 50 m（40 + 10 m） | 755 m |
| 85 m（60 + 25 m） | 493 m |
| 110 m（85 + 25 m） | 305 m |

表中距离按 `max_one_way_m(layer="proof", ...)` 的对称航程折算。折线应按各腿实际路程累计，并以 `x1_static_violations` 检查整条航线。3 km 单程／10 分钟目标超出 OP-A 的范围。OP-B 是设计中的另行评审选项。

静态预算按配平空速 15 m/s 减去机型声明的 3 m/s 风裕度，对去程和回程都取 12 m/s。实时证明按各腿航向投影新鲜风估计（至多 1.5 s），逆风取机型风裕度与该腿逆风分量加模板 2 m/s 不确定度的较大者，再把预算地速封顶在 12 m/s。去程顺风在反向腿上计为逆风，顺风不抵扣预算。缺少新鲜估计时回退至静态风裕度，地速低于 6 m/s 时以 `WIND_EXCEEDS_RETURN` 拒绝该证明。使用 `wind_step` 的试验仍须遵守绑定场景的风界和故障作用域。

计划内 FW 飞回 home 属于 `continue` 路线。走廊原生 `return` 和 FW Hold（`loiter`）要求经审阅的 v2 FW RTL 清单及场地、镜像、参数和测量证据。FW Hold 还要证明驻留后的完整返航连接。目前走廊审阅清单为空，这两类动作关闭。`back_transition` 后的 MC 终端仍可按绑定证据申请 `land-now` 或已登记的 MC Hold（`loiter`），分别检查着陆区或驻留范围，并保留现有准入与复核条件。PX4 保留原生保底。

### 固定翼 Hold 后的原生 RTL（仅限经审阅的 SITL 候选）

Altair 请求 `return {}`，执行器只接受或拒绝。准入后仍发送同一个原生 `MAV_CMD_NAV_RETURN_TO_LAUNCH`；没有额外航点、补发动作或自动降级方案。须存在未到期的 FW Hold、已确认的 `DO_ORBIT` 目标、同一飞控的新鲜 `AUTO.LOITER` 和 `HOME_POSITION`，并通过既有遥测与动力学门槛。`DO_ORBIT` 的速度参数不是飞控速度证明。FW `land-now` 仍被 MC 互锁拒绝；FW Hold 上的再次 `loiter`（包括续驻同一站点）没有证明转接及新驻留，故拒绝。未准入且 Hold 到期仍报 `hold_residence_expired`，PX4 保持原生保底。

候选清单仅支持各自独立绑定的 Iowa `wind_step`（Case C）或 `target_found`（空中换装）证据范围，不接受组合故障或跨范围借用。换装后，只有候选已核验、串行路线槽的当前索引、计划摘要和运行修订一致、处于 FW 且新鲜位置和地速满足已审阅的 Hold 入口时，才向 Altair 提供同一个经审阅的 FW Hold。不会把已安装原语的索引套到原路线的 `suffixes` 或 `native.return`。`continue` 仍使用绑定已安装计划的证明且不发指令；原生 Hold 一旦接管，`continue` 和 `retask` 仍无权夺回路线。事件和 cadence 处理回复时，都用新的快照重新证明 Hold 或原生返航；票据有效不代表动作获准。

只对审阅过的 PX4 提交 `53afcc82b2a51a90424ff7e2c35583e491587eb5` 建模。尾座机 4051、`VT_TYPE=0`，`RTL_TYPE=0`，无改选目的地的 rally/approach 或避障绕行，`RTL_APPR_FORCE=0`、`RTL_PLD_MD=0`。`rtl.cpp` 的 FW 返航高度为当前高度与 `home + RTL_RETURN_ALT` 的较大者；`rtl_direct.cpp` 保留已建立的盘旋或使用激活点盘旋爬升，随后获取 home 周围的 `RTL_LOITER_RAD` 轨道，盘旋降至 `min(返航高度, home + RTL_DESCEND_ALT)`。Iowa 在武装前写入并读回 `RTL_DESCEND_ALT=73 m`，约 85 m AGL 的 FW 巡航仍高于盘旋目标。关闭 MC 距离锥并保持 `RTL_LAND_DELAY=0`。FW 不额外驻留，Case B 的 MC 直返仍在到达 home 后跳过下降盘旋并直接 `LAND`。`MOVE_TO_LAND` 按地速和 `VT_B_DEC_MSS` 扩大接收半径且**不检查高度**。尾座机按俯仰角或 `VT_B_TRANS_DUR` 切到 MC，再移至 home 上方并开始降落；还须核对 `MPC_XY_CRUISE`、`MPC_Z_V_AUTO_DN`、`MPC_LAND_SPEED`、`NAV_ACC_RAD` 等，不把设定值当作实际速度保证。

PX4 `53afcc82` 的 `rtl_direct.cpp` 在 RTL 激活时重置巡航空速；`mission_block.cpp` 将该值传给定位设定点，`FixedWingModeManager.cpp` 对非正值使用默认空速。`navigator_main.cpp` 的固定翼 `DO_ORBIT` 分支使用 `param1` 或 `NAV_LOITER_RAD` 决定半径，不读取 `param2` 指定速度。`PerformanceModel.cpp` 以 `FW_AIRSPD_TRIM` 为基准、以 `FW_AIRSPD_MIN` 计算负载/襟翼修正后的下限；`FwLateralLongitudinalControl.cpp` 以 `FW_AIRSPD_MAX` 限制校准空速设定值，但在防失速下限超过上限时优先下限，并使用 EAS/TAS 转换。参数读回不是实测真空速保证；实测真空速超过机型 18 m/s 加独立估算的传感器噪声容差时，拒绝该批证据，不抬高机型上限。

**这是预武装可复现性约束，不是动力学修复。** 归档格 `o39-w6-b1800-a85-r1` 的原始 ULog `P` 已记录生效值 `MAX=18`、`MIN=12`、`TRIM=15`、`FW_WIND_ARSP_SC=0`；`Q` 的 `0x01` 系统默认值 `MAX=20`、`MIN=10` 并非本趟生效值。旧矩阵仍记录了约 23 m/s 的真空速峰值；不能把它归因于编译默认值，也不能从新 pin 推断复飞将合规。`FW_WIND_ARSP_SC=0` 只关闭额外的风估计下限增量，不关闭航向/最低地速引导。降低 TRIM 仅影响使用默认目标、且未被引导或防失速下限抬高的阶段；降低 MAX 只能降低名义 CAS/EAS 设定值上限，实际 TAS 还取决于 EAS/TAS 比值、跟踪误差和瞬态。没有同步日志证据前不调整 TRIM/MAX，不降低 MIN 或更改 PX4 防失速行为。

建议按阶段对齐 `airspeed_validated` 的 CAS/TAS/来源、TECS 的空速目标与响应、风估计、滚转、爬降和过渡边界；必要时与独立的 SIM 真值或独立校验的皮托记录交叉验证。若后续独立数据能约束 EAS/TAS 比值上界 `r` 和正向 TAS 跟踪误差上界 `e`，可用 `(18 − e)/r` 筛选名义 CAS/EAS 上限候选；这不是新 pin 值，不能绕过防失速下限、航向可行性或真空速验收。噪声容差仅用于测量误差，不吸收飞行瞬态、日志缺口或未解释的超速。

扫掠从实时位置覆盖 Hold 全圆及捕获转弯、返航连接、home 盘旋下降、转入 `MOVE_TO_LAND`、后过渡漂移、MC 回中和从残余速度状态开始的着陆。`FW_R_LIM` 和机型空速上界只给出转弯半径的可行性下限，跟踪、垂直偏离和阶段完成时间仍须由实测限定。功率缺少有效电流时，SIM 审阅可采用 Iowa 后缀模型的 344 W FW、750 W 后转及 MC 上界，并在验证记录中标为 `ASSUMED_SIM`。扫掠超出任务走廊、围栏、障碍覆盖或 DEM 误差后的高度余量，或无法在剩余时间、能量及现有返航储备内完成时，均拒绝。末段整个落点包络必须落在测量过的 pad 内；仍使用现有 40 m 空中 AGL 下限。

**Case C 变体 SITL 验收（本文档未运行）**

1. 基线归档为 `/data/scratch/cx-wc-sampling/.tmp/iowa-native-handoff.AonhPrm1/live-wind_step-hold`。其镜像运行的是 PX4 `83c4f4e580558816f0e398918f66358988004696`（`v1.18.0-beta2`），不会被本证明认可。先单独提交并审阅包含上述 `53afcc82…` 的不可变候选镜像；核对 4051、固件构建来源、无其他 RTL 分支、配方和电池模型、不会在武装后更改的完整参数快照，以及同一任务、DEM、障碍清单、风注入与皮托插件。不要替换既有 Iowa 镜像标签或证据记录，也不要把 CPU 夹具作为实测证据。
2. 用候选镜像的独立证据覆盖所有准入航向和风、采样到命令的间隔、轨道捕获及 `MOVE_TO_LAND` 高度偏差、过渡漂移和高度损失、MC 残余横向速度至触地的偏差，以及每阶段最迟完成时间。将依据保存为宿主 `NAME.validation.json`，逐项标明实测 `MEASURED_SIM` 或两项功率的 `ASSUMED_SIM` 推导，由审阅者核对并把 SHA-256 和相同任务的 `motion_scope_sha256`、镜像 ID、实际参数写入 `NAME.json`（字段见 `FwRtlBounds`）。`--fw-rtl-host-manifest NAME.json` 选择该单独镜像；原假设文件仍是范围与基线锚点。`NAME.validation.json` 顶层还须包含 `fw_airspeed_parameters`，以三项经读回与 ULog 核对的值（MIN/TRIM/MAX = 12/15/18 m/s）绑定清单；另记录真空速样本极值、噪声容差的推导与接受/拒绝结果。运行前核对 `scenario_fw_rtl_readback`，不匹配时不得武装；不从覆盖格向公用 MC RTL 写入 `RTL_RETURN_ALT`。仅有 source commit、镜像摘要或一次成功飞行不能建立全部边界。
3. 在获授权的 SITL lane，以 `.venv-vlm/bin/python` 运行 `scripts/run_x1_autonomy_demo.py`。沿用归档 `result.json` 的原始任务输入、`wind_step` 计划、同一风插件和 `wind_step.site-bounds.json`；参数包括 `--inject-fault wind_step --generate-live-evidence --assumed-sim-bounds configs/simulation/x1-assumed-sim-bounds-iowa.json --live-evidence-bounds CASE.site-bounds.json --fw-rtl-host-manifest NAME.json`。使用新输出目录与已批准的空闲 slot（`MULTIDETECT_SITL_SLOTS >= 3`，不用 slot 3、4）。Case C 回放后端按事件只回放归档中的 `loiter {"hold_ref":"hold:yard-orbit"}` 和 `return {}`，并回显本次票据；不在执行器中编写动作选择规则。
4. 留存 `result.json`、决策回执、MAVLink 和 PX4 ULog。必须依次看到 `DO_ORBIT` ACK、`AUTO.LOITER (4,3)` 及正确 Hold 目标、返航准入、原样 RTL 与 `AUTO.RTL (4,5)` 的飞控确认。用位置及 setpoint/VTOL 主题核对返回高度、home 盘旋半径/下降、`MOVE_TO_LAND`、FW→MC、回中及下降，并用轨迹与扫掠逐段对拍。最终须观察 `ON_GROUND` 且实际落点在 16 m pad 内，无 Hold 到期和模式替代；任何阶段/证据缺失不可计为通过。

## 持续决策循环（continuous decision loop）

`run_x1_autonomy_demo.py --decision-port PORT --decision-cadence-s SECONDS` 可在无注入故障的任务上启用节拍，也可与 `--contingency-scenario` 一起使用。默认 `0`，只在原事件入口请求 Altair。启用后，collector 在各飞行阶段继续轮询；只有同一份 `latest_control_state` 快照经同一单调时钟截点核对后，同时确认 PX4 心跳为 `ACTIVE`、已武装且 `EXTENDED_SYS_STATE.landed_state=2`（`IN_AIR`），才提交节拍请求。`TAKEOFF`、`LANDING` 及飞行状态未知时不提交节拍；执行器仍独立复核。稳态巡航间隔为 `max(1, SECONDS)` 主机单调时钟秒，其他合格阶段为其一半。间隔从最近一次提交计算，真实事件可以越过限频。忙碌期间不积累节拍，也不在恢复时补发。

节拍沿用 `x1.contingency.request.v1` 和同一份 `state`，增加顶层 `trigger: "cadence"` 与 `mission_progress`。事件请求保持原 wire 格式。两类请求共用同一 `request_id` 序列。节拍的 `event_id` 是 `2^52 + 请求序号`，用于满足 v1 回包的正整数关联字段。真实事件 ID 从 `1` 起并保留在 `state.events` 和 `state.latest_event_id` 中。Altair 回显顶层的 `request_id`、`event_id` 及 `state` 的 `mission_id`、`revision`。

| `mission_progress` 字段 | 含义 |
| --- | --- |
| `step_index`、`step_id`、`primitive` | 当前编译步骤索引、源计划步骤 ID 和执行原语。原生接管以后仍可看到被暂停的原计划位置。 |
| `remaining_plan_sha256` | 编译计划摘要和从当前执行原语起的固定后缀的规范 JSON SHA-256。与请求顶层同名字段相同。 |
| `phase` | `takeoff`、`transition`、`cruise`、`approach` 或 `hold`。 |
| `next_waypoint_distance_m`、`home_distance_m`、`eta_s` | 新鲜本地 NED 遥测给出的水平距离、按向航点的当前闭合速度估计的抵达秒数。遥测缺失或闭合速度不足时对应值为 `null`。 |
| `remaining_energy_wh`、`remaining_time_s` | 同一拍可用的可信预算下界，缺失时为 `null`。 |
| `recent_receipts` | 最近至多四条事件或节拍的准入结果，包含 IDs、触发方式、动作、状态和原因。 |
| `hold` | 当前已派发的驻留、引用、剩余主机秒数，以及原生动作和读回状态。 |

任一时刻最多保留一个在途后端调用，节拍票据截止时间为提交后 3 秒。步骤、PX4 模式、修订、飞行状态或更晚的事件改变时，旧票据立即失效。新事件先撤销节拍票据，等单工作线程释放后优先提交仍在有效窗内的事件。后端调用若无法即时取消，旧回复只会被丢弃。超时、无效 JSON、失效票据及准入拒绝均无新指令，当前已通过准入的航线或 PX4 已接管的状态继续生效。现有 PX4 保护继续作为最后一道保障。

启用感知工具后，工具回合保留同一张节拍票据的 `trigger`、合成 `event_id` 和 `mission_progress`。窗口取原票据截止与工具总预算中较早者。新事件撤票后，正在进行的读取或推理可以结束，旧窗口随后停止，不再开启下一回合或提交动作。后端尚未返回时仍占用唯一调用位置。

节拍仍从同一个执行入口经过 MotionProof、适用的 ASSUMED_SIM 证据和原生接管。缺少生命周期子段或当前位置的受证据约束后缀时，`continue` 可收到拒绝回执，已在执行的航线继续。一般节拍写一条 `scenario_decision_receipt`，带 `trigger`、IDs、阶段、步骤、结果和 `latency_ms`。延迟是从提交到 collector 处理回复或撤票的主机毫秒数。`continue` 无新指令，回执省略原回包和完整证明。仅节拍 `continue` 的 `px4_owns_or_flight_state_unknown` 和 `flight_mode_or_position_unverified` 拒绝保留在 `recent_receipts` 而不逐条写日志；结束时在 `scenario_decision_cadence_summary` 按原因计数。净空、空速、事件回执及原生接管证据照常记录。

## in-flight retask and evidence-driven tasking

Altair 可在巡航期间根据新观测提交完整的 **剩余** `MissionPlan v1`。collector 在当前原语结束后、下一个原语发命令前，用新鲜的机载状态审查该后缀。已完成的原语保留。原子换装同时设置起始游标和递增后的运行 `revision`。日志先记 `scenario_decision_receipt`，再记 `scenario_plan_replaced`。与原决策不同的模式、事件或运行修订，以及迟到回复，均不得写入执行槽。周期请求可在机载模式和命令归属稳定时跨一个相邻原语边界，沿用同一请求的时限。

| 场景 | 可接受的后缀 |
| --- | --- |
| FW | `fw_goto_waypoint`、`loiter_observe`、`gimbal_stare_at`、`x1_search_leg` 等巡航技能，之后显式 `back_transition → return_home` |
| MC | 可选 `x1_mc_reposition`，之后显式 `return_home` |

`x1_search_leg` 引用原任务包内含 2–4 个航点的命名路线。`loiter_observe` 由 Altair 指定圆心、半径、驻留时长和高度。云台观察要求托管云台会话就绪。`return_home` 由既有 X1 原语降解为归航和着陆。候选扫掠体必须留在原任务包的围栏与走廊内，满足 DEM/障碍物、AGL、姿态过渡、能量及时间储备、着陆点准入。`retask_geofence`、`energy_or_reserve_limit`、`transition_interlock` 等拒绝回执均对应零新增命令。运行时逐步检查超时与遥测确认。发生执行异常时停止推进后缀，PX4 原生安全行为仍是最后一层。

等待观测帧后、首条替换指令下发前，再用当时的 PX4 模式、VTOL 状态、新鲜位置和预算对照同一个后缀。若归属或模式变化、缺少证据，或复核失败，记录 `collector_x1_retask_dispatch_blocked`，不下发替换指令，不修复计划，也不回退执行旧路线。换装后不会复用旧路线的运动证明。

`target_found` 在故障窗提供一条带 `fault` 来源的合成观测，包含类别、机体 FRD 方位和俯仰、WGS84 位置、AMSL 高度及误差上界。仅在启用 Iowa 空中换装证据范围时，collector 等到 FW 原语执行完毕、下一条原语尚未下发的串行边界才首次取样：仍须在注入窗内，且有不超过 500 ms 的位置、处于 FW 模式，并距原航线出航终点不超过 28 m。该边界也是换装准入点；在前一原语提前取样会让同一条读数在准入前过期。观测保留首次取样时间，不把旧读数改成新时间。边界只短等快速回复，每次回复就绪后用新快照复核；未能在边界获准则不下发替换指令。

事件与周期请求均通过 #70 的 `ToolRunner` 读 `look` 和 `target_status`，票据上限 3000 ms、工具窗口上限 2500 ms、读数年龄上限 1000 ms，均不延长。`finding:1` 仅在同一张票据实际读取了匹配的 `target_status`、且边界处仍为同一条新鲜、误差有界的观测时可用。`scenario_observation` 只写入审计日志，不发送给模型。换装后，`route_status` 显示已安装的后缀。

当前准入范围是固定 Iowa `ASSUMED_SIM` 假设及一次空中替换。假设和运动证明均带范围摘要。`evidence-follow` 在 FW 巡航末段报告原航线外的地面标记，recorded backend 请求接近、盘旋观察、后过渡、返航和降落。周期变体在普通周期和 `target_found` 事件票据上回复 `continue`，在标记后的下一张周期票据提交 `retask`；真实新事件仍可抢占周期票据。验收脚本要求同一修订有 FW 穿越、盘旋持续驻留、MC 过渡、回家稳定及落地的 PX4 遥测。Iowa CPU 预检与 SITL 遥测验收分别运行。命令见 lane 的 `.tmp/x1-live-retask-port-REPORT.md`。
