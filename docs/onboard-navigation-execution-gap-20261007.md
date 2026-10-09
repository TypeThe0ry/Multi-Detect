# 导航 AI 到实际飞行：还差哪些接线

2026-10-07，只读代码调查。目标是让 Gazebo 与 Jetson 使用同一受审模型决定下一步动作，
不是让程序按固定任务表代替模型判断。本记录没有修改执行代码、默认模型或发布门，
也没有启动 GPU、仿真或飞机；文档完成、CPU 测试通过与飞行通过是三种不同结果。

## 目前不是把新模型路径换进去就能飞

本次训练输入是原文任务、当前 RGB 和最多一张最近过去 RGB，输出是 **v1 导航原语**：
机体坐标系的前进／平移、左右转向、停止；未知距离或角度保持 null。
旧 **v2 任务输出**却要求 response、reason、confidence、task_action，位移使用地理北／东。
两套输出含义不同，不能硬套，也不能补造“confidence=1”或“当前阶段=search”。
依据：[监督输入／输出，第 1148–1200 行](../src/multidetect/autonomy/onboard_navigation_supervision.py#L1148)、
[旧输出，第 571 行](../src/multidetect/autonomy/onboard_agent.py#L571)。

现直播桥只保存、推理相机图像，没有飞控写入；旧外部飞行入口仍加载 ONNX runner。
现 Qwen backend 只加载 base，直播消息又与这次训练 actor 不同，不能视为导航 LoRA 已接入。
下一步必须绑定同一个 base、adapter、processor 和处理预算，保持 actor 的文本／图像顺序一致，
检查真实输入张量，而不只是模型名称相同。
依据：[直播桥，第 45 行](../src/multidetect/autonomy/onboard_live_shadow.py#L45)、
[旧飞行 runner，第 942 行](../scripts/run_external_onboard_model_acceptance.py#L942)、
[Qwen 加载，第 40 行](../src/multidetect/autonomy/onboard_vlm_backend.py#L40)、
[本次 encoder，第 418 行](../scripts/train_onboard_vlm_navigation_pilot.py#L418)。

## 动作不能翻译错

- **前进不是往北飞。** 须用源图像时刻的真实 roll/pitch/heading，把机体“前／右／下”转成
  地理“北／东／下”，和源位置一起锚定目标。模型返回时飞机可能已移动或转向；按最新状态
  重新检查旧目标，不按最新朝向重解释旧“前进”。现 ContextSnapshot 没有姿态字段，需新增
  显式姿态锚定，不能从 OpenFly 标签补实时姿态。
- **左／右转是改变朝向，不是横向平移。** 左负、右正的相对 yaw 要绑定源 heading，并由
  飞控姿态反馈确认实际转向。现计划没有 yaw 接口；MissionItem 已有 param4，所以不一定
  要新增 MAV_CMD。先核验所用 PX4 版本的航点 yaw 与完成语义，再决定如何编译。
- **STOP 不是 LAND，也不是任务完成。** 它只表示这段外部导航序列停止。应撤销尚未执行的
  旧建议并进入受审保持路径；找到目标、记录 GPS、返航、报告必须有各自真实证据，不能由
  STOP 偷换出来。null 动作不能靠默认距离／时间变成可执行建议。

依据：[北／东计划，第 316 行](../src/multidetect/autonomy/onboard_agent.py#L316)、
[ContextSnapshot，第 278 行](../src/multidetect/autonomy/contracts.py#L278)、
[param4，第 823 行](../src/multidetect/autonomy/contracts.py#L823)。

实时接线还必须提供：源帧 hash、序号、相机时间与 host 时钟身份、帧时刻姿态和 GPS、
GPS 质量／龄期、HOME 与高度基准、Gazebo／Jetson 相机内外参与安装朝向标定、
实测路线净空、电池、围栏、armed／mode／链路状态、当前 Mission/cursor/revision。
图像序列不能证明这些安全状态；直播 partial facts 将缺项明确记为 unknown。
依据：[未知字段，第 45 行](../src/multidetect/autonomy/onboard_live_vlm_contract.py#L45)。

## 两条路都可以重新评估

| 路线 | 收益 | 需要新增与验证的成本 |
| --- | --- | --- |
| 连续改进：短 Mission 段 | 复用 planner、安全检查和 upload/readback/commit/permit，改动范围较小 | 增加原语／yaw／STOP、重复段生命周期；量测事务停顿和闭环延迟 |
| 架构重考虑：PX4 Offboard 持续 setpoint | 有望更自然地连续移动、转向，减少反复上传；尚无本项目性能证据 | 新增持续发布、模式切换、超时／失联／模型卡顿处置与仲裁；重新验证坐标、控制及执行证据 |

Mission-only 是历史实现，不是不可改变的物理约束。当前完整 multirotor compiler 强制
TAKEOFF→WAYPOINT→LOITER→LAND，不能每一帧导航建议都重复整趟起降。
已有 continuation 可复用在空中段与 pause 后重新审查／事务／cursor，但它只处理一个
原 parent，拒绝零水平位移，不是整条循环、原地转向或 STOP 的现成实现。
依据：[完整起降，第 1584 行](../src/multidetect/px4_mission.py#L1584)、
[段编译，第 376／617 行](../src/multidetect/autonomy/onboard_continuation.py#L376)、
[单 parent host，第 1／344 行](../src/multidetect/autonomy/onboard_continuation_host.py#L1)。

Offboard 不能让慢速 VLM 兼任持续控制心跳。官方 main 文档要求连续存活信号，并描述
入模式前的信号、状态要求及丢失超时；应由独立受审发布器维持，模型只更新有界目标。
这是[官方能力说明](https://docs.px4.io/main/en/flight_modes/offboard.html)，不是当前镜像／
真机版本已经通过的证据。选择这条路前必须绑定实际 PX4 commit／固件与参数，按**本版**
官方源码／文档核验支持的 setpoint、坐标、yaw 和失联行为，再做外部 SIM；不能拿 main
文档替代本版验收。

## 推荐现在先做的最小一步

先增加有类型的导航运行时契约、同 actor 的受绑定模型 runner、源姿态锚定和无执行权限的
动作解析。安全与几何检查可以确定性验证，但不能替模型决定搜索方向或伪造任务判断。
这一步对 Mission 与 Offboard 都有用，收益明确、验证成本较低；先量测真实推理与段执行
延迟，再选执行路线，不为了维护旧代码保留更差架构。

接线回归至少覆盖：坐标／yaw 符号；陈旧帧与任务切换拒绝；缺 GPS／净空禁止移动；
STOP 不变成成功／降落；连续段不重复 ARM／TAKEOFF／LAND且回执闭合。
这些是后续验收要求，不是本轮已跑过的测试。模型质量、Gazebo 闭环、Jetson、HIL／REAL、
室外 GNSS 和最终发布门保持各自证据边界；本记录不宣布任何飞行通过或候选晋升。
