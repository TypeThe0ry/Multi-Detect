# OnboardAI：从持续看图到下一步飞行

更新时间：2026-10-07。本页区分已实现的软件链路与尚未完成的在线飞行验收。

最新开发增量：[原主机编写／实际 HOLD／子航段与 UDP 恢复接入](onboard-continuation-host.md)。
已实现单次调用与取消，不表示 whole-sortie dispatcher 或实际 Gazebo 飞行已接通；
测试中的 VLM 回答与净空仍明确为软件夹具，没有新增原生模型生成。

## 要实现的目标

用户只需描述任务，机载 AI 持续读取相机、飞机状态和任务记忆，自主决定下一步：继续搜索、
改变方向、跟随、记录位置、返航或结束任务。遇到遮挡、目标变化和风险时重新判断，不靠
固定颜色或预写阶段给出答案。Gazebo 与实际 Jetson 应使用同一模型与高层执行契约；
PX4 继续负责稳定飞行，现有安全规则检查模型提出的计划。

本轮已新增 v2 意图合同、独立推理 worker 和非阻塞 provider；真实模型到 typed 意图的
进程链已运行，现有 executor/planner/compiler 交接有软件夹具验证。两者分别核验，
没有把夹具的导航答案或 native HOLD 当成真实模型完成自主飞行。

## 模型究竟能看到什么

输入是用户原始 Prompt、真实 RGB、按时间排列的有限历史、当前可获得的遥测，以及来源
经过 host 审核的执行回执。缺失 GPS、HOME、电量或姿态就保持未知，不能补成看似完整的
飞机状态。场景真值、mask、测试答案和带有“遮挡／重现”提示的来源名字不送给模型。

记忆区分“提出计划”“执行器接受”“确实执行”“已经完成”。模型说自己完成了任务，
不能成为完成证据；上传被接受也不等于飞机飞到目标位置。host 必须核对回执来源、任务、
飞机、时间和实际结果。文件哈希只证明字节一致，不能代替执行事实。

v2 要把输入身份、任务边界、证据引用与结构化飞行意图绑定。原始回答必须严格解析，不
修补 JSON、猜目标 ID 或替模型补动作。模型可以提出有界移动计划，但不能直接越过
planner、安全检查和已有 Mission 上传、读回、提交与许可链。

## 推理慢了，飞行循环也不能卡住

已实现持久独立进程：模型加载后常驻，由主程序提交有截止时间的请求，飞行循环不等待
生成结束。超时或取消必须能终止实际工作；卡住的 worker 必要时终止并重新建立，旧请求
的迟到答案不允许复活。模型内部软超时或生成完成后才丢弃答案，不算硬取消通过。

provider 只交出已经完成、仍新鲜且精确源身份校验合格的结果。`pending` 表示尚无决策，不能
送进 adapter，更不能被伪装成模型的 `HOLD`，从而改任务状态、上传航线或产生飞控副作用。
取消、换任务、换飞机和时钟重置都要隔离旧结果。真正的安全故障由既有监督机制处理，
不能把“模型还没算完”和“必须悬停”混为一谈。

## 现在已经有什么，仍缺什么

此前 Qwen3-VL 已在本机读取真实三维 RGB。事实微调也确实完成 26 次参数更新与父／子
各 84 个回答，但遮挡判断和目标指代出现回归，候选质量不合格，不晋升。独立重载通过
只证明保存参数可以正确恢复；原训练重载失败报告、旧 v1、probe 和历史训练证据保留不改。
详见[事实微调结果](onboard-vlm-grounding-training.md)。

本轮只读核验时 Docker Desktop 为 stopped，Docker API 超时，没有新增 Gazebo 飞行。
重开来源通过的三段 Gazebo RGB+Depth 共 300 帧，全部属 train；没有审核动作标签和独立
留出，HOME 来自首帧 GPS，跨时钟映射也未证明。真实像素不等于动作训练 ready。

另一个未完成门是“来源合格”与“当前执行可接纳”的区分：旧录制身份成立，仍不代表
此刻任务、飞机、HOME、观测新鲜度和权限成立。新 v2／worker 接入必须分别验证这两层，
不能沿用来源通过标记自动授权当前飞行。

## 接下来实际怎么推进

共享 v2、worker 与 ready-only provider 已合入工作区并通过定向验证。实际 Qwen 基座两条
请求退出 0，原始回答严格通过，耗时 7.06／6.05 秒；因缺飞机状态，两次均 HOLD／空计划。
未加载质量失败 LoRA。CPU 重开 RGB 后，实际 processor tensor hash、原始 token 解码和
parser 身份一致。软件夹具证明 pending 无 adapter 副作用、一次 ready 交接经现有 planner
和 compiler 重规划。没有在线 collector：真实 receipt 回流仍需接事务、执行遥测和完成证据。

下一步是源观测与较新执行 context 的独立准入，以及任意 Prompt 的任务约束解析。
目前必须精确匹配源 context/event/scope/receipt revision，不能把旧答案重打时间戳；
普通新帧只更新下一窗口，以免每帧取消使模型永远算不完。再记录同帧输入、计划、实际
ACK、执行结果、真实 HOME_POSITION 和三种时钟，形成可审核动作 episode。

恢复 Gazebo 后采集包含真实 HOME、状态、动作和可核对时钟的完整 episode，隔离训练、
验证和测试；审核之后才进行动作微调。最终比较完整任务结果、错误动作、身份恢复和
时延，再分别验收 Jetson、HIL／REAL、室外定位。当前没有这些新增通过，版本与发布门不变。

可复用入口是 [sensor stream](../scripts/gazebo_sensor_stream.py)、
[外部 OnboardAI runner](../scripts/run_external_onboard_model_acceptance.py)、
[Mission 同容器采集](../scripts/run_vtol_mission_sensor_capture.py)和
[动作提案导出](../scripts/export_onboard_sortie_transitions.py)。runner 默认 synthetic 模式
不是实际图像验收；导出提案也不是自动生成合格标签。

可用 API 是 `OnboardVlmRuntimeRequest`、`OnboardVlmProcessWorker` 和
`OnboardVlmReadyDecisionProvider`／`OnboardVlmReadyScheduler`。worker 在事件循环锁外
显式 `start()` 预热，`submit()`／`poll()` 非阻塞；取消退休后需外层重新预热，不偷偷重启。
ready scheduler 只对精确源调用旧 adapter；provider 不能用于等待。

## 后续进展：源模型意图不变，在较新状态下重新准入

新增 `onboard_vlm_admission.py`／`onboard_vlm_execution.py`，并没有改写上述冻结 v2、
worker 或精确源 provider。模型读到的是源时刻的 RGB、context、事件与回执；它选择的
SEARCH／MOVE 意图先以源位置固定到 WGS-84 目标，再按最新飞机位置重算 N/E。
原 request、raw 回答、时间和权重身份保持原样。新 platform plan／Mission 只绑定
execution context，证据另外记录 source snapshot 和 `host_geodetic_reprojection`。
这不是把旧回答重贴到新画面，也不是主程序替模型决定语义动作。

首版仅接一个不变的已注册多旋翼任务。GOTO／FOLLOW 有目标身份和旧 adapter 覆盖
语义，REPORT／记录位置有新鲜匹配和完成事实依赖，本版不跨状态搬运它们。模型算完
之前没有决策，不提交 adapter、不清 Mission；新普通 RGB 只排下一窗口。ready 后在
锁外取原结果／预编译，提交短锁内核对 wrapper head 与真实 adapter history、Mission、
task-state receipt count、scope、runtime 和 admission policy。已消费／退休结果不复活。

执行前最后一刻的“暂不可用”不是模型故障。新 host-only 取消信号让旧 executor 在产生
decision、清空 Mission 或记 event 之前退出，由 scheduler 精确接住并返回 `None`。
普通新观察解除 arm、净空证据过期和源时间预算耗尽不能制造 HOLD；真正 raw／generation／
模型身份／原 binding 异常仍保留原故障处理。信号不来自模型回答，不改时间戳，也不
跳过执行检查。源真的过期时永久退休旧结果，但保留仍新鲜、同身份的 latest 请求。

当前 GPS、围栏、已知 relative-home 高度、资源和 canonical planner 重新校验。
原图像过期、时钟回退、HOME／任务／飞机／profile／epoch 变化、真实 Mission／回执
变化、危险事件、未知障碍或 legacy 5000 m 都不能准入旧移动。逆投影必须保留
`MISSION_ITEM_INT` 的整数目标；剩余位移越界就拒绝，不裁剪成另一个目的地。

仅有“前面距离 100 米”不能证明整条航线通行。独立 host receiver 必须提供绑定最新
context／event、固定目标与完整 wire hash 的原始 JSON 覆盖回执，包括三维爬升、
HOME／起飞／loiter／降落段；长度下限只是几何检查，不是实际动力学或感知可靠性证明。
调用者显式配置受信来源；哈希不等于认证。默认白名单为空，没有真实 producer，所以
本版不能自动批准在线移动。测试覆盖回执全部明确标作软件夹具，不冒称传感器测量。

旧 compiler 生成完整 sortie，局部重规划也带 HOME 起飞项和降落；空中 TAKEOFF 的
实际语义是航点，并非必定再次起飞，但依然会引入 HOME 绕行。现在新增独立
continuation 编译／接收准入，不裁剪旧 wire 冒称原完整计划：当前位置→固定目标→
有限驻留，显式 cursor1，预算仍含返航。旧冻结执行证明不改，新路径也尚未挂实际
PX4 replacement transport／在线 receiver／等待返航监督器。见[在途航段](onboard-continuation.md)。

## 执行记忆从哪里来

新增 `onboard_evidence_collector.py`，只接收、校验和记录，不发送指令。原回答重解析
形成 proposed；实际 adapter revision 与原字节、scope、Task／Mission／有效计划匹配
才 accepted。较新状态准入时保留原模型输出与 host 重投影分别的因果记录。
请求保存的真实 ledger 前缀可以晚于后续新 RGB 返回，不能因持续采图就拒绝历史事实。

绑定 committed／readback journal 和许可仍不是 executed。collector 需要同飞机／
component／Mission／transaction／clock 的新鲜 AUTO.MISSION、armed、in-air 和有效
Mission feedback；完成还需 terminal 后的新鲜 landed／disarmed，由现 outcome monitor
推导。原始 receive-envelope 与可用 wire bytes 留存，字段未知就拒绝升级。UTC journal
与 boot／host clock 不会被悄悄换算；显式 epoch 是调用者契约，不代表映射已经测量。
模型 REPORT、ACK、Permit 或 route progress=1 不够。snapshot 生成原 MissionEvidenceLedger，
下一次实际 serializer 可以把审核后的原始事实送给 VLM。

遥测 receive 时间与 host processing 时间各自单调，原始字节不改。排队延迟不能使下一
条合法 source 时间被判为倒退，也不能把旧 heartbeat／lifecycle／REACHED 刷成新鲜事实。
tracker／outcome monitor 使用 processing 域；回执同时记录每条原 source 与处理时间。

采集器当前未自动挂到真实 transport receiver；其 receipt revision 与 scheduler 的
task-state receipt count 也还没有做统一在线事务 head。软件 accepted→executed→completed
与序列化夹具通过不意味着真实闭环已通。必须先接真实回流和联合失效，才可做动作训练。

## 本轮证据和剩余工作

独立审查的未知高度、水平覆盖以及全生命周期覆盖反例已用于回归；同样检查公共 ready
消费口的时效、单次使用与新帧前缀。没有本轮 GPU 训练或原生模型生成，不改已失败 LoRA、
r33、COCO 或 production parity。所有测试都是软件验证，不是新增 Gazebo 飞行。

最终 617 项联合回归及独立 195 项审查通过，源／执行准入、原始记忆和中性取消边界见
[本轮软件证据](evidence/onboard-vlm-execution-admission-20261007-r1.json)。
此回执绑定实际源码／测试及旧基线字节，不覆盖上一轮 native runtime 回执。

上一轮集群 JSON／stdout 已通过正式 CLI 下载，bytes／SHA 精确匹配。报告实际上是
probe exit2、Docker info exit1、原生 gz 不存在、镜像未检查；collector exit0 仅表示
诊断收集成功。[恢复核验](evidence/cluster-gazebo-preflight-recovered-20261007-r1.json)。
本地 Docker 仍 stopped；未重启或迁移用户数据。

接下来仍是恢复可用的有图像 Gazebo、真实 HOME／时钟／状态／执行回执联合采集，
增加在途续航路径与任意 Prompt 任务语义，审核完整 episode 后动作微调／独立留出。
默认策略未替换，`0.3.0-pre.8` 与 Jetson/HIL/REAL/GNSS/最终发布门不变。

原生入口 `python scripts/run_onboard_vlm_runtime_replay.py --help` 已核验能独立加载模块，
输出目录禁止覆盖。实际报告见
[native replay](../artifacts/evaluation/onboard-vlm-process-runtime-20261007-r1/report.json)，
[持久回执](evidence/onboard-vlm-runtime-20261007-r1.json)。该入口明确是离线真实 RGB 回放，
不是 live telemetry builder，也不会创建 Mission 或 transport。

上一个冻结 runtime 回执记录 Linux 只读预检退出 2、诊断采集退出 0，当时只有 artifact ID。
后续通过正式 CLI 已取回并核验原始内容/hash；具体权限失败见本页“本轮证据和剩余工作”
及恢复核验回执。取回诊断不代表 Gazebo 已运行，原冻结回执保留当时的观察结果。
