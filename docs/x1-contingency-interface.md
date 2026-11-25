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
| `loiter` | `{"hold_ref":"hold:a"}` | 只接受请求中的引用。MC 进入当前位置 Hold，FW 使用已证明的圆心、半径、高度和速度 |
| `return` | `{}` | 分派 `rtl()`，按本轮 PX4 路径、过渡与返航走廊证明准入 |
| `land-now` | `{}` | MC 稳态下分派 `land()`，需当下着陆区及下滑净空证据 |
| `abort-transition` | `{}` | 在已验证支持的前过渡场景发出 `vtol_transition("mc")`。后过渡问题保留独立事件和评估，当前 v1 对未验证的中止路径拒绝 |
| `gimbal-retarget` | `{"pitch_deg":-20,"yaw_deg":5}` | 云台直接接口，机型俯仰、偏航及默认滚转须在角限内，需稳态且云台控制权可用 |

`retask` 的 `plan` 保持 `{plan_version:1,goal,steps,report}`。每步仍使用 `{id,skill,params,on_success,on_fail}`。外层 `revision` 是运行版本，不受模型控制。挂接 `X1AirbornePlanAdapter` 时，FW 接受最多四步 X1 巡航或观察原语后接 `back_transition → return_home`，也接受仅后两步。MC 接受 `return_home`。边必须显式、有限并终止。空中 failure edge 需要由 X1 原语执行器在遇到异常时上报新事件，并等待已通过校验的 Altair 决策后再确定后续动作。计算线的 `replace_plan` 映射为 `retask`。`clarify/safe_stop` 不应转译成执行器自行挑选的命令。需要恢复动作时由 Altair 显式产出 `loiter`、`return` 或 `land-now` 等具体动作。

## 运行时联接

1. **消息身份与截止**。一个事件至多开一张票。更新的事件淘汰旧票。执行端使用与事件相同的单调时钟，确认事件、任务、版本和最新帧。票据匹配的有效决定最多消费一次。重启用新的 `run_id`，并重建活动计划与控制权。
2. **可信证据**。执行端用精度未截断的 `RuntimeFrame` 及 `MotionProof`，将路径、转弯包络、剩余时间与电量、过渡 AGL、围栏、地形和障碍裕度绑定到 `frame_id` 与规范 JSON SHA-256。前过渡离场条件、返回路径和着陆走廊需由机型与实际 PX4 配置建模。保护预算与已授权任务包由宿主提供。输入 JSON 的显示字段以及模型解释不参与证明。
3. **提交**。在现有串行控制 tick 验包并提交。X1 原语执行器应提供 `replace_remaining(plan, expected_version)`，将已证明的同一套原语原子替换，失败保留旧版本。`claim_native()` 必须在同一控制权域完成交接，防止普通任务发令与原生模式争用。订阅真实原生模式、ACK 与动作进度。链路允许恢复和任务重新接收前需独立确认原生控制权释放。
4. **回读**。记录关联 ID、决策摘要、动作、`Admission.status/reason`、PX4 ACK、模式与实际结果。`installed/continued` 只说明本地状态改变，`accepted_unconfirmed/sent_unconfirmed` 表示需回读。`transport_denied/transport_uncertain/executor_failed/rejected` 进入宿主诊断。新鲜事件、模型超时、非法计划、传输失败均使用同一验收日志。执行器不从这些信号派生另一条动作。原生保底、Altair 动作和不明归因分别评分。

### 逐决策运动证明

`X1MotionProofBuilder` 绑定通过起飞前检查的 X1 单航点降级结果、原任务包、DEM 和机型。串行 tick 先核对票据、版本及帧时间，再用解码后的决策计算证明。证明与宿主提供的 `ExecutionSweep` 都绑定当前 `frame_id` 和决策摘要。扫掠段使用 DEM 本地北／东坐标和海拔（AMSL）；每段包括进入、退出、转弯与跟踪误差的空间包络，以及有证据支持的耗时和峰值功率上界。规划器只提出动作和引用，不提供这些上界。

扫掠体须落在原任务包运行区域与走廊内，符合任务半径和禁飞区；`continue` 和 `return` 还须留在已验证的单航点走廊及过渡延伸区内。用 DEM 相交网格单元的顶点和已知高程误差界算净空；前向地形冲突可否决。DEM 不覆盖、无可信地形误差界、障碍覆盖缺失或终端着陆区未经确认均不能算作净空。地面接触只在已确认可着陆的覆盖区允许。按各段功率与耗时上界、机型功率和过渡能耗下限算需求；以当前帧的剩余可用能量和剩余任务时间下界校验原有储备。电池百分比不能单独当作剩余 Wh，机型上限也不能代替预计动力学。

| 动作 | 证明器支持范围 |
| --- | --- |
| `continue` | FW 单航点、后过渡进入前、MC 原航线回家；须覆盖终端与所有可执行路径。若 FW 异常会走不同分支，宿主须先停在原证明边界并重新准入，不可沿未证明分支继续。 |
| `loiter` | 请求中已有的 MC 原地 Hold 或 FW 圆盘；须覆盖接入和跟踪范围，且宿主能强制在有界停留期结束前重新验证。 |
| `return` | 须证明实际 PX4 RTL 扫掠体，含 FW 返回所需过渡，再沿原走廊回家着陆。原任务的 MC 回家航段不能直接代替 RTL。 |
| `land-now` | MC 当前位置下降；须有对应着陆区和实际着陆走廊的证据。 |
| `retask` | MC 回家、FW 后过渡回家，或 FW 一处任务包已授权航点后过渡回家。新路径使用同一份任务包围栏／走廊及 DEM；尚不支持多航点、观察原语和未验证分支。 |
| `abort-transition` / `gimbal-retarget` | 前者暂无运动证明；后者仍用原有云台准入，不使用 MotionProof。 |

当前场景 collector 已接入证明入口并读取同一遥测快照中的原始位置，但尚未提供认证过的实际扫掠轨迹、地形误差硬界、障碍覆盖或剩余预算。运动动作因此仍会拒绝；collector 也没有原生模式交接和换计划执行权限。CPU 测试使用显式合成证据，只验证准入逻辑，不证明实际 PX4 轨迹或实飞能力。

字段对照基于 `/data/scratch/cx/altair/autonomy/AUTONOMY-GAP.md`。该文的 `timestamp_monotonic_ms` 对应 `events[].t_ms`，`operator_link` 对应 `telemetry.link.gcs`，剩余计划 hash 和可执行技能分别对应 `remaining_plan_sha256` 和 `capabilities`。回归样本保持起飞前规划与 identity 的原有门禁，飞行中主验收使用 [闭环矩阵](x1-contingency-acceptance.md)。
