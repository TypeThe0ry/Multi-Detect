# 机型档案（AirframeProfile）

机型档案是规划器、机载运行时和地面站共同使用的一份能力说明。它只描述“这架飞机能安全做什么”，不替代 PX4 参数，也不假设有测距仪。当前硬件基线是 Jetson Orin Nano 8GB、单目相机和 Pixhawk PX4。

## 能力差异

| 类别 | 能否悬停 | 典型模式 | 起飞 | 降落 | 任务设计重点 |
| --- | --- | --- | --- | --- | --- |
| 多旋翼 | 能 | hover、multirotor | `multirotor` | `multirotor` | 可在目标附近停住，抗风和能量以悬停功率为主 |
| 固定翼 | 不能 | 固定翼巡航、盘旋 | 手抛、弹射或滑跑 | 跑道或 belly | 必须保持空速，盘旋半径受坡度和空速限制 |
| VTOL | 能（多旋翼段） | hover 与 fixed-wing loiter | VTOL、必要时跑道 | VTOL、跑道或 belly | 转换前后要满足高度、空速和超时约束 |

`tailsitter` 特别要求 MC 和 FW 两套相机姿态；同一物理相机在机体竖直悬停和水平巡航时不能共用一个姿态矩阵。没有测距仪时，档案不会暗示有 AGL 真值，任务仍应使用 PX4 高度和保守安全高度。

## 字段说明

- `id`、`category`、`subtype`：稳定档案 ID、能力大类和构型。大类只有 `multirotor`、`fixed_wing`、`vtol`；构型包括 `quad`、`quadplane`、`tailsitter`、`tiltrotor`、`conventional`、`flying_wing` 和 `other`。
- `px4_autostart`：仿真/启动用的 PX4 airframe 编号，例如 rc_cessna 为 4003、standard_vtol 为 4004。
- `min/cruise/max_airspeed_mps`、`stall_airspeed_mps`：校验关系是 `stall <= min <= cruise <= max`。多旋翼也保留这些字段，便于统一规划接口。
- `min_turn_radius_m`、`max_bank_deg`：最小转弯半径不得低于 `v²/(g*tan(坡度))`。未填写半径时，合同按物理下限增加 10% 安全余量计算。
- `max_climb_rate_mps`、`max_descent_rate_mps`：规划器的垂直速率上限。
- `can_hover`、`hover_modes`：是否有可控悬停段及对应模式。固定翼声明悬停会被拒绝。
- `takeoff`：方法可以是 `hand`、`catapult`、`runway`、`vtol`、`multirotor`。手抛/弹射必须有 `FW_LAUN_AC_THLD` 和 `FW_LAUN_AC_T` 的 launch detection；滑跑必须给 `RWTO_ROT_AIRSPD` 旋转空速。
- `landing`：支持 `runway`、`belly`、`parachute`、`vtol`、`multirotor`。
- `transition`：仅 VTOL 有此字段。`MAV_CMD_DO_VTOL_TRANSITION` 的 `mc_to_fw_param1=4`、`fw_to_mc_param1=3`，并记录最小高度、最小空速和超时。
- `camera`：单目相机安装方向（forward/downward/gimbal）和 body 到 camera 的 3x3 行优先旋转矩阵。tailsitter 必须同时有 `attitude_rotations.mc` 和 `.fw`。
- `endurance`：电池 Wh、储备比例、悬停/巡航/盘旋/爬升/下降功率、转换能耗、滑翔下沉率和风余量。x500 的回归值是 360/400/500 W 和 5 m/s。
- `limits`：单个控制步允许的最大位移、加速度和角速率。
- `notes`、`provenance`：记录每个估计值的来源。引用 PX4 参数的参数对象含 `px4_name`；仓库参数文件没有的名字必须显式写 `unverified: true`。

档案由冻结 dataclass 表示。`AirframeProfile.from_json()` 加载并严格检查未知字段、数值范围、构型组合和 PX4 参数引用；`to_json()` 可保存，保存后再次加载与原对象一致。`thinking_behavior()` 返回 hover/loiter/mode choice，`orbit_radius(requested)` 对请求半径施加物理下限，`can_stop_near_target()` 判断是否能在目标附近停住，`loiter_radius_m()` 返回可用盘旋半径。

## 默认档案与来源

| 文件 | 构型与起飞 | 关键数值来源 |
| --- | --- | --- |
| `x500_mono_cam.json` | quad，多旋翼 | 现有 `energy_estimator.py` 的 360/400/500 W、5 m/s；相机位姿来自 x500 mono_cam SDF |
| `rc_cessna_hand.json` | conventional，手抛 | 4003 脚本和 PX4 `FW_LAUN_*`、`FW_T_CLMB_MAX=5`；固定翼空速采用参数默认 |
| `rc_cessna_catapult.json` | conventional，弹射 | 同一 rc_cessna PX4 参数，launch detection 沿用 `FW_LAUN_*` |
| `rc_cessna_runway.json` | conventional，滑跑 | `RWTO_TKOFF=1` 来自 4003；12 m/s 旋转空速是保守估计 |
| `advanced_plane.json` | conventional，滑跑 | 4008 脚本的爬升 6、最大下沉 2.7 m/s；旋转空速为估计 |
| `standard_vtol.json` | 4+1 quadplane | 4004 脚本的 `VT_TYPE=2`、`FW_T_CLMB_MAX=8`、`FW_T_SINK_MAX=2.7`；相机位姿来自 standard_vtol mono_cam SDF |
| `quadtailsitter.json` | 四旋翼 tailsitter | 4018 脚本的 `FW_AIRSPD_STALL=10`、`MIN=14`、`TRIM=18`、`MAX=22`、`VT_ARSP_TRANS=15`；MC/FW 相机姿态按构型显式记录 |
| `tiltrotor.json` | tiltrotor | 4020 脚本的 `VT_TYPE=1`、`VT_TILT_TRANS=0.6` 及固定翼速率；功率和续航是保守估计 |

### 当前未核实项

默认档案中，`rc_cessna_runway` 和 `advanced_plane` 的旋转空速 12 m/s 需要真实机体/跑道标定；各固定翼和 VTOL 的电池容量、功率、转换能耗、风余量、单步加速度和角速率也属于工程估计。它们在 `notes`/`provenance` 中标明了来源，不伪装成 PX4 参数。若新增 PX4 参数名而该名字不在 `docs/reference/px4-pinned-fw-vtol-params.json`，必须把对应引用标为 `unverified=true`。
