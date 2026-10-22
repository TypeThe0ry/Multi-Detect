# GCS 任务包流程与 TUNNEL v2

飞行前由操作员装载任务包，逐任务选择风险等级和失联策略。无人机独立执行任务；地面站仅提供可选监控、中止、暂停／恢复及策略收紧。上传确认不等于解锁、起飞或开始执行。

## 界面与坐标

Fly 和 Plan 页均有“任务包”入口，复用 QGC 的弹窗、控件、配色和文件选择器。表单编辑任务描述、编号、地点、圆形区域及风险标签、区域监督要求、作业范围、高度、时间／能量预算与储备、返航点、风险等级、失联策略与计时、心跳超时和监督步骤。完整 JSON 编辑器保留已存在的路线与步骤计划。未应用的文字／数值草稿先应用再校验；无效输入阻止上传。

地点、返航点和路线采用机载任务坐标系的 `[north_m, east_m]`，不是经纬度。地图中心取点要求操作员明确输入与机载一致的 WGS84 原点，不会自动使用飞机位置。原点仅用于地图换算，不加入任务包未知字段。移动原生地图到目标后，打开面板并选择“地图中心取点”。界面支持取点与圆形区域参数编辑，不提供任意多边形绘制。

`places` 的合同只允许坐标数组，风险标签属于 `zones`。地点的“为地点添加风险区域”会建立一个同中心的独立圆区，保留地点坐标格式；操作员在区域编辑器填写半径、风险标签与监督要求。已有区域不会被覆盖。

| 风险等级 | 建议默认失联策略 |
| --- | --- |
| 低 | `continue` |
| 中 | `continue_then_return` |
| 高 | `return_now` |

飞前可以明确覆盖默认建议。计时策略必须提供非负 `timeout_s`；心跳超时至少为 0.001 秒。飞行中监督器按既有保守顺序接纳收紧，并限制已有返航期限；恢复连接不会解除已锁定的返航／降落。

本地 C++ 校验镜像 `autonomy/mission_package.py` 与 `mission_plan.py` 的准入约束，包括严格字段、有限数值、预算大于储备、区域包含、引用、步骤图、监督步骤及风险／策略值。JSON 重复字段、非法 UTF-8 和整数参数的浮点写法被拒绝。机载端仍使用原始 Python 准入函数做最终检查。未修改 `configs/schemas`。

## 协议

继续使用签名 MAVLink2 `TUNNEL`，`payload_type=42000`，原端点地址及密钥配置不变。现有类型 1–27 的 v1 字节格式完全保留；只有类型 28–36 使用 v2。类型与版本必须匹配，旧端不会误解新任务消息。

外层仍为大端 `>2sBBBBIQH`：`MD`、版本、类型、零 flags、零 reserved、32 位序号、64 位毫秒时间、16 位 body 长度。尾部仍为 HMAC-SHA256 前 16 字节。外层共 36 字节，body 最多 92 字节，每个 TUNNEL 最多 128 字节。

| 类型 | 名称 | 方向 | 大端 body |
| --- | --- | --- | --- |
| 28 | `MISSION_PACKAGE_CHUNK` | GCS → Jetson | `>QHHI32s` + 数据：上传 token、分片编号、总片数、总字节数、SHA-256 |
| 29 | `MISSION_ACK` | Jetson → GCS | `>QBHB` + 最多 80 字节 UTF-8：请求 token、原类型、分片编号、是否接受、说明 |
| 30 | `MISSION_ABORT` | GCS → Jetson | `>QQ`：任务 token、请求 token |
| 31 | `MISSION_PAUSE` | GCS → Jetson | `>QQ` |
| 32 | `MISSION_RESUME` | GCS → Jetson | `>QQ` |
| 33 | `MISSION_POLICY_TIGHTEN` | GCS → Jetson | `>QQBd`：任务 token、请求 token、策略编号、double 超时秒数 |
| 34 | `MISSION_HEARTBEAT` | GCS → Jetson | `>QQ` |
| 35 | `MISSION_TELEMETRY_CHUNK` | Jetson → GCS | 与类型 28 相同；重组后为状态或日志 JSON |
| 36 | `MISSION_TELEMETRY_ACK` | GCS → Jetson | `>QH`：下行 token、分片编号 |

策略编号依次为 `continue=0`、`continue_then_return=1`、`hold_then_return=2`、`return_now=3`、`land_now=4`。任务 token 是任务编号 UTF-8 的 SHA-256 前八字节按大端整数解释。请求／上传 token 非零；控制 ACK 的分片编号为零。

分片数据最多 44 字节，文档最多 65,536 字节，最多 1,490 片。片数与每片长度必须与总长度一致；最终摘要匹配后才做 Python 包准入和装载。采用逐片等待确认；1 秒重试，单片最多五次。ACK 丢失可以幂等重发，token 冲突、内容冲突及乱序不能替换已接受任务。上传中断会在无进展 30 秒后清理。只有最后一片确认成功，GCS 才显示机载装载完成；超时显示结果未知。

GCS 序号由 Unix 毫秒低 32 位初始化，后续保持串行递增并向前跟随时钟，支持普通地面站进程重启。Jetson 使用 32 位串行高水位及请求去重，不因断链自动清空重放保护。严重时钟回拨或超过半个序号周期的间隔需要运行时重新建立受认证会话；不会为此放宽飞行策略。

## Jetson 接入边界

Python 两端定义位于 `operator_protocol.py`、`autonomy/mission_operator_protocol.py` 和 `operator_mavlink.py`；Jetson 实际 UDP 接收路径位于 `operator_udp.py`。

`UdpOperatorSelectionServer` 已接入新消息分派与后台下行轮询。机载任务运行时注入 `MissionOperatorAdapter`，提供两个有实际硬件状态依据的依赖：

```python
adapter = MissionOperatorAdapter(
    mavlink.codec,
    package_loader=atomic_load_package,
    preflight_allowed=aircraft_is_disarmed_and_preflight,
    supervisor=current_supervisor,  # 没有已运行任务时可省略
)
server = UdpOperatorSelectionServer(
    bind_host=config.operator_bind_host,
    port=config.operator_port,
    mavlink=mavlink,
    guard=selection_guard,
    mission_adapter=adapter,
)
server.start_background()
```

`atomic_load_package(package) -> MissionSupervisor` 必须在安装时再次原子检查飞行状态，采用机载现有编译器处理只有 `task_text` 的包，验证编译计划，安装执行器并返回实际使用的监督器。失败时保留原任务。适配器在首片和最终装载前检查准入门；控制事件绑定任务编号，并由监督器采样机载时钟，不能把 GCS 的墙钟当作飞行事件时间。

适配器在调用加载器前保存文档、引用及可选计划快照，拒绝原地修改或替换后的包。上传已含计划时，执行器必须使用该计划；只有任务文字时，保留 `package.plan=None`，将机载编译结果放在 `supervisor.executor.plan`，并再次验证其引用与约束。

当前仓库尚无创建 `MissionSupervisor` 的生产 CLI 运行时调用点。默认 UDP 服务识别 v2，但在未注入上述硬件门与原子加载器时拒绝上传；不能将此默认端点当作已部署任务执行器。现有其他 CLI 管线保持原行为。前台手动调用 `serve_once()` 的宿主还需周期调用 `poll_mission_telemetry()`；后台循环已包含该调用。

## 下行监控与日志

## 提示词任务的机上执行

地面站上传只含 `task_text` 的已签名任务包后，机上会在飞行前重新校验边界，并使用与 onboard policy 相同的 `from_text` 编译路径生成计划。计划以 `kind=plan` 回传，包含 `plan_sha256`、步骤和 `requires_approval`。要求批准时，监督器保持等待状态，不会起飞或推进步骤；地面站发送带相同任务 ID 和摘要的 `approve_plan` 后才开始执行。摘要不匹配、过期或收到 `reject_plan` 都会安全结束任务并保持未起飞。

任务执行期间约每秒发送 v2 `status`，包含当前步骤、技能、进度、计数下限标记、发现数、报警数、预算和链路状态。每个发现和报警都会单独发送记录。最终摘要超过单包预算时拆成 `final_summary_page`，地面站必须收齐页后再展示，不能把不完整摘要当成结果。

状态 JSON 包含 `kind=status`、任务编号、递增序号、监督器判定／原因、有效策略／超时、剩余时间、剩余能量及飞行状态门。未知能量显示“尚未收到”，不把名义预算当作测量值。超过五秒未收到更新，界面明确标为过期；恢复与收紧操作要求新鲜状态。

机载 `OnboardMissionLog` 保留完整 JSONL。下行接入既有 `DownlinkQueue`：只在其后台线程等待逐片 ACK，飞行线程不等待网络；最后一片确认后才推进持久化游标。较大日志下行压缩为最多 1,536 UTF-8 字节的摘要，保留任务／序号／类型及原始 SHA-256、字节数和截断标志。GCS 保留最近 50 条已收到记录，并按任务／序号去重；完整证据仍在机载文件中。

### 下行选择、优先级与吞吐

每条记录都写入机载 `mission.jsonl`（证据不删减），但 TUNNEL 每个确认分片只有 44 字节，链路只承载操作员需要的内容（`DownlinkSelector`，`src/multidetect/autonomy/onboard_mission_log.py`）：

- 总是下行：`mission_started`、`mission_refused`、`plan`、`plan_approved`／`plan_rejected`、`finding`、`alert`、`escalation`、`envelope_violation`、`replay_state_lost`、`final_summary`（大摘要分页）。
- `decision`：只有 `(action, reason)` 与上一条已下行的判定不同才发送，另外按记录时间每 30 秒发一条保活。
- `ground_event`／`ground_event_rejected`：除例行 `heartbeat` 外全部发送。
- 其余（`execution`、`estimates`、`perception_verdict` 等）只留在机载日志；实时进度由约 1 Hz 的 `status` 提供。

游标仍顺序推进，跳过的记录也会被确认越过，所以 `acknowledged_sequence` 能到达日志末尾（SIM 退出前的排空依赖它）。积压中的 `finding`、`alert`、`escalation`、`final_summary`、计划类记录会越过前面的例行记录先发送；重启后可能重复送达，GCS 按任务／序号去重。适配器中优先记录不排在到期的 `status` 之后；普通记录与 `status` 交替。类型 36 ACK 使用独立的速率桶（默认 200/s），不再与普通命令共用 20/s——此前一个约 20 片的状态就耗尽额度，被丢弃的 ACK 让每次传输等待 1 秒重发（SITL run7 只送达 2051 条中的 735 条）。

例行记录（心跳、判定、估计、执行等）只 flush 不逐条 fsync，下行线程读取日志不持有追加锁，避免慢盘时控制循环和链路线程卡顿（run8 曾卡约 5.6 秒）；发现、计划、升级、地面命令和最终摘要仍在返回前 fsync。

### 最终摘要

每个终止路径恰好写一条 `final_summary`：正常完成（`completed`）、监督器锁定返航／降落（含操作员中止、能量／时间储备、失联策略，状态 `ended_early`，在锁定时立即写，不等返航飞完）、计划被拒（`plan_rejected`）、宿主停止任务（`stopped`）。摘要包含已有历史、发现、报警、计数、每个计数步骤的 `count_lower_bounds`、`count_is_lower_bound`（计数步骤未完成覆盖即为 true）、`terminal_action`／`terminal_reason`。SIM 采集器在 `policy.close()` 之前调用 `_drain_sim_operator_link`：先补写摘要，再等待下行确认到日志末尾，最后在所有路径关闭地面链路服务器。

界面主要显示中文判定、原因和日志摘要。勾选“显示机载原始审计字段”可以查看机器原文。地面站离线不暂停机载日志，也不引入正常步骤的地面批准要求。

## 本次验证与构建边界

本机已有 Qt 6.11.1、MSVC 19.44、CMake 4.4.2 和 Ninja。完整文档入口 `scripts/build_multidetect_windows.ps1 -Configuration Release -Parallel 2 -BuildSelfTests` 已尝试，在第 214 行因本工作树缺少 `ground-control/MultiDetectGCS/.venv/Scripts/python.exe` 退出 1；没有配置、下载或安装。

可复现的独立检查入口是 `ground-control/MultiDetectGCS/custom/tests/mission_package_check/CMakeLists.txt`，只需要已安装的 Qt Core 和 C++20 编译器。在已经初始化的 MSVC Developer Command Prompt 内运行，全部产物及临时目录指定在工作树内：

```powershell
$env:TEMP = "$PWD/build/mission-gcs-check/tmp"
$env:TMP = $env:TEMP
cmake -S ground-control/MultiDetectGCS/custom/tests/mission_package_check `
  -B build/mission-gcs-check/repro -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_PREFIX_PATH=<本机已安装的Qt目录>
cmake --build build/mission-gcs-check/repro --parallel 2
ctest --test-dir build/mission-gcs-check/repro --output-on-failure
```

独立检查编译任务包校验器、控制器、wire、原协议及 AUTOMOC，自检覆盖旧 v1 golden vectors、新 v2 golden bytes、上传确认、保守策略与日志重组。Python `tests/test_mission_gcs_contract.py` 可通过环境变量 `MULTIDETECT_MISSION_VALIDATOR` 指向该自检 EXE，逐例比较 C++ 与 Python 准入；运行时需把匹配版本的 Qt `bin` 放在 PATH 前面。

最终验证结果：两个 C++ 自检通过；Python 协议、适配器、监督器、UDP 与跨语言准入共 237 项通过，其中跨语言比较 44 项。三处 QML 经 `qmlformat` 解析通过，`rcc --binary` 资源编译通过；新增构建脚本目标通过 PowerShell AST 语法检查。Ruff 与 `git diff --check` 通过。

QML 解析与资源编译可以独立检查；完整 `qmllint` 语义及原生界面运行仍需要生成的 QGroundControl imports 和完整应用构建。本次未宣称完整 GCS EXE、真实硬件或人工飞行界面验收通过。
