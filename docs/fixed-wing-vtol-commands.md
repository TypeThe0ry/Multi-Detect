# PX4 固定翼 / VTOL 指令层

这层代码把“飞到某处、盘旋、转换和降落”编码为 PX4 能理解的 MAVLink
`COMMAND_LONG`。它不依赖连接，命令编码在
`src/multidetect/onboard_runtime/fw_vtol_commands.py`，真机发送和状态读取在
`onboard_runtime/vehicle.py`，仿真教师执行器在 `sim_teacher/fw_executor.py`。

## 固定翼的运动语义

固定翼没有多旋翼的悬停能力。虽然 PX4 在 `OFFBOARD` 下接受
`SET_POSITION_TARGET_LOCAL_NED`，固定翼会飞向目标并按固定翼导航逻辑继续飞行，
不能把它当成“停在目标点”。需要明确保持航迹时优先用：

* `MAV_CMD_DO_REPOSITION`（192）：飞到 WGS84 点，并带盘旋半径；
* `MAV_CMD_DO_ORBIT`（34）：围绕 WGS84 中心盘旋；
* 任务飞行用 `NAV_WAYPOINT` 和 `NAV_LOITER_*`。

`loiter_at()` 使用 orbit，`reposition()` 使用 reposition。调用结果是
`CommandResult`：只有收到 `MAV_RESULT_ACCEPTED`（0）或 `IN_PROGRESS`（5）才是
`accepted`；发送异常、拒绝和超时都返回明确失败状态，不会把异常抛出到飞行状态机外。

## 已核实的命令参数

表中的参数顺序是 `param1..param7`，位置和高度使用 WGS84 纬度、经度、米。

| 命令 | 编号 | 关键参数 | 来源/状态 |
| --- | ---: | --- | --- |
| `NAV_LOITER_UNLIM` | 17 | p3 半径，p4 航向，p5..7 中心 | 本机 pymavlink common.xml 已核实 |
| `NAV_LOITER_TIME` | 19 | p1 秒，p3 半径，p5..7 中心 | 已核实；p4 的固定翼退出语义由 PX4 处理 |
| `NAV_TAKEOFF` | 22 | p1 俯仰角，p4 航向，p5..7 目标 | 已核实 |
| `NAV_LOITER_TO_ALT` | 31 | p1 是否要求航向，p2 半径，p5..7 中心 | 已核实 |
| `NAV_VTOL_TAKEOFF` | 84 | p4 航向，p5..7 目标 | 已核实 |
| `NAV_VTOL_LAND` | 85 | p1 选项，p3 进近高度，p4 航向，p5..7 着陆点 | 已核实 |
| `DO_ORBIT` | 34 | p1 半径，p2 速度，p3 yaw 行为，p4 圈数，p5..7 中心 | 仓库 QGC 已使用；当前 common.xml 未提供 enum，具体 PX4 语义需核实 |
| `DO_CHANGE_SPEED` | 178 | p1 速度类型，p2 速度，p3 油门 | 已核实 |
| `DO_LAND_START` | 189 | p1..4 为空，p5..7 可选任务降落定位 | 已核实 |
| `DO_REPOSITION` | 192 | p1 地速，p2 flags，p3 盘旋半径，p4 航向弧度，p5..7 目标 | 已核实 |
| `DO_VTOL_TRANSITION` | 3000 | p1：MC=3、FW=4 | 仓库 `contracts.py` 和 PX4 规范已核实 |

`fw_land("runway"/"belly")` 先发 `DO_LAND_START`，再发 `NAV_LAND`，由 PX4
任务中的 landing sequence 完成具体进近。`fw_land("parachute")` 目前只做
`COM_PARACHUTE` 参数检查并返回“需核实”，不会假装已经知道某个固定输出通道。

## 模式与状态

支持的 PX4 custom mode 编码为：`AUTO_TAKEOFF=(4,2)`、`AUTO_LOITER=(4,3)`、
`AUTO_MISSION=(4,4)`、`AUTO_RTL=(4,5)`、`AUTO_LAND=(4,6)`、`POSCTL=(3,0)`、
`OFFBOARD=(6,0)`。旧多旋翼接口中的 `HOLD`/`RTL`/`LAND` 名称继续可用。

`EXTENDED_SYS_STATE` 同时记录 `vtol_state` 和 `landed_state`。其中 VTOL 状态的
3 是多旋翼，4 是固定翼；状态未知时保持 `None`，不能据此放行动作。

## 起飞预检

`check_takeoff_preflight()` 可接参数字典或 `read_parameter` 回调，缺失和不一致
分别列在 `missing`、`mismatched`：

* 手抛/弹射：`FW_LAUN_DETCN_ON=1`、`FW_LAUN_AC_THLD>0`、`FW_LAUN_AC_T>0`；
* 滑跑：`RWTO_TKOFF=1`；
* VTOL：`VT_TYPE` 在 0..2，`VT_ARSP_TRANS>0`、`VT_FW_MIN_ALT>=0`、
  `VT_TRANS_TIMEOUT>0`。

这些名字都来自仓库固定镜像参数文件，不从现场参数表猜名字。

## SITL 手抛/弹射判断

PX4 的 launch detection 依赖加速度阈值和持续时间（`FW_LAUN_AC_THLD`、
`FW_LAUN_AC_T`）。普通 Gazebo SITL arm 后不会自然产生手抛加速度，因此仅把
`FW_LAUN_DETCN_ON` 打开不能稳定触发起飞。可行的两种方案是：

1. 在 SITL 参数中开启 launch detection，并通过 Gazebo 模型/插件施加一次可控的
   初速度和加速度；适合验证真实检测路径，但需要主线程按所用 Gazebo 版本调参。
2. SITL 直接走 `RWTO_TKOFF=1` 滑跑，或者在测试桩中注入加速度/速度遥测；适合
   CPU 单测和确定性回归，不能证明真实手抛动力学。

本会话选择第一种作为真实验证方案、第二种作为自动化测试回退。`fw_executor`
不会伪造“已检测到手抛”，只发 `NAV_TAKEOFF` 并把 PX4 的 ACK/状态交给调用方。

