## 2026-10-11?SIM ?????????

????? latest-wins??? PX4 sim-time ????????? sim stamp ? MAVLink ?????gen_w0025 ???? |sim skew| ? 0 ms??? p99 376 ms??dataset builder/BC loader ????? step?40 ??? world ?? 28/6/6 ????? channel hash?truth world-relative altitude?Windows pipe-close??????????????????

expert teacher ??? obstacle top + 4m ????? segment-vs-obstacle ???forward-facing legs ???????????????? detector ? Gazebo ?? recall 0/11?SIM-truth ???????YOLO26n trainer ? held-out evaluation ????644 ???? 3-epoch GPU smoke??????? val/test-world ???policy v2/DAgger rollout ??????????campaign-20261011-r3 ? 4.5m ?? 27/41 episodes ???????????????? detector diversity??????????? campaign?? SIM????????????? 0.3.0-pre.8?r33 ?????

# Multi-Detect 当前进度


## SIM 教师数据图像对齐说明（2026-10-10）

在 commit `29d893e` 之前，Gazebo image frames 是 FIFO backlog，和 pose 时间错位；因此 `urban-explore-r12`、`urban-expert-r1..r5` 以及更早的 Codex Gazebo image evidence（包括 8/8 STOP shadow 结果）都不能支持结论。latest-wins frame server 加入 sim-time 对齐后修复该问题；`aligned-explore-r1` 的最大 `|sim skew|` 为 44 ms。

## 最新：首个完整 REAL 闭环 Gazebo PX4 SITL 教师架次（2026-10-10）

SIM 教师采集已与离线审计解耦；urban-explore-r12 完成 5 个动作步骤，保存 10 张 RGB、10 张 float32 深度图和 17789 行 MAVLink。审计通过：时长 118.484 s，位置误差均值/最大值 0.114270/0.217683 m，偏航误差均值/最大值 2.497954/5.485699°。这里的 REAL 仅指 Gazebo/PX4 软件闭环，仍不代表 HIL、真实硬件、户外飞行、训练或晋升。

## 最新：完整架次预算与当前输入校验

三个子智能体并行处理有限采集／执行资源、当前输入时延和公开模型，主线程接线。
NEW完整预算含返航／清理，原件写前预扣、写后核对；V4／CP-v2与旧接口显式隔离。
地图／地形／整路软件检查已补上，条件地面假设不冒充PX4实际飞行保证。
五轮actual no-ARM均native1拒绝，原包、源码副本和owned清理保留并核验。
R5启动HOME稳定通过，但校验末尾过期；正缩短实际延迟，不刷新原时间或放宽TTL。
没有新ARM／Mission／动作训练或晋升，默认r33、canonical与pre.8保持不变。
见[本轮说明](onboard-teacher-sortie-foundation-20261008.md)。

## 最新：实时检查开发入口与实际出生高度对照

三个子智能体并行处理最新观测／执行重验／机体几何，主线程整合并实际运行新Gazebo。
spawn变体只增加初始高度15cm，自然落稳modelZ约.0669998165、bodyZ约.3069998165；
足点与8cm垫面差−.186µm，较原−6.44cm重叠改善，不冒测得接触力或已验证物理后端。
实际root／child0、4RGB／4Depth／5222原包，容器与ownedJob清理核验；新封存导航
重算与15静态碰撞一致性通过，不授当前live资格。R1误用raw hash的启动前失败保留，
R2修canonical channel hash后实际成功。NEW提交前缀与V8有限执行接线分原计划／
当前观测，不刷新旧时间或自动授Grant。独立审阅发现RX误用main-only writer检查，
补跨线程回归并修复；旧8192包／60秒意图上限、地图／DEM门和接触域仍待整合。
修复后最终19文件联合715项CPU测试通过；V8新增63项、实时输入36项、几何50＋30项，
独立核对signed实际原件18文件与首轮未覆盖的执行线程路径。来源／原件哈希写入
新证据和索引，未把同一静止画面的4次采集扩算为动作训练样本。
文档／证据写入后另104项索引与parity回归通过；50列明文件大小与SHA全部重验一致，
当前源码重算的进度验证通过，release_allowed=false。
未ARM／飞行／新增推理或训练，r33、canonical与pre.8不变。
详见[本轮说明](onboard-teacher-live-checkpoint-and-spawn-20261008.md)。

## 最新：同期三维源窗与场景一致性通过，正常解锁接口已实现

三个子智能体并行采集／坐标／执行接口，主线程整合并实际运行新的PX4/Gazebo。
新运行root／child退出0：4RGB／4Depth、5819原包（5705合法／114foreign／0orphan），
37只读查询／74完整输出；33列明来源前后不变，owned容器与Job清理核验。
Gazebo实际导出与仓库15碰撞体定义一致；不冒求解器接触或机体包络／整路净空许可。
原始四帧与MAVLink／listener／安装偏移坐标链重算通过，最大软件SIM残差约12.9cm。
R1将窗前HOME初始化误算窗口重置的失败保留；R2限定真实窗口、不删原包／不放宽门。
封存audit不创建live Context／runtime attestation，origin_qualified=false，不签当前Grant。
新唯一RX/send入口支持受审持久事务后的正常ARM／一次RTL收敛，force参数0；仅CPU验证。
纠正heading_good_for_control=false不等于禁止ARM；健康位必须对应实际当前模式。
默认r33、旧canonical与pre.8保持不变，没有新ARM／飞行／模型训练或晋升。
最终602项联合CPU回归／13文件Ruff及format通过；独立226文件／全5819切片复核一致。
文档写入后另99项证据／索引回归及当前canonical源码重算通过，release_allowed=false。
详见[本轮说明](onboard-teacher-origin-window-and-normal-mission-20261008.md)。

## 最新：三维净空与 Mission 接口已实现，正在核清实际导航参考点

三个子智能体并行完成碰撞几何、单RX/send Mission传输与原点调查，主线程集成。
新静态检查器覆盖起飞、去程、驻留、显式回到家上空、降落和位置／机体裕量；
不把缺模型／动态障碍／不支持的坐标系当空地，不自动修改原观测资源状态。
窄Mission接口只接受已授权持久事务和精确INT线数据，原包落盘后才消费回复；
不生成PASS／Grant，不解锁、不切模式、不启动。

旧实际R2原点确实在新加坡，但模型原点不能冒充导航点。NEW固定镜像原件提取
确认base_link／GPS有24厘米安装偏移，提取容器从未启动，精确自有ID清理已核验。
场景ENU风与旧runtime北东方向声明不一致，NEW配置风检查记录差异，不改历史文件。
尚未签发实际当前原点／路线净空许可，实际位姿、参数及重置连续性必须另行采证。
新模块为开发入口，单独绑定源码；旧canonical共享核心、默认r33与pre.8保持不变。
本轮新actual只读运行root／child退出0：20观察中16合格、2RGB／2Depth、4356原包，
18命令／36原始输出全部核验，22／28列明来源窗口前后不变，owned容器与Job清理。
晚采估计器heading_good_for_control=false；这些参数／位姿不是同窗原点资格。
最终646联合CPU回归和12文件Ruff／format检查通过；仍无Mission／ARM／实际动作训练。
详见[本轮说明](onboard-teacher-3d-origin-and-mission-20261008.md)。

## 最新：真实观测的模拟预算与共享教师 Mission 编译已接通

三个子智能体并行完成规划输入桥、GCS身份修复和预训练动作候选查验，主线程集成。
新版原观测／raw引用／显式SIM模型在编译前重新读取与重算，保留未知空速、GPS、HOME、
电量、资源状态和原接收时间；返航余量只扣一次。多旋翼SIM教师路线可编译驻留／转向
命令，不要求不存在的识别器；普通目标任务与其他机型默认资源门和v2隔离不变。
严格检查时效、时钟、runtime/profile、任务／geometry／policy绑定；过期或替换拒绝。
GCS255/190可走共享INT上传／回读协议，发送前核actual sender，foreign／broadcast拒绝。
这些是CPU结构／协议验证，没有调用真实Mission、ARM或飞行；编译绑定不是自动Grant。
最终669核心／协议＋122文档／parity／索引回归通过，Ruff与当前源码索引核验通过；
保留文件审计不等于新外部SIM运行，两个接收龄期／返航余量边界已修复并独立复核。

仍缺当前相机／围栏来源、EKF／地图原点、完整3D路线净空、唯一RX/send事务接线和真正
验证报告／一次性SIM意图。旧实际R2原件不覆盖，也不把闭合快照当当前实时来源。
新增MiniCPM-Robot／MolmoAct2／GR00T固定来源研究；许可、资源与飞行动作域分开，
没有下载／加载模型或优化器更新。默认r33、产品pre.8、所有物理／最终发布门保持不变。
详见[本轮说明](onboard-teacher-sim-planning-admission-20261008.md)和[模型研究](onboard-pretrained-action-research-20261008-r3.md)。

## 最新：真实 Gazebo/PX4 地面待命观测合格，行动训练仍待真实飞行数据

三个子智能体并行实现Context v2、SIM操作员契约并独立审核。R1参数无值行退出1，
0变更／0相机，原失败封存；NEW V2精确quiet查询后的actual R2 root／child退出0。
只在owned一次性SIM将COM_RC_IN_MODE 1→4并读回，八项安全参数不变；GCS心跳22次
API返回不冒独立网络交付。20次观测中16合格，另1未齐／2非STANDBY／1时间偏差拒绝。
保存2RGB／2Depth（1280×960）、4428原包，4340接受／88foreign仅记，0orphan／overflow。
owned容器清理和Job空，61源在执行窗口前后相同；闭合快照不是当前可控实时状态。

v2允许空速／续航未知并标记来源，SIM模型禁止进入非SIM规划；旧38／40维模型和
未迁移Mission入口拒v2，不填0或放行fixture。458定向＋122文档／索引／回执回归共580
CPU测试通过，当前源码与进度索引重核通过；这些不是飞行或模型能力评分。
默认r33与产品0.3.0-pre.8未变；当前core／Mission／task parity已变，历史矩阵只是
明确标记的retained-file静态重绑定，不冒新发布运行。原32发布阻断保留、release=false。
见[人话说明](onboard-teacher-sim-operator-20261008.md)及[绑定摘要](evidence/onboard-teacher-sim-operator-20261008-r2.json)。
接下来接真实SIM身份、显式预算、origin／clearance和真实Mission事务，采一次起飞后的
实际动作回流再审数据／训练；公开模型调研见[候选查验](onboard-pretrained-candidate-refresh-20261008-r2.md)。
没有新模型加载／推理／优化器更新、ARM、Mission上传、飞行、晋升、HIL／REAL或室外GNSS。

## 最新：实际 PX4 参数已读，日志超限失败如实保留

三个子智能体并行完成事件、唯一RX只读请求及独立审查，249个新增CPUcase通过；
与既有V3采集及文档／parity定向回归合计393通过，Ruff／格式及产品保护SHA核通过。
新实际运行保存4623条原包、0orphan／0overflow；真实版本和18项参数共19次native读
退出0，原输出保存。AUTOPILOT_VERSION与17个参数的原包CRC及bytewise解释另核通过，
`BAT1_A_PER_V`缺失不补值。实际`COM_RC_IN_MODE=1`选择MAVLink手动输入，容量仍-1；
RC／GCS失联动作均为2。本轮不改参数，正常预飞／教师动作仍未通过。

最后日志输出超过256KiB且未持久化，整轮root／child native1仍是失败；owned树空、
容器清理及独立absence／PID确认完成，40源仅执行窗口内一致。独立consumer审核后
另存新版派生报告，不冒旧源版本。此前R3的24原UNKNOWN包另行CRC核及16事件匹配，
两条“No manual control input”与其他GCS／任务／位置提示不是唯一根因或同次状态。
见[本轮进度](onboard-teacher-px4-diagnostics-20261008.md)与[绑定证据](evidence/onboard-teacher-px4-diagnostics-20261008-r1.json)。
下一步明确无人手动输入SIM契约、机型缺测与显式能量模型，再采实际行动回流和训练。
没有新加载、优化器更新、飞行、候选晋升、Jetson／实飞或正式发布。

## 最新：V3 真实并行 Gazebo 观察已完成，仍未飞行或新训练

实际R3保存6张1280×960 RGB／6份深度、5943原包、0orphan／0队列溢出；57份九消息
快照保持原receive时间，最后9／9新鲜。三个相机helper native0，容器launch-stop-rm0
且最终缺失、owned树空、26来源前后不变。见[R3摘要](evidence/onboard-teacher-gazebo-observation-20261008-r3.json)。
108新CPUtests＋旧相关回归合计366通过，与实际运行分开记账。

中位采集延迟10.08ms、189.68条／秒，但P95升至223.39ms，实时门未通过。60次资格
尝试为3未齐／12skew／45非STANDBY拒绝；root及child native1/context-refused，error
null。23条实际HB均status0，原电池time_remaining0/current-1、空速NaN仍保真。
具体预飞故障未定因；下一步只读固件event／真实参数／预飞状态，完整迁移机型有效性与
SIM能量来源，再进行origin／clearance和实际教师Mission。未改默认模型或发布门。

## R2：真实 Gazebo 观察入口与失败回流

R2已真实保存2张1280×960 RGB／2份深度、1057条原包及可逆投影；1033条来源过滤接受、
24条仅记录，0orphan，4228文件size/SHA独立复核。258项定向CPUtests通过。
整轮仍context-refused／root与child native1：最后快照错误是observer skew；截止2秒门
仅4／9类消息新鲜，HOME／BATTERY各保存一次后陈旧。原电池time_remaining=0，原空速
NaN也保留为诊断，不能把诊断混成原refusal。owned树空／容器stop-rm0及absence核，
19代码／环境／产品保护来源前后相同。见[R2原件摘要](evidence/onboard-teacher-gazebo-observation-20261008-r2.json)。
串行记录中位36.43ms、约25.81条／秒；下一版改有界双journal批次＋并行相机，保留
原receive时间与有效性，不放大门限。V3现已实现并实际运行，见顶部；机型空速有效性与SIM能量来源仍待完整迁移。

NEW owned-x500会话、source-bound observed context和observer已接通，168项定向CPU
夹具通过。独立审核修复首次UDP无peer的虚假发送计数、慢I/O后的freshness终门、风配置
原件漂移、seal/abort掩盖原异常和stop异常跳过rm。所有新增授权／飞行门仍false。

真实R1已启动固定镜像容器，在相机前因原记录器拒绝document.data非JSON字段退出；
保存1合法COMMAND_LONG原包41B，0HEARTBEAT／相机／续航包，不能声称实测time_remaining=0。
root／child native1，owned树空、容器stop/rm0且absence确认；12source／受保护文件
前后SHA一致。原失败包未保存，具体类型未知；另建v2可逆typed recorder／observer，
不改原实验，不替换默认模型。见[阶段说明](onboard-teacher-gazebo-observation-20261008.md)
与[首轮真实回执](evidence/onboard-teacher-gazebo-observation-20261008-r1.json)。

官方VLM/VLA并行复查已补独立环境、完整底座依赖与真实显存边界；未下载新权重、
未模型加载／训练／飞行，详见[模型查验](onboard-pretrained-navigation-search-refresh-20261008.md)。

## 最新推进：一次起飞的教师航段与合法来源遥测

NEW held/yaw authoring已有82项新CPU测试、邻接154项和独立82项通过；每段停留／
转向语义与新增保守移动／升降／转向／停留预算均重新编译并绑定，原共享安全及传输
路径不改。NEW唯一接收者先保存原packet／projection、隔离foreign，再更新合法来源
cache；22项作者／独立通过，与既有interval／planner／multirotor联合248项通过。
实际SIM venv的pymavlink仅做7个CPU构造报文的编码解码采集，5合法／2外来，原字段
与时序保留；不是网络遥测、teacher飞行或训练。见
[新软件回执](evidence/onboard-teacher-authoring-and-raw-telemetry-software-20261008-r1.json)。

独立调查排除了完整VTOL force-arm流程和fixture自动PASS／grant：下一入口需要实际
场景／航线检查、明确owned软件SIM意图、合格fresh context、正常ARM和同一持久Mission
事务。底层未过滤cache不得进入context；reached不等于停稳，同QPC不等于时钟／EKF
原点已经映射。本轮仍没有新teacher轨迹、模型更新、飞控或HIL／REAL验收。

NEW独立600s native监督器51项作者及独立CPUtests通过，修正Windows venv launcher／
worker身份与pre-Job子进程竞态；actual R4只读预检native1资源门拒绝，physical5.05GiB／
commit8.08GiB仍低于原8／16GiB门，0load。自有进程树空、两个PID已退出，无watchdog／
溢出；原stdout及泛化错误保留，拒绝不当作崩溃或OOM定因。另行来源56项inventory
核验及独立13小binding／原报告审核通过，不回写原source-final=false为成功。见
[新监督器与实际预检回执](evidence/onboard-navigation-owned-supervisor-preflight-20261008-r1.json)。
Controller当前不可达，本轮未派远程任务，未关闭用户程序或重跑大模型。
最终36项文档／release parity检查、17份新source／evidence／受保护文件的逐字节绑定、
六源Ruff／format和scoped diff通过；README为49,820B。默认r33、产品版本与release
parity原SHA不变，没有自动commit／push，持续goal保持活动。

## 2026-10-08 最新：R3现场特征采集加载失败，Mission区间记录已验证

新R3实际尝试在精确加载流程完成前发生`0xc0000005`原生崩溃，外层native1，
Windows事件指向同一模型进程的c10.dll。watchdog未触发；0新增RGB／feature／
forward／optimizer／飞控，worker-report不存在。计划、源码、日志及supervisor封存，
没有启动新的Gazebo容器，launcher／模型进程已退出，13个其他容器未变。
内存压力已观察，小CPU运算正常，但故障原因未证明，不盲目重跑或改写R3。

R3入口26项软件tests通过。NEW父Mission区间记录72项、现场feature CPU审计19项
独立进程tests及Ruff通过；前者区分唯一父提交和后续seq激活／到达／稳定端点，
后者要求成功的8份现场特征，当前无数据所以没有实际运行。137项混进程曾失败：
5项正确拒绝已导入Torch的审计环境，1项暴露旧记录器atime误报，不能写成联合通过。
NEW记录器v2仅修复atime内容身份判断，原20＋新6项CPUtests通过；保留真实字节SHA，
旧v1与R3不改，v2合同需下一版入口显式消费，尚无实际采集成功证据。
NEW exact-load诊断44项作者及独立CPUtests通过，R4计划实际发出；只读资源预检
native1拒绝（可用物理5.64GiB／commit8.79GiB，计划8／16GiB保守门），0模型加载，
不创建执行目录、不关闭其他程序；门未通过不等于R3 OOM根因已证。
最终36项进度文档／release parity检查、25份source／plan／evidence／受保护文件
逐字节绑定核验及scoped diff通过；README为49,822B，三个受保护SHA未变。

完整状态、实际失败回执和下一步见[本轮记录](onboard-navigation-live-feature-attempt-20261008.md)。
后续先定位加载并保存现场特征，同时补中间航段停留／yaw、真实时间／原点转换及
父Mission journal因果接线，再采集教师行动反馈、审核和训练。旧训练预算／默认引用／
产品版本／release parity／Jetson与发布门不变，没有自动commit或push。

## 上一阶段：动作头诊断完成，三维执行反馈接口已实现

实际CPU重放既有208特征、11个保存头和502份raw，类别全一致，最大数值误差约
0.00000286；不重新运行VLM或Gazebo、不训练。最终208头在同TRAIN内预测前进53／
左54／右56／STOP45，raw类别正确83／208；九个episode留出raw正确53／208不是此前
合法动作46／208的改善。11个STOP bias都比其他类别低，不支持“恒定STOP或偏置
压倒一切”的解释。现场八帧只有featureSHA，不能独立复算live分布或定因。
[诊断与边界](onboard-navigation-head-diagnostics-20261008.md)说明具体数值、原CPU报告和下一项证据。

NEW三维执行记录分开教师命令、安全批准、唯一提交、失败／未知／执行结果和真实
前后画面／完整姿态；保存原始source JSONPointer，源文件逐SHA与字段投影核验，
支持FRD／NED／ENU及Gazebo FLU显式转换。原始命令不被实际位移覆盖，端点差不
证明连续净空或执行因果，STOP不变成返航／完成。107合同＋33集成＋20诊断共160
小CPU测试root与独立复跑通过；source、输出期望SHA、重复执行UUID与原JSON数值门
都已检验。[数据接口](onboard-executed-trajectory-data-20261008.md)给出准确入口和剩余真实采集门。

最终36项进度文档／release parity测试与六源Ruff／format通过；两份新回执、来源与CPU
报告逐字节核验一致，product版本／默认模型引用／release parity三个受保护文件保持原SHA。
README为49,869字节，scoped diff检查通过；没有自动commit或push。

当前没有新增teacher飞行数据或训练更新，没有运行GPU／模型／Docker或发送飞控。
下一步须一次受审teacher Mission／一次起飞，按seq及实际稳定端点关联画面和动作；
不能每个小动作重复完整起降，也不能给只观察callback偷偷加飞控。真实采集、生产者／
专家审核、world分组、三维输出学习与未见world评估仍未完成，默认／版本／发布门不变。

## 上一阶段：真实画面与候选AI的推理接线

## 2026-10-08 最新：真实Gazebo画面已进入候选AI，尚不能自主导航

精确加载已有267候选与保存的动作头／TRAIN归一化，先加载模型，再读取8帧新的
1280×960 Gazebo RGB与8份深度；同一共享Actor生成8个请求。实际视觉语言融合与
动作头计算均完成8次，协议接受8次、拒绝0次；原生退出0 `5afbe8`。**8次全是
sequence_stop**，不是返航、降落、找到目标或任务完成，也没有专家动作／净空可评分。
[实际结果回执](evidence/onboard-navigation-gazebo-learned-shadow-actual-20261008-r2.json)
绑定原始输出、worker、supervisor、输入记录及采集记录，保留失败尝试与成功接线的区别。

首次尝试加载身份已闭合，但官方processor返回BatchFeature，被旧exact-dict门拒绝，
8次均未进入融合／head。保存原失败后，新R2只做严格浅投影：四字段必须完整、Tensor
对象保持不变，不滤掉额外字段、不改变数值、设备或时间门。loader／plan／worker新150项
CPU测试通过，R2新增适配与worker37项通过，独立37项通过；不是靠放宽质量标准接通。

当前／最近过去RGB与任务原文是模型输入，depth、姿态、gold、未来画面与source名称
不进入Actor。raw输出全部保存：7个未选中的距离为负，STOP没有距离／角度目标；没有
取绝对值、截断或用固定尺度修补。前后模型参数、head、归一化、来源与运行时身份闭合。
本轮新optimizer／generation／flight均0；旧head3000与LoRA280分别继承，两次相机请求
累计16而不是重置为8。owned容器stop／remove均0，worker退出，13个其他容器未改变。

主机请求接受耗时首条1.774s，其余0.550–0.686s，包含prefix处理、融合、CPU动作头与
最终绑定检查，不是纯GPU耗时。sensor跨度0.232s、host收到跨度9.4177622s；没有曝光到
主机的时钟映射，不能宣称端到端实时、Jetson实时或飞行闭环。完整安全context仍unknown。

独立文件终态审计已核16份payload共68,812,800B、522个文件、8条raw／request与head四
张量；没有独立重跑模型。[独立审计及范围修正](evidence/onboard-navigation-gazebo-learned-shadow-final-audit-20261008-r2.json)
明确24.281秒属于整个采集验收流程，不是精确容器存活或模型延迟；原记录不重写。

下一步是同输入的底座／warm／候选公平对照，以及[新三维教师数据与动作覆盖](onboard-navigation-gazebo-training-data-next.md)：
真实执行后的反馈、横移／升降／变距离、受阻恢复、多任务及完整未见world留出。
默认模型、0.3.0-pre.8、质量晋升、Jetson、HIL／REAL、室外GNSS和最终发布门都不变。

## 历史接线阶段：尚未做候选推理时的记录

## 2026-10-08：真实Gazebo相机到共享导航输入已闭合，尚未做候选推理

新入口实际运行固定Docker/PX4/Gazebo，native0 `35c47c`，总耗时19.281秒；8份1280×960
RGB＋8份深度，8个共享请求（history为1、随后七个2）闭合。来源QPC/perf时钟同一实例，
十项小source结束保持一致；原helper恢复，owned stop/rm均0、容器移除，命令0。
独立纯文件native0 `f5cf0d`重hash全部68,812,800B payload及wire/RGB/Actor/request，
15次sourcefact引用与当前十源均一致；不是再次运行模型或Gazebo。
[实际回执](evidence/onboard-navigation-gazebo-input-capture-actual-20261008-r1.json)绑定四项产物，
[第一帧预览](../artifacts/evaluation/gazebo-navigation-sourcepose-20261008-r1/preview-frame-000000.png)
无损解码真实三维道路／建筑。三个组件root合计120 CPU fixtures／Ruff通过，独立37项通过。

这是地面只观察，不是训练就绪或专家轨迹：model/processor/inference/optimizer/flight均false，
模型SHA只声明未认证，完整安全context仍unknown。八帧sensor跨度0.232s、host收到跨度
4.7689256s，未建立曝光→主机映射，不宣称实时、质量或GUI。精确候选＋head只观察worker、
公平底座／warm／候选对照和[新三维教师数据](onboard-navigation-gazebo-training-data-next.md)
仍待执行，默认／版本／Jetson／HIL／REAL／室外与发布门保持。

## 2026-10-08：共享导航相机与学习动作后端完成软件验证

新增实际Gazebo wire→同NavigationActor输入和preloaded学习动作后端，两组83 CPU fixtures
通过；独立review修复最后一次图像绑定校验的耗时门。原文与最多两张当前／过去RGB才入
模型消息，pose／depth／wire／gold留在外侧；四prefix融合→FP32特征→保存head与TRAIN
归一化，不生成token、不补固定尺度。组件不负责模型认证、绝对超时、去重或飞控授权。
[接线状态](onboard-navigation-gazebo-shadow-20261008.md)和
[软件回执](evidence/onboard-navigation-gazebo-shadow-components-software-20261008-r1.json)
不代表实际Qwen／Gazebo推理或自主飞行。新独立相机env依赖已安装、真实import通过，
不改原训练env；独占采集入口的时钟传递、helper回调中断清理与来源终态门已通过，
实际相机终态见首节，不能把它当模型加载或推理。

## 2026-10-08：208条真实特征与稳定训练已完成，导航质量仍不合格

单GPU提取已真实退出0，worker900／launcher45772退出；208条任务＋当前／过去RGB的
共享输入正常闭合，四prefix与CPU参考逐条一致。保存的208×2048 FP32矩阵精确重载，
参数与显式来源结束不变，0生成／0优化器。[实际缓存回执](evidence/onboard-navigation-candidate208-feature-cache-actual-20261008-r1.json)
封存八项绑定；独立纯文件审计重算完整矩阵及208行SHA与425984个有限值，来源中243个
唯一小文件重hash，其余308个唯一路径仅stat，不冒充重新运行模型或重读大权重。

固定计划后纯CPU实际训练11个动作头，每头200次AdamW更新；九个整段episode-out诊断，
另两头只描述自身TRAIN。2200次新更新／8800四阶段日志闭合，旧800合计3000；LoRA280
另账且不新增。502条raw全部保存后才评分，11头与各自TRAIN归一化保存后精确重载，
最终训练loss重新测量均改善。实际native0 `decfdc`，worker53236／launcher36224退出，
PID检查`9d0283`；没有GPU／底座／processor／PNG再加载。
[训练实际回执](evidence/onboard-navigation-stable-cache-training-actual-20261008-r1.json)封存，
独立`21008a／6aca32`核WAL、保存与显式来源，`b0857f／9a75a7`逐raw重算计分；
审计是纯文件检查，不冒充再次运行模型、重测loss、实时fsync或transitive DLL验收。

留整段诊断合法动作46／208，动作＋标签尺度共同正确5／208（均STOP）；55条尺度被拒。
同TRAIN描述结果为old86动作51／86、joint3／86，all208动作82／208、joint9／208；
它们不是独立测试，也不证明新增数据帮助。尺度阈值只是严格标签复现，不是实飞容差；
即使不看尺度，动作选择本身仍不合格。原底座已见old86，不能称官方／独立底座holdout。

数据为old86＋new122、九段路线／两个scene，不混开发59或NEW82。178条前进全为3m，
转向只有±30°，没有升降／横移／多尺度／受阻恢复或完整返航任务训练。修复数值稳定性
不等于学会自主导航。下一步先公平比较原始底座／warm／候选的相同输入，再补真正Gazebo
多任务与动作覆盖，并做只观察、不执行的共享Actor闭环；不盲加同一候选更新。

[Controller调度预检](evidence/onboard-cache-worker-placement-preflight-20261008-r1.json)
无合格在线节点，本轮实际在本机完成；没有远程执行证据。默认模型、飞行、实时、Jetson、
HIL／REAL／GNSS与最终发布门不变。下方未训练／运行中的描述为历史阶段，不覆盖此终态。

## 2026-10-08：稳定训练与真实特征缓存组件完成，尚未开展新真实训练

新增[稳定CPU组件](../scripts/onboard_navigation_stable_readout.py)：训练行专属归一化、
小学习率AdamW、最后一次更新后重算真实训练loss；失败保留四阶段计账并阻止不确定
更新继续。小型FP32实际字节缓存及有序Actor／四prefix／episode来源绑定、head与训练
归一化一起保存重载均有检查，调用方声明不是实际模型来源验收。

root独立64项fixture＋36项doc/parity共100passed/native0 `c6fd8d`，Ruff／format和两份
代码SHA核对native0 `4cbbce`；独立全文审查＋stdlib预算诊断native0 `048676`。
[软件回执](evidence/onboard-navigation-stable-readout-software-20261008-r1.json)封存。
fixture会训练小tensor头，但不是本项目真实导航训练，没有新GPU模型或新真实feature缓存。

下一阶段先以真实加载／共享CPU输入／结束来源准入生成缓存，再锁实际稳定训练预算，
旧800头及280LoRA成本继续保留。开发59已观察，不能拿来挑模型冒充finalholdout；
新122仍只完成输入验收。默认模型、飞行权限、Jetson与最终发布门均未变。

## 2026-10-08：六条新增3D路线全部通过实际CPU输入验收，尚未训练

修复版CPU122 session50728实际native0 `b5b317`，worker42000及相关launcher已退出
（`57ee08`），耗时99.987s。122条共享official prefix及response-only监督元数据与旧记录
canonical精确相同，prepared整文件byte-identical，122来源会话正常关闭；56小source、
10processor、21runtime及10optional runtime前后绑定闭合，CUDA前后未初始化。

可选Arrow导入局部拒绝2次，Arrow／datasets始终不在已加载模块中；自己的finder已移除，
原finder身份／顺序保持，合法新增Six finder留存。没有清理模块掩盖失败或修改site-packages。
99项软件测试、Ruff、独立保存元数据与小来源审查native0 `0a3697`；
[实际CPU准入回执](evidence/onboard-navigation-indexed-cpu122-admission-20261008-r3.json)封存。
回执r3只校正来源计数命名（56来源＋独立plan＝57），保留r2，不是第三次实际运行。

这是六条同scene的original TRAIN路线，108个3m前进、4左转／4右转±30°、6序列STOP；
增加prompt和路线，不增加场景、连续尺度或升降覆盖。旧失败仍保留，新增数据尚未训练，
没有模型质量、默认替换、Gazebo自动飞行或硬件验收。下一训练计划须单独固定整段分组与预算。

## 2026-10-08：动作头实验正常结束，但优化不稳定、导航质量失败

真实session20887已native0 `3f3149`，launcher／worker退出（`ca79e6`）。底座及LoRA冻结，
145条prefix融合特征观察、4×200＝800次CPU头更新、231条raw、保存与精确重载全部闭合。
独立纯文件审查`4d4534`／`950e6b`／`0a50de`核对参数、WAL、Actor与四prefix、逐类配对评分；
[实际回执](evidence/onboard-navigation-action-readout-actual-20261008-r2.json)已封存。

LOEO86 action29／joint0，同episode置换分类逐条不变；开发59 action及joint只有2，全是
STOP，输出56STOP＋3非法尺度左转。2条升降未支持仍留分母，17验证1正确／42测试1正确。
四头pre-step loss从4–6震荡至321–424且最小值均第一步，说明当前SGD配方未收敛，不能
据此否定VLM特征或把错误仅归因JSON。原800预算已耗尽，旧280不重置；下一稳定训练
组件已通过train-only归一化、feature字节缓存和终态loss门的软件验证，尚不是新的真实训练结果。

下方“59／145仍运行”等条目仅保留历史过程，不覆盖此终态。质量、默认替换、飞行、
实时、Jetson及最终发布门仍false。新122输入验收与模型质量分开记录。

## 2026-10-08：122条新训练输入全部编码，整轮因可选导入检查拒绝

59项root软件测试／Ruff／独立源码审查通过后，计划真实native0 `d4ec6f`，CPU实际
session2540以native1 `d31e7f`结束。122条official prefix配对和full-chat response-only
监督元数据、122正常verified sessions、46小source／10processor／21runtime前后不变，
15实际origin和CUDA始终未初始化均记录；但末尾禁止模块检查失败，整轮准入false。
独立后验审核native0 `7ffcae`，[失败记录](evidence/onboard-navigation-indexed-cpu122-failure-20261008-r1.json)
已封存，不把122单样本编码完成追认为整轮native0或训练通过。

单独小CPU诊断native0 `962e80`核到官方processor仅导入也经pandas导入可选pyarrow；
新进程的局部Arrow隔离探针native0 `edb132`，processor构建成功、2次可选导入被拒，
Arrow／datasets未入、未处理图像或模型且CUDA未初始化，自己的finder正常移除。
这不是重跑122或原失败进程的历史模块快照。新v2仅隔离无用可选依赖、保留禁止门，
会绑定旧失败与122保存元数据并逐条比较；这个历史实现阶段已由顶部v2实际成功取代，
仍无新数据微调，原native1失败保持不变。

## 2026-10-08：跨数据原图像素与整段分组审核真实闭合

CPU分析session6082已native0 `689be7`，root46软件用例及Ruff通过；独立小来源／保存
identity图重算native0 `0a2084`，封存[实际overlap记录](evidence/onboard-navigation-dataset-overlap-20261008-r1.json)。
old86＋NEW82＋NEW122共290原PNG全部strict校验及RGB解码，290current＋278past＝568
引用闭包覆盖，12episode保留既有group得到10component。exact RGB／episode身份重复、
跨train/dev冲突与跨dataset train重复均0，exact-overlap gate true；54小source＋5Pillow入口
前后不变。独立审核不再解码PNG，不冒第二次像素验证。

没有近似重复或独立性证明，没有官方holdout或processor准入；不drop／merge／改split。
该审核自身不运行processor；后继CPU122现已准入，见顶部终态，尚未训练这批新数据。
动作头session20887现已闭合但质量失败，见顶部终态，不把上述数据审核当模型质量。

## 2026-10-08 历史过程：修复后的动作头实验进入特征提取

v2新增29项定向CPU测试、Ruff及独立导入顺序／共同800预算审查通过；计划native0
`6add54`，唯一GPU运行session20887已启动。真实stream reader覆盖底座及adapter，原
加载闭合门通过；最近已记录59／145条实际特征（`8a7267`），仍运行中，不把progress
信号当终态。原r1失败完整保留，四计数0；r1＋r2共用800，不授第二预算或重置原280。

并行跨数据审核已真实native0 `689be7`：290原PNG全部解码，覆盖568次Actor引用，
未发现exact RGB／episode身份跨训练开发重复，gate true；独立产物审核及封存已完成。
这不是near-duplicate独立性／官方holdout／processor准入，NEW122还未与旧数据混合训练。

## 2026-10-08：动作头真实首轮在加载观测检查停止，零训练消耗

修复后入口及组件163项CPU测试、Ruff和独立运行环境／配对统计审查通过；但实际r1
session10533以native1 `253d54`结束。模型与LoRA的实际参数身份正确，新增环境检查提前
导入PEFT，使Transformers缓存了既有streaming scope安装前的safe_open入口；完整底座流式
读取观测缺失，所以保留原检查并拒绝继续。独立原因复核native0 `9ed350`。

实际features0、raw0、head optimizer四计数0，worker7244退出，目录只有report；
[失败记录](evidence/onboard-navigation-action-readout-loader-failure-20261008-r1.json)已封存。
新v2只修导入时机，原r1及冻结依赖不改；仍共用800小层更新总上限，不重置此前280次
base／LoRA消耗。当前没有动作头模型成绩、实时加速、默认替换或飞行通过。

另一个子智能体并行实现NEW122与旧训练／开发数据的原图像素重复、完整episode分组审查；
当前还未实际执行该新审核。下方NEW122数据审核与NEW59模型失败成绩继续有效。

## 2026-10-08：六条新三维路线122原图与逐步动作审核真实完成

计划发射native0 `04055a`，CPU处理session33533真实native0 `7f6c5c`；root37测试native0
`4f2666`、Ruff及scope diff通过，独立产物／来源／几何审查native0 `7e1450`。
[NEW122已封存记录](evidence/openfly-indexed-training-supervision-20261008-r1.json)绑定17控制来源，
[人话说明](onboard-navigation-indexed-training-data-20261008.md)记录范围和下一步。

6个同env26 TRAIN独立episode，实际122张原PNG共83,617,855B，实测853×480；116
successor＋6末步STOP，raw108straight／4left／4right／6stop。严格schema／原pose对齐、
原编码PNG检查与解码、FRD planned gold、正常ClosedSession和Actor隔离全部实际通过。
source47＋CPU实际入口10前后相同；独立逐SHA122PNG／47source／10runtime，6pose
重新parse并复算geometry，191distinct绑定；不是第二次像素解码或独立原native退出观察。

仅既定786432B annotation前缀，实际解析205记录（旧发现索引196是candidate-cap停点），
六原record compact canonical与派生selection精确一致；不验证405MB全文／完整array。
未混旧86／新82、未加载model／processor／GPU、未优化或替换默认。训练前仍需视觉重复
分组与真实processor输入准入；train-ready、holdout、physical／execution／release全false。

小动作头真实实验入口并行实现／软件复核；尚未提取真实feature或训练sidecar，不把该122
数据处理成功当模型学习成功。下方payload／NEW59运行中为前序观察，最新状态以上节为准。

## 2026-10-08：59条真实跨场景推理闭合，动作及数值质量未通过

实际GPU推理native0 `30d143`，launcher／worker退出，逐类分析native0 `1d41f1`；
独立raw复算、来源身份、59条实际prefix／hook／token观察复核native0
`f4ae31／354b1b／57b50b`。[真实评估记录](evidence/onboard-navigation-original59-actual-evaluation-20261008-r1.json)
已封存，原始预测逐条先flush／fsync并全部关闭后才评分，没有把gold输入模型。

| 内部开发检查 | 支持数 | 动作正确 | 动作及数值正确 |
| --- | ---: | ---: | ---: |
| 全体新场景 | 59 | 16 | 0 |
| dev-validation | 17 | 15 | 0 |
| dev-test | 42 | 1 | 0 |
| 非forward少数类 | 5 | 0 | 0 |
| STOP | 2 | 0 | 0 |

严格输出格式59／59不等于行动正确。forward54中仅16条预测可比，条件位移误差均值5.5m，
另38条wrong-kind不能藏进这个均值；全59有43条wrong-kind。报告均值5.826s是H2D之后
含prefix观察／generate／sync／D2H／decode的计时，不是CPU编码、在线E2E或Jetson实时证据。
原23train行未进模型；publisher TRAIN内部17＋42分组不是官方holdout。没有新增优化、
匹配59的base／warm模型对照、默认替换、执行授权或质量晋升。

并行的6条TRAIN原路线获取实际native0 `29fe60`，12原文件共83,692,236B逐字节digest
校验；独立opaque流复核native0 `84d1cc`通过。
[有界payload记录](evidence/openfly-indexed-route-payloads-20261008-r1.json)不冒充schema／
RGB／pose几何或训练就绪。122仍是metadata声明；新的CPU逐route审核正在实现，
派生annotation只保留已验786432B前缀中的原记录，不读405MB全文或造标签。

## 2026-10-08：267训练及保存后重载完整闭合（前序阶段）

训练实际native0 `aa1ca2`、独立fresh reload实际native0 `b40874`；launcher／worker退出，
活动锁消失。实际v2终态审计native0 `ea780c`及独立复核`fcece8`通过，
[完整终态记录](evidence/onboard-navigation-recovery267-terminal-20261008-r2.json)已封存。
34个exact5快照／1068行WAL／534个draw事件、四计数267与旧13＋新267＝280相符；
候选与重载86条raw／token／actor／prefix／诊断逐条一致。当前206小来源SHA、6大型stat
及显式原warm adapter字节检查的范围明确，不冒充所有输入或5GB底座独立重验。

这说明保存／重载链可信，不说明模型会飞。动作仍27／86、数值8／86、STOP0／3，
旧warm动作55／86；候选不晋升，不加旧86重复轮数或重置原280消耗。

随后root发出NEW59精确计划（`f0c584`退出0），session32864完成真实GPU推理；
只选17dev-validation＋42dev-test，排除23train。真实闭合失败成绩已在上节公开，
publisher TRAIN内部开发分组不是官方holdout。
[本轮人话说明](onboard-navigation-terminal-and-dev-evaluation-20261008.md)记录当前与历史边界。

[网络复查](onboard-pretrained-navigation-search-refresh-20261008.md)新增LightVLN、
VLN on the Fly、AeroVLA和AutoFly，分别核公开权重／方法／小型部署与限制；尚未运行新
预训练模型或改变默认引用。下方“重载仍运行／59未运行”为此前观察，已由最新闭合结果取代。

## 2026-10-08：真实原始路线索引完成，下一批数据不再只有3条任务

有界索引实际原生退出0，65项软件测试、Ruff及独立真实前缀复核通过。只读786,432字节，
196条原始记录形成120条不同路线／Prompt、5scene、4077个候选帧索引；因下一条会突破
4096帧预算而正常partial停止，不宣称完整扫描或重新验收全405MB SHA。
[真实索引证据](evidence/openfly-bounded-annotation-index-20261008-r1.json)封存来源与边界。

scene18／26保留train，共45路线；scene23有17dev-test；gz／sh共58保持未分配，无训练准入。
本前缀没有scene16的dev-validation，不能说四角色数据都已准备。10条含-1／-2 padding的
注释只隔离统计，不转成飞行动作；65条非AirSim跳过。最初大整数转存导致计划身份拒绝、
第二次padding拒绝均保留，修复的是发现工具，不改旧严格动作解析器或模型阈值。

4077是metadata推导，不是已下载／验证RGB；聚合codes不等于原pose逐步FRD gold。
下一步仅查询最多6条train路线精确publisher metadata，再做有界RGB／pose获取和逐步
几何检查。原候选fresh reload仍运行，59模型评估未运行；默认模型、版本和发布门不变。

## 2026-10-08：动作读出小层完成软件验证，下一轮不再只学习文本格式

[新的动作读出组件](onboard-navigation-action-readout-status-20261008.md)已实现；作者／root
各89项CPU小tensor测试、Ruff与独立审查通过，修复单条batch类别权重抵消缺口。
小层在同底座特征上学习四类行动与距离／转角，未知数值不补零，不硬填3米或30度，
不会伪造文本生成token。四类是诊断范围，不代表已支持升降／横移／完整任务推理。
[软件验证记录](evidence/onboard-navigation-action-readout-software-20261008-r1.json)
只证明组件行为，没有真实特征提取、探针训练、sidecar重载、延迟或飞行验收。

原候选训练已实际结束、动作质量仍失败，独立fresh reload继续运行。真实有界数据索引
发现原注释的-1／-2 padding码，严格旧动作解析器拒绝；现已只隔离跳过并完成真实索引，
见上方最新阶段，不扩大飞行动作标签。下方为训练阶段与历史观察；默认模型、版本不变。

## 2026-10-08：完整训练正常结束，候选会输出格式但动作还不可靠

06:25训练phase实际原生退出0，旧launcher／worker已退出；267次attempt／return／sync／
completed全部完成，保留旧13次消耗、累计280，没有重置预算。最终候选86条原始推理和
训练终态报告已保存；来源前后身份一致，独立复算原始输出与报告相符。
[完整训练阶段记录](evidence/onboard-navigation-recovery267-train-closed-20261008-r1.json)
取代“candidate86仍运行”的当前状态；下方旧观察与已封存证据保留为历史，不改写。

| 86条训练内再测 | 旧warm模型 | 新候选 |
| --- | ---: | ---: |
| 严格输出格式正确 | 69 | 86 |
| 动作类别正确 | 55 | 27 |
| 动作及数值正确 | 0 | 8 |
| 停止动作正确／支持 | 0／3 | 0／3 |

四类macro recall有所上升，但主要由7条right贡献，不能掩盖前进19／70与停止0／3。
19条类别正确的forward全部输出2米而非3米；其1米误差不含另51条wrong-kind。
这86张图全在同一scene、仅3条episode／prompt，不是独立holdout。生成计时mean约
6.37秒，仅覆盖H2D后generate／sync／D2H／decode，不是在线完整决策延迟。

06:26已启动独立新进程重新加载实际保存adapter，06:35仍运行，当前没有reload终态。
其后须实际完成v2终态审核和59条跨场景开发评估，再讨论下一独立实验；不盲加同86更新。
并行准备有界多场景annotation索引，先找真实方向／STOP／升降数据，再复核逐步几何。
学习型动作头仍只是候选路线，不宣称已实现或加速。默认模型、版本、质量、实时／飞行／
Jetson／HIL／REAL／室外GNSS与最终发布门全部不变。

## 2026-10-08：新路线评估会按动作揭示错误，不用多数类总分代替导航能力

独立59条结果分析工具完成：作者／root各56项测试、Ruff及只读审查通过。只读闭合评估
report和小文件，核17＋42开发角色／排23train；真实raw＋gold重算，与stored score及
summary比对后才发布。新增每类support／recall／macro、非forward5条、STOP2条、误差
coverage／wrong-kind／unknown和分区结果；原始配对与预算合格候选配对分开，cap或超时
不能算一次可用成功。数学always-forward参照无假token、延迟或模型执行记录。

标签数学参照54／59约91.5%，五个有支持类别macro仅20%，非forward0／5；不是模型实测。
[软件验收已封存](evidence/onboard-navigation-original59-analysis-software-20261008-r1.json)，
实际59报告缺失时CLI退出1，没有分析目录或模型加载。原267已保存，candidate86进程仍
在运行，完整退出／fresh reload／新59模型成绩尚未完成。质量、默认模型、版本、实时／
飞行／发布门不变；人话说明见[怎么判断真正学会下一步](onboard-navigation-result-interpretation-20261008.md)。

## 2026-10-08：真实保存格式的审计缺口已修复，完整模型结果继续等待

新的v2审计明确接受并验证PEFT的五文件保存格式：原四文件＋README，不忽略未知extra。
作者和主智能体各50项针对性测试、Ruff及独立只读审查通过；README在快照检查和最终来源
重验中均校验SHA，旧r1源码／测试／失败／证据保留。README是当前观察的metadata，不是
旧receipt声明或模型质量证明；其余训练、日志、实际参数和重载原始输出门全部保留。

[新v2软件验收](evidence/onboard-navigation-recovery267-terminal-audit-software-20261008-r2.json)
已封存。主智能体实际CLI仍观察到active-phase并退出1，没有审计目录或live WAL读取。
267更新与final保存已完成，但同一个GPU进程还在candidate86推理；完整phase退出、fresh
reload、跨场景59条实际模型结果仍未完成。默认模型、版本和实时／飞行／发布门不变。

另已核对[默认模型绑定诊断](evidence/onboard-current-reference-binding-diagnostic-20261008-r1.json)：
当前reference、产品版本和parity身份与本轮前置观察一致，未切换本轮LoRA。旧20261006
receipt内的reference SHA与实际文件有相邻两位顺序差异；旧文件和首次校验失败保留，
另封当前诊断，不据此推断默认模型被改动，也不声称已查清历史差异的起因。未读ONNX权重。

## 2026-10-08：267个更新已完成并保存，候选推理仍运行

实际GPU进程输出267／267，最终adapter回执已生成；独立标准库审核核对112个FP32张量、
802,816有限参数的真实保存字节摘要，匹配receipt且不同于原warm模型。1068行不可变前缀
四阶段均267、保留旧13成本、累计280，四核心文件before／after SHA一致；没有读取live WAL。
[封存的最终更新快照](evidence/onboard-navigation-recovery-v2-checkpoint267-20261008-r1.json)
只是已保存模型的阶段证据：进程仍在candidate86推理，没有train终态报告，fresh reload、
完整结束来源复核、质量和实时／飞行仍未验收，默认模型与版本不变。

同时发现真实格式缺口：PEFT为全部34个快照额外生成README，目录实际exact5；34份小README
真实SHA相同并稳定。r1审计器的fixture只使用exact4，会误拒真实目录；首条只读准备失败保留，
没有改写sealed r1。独立v2已明确绑定README并通过软件验收，保留其他所有门，不忽略extra。
README只作为本次观察的metadata，不伪称旧checkpoint已声明它，也不是参数或质量证据。

## 2026-10-08：完整训练／重载审计工具通过，真实终态仍待完成

终态审计工具已新增，50项小文件／FP32 fixture测试经作者和主智能体通过，最终全文独立
审查及Ruff通过；先检查完整phase报告与活动锁，再读日志。后续将核267四阶段计数、
1068 WAL／534 draw、33个周期＋1最终快照、112 FP32原始摘要和全86重载raw／token／Actor／
prefix，并用冻结评分器重算四组86诊断。外部运行时只限冻结计划的绝对只读输入，输出不越仓库。

主智能体实际CLI观察到活动锁时退出1，未创建审计目录、未读取live WAL。
[封存的软件验收](evidence/onboard-navigation-recovery267-terminal-audit-software-20261008-r1.json)
不是实际完整训练审核；GPU训练仍运行，完整train＋fresh reload真实退出及进程记录仍待验收。
文件audit不会代替原生退出码和进程退出证明；大原始源独立复查仍有stat-only边界。
后续发现PEFT README的exact5格式缺口，见上方最新阶段；r1仅fixture通过，不作真实终态验收入口。
当前quality、默认模型、版本、实时／飞行／硬件及release门保持不变。

## 2026-10-08：59条新路线评估入口通过软件验收，完整模型结果仍待完成

新增离线评估入口，作者与主智能体分别通过83项测试，Ruff及独立只读审查通过。
固定只选17条dev-validation＋42条dev-test，23条训练侧样本不进入模型；原任务与当前／最近
过去原RGB走同一Actor、prefix处理与精确candidate loader，不把姿态、future或gold送入模型。
全59条raw先持久化后评分，原始诊断与预算内有效结果分开；触顶192 token或超过30秒不计
eligible。完整输出prefix、suffix token及raw decode复查与异常hook恢复均有软件覆盖。

[封存的软件验收](evidence/onboard-navigation-original59-evaluation-software-20261008-r1.json)
不表示实际模型评估通过。主智能体实际验证：完整train＋fresh reload回执缺失时，CLI退出1，
没有计划／新输出目录，没有第二个模型worker。原GPU训练继续，完整267、candidate86和
fresh reload尚未终态验收；实际59条模型预测数仍0，quality／默认切换／实时／飞行／发布门未变。
人话说明见[到另一条路线还能不能选对动作](onboard-navigation-original59-evaluation-20261008.md)。

## 2026-10-08：82条跨场景开发样本已实际生成，GPU训练继续

原始三维场景的82张RGB已形成独立开发数据，实际转换原生退出0，62项针对性测试和
独立终态审核通过；201项仓库来源前后不变，实际图像／YAML入口六文件绑定，完整验证
会话正常结束后才发布样本。模型输入仅原任务文字、当前与最近过去RGB，不传姿态、
未来画面或动作答案。数据与正在训练的旧86条分开保存，没有改旧数据或冻结训练计划。

划分为23条train、17条dev-validation、42条dev-test；原始来源均为publisher TRAIN，
不是官方unseen。79个相邻姿态／动作和3个终末STOP复算通过，升降标签保留；总计75前进、
2右转、各1升／降、3停止，仍无左转和多样距离。独立审核复查全部82个typed样本及
202个小输入文件SHA，5个大文件只核当前大小；没有再解码图像或独立重算像素identity。

[封存的NEW82结果](evidence/openfly-original82-supervision-20261008-r1.json)不意味着训练就绪、
模型学会新动作、实时能力或飞行通过。旧86 GPU训练仍按原计划每8步保存检查点；完整267、
candidate86、新进程重载和结束来源复核尚未验收。以下为此前阶段记录，以最新结果为准。

## 2026-10-08：导航训练实际恢复，首个已保存候选通过独立检查

独立v2保留r1字段检查失败，用实际constructor／warmup schema修复准入，100项定向测试
由作者与主智能体分别通过；独立审核直接读取旧真实报告和checkpoint小header验证修复，
没有加载模型或造PID。v2真实来源计划和CPU准入原生退出0，实际GPU训练已进入更新。

首个checkpoint-008已保存，绑定8次更新的32行不可变阶段日志；独立标准库审核重算
112个FP32张量、802,816有限元素和实际参数摘要，确认候选不同于原warm模型，累计旧13＋新8。
[封存的8步阶段证据](evidence/onboard-navigation-recovery-v2-checkpoint008-20261008-r1.json)
不封存增长中的live journal，也不把保存模型当作完整训练／质量验收。训练继续每8次保存，
完整267更新、candidate86、fresh reload和结束来源复核仍未完成；默认模型与发布门未变。

## 2026-10-08：完整CPU86通过，GPU启动字段检查缺陷正在修复

官方processor完整86条实际运行原生退出0，86个单样本会话最终验证通过；完整输入metadata
及保存的143,019字节表与旧记录相同，196来源前后不变，178边界观察。独立只读审核通过，
复核55个小binding及所有来源当前size，不重编码或重散列大资产。
[CPU86封存结果](evidence/onboard-navigation-closed-cpu86-20261008-r1.json)不代表GPU或飞行通过。

NEW267真实来源计划和admit-cpu均已完成，但GPU阶段在加载模型后因constructor报告没有pid，
验收代码访问该字段而KeyError，实际退出1；尚无反向预检、optimizer、journal或候选保存。
本次消耗0次更新，旧13次消耗不变，剩余上限267。原source／tests／plan／report保留，
[实际失败](evidence/onboard-navigation-recovery-loader-failure-20261008-r1.json)不改成成功；
独立v2修复真实schema门并增加实际报告正负例，再继续训练。不是GPU显存不足。

另已完成[同底座学习型动作头设计](onboard-navigation-learned-head-experiment-20261008.md)，
仅为降低文本生成开销的可验证替代实验，没有实现／训练head或测得延迟收益。

## 2026-10-08：82张新三维原图的几何检查通过，尚未形成训练／留出数据

实际离线运行原生退出0：三条原TRAIN路线82帧，79项相邻姿态与原动作几何核验、3项终末
零STOP检查通过；STOP不表示降落或任务完成。按已审的单位尺度，将原始世界坐标位移转换
为body-FRD米、右转正弧度，不从未知dt推速度。旧86帧与新82帧全部真实解码，6组场景对
按RGB摘要及现有dHash／均色规则没有发现匹配边；不是通用视觉独立性证明。

182项使用的仓库／资产字节前后相同；27项测试、Ruff及独立只读报告审核通过。
[封存检查](evidence/openfly-original82-geometry-visual-audit-20261008-r1.json)明确不包括完整
Pillow／PyYAML运行时封存。新数据75前进、2右转、各1升／降、3停止，没有左转；平移全3米，
转向全30°，不能声称多样尺度或全任务判断已学会。train／dev-validation／dev-test仍只是
原来声明的路线角色，没有创建样本、晋升划分、读取保留的官方seen／unseen集或运行模型。

新的完整CPU86验收和NEW267训练入口正在实现／独立审核；旧13次失败账与已保存检查点
不变。远端资源预检的plan及原子submit均返回no_eligible_node，未派发任务；继续本机工作，
不改应用／pagefile，也不将远端不可用当成本地实现的阻断。

## 2026-10-08：单样本闭合编码通过真实 CPU 配对检查，尚未恢复训练

原生退出0：失败draw14及原前进／左转／右转／停止四条，共5个不同真实样本，
旧→新每样本各运行一次，共10次编码、5组配对。完整输入metadata与原官方CPU记录一致；
新路径5次会话最终来源验证通过，192项来源前后相同，保留93次观察，CUDA未初始化。
worker及venv启动器正常退出，没有模型／optimizer或飞控指令。
[封存诊断](evidence/onboard-navigation-closed-sample-cpu-profile-20261008-r1.json)单独保留，
独立审核保存的metadata／计时／小binding通过，没有重跑processor或大来源SHA。

本批次inclusive编码平均旧32.58秒／新16.74秒；固定顺序、各一次，不作为公平性能基准。
working set／commit记录的是进程生命周期峰值，不是显存、独立阶段峰值或旧故障现场。
旧MemoryError根因、内存收益、完整86条新路径、GPU恢复与质量提升仍未证明。

新增输入入口／采样／profiler与Actor／监督回归分别163＋51项通过；独立NEW267预算合同
63项通过，限制13已消耗＋267新attempt不超过280、覆盖86来源，不复制旧journal或授权训练。
真实来源绑定的新抽样计划、训练入口／journal和完整CPU门仍待完成，不能叫exact resume。
[用普通语言说明这次恢复](onboard-navigation-input-lifecycle-recovery-20261008.md)。
此前13次失败账、172条基线、82张新原图、默认r33／版本／发布门均保持不变。

本轮又查询官方HF API：3DG-VLN及WorldVLN均返回非gated权重元数据，revision与旧记录相同；
[网络复查](onboard-pretrained-navigation-search-refresh-20261008.md)单独记录，没有下载或运行这些模型。

## 2026-10-08：续训13次更新后主机内存失败，整体已退出1

监督器39122原生退出1，train worker也退出1；原始日志和
[失败证据](evidence/onboard-navigation-streamed-training-failure-20261008-r1.json)单独封存。
四类梯度预检只做forward/backward，不执行更新。正式journal有52条事件，原计划前13个draw的
attempted／returned／synchronized／completed均为13，更新不确定标记为false；不是零次更新。

第14条输入准备在来源验证会话退出、读取1MiB文件块时触发主机MemoryError，没有下一次
optimizer attempt，也没有CUDA OOM报错。没有保存新adapter、候选评估或独立重载。
来源后验未完成，不能把sources_unchanged=false解释成已有来源发生字节漂移。
没有自动重试、重置预算、调整pagefile或关闭用户应用；此前基线与82张原图阶段的成功仍有效。

下一轮先测CPU输入生命周期与内存，尝试单样本共用一个完整闭合的来源验证会话，确保
来源验证成功后才进入loss/optimizer。这尚未实现或证明能解决内存错误；不修改本轮冻结
源码／计划，也不把未保存的13次更新当成可恢复checkpoint。

## 2026-10-08：三个新场景的82张原始三维训练图像已实际提取

实际CPU提取原生退出0，三条路线分别23／17／42张，总计56,041,231原编码字节，
全部为853×480 RGB PNG。原字节没有转码，完整性检查和真实解码通过；独立复核82个
输出摘要、PNG结构与shard／row／frame身份，原pose也与保存的82行元数据对应。
174项仓库来源、10个明确CPU runtime来源前后相同；35项CPU测试通过。
[原图提取证据](evidence/openfly-parquet-original-images-20261008-r1.json)与先前获取分片／
只校验元数据的回执分别封存。人工查看一张原图，确实是公开三维城市场景画面。

首次r1计划启动因七个yaw数值被序列化为不同JSON数值类型而被拒绝，原生退出1且未写图；
旧计划和失败保留。成功使用另建r2计划，保留原始导出字节，没有放宽来源校验。

本阶段不是本机Gazebo闭环或实飞验收。图像仍来自原TRAIN路线，开发用划分只是建议；
视觉重复／独立划分、下一步动作FRD几何与训练就绪尚未通过。没有改旧86样本或运行中的
280次GPU计划，没有执行模型／optimizer、下载权重、接飞控或批准候选与最终版本。

## 2026-10-08：完整GPU基线结束，训练阶段已启动但更新尚未验收

底座与旧导航LoRA各86条真实推理全部完成，baseline worker及监督器观察的子进程退出均为0。
[完整阶段证据](evidence/onboard-navigation-streamed-baseline-phase-20261008-r1.json)另行封存，
保留此前partial观察和旧主机内存失败，不把后续阶段的状态倒写进旧回执。

底座格式／动作／尺度均0/86；旧LoRA格式69/86、动作55/86、尺度0/86。69条合格格式均为
前进，左转／右转／停止没有正确完成；55条匹配前进的位移误差为97米×42、7米×13。
这是旧训练样本上的重代入，不是独立质量证据，也不满足自主飞行要求。

旧LoRA生成计时mean6.343秒、p50 5.468秒、p95 9.159秒，仍从prefix H2D之后开始，
不是相机到执行延迟。来源180项、底座与原adapter参数前后相同，本阶段没有optimizer。
随后train子进程已启动；280次正式更新、候选保存／独立重载及全pipeline退出码仍未验收。

## 2026-10-08：三个新场景的原始分片实际获取与元数据校验通过

实际CPU获取／审计原生退出0，九个原始文件共56,091,088字节（约53.49MiB），大小与发布者
LFS／Git摘要均通过。三条路线共82行元数据（23／17／42）逐行匹配原pose，schema也通过；
161项仓库来源和5个明确CPU runtime入口前后相同，独立复核九个输出仍一致。27项CPU测试通过。
[实际获取证据](evidence/openfly-parquet-acquisition-metadata-20261008-r1.json)与先前“元数据文件存在”分开。

整份shard包含内嵌图像字节，但还没有读取图像列、逐张解码或提取；正在另做原图提取入口。
视觉重复／开发用划分、下一步动作的FRD几何映射、训练就绪与质量仍未验收。没有改旧86样本
或正在运行的GPU计划，没有加载新模型、执行optimizer或接飞控，旧缺图失败保持原样。

## 2026-10-08：已训练导航模型查验与实际底座对照

在线核到3DG-VLN公开完整权重约16.58GB、WorldVLN一套主干约35.07GB，均未下载或运行；
WorldVLN论文实飞推理在地面服务器，Jetson只做采集／接口，不能写成全机载验证。
Qwen-VLA官方发布说明未提供权重链接，第三方drone-vla-toolkit只有脚本；
[选型结论与来源](onboard-pretrained-navigation-model-options-20261008.md)写入仓库，默认模型未切换。

实际流式GPU底座对照已保存86条原始结果，逐条独立重算：strict／action／metric均0/86；
post-H2D生成计时mean7.396秒、p50 7.299秒、p95 8.351秒，不是相机到执行总延迟。
原LoRA对照仍进行中，baseline／pipeline原生终态未知，280步续训未开始；
[partial真实GPU观察证据](evidence/onboard-navigation-streamed-base-round-20261008-r1.json)不替代完整phase验收。

新数据来源已确认：3条既定原train路线的精确Parquet分片加原pose/config共56,091,088字节，
约53.49MiB；有界元数据请求native0、156来源前后记录相同，26项测试通过。
[精确来源证据](evidence/openfly-parquet-exact-route-metadata-20261008-r1.json)只证明文件发布及绑定，
该条为历史metadata-only快照；后续实际获取见上文。此快照本身不证明图片／schema／动作有效，
不读取官方seen/unseen，不把待验证图像当已可用holdout。

## 2026-10-08：导航推理接口与实验适配层通过软件验证

共享导航请求/结果158项、预加载实验backend89项测试通过；与流式CPU证据15项、文档8项
合计270项通过，原生退出0。backend只使用共享Actor前缀，不读取gold；保存实际suffix
token、原始输出与耗时，拒绝过期、截断、身份不匹配和持续的LoRA状态漂移。
这是纯Python替身的编排验证，尚未精确加载或运行真实模型、processor/GPU，更没有接飞控。
下一步复用已有精确加载能力并建立独占worker；[软件验收证据](evidence/onboard-navigation-inference-software-20261008-r1.json)、[连续自主飞行接线](onboard-navigation-live-integration-plan-20261008.md)。

多场景预检46项通过，但实际下载在指定env26/139目录发现只有pose、没有RGB，原生退出1，
没有取得新增训练/验证/测试图像。保留失败，另做有界的官方元数据可用性核查，不用假图或
把同场景重复帧划成holdout。该阶段GPU进度为历史快照，最新partial对照见上文。

## 2026-10-08 最新续作：流式输入替代全量张量驻留，GPU 基线执行中

旧均衡续训入口238项测试、独立官方CPU86验收通过；完整GPU流程却在基线输入编码阶段
触发主机`MemoryError`，监督器原生退出1。底座与旧LoRA已精确加载，但没有推理或optimizer更新；
它不是CUDA OOM，失败和冻结来源保留，不改写成成功。
新流式入口及相关模块287项测试通过，按访问重新编码单样本，不保存86条完整张量缓存。
新计划已封存，CPU86 phase原生退出0，GPU基线完整编码86条后进入推理；后续280次更新与
独立重载尚未通过。模型、旧LoRA、数据、280次抽样顺序和预算保持相同；尚未证明内存根因或训练收益。
它加载原LoRA后建立新优化器，不恢复旧optimizer／scheduler／RNG状态；仍为86unique同场景train回代。
默认飞行引用、实时导航backend、SIM/REAL和硬件验收不因此改变。
[执行入口与计划](onboard-navigation-balanced-continuation-plan-20261007.md)。下文旧阶段结果按历史保留。

## 2026-10-07 最新：共享导航输入已实现，官方 86 样本离线一致，动作质量仍不合格

新的共享导航actor输入基础已实现。官方本地Qwen3VLProcessor在CPU上处理全部86个
原始样本：监督样本投影与可观测输入构造的identity、四个实际tensor字节全部一致，
原生退出0，129个来源前后不变；最长453token、每图最大46080pixels，未超过4096／50176预算。
独立只读核验重开129来源、重算86个actor identity并核对已保存tensor记录和预算，退出0；
没有二次运行processor。这是保留的离线RGB投影，不是独立实时采集；新system指令
不能声称与旧训练prefix逐字节相同。没有加载模型、推理或optimizer，CUDA未初始化；
还没有接新trainer／导航live backend，未执行280次更新或替换默认飞行策略。
[共享输入证据](evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json)。

新入口完整读入625个BF16底座张量，原FP32 q/v LoRA初始化通过，三层验收和独立审核均通过。
随后原计划86个样本／一轮的四阶段更新全部完成，底座参数前后不变、adapter已变化并保存。
同进程第二次fresh底座重载出现CUDA分配失败，训练原报告仍退出1。新独立GPU worker已
精确恢复同一base与adapter，4个原输入／生成token一致，worker退出0；原外层supervisor因
Windows venv启动器与worker的PID不同退出1，这个失败保留。新增只读身份跟进检查已退出0，
真实启动器→worker进程链及命令一致，独立340文件字节审计也退出0。没有删除PID门、
重训或改写原失败；保存后的可复现性通过不替代动作质量。
4个train内probe：严格输出与动作各0/4→1/4，尺度0/4；唯一合法前进为10m、gold为3m。
这不是独立质量验收，不能接飞控。[完整加载证据](evidence/onboard-navigation-full-load-only-20261007-r2.json)、
[独立精确恢复证据](evidence/onboard-navigation-exact-reload-20261007-r1.json)、
[动作训练](onboard-openfly-navigation-pilot.md)、[实际执行接线缺口](onboard-navigation-execution-gap-20261007.md)。

[下一轮质量方案](onboard-navigation-quality-next-20261007.md)已写入：保留严格评分，训练与运行
共用导航输入，均衡前进／左转／右转／停止曝光，再补独立场景。四类各70次、总280次draw
的抽样表已实际生成，r1/r2字节一致并经独立重建核对，86个原样本全覆盖；仍只有一个场景、
没有holdout。原100步预算没有改；共享actor输入已实现，但独立训练计划、新trainer及
导航live backend接线仍待完成，未执行280次更新，旧失败和训练内评分不改。
[均衡抽样证据](evidence/onboard-navigation-balanced-schedule-20261007-r2.json)。
[下一轮续训实现方案](onboard-navigation-balanced-continuation-plan-20261007.md)已保存，
包含全部86个样本的公平对照、新优化器和逐draw日志；它不是已实现的训练入口或新训练结果。

## 2026-10-07：逐张量读取入口通过软件验证，完整加载遇到新的 CUDA 分配失败

新增reader82项、wrapper70项软件检查；与原兼容层和supervisor联合236项通过，
独立审核通过，31个runtime源加manifest校验。新完整load-only仍退出1，两个头部打开
成功、各读76248字节，但实际tensor payload为0；报告OutOfMemoryError申请约3.96GiB，
不把约6.93GiB的错误文字当精确可用资源或碎片化诊断。55源前后不变，独立审核native0
不是模型native0；没有初始化、推理、反向或更新，journal计数仍null。
[本次失败证据](evidence/onboard-navigation-streaming-load-only-20261007-r1.json)已独立封存。
正在新增仅子进程关闭可选整块缓存预热的薄入口，保持原模型／精度／数据／86步预算；
[源码依据与限制](research/onboard-navigation-allocator-warmup-20261007.md)。旧入口和失败不改。

现有Qwen导航LoRA入口完成50项CPU检查；固定86步／一轮、底座与数据来源、像素及显存
预算。实际首轮session20379退出1，最后日志在模型加载；相关Windows事件记录
torch_cpu.dll／0xc0000005。没有原训练report／events／adapter，更新计数未知，不填零，
不声称训练完成或OOM已证实。原失败保留，没有修改封存源码、底座或预算。

新增进程外监督与load-only入口，先保存完整原生日志和PID、退出码，再分段定位加载；
40项CPU检查与独立复核通过。单独加载原生退出 `0xc0000005`；完整堆栈定位到
`torch/storage.py` 的权重切片，底座加载尚未返回，未进入LoRA附加／optimizer。
真实worker PID与Windows事件匹配，26文件前后不变；根因和OOM仍未证明。
[独立加载失败证据](evidence/onboard-navigation-load-only-failure-20261007-r1.json)已封存。
新0.8.0／显式pread与旧0.4.5真实CPU对照已退出0、gold一致；50项probe与44项wrapper
软件测试通过，独立审核通过。完整pread加载仍native1／Windows1455页面文件错误，
首次safe_open未返回；53文件不变，未推理／反向／optimizer。事后C盘约200MB可用。
[本次兼容实验证据](evidence/onboard-navigation-pread-load-only-20261007-r1.json)独立封存。
仅头部与逐张量读取入口已完成，避开checkpoint整文件写时复制映射；完整加载结果见上方。
保持模型、精度、数据和预算，未清文件、关闭其它应用或修改页面文件。三名子智能体并行复核。
已写入Qwen／OpenFly／Cosmos官方选型；主线仍沿现有Qwen新增导航LoRA，不即装即飞。
[训练状态](onboard-openfly-navigation-pilot.md)、[模型选型](onboard-pretrained-model-options-20261007.md)。
[首轮失败证据](evidence/onboard-navigation-native-load-failure-20261007-r1.json)单独封存，后续加载诊断不改写它。

## 2026-10-07：首批真实 3D 导航动作数据已审核

已下载固定OpenFly原train，完整解析100226条记录；原索引0／7／8三条完整AirSim路线，
19／30／37步共86张原图，图片与pose约52.5MiB。缺图路线排除并保留失败r1，
没有假图、未来关键帧、原seen/unseen或新7B权重。原Prompt保持；83次转移按raw动作
核对FRD距离／yaw，14处聚合annotation幅度错位保留诊断、不用作gold。

native下载／构建／独立来源审核退出0，142项定向CPU检查通过；独立重开110原始
来源与7实现快照，实际169个actor图像输入对原PNG像素exact，未发现答案泄漏。
类别前进70／左6／右7／stop3，没有升降；同一scene只能1组／alltrain86，无独立
holdout。review只批准一次封存预算的train-only导航试训，loader training_ready仍false。

后续训练已在上方阶段启动，但尚未完成更新／保存／精确重载；训练内probe不当泛化。
默认r33、COCO、parity和版本不改；无新Gazebo架次、模型晋升、Jetson／HIL／REAL、
室外或最终发布。旧阶段七份文档已精确快照。
[面向用户的动作训练说明](onboard-openfly-navigation-pilot.md)、
[来源证据](evidence/onboard-openfly-navigation-dataset-20261007-r2.json)。

## 2026-10-07：真实 Gazebo 两帧新接口 GPU 回放与独立 CPU 审计完成

三名子智能体并行完成原生回放入口、独立 tensor／token 审计和公开导航模型研究；
主线程执行 RTX 4060 单次 Qwen3-VL-2B 基座推理，不加载失败 LoRA。输入原 Prompt、
原两帧 1280×960 RGB 和同源 GPS／odom，保留旧 host／sensor 时间；HOLD、空计划，
未编译 Mission 或创建飞控通信。等待9.015秒，实际处理180224像素／预算200704，
input2646／output156，峰值CUDA分配5.37GiB；单例不是吞吐／安全质量结论。

原生退出0；CPU独立复算消息、四tensor shape/dtype/SHA、grid和token解码完全一致，
31个绑定文件前后不变、退出0。新入口／审计51项软件测试、封存后54项证据／进度／
parity检查分开通过，不重复相加为任务正确率。kwargs原地消费导致
原生只记录调用后{}，CPU记录重建前与实际调用后，不篡改旧数据。原采集整架次
status=failed保持，回放不是新Gazebo运行、Prompt完成或动作训练。

结构合格≠语义合格：模型声称“系统安全／不存在目标”，但安全状态未知、有限画面
不能证明目标不存在；confidence0.95未经标定。9秒级也非高速避障控制。冻结worker
失败metrics与CLI枚举前删除加固缺口保留；本次四预期产物独立重开无缺失。

公开OpenFly／AeroVLA／TravelUAV权重确有实物，已核对来源／许可／动作域；下一步
优先导入train-only原动作轨迹、审因果历史及单位，在当前Qwen新增动作adapter，
不把外部planned动作当本域executed。真实HOME／净空／行动反馈和新Gazebo采集未通过。
原六份阶段文档已精确快照，旧receipt／默认r33／COCO／parity／版本不改；Jetson、
HIL／REAL、室外与最终发布未通过。
[实际结果与训练方向](onboard-live-vlm-retained-replay.md)、
[源绑定回执](evidence/onboard-live-vlm-retained-replay-20261007-r1.json)。

## 2026-10-07：连续 RGB／部分状态入口与长 episode 记录完成软件验证

三名子智能体并行完成来源绑定输入、长记录器和独立审查，主线程联调实际 callback
与 CLI。新画面在旧检测过滤前入独立推理分支；原文 Prompt、有限连续 RGB 和 wire
覆盖的 GPS／odom 入模型，缺 HOME／电量／净空等保持未知。逐请求保存 raw／metrics，
拒绝／部分失败不升级执行，普通新帧不取消运行中请求。

联合444项：443passed／1 Windows symlink权限skip／native0；官方 pinned processor
两帧／四帧 CPU 验证通过，修复真实 grid 超预算。十文件 Ruff 与实际无进程预检0，
没有加载权重或启动 Gazebo。上轮失败 LoRA 不加载，默认r33／COCO／parity与版本不变。
07:01:57 UTC 新诊断 worker offline／no_eligible_node，未提交采集。新 VLM 未驱动飞行，
完整行动回流、动作训练、Jetson／HIL／REAL／室外与最终发布仍未通过。
[说明](onboard-gazebo-live-shadow.md)与[源绑定证据](evidence/onboard-gazebo-live-shadow-development-20261007-r1.json)。

## 2026-10-07：已有 VLM 增量训练与独立审计完成，质量回归；Gazebo 新采集仍失败

独立审核新增 128 个实体三维 RGB 样本后，RTX 4060 实际完成 154 次单轮更新：
继承 r1 adapter，154 train／41 val／43 test；原 84 个留出输入未改动。原生训练与独立
raw／日志／参数审计均退出 0，保存参数与固定验证回答精确重载；质量回归、不晋升：
strict73→70、exact61→53，可见目标命中47→36，8 项质量门有 5 项失败。
[终态源绑定证据](evidence/onboard-vlm-grounding-increment-20261007-r2.json)。
源码／parent111项、审计器82项、终态证据／进度／parity57项均退出0，合计250项，
不把重复子集相加；这些工程检查通过不代表模型质量通过。
[面向用户的说明](onboard-vlm-grounding-increment.md)。

Controller 实际诊断发现可工作的远程 Docker daemon，固定摘要的 PX4/Gazebo 镜像
pull／inspect 均退出 0。随后的被动八帧采集退出 1，失败任务未返回内层报告；最新
诊断派发因 worker offline 拒绝，没有再次执行。下载日志的 bytes／SHA 已核验，镜像
准备不冒充渲染或飞行通过。[完整事实](evidence/gazebo-runtime-preparation-20261007-r1.json)。

默认 r33、COCO、生产 parity 与 `0.3.0-pre.8` 不变。下一轮重点是实际观察／状态／行动／
执行反馈的因果轨迹；新 grounding 例子不是自主航线训练，也不是 Jetson/HIL/REAL 证据。

## 历史阶段（2026-10-07）：主机 HOLD／子航段／恢复开发接入与内核 UDP 联调

新增原任务编写记录和封存、单次暂停 token、实际 HOLD 接收准入，以及主机调用的
暂停→新位置编译／净空→独立授权→上传／读回→cursor1→恢复路径。软件 peer 通过真实
UDP/MAVLink socket 返回原报文，但没有飞机动力学；原回答／原事务／净空明确为测试夹具。
取消、所有写入前人工模式检查、最新授权期限和缓存后原事实年龄均有拒绝路径。
旧排队事实不冒充本次指令确认，主机 commit 不冒充 PX4 的第二次 commit。

最新本地只读探测：Docker client 可用，daemon 查询超时、状态未知；当时 Desktop/backend
进程存在。未重启、搬数据、检查镜像或启动 Gazebo。默认 r33、失败 LoRA 和产品版本不变，
没有本轮模型训练／生成、Gazebo 飞行或硬件证据。重复父子链、持久消费、空中 watchdog、
真实净空／来源注册和原 whole-sortie dispatcher 仍未接通。

[接入能力与完整边界](onboard-continuation-host.md)。精确回执单独新增，不覆盖上一阶段。

最终源码定向 **295 项软件测试 + 14 项真实内核 UDP 测试通过，两个原生退出码均为 0**；
13 个 Python 文件 Ruff check/format 通过。UDP JUnit 保存 14 份 session、326 个实际原接收
报文和 2 次对端模式确认，不把这些数字记成飞行架次或模型正确率。
重复 journal 深解析热点已拆分为每阶段完整验证、逐写准确字节/hash与时间检查；
旧 profiling 的 native1 过期失败保持原文件，不拿最终成功覆盖它。
单独 15 秒只读 daemon 查询仍超时，Docker 自身没有退出码，shell invocation 为 1；
原短 probe 的 native2 与超参数拒绝的 native1 分开记录。
[封存回执](evidence/onboard-continuation-host-development-20261007-r1.json)绑定全部新源码、
原始 JUnit、旧基线与诊断，production parity 和默认模型引用字节未变。
封存后新旧证据、进度与 production source parity 的 **107 项定向复核通过，native exit 0**。

## 历史阶段（2026-10-07）：AI 空中连续航段的软件编译与接收核验

新的开发路径让 AI 原先选出的目标保持不动，以无人机较新的实测位置计算下一航段。
航段只有“当前位置 → 目标 → 有限驻留”，不会夹带一次完整起飞、回 HOME 和降落。
预算仍留出返航、垂直运动、降落、电量余量与下一次推理等待；保守假设只能收紧，
尚不是机体或电池实测标定。

23 文件联合软件回归 **911 passed，native exit 0**；独立意图／原编写声明 54 项与
接收协议 146 项均通过，9 份新 Python 源码／测试 Ruff check 和 format check 通过。
这些数字不当作飞行架次、训练样本数量或模型正确率。
封存后，回执／原始诊断／旧基线／进度／parity 的 110 项定向复核通过，原生退出 0；
production source parity 重算仍匹配，旧观察条目不覆盖或改写。

模型回答与当前飞控事实分别保留。原任务全文、完整编译／安全配置、原位置快照、
父 Mission 与原事务声明逐项绑定，避免同名任务或相同航点掩盖条件改变。默认没有
受信主机声明来源。空中状态须由原始 armed／in-air／AUTO.MISSION／HOME／位置／
参数与序号证据验证；高度大于零、模型 REPORT、上传成功都不能代替它。

PX4 读回以实际地面站地址和当前 cursor 核验，原接收报文保留；只在审查过的内容
比较中处理收发方向与 current 位，上传 wire 契约不变。读回相符仍不等于已经提交、
恢复飞行或完成任务。软件结果及精确源码／旧基线身份见
[本阶段回执](evidence/onboard-continuation-development-20261007-r1.json)；
[面向用户的说明](onboard-continuation.md)。

本轮 Controller 调度 Linux 只读诊断，原始产物下载后与 Controller 的 bytes／SHA
精确核验。远端 `/snap/bin/docker` 在 `--version` 阶段就报 launcher profile 权限
错误，还没证明 daemon 可查询；本地客户端可用但 daemon 查询超时。collector exit0
表示诊断收集成功，两个 probe exit2 都不表示 Gazebo 可用。未启动／重启服务、移动
Docker 数据或检查镜像。

尚未接通真实 Mission 替换 transport、连续父子事务链、用户取消后的恢复阻断、
空中等待返航监督器、真实净空 producer 和在线接收回流；没有新增 Gazebo 飞行、
动作训练或模型生成。原默认模型、旧失败 LoRA、`0.3.0-pre.8` 保持不变，
Jetson／HIL／REAL／室外 GNSS／最终发布门仍未通过。

## 2026-10-07：旧画面意图与较新执行状态独立准入、执行记忆采集器

新增开发链保留原 request／RGB／raw 回答和源时间，先把源 N/E 固定成 WGS-84 目标，
再从较新真实 context 重算位移。执行 plan 绑定当前 context；没有假装模型读过新状态。
提交前核对最新 Mission／任务回执／scope／HOME／epoch 与注册 task/runtime；pending
不进入 adapter，普通新图像不取消推理，迟到／重复结果不复活。仍复用原 planner/compiler。

最后消费时事实过期或普通新观察解除 arm 属于暂不可用；host-only 取消协议在旧执行器
提交前退出，scheduler 返回 None，保留当前 Mission，不误转模型故障 HOLD。真正模型／
原始绑定异常仍拒绝或走旧安全处理。源过期退休旧结果时，仍保留合法的 fresh latest 队列。

独立审查发现并修复未知高度、只验水平段和漏覆盖起飞／loiter／降落的缺口；现在要求
已知 relative-home 高度与完整 wire Mission 的 host-reviewed 三维覆盖证据。默认来源
白名单为空、没有真实覆盖 producer，因此真实跨源移动仍 fail-closed。当前全生命周期
compiler 每次包含 HOME 起飞与降落，不适合直接作为连续在途动作；续航段是下一架构门。

新增 receive-only collector 重解析原回答、核验实际 adapter revision 与原始字节，保存
proposed→accepted→executed→completed 因果链。执行／完成由已提交读回 journal、同来源
新鲜 AUTO.MISSION／armed／in-air／Mission feedback、terminal 与后续 landed/disarmed
推导；Permit、ACK、模型 REPORT 或进度 1.0 不能自行升级。较新状态下的接受保留原意图
及 host 重投影计划；下一次 VLM serializer 可读取这些原始事实。尚未自动接 live receiver。
receive 与 processing 时钟独立检查，原 source 时间与实际排队处理时间均留存；处理
延迟不刷新旧事实，未知 clock 映射仍不擅自换算。

远端旧探测产物已经正式 CLI 下载并精确核验：collector exit0 只代表收集成功，实际
probe exit2、Docker info exit1（权限错误），原生 gz 不存在，镜像未检查；本地 Docker
仍 stopped。没有本轮模型训练、生成、Gazebo 飞行、硬件验证、晋升或发布。
版本仍 `0.3.0-pre.8`。[运行链说明](onboard-vlm-runtime.md)、
[远端原始诊断核验](evidence/cluster-gazebo-preflight-recovered-20261007-r1.json)。

最终 16 文件联合软件回归 **617 passed，native exit 0**；独立审查三模块 **195 passed**，
7 份新 Python 文件 Ruff check/format 通过。原 native proof、模型与 production parity
字节未变；新增源码／测试与旧基线精确 SHA 封存在
[本轮软件证据](evidence/onboard-vlm-execution-admission-20261007-r1.json)，进度索引新增
独立条目，旧观察不改写。无 commit／push；这不是新增 Gazebo 飞行或动作训练验收。
封存后，证据／进度索引／parity／旧运行证明的 103 项定向复核通过，source 重算通过；
未用这些重复覆盖的测试数量冒充更多独立任务或飞行 episode。

## 2026-10-07：图文推理进程与 ready-only 交接已实现，尚未在线自主飞行

新增 v2 合同把实际 RGB、原始 Prompt、逐帧 context、事件及 host 审核的回执送入模型；
缺失信息保持未知。模型不复制长 SHA，父进程封存输入身份，并严格检查原始回答、
引用、时间与兼容性。当前 compiler 不消费的 speed／duration 和未校准 target reference
一律不准入，旧障碍 5000 m 占位值不当作已测净空。

模型在持久独立进程运行，加载与请求分别有硬截止时间；host watchdog 真正取消并回收
owned process。等待不进入 adapter，不制造 HOLD，不清空 Mission；普通新 RGB 仅排
最新窗口，任务／HOME／epoch／回执变化隔离旧结果。ready-only provider 已接现有共享
executor／planner／Mission compiler 的软件夹具，验证一次交接、单次消费和 pending 零副作用。

RTX 4060 原生生成两条 v2 回答，退出 0，严格合格 2/2，耗时 7.06／6.05 秒，峰值
allocated 4,700,770,304 bytes。CPU 重开原始图像后，processor tensor hash、token 解码、
request/hash 和 raw parser 均精确复现。因真实飞机状态缺失，两次均 HOLD、空 flight_plan
和 task_action；这只证明新运行链路，不是动作质量、实时达标或自主飞行验收。

本轮 Docker Desktop 原生状态为 stopped，C 盘约 0.40 GiB，数据 VHDX 约 130.44 GB。
没有重启／移动 Docker 或用户数据。另按调度规则派发 Linux 只读预检，初次原生退出 2；
诊断采集 wrapper 退出 0 不等于 Gazebo 前置条件通过，远端 artifact 仅有返回 ID，未取得
内容/hash。300 帧来源合格 RGB+Depth 仍全为 train，缺审核动作、真实 HOME 和时钟绑定。

未完成：源观测对较新执行状态的独立准入、任意 Prompt 任务解析、真实 receipt collector、
动作 episode 与动作微调、外部 Gazebo 飞行及 Jetson/HIL/REAL/室外门。
原 v1/probe、LoRA native1 与独立重载 native0、r33／COCO／parity 保留不变；版本仍
`0.3.0-pre.8`，未晋升、未发布。[运行时说明](onboard-vlm-runtime.md)、
[持久证据](evidence/onboard-vlm-runtime-20261007-r1.json)。

最终一组 334 项接口／worker／provider／原 executor／parity／文档定向回归通过；
另补当前进度绑定回执检查。10 份新 Python 文件 Ruff check/format 通过，
编辑文件差异检查及 current-progress source 重算通过，无 commit／push。

## 2026-10-07：VLM 真正微调并完成配对评估，候选仍不合格

实体 SDF 的 Blender RGB 经过独立全量审核，保留训练 26／验证 41／测试 43 个图文样本；
实际参考裁剪与当前图像进入同一固定 Qwen3-VL-2B，10 个模糊碎片排除，整段分区不移动。
RTX 4060 执行一轮 26 次 CUDA LoRA 更新，只训练 802,816 个语言注意力参数，基座及
视觉主体字节保持不变。训练不是固定车辆剧情，也没有借用场景真值作为模型输入。

84 父／84 子相同留出输入，原始 token 与回答均保存并独立复算。完整接口合格 1→73，
全字段正确 1→61；主要收益包含补齐原模型遗漏的三个未知字段。另行诊断可见性字段
平衡准确率 72.48%→79.05%，但遮挡正确 22/26→16/26、正确目标参考 ID 84/84→73/84。
有实际视觉字段变化，也有明显回归，不能按严格接口分数增长夸大视觉进步。

原生训练／配对生成完成后，任务在保存 adapter 的重新加载字节检查失败，退出 1。
原失败报告保持不变；独立排查已确认保存的 112 个 FP32 张量精确等于训练后参数哈希，
新的 fresh-base／FP32-default 加载路径实际退出 0，112 个参数字节、固定 val 首例输入
及生成 token／raw 全部一致，基座哈希不变、没有 optimizer 更新。恢复检查与原训练分开登记，
不把原 native1 改为 native0。质量不通过，未晋升、不编译 Mission、
不创建 transport、没有动作训练。世界模型未运行；Jetson、HIL/REAL、室外 GNSS 与发布门不变。

[训练说明](onboard-vlm-grounding-training.md)、
[证据](evidence/onboard-vlm-grounding-training-20261007-r1.json)。版本仍是 `0.3.0-pre.8`。

## 历史基线：2026-10-07 Qwen3-VL 看图推理，视觉时序判断仍不可靠

三名子智能体分别完成共享图文／证据接口、实体三维RGB用例和本机VLM入口；主代理完成
集成与持久记录。公开Qwen3-VL-2B固定revision已在独立环境使用RTX 4060生成8个回答，
原生退出0。7个回答结构合法，1个误用response被原样拒绝；7个合格回答均为HOLD，全部
flight_plan/task_action为空，没有生成或执行自主飞行计划。

单帧8.43–13.69秒、4帧14.72秒、6帧19.82秒；峰值allocated5.403GiB，reserved5.832GiB。
无token或历史截断。来源是原train的SDF Blender实际RGB，非本轮Gazebo采集或盲测。
GPS/HOME/姿态/电量等保持未知，执行ledger为空；离线30秒预算不代表生产实时门。

独立审查发现并修正源录制身份漏绑定和语义用例ID泄漏两处输入问题，改写评估标签不改变
模型输入。运行后重算179条绑定、148个唯一文件并从raw重跑parser。合同计数可信，但
视觉语义失败：完全遮挡／重现的目标被说成全程可见，没有遥测仍声称飞机没动；自报
confidence不能替代事实校验。此前仅研究阶段的回执保留为历史，不改写其当时状态。

[持久回执](evidence/onboard-vlm-native-shadow-20261007-r1.json)、
[接入与训练状态](onboard-vlm-shadow-status.md)、
[完整原生报告](../artifacts/evaluation/onboard-vlm-qwen3vl-native-probe-20261007-r1/report.json)。
下一步补明确目标指代、可见性变化和未知状态的审核图文窗口，按clip／场景隔离，再做
小adapter/LoRA的backward显存预检。动作训练仍缺同步执行轨迹和审核，不造动作gold。
本轮未VLM微调、未运行世界模型、未创建transport，硬取消worker未实现。r33／COCO／
parity不变，未晋升、未获新Gazebo飞行／Jetson／HIL/REAL／室外GNSS或最终发布通过。
447项共享接口、VLM入口、用例、原模型、文档与parity定向回归通过；Ruff、编辑文件差异
检查及进度索引／显式源码parity重算通过。保留的旧外部证据没有被当作本轮重跑。

## 2026-10-07：第四轮实体三维视觉微调完成，候选未晋升

RTX 4060沿r3视觉权重实际微调12轮，继承708/708参数项，训练及传统raw ONNX导出退出0。
1344张实体SDF的Blender图像按832/256/256划分；仅加入原train的64张连续帧，原val的
64张连续帧用于checkpoint选择。测试是已做过诊断的开发留出，不冒称全新盲测。

| 同像素、同阈值、原生ONNX | r3 TP / FP / FN | r4 TP / FP / FN |
| --- | ---: | ---: |
| 远距离静态 | 33 / 5 / 16 | 24 / 2 / 25 |
| 旧静态 | 27 / 8 / 24 | 28 / 5 / 23 |
| 旧静态部分遮挡（子集） | 14 / 6 / 13 | 15 / 3 / 12 |
| 远距离连续片段 | 11 / 18 / 24 | 10 / 6 / 25 |
| 旧连续片段 | 6 / 2 / 6 | 2 / 0 / 10 |

四组对照共6个原生进程均退出0，confidence=0.25、匹配IoU=0.5不变。误报减少但远距离与
旧连续召回退步，同ID恢复仍0。五项预注册质量规则仅旧静态及部分遮挡子集通过，不晋升、
不覆盖COCO或r33。ID切换下降可能只是检测更稀疏。
[完整回执](evidence/onboard-visual-replay-increment-20261007-r4.json)。

连续观测审计区分403张可读payload与303张完整身份合格帧，4个recording仍全在train。
urban r1 staging seed42017与raw71041不符，正确退出2；另有default/proxy telemetry、时钟
域和实际HOME_POSITION缺口，没有启动next-latent或动作优化。合并器已修复pending manifest
封存缺口并独立验证；首份修复前产物保留、不用于训练。209项数据／训练／回放定向测试通过。

下一阶段并行核验预训练VLM与世界模型，读取实际RGB、Prompt和任务记忆，再输出动态飞行
意图；保留执行／安全路径，不把40维表示、模板和阶段表当硬约束。本轮只训练视觉检测，
未训练Prompt理解、时序任务策略或飞行动作。版本仍0.3.0-pre.8，没有新Gazebo/PX4闭环、
Jetson、HIL/REAL、室外GNSS或最终发布验收。

三代理调查已完成：5个公开权重VLM、NVIDIA/Meta/DIAMOND三个世界模型家族及仓库接入
seam。建议Qwen3.5-2B／Qwen3-VL-2B／SmolVLM三方实际对照，V-JEPA 2.1小视频编码器作
离线teacher候选；Cosmos的型号、动作域及显存要求必须逐项区分。先实现versioned shadow
输入、任务记忆、严格parser和deadline，不给模型transport权限。没有现成可确认的通用
PX4/UAV飞行动态权重，也没有本轮VLM推理或微调。
[调查回执](evidence/onboard-pretrained-multimodal-research-20261007.json)绑定三份研究文件。
收尾115项证据／研究文档／进度索引／parity测试通过，与前述209项为不同测试文件；
Ruff、源重算及git diff --check通过。独立复核34个文件引用与32次clip源码绑定，并从
保存预测重算身份／IoU，确认质量门失败。没有commit或push，当前goal仍active。

## 2026-10-07：r33 历史策略扩展已导出并运行，正式时序微调仍缺合格轨迹

本轮把上一阶段的序列输入接入实际模型，而不是继续只堆单帧检测分数。保留 r33 主体和
任务／飞行动作输出，新增 8 次观察的有序历史分支；真实 dt、起点重置、填充隔离和会话
身份统一用于训练与推理。搜索等待期间会收集观察，不重复提交 Mission，也不读取场景 truth。
这是短期历史计算基础，不是长期多目标记忆或任意 Prompt 理解完成。

实际父权重迁移、四输入 ONNX 导出退出 0，候选 296318 字节。真实 CPU ORT 的 128 组
原始输出对照和 12 次带会话连续推理通过；当前阶段只验证迁移与接口，未执行时序优化。
正式入口实际尝试当前三维 trajectory staging，因为缺少动作审核回执退出 1，输出目录未创建。
当前暂存只有1个episode／100个观察，全部在train；独立验证／测试、完整来源与动作监督也未补齐。

319项模型、历史边界、训练、旧执行流程及证据工具回归通过；独立复核的86项历史／生命周期
回归也通过。已修复同帧GPS记录／返航报告、成功任务切换重置以及episode内home变化问题。
这些fixture和CPU推理验证不代表已训练、已飞行或已通过硬件门。
收尾116项证据／文档／索引／parity回归通过，Ruff及git diff --check通过。当前索引按源码
重新核验；26个保留证据审计副本使用短路径，原始release report／mode probe不重写，
`current_release_parity_bound=false`如实保留，绝不把旧运行升级为新源码已通过。

下一步补有完整感知、真实时间与审核动作的独立三维连续任务，再微调并做无历史／打乱
历史对照。当前 r33、COCO 和产品版本不变，候选不晋升。没有新 Gazebo/PX4 飞行、Jetson、
HIL/REAL、室外 GNSS 或最终发布通过；总目标继续 active。
[完整本轮回执](evidence/onboard-history-policy-development-20261007.json)。

## 2026-10-07：第三轮远距离视觉微调完成，误报与身份恢复仍不合格

新576张实体3-D RGB/visible/amodal图已封存并全量审计，384/96/96分区、世界与测试几何隔离。
训练116张实际可见图中，低视角≤10°且框高≤55px样本73张，上一轮为10/135；不是计划标签计数。
从r2继承708/708参数，在RTX 4060/CUDA完成30轮，训练、导出、四组评估均正常退出0。

同一raw ONNX接口、confidence=0.25、IoU=0.5，命中／误报／漏检：

| 数据 | r2 | r3 |
| --- | ---: | ---: |
| 新远距离静态96图／49可见 | 12 / 3 / 37 | 33 / 5 / 16 |
| 新连续test48帧／35可见 | 1 / 0 / 34 | 11 / 18 / 24 |
| 旧静态96图／51可见 | 27 / 5 / 24 | 27 / 8 / 24 |
| 旧连续16帧／12可见 | 0 / 0 / 12 | 6 / 2 / 6 |

远距离静态召回24.49%→67.35%，新连续召回2.86%→31.43%，但误报明显增加。旧静态
partial子集TP16→14、FP2→6、FN11→13；clear的改善抵消了这项退化，总体命中持平不能算全部保真。新旧连续
测试均有2次ID切换，同ID恢复均0；重现后检出不能当作稳定重捕。因此r3保持隔离，
不覆盖COCO或r33，不晋升，不产生飞行动作标签。新开发留出已用于诊断，不宣称盲测基准。

该轮已另完成episode历史窗口输入基础，当时尚未接模型或训练时序策略。下一阶段必须
补误报／身份稳定性，以及模型可读的感知历史、飞机状态、任务记忆和经审核动作监督。
Blender开发证据不是Gazebo传感器/PX4飞行、Jetson、HIL/REAL、室外GNSS或最终发布。
[完整训练与四组对照回执](evidence/onboard-far-visual-increment-20261007-r3.json)。总目标继续active。

收尾464项渲染／训练／真实跟踪／序列输入／证据／文档／索引／parity回归通过；两处测试
格式修正后的105项定向回归也通过。Ruff、进度索引源重算、git diff --check通过，README
保持50KB入口上限内。独立复核36个模型／源／报告绑定及64个保存预测帧，与实际mask重算一致。
原COCO、r33模型引用和r33 ONNX字节身份保持不变，没有commit/push或新发布。

## 2026-10-07 08:15 SGT：连续任务训练窗口基础完成，决策模型尚未获得时序记忆

新增`load_trajectory_episodes`与`collate_episode_windows`，直接复用现有trajectory parser和
`BootstrapSample`，不另造动作或轨迹schema。同一episode组成有界窗口，保留顺序、时间差、
任务／机型／录制身份和真实起点；尾部padding为None，缺动作标签仍拒绝。显式未审核opt-in
的可训练标志保持false。43项定向测试、原parser3项关键回归和独立审查通过。

这是时序输入基础，不是新训练模型：现有MLP和r33引用未改，尚未接入history encoder，
没有新材料化的审核飞行动作轨迹或Gazebo通过。详见[回执](evidence/onboard-sequence-input-development-20261007.json)。

## 2026-10-07 08:00 SGT：第三轮远距离视觉课程准备回执

已实现远距离／低视角三维课程、近中远多段连续画面和逐帧数据审计。新连续 suite 实际生成
144帧，test为48帧，其中35帧可见；父r2仅命中1帧、漏检34帧、误报0。唯一重现后检出是
首次建立track，不是旧ID恢复；中距离train片段16帧均全遮挡，只能算负片段。

36张新课程smoke已实际渲染并审计通过，实际相机距离覆盖至48.32m、光轴俯角3–12°。
旧576张课程的全量审计确认训练135张可见图中，低视角≤10°且框高≤55px只有10张。
整批新576张仍在生成，尚未封存manifest，观察时尚未启动第三轮训练。416项相关回归通过。
这是Blender实体离线开发，不是Gazebo采集或自主飞行；原COCO和r33引用、晋升及发布门不变。
完整来源、运行退出码及时间边界见[准备回执](evidence/onboard-far-visual-preparation-20261007.json)。

## 2026-10-07：r2 多几何视觉增量训练完成，静态留出改善、连续留出失败

已新增三种不同实体 SDF 几何，连同原型形成四种目标形状。实际渲染576张640×480 RGB，
train/val/test=384/96/96，分别使用不同world；训练／验证只用前三种，测试仅用未见utility van。
目标旁加入实体设施、五个非目标干扰物、变化光照及落地遮挡屏障。每张可见框来自实际mask；
SDF source、primitive geometry、所有像素及标签均已核验，封存时没有不一致mask。

从r1继承708/708参数项，在RTX 4060/CUDA完成30轮微调，训练和传统raw ONNX导出退出0。
同一现有推理接口、confidence=0.25、IoU=0.5、同96张测试图，对照如下：

| 指标 | r1 | r2 |
| --- | ---: | ---: |
| 正确检出 | 4 | 27 |
| 误报 | 25 | 5 |
| 漏检 | 47 | 24 |
| 召回率 | 7.84% | 52.94% |

部分遮挡的27个可见样本，命中0→16；21张完全遮挡图的误报1→0。改善只属于这套低多边形
离线留出集，仍有24次漏检，不代表真实环境、语义任务或飞行通过。PyTorch AP与上述固定
阈值的传统ONNX计数分开记录，不能互换。

另完成48帧／3段实际连续3-D画面：每段6帧clear、6帧partial、4帧full，并重新出现。
现有检测器／真实UnifiedTargetPool只接收RGB+声明的dt，全部预测后才读GT。连续test段中，
r1与r2都没有检出12个可见帧，未建立跟踪，也未重捕；零ID切换不能算跟踪成功。
因此r2仍隔离，不改原COCO或r33引用，不晋升，不生成飞行动作监督。

只读诊断发现连续画面距离34.07–35.38m，超过静态训练最大约29.77m；视角7.31–7.59°、
无遮挡框高度50–51px，并扫过画面边缘。输入／解码正常，所有帧最高置信度仍低于0.25；
静态与连续test使用相同van几何。训练中联合低视角／小目标样本仅10/135，支持分布缺口，
但光照和采样噪声等因素的贡献尚未隔离。下一轮补这些覆盖及多个独立连续clip，阈值不降。

新增训练前invocation binding、父权重实际类名核对、checkpoint-bound export receipt、
实际SDF几何重算、ONNX原始类／shape验证与amodal外像素拒绝；独立审查缺口已修复。
239项训练／渲染／真实跟踪回归通过；收尾合并证据、文档、进度索引和parity检查，共339项
回归通过，Ruff、进度索引源重算和git diff --check也通过。独立审查核对17个实际产物引用。
集群本轮Blender探针未找到满足能力要求的worker，
没有执行远端任务；本机完成上述训练。核心parity未改，Docker故障与恢复待答复仍单独保留。

模型、原始报告与字节身份见[本轮持久回执](evidence/onboard-sdf-blender-curriculum-training-20261007-r2.json)
和[当前训练状态](onboard-training-status.md)。这是Blender离线开发，不是新的Gazebo/PX4飞行、
Jetson、Pixhawk、HIL/REAL、室外GNSS或最终0.3.0通过；总目标继续active。

## 2026-10-07：实体三维像素训练完成，辅助视觉候选未晋升

本轮不再把红车／绿车例子或状态表格当作 Gazebo 验证。已用 Blender 4.5.3 导入现有 SDF
场景和多部件实体目标，实际渲染 120 张 640×480 RGB、可见像素 mask 和 YOLO 标签；
urban/train=72、river/val=24、coastal/test=24，包含 22 张真正无目标负样本。

沿用现有 YOLO26n 权重，在 RTX 4060/CUDA 上完成 20 轮视觉微调，导出传统 raw ONNX。
新模型是 `search_target` 辅助候选，不覆盖原有 COCO 检测器或 r33 策略。现有
`OnnxRawYoloDetector` 对 24 张留出图得到 TP=20、FP=0、FN=0，CPU 检测 p95=16.52 ms。
三套 world 共用一种目标几何且背景较简单；所有目标存在帧均可见，不能声称完整遮挡、
通用识别、Jetson 速度或自主搜索成功。相机采样使用离线已知目标位置，标注不进入运行时。

42 项专项测试、Ruff、独立训练审查和 120 帧 mask／标签核对通过；数据输入只来自验过 hash
的封闭副本。YOLO26 验证原地融合引起的导出故障已通过新加载同一 checkpoint 修复，没有
重训或修改原模型引用。详见[当前训练状态](onboard-training-status.md)和
[三维视觉训练持久回执](evidence/onboard-sdf-blender-visual-training-20261007.json)。
收尾 146 项训练／证据／进度／parity／契约回归全通过，进度索引按当前源重算通过。

本机 Docker backend 最新明确报告空间不足，集群探针实际执行后因 Docker profile 权限错误
退出；不是旧 source/cwd 问题。没有删除缓存、镜像或卷，也没有重启 Docker；恢复提案仍待
用户答复。[运行时阻塞持久回执](evidence/gazebo-3d-capture-runtime-blockers-20261007.json)
单独保存这两个问题。上述训练是离线 Blender 开发证据，不是 Gazebo／PX4、HIL／REAL、
室外 GNSS 或最终 0.3.0 通过。

## 2026-10-07：先训练现有 OnboardAI，r90 有收益但不晋升

本机 RTX 4060/CUDA 已从现有 r33 继承完成 r90 的60轮开发训练，105,200行结构化状态课程，
没有使用红车/绿车主线或重复9帧冒充真实三维监督。新增每轮训练分区shuffle和action-only父模型
蒸馏，导航诊断也改成与当前运行接口一致的完整目标偏移；40米短步只保留为显式参考。

同输入、同新种子的900行场景状态对照中，r90 task/flight=`99.78%/99.89%`，r33为
`83.44%/83.67%`；固定22个回归raw均`19/22`，guarded从`21/22`到`22/22`。
SEARCH方向p95从`117.04°`到`23.32°`，距离MAE从`189.20m`到`86.52m`；INSPECT方向p95
`27.87°`、距离MAE`60.43m`仍不合格。r90不替换r33，不声称视觉训练或自主飞行通过。
详细限制、结果和实际后续路线见[当前训练状态](onboard-training-status.md)。

另一个agent已修复三维传感器staging的来源问题：保留真实Gazebo时间、实际wrapper/resource
hash和观测CameraInfo，不生成伪生产manifest，也不把准备资产当运行时挂载证明。离线标注到
YOLO视觉训练格式的导出已接通：真实图像像素与可见实例框生成PNG、YOLO labels和独立录制
split；这条 Gazebo 出口当时仍是 staging，没有实际视觉训练数据导出或视觉微调。后续离线
Blender 开发训练见本页顶部，不能用它替 Gazebo 出口通过正式数据门。标注绑定本身不会训练YOLO，
也不会让40维策略头直接学看画面。

已实际重新整理原有20帧sensor stream到
[`materialized-r8-staging`](../artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1/materialized-r8-staging/staging-receipt.json)，
来源wrapper/resource/CameraInfo与Gazebo时间绑定通过，但capture认证与主线挂载证明仍未声明，
`training_ready=false`。这不是新Gazebo运行，也不是视觉训练集已经生成。

## 2026-10-07：训练入口收紧为实体 3-D 监督

当前优先级是训练现有 OnboardAI，而不是继续扩展红车/绿车示例。`multidetect_search_target_v1`
等中性实体 3-D 模型产生的 RGB、FLOAT32 Depth、CameraInfo、姿态和 PX4 telemetry，必须再与
capture-only 的 3-D bounding box / RGB_INT8 labels-map 通过 `sequence + frame_id + RGB payload
SHA-256` 精确绑定，才允许进入训练-ready 数据集。`scripts/build_onboard_rendered_3d_dataset.py`
提供 `--require-annotation-supervision`，预检和训练器也会拒绝没有逐帧标注监督的伪 training-ready
文件；标注 topic 始终是离线监督，不进入 OnboardAI 运行时输入。

本轮桥接测试已通过，验证了 v2 annotation staging → canonical v1 frame-set → rendered dataset
的严格路径，包括场景 manifest hash、labels-map payload hash/几何、3-D box、wrapper hash 和
frame identity。最新预检仍是 `blocked`：Docker daemon、宿主 Gazebo/PX4 CLI 不可用，现有 r3
数据没有 calibrated geometry 和 3-D annotation supervision，所以没有启动真实微调、没有替换
r33 reference，也没有新增 Jetson/Pixhawk/HIL/实飞声明。

## 2026-10-07：OnboardAI 3-D 训练管线再次 preflight

本轮没有伪造 Gazebo 运行结果。训练入口
`scripts/run_onboard_3d_training_pipeline.py` 已从当前 r33 reference、当前
`gazebo-3d-mainline-v1` manifest 和仓库已有 YOLO detector 边界重新执行，输出
[`onboard-3d-training-pipeline-20261007-r2.json`](../artifacts/evaluation/onboard-3d-training-pipeline-20261007-r2.json)。
它正确保持了 SIM/未来 Jetson 的同一父模型绑定，但因为 Docker server 探针不可达、宿主没有
`gz/gazebo` 与 PX4 CLI，而且现有 RGB/Depth 录制没有目标可见的检测/深度/标定几何行，结果仍是
`preflight_only`、`training_ready=false`、`candidate_promoted=false`。详细捕获命令和阻塞原因见
[`onboard-3d-training-preflight-20261007-r2.json`](evidence/onboard-3d-training-preflight-20261007-r2.json)。

本次还把 `multidetect.onboard-3d-target-visibility-gate-v1` 写进训练前置检查：每一帧必须同时
有 detector 命中、FLOAT32 深度测量和 calibrated camera/depth/pose NED 几何，且至少累积 100 行，
才算进入真实 3-D 训练候选；当前所有仓库数据均为 0 行。

这次 preflight 不是新的模型训练，也没有改变 r33 reference；恢复 Docker 后必须按报告生成的
multirotor capture command 执行带 Mission 的实体 3-D 采集，确认目标进入相机视场，再封存 RGB、
FLOAT32 Depth、CameraInfo、PX4 telemetry 和独立审阅标签，之后才允许从 r33 做真正像素级增量微调。

## 2026-10-07：OnboardAI 增量训练 smoke 与 3-D 标注契约收口

本轮先推进 OnboardAI，而不是继续用颜色示例验证 Gazebo。以当前 r33 ONNX 为父模型，在本机
RTX 4060/CUDA 上完成了一个不含 synthetic bootstrap 的开发候选：
[`onboard-policy-r89-3d-sensor-contract-smoke-20261007`](evidence/onboard-policy-r89-3d-sensor-contract-smoke-20261007.json)。
它只消费现有 9 行、尚未 review 的 3-D sensor-contract 数据并重复加权到 540 行，成功导出
40 输入/20 输出的 ONNX，模型 SHA-256 为
`d7afbef8ae4f822d1598cfe0b943106b4cd84dc1f56c50e00a41063fc534d80a`。这是训练/导出链路验证，
不是视觉监督训练完成、不是候选晋升，也没有替换当前 r33 reference。

3-D annotation 链路也已收口：r7 staging 保留 `gazebo_sensor` 原始时间和 RGB/Depth wire hash；
collector 现在必须接收同一 staging manifest 提供的 exact RGB hash，并必须把 RGB_INT8 labels-map
落盘。binder 会重新解析 labels-map 的 semantic byte 2、instance bytes 1/0、可见 bbox 和像素数，
再验证 annotation config、车辆/目标 wrapper hash 和 runtime boundary。新增的
`scripts/adapt_gazebo_annotation_staging_to_frame_set.py` 明确把已验证 v2 staging 转成训练器能读的
canonical v1 frame-set，但不会伪造 RGB production manifest。当前 annotation collector 仍为 0 bytes，
所以真实 annotation 训练继续 fail-closed。

最新训练 preflight 为 `artifacts/evaluation/onboard-3d-training-preflight-20261007-r1.json`，状态仍为
`blocked`：Docker server、宿主 Gazebo/PX4 CLI 都不可用，仓库现有数据也没有 training-ready 的
独立像素 holdout。r33 仍是 SIM 和未来 Jetson 的唯一当前 reference；r89 仅是隔离开发候选。

随后用同一 seed、同一三类实体 3-D sensor-contract holdout 做了 r33/r89 成对复核：r89 的任务/飞行
准确率为 `86.67% / 87.50%`，r33 为 `87.08% / 87.08%`；r89 虽然 range MAE 从约 `24.11 m`
降到 `13.45 m`，但通用压力集出现 `bearing-left` 失败，导航角度 p95 从约 `122.11°` 退化到
`171.88°`。因此 r89 被明确拒绝，当前 r33 不变。完整成对报告见
[`onboard-policy-r33-r89-paired-evaluation-20261007.json`](evidence/onboard-policy-r33-r89-paired-evaluation-20261007.json)。

另外把当前 urban 3-D RGB/Depth 录制的前三帧送入仓库已有 COCO ONNX detector。三帧均无 detector
hit、无有效目标深度几何，人工查看也确认目标尚未进入相机视场；这证明传感器 wire 可以读取，
但不具备视觉监督。结果见
[`onboard-rendered-3d-detector-smoke-20261007.json`](evidence/onboard-rendered-3d-detector-smoke-20261007.json)。
下一轮必须执行 capture-plan 航线进入实体目标可见区，再采集 capture-only 3-D labels-map，不能
用红车/绿车回归夹具补这个缺口。

## 2026-10-07：按 3-D 模型和 OnboardAI 训练主线继续推进

这里明确纠正验证方式：颜色只是用户举例，不能作为 Gazebo 主线的目标定义。主线使用仓库中
`multidetect_search_target_v1` 的实体 3-D 结构、三类 PX4 机型、RGB、FLOAT32 Depth、CameraInfo、
姿态和同帧 PX4 telemetry；运行策略不读取 `truth.json`、世界坐标或离线 annotation topic。

目前已有两类真实外部 Gazebo/PX4 传感器证据：

- `onboard-3d-capture-river-20261007-r4` 保存了 100 帧 RGB 和 100 帧 FLOAT32 Depth，传感器、
  深度标定、同帧姿态/遥测和容器清理门通过；VTOL Mission helper 在采集完成后退出非零，因此
  不能把它写成完整自主任务成功。
- `onboard-3d-flight-urban-20261007-r1` 保存了 100 帧 RGB、100 帧 Depth 和连续遥测，加载的仍是
  `onboard-policy-r33-balanced-bearing-3d-dev`。100 个 transition 中只有 1 个完整绑定到
  OnboardAI bridge，当前导出为 `1 bound / 99 pending`，所以还没有可训练的完整 sortie。

为真正开始像素级 OnboardAI 微调，已准备 capture-only 3-D 标注 wrapper：车辆增加 3-D bounding-box
和 instance-segmentation sensor，目标增加离线 label plugin。它只给离线监督使用，不进入实时策略，
也不会把 Gazebo 真值偷偷变成 Jetson 输入。随后已经启动过一次隔离的真实 Gazebo/PX4 annotation
world：实际 topic 输出过 `AnnotatedOriented3DBox_V`，可见 `label_id=42` 和 3-D center/size；同一
次运行还保存了 20 帧 RGB、FLOAT32 Depth、CameraInfo、NavSat、Odometry wire records。Docker
Desktop 随后发生 storage I/O failure，导致 annotation collector 没能把 topic rows 封存，因此这次
不能宣称 annotation frame set 已完成。

这 20 帧的原始 MDGZSIM3/MDGZDEP1 仍保留；早先的 `materialized-r4`/r5 派生生产清单已标记为
历史无效（训练器也通过
`configs/training/gazebo-capture-provenance-invalidations-v1.json` 拒绝它们），因为它们错误地把 Gazebo sensor time 写成 `host_monotonic`，并把 annotation wrapper
资产重新标成主线模型。当前只生成隔离的
`artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1/materialized-r7-staging/`：20 帧
RGB、Depth、wire hash、GPS/姿态和精确 `sensor_stamp_ns` 均保留，时钟明确为 `gazebo_sensor`；
它不是生产 manifest，不能被训练器直接读取。后续必须实际采集逐帧 annotation，以
`sensor_stamp_ns + RGB payload SHA-256` 精确绑定，并通过独立 review seal 后，才允许从同一个
r33 父模型增量微调。

现场观察与落盘证据分开记录在
[`gazebo-3d-annotation-topic-observation-20261007.json`](evidence/gazebo-3d-annotation-topic-observation-20261007.json)：
它确认 topic/message type/3-D geometry 已被看到，但明确记录 collector 输出为 0 bytes，不能当成
reviewed annotation supervision。

新的主机侧 binder `scripts/bind_gazebo_annotation_staging.py` 已就绪：它只接受精确
`sensor_stamp_ns + RGB payload SHA-256`，要求真实 RGB_INT8 labels-map 中的目标可见，并把 3-D
rendering-view center/size/orientation、可见 bbox 和 labels-map hash 一起封存为离线 staging。
它不会把最近时间、颜色示例或 Gazebo truth 转成训练行；输出仍保持 `training_ready=false`。

因此当前训练状态仍是 `training_ready=false`、`candidate_promoted=false`。先补齐实体 3-D 多场景
连续采集和审核，再训练；不再用红车/绿车或结构化答案代替视觉训练，也不宣称 Jetson、Pixhawk、
HIL/REAL、室外 GNSS 或最终 `0.3.0` 已通过。

## 2026-10-07：r6 真实 3-D 传感器录制已绑定到同一 OnboardAI 训练链

为避免把“有一组 RGB 文件”误写成“模型已经训练”，本轮把一次当前主线实体 3-D 采集完整绑定到
录制清单和轨迹暂存：`onboard-3d-capture-urban-20261007-r6` 保存了 100 帧 RGB、100 帧
FLOAT32 Depth 和 100 帧同帧遥测，recording hash 为
`27053380c58e9af01aa443f5b51df98bca24328677f943ce2043eebcbb51c51c`。外部 Gazebo/PX4 软件
传感器门通过，但这次是 sensor-only 采集，没有请求 Mission，所以不能写成完整自主飞行成功。

同一录制已生成：

- [`recording.manifest.json`](../artifacts/evaluation/onboard-3d-capture-urban-20261007-r6/recording-r2/recording.manifest.json)，
  100/100/100 帧绑定且 `truth_free=true`；
- [`rendered-detector.json`](../artifacts/evaluation/onboard-3d-capture-urban-20261007-r6/rendered-detector.json)，
  100 行，但 detector hit、有效深度测量、标定 NED 几何和目标匹配都是 `0`，事件全部是
  `target_lost`；
- [`trajectory-staging.json`](../artifacts/evaluation/onboard-3d-capture-urban-20261007-r6/trajectory-staging.json)，
  100 个 recording-bound transition，但每行仍是 `pending_independent_review`。

因此这批数据已经证明“真实 3-D 传感器 → recording → trajectory/rendered dataset → 同一 r33
训练入口”的数据管道能工作，但还没有达到像素级微调条件。把 r2、r6 和 river 录制合并后得到
[`onboard-3d-rendered-merged-20261007-r3.json`](../artifacts/evaluation/onboard-3d-rendered-merged-20261007-r3.json)，
共 300 行，仍 `training_ready=false`；最新 pipeline/preflight 也保持 `preflight_only`、
`train_requested=false`、`candidate_promoted=false`。当前 r33 ONNX（40 输入 / 20 输出）仍是
SIM 和未来 Jetson 共用的唯一父模型。

另外修正了库存审计的契约比较：采集运行时使用场景索引的 canonical hash，审计现在也使用同一
canonical hash，不再把当前主线录制误报为 stale。最新库存预检识别出 5 个 current-mainline
capture-eligible 录制，但仍要求人工审核的目标匹配、有效 3-D 几何、独立 holdout 和事件覆盖，
所以没有启动训练、没有替换模型，也没有增加 Jetson/Pixhawk/HIL/REAL/室外 GNSS 证据。

我随后确实调用了 `--train --device cuda` 的严格增量入口；它从 r33 启动，但以退出码 `1`
fail-closed。回执是
[`onboard-3d-training-pipeline-train-attempt-20261007-r6.json`](../artifacts/evaluation/onboard-3d-training-pipeline-train-attempt-20261007-r6.json)，
SHA-256=`861186537026316ba09d1bfce24d5c5f72c449eb79a96a262ce777919fb81a32`。失败原因不是
“没有调用训练”，而是训练资格不足：可用深度行和标定 NED 行均少于 100、没有第二独立场景和
独立测试 episode、没有 `target_moved` 或 GPS/电量/障碍/推理健康事件、轨迹尚未独立审核封存。
候选目录没有被接受，r33 父模型保持不变。

为了补齐下一轮真实监督，我还用同一个主线 urban world 生成了隔离的 capture-only 标注 world：
[`urban_inspection_multirotor_001-r2.sdf`](../artifacts/annotation-capture/urban_inspection_multirotor_001-r2.sdf)
和 [`binding-r2.json`](../artifacts/annotation-capture/binding-r2.json)。它只增加 3-D bounding-box
和 instance-segmentation 离线 topic，资源根包含 8 个 hash 绑定文件；当前状态仍是
`prepared_not_executed`，没有声称 topic 已运行，更没有把 annotation topic 接到 OnboardAI 输入。
下一步必须在这个 world 中实际采集并将每帧标注与 RGB/Depth payload hash 精确绑定，再经过独立
review seal，才有资格重新调用同一个 r33 增量训练入口。

另外新增了 [`export_onboard_sortie_transitions.py`](../scripts/export_onboard_sortie_transitions.py)：
它把外部 acceptance 的 OnboardAI bridge observation/revision 与 recording-bound trajectory
按 frame identity 精确连接，只接受 `mission_bound=true`、`planner_revalidated=true`、完整
decision hash 且 truth-free 的行；缺失 bridge 或任何歧义都保持 `pending`/直接拒绝，不生成
标签、不创建 reviewer receipt、不晋升模型。现有 r2 acceptance 没有 bridge，实际按设计
fail-closed；该导出器的 3 项回归测试通过。

## 2026-10-07：3-D 运行时预检没有冒充 Gazebo 成功

本机 Docker 客户端存在，但 `desktop-linux` 的 Linux daemon 管道不存在；只确认到一个
`sailor-ingest.sock` 重解析点，未删除、未轮换。随后通过 ClusterYourCodex 对在线 Linux 节点
提交了只读 3-D 运行时预检：调度曾选中节点，但第一次因 snapshot 尚未上传而在 source
preparation 阶段返回 `404`；上传后控制器保留旧活动 slot，重试没有可用节点。因此本轮没有
启动 Gazebo/PX4、没有采集 RGB/Depth/telemetry、没有训练或生成候选模型。证据见
[`cluster-3d-runtime-preflight-20261007.json`](evidence/cluster-3d-runtime-preflight-20261007.json)。
SHA-256=`6e413fe3d61d1bc070f3808f1c3b848f23596401fed644d2d718bfe0ece28493`。

## 2026-10-07：把 3-D 遥测故障接入同一套 OnboardAI 训练事件

本轮没有新增“红车/绿车”验证。`build_onboard_trajectory_staging.py` 现在从每个真实
Gazebo/PX4 frame-bound telemetry 直接生成现有 `OnboardRuntimeEvent`：GPS fix/水平精度异常
生成 `gps_degraded`，剩余电量低于 20% 生成 `battery_low`，明确的近距离障碍遥测生成
`obstacle_risk`；这些事件优先于目标检测，避免健康故障被一帧看似正常的 RGB 覆盖。渲染数据
和轨迹数据都记录 `multidetect.onboard-3d-event-coverage-v1` 计数，仍不读取 `truth.json`。

严格真实 3-D 训练门现在要求轨迹至少同时有 `target_moved`、`target_lost` 和一个 GPS/电量/
障碍/推理健康事件。这样 OnboardAI 才会看到目标移动、丢失和突发安全状态，而不是只有静态
目标分类；仍沿用 r33 父模型、40 维输入、20 维输出和同一 Jetson/PX4 适配器。当前历史数据
只有 `target_lost`、没有完整 3-D 连续采集，因此训练继续 fail-closed，`training_ready=false`，
没有候选晋级，也没有外部 Gazebo/Jetson/实飞通过声明。用历史输入实际运行严格入口得到
`trainer_exit_code=1`，没有创建候选目录；阻断回执为
[`onboard-3d-training-event-coverage-gate-20261007.json`](evidence/onboard-3d-training-event-coverage-gate-20261007.json)，
SHA-256=`87dfeb28609f24c4b52f96d4c92ab37624ab60cb85df89c5a6cf46bac51991a9`。

另外，严格训练门会拒绝带有 `red_car`、`red_green`、`color` 或 `colour` 标记的验证场景；这些
场景只保留给回归测试，不能把用户举例用的颜色当成 OnboardAI 的训练标签。

## 2026-10-07：3-D OnboardAI 采集矩阵按机型接入，固定翼保持被动采集边界

预检入口现在不再把 VTOL/固定翼简单归为“没有 runner”。三类仓库自有实体 3-D 场景继续
共用当前 r33 OnboardAI reference、同一 RGB/Depth/telemetry 数据契约和同一训练门：多旋翼
生成完整自主任务采集命令，VTOL 复用现有 `run_vtol_mission_sensor_capture.py` 完整任务飞行
采集 RGB、FLOAT32 Depth 和同容器遥测，固定翼生成 `run_simulator_docker_acceptance.py`
的真实 RGB-D/遥测采集命令，但明确标为 `ready_to_execute_sensor_only`，不会把被动采集写成
固定翼自主飞行。三条命令都只在显式 `--execute` 且运行时可用时执行，预检本身不启动任何
Gazebo/PX4/Docker。

本轮生成的矩阵证据为
[`onboard-3d-training-preflight-20261007-r1.json`](evidence/onboard-3d-training-preflight-20261007-r1.json)，
SHA-256=`a5e86840d5d7f81ec24f59353912b5e36a0e360d6f1a26b11bd371435ff22deb`。当前本机 Docker
Linux daemon 仍不可达，历史 3-D 数据仍是 `training_ready=false`，所以没有声称外部 Gazebo、
Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 通过；下一步仍是恢复运行时并采集足够的
多场景连续 RGB+Depth+telemetry，再进入严格真实数据独占训练。

## 2026-10-07：严格 3-D 微调改为真实数据独占，继续使用 r33 父模型

训练入口发现并修复了一个会稀释真实数据的窗口：`train_onboard_model.py` 默认会生成结构化
teacher 行，严格 pipeline 如果只绑定 RGB/Depth/trajectory，真实传感器行可能被合成行淹没。现在
`run_onboard_3d_training_pipeline.py --train` 显式传递 `--no-synthetic-bootstrap`，并禁止
neutral/phase/bearing/SDF/legacy 等合成增强；训练只消费通过真实 3-D gate 的 rendered rows 和
recording-bound reviewed trajectory rows，仍从 hash 校验过的 r33 ONNX 权重初始化。配置会记录
`synthetic_rows_included=false` 和 `synthetic_structured_scene_rows_present=false`，候选回执也会
再次校验这两个字段。

使用当前不足的历史 3 帧输入做回归，入口仍以退出码 `1` 阻断，没有生成候选模型或修改 r33；本轮
训练器/pipeline 定向测试和 Ruff 均通过。这个改动只收紧训练数据边界，不代表真实 Gazebo、Jetson、
Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 已通过。

## 2026-10-07：Docker 采集计划保留资产绑定，采集回归恢复通过

真实传感器 frame-set 生成器原来依赖 `DockerSimulatorPlan.assets`，而计划对象只保留了
分散路径，导致真实 RGB/Depth 采集在写 manifest 前崩溃。现在计划对象保留同一份已解析的
资产 bundle，采集 acceptance 继续使用 world/model/config/stack/model-root 的原始 hash；
`tests/test_simulator_docker_acceptance.py` 全部通过，未新增另一套资产解析逻辑。

当前工作区仍存在一个更早的治理差异：未提交的 acceptance 主线资产绑定改动与 checked-in
`configs/release-parity.json` 的旧 PX4 hash 不一致，因此 release parity canonical 单测仍失败。
本轮没有擅自重写 parity、没有改变 release label，也没有把该状态当作 release 通过；需要在
单独的 parity 审计/批准动作中处理。

## 2026-10-07：真实 3-D 采集输入链跑通，未复核地面样本被训练门拒绝

训练优先工作修复了三个真实数据阻塞：MDGZSIM3 零基帧序号、MDGZDEP1 schema v1/v2
兼容、以及只读 FLOAT32 深度视图在光轴深度转换时的原地写入崩溃。旧的真实 capture 可保留
原始资产/地形 hash 并进入 raw replay，但明确不冒充当前 mainline 资产绑定或训练资格。

使用历史 `depth-companion-sim-20261003-r2` 的真实 RGB/Depth/telemetry 得到 3 帧 recording、
1 个 recording-bound episode、3 个 transition；实际画面是地面采集，低阈值 detector 输出
尚未经复核，不是正确目标标签。rendered/staging 都保持 `training_ready=false`。实际调用
CUDA 严格增量训练入口后，trainer 返回 `1`：样本、场景、复核和独立 test episode 均不足，
没有生成新模型，没有晋级或修改 r33 reference。相关采集/训练回归共 88 项通过。

外部 cluster capture 本轮返回 `1` 且没有 capture artifact；后续诊断任务返回 `0` 只说明
诊断命令完成，原始日志尚未取回，不能判断远程 Docker/PX4/Gazebo 成功或确定失败根因。
本机 Docker daemon 仍不可达。下一步需获得飞行中、多场景、带连续遥测与独立复核的真实
3-D 轨迹，再从已有 OnboardAI 做增量训练；结构化 teacher 不能代替这类训练数据。
摘要：`docs/evidence/onboard-real-3d-input-chain-20261007.json`。

## 2026-10-06：收紧 OnboardAI 3-D 增量训练成功判定

修正 `scripts/run_onboard_3d_training_pipeline.py` 的一个假成功窗口：训练子进程返回 `0` 不再
单独等于“候选训练完成”。训练前现在拒绝非空或链接形式的候选目录，防止旧 ONNX/旧回执被误收；
训练后必须重新读取并校验 `training-config.json`、`training-report.json`、父 r33 SHA、三套输入
数据 SHA、reviewed real-3-D gate、train/evaluation/test 三路样本计数和指标，再用 ONNX checker、
CPU ONNX Runtime 和有限值 smoke 输入复开 `[1,40] -> [1,20]`。输出模型固定为唯一的
`onboard_policy.onnx`，缺失、重复、外部权重、哈希不一致或 test split 缺失都会标记
`candidate_output_invalid`，不会写成 `candidate_trained`，也不会 promotion。新增回归覆盖空目录门禁；
本次只修训练证据边界，没有改变当前 r33 reference 或 release parity。

## 2026-10-06：真实 Gazebo 3-D capture 已接入 OnboardAI 训练前绑定链

新增 `scripts/build_gazebo_raw_recording_manifest.py`，把现代 Gazebo 的 `MDGZSIM3` RGB frame-set、
`MDGZDEP1` depth companion 和同帧 telemetry 绑定成现有 raw replay recording。它会校验 RGB/Depth
场景 hash、主线 3-D 资产 hash、每帧 wire/payload hash、时间顺序和 `truth_loaded_by_runtime=false`；
原始 BGR24 文件作为来源证据保留，回放目录再生成无损 PNG，解码后的像素与 BGR24 逐字节一致。
输出的 `recording.manifest.json` 和 `gazebo_capture_binding.json` 明确标记
`training_ready=false`，不会生成目标标签，也不会把红车/绿车示例当作 Gazebo 主线训练目标。

该 recording 可以交给 `build_onboard_trajectory_staging.py --recording-manifest` 建立连续 episode，
但仍必须经过不同身份的轨迹复核、独立 train/evaluation/test 划分，才允许
`run_onboard_3d_training_pipeline.py --train` 从当前 r33 做增量微调。当前本机 Docker/Gazebo/PX4
仍不可用，所以本次只完成契约、绑定和回放测试，没有新增外部 SIM、Jetson、Pixhawk、HIL/REAL、
室外 GNSS 或最终 `0.3.0` 证据。

外部入口也已接入这个绑定器：`run_external_onboard_model_acceptance.py` 新增
`--recording-output-directory`、`--recording-id` 和 `--recording-scenario-seed`。当外部 capture
成功、RGB/Depth 输出目录和至少两个传感器帧满足条件时，它会在同一轮 evidence 中创建 recording；
dry-run 只记录 `planned_after_capture`，不会伪造 recording。该入口不会自动训练或 promotion，
后续仍然进入同一套 rendered dataset、trajectory review 和 r33 增量训练门。

## 2026-10-06：OnboardAI 几何超参探针完成，仍保持候选隔离

为验证几何 head 学习是否过于保守，又从同一个 r33 reference 做了一轮受控增量训练：
RTX 4060/CUDA 12.4、30 epochs、学习率 `0.005`、geometry weight `20.0`，仍是同一套
40 维输入/20 维输出 ONNX 契约。训练只使用中性多部件 SDF 的结构化 sensor-contract 行，
不是渲染像素；候选为
`artifacts/models/onboard-policy-r33-geometry-focused-cuda-20261006/onboard_policy_r33_geometry_focused_cuda_20261006.onnx`。

这次 bounded next-step range MAE 降到 `115.52m`，但方向 20° 内比例只有 `73/300=0.2433`，
综合诊断仍不通过。较高几何权重没有带来可接受的整体收益，候选未替换 r33。没有 RGB/depth、
reviewed trajectory、独立 test episode，也没有真实 Gazebo、Jetson、Pixhawk、HIL/REAL 或室外
GNSS 证据。下一轮训练必须建立在真正 Gazebo 3-D 采集的连续 episode 上，而不是继续堆结构化
teacher 行。证据见
[`onboard-policy-r33-geometry-focused-cuda-20261006.json`](evidence/onboard-policy-r33-geometry-focused-cuda-20261006.json)。

## 2026-10-06：OnboardAI 第三轮继续从 r33 做 3-D 几何增量训练，仍未晋级

本轮继续使用当前 OnboardAI r33 reference，在本机 RTX 4060/CUDA 12.4 上做增量微调；没有
新建一套模型，也没有把红车/绿车示例当作主线目标。训练课程读取中性、多部件的 Gazebo
SDF 场景结构，保持与 Jetson/Pixhawk 运行时相同的 40 维输入和 20 维输出契约，共 `251,000`
行、45 epochs。候选模型为
`artifacts/models/onboard-policy-r33-geometry-curriculum-cuda-20261006/onboard_policy_r33_geometry_curriculum_cuda_20261006.onnx`，
SHA-256=`a9f8ed7bfd8a81664343a786cd78b7c1e1155be945654bac0bc7b411de0bfce0`。

这轮训练仍然是结构化 3-D bootstrap：真实 Gazebo RGB、注册 FLOAT32 depth、CameraInfo、
同帧姿态/PX4 telemetry、reviewed trajectory 和独立 test 行数全部为 `0`，因此不能称作
Gazebo 像素训练或 Jetson 性能。评估器已改为按 raw head 的真实语义计算 `40m` bounded
next-step NED，而不是把下一步向量错误地与完整目标距离比较；校准后的 FOLLOW/GOTO 仍由
相机深度、内外参和姿态产生 measured NED，覆盖 raw head。候选的 bounded-step angle MAE
为 `33.12°`、range MAE 为 `127.46m`，诊断不通过，候选保持隔离，r33 reference 不变。

下一步不是继续堆结构化 teacher 行刷分，而是用真正的 3-D Gazebo 场景采集连续 RGB/depth/
CameraInfo/pose/PX4 episode，人工审核 search、inspect、record、return、abort 转移，再以
场景级 disjoint train/evaluation/test 进入下一轮增量微调。完整证据见
[`onboard-policy-r33-geometry-curriculum-cuda-20261006.json`](evidence/onboard-policy-r33-geometry-curriculum-cuda-20261006.json)。

## 2026-10-06：OnboardAI 第二轮几何增量训练仍未晋级，但方位学习明显改善

在第一轮 CUDA 候选的独立导航诊断显示角度和距离都不合格后，本轮继续从同一个 r33
reference 增量微调，没有新建模型体系，也没有把红车/绿车例子当作 Gazebo 目标。训练数据来自
仓库中性 3-D SDF 场景的结构化 sensor-contract curriculum，并提高连续 bearing/range
几何样本比例；实际运行在本机 NVIDIA GeForce RTX 4060、CUDA 12.4、PyTorch
`2.6.0+cu124` 上，共 `121,000` 行、35 epochs，仍保持同一 40 维输入和 20 维输出。

候选为
`artifacts/models/onboard-policy-r33-range-focused-cuda-20261006/onboard_policy_r33_range_focused_cuda_20261006.onnx`，
SHA-256=`7256576469665e7f3173e2563345646075ee4277e095f2032286836ca9e39588`。独立连续导航诊断中，
方向 MAE 改善到 `11.05°`、20° 内比例 `256/300=0.8533`，但距离 MAE 仍为
`126.998m`，未达到晋级阈值，所以候选保持隔离，r33 reference 不变。这个结果只证明
训练目标中的几何方向关系改善，不代表真实 Gazebo、Jetson、Pixhawk、HIL、REAL 或室外 GNSS
性能。完整证据见
[`onboard-policy-r33-range-focused-cuda-20261006.json`](evidence/onboard-policy-r33-range-focused-cuda-20261006.json)。

下一轮不再继续用结构化 teacher 行刷指标；必须先取得真实 Gazebo 3-D RGB、注册 FLOAT32
depth、CameraInfo、同帧机体姿态/PX4 telemetry 和独立审阅的连续 episode，再通过严格训练门。
训练门现已额外要求至少一个独立 `test` episode/transition；只有 train/evaluation 的数据会
直接 fail-closed。新增 gate 回归通过，避免把同一批轨迹同时当训练和最终评估。

## 2026-10-06：OnboardAI 增量训练已在 CUDA 上完成一轮，但候选保持隔离

本轮从当前 r33 reference 继续训练，没有新建飞控模型，也没有使用红车/绿车颜色作为训练目标。
本机 NVIDIA GeForce RTX 4060、CUDA 12.4、PyTorch `2.6.0+cu124` 已实际运行训练：结构化实体
3-D SDF sensor-contract curriculum 共 `66,000` 行、`8` epochs，仍保持同一 40 维输入和 20 维
输出。候选、报告和配置分别绑定于
`artifacts/models/onboard-policy-r33-incremental-3d-cuda-20261006/`，候选 SHA-256 为
`43389c168fb9d1293b83bdd4678869e89ff7340bcc5e9a8ccf64c35983b79b93`。

这不是 Gazebo 像素训练成功：报告明确为 `synthetic_bootstrap=true`、`rendered_rgb_rows=0`、
`reviewed_trajectory_rows=0`、独立 `test` 行数为 `0`。同一 ONNX runner 的独立连续几何诊断为
方向 20° 内比例 `55/300=0.1833`、方向 MAE `60.33°`、距离 MAE `191.72m`，因此候选没有
替换 r33，也没有写入 reference 或发布清单。完整边界和哈希见
[`onboard-policy-r33-incremental-3d-cuda-20261006.json`](evidence/onboard-policy-r33-incremental-3d-cuda-20261006.json)。

训练前的数据链也完成了一次必要修正：`build_onboard_trajectory_staging.py` 现在要求 Gazebo
`FLOAT32_M + optical_axis_z_m + registered_to_rgb` 元数据，绑定 RGB 尺寸/stride/CameraInfo，
并使用同帧姿态、相机内外参和 `_target_rays` 生成 NED 几何；没有 CameraInfo 时保留 metric
distance 但只标为 `camera_relative`，不伪造导航方向。重复 depth frame、未知语义、缺少内参或
尺寸不一致都会 fail-closed。相关训练/感知回归已通过 `75 passed`，Ruff 与 diff check 通过。
真正的下一轮训练仍必须等待当前三类 3-D Gazebo 的连续 RGB/Depth/CameraInfo/pose/PX4 telemetry
和独立审阅轨迹；Docker/Gazebo 外部运行时当前仍未启动。

## 2026-10-06：Docker 外部仿真恢复尝试已留存可回滚证据，仍未启动训练

为进入真实 Gazebo/PX4 3-D capture，已对 Docker Desktop 做了最小范围的可回滚恢复：先旋转
`Docker/run` 目录，再旋转 `docker-secrets-engine` 父目录，原始内容均保留在带时间戳的 backup。
Docker Desktop 重启后仍在 `sailor-ingest.sock` 的 Windows reparse point 处以 WinError 1920 崩溃；
对单个精确 entry 的清理也被系统拒绝，因此没有 factory reset、没有删除镜像/容器/卷，也没有
声称 Docker、Gazebo 或 PX4 已运行。证据见
[`docker-desktop-stale-socket-recovery-20261006-r1.json`](evidence/docker-desktop-stale-socket-recovery-20261006-r1.json)。
当前训练主线仍保持 `training_ready=false`、没有启动 OnboardAI 训练、没有生成候选模型；仓库内的
3-D capture 命令和 `--device cuda` 入口已准备好，等待外部 runtime 恢复后执行。

## 2026-10-06：训练入口已显式绑定 4060/CUDA，但仍等待真实 3-D 数据

OnboardAI 的 3-D 增量训练入口现在支持显式 `--device auto|cpu|cuda`，并把请求的设备写入
训练回执；`--device cuda` 会把同一参数继续传给现有 `train_onboard_model.py`，不会改变
40 维输入、20 维输出或 r33 父模型绑定。刚执行的 CUDA preflight 记录了
`training_device_requested=cuda`，但仍为 `preflight_only`：当前实体 Gazebo 的 RGB、FLOAT32
Depth、CameraInfo、姿态、GPS/telemetry 和 reviewed trajectory 尚未齐备，所以没有启动训练、
没有生成候选模型。回执为
[`pipeline.json`](../artifacts/evaluation/onboard-3d-training-pipeline-preflight-20261006-cuda/pipeline.json)
（SHA-256=`ca10860f6583cb0772304aa22e0ad4a3c8c4d3930174f4c7746d2745121253d4`）和
[`preflight.json`](../artifacts/evaluation/onboard-3d-training-pipeline-preflight-20261006-cuda/preflight.json)
（SHA-256=`78ee55992eda6df24aa98ae8bb503d245101d3749274202654fe347bc65e65a1`）。

## 2026-10-06：4060 训练主机已就绪，但真实 3-D capture 仍是唯一入口

本机训练环境已确认可用：NVIDIA GeForce RTX 4060、CUDA 12.4、PyTorch CUDA 可用，
可以承担后续 OnboardAI 增量微调；这不是 Jetson 或实际飞行证据。当前训练入口仍只允许
从 r33 父模型继续微调，保持 SIM 与未来 Jetson 的同一 40 维输入/20 维 ONNX 输出契约。

刚执行的真实 3-D pipeline preflight 明确返回 `preflight_only`、`training_ready=false`、
`capture_eligible_count=0`。阻塞原因是 Docker/Gazebo/PX4 runtime 尚未可用，以及没有当前
mainline 的 RGB、FLOAT32 Depth、CameraInfo、姿态、GPS/telemetry 和 reviewed continuous
trajectory episode；因此没有启动训练、没有生成候选模型，也没有把结构化 teacher 或旧像素
fixture 当成训练数据。回执为
[`pipeline.json`](../artifacts/evaluation/onboard-3d-training-pipeline-preflight-20261006/pipeline.json)
（SHA-256=`3cb828328717c1eb250c24eb3cfa019f0f14e203179df1d624d92b497eb1357b`）和
[`preflight.json`](../artifacts/evaluation/onboard-3d-training-pipeline-preflight-20261006/preflight.json)
（SHA-256=`9c78980bc4f5afe612b715bc5797812ee7ad13600612e7104c8ada47f4474f6b`）。

## 2026-10-06：训练主线改为实体 3-D Gazebo 传感器闭环

红车/绿车等名称只保留为历史适配器回归示例，不再作为 Gazebo 训练目标或任务答案。当前
训练主线固定使用仓库内的实体 SDF world、车辆模型和目标模型，通过真实 Gazebo camera、
FLOAT32 depth、CameraInfo、机体姿态和 PX4 telemetry 进入现有 YOLO/跟踪/测距适配器，再把
同一个 40 维 OnboardAI 输入交给 r33 父模型做增量微调；不会用手写颜色规则或另造一套模拟 AI。

r88 已完成但低于 r86，已隔离，当前 SIM/未来 Jetson reference 仍是 r33。训练编排入口为
`scripts/run_onboard_3d_training_pipeline.py`，没有合格 RGB/Depth/审核运动轨迹时会明确
`preflight_only`/`training_ready=false`，不会把结构化 3-D teacher 行、旧像素 fixture 或
远程主机探测当成训练数据。下一次真正训练只在当前 mainline 的渲染 episode 通过后启动。

本轮还故意把旧的渲染/结构化数据送入训练入口做拒绝测试。结果为 `blocked`：渲染行数、独立
3-D 场景数、可用 FLOAT32 depth、标定 NED 几何、detector-positive 帧、连续轨迹 transition
和 recording-bound episode 均未达到门槛，训练器没有生成候选模型。这证明“有一个 JSON 或有
结构化目标行”不会被误当成 3-D Gazebo 训练数据；回执为
[`onboard-3d-training-pipeline-reject-old-data-20261006.json`](../artifacts/evaluation/onboard-3d-training-pipeline-reject-old-data-20261006.json)，
SHA-256=`335a2159f395f9ad7a6f441de55055cb6cec26a68853f055f9fad52d26263064`，预检回执 SHA-256=
`6441c539cbcdbf9ab6af16cb07ef1fbe36c0fdde6e8346015f073ba256b7036c`。

同时修正了训练数据和 Jetson 实时推理之间的距离语义：Gazebo 的 FLOAT32 depth 声明的是
光轴 Z 距离，而实时适配器使用的是射线斜距。构建器现在按同一组 CameraInfo 内参逐像素完成
Z→斜距换算，再计算 bbox 的稳健中位数；缺少内参或出现未知距离语义会直接拒绝。新增的
optical-axis parity、ray-slant 保持和缺内参 fail-closed 测试共 `11 passed`。这不是训练成功或
Jetson 运行证据，但消除了训练标签与实际推理几何定义不一致的实质缺陷。

另外补齐了真实渲染数据的审核封存入口：
`scripts/seal_onboard_rendered_3d_dataset.py` 要求每一帧都有独立审核 JSONL，逐帧绑定
`frame_id`、RGB payload SHA-256、FLOAT32 depth payload SHA-256、任务动作、飞行动作和回归标签；
annotator 与 reviewer 必须不同，且必须显式传入 `--confirm-reviewed`。它只生成一个新的
`training_ready=true` 数据集副本和同目录 review receipt，不修改原始 capture，也不会开启飞控或
promotion。训练器现在会重新验证 receipt 的 canonical hash、review JSONL、每一帧 payload 绑定和
最终标签，篡改、漏帧、重复帧、truth 字段或非独立审核都会拒绝。相关测试与训练回归共 `25 passed`，
但当前仓库仍没有真实 capture，因此没有生成 training-ready 数据。

## 2026-10-06：r88 保守实时增量仍低于 r86，停止结构化 teacher 扩张

从 r86 继续以较低学习率和更强父模型蒸馏训练 r88，只加入实时转移、中性目标和三类实体 3-D
场景课程，没有重复放大 phase/bearing 样本。RTX 4060/CUDA 共 386,000 行、20 epochs；独立
truth-free 泛化为 raw=`19/22`、安全门后=`22/22`、方向/距离=`22/22`，仍低于 r86 的 raw=`22/22`。
因此 r88 隔离、不 promotion，r33 仍是当前 reference。证据见
[`onboard-policy-r88-realtime-conservative-20261006.json`](evidence/onboard-policy-r88-realtime-conservative-20261006.json)。
这次结果确定了下一训练动作：不再继续增加结构化 teacher 行，必须先取得真实 3-D Gazebo RGB、FLOAT32
Depth、CameraInfo、姿态和 PX4 telemetry episode，再做像素级增量训练和独立 holdout。

## 2026-10-06：三类 PX4 Mission 编译入口补齐 VTOL

通用 `scripts/run_external_vehicle_mission_campaign.py` 现在同时覆盖 `multirotor`、`vtol` 和
`fixed-wing`。VTOL 使用现有 `VtolMissionGeometry`、`Px4MissionSafetyPolicy`、同一任务选择/规划
重验证和 airframe profile hash；没有另写一套飞控逻辑。新增回归确认 VTOL 的 `task_binding` 为
`mission_bound=true`、`planner_revalidated=true`、`truth_free=true`，且仍保持
`policy_applied_to_actuator=false`。这只是本地 Mission 契约验证，不是外部 Gazebo/PX4、HIL、REAL 或
硬件飞行证据。

## 2026-10-06：r87 实时自主转移增量训练完成，但不替换 reference

Docker 外部运行时仍被 Windows AF_UNIX reparse point 阻塞时，本轮先在本机 RTX 4060/CUDA 上从 r86
继续训练同一套 OnboardAI。r87 加入实时状态转移、目标匹配后记录/返航、目标不匹配继续搜索、
GPS/电量/障碍/推理故障保护、阶段转移和左右对称 3-D 几何课程；保持 40 维输入、20 维输出、
同一 ONNX 文件给软件 SIM 与未来 Jetson 使用，没有把红车/绿车写成 Gazebo 训练标签。

训练共 572,000 行，32 epochs；模型哈希为
`0dedf5300911db4bb40c28e9904205e04a1a3ce428f9840bdcaf4a6e86fd24af`。独立 truth-free 泛化为
raw=`18/22`、安全门后=`22/22`、导航方向=`22/22`、距离范围=`22/22`；相对 r86 的 raw head
有回归，故记录为 `candidate_quarantined_no_promotion`，r33 仍是唯一 reference。训练报告和独立
评估见 [`onboard-policy-r87-realtime-3d-autonomy-20261006.json`](evidence/onboard-policy-r87-realtime-3d-autonomy-20261006.json)。
这轮仍没有真实 Gazebo RGB/Depth/CameraInfo/pose/telemetry，也没有 reviewed trajectory，不能证明
视觉检测、Jetson、Pixhawk 或实际飞行性能；下一次应优先使用真实 3-D 渲染 episode，而不是继续堆结构化 teacher 行。

随后用当前 hash-bound r33 reference 做了中性任务链软件回放：`search → match_target → record_location →
return_home → report`，planner 重新校验为 true，生成 2 个 movement Mission、1 次 return request 和
1 份 report。回执为 [`onboard-policy-chain-r33-neutral-3d-20261006.json`](../artifacts/evaluation/onboard-policy-chain-r33-neutral-3d-20261006.json)，
SHA-256=`70aef56699f2c7acbd48362e9fc58567f70f6a0c1a223e11393e855092c92011`。这证明当前 reference
已经接入软件自治链，不证明 Gazebo/PX4 外部运行、Jetson、Pixhawk 或实际执行；回执明确
`software_sim_only=true`、`policy_applied_to_actuator=false`。

## 2026-10-06：r86 右侧导航增量训练通过独立压力集，但仍不替换 reference

针对 r84/r85 唯一暴露的 `bearing-right` 失败，本轮从当前 r33 继续微调同一套 OnboardAI，
没有另起模型或把红车/绿车变成 Gazebo 标签。r86 增加了左右对称、不同距离和不同飞行状态的
3-D 结构化几何课程，并保留父模型蒸馏；本机 NVIDIA GeForce RTX 4060/CUDA 训练 35 epochs，
共 255,500 条结构化样本，其中 48,000 条来自当前三类实体 SDF 场景课程。

独立 truth-free 泛化为 raw=`22/22`、安全门后=`22/22`，导航方向=`22/22`、距离范围=`22/22`，
之前的 `bearing-right` 已通过。模型仍记录为 `candidate_quarantined_no_promotion`：本轮没有真实
Gazebo RGB/Depth/CameraInfo/pose/telemetry 行，没有 recording-bound reviewed trajectory，外部
Gazebo/PX4 runtime 也没有在本机启动。因此 r86 只能证明结构化导航回归改善，不能证明视觉检测、
实机 Jetson 或飞行性能；SIM 与未来 Jetson 的当前 reference 仍为 r33。

证据见 [`onboard-policy-r86-3d-bearing-robust-20261006.json`](evidence/onboard-policy-r86-3d-bearing-robust-20261006.json)。
下一步仍是恢复 Gazebo/PX4，采集真实中性 3-D 模型的 RGB、FLOAT32 Depth、CameraInfo、姿态、GPS/telemetry，
并在 reviewed continuous episode 通过后再评估像素级增量候选。

本轮对 Docker Desktop 做了只针对运行时的诊断：`docker-desktop` WSL distro 仍为 stopped，backend
日志先后暴露 `docker-secrets-engine\\engine.sock` 和 `sailor-ingest.sock` reparse point 无法重命名为 `.stale`，因此 Docker engine pipe
没有启动。预检现在会从 backend.error.json 动态提取实际 socket 路径并标记为 `docker_desktop_stale_socket`，而不是笼统写成 daemon
不可用；最新回执为
[`onboard-3d-training-preflight-20261006-r11.json`](../artifacts/evaluation/onboard-3d-training-preflight-20261006-r11.json)，
SHA-256=`25bc1bc7e953df1673b1422897a0cc191608b04ed154bccd5cf6177bb940aefa`，其中 backend 日志 SHA-256 为
`f360ae3f5b5e8dffe0b228085c5bc90b5c383be720ba465b7e13e0d583d24e8b`。刚才再次执行官方
`docker desktop start --detach` 返回成功，但 daemon 仍不可达，故不能把命令退出码误写成 Gazebo 运行证据。
仓库脚本不会删除该系统文件或执行 factory reset；本轮已把 `docker-secrets-engine\\engine.sock` 父目录
移到可回滚备份，但 Docker 重启后又在 `sailor-ingest.sock` 处失败，Windows 对最后一级 reparse point
的 WinError 1920 清理也被拒绝。因此外部 Gazebo 仍是 blocked；没有删除镜像、容器或卷。恢复入口默认只读，
并支持对 backend.error.json 给出的精确路径做可逆父目录轮换。恢复过程见
[`docker-desktop-socket-recovery-20261006.json`](evidence/docker-desktop-socket-recovery-20261006.json)。

随后把当前实体 3-D world、模型和 OnboardAI 入口打成受控快照，在 Linux `nuc` worker 上做了
第二次 Docker/PX4/Gazebo 主机探测。快照为 865 个文件，SHA-256=`f9c3b650342188b2c3c629a75d90b28208ef17092df1d89af7a27c76c6281bc5`；
远程任务实际到达 worker，但 pinned `px4io/px4-sitl-gazebo` 镜像和 daemon 门仍未就绪，故
`external_gazebo_runtime=false`、`rendered_rgb_depth_capture=false`、`training_ready=false`。
这次探测只证明远程前置条件仍不满足，不是 Gazebo 运行或 OnboardAI 训练证据。回执见
[`cluster-gazebo-preflight-20261006.json`](evidence/cluster-gazebo-preflight-20261006.json)。

## 2026-10-06：r81/r82 结构化 3-D 候选对照完成，当前 reference 不变

按“先训练同一套 OnboardAI，再接真实 Gazebo 像素”的顺序，在 RTX 4060 上从现有 r33/r80
分别训练了两个候选。两者都读取当前三套 Gazebo world SDF 的多 link 几何统计，保持原来的
40 维输入、20 维输出和 ONNX 接口；没有把红车/绿车写入任务，也没有把 world truth 送入模型。

r81 从 r80 继续训练，22-case truth-free 泛化 raw head=`19/22`、安全门后=`22/22`；r82
从 r33 重新开始，raw head=`20/22`、安全门后=`21/22`，并失败 `bearing-left`。两轮都没有
任何实际 Gazebo RGB/Depth 行，也没有 reviewed trajectory，因此都明确 `candidate_rejected`，
不能替换 r33。完整模型、训练配置、报告和泛化报告哈希见
[`onboard-policy-r81-r82-geometry-candidate-comparison-20261006.json`](evidence/onboard-policy-r81-r82-geometry-candidate-comparison-20261006.json)。

本轮又把真实 3-D 采集的航线约束接上：`configs/scenarios/gazebo-3d-capture-plan-v1.json`
从当前 urban SDF 场景包络生成仅供初始 Mission geometry 的多旋翼 lawn-mower 路线；目标 pose
不参与路线生成，不进入运行时输入，也不会进入 OnboardAI feature vector。`run_external_onboard_model_acceptance.py`
的 dry-run 已记录 `target_pose_excluded_from_runtime=true` 和
`target_pose_excluded_from_model_input=true`。这不是用目标真值验证模型，而是让真实相机/Depth
采集的初始 Mission geometry 只覆盖场景搜索包络；进入实时事件后，模型仍可重规划，
所以这里不把五个离线路点冒充为最终飞行轨迹覆盖。

最新预检回执为
[`onboard-3d-training-preflight-20261006-r6.json`](evidence/onboard-3d-training-preflight-20261006-r6.json)，
仍为 `blocked`：当前 Docker/Gazebo/PX4 外部运行时不可用，合格 RGB+Depth capture 为 `0`；
urban multirotor 已绑定 capture-plan，river VTOL 和 coastal fixed-wing 继续明确阻断，避免
误用多旋翼航线。r33 仍是当前 reference，r81/r82 仍未 promotion。

随后在 RTX 4060 上完成了 r83 增量实验：从 r33 载入同一 40/20 ONNX 契约，加入 21,000 条
当前三场景 SDF 结构化行、左右导航 sign loss 和父模型蒸馏。独立 22-case 泛化为 raw=`21/22`、
安全门后=`22/22`，导航方向/距离均 `22/22`，没有 guarded failure；但它仍没有任何实际
Gazebo RGB/Depth 行或 reviewed trajectory，因此只记录为 `candidate_quarantined_no_promotion`，
不能替换 r33。泛化回执中的历史压力用例名称不是 Gazebo 训练标签，颜色没有进入 3-D 训练标签。
证据见 [`onboard-policy-r83-3d-sign-balanced-20261006.json`](evidence/onboard-policy-r83-3d-sign-balanced-20261006.json)。

随后在 RTX 4060 上做了两次针对训练配比的增量试验。r84 把中性 SDF 场景结构行扩大到 75,000，
但独立泛化只有 raw=`20/22`、安全门后=`21/22`，`bearing-right` 失败；r85 降低 3-D 结构行比例、
增加左右对称 bearing 锚点后，raw/guarded=`21/22`,`21/22`，仍然在 `bearing-right` 失败。两版都
沿用 r33 的 40/20 ONNX 契约、同一父权重和同一 3-D manifest，没有 RGB/Depth 渲染行，也没有
recording-bound trajectory，所以都记录为 `candidate_quarantined_no_promotion`；r33 继续是 SIM 与
未来 Jetson 的唯一 reference。证据分别见
[`onboard-policy-r84-3d-geometry-curriculum-20261006.json`](evidence/onboard-policy-r84-3d-geometry-curriculum-20261006.json)
和 [`onboard-policy-r85-3d-balanced-navigation-20261006.json`](evidence/onboard-policy-r85-3d-balanced-navigation-20261006.json)。

这两次失败给出明确结论：继续堆结构化 SDF teacher 不能替代真实 3-D 视觉/深度 episode，且不能掩盖
左右方位回归的独立失败。下一轮优先级不是再加颜色 fixture，而是恢复 Gazebo/PX4，采集实际中性 3-D
模型的 RGB、FLOAT32 Depth、CameraInfo、姿态、GPS/telemetry，再用同一 OnboardAI 做像素级增量训练。

同时修正了 3-D 训练预检：当前外部 acceptance runner 只实现并审查过 multirotor geometry/policy，
所以预检现在只为 urban multirotor 生成可执行采集命令，river VTOL 和 coastal fixed-wing 明确
标为 `blocked_unsupported_vehicle_runner`，不会误用多旋翼航线生成训练数据。三种机型后续仍使用
同一 OnboardAI 接口，但必须先各自接入正确的 Mission geometry、safety policy 和 camera calibration。

## 2026-10-06：渲染数据生成器统一 Jetson/SIM 几何契约

`scripts/build_onboard_rendered_3d_dataset.py` 现在直接复用现有 OnboardAI 的
`ContextSnapshot + OnboardRuntimeEvent + OnboardModelInput.feature_vector()`，不再在训练数据生成器
里维护第二套特征归一化公式。存在 CameraInfo、Depth、机体姿态和 heading 时，目标框会经过同一套
相机射线、安装偏移和 NED 变换生成 bearing、水平距离和 North/East 回归；缺少标定时保留
`camera_relative`，不伪造 NED 方位、不生成绝对 GPS/航点标签。新增的 8 个回归测试覆盖 heading
`0/90/180°`、pitch、Depth、相机偏移、无标定和 truth 注入拒绝，全部通过。该工具仍只生成训练输入，
不代表 Gazebo 外部运行、Jetson、Pixhawk、HIL、REAL 或室外 GNSS 已通过。

## 2026-10-06：r80 加入 world SDF 场景级 OnboardAI 课程，结构化 3-D holdout 通过

这轮没有再用红车/绿车做目标验证，也没有新建一套模型。训练器从当前 r33 父模型继续增量训练
`onboard-policy-r80-world-geometry-dev`，保持 40 维输入、原任务动作头、原飞行动作头和同一 ONNX
输出契约。新增的课程直接读取仓库三套 Gazebo world SDF 的 visual/collision 几何统计，把场景复杂度
用于生成搜索能见度、障碍间隙和置信度的教师条件；它不读取目标真值位置、不读取颜色、不把答案写进
输入特征。训练在本机 NVIDIA GeForce RTX 4060/CUDA 完成，共 12,000 条实体 3-D 结构化场景行。

r80 的独立 1,200-case 三场景 holdout 为 task/flight/compatible=`0.9958/0.9950/0.9967`；22-case
泛化集 raw head `20/22`，经既有安全兼容性门后 `22/22`，导航方向 `22/22`、距离范围 `22/22`，
class/color invariance 通过。这个“通过”只代表 truth-free 结构化软件测试，不代表视觉检测精度或飞行性能。

r80 仍不替换 r33：当前没有合格的连续 Gazebo RGB + FLOAT32 Depth + CameraInfo + pose + PX4 telemetry
训练 episode，也没有 recording-bound reviewed trajectory。因此 `candidate_promoted=false`，SIM 与未来
Jetson 的当前 reference 仍为 r33。完整绑定见
[`onboard-policy-r80-world-geometry-20261006.json`](evidence/onboard-policy-r80-world-geometry-20261006.json)。

## 2026-10-06：r73 几何加权候选拒绝，真实 3-D 训练门继续收紧

在本机 RTX 4060 上继续从 r33 增量微调 r73，使用同一 40 维 OnboardAI 输入、任务/飞行动作输出、
实体 SDF 主线和更强的导航几何损失。独立语义 holdout 仍为 `7/7`，但 900-case 结构化 3-D
holdout 为 task/flight/compatible=`0.8767/0.8789/0.9578`，几何诊断 angle/range MAE=`68.91°/191.39 m`，
因此 r73 明确拒绝，r33 仍是 SIM 与未来 Jetson 的唯一 reference。这个候选没有 RGB/Depth 像素训练，
也没有外部 Gazebo、Jetson、Pixhawk、HIL/REAL 或室外 GNSS 证据。

同时收紧真实 3-D 训练门：即使 RGB、FLOAT32 Depth 和 detector hit 都存在，每个训练帧还必须有
`camera_calibration_depth_pose_ned` 几何来源，才能计入 calibrated NED geometry rows；缺少 CameraInfo、
相机/机体姿态或 heading 的帧只能做 plumbing 检查，不能进入 `training_ready` 微调。

## 2026-10-06：r72 中性 3-D OnboardAI smoke 微调完成，但候选被拒绝

在本机 RTX 4060/CUDA 上从当前 r33 继续训练同一套 40 维 OnboardAI，使用仓库三类实体 SDF
manifest、中性目标状态、父模型蒸馏权重 `0.5` 和几何辅助损失 `0.5`。没有使用红车/绿车 fixture，
没有生成第二套飞控模型，也没有修改当前 SIM/Jetson reference。

r72 的独立语义 holdout 为 `7/7`，但独立 900-case 结构化 3-D sensor holdout 只有
task=`0.8789`、flight=`0.8833`、compatible=`0.9656`，泛化集失败 `bearing-right`，连续几何诊断
也未通过（angle MAE=`75.14°`、range MAE=`151.32 m`）。因此 r72 已明确 `candidate_rejected`，
r33 继续保留。完整模型、报告、holdout 和 SHA-256 见
[`onboard-policy-r72-neutral-3d-distilled-smoke-20261006.json`](evidence/onboard-policy-r72-neutral-3d-distilled-smoke-20261006.json)。
这轮确认了 4060 训练链条可用，同时证明不能仅靠结构化 SDF teacher 宣称视觉训练成功；下一轮必须
使用真实 Gazebo RGB/Depth/CameraInfo/PX4 telemetry episode。

## 2026-10-06：真实 3-D OnboardAI 训练预检完成，当前仍缺采集数据

按“先训练同一套 OnboardAI，再接 Gazebo 像素微调”的顺序执行了新的预检。预检扫描 122 份历史
RGB manifest，但当前三类实体 3-D mainline 的 `capture_eligible_count=0`、`training_ready_count=0`。
历史帧要么场景哈希已过期、帧数不足，要么没有同帧 FLOAT32 depth companion；因此没有任何一行被送入
训练，也没有把红车/绿车例子当成 Gazebo 验收标签。

本机 Docker daemon、Gazebo 和 PX4 CLI 当前不可用。下一次真正训练必须先用同一个实体 3-D world
采集起飞后的连续 RGB、Depth、CameraInfo、姿态和 PX4 telemetry，再绑定 reviewed trajectory 和
独立 holdout。预检回执见
[`onboard-3d-training-preflight-20261006-r2.json`](evidence/onboard-3d-training-preflight-20261006-r2.json)。
当前 r33 仍是 SIM 与未来 Jetson 共用 reference；最终 `0.3.0`、Jetson、Pixhawk、HIL/REAL、室外
GNSS 和物理性能均未宣称通过。

## 2026-10-06：OnboardAI 增量训练加入父模型蒸馏，候选仍隔离

按“先训练现有 OnboardAI、再接真实 Gazebo 3-D 采集”的顺序，在 RTX 4060 上继续使用同一份
r33 父模型、40 维输入和任务/飞行动作输出。r70 使用中性 3-D SDF 课程但没有父模型保持约束，
900-case 结构化 holdout 退化到 task=`0.8889`、flight=`0.8800`、compatible=`0.9589`，已明确拒绝。

因此训练器新增可选 `--parent-distillation-weight`：父模型权重先经过 SHA-256 校验并冻结，增量
课程学习新样本时同时约束完整输出，避免覆盖既有搜索、跟随、记录、返航和故障处理边界。r71 从
r69 继续微调，使用中性 3-D 课程和蒸馏权重 `0.5`；独立任务 holdout=`7/7`，结构化 3-D
holdout 为 task=`0.9944`、flight=`0.9878`、compatible=`0.9933`。r71 仍未 promotion：它是
结构化传感器契约候选，不是像素视觉成绩；独立的 `TARGET_MOVED + INSPECT_TARGET` 几何诊断仍
未通过，实际 N/E 仍必须来自经过 CameraInfo/Depth/姿态/heading 标定的感知几何，再经 planner
和 PX4 Mission 重验。完整成对证据见
[`onboard-policy-r70-r71-parent-distillation-20261006.json`](evidence/onboard-policy-r70-r71-parent-distillation-20261006.json)。

当前 reference 仍是 r33；没有新增外部 Gazebo runtime、Jetson、Pixhawk、HIL/REAL 或室外 GNSS
证据。下一步应优先恢复真实 3-D renderer，采集带 RGB、FLOAT32 Depth、CameraInfo、姿态、GPS/
PX4 telemetry 的连续 episode，经过独立标签审核后才允许像素级增量训练。

## 2026-10-06：先修正 3-D 几何契约，再继续训练 OnboardAI

本轮没有把红车/绿车例子当成 Gazebo 场景。主线仍使用仓库自有的中性多部件 SDF 目标、城市多旋翼、
河道 VTOL 和海岸固定翼 3-D 场景；这些资产已通过 manifest 结构审计，但本机 Docker/Gazebo 外部运行
仍未启动，不能写成真实仿真证据。

审计发现一个会直接污染训练的坐标问题：相机未完成姿态/安装标定时的 bearing 是图像相对角，旧回退却
把它当成 NED 绝对角。现在 `OnboardRuntimeEvent`/`OnboardPerceptionObservation` 明确标记
`target_geometry_frame`：只有 `ned` 才进入绝对导航特征和 GPS 极坐标回退；`camera_relative` 不能带
NED 偏移，也不能驱动 FollowTarget 的绝对航点。完成相机、深度、机体姿态和 heading 标定后，适配器
会用同一帧的 NED offset 重新计算水平距离和 bearing。未标定输入 fail-closed。

训练损失也已修正：几何向量使用与 `tanh × 500 m` 相同的归一化量纲，并加入方向夹角和径向距离项；
bearing curriculum 改为全圆角度、8–420 米连续距离，而不是固定 75 米和只覆盖前向左右。r67/r68/r69
均从现有父模型增量训练，仍保持 40 维输入、同一任务/飞行动作头和同一 ONNX 输出，不替换当前 r33。

独立连续 NED 几何诊断（256 个随机角度/距离，非训练 teacher）结果：r66 角度 MAE=`65.77°`、
距离 MAE=`143.75 m`；r67=`38.51°/164.28 m`；r68=`32.27°/166.02 m`；r69=`28.69°/165.68 m`。
方向有改善，但距离仍失败，因此所有候选均 `candidate_promoted=false`，也不允许真实 FollowTarget。
完整逐例报告和哈希见 [`onboard-navigation-geometry-20261006.json`](evidence/onboard-navigation-geometry-20261006.json)。
当前正确的工程决策是：由 OnboardAI 判断是否 FollowTarget，由经过标定的感知几何提供实际 NED 目标向量，
再交给既有 planner、geofence、能量和 PX4 Mission 编译器；不要让模型凭空猜测已经由深度和姿态测出的坐标。

## 2026-10-06：分层记录外部 3-D preflight 失败位置

复查 Controller 历史后确认，外部失败不是单一原因：旧快照的 NUC 任务
`adf1a049-e1ea-4251-975f-76f34a7fe723` 已完成 source preparation 并启动
`verify-and-run-external-3d-capture`，但 step 以 exit `1` 结束且没有保存 stdout/stderr，
所以不能判断是 Docker daemon、镜像还是脚本命令失败；这仍不构成 Gazebo 运行证据。当前工作树
快照 `455df0e4-8258-459c-9991-45bc7bd18758` 则在 worker source preparation 以 HTTP `404`
结束，重试又被 Controller 的 stale slot 误拒绝，尽管 fleet telemetry 显示 running/queue 为 0。
分层记录见
[`cluster-docker-gazebo-preflight-20261006-r4.json`](evidence/cluster-docker-gazebo-preflight-20261006-r4.json)。

因此下一次外部运行只需要先清除 Controller reservation，再使用当前快照并强制保存每一步的
stdout/stderr；在这之前不修改 3-D world、OnboardAI 或 release gate 来掩盖环境问题。

## 2026-10-06：外部 Gazebo/PX4 preflight 仍未启动，已保留可复核失败证据

本轮先启动本机 Docker Desktop，daemon 仍无法连接，WSL `docker-desktop` 保持 stopped；随后将
当前 3-D world、脚本、配置和 reference/candidate 模型打成 SHA-256 绑定快照并上传到 ClusterYourCodex。
Controller 正确选择在线 Linux `nuc`，但任务在 worker source preparation 阶段以 HTTP `404 Not Found`
终止，Docker/Gazebo step 没有开始。因此这次没有 RGB/Depth、CameraInfo、PX4 telemetry 或外部
Gazebo 运行证据。完整记录见
[`cluster-docker-gazebo-preflight-20261006-r3.json`](evidence/cluster-docker-gazebo-preflight-20261006-r3.json)。

这不是模型训练失败，也不是 Gazebo 通过；r66 结构化 3-D 训练结果保持不变，candidate 仍未 promotion。
下一步是修复 Docker daemon/worker source preparation 后，使用同一快照和同一 3-D world 重跑外部
campaign，再接入连续 RGB+Depth+CameraInfo+PX4 telemetry。

## 2026-10-06：r66 完成左右对称导航增量训练，仍不替换 reference

本轮继续从 r65 增量训练同一套 OnboardAI，没有另起飞控模型，也没有把红车/绿车示例当作
Gazebo 场景。r66 在 RTX 4060 上使用同一 40 维 truth-free 契约、三类实体 SDF manifest，
加入更强的左右 bearing 对称采样、有限 navigation sign loss 和 geometry loss；训练共 112,600
行、20 epochs，模型、训练配置和报告均已用 SHA-256 绑定。

两个独立 structured holdout（各 1,200 case）结果为 task=`0.995833/0.993333`、
flight=`0.991667/0.990000`、compatible=`0.993333/0.993333`。22-case 泛化集的 raw head
为 `20/22`，但经过现有安全兼容门后的 guarded、navigation direction、navigation range
均为 `22/22`，没有 guarded failure。证据见
[`onboard-policy-r66-bearing-symmetry-20261006.json`](evidence/onboard-policy-r66-bearing-symmetry-20261006.json)。

r66 仍是结构化 3-D geometry candidate，`candidate_promoted=false`，r33 继续是 SIM 与未来
Jetson 的唯一 reference。r66 没有 RGB/Depth rendered rows，也没有启动外部 Gazebo/PX4，
因此这些数字不是像素视觉、Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。
下一道真正的训练门仍是：恢复 Gazebo 后采集中性 3-D 模型的连续 RGB+Depth+CameraInfo+
PX4 telemetry，独立审核轨迹，再用同一 r33/r66 契约做像素级增量微调。

## 2026-10-06：r64/r65 完成实体 3-D 几何课程增量训练，仍不替换 reference

这一轮按“先训练现有 OnboardAI，再接真实 Gazebo 像素采集”的顺序推进，没有把红车/绿车示例
当作场景或判断逻辑。`scripts/train_onboard_model.py` 的 3-D teacher 现在对仓库主线的三个实体
SDF/world 做笛卡尔覆盖：3 个场景 × 7 个实时事件 × 9 个任务阶段 × 5 个通用语义状态；
`TAKEOFF/SEARCH/INSPECT/MATCH/RECORD/RETURN/REPORT/HOLD/LAND` 都进入同一 40 维
OnboardAI 训练契约。新增回归测试确认每个场景、事件、阶段都有五个独立语义状态，颜色槽仍为
unknown，不读目标 pose、truth 或固定颜色。

在 RTX 4060 上从当前 r33 reference 训练 r64，随后从 r64 继续训练 r65。两轮都保持同一套
ONNX 输入、任务头、飞行动作头和 North/East/高度/速度输出；r65 额外使用有限 navigation
sign loss、bearing/range geometry loss 和 24,000 条左右对称锚点。r65 的两个独立 structured
3-D holdout（各 1,200 case）为 task=`0.988333`、flight=`0.980000/0.985833`、
compatible=`0.982500/0.991667`，高于同 seed 的 r33/r63 对照。

但 22-case 通用压力集仍有 `bearing-left` 失败（raw=`20/22`、guarded=`21/22`），而且当前
仍没有 training-ready 的 Gazebo RGB/Depth/CameraInfo/telemetry 连续 episode。因此 r65 只是
结构化 3-D 几何 candidate，`candidate_promoted=false`，r33 继续是 SIM 与未来 Jetson 的
唯一 reference；这不是 Gazebo 像素训练、Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终
`0.3.0` 证据。完整哈希和对照见
[`onboard-policy-r64-r65-3d-cartesian-20261006.json`](evidence/onboard-policy-r64-r65-3d-cartesian-20261006.json)。

下一步训练门不是继续堆颜色 fixture，而是：先解决左右 bearing 的独立泛化，再在 Docker/Gazebo
恢复后采集三类实体的真实 RGB+Depth+CameraInfo+PX4 telemetry 连续 episode，经过独立 review、
planner/Mission 证据和 pixel holdout 后，才允许把同一个 OnboardAI 做像素级增量微调。

## 2026-10-06：r63 通过结构化 OnboardAI 软件门，但不替换 r33 reference

本轮继续从 r33 增量训练同一套 OnboardAI，没有新建飞控模型，也没有把红车/绿车示例做成
Gazebo 判断。训练修复了 3-D 课程的场景、事件、生命周期阶段和通用感知状态耦合问题，加入
FOLLOW_TARGET 的 bearing/range 几何一致性辅助损失；输入仍是同一 40 维契约，输出仍是任务头、
飞行动作头和高层 North/East/高度/速度计划。RTX 4060 训练 93,200 行、30 epochs。

结果：独立结构化 holdout=`7/7`；900 条三类实体 3-D sensor-contract holdout 的 task/flight/
compatible=`0.997778/0.996667/0.998889`；22-case 泛化为 raw/guarded=`22/22`、方向和距离
=`22/22`；模型驱动链路继续产生 `search -> match_target -> record_location -> return_home
-> report`，两个运动计划均重新经过 planner 校验。证据见
[`onboard-policy-r63-geometry-lr-20261006.json`](evidence/onboard-policy-r63-geometry-lr-20261006.json)。

本轮又用同一 seed、同一三场景 manifest 和同一 `OnboardOnnxModelRunner` 对 r33/r63 做成对复测：
r33 的 900 条结构化 holdout 为 task/flight/compatible=`0.982222/0.990000/0.993333`，r63 为
`0.996667/0.993333/0.996667`；22-case 实时压力集的 guarded pass 从 `20/22` 提升到 `22/22`，
左右 bearing 和距离检查均为 `22/22`。这只是结构化软件 SIM 的训练决策证据，不是 Gazebo 像素
训练或硬件证据；成对回执见
[`evidence/onboard-policy-r33-r63-compare-20261006.json`](evidence/onboard-policy-r33-r63-compare-20261006.json)。

这仍然是确定性结构化软件 SIM 训练候选：`rendered_rgb_depth_rows=0`，没有外部 Gazebo/PX4
运行、Jetson、Pixhawk、HIL/REAL 或室外 GNSS 证据，所以 `candidate_promoted=false`，SIM 与
未来 Jetson 的当前 reference 仍是 r33。下一步不是继续堆颜色 fixture，而是恢复 Gazebo daemon，
用仓库里的三类实体 SDF/world 采集真实 RGB+Depth+CameraInfo+PX4 telemetry 连续 episode，再
用相同 r63 训练入口做像素级微调和独立轨迹审核。

## 2026-10-06：r52 3-D curriculum 候选完成训练，但拒绝晋升

本轮在 NVIDIA GeForce RTX 4060 上从 r33 继续增量训练 `onboard-policy-r52-3d-curriculum-dev`，
使用当前三类实体 SDF 场景、生命周期、左右 bearing 和中性目标 teacher，共 93,200 条样本、18
epochs，保持原有 40 维输入和任务/飞行动作输出，没有新建第二套飞控模型。独立结构化 holdout
为 `7/7`；三类 3-D sensor-contract holdout 为 task=`0.9967`、flight=`0.9944`、
compatible=`0.9978`。

但通用方向压力集仍在 `bearing-left` 失败（guarded=`21/22`），且当前没有任何 reviewed
pixel-level RGB/Depth episode，所以 r52 保持 quarantined，r33 仍是 SIM 与未来 Jetson 的
reference。它没有使用 Gazebo truth、红车/绿车颜色标签，也没有被写成外部 Gazebo、Jetson、
Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。完整证据见
[`onboard-policy-r52-3d-curriculum-rejection-20261006.json`](evidence/onboard-policy-r52-3d-curriculum-rejection-20261006.json)。

## 2026-10-06：当前 r33 OnboardAI 已跑通模型驱动的任务闭环（软件 SIM）

我用当前唯一 reference `onboard-policy-r33-balanced-bearing-3d-dev` 的 ONNX 权重，走了与
未来 Jetson 相同的 `OnboardOnnxModelRunner -> ModelBackedDecisionProvider ->
OnboardAIExecutor -> Mission planner` 链路。模型在连续事件中输出了两个真实的导航计划：先对
不匹配候选执行 `move_relative`，再对匹配目标执行 `follow_target`，两个计划都重新经过 planner
可行性校验，随后完成 `record_location -> return_home -> report` 生命周期。这里没有使用固定的
“红车/绿车”判断，也没有读取 `truth.json`；颜色只是用户举例，不是训练标签或 Gazebo 验证方法。

证据见 [`onboard-policy-chain-neutral-r33-20261006.json`](evidence/onboard-policy-chain-neutral-r33-20261006.json)，
源回放为 [`onboard-policy-chain-neutral-r33-20261006.json`](../artifacts/evaluation/onboard-policy-chain-neutral-r33-20261006.json)，
源 SHA-256=`e02f1841755964035e69840feb96d5f9a3c1e10fe4490f395232ac47b89439af`。这证明的是当前
模型驱动的确定性软件闭环，不是外部 Gazebo、PX4、Jetson、Pixhawk、HIL/REAL 或室外 GNSS
通过；下一步训练仍必须从实体 3-D 场景产生的 RGB+Depth+CameraInfo+telemetry episode 开始。

## 2026-10-06：重新验证 Linux Docker daemon，外部 Gazebo 仍未启动

我重新打包了当前实体 3-D simulation assets，并由 ClusterYourCodex 调度到在线 Linux
`nuc`。严格探针要求 `docker info` 成功才返回 0；本次 job 的 native `exit_code=104`，所以
Docker daemon 仍不可达，`gz/px4` 检查和 Gazebo/PX4 capture 没有继续执行。证据见
[`cluster-docker-strict-probe-20261006.json`](evidence/cluster-docker-strict-probe-20261006.json)。

这次只确认了外部运行时前置条件失败，没有生成 RGB、Depth、CameraInfo、Odometry、GPS 或
MAVLink episode，也没有改变 r33 reference、候选晋升或 release gate。Docker daemon 恢复后，
下一步仍是按当前 `gazebo-3d-mainline-v1` 运行三类实体场景，再做同一 OnboardAI 的像素级增量训练。

## 2026-10-06：外部 PX4 campaign 已接入共享 OnboardAI，运行时验收仍待真实环境

现有 `scripts/vtol_selected_task_campaign.py` 已把 `SharedTaskPolicy` 接到原有
`select_and_compile_task` 和 PX4 Mission upload/readback/commit/permit 生命周期中。每个 sortie
都会记录 policy artifact hash、ranker hash、planner 重验结果、Mission hash 和 policy evidence；
Prompt 解析必须与最终编译 Mission 完全一致，GPS fix 不合格时在进入 Mission transaction 之前
fail-closed，旧的 ranker-only 调用仍然保留。

验证已通过：`tests/test_vtol_sitl_mission_acceptance.py` 完整文件通过，专项的 shared-policy、
Prompt、airframe mismatch、GPS abstain 测试通过，Ruff 通过。证据见
[`shared-policy-external-campaign-integration-20261006.json`](evidence/shared-policy-external-campaign-integration-20261006.json)。
这仍是 fixture/编排层验证，不等于真实 PX4/Gazebo runtime、Jetson、Pixhawk、HIL/REAL 或室外
GNSS 已通过；下一步必须在可用 Docker/PX4/Gazebo 上运行同一 campaign 并保留完整 telemetry、
sensor、journal、cleanup 和 Mission evidence。

## 2026-10-06：训练预检已绑定实体 3-D 采集库存，仍未进入像素训练

`scripts/run_onboard_3d_training_preflight.py` 现在会直接调用
`scripts/audit_onboard_3d_capture_inventory.py`，在生成训练预检时逐份检查当前实体
`gazebo-3d-mainline-v1` 的 RGB manifest、实际 Gazebo payload、FLOAT32 Depth companion、
同帧 record hash 和 truth-free provenance。这样训练入口不会只看“有文件”或“有旧帧”，而是先
确认数据是否真的来自当前 3-D 场景。

新报告为
[`onboard-3d-training-preflight-20261006-r4.json`](../artifacts/evaluation/onboard-3d-training-preflight-20261006-r4.json)，
SHA-256=`10b70bf648cf536945a469e02a778e7d775129adaac4e3fa75f6ad8c8619d50b`。它扫描 122 份
RGB manifest，当前 `capture_eligible=0`、`training_ready=0`；因此本轮没有把历史 RGB、缺
Depth、过期场景或颜色例子送入 OnboardAI。当前 r33 reference 不变，外部 Gazebo、Jetson、
Pixhawk、HIL/REAL 和最终 `0.3.0` 仍未宣称通过。下一步只有在 Docker/PX4/Gazebo 可用后，采集
三类实体 3-D 场景的连续 RGB+Depth+telemetry，才进入同一 r33 训练入口的像素级微调。

## 2026-10-06：Docker Desktop 仍停在 socket 修复前，未启动 Gazebo

为了继续真实 3-D 采集，我启动了本机 Docker Desktop 并检查了后台日志。`desktop-linux`
pipe 仍不可用，`com.docker.backend` 的明确错误是无法把
`C:\Users\admin\AppData\Local\Docker\run\sailor-ingest.sock` 重命名为 `.stale`，两个残留
reparse-point socket 在当前权限下也无法移动或删除。没有执行 factory reset，没有删除镜像、容器、
WSL 数据或仓库文件；本轮由我启动的 Docker 进程已停止。证据见
[`docker-desktop-socket-preflight-20261006.json`](evidence/docker-desktop-socket-preflight-20261006.json)。

因此当前仍没有新的 Gazebo RGB/Depth/telemetry episode；恢复 Docker Desktop 后，直接运行预检
生成的三条实体 3-D 场景命令，再重新执行采集审计和 OnboardAI 像素级微调。

## 2026-10-06：先把 3-D 传感器数据审计干净，再训练 OnboardAI

历史目录里有 122 份 RGB manifest，但它们来自不同版本的场景、不同任务和不同深度批次。
新增 `scripts/audit_onboard_3d_capture_inventory.py` 做只读筛选：必须绑定当前实体 SDF
mainline 的场景哈希，有实际 Gazebo RGB payload、完整 FLOAT32 depth companion、同帧记录绑定，
并且明确声明运行时没有读取 truth。审计结果是 `capture_eligible=0`、`training_ready=0`；这
不是训练失败，而是把旧数据、缺深度的数据和过期场景主动挡在训练入口外。

审计报告：[`onboard-3d-capture-inventory-20261006.json`](../artifacts/evaluation/onboard-3d-capture-inventory-20261006.json)，
SHA-256=`895f54028793988aa978a796af7d29f8c845fefa675b4d55ddb8f55c545a22de`。因此当前不能用
红车/绿车示例、旧 RGB、结构化 teacher 行或手写标签继续冒充 OnboardAI 的 3-D 视觉训练；下一
个真实训练输入必须来自当前三个实体 3-D world 的起飞后 RGB/Depth/telemetry episode，随后再做
独立轨迹审核和 holdout。

## 2026-10-06：把 3-D 采集命令改成真正的起飞后传感器训练入口

本轮没有继续堆结构化 synthetic epoch。`scripts/run_onboard_3d_training_preflight.py` 现在为
每个实体 3-D 场景生成同一条可审计的 disposable SITL 命令：使用当前 r33 reference、现有
YOLO detector，执行 PX4 Mission upload/readback/commit/permit，显式 opt-in
`--execute-autonomous-flight`，通过起飞门后再保存真实 Gazebo RGB、FLOAT32 Depth、CameraInfo
和 wire-bound telemetry。它仍然不在 preflight 阶段启动 Docker、Gazebo 或 arm，也不会读取
`truth.json`，模型没有执行器权限。

实现与回归已通过 `tests/test_onboard_3d_training_preflight.py` 和 Ruff。重新生成的预检报告仍为
`blocked`：本机及 `nuc` 的 Docker CLI 可见，但 Docker daemon 不可达，因此本轮没有新增外部
Gazebo 帧，也没有把旧的地面/无 Depth 记录写成训练通过。证据见
[`onboard-3d-capture-command-upgrade-20261006.json`](evidence/onboard-3d-capture-command-upgrade-20261006.json)，
其内容明确保留 `training_ready=false`、`external_gazebo_runtime=false` 和
`candidate_promoted=false`。

## 2026-10-06：重新验证集群 3-D 采集前置条件，Docker daemon 仍未就绪

本轮没有重复使用旧快照，而是把当前工作树中源码、配置、仓库自有 SDF 场景/模型和 r33
reference 重新打包为 879 个文件，快照 SHA-256 为
`sha256:b5de134bf13a33b42ac16f593b8b1729080eb98ddd6f35582c327489ffa0eee1`，由控制器重新
选择在线 Linux 节点 `nuc`。节点能接收任务，但 Docker `info` probe 仍以退出码 `1` 在
daemon 连接处失败；后续捕获 stderr 的诊断任务本身执行成功，但没有生成可运行的 Gazebo
容器。因此这次仍然没有 RGB、Depth、PX4 telemetry 或外部 Gazebo runtime，不能写成 3-D
采集通过。

本机也重新确认 Docker Desktop 进程存在但 `desktop-linux` server pipe 不存在，WSL
`docker-desktop` 状态为 `Stopped`。最新证据见
[`onboard-3d-cluster-preflight-20261006-r2.json`](evidence/onboard-3d-cluster-preflight-20261006-r2.json)，
SHA-256=`6e9c0ddd2475a291f782636f39b078e33aeb0211e2e15af661a701894146a004`。下一步仍是恢复
一个实际可用的 Docker Linux daemon，然后按预检生成的三场景命令采集真实 RGB/Depth/telemetry；
训练 reference、r50 候选和 release gate 均没有被这次基础设施失败改写。

## 2026-10-06：r50 几何方向候选训练完成，仍不替换 r33

为了继续优先训练现有 OnboardAI，本轮从 r33 父权重直接做了 r50 增量训练，没有换网络、
没有增加第二套 Jetson 模型，也没有把固定策略接到执行器。r50 在 RTX 4060 CUDA 上训练
172,400 行、48 epochs，仍保持 40 维输入和原有任务/飞行动作输出；方向辅助损失只用于
稳定现有 N/E 导航回归，不改变 SIM/Jetson 接口。

结果：7/7 独立结构化 holdout；22/22 通用 guarded、22/22 导航方向、22/22 距离边界；
900 行三类实体 3-D manifest 结构化传感器 holdout 为 task/flight/compatible
`0.9867/0.9856/0.9833`。这是相对 r48/r49 的方向回归改善，但 5 个案例依靠 compatibility
或阶段安全 mask 修正，且训练仍是结构化 teacher，`rendered_rgb_depth_rows=0`，没有真实
Gazebo 像素、连续 episode、Jetson、Pixhawk 或室外 GNSS 证据，所以 r50 不 promotion，
r33 继续是 SIM 与未来 Jetson 的唯一 reference。

模型、训练配置、训练报告和三份评估的 hash 绑定见
[`onboard-policy-r50-geometry-direction-candidate-20261006.json`](evidence/onboard-policy-r50-geometry-direction-candidate-20261006.json)，
SHA-256=`22cbbe958eae3977dc633444423fa6fac6408c29497b8ae2501b9e60b168e3c9`。下一步不是
继续堆合成 epoch，而是恢复可审计的 Gazebo/PX4 3-D runtime，采集 RGB、FLOAT32 Depth、
CameraInfo、姿态、GPS/telemetry 和独立审核轨迹，再从同一 r33 接受真实像素级微调。

## 2026-10-06：修正 OnboardAI 3-D 教师几何，停止把示例当作 Gazebo 验证

本轮没有继续盲目增加合成训练轮次。审查发现 r46/r47 的结构化增量样本把随机 bearing
和固定 N/E 回归混在一起，中性目标样本的距离特征也与位移标签不一致；两者都曾在
`bearing-right` 退化，因此没有 promotion。现已修正 `scripts/train_onboard_model.py`：
中性目标与 `FOLLOW_TARGET` phase 样本统一使用“距离特征 → bearing → N/E”几何，bearing
锚点改为多角度且每条样本独立分组。训练回归已通过 `tests/test_train_onboard_model.py`。

同时修正 `scripts/build_onboard_rendered_3d_dataset.py`。真正的 Gazebo RGB/Depth 帧现在
必须通过 CameraInfo/相机标定、同帧机体姿态、目标框中心和米制 Depth，复用现有
`_target_rays`/相机杆臂变换生成 NED 标签；不再写死 `north=0.06/east=±0.18`。缺少标定或
深度时只保留 plumbing 行并标记原因，不能冒充可训练的 3-D 几何。新增测试覆盖 bearing、
距离、N/E 符号与标定维度。

因此当前唯一引用仍是 r33；它和后续候选都是同一套 OnboardAI 40 维契约，尚未用真实
Gazebo RGB/Depth/telemetry 完成微调。当前预检仍显示主线数据只有 25 行、0 个有效 Depth
和 0 个 detector hit，Docker/PX4/Gazebo 外部运行时也未恢复。下一步是取得三类实体 3-D
场景的真实连续传感器帧和独立审核轨迹后，再从 r33 做增量训练；没有这些输入不替换模型。

## 2026-10-06：r48/r49 几何标签修复后候选训练完成，仍不替换 r33

在 4060 CUDA 上从当前 r33 继续训练了两轮，不改变 40 维输入、20 维输出或 SIM/Jetson
共享入口。r48 先使用修复后的距离→bearing→N/E 教师和三类 Gazebo 场景结构化行；r49
再提高 bearing 符号样本权重。两轮都通过独立结构化 holdout `7/7`，900 行三类场景
结构化传感器 holdout 分别为 r48 `0.9833/0.9756/0.9844`、r49 `0.9933/0.9900/0.9900`
（task/flight/compatible）。通用 22 案例评估仍各有 `bearing-left` 失败，且这些行不是
真实 Gazebo RGB/Depth 像素；因此候选都拒绝 promotion，r33 继续作为唯一 reference。

完整比较、模型哈希和边界见
[`onboard-policy-r48-r49-geometry-label-candidate-comparison-20261006.json`](evidence/onboard-policy-r48-r49-geometry-label-candidate-comparison-20261006.json)。
这一步证明的是“增量训练链和结构化契约可运行”，不是实飞、Jetson、HIL、REAL 或外部
Gazebo 通过。下一步仍是先恢复真实 3-D 采集，再把 RGB、FLOAT32 Depth、CameraInfo、姿态、
GPS 和审核轨迹送入同一套增量训练入口。

## 2026-10-06：中性 3-D 目标接入共享 OnboardAI，r46/r47 候选均未 promotion

本轮修正了一个会把示例误当成产品约束的问题：RGB/Depth 适配器原先默认
`target_color="red"`，现在颜色是可选任务属性；默认实体 3-D 目标只按类别、连续跟踪、
metric depth、GPS/安全状态判断，颜色不约束。实时搜索去重逻辑也已修复：类别匹配且颜色
未知的目标不会被当成上一帧“搜索重复”而吞掉。对应回归覆盖了
`tests/test_perception_adapter.py`、`tests/test_onboard_realtime_bridge.py`，默认软件回放已验证
`search → match_target → record_location → return_home → report`，r33 仍为当前 reference：
[`onboard-policy-chain-neutral-r33.json`](../artifacts/evaluation/onboard-policy-chain-neutral-r33.json)
（SHA-256=`bb8cbb7a117b58f821d0a3e0a8a062a8d8ab05be8ffca8ffe086544256b88320`）。

为优先训练现有 OnboardAI，本轮在 RTX 4060 上从 r33 父权重训练了两个隔离候选：r46 加入
“类别匹配、颜色不约束”的中性目标转移样本，3-D 结构化 holdout 为 task/flight/兼容输出
`0.9800/0.9825/0.9850`；r47 提高 bearing 样本权重后为 `0.9875/0.9850/0.9900`。但两者的
独立泛化都仍在 `bearing-right` 失败（`21/22`），所以 r46/r47 均拒绝，当前引用不变为 r33。
这些训练仍是结构化 teacher，不是 Gazebo 像素训练；真正的 RGB/Depth/telemetry 采集仍要等
Docker/PX4/Gazebo 外部运行时恢复后进行。候选训练报告和评估文件保留在各自
`artifacts/models/onboard-policy-r46-neutral-target-3d-dev/`、
`artifacts/models/onboard-policy-r47-neutral-bearing-3d-dev/` 与 `artifacts/evaluation/`，
没有 promotion、Jetson、Pixhawk、HIL/REAL 或最终发布声明。

## 2026-10-06：Linux 集群 Docker/PX4 3-D 外部 preflight 仍在容器前阻塞

为绕开本机 Windows Docker Linux daemon 不可达的问题，本轮把当前脏工作树中必要的源码、
三套实体 3-D 场景、当前 r33 reference 和运行脚本打成受限快照，上传到在线 Linux `nuc`。
快照 SHA-256 为
`e08189f1cae400bbb3da3e7474a7964b822b4d32fd880ea58d205215410a462a`，共 519 个文件、约 29 MB。

集群控制器成功选择 `nuc`，但只读 `docker info` probe 返回退出码 `1`，实际外部采集作业也在
容器创建前退出、没有 artifact、没有 PX4 telemetry、RGB 或 Depth 帧。因此这次没有 Gazebo
运行结果，不能写成外部 SIM 通过；证据只记录“集群 Docker runtime 未就绪”：
[`onboard-3d-cluster-preflight-20261006.json`](evidence/onboard-3d-cluster-preflight-20261006.json)。
当前可继续的下一步是修复 `nuc` Docker daemon 或恢复一台在线、允许 Container job 的 GPU 节点，
然后重新执行同一快照的实际 3-D 采集门。

## 2026-10-06：统一 OnboardAI 默认入口到当前 r33 reference

发现两个用户可直接调用的入口仍把历史 `onboard-policy-r4` 写成默认身份：
`scripts/run_onboard_policy_chain.py` 和 `scripts/run_external_onboard_model_acceptance.py`。
本轮没有替换模型或改变 40 维契约，只把无参数路径改为自动加载并逐字节校验
`configs/training/onboard-policy-current-reference-v1.json`，当前实际使用
`onboard-policy-r33-balanced-bearing-3d-dev`，模型 SHA-256 为
`727d0e71834311d8aa51dfe072e08ec249062b1945d9501394f5d89ad7a16ee6`。

软件链默认运行已生成
`artifacts/evaluation/onboard-policy-chain-current-reference-default-20261006.json`
（SHA-256=`fc6d7fbdf4ea1c0e332b33657339b694a88eb3886408ef17d37661856f7ff9b3`）；
外部 runner 默认仅生成
`artifacts/evaluation/external-onboard-model-current-reference-api-default-20261006.json`
（SHA-256=`ac8f5ad46569379629f5d8cee867e9a0c7364076e1964051e64813ae4aef6db3`），状态为
`planned_not_executed`。本机 Docker daemon 仍不可达，
所以这一步只证明模型身份一致性，不是外部 Gazebo、Jetson、Pixhawk、HIL/REAL 或室外 GNSS 通过。

## 2026-10-06：r45 低权重实体几何候选（拒绝，r33 保持）

为验证 r41–r44 的退化是否只是几何样本和 bearing 锚点过重，本轮回到 r33 的增量比例：
实体 SDF 几何教师 3,000 行、bearing 锚点 6,000 行、学习率 `0.0015`、回归损失 `0.20`，
仍使用同一 40 维 OnboardAI 契约。r45 的独立语义保持集为 `7/7`，但通用泛化仍为 `21/22`，
在 `bearing-right` 失败。这个结果表明仅调整结构化教师权重不能解决方向泛化；r45 未 promotion，
当前引用继续为 r33。证据见
[`onboard-policy-r45-geometry-low-weight-rejection-20261006.json`](evidence/onboard-policy-r45-geometry-low-weight-rejection-20261006.json)。

至此，继续搜索合成结构化超参数的收益已经被验证为不足。下一步必须获得真实 3-D Gazebo/PX4
连续 RGB、FLOAT32 Depth、CameraInfo、Odometry、GPS/飞控 telemetry 和独立审核轨迹，再从同一
r33 父模型做像素级增量训练；没有这些输入，不再把新候选接入当前引用。

## 2026-10-06：实体 3-D 几何教师候选 r41/r42/r43/r44（全部拒绝，r33 保持）

这四轮仍然是同一套 OnboardAI 的增量训练，不是另起一套飞控模型，也没有把红车/绿车示例当作
Gazebo 场景。训练只读取主线实体 SDF 的部件数量、尺度和高度统计，保持原来的 40 维输入、
任务头、飞行动作头和安全/PX4 Mission 边界；本轮还把导航符号损失作为可选项做了两组对照。

四个候选的独立语义保持集都是 `7/7`，但 22 个 truth-free 泛化用例都只通过 `21/22`：r41、
r42、r43 在 `bearing-left` 失败，r44 在 `bearing-right` 失败。r42/r43 的符号损失没有消除
方向错误且使回归误差变差；r44 延长训练后出现结构化数据过拟合，评估飞行准确率下降。因而
没有候选达到现有导航方向和生命周期门槛，当前引用仍是 r33，四个候选全部
`candidate_promoted=false`。完整 hash 绑定见
[`onboard-policy-r41-r44-geometry-bearing-candidate-comparison-20261006.json`](evidence/onboard-policy-r41-r44-geometry-bearing-candidate-comparison-20261006.json)。

这个结果说明继续堆叠合成结构化样本已经不能可靠解决方向泛化。下一步必须回到真实 3-D
Gazebo/PX4 运行时，采集连续 RGB、FLOAT32 Depth、CameraInfo、Odometry、GPS/飞控 telemetry
和独立审核轨迹，再用同一 r33 父模型做像素级增量训练。当前仍未宣称外部 Gazebo、Jetson、
Pixhawk、HIL/REAL、室外 GNSS、实飞或最终 `0.3.0` 通过。

## 2026-10-06：实体 SDF 几何教师训练 r38/r39/r40（全部拒绝，r33 保持）

本轮继续优先训练现有 OnboardAI，没有启动或伪造 Gazebo 运行。训练器现在从主线实体目标
`simulation/gazebo/models/multidetect_search_target_v1/model.sdf` 读取可复现的结构统计（部件数、
径向尺寸和高度），只用来生成更接近 3-D 视场的观测尺度/距离分布；它不读取目标世界坐标、
truth、颜色或期望答案，仍然使用原来的 40 维 `OnboardModelInput` 和 r33 父权重。

在 RTX 4060 上完成了三轮增量候选：r38 的结构化 holdout 为 task/flight/兼容输出
`0.990333/0.988000/0.992000`，但 `bearing-left` 方向失败；r39 增加左右对称锚点后为
`0.993000/0.991333/0.994333`，但换成 `bearing-right` 失败；r40 提高几何回归损失后 holdout
为 `0.996000/0.996000/0.996667`，但通用评估仍 `bearing-left` 失败，训练 evaluation 的
flight accuracy 下降到 `0.8999907313`。三轮全部 `candidate_promoted=false`，当前引用继续是
r33。完整比较见
[`onboard-policy-r38-r40-geometry-candidate-comparison-20261006.json`](evidence/onboard-policy-r38-r40-geometry-candidate-comparison-20261006.json)。

这轮结果说明训练门挡住了一个真实风险：平均分类准确率上升不能覆盖左右导航符号不稳定。
下一轮不能继续盲目加 epoch；必须有实际 Gazebo RGB/Depth/telemetry 连续帧和独立轨迹标签，
再在同一模型上做像素级微调。当前仍未宣称外部 Gazebo、Jetson、Pixhawk、HIL/REAL、室外 GNSS、
实飞或最终 `0.3.0` 通过。

## 2026-10-06：OnboardAI r37 3-D 模型教师增量训练（未 promotion）

r37 从当前 r33 父权重继续训练，保持同一 40 维输入、任务/飞行动作头和安全/PX4 Mission
边界。训练绑定三类实体 3-D world/model/physics/sensor manifest，不把红车/绿车例子当场景。
RTX 4060 完成 57,700 行、35 epochs；3,000 行独立结构化 sensor-contract holdout 的 task=
`0.979333`、flight=`0.984333`、兼容输出=`0.993333`；通用回归 raw=`19/22`、guarded=`22/22`。
r36 的 `bearing-right` 回归失败，所以拒绝。证据见
[`onboard-policy-r37-3d-model-teacher-20261006.json`](evidence/onboard-policy-r37-3d-model-teacher-20261006.json)。

这个候选仍没有真实渲染行、审核轨迹或独立 pixel test，不能称为 Gazebo 像素级训练成功。
当前引用仍保留 r33；r37 未 promotion，外部 Gazebo、Jetson/Pixhawk、HIL/REAL、室外 GNSS、
实飞和最终 `0.3.0` 均未宣称通过。下一步需要实际采集三类 3-D 场景的连续 RGB+Depth+telemetry。

## 当前模型引用与 3-D 训练边界（2026-10-06）

SIM、4060 增量训练和未来 Jetson 接入现在统一使用
[`configs/training/onboard-policy-current-reference-v1.json`](../configs/training/onboard-policy-current-reference-v1.json)
作为显式入口。它选择 r33，并校验 ONNX、训练配置、训练报告和
[`gazebo-3d-mainline-v1`](../configs/scenarios/gazebo-3d-mainline-v1.manifest.json) 的 SHA-256；
历史 r4 只保留给旧证据，不再作为新流程的隐式模型身份。

本引用仍是 `development_reference_not_promoted`。当前 r33 的训练仍没有 reviewed RGB/Depth
episode、pixel-level test 或外部 Gazebo runtime；本机 Docker Linux daemon 不可达。因此这一步
只解决模型身份一致性和训练入口一致性，不宣称 Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终
`0.3.0` 已通过。

## 2026-10-06：实体 3-D Gazebo 训练场预检（运行时阻塞，未训练）

新增 `scripts/run_onboard_3d_training_preflight.py`，把当前模型引用、三套实体 3-D world/model
manifest、Docker/Gazebo/PX4 前置条件和后续 RGB/Depth/telemetry 采集命令放进一个可复现报告。
它不读取 `truth.json`，不使用红车/绿车颜色示例，不启动 Docker/Gazebo/PX4，也不生成飞行命令。

本机结果为 `status=blocked`：Docker Linux daemon 不可达，`gz/gazebo/px4` 也不在 PATH；
因此目前只能证明仓库 3-D 资产审计和 r33 hash-bound reference 通过，不能声称外部 Gazebo 已运行。
报告为
[`docs/evidence/onboard-3d-training-preflight-20261006.json`](evidence/onboard-3d-training-preflight-20261006.json)，
SHA-256=`04517f7c4a31bcb28538d86803b9e2295c40461f208cc6ebeb23e790307b829f`。报告同时盘点了
仓库内已有的渲染数据和轨迹暂存：当前最大主线数据只有 25 行、0 个有效 Depth 行、0 个
detector hit；轨迹暂存只有 3 个 episode/9 个 transition、`recording_bound=false`，所以它们仍
不能进入训练。它明确保持
`training_ready=false`；下一步是启动可审计的 Gazebo/PX4 运行时后采集三类实体场景的连续 RGB、
Depth、CameraInfo、Odometry、GPS/飞控 telemetry，生成独立审核轨迹，再用同一 r33 父模型做增量训练。

本轮重新验证了外部运行时：Controller 重新选择在线 Linux `nuc`，最小快照成功上传，随后
容器内 `docker info` 原生退出码仍为 `1`；第一次 Git 源码准备失败的退出码 `128` 已与
daemon 探针分开记录。Windows 本机 Docker Desktop 启动时还因为残留
`sailor-ingest.sock` 无法重命名而后端崩溃。证据见
[`evidence/cluster-docker-strict-probe-20261006-r2.json`](evidence/cluster-docker-strict-probe-20261006-r2.json)。
因此这次只证明运行时前置条件仍未满足，没有启动 Gazebo/PX4、没有生成 RGB/Depth episode，
也没有改变当前 r33 reference。

为避免预检、数据集构建和训练命令分散造成误用，新增统一入口
`scripts/run_onboard_3d_training_pipeline.py`。不带 `--train` 时它只写当前父模型、3-D
manifest、检测器、采集库存和阻断原因；带 `--train` 时必须同时提供真实 RGB/Depth 数据集与
recording-bound 审核轨迹，并由既有训练器从当前 reference 增量微调。它不会启动 Gazebo、读取
场景真值或自动 promotion。当前已运行 preflight-only 回执
`../artifacts/evaluation/onboard-3d-training-pipeline-20261006-r2.json`，状态仍为
`preflight_only`，主线合格 capture 为 `0`。

# 2026-10-06：OnboardAI r33 生命周期与 bearing 几何增量训练（未 promotion）

本轮把重点放在现有 OnboardAI 模型训练，而不是用红车/绿车示例充当 Gazebo 验证。r33 从 r32
继续增量训练，保持同一 40 维输入、同一任务/飞行动作头、同一安全适配器和同一 PX4 Mission
边界；训练入口继续绑定仓库的多旋翼、VTOL、固定翼 3-D Gazebo world/model manifest。

本轮修正了两个训练根因：一是匹配目标的东向回归此前在部分 temporal/semantic teacher rows
中被固定成正值，导致左右 bearing 被压成同一方向；二是 phase augmentation 实际只生成了
TAKEOFF。现在阶段样本覆盖 TAKEOFF、SEARCH、INSPECT、MATCH、RECORD、RETURN、REPORT、HOLD、
LAND，并增加了按目标距离与 bearing 计算北/东位移的对称几何锚点。锚点按混合 episode group
切分，避免左侧或右侧整组只落到 train/evaluation。

训练在本机 RTX 4060 完成：45,600 行、50 epochs，模型
[`artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx`](../artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx)，
SHA-256=`727d0e71834311d8aa51dfe072e08ec249062b1945d9501394f5d89ad7a16ee6`。固定 22 个
truth-free generalization case 为 raw=`19/22`、guarded=`22/22`、导航方向=`22/22`；900 行
独立结构化 3-D sensor-contract holdout 为 task=`0.976667`、flight=`0.977778`、兼容输出
`0.994444`。完整证据见
[`docs/evidence/onboard-policy-r33-balanced-bearing-3d-20261006.json`](evidence/onboard-policy-r33-balanced-bearing-3d-20261006.json)。

这些结果仍不是像素级视觉训练成绩：r33 训练源中 rendered sensor rows=`0`、reviewed trajectory
rows=`0`、test split=`0`。仓库中的 Gazebo 3-D manifest 绑定了真实 world/model 资产，但本轮
没有启动外部 Gazebo/PX4，当前 Docker Linux daemon 也不可达。因此 r33 保持
`candidate_promoted=false`、`final_release_allowed=false`，不能宣称 Jetson/Pixhawk、HIL/REAL、
室外 GNSS 或实飞性能通过。下一步必须先采集当前 manifest 对应的 RGB/Depth/CameraInfo/Odom/GPS
连续 episode，经过独立标签审核后，才进入同一训练入口的像素级微调。

本轮同时修复了 `scripts/build_onboard_rendered_3d_dataset.py` 的仓库根目录导入路径，直接从
仓库根目录执行该入口现在可以正常启动。对保留的旧 RGB wire-v3 采集执行当前 manifest 校验时，
入口明确因 `scenario_manifest_hash` 不在当前批准主线中而 fail-closed；没有把旧帧重新标成当前
3-D 训练数据，也没有改写任何历史采集证据。

# 2026-10-06：OnboardAI r27 中性 3-D 目标语义增量训练（未 promotion）

本轮先把训练重点放回 OnboardAI，而不是用红车/绿车夹具做 Gazebo 验证。r27 从 r26 的同一
40 维 OnboardAI/ONNX 权重继续训练，使用仓库三类中性 3-D 场景 teacher；3-D teacher 的颜色
输入保持未知，目标确认依赖类别/跟踪一致性、通用匹配分数、深度和实时事件。模型输出仍必须
经过现有 planner、PX4 Mission 编译和安全适配器，不能直接拥有执行器权限。

模型文件为
[`artifacts/models/onboard-policy-r27-gazebo3d-generic-dev/onboard_policy_r27.onnx`](../artifacts/models/onboard-policy-r27-gazebo3d-generic-dev/onboard_policy_r27.onnx)，
SHA-256=`c84272bd70877b3d6bf75c05c1fdb5fa7a151d358a3740be32b9a721614bb0b9`。训练在 RTX 4060
上完成 35,100 行、45 epochs；evaluation task/flight=`0.873445/0.880355`。中性 3-D holdout
和通用独立 holdout 都是 `7/7`，但 test split=`0`，所以这只是结构化策略回归，不能当作
像素级视觉准确率或真实飞行证据。随后使用同一个 ONNX runtime 跑了 22 个 truth-free
generalization pressure case：原始模型头通过 15/22，安全 runner 保护后的结果通过 22/22，
其中 7 个是安全屏蔽成功而不是模型学习；class/color 字符串不改变输出。完整绑定见
[`docs/evidence/onboard-policy-r27-generic-3d-20261006.json`](evidence/onboard-policy-r27-generic-3d-20261006.json)。

本机 Docker daemon 当前不可达，尚未启动外部 Gazebo/PX4；尚未取得足量真实 RGB/Depth、有效
detector/depth、审核连续 episode 或独立 pixel test。r27 不替换参考模型，仍保持
`candidate_promoted=false`、`jetson_hardware_evidence=false`、`pixhawk_hardware_evidence=false`、
`hil_real_evidence=false`、`outdoor_gnss_evidence=false`、`final_release_allowed=false`。

# 2026-10-05：主线 3-D 目标模型接入（训练资产，runtime 未宣称）

三类主线 Gazebo world 现在绑定同一个仓库自有的
`multidetect_search_target_v1` 实体模型。它不是红车/绿车夹具，而是有底盘、座舱、轮组、
桅杆、传感器头、碰撞体和多个深度层次的中性结构目标；每个场景的模型文件、配置、include
名称和放置位姿都由 mainline manifest 的 SHA-256 固定。OnboardAI 运行时仍只能读取 RGB、
Depth、GPS/飞控 telemetry，不能读取模型 ID、世界真值或目标坐标。

已通过离线 XML/资产哈希/stack-plan 审计；尚未启动外部 Gazebo、尚未生成新的真实 RGB+Depth
训练集，也没有宣称 Jetson、Pixhawk、HIL/REAL 或室外 GNSS 结果。下一步仍是用这三个场景
采集足量连续帧和审核轨迹，再在 RTX 4060 上从现有父模型增量微调。

# 2026-10-05：真实 3-D OnboardAI 训练入口收紧（当前未通过）

`scripts/run_vtol_mission_sensor_capture.py` 现在支持显式的 `--depth-output-directory`，会把同一
PX4/Gazebo 任务生命周期内的 RGB wire-v3 与米制 `FLOAT32_M` Depth 写入成对目录，并保留任务屏障、
GPS、时间、机体和 exact-container 绑定。`scripts/train_onboard_model.py` 新增
`--require-real-3d-capture`，训练前必须检查至少 100 个唯一连续帧、至少两个实体 3-D 场景、有效
深度、真实 detector 输出、recording-bound episode 和独立 planner/Mission 复核。

对当前仓库数据执行该门得到预期的 fail-closed：只有 9 帧、0 个有效深度、0 个 detector hit、
暂存轨迹未 training-ready 且未绑定 recording。因此本轮没有把新的 r27 权重标成真实 3-D 像素训练；
这是防止把红车/绿车示例、结构化 teacher rows 或重复帧权重冒充 3-D OnboardAI 视觉训练。Docker/Gazebo 没有在本轮重新启动，
Jetson/Pixhawk、HIL/REAL、室外 GNSS 和最终 `0.3.0` 仍未宣称。

## 2026-10-05：OnboardAI r26 真实 3-D 场景契约增量训练（未 promotion）

r26 从现有 r25 权重继续微调，仍使用 Jetson/Pixhawk 运行时共用的 40 维观测、同一任务头和同一
飞行动作头；没有另起飞控模型，也没有把红车/绿车示例当作 Gazebo 世界定义。训练使用仓库三类
3-D vehicle/world 场景契约，并接入已持久化的 RGB+Depth 传感器记录。模型为
[`artifacts/models/onboard-policy-r26-gazebo3d-scene-dev/onboard_policy_r26.onnx`](../artifacts/models/onboard-policy-r26-gazebo3d-scene-dev/onboard_policy_r26.onnx)，
RTX 4060 训练完成，34,344 条混合行中 12,000 条来自结构化 3-D scene teacher，渲染源只有 9 个
唯一帧，重复 16 倍只作为受控权重。

独立结构化传感器保持集为 2,100 个未参与训练的 case：task=`0.746190`、flight=`0.779524`、
compatible=`0.950000`。渲染源 detector hits=0、可用深度中值=0、没有审核连续轨迹和独立 pixel
test，因此 r26 仍 quarantined，不替换参考权重。训练报告和边界证据见
[`docs/evidence/onboard-policy-r26-gazebo3d-scene-20261005.json`](evidence/onboard-policy-r26-gazebo3d-scene-20261005.json)。
这一步是本机 GPU 的同模型增量训练与 3-D 传感器契约验证，不是新一轮外部 Gazebo/PX4 运行，也不
宣称 Jetson、Pixhawk、HIL、REAL、室外 GNSS 或最终 `0.3.0`。

## 2026-10-05：真实 Gazebo 3-D 连续轨迹暂存层（未进入训练）

本轮没有继续用红车/绿车示例当作 Gazebo 验证。新增
[`scripts/build_onboard_trajectory_staging.py`](../scripts/build_onboard_trajectory_staging.py)，直接读取
持久化 Gazebo wire-v3 RGB、Depth 和同帧遥测，把每个采集目录组织成一个连续 episode，并为每帧重新
构造共享的 `ContextSnapshot + OnboardRuntimeEvent + 40 维 OnboardModelInput`。相邻帧通过
`next_frame_id` 绑定，RGB/Depth payload、GPS、场景和机体身份都保留 SHA-256。

当前生成的暂存文件为
[`artifacts/training/onboard-trajectory-3d-staging-20261005.json`](../artifacts/training/onboard-trajectory-3d-staging-20261005.json)：
3 个真实 3-D Gazebo 采集 episode、9 个连续 RGB+Depth transition，schema 校验通过。它明确标记
`training_ready=false`、`review_required=true`、`recording_bound=false`；当前采集缺少完整电量、风场、
geofence 等任务上下文，也没有独立 review receipt、planner/Mission label proof，所以没有喂给生产
模型，不能写成 OnboardAI 视觉训练完成。检测器只作为可选的同一感知边界，未写入世界坐标或场景 truth。

暂存层现在会拒绝时间倒退/重复帧、缺失 Depth、RGB/Depth 不匹配和 `target_world_position` 等真值字段。
下一批 Gazebo 采集必须由完整 3-D world 连续运行产生 RGB、Depth、PX4 telemetry、任务阶段、目标跟踪和
raw replay recording，完成独立审查后才可转换为 `training_ready=true`，再使用 RTX 4060 做同一父模型的增量微调。

## 2026-10-06：r34/r35 增量训练复核（均未 promotion）

在 r33 之上继续使用同一 40 维 OnboardAI 契约、同一三场景 Gazebo 3-D manifest 和 RTX 4060 做了
两轮候选训练。r34 的 900-case 结构化 3-D holdout 为 task=`0.983333`、flight=`0.993333`、
compatible=`0.996667`，但固定导航压力集的 `bearing-right` 失败；更保守学习率的 r35 在
`bearing-left` 失败。两轮都因此拒绝 promotion，当前参考保持 r33（左右 bearing 均通过）。

这不是 Gazebo 视觉验证：两轮都没有 reviewed RGB/Depth episode、pixel-level test 或外部 Gazebo
重跑。完整模型、报告、SHA 和选择理由见
[`docs/evidence/onboard-policy-r34-r35-rejection-20261006.json`](evidence/onboard-policy-r34-r35-rejection-20261006.json)。
下一步仍是从当前实体 3-D world 重新采集足量 RGB/Depth/telemetry 连续 episode，完成独立标签审查后
再训练，而不是把更高的结构化分数直接当成视觉能力。

本轮 r33 训练器、bearing 几何 teacher、生命周期兼容性、当前模型引用和引用校验器更新后，当前 parity
文件为 13,598 bytes，SHA-256 为 `634f1b357908de3c6dc083d19e7708480ebcb7ef8394efd2a3735793a08dad41`，
core hash 为 `7ceeceeef9ebe917e4464c3647abce384df8acdb268d89c7e937e7c2dfffc9df`。这是当前源码身份绑定，
不会把历史外部运行重新声明为新运行。

## 2026-10-05：r24 同一 OnboardAI 的 3-D 多场景增量微调（未 promotion）

`r24` 继续继承 r23 的同一 40 维 OnboardAI/ONNX 权重，在 RTX 4060 上使用 12,944 条按 source
group 切分的样本完成 30 epochs 微调。模型为
`artifacts/models/onboard-policy-r24-gazebo-multiscene-dev/onboard_policy_r24.onnx`，SHA-256=
`c764e08a46208802315898b853404b6455a6f99995a09b438c2f5be31f42f8ae`；独立结构化 holdout
task/flight=`0.718333/0.800000`，相同 seed 的 r23 为 `0.676667/0.777500`，兼容输出率为
`0.970833`（r23=`0.949167`）。这只是结构化契约改善，holdout 尚不是像素级渲染泛化证据。

当前真实 RGB+Depth 训练帧仍只有 9 帧、detector hits=0、标签为
`sensor_contract_heuristic_plumbing_only`，所以 r24 不替换 r17、不 promotion。训练候选证据见
[`docs/evidence/onboard-policy-r24-gazebo-multiscene-dev-20261005.json`](evidence/onboard-policy-r24-gazebo-multiscene-dev-20261005.json)。

训练入口现在不会丢弃连续 episode 的数据集分区：reviewed trajectory 的 `train/evaluation/test`
由 episode 声明固定，test 单独保留且不进入训练或 evaluation；混合的 synthetic/plumbing 行只能
进入 train。trajectory group identity 已改为校验后的 `episode_group_sha256`，training-ready
数据若没有可复现的 `materialized_sources=true` RGB/Depth manifest 会 fail-closed。下一轮必须
使用正式 review receipt 和真实连续 3-D 录制，再做独立像素级 test；当前不把这项管线修复写成模型
性能通过。

训练 loader 新增 `materialized_sources=true` 的严格入口：审核数据必须真正打开 RGB/Depth manifest，
验证 manifest/frame/时间戳以及 payload SHA；无法复现原始 3-D 录制时 fail-closed。当前数据尚未满足
training-ready，因此不能把本轮称为视觉训练完成，也不能宣称外部 Gazebo、Jetson、Pixhawk、HIL、
REAL、室外 GNSS 或最终 `0.3.0`。

## 2026-10-05：r23 RGB+Depth 3-D OnboardAI plumbing 微调（未 promotion）

本轮先修训练可信度，再做模型增量训练。`scripts/build_onboard_rendered_3d_dataset.py` 现在会校验每帧
RGB 与 Depth 的 SHA-256、同帧 telemetry 时间戳和 source split group；`scripts/train_onboard_model.py`
按 source group 切分训练/评估，重复帧不能跨集合泄漏。新数据集
`artifacts/training/onboard-rendered-3d-depth-v2.json` 来自 3 个实体 Gazebo 3-D VTOL 传感器采集，
共 9 个 RGB+Depth 帧、7 个独立 split group，深度 companion 全部绑定成功。检测器在这批帧上没有命中，
标签因此仍是 `sensor_contract_heuristic_plumbing_only`，不是人工审核目标标签。

在同一个 r17 ONNX parent 上使用 RTX 4060、学习率 `0.0003`、40 epochs 做了 r23 微调：
`artifacts/models/onboard-policy-r23-3d-depth-plumbing/onboard_policy_r23.onnx`，SHA-256=
`69da862e02fb7757135e433c54e45f16aaeb48054ecd85fd3a930402a4af8af5`。同 seed 的结构化 3-D holdout
中，r17 的 task/flight 为 `0.574444/0.705556`，r23 为 `0.678889/0.778889`；这只是结构化契约回归改善，
不是视觉泛化或真实飞行证据。r23 明确不替换 r17、不 promotion。完整记录见
[`docs/evidence/onboard-policy-r23-3d-depth-plumbing-20261005.json`](evidence/onboard-policy-r23-3d-depth-plumbing-20261005.json)。

当前仍缺少：人工审核的连续任务标签、目标匹配/跟踪/记录位置/返航转移、独立 rendered-pixel holdout、
以及可用的外部 Gazebo 重跑。因此不能宣称 Jetson、Pixhawk、HIL、REAL、室外 GNSS 或最终 `0.3.0`。

## 2026-10-05：r22 接入真实 Gazebo 3-D 渲染帧的 OnboardAI 微调（未 promotion）

本轮把已经持久化的实体 3-D Gazebo RGB 帧和同帧 PX4 telemetry 接入现有训练入口，新增
`scripts/build_onboard_rendered_3d_dataset.py` 和 `--rendered-3d-dataset`。数据来自 x500
多旋翼、standard VTOL、rc_cessna 固定翼三种主线 world；构建器逐帧校验 RGB SHA/尺寸，只使用
像素、检测器输出、相机几何和 telemetry，不打开 `truth.json`，没有把目标真值位置写进 40 维向量。
25 条帧经同一 COCO ONNX detector 后均未产生可用目标框，所以标签是实际观测到的
`target_lost → search`，不是红车/绿车规则。

r22 从 r17 的同一 ONNX 权重继续微调，使用 RTX 4060、37,100 条混合样本（其中渲染帧重复权重
2,000 条），模型为
`artifacts/models/onboard-policy-r22-rendered3d-finetune/onboard_policy_r22.onnx`，SHA-256=`e016bd5df23526a090ec146d936cb88016fd8ccbdf4bd12ac1af84f2921fab4f`。
独立结构化 3-D holdout task/flight=`0.984444/0.981111`，相同 seed 的 r17 对照为
`0.570000/0.708889`；r22 仍未 promotion，不能替换当前参考模型，也不能把这 25 帧当作完整
像素级训练集或泛化证明。训练集清单为
[`artifacts/training/onboard-rendered-3d-mainline-v1.json`](../artifacts/training/onboard-rendered-3d-mainline-v1.json)。

这一步证明的是“真实 Gazebo 3-D 渲染帧可以进入同一 OnboardAI 训练契约”；Docker 当前再次无响应的
独立 external run 仍没有新增容器、进程或帧，因此没有外部 Gazebo 自动飞行、Jetson、Pixhawk、
HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布结论。下一步是积累多天气、多视角、多目标的独立渲染集，
并在同一固定 holdout 上比较候选后再决定是否 promotion。

## 2026-10-05：OnboardAI r18 3-D 传感器契约增量训练（r22 的结构化父线）

在 r17 基础上继续训练同一套 OnboardAI，不增加第二个飞控模型，也不改变 40 维输入、9 个任务
动作、7 个飞行动作、4 个运动回归输出或安全适配器。新增的 `--gazebo-3d-manifest` 训练入口读取
仓库主线的三种实体 3-D 场景：`x500` 多旋翼城市巡检、`standard_vtol` 河道救援和 `rc_cessna`
海岸固定翼；每个场景都要求 RGB、Depth、PX4 telemetry，固定翼还要求 airspeed。它将载具速度/
高度/距离、相机/深度可用性、阶段、安全事件和结构化检测结果加入同一模型分布；没有把 Gazebo
真值坐标、billboard 或某个红/绿目标标签输入模型。

候选模型为 `artifacts/models/onboard-policy-r18-gazebo3d-sensor/onboard_policy_r18.onnx`，
62145 bytes，SHA-256=`c49ddd7c88f5217957886ccd0203373b071b7a6f947c294d0bd1ec7b7715eef2`，父模型
r17 SHA-256=`7434d94fb676427cdf34086f92b42f57cec5ac467b7942e7b116ea5c8bfdbed3`。RTX 4060 使用
35100 条样本、80 epochs；evaluation task/flight accuracy=`0.9908831909/0.9914530`，回归 MAE
为 north `10.851 m`、east `23.668 m`、altitude `5.528 m`、speed `1.580 m/s`，clip rate=`0`。
独立 holdout 为 `7/7`。22 个 truth-free 泛化用例 guarded=`22/22`、navigation range=`22/22`、
class/color invariance=`4/4`；raw head=`17/22`，`masked_success_count=5` 单独表示安全/阶段
契约补救。绑定证据为
[`docs/evidence/onboard-policy-r18-gazebo3d-sensor-20261005.json`](evidence/onboard-policy-r18-gazebo3d-sensor-20261005.json)。

这仍是 RTX 4060 的合成结构化/软件训练，不是像素级 3-D 渲染训练，也不是 Jetson、Pixhawk、
外部 Gazebo 自动飞行、HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布证据。Docker engine/API 仍不可达，
外部 3-D 运行继续保持未宣称。

## 2026-10-05：3-D replay 配比审计（r19/r20/r21，均不 promotion）

为避免把“训练规模变化”误判成 3-D 能力提升，使用同一 r17 父模型和同一基础训练规模做了
三组增量实验。r19 加入 3,000 条 3-D 传感器契约样本，整体 evaluation task/flight 为
`0.990625/0.991890`，但独立 3-D holdout 为 `0.956667/0.951111`；它确实学会了更多跨
多旋翼、VTOL、固定翼工况，但损伤了通用策略准确率。r20 只降低学习率到 `0.0005`，整体降至
`0.979464/0.981771`；r21 只加入 1,000 条 3-D replay，整体为 `0.989954/0.990798`，
3-D holdout 为 `0.852222/0.862222`，仍未同时达到父模型的两个目标。相同 seed 的无 3-D
对照为 `0.995483/0.994860`，因此当前保留 r17 作为选定参考，r18/r19/r20/r21 全部标记为
candidate，不替换主模型。比较证据为
[`docs/evidence/onboard-policy-3d-replay-comparison-20261005.json`](evidence/onboard-policy-3d-replay-comparison-20261005.json)。

这次审计确认下一轮训练必须使用真正渲染的三维 RGB/Depth/telemetry/perception 记录，并同时
保留独立基础策略 holdout；不能继续只扩大结构化随机样本。当前一次 `urban_inspection_multirotor_001`
实体 x500 Gazebo 运行在 Docker inspect 阶段因 Docker API 无响应而未创建容器、未启动外部进程、
未生成帧，故没有外部 Gazebo 通过结论。

## 2026-10-05：OnboardAI r17 teacher 一致性与阶段安全增量训练（r18 父模型）

在 r16 基础上修正训练 teacher：class/color 任一未知或不匹配时，`target_match_score` 不再
生成真实 perception adapter 不可能产生的高匹配正例。r17 继续保持同一个 40 维输入、9 个
任务动作、7 个飞行动作和 4 个运动回归输出；新候选默认不注入红车 fixture，保留类别/颜色
无关属性组合、未知属性搜索、健康事件和 TAKEOFF 阶段安全转移。模型文件为
`artifacts/models/onboard-policy-r17-teacher-consistent/onboard_policy_r17.onnx`，62145 bytes，
SHA-256=`7434d94fb676427cdf34086f92b42f57cec5ac467b7942e7b116ea5c8bfdbed3`；父模型 r16
SHA-256=`478b2abf0c68c271971b45a7f21b2ad7c60e6913122f5176f872d604b1cacd12`。RTX 4060
评估 task/flight 准确率为 `0.9932242991/0.9936137072`；回归 MAE 为 north `7.618 m`、
east `21.805 m`、altitude `4.373 m`、speed `1.343 m/s`，altitude P95=`25.583 m`，
clip rate=`0`。

独立结构化 holdout 为 `7/7`。22 个 truth-free 泛化压力用例经同一 runner 的 guarded 结果
为 `22/22`，导航范围 `22/22`，类别/颜色字符串不变性 `4/4`；`masked_success_count=5`
单独表示安全/阶段约束补救，不算 raw head 学会。证据为
[`docs/evidence/onboard-policy-r17-teacher-consistent-20261005.json`](evidence/onboard-policy-r17-teacher-consistent-20261005.json)。

这一轮仍是 RTX 4060 合成/软件验证，不是 Jetson、Pixhawk、HIL、REAL、室外 GNSS、真实
3D Gazebo 飞行或最终 `0.3.0` 发布。3D 主线仍使用实体 x500/VTOL/固定翼模型与
RGB/Depth/GPS/里程计传感器；本机 Docker Linux engine/API 当前不可达，外部运行没有启动。

在 r14 → r15 → r16 上连续增量训练，没有另起飞控模型。r16 保持同一个 40 维输入、9 个
任务动作、7 个飞行动作和 4 个运动回归输出；新训练默认关闭红车 fixture 注入，增加了
类别/颜色无关的属性组合、未知属性搜索、健康事件和 TAKEOFF 阶段安全转移。模型文件为
`artifacts/models/onboard-policy-r16-phase-safe/onboard_policy_r16.onnx`，62145 bytes，
SHA-256=`478b2abf0c68c271971b45a7f21b2ad7c60e6913122f5176f872d604b1cacd12`；父模型 r15
SHA-256=`01f988f8a8083290f0f12f24ad311d4ec09175993f67a98a53b6d4727dd93673`。RTX 4060
评估 task/flight 准确率为 `0.9906542056/0.9909657321`。训练报告现在包含 north/east/
altitude/speed 的 MAE、P95 与 clip rate；r16 evaluation 的 altitude MAE=`5.767 m`、P95
`32.467 m`、clip rate=`0`。

独立结构化 holdout 为 `7/7`。新的 22-case truth-free 泛化压力使用同一
`OnboardOnnxModelRunner` 同时记录 raw head 与 guarded result：guarded `22/22`，导航范围
`22/22`，类别/颜色字符串不变性 `4/4`；其中 `masked_success_count=4` 明确表示安全/阶段
约束补救，不算模型 raw head 学会。证据为
[`docs/evidence/onboard-policy-r16-phase-safe-20261005.json`](evidence/onboard-policy-r16-phase-safe-20261005.json)。

这一轮仍是 RTX 4060 合成/软件验证，不是 Jetson、Pixhawk、HIL、REAL、室外 GNSS、真实
3D Gazebo 飞行或最终 `0.3.0` 发布。3D 主线仍使用实体 x500/VTOL/固定翼模型与
RGB/Depth/GPS/里程计传感器；本机 Docker Linux engine/API 当前不可达，外部运行没有启动。

## 2026-10-05：OnboardAI r14 增量训练与独立安全事件 holdout（当前最新）

在现有 OnboardAI 结构和 r13 权重上继续增量训练，没有另起飞控模型。r14 保持同一个 40 维
输入、任务头和飞行动作头，在本机 RTX 4060 上使用 temporal transition augmentation，加入
目标不匹配后的搜索、匹配跟随、位置记录返航、报告，以及 GPS 降级、电量不足、障碍风险和
推理故障时的 HOLD/RETURN/LAND 转移。模型文件为
`artifacts/models/onboard-policy-r14-autonomy-transitions/onboard_policy_r14.onnx`，62145
bytes，SHA-256=`7a50fb9669f46d78937ff89d261e63603373cd73c4840cf243603d4ded1edff8`；父模型
r13 SHA-256=`7db556f6916fac45d3ab64dda9cd2684d4664a9579c615cdc2bc757c413a2027`。评估集 task
准确率 `0.9772115385`、flight 准确率 `0.9757019231`。

独立结构化 holdout 使用未参与训练的 7 个安全/任务状态，通过率 `7/7`、task/flight 准确率
`1.0`，报告为 `artifacts/evaluation/onboard-policy-r14-independent-holdout.json`。同一
模型的 truth-free 软件 replay 完成 `search → match_target → record_location → return_home
→ report`，planner revalidated 为 `true`，报告为
`artifacts/evaluation/onboard-policy-chain-autonomous-20261006-r14-temporal.json`。本轮同时
修正 ONNX runner：健康事件可以在 SEARCH/INSPECT_TARGET 阶段提出 HOLD/RETURN/LAND，但最终
PX4/MAVLink 权限仍归 adapter 安全监督器，模型不直接驱动执行器。

摘要证据为 [`docs/evidence/onboard-policy-r14-autonomy-transitions-20261006.json`](evidence/onboard-policy-r14-autonomy-transitions-20261006.json)，
文件 3743 bytes，SHA-256=`ab47f678d49e66055118b29392a00df2c10e80b7fefa25a9c63f865e80461447`。这仍是
truth-free software replay/独立 holdout：不是用户要求的真实 3D Gazebo 自动飞行主线，不是
Jetson、Pixhawk、HIL、REAL、室外 GNSS，也不允许 promotion 或最终 `0.3.0` 发布。下一步是
把同一 r14 契约接入真实 3D vehicle/world/sensor flight loop，并为 3D 场景单独建立可复核证据。

## 2026-10-05：Gazebo 3-D 主线资产审计

已把 Gazebo 主线从红车/绿车感知夹具中分离出来，固定为
`configs/scenarios/gazebo-3d-mainline-v1.manifest.json`：多旋翼
`urban_inspection_multirotor_001` + `x500_mono_cam`、VTOL `river_rescue_vtol_001` +
`standard_vtol_mono_cam`、固定翼 `coastal_patrol_fixed_wing_001` + `rc_cessna_mono_cam`。
Manifest 同时绑定 world/runtime/scenario/model/config/map/DEM 的 SHA-256 和字节数，验证 ODE
物理、WGS-84/ENU、实体 `model://` 载具依赖、RGB/Depth/PX4 telemetry/airspeed 传感器契约，
并拒绝 billboard、网络下载、evaluator truth 或真值注入。

离线 verifier `scripts/verify_gazebo_3d_mainline_manifest.py` 已重新解析 SDF/XML、调用现有
stack planner 做路径/模型复解析；结果为 3 个场景通过，退出码 `0`。摘要证据为
[`docs/evidence/gazebo-3d-mainline-manifest-20261005.json`](evidence/gazebo-3d-mainline-manifest-20261005.json)，
1758 bytes，SHA-256=`a809456f49f08c25fc500ccdead2de799c800732a09d04d055f639281d041cda`。
这是仓库资产和契约审计，不是已经启动 PX4/Gazebo 的外部运行，不是 Jetson/Pixhawk、HIL、
REAL、室外 GNSS 或最终 `0.3.0` 证据；下一步才是把 r14 加载到这个 3-D flight loop 做实际
传感器和飞行状态 replay。

### 2026-10-05：r14 接入 3-D 外部入口的第一次尝试（blocked）

使用同一个 r14 ONNX artifact 和 `urban_inspection_multirotor_001` 实体 x500 world 做了外部
PX4/Gazebo 入口尝试，入口只请求 RGB/Depth/GPS/里程计采集，不发送 ARM、Mission 或执行器命令。
仓库 preflight 和容器计划均通过，但本机 Docker 在 `container inspect` 阶段超时（10 秒），
没有确认容器创建、没有收到 telemetry、没有采到传感器帧，因此这轮不能写成 external Gazebo
通过。原始 evidence 为 `artifacts/evaluation/external-onboard-r14-urban-3d-20261005.json`，
摘要为 [`docs/evidence/external-onboard-r14-urban-3d-20261005.json`](evidence/external-onboard-r14-urban-3d-20261005.json)，
状态 `failed`。这是环境阻断，不改变 r14 的软件 holdout 结果，也不改变 3-D manifest 资产审计
结果；后续需要先恢复 Docker daemon/inspect，再重跑同一 manifest、同一模型和同一输入绑定。

## 2026-10-05：r9 软件闭环回放（最新）

在 r8 候选模型和现有 OnboardAI 结构上重新执行了最新软件回放。r9 没有另起飞控模型，
仍使用 `artifacts/models/onboard-policy-r8-redcar/onboard_policy_r8.onnx`，但把最新的
任务阶段约束、飞行动作兼容约束和有界高度解码一起纳入回放。阶段顺序完整为
`search → match_target → record_location → return_home → report`，搜索和跟随 Mission
均经过现有 planner/PX4 Mission 编译重验证。回放文件为
`artifacts/evaluation/onboard-policy-chain-autonomous-20261005-r9-candidate.json`，30564
bytes，SHA-256=`fdcda88c43ebf0f649d3eac76faea9c6a473cba180329efc1c05a6aaa75cc359`；摘要见
[`docs/evidence/onboard-policy-r9-candidate-20261005.json`](evidence/onboard-policy-r9-candidate-20261005.json)，
其 SHA-256=`6d153697d96e8a0e6d62fc194590dd4bfb75d9b70bf39b46394b5a768f643d27`。
这仍是 truth-free software SIM candidate replay，不是外部 Gazebo 自动飞行、Jetson、Pixhawk、
HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布证据。

## 2026-10-05：从现有 OnboardAI 增量训练红车任务候选

本轮没有另起一套飞控模型。训练脚本从已审核的 r3 ONNX 权重校验 SHA-256 后继承，
加入红车任务样本、task/flight 联合兼容损失，并在同一个 ONNX runner 中加入任务阶段和
飞行动作兼容约束。RTX 4060 上完成了 24,000 条 synthetic 样本、100 epochs 的候选训练：
任务准确率 0.910625，飞行动作准确率 0.91375。候选模型为
`artifacts/models/onboard-policy-r8-redcar/onboard_policy_r8.onnx`，SHA-256=
`ac98562040c9ed13e6fd4c941dd1c7dfd9a4c2d0fdc198d285ef8915cbf477dc`，父模型 SHA-256=
`617b666fd6daf538421ba8ce4af4aa1332bd882e0aec6c4d8ba8115fee16786d`。

候选在确定性软件回放中完成了：
`search → match_target → record_location → return_home → report`，并且搜索和跟随意图都
经过现有 planner/PX4 Mission 编译重验证。回放文件为
`artifacts/evaluation/onboard-policy-chain-autonomous-20261005-r8-candidate.json`，
SHA-256=`408f207f29a62bcdd06eef8de7f1525506fa3403d8cfdca14dd962eb49eceed2`；摘要见
[`docs/evidence/onboard-policy-r8-candidate-20261005.json`](evidence/onboard-policy-r8-candidate-20261005.json)。
这只是 software SIM candidate，尚未 promotion，也不代表 Jetson、Pixhawk、HIL、REAL、
室外 GNSS 或最终 `0.3.0` 发布。

同一候选在外部 PX4/Gazebo opt-in 轮次 r13 已确认 Docker、GPS、ARM 和起飞，但实时任务
重规划被两个独立门阻断：PX4 遥测电量约 51% 时，现有规划器的 40% 返航余量加 15% 安全
余量使搜索任务正确 abstain；随后安全 HOLD 的 POSCTL 模式没有得到心跳确认。因此该轮
没有伪造红车命中、GPS 记录、RTL 或报告成功；原始证据为
`artifacts/evaluation/external-red-car-autonomous-20261005-r13-structured.json`。

另外，自动飞行 + actual detector 现在会拒绝起飞前缓存帧，避免把预采样图像冒充飞行中实时
视觉；真正的实时相机 worker 仍是下一阶段工作。

> **当前权威快照（2026-10-05）**：版本仍为 `0.3.0-pre.8`，最终 `0.3.0`
> 保持阻断。当前 `configs/release-parity.json` 为 13,537 bytes，文件 SHA-256=
> `1bfed685146c813f1561980a893478d12f098a35ee9b5e653a3bc061cd0b96e7`，core hash=
> `5c7fa166279e8368824ed900a4e6a7bcbd40f06f1d95b78ff45ec60e37bff704`。代码与 parity
> 源重算已通过；历史 retained evidence 的逐项 parity 重绑定仍需继续，当前严格索引校验
> 暂不宣称通过，
> 但 fail-closed release audit 仍为 `release_allowed=false`、32 个 blocker。

> **2026-10-05 训练入口收紧**：`training_ready=true` 的 OnboardAI 轨迹现在还必须绑定独立的
> `multidetect.onboard-trajectory-label-review-v1` receipt、canonical JSONL 标签、实际 RGB/Depth
> manifest、可由 `RawSensorReplayRecording` 重开的完整 raw recording、完整 episode 分区和
> planner/Mission 证据；receipt 不能包含 Gazebo 世界真值、模型 ID 或自标注。当前仓库已有的 3D
> 资料仍是 3 帧 RGB/Depth plumbing 或短时 fixture，没有足够的长连续 3D episode、人工审核标签
> 和完整 search→record→return 轨迹，因此没有把它们标成训练完成。

> 同一训练门槛现在还会检查 receipt、label manifest、raw recording manifest 和每条 transition
> 的五类 proof artifact 是否真实存在于数据集目录，并重新计算文件 SHA-256；RGB payload 必须
> 与同一 raw replay frame 逐帧相等。这样可以阻止“哈希格式正确但文件不存在”或“RGB 与 raw 不是
> 同一帧”的假连续轨迹进入微调。该改动是证据完整性修复，不是新的 Gazebo/PX4 运行证据。

> **r25 增量训练（2026-10-05）**：在现有 r23 ONNX 权重上用 RTX 4060 训练同一 40 维
> OnboardAI，输入包含仓库现有 3-D RGB/Depth plumbing 集。候选为
> `artifacts/models/onboard-policy-r25-3d-depth-gated-dev/onboard_policy_r25.onnx`，但源集只有
> 9 条未审核行、独立 test 行数为 0，evaluation task/flight=`0.873393/0.869512`；因此保持
> quarantined、未 promotion、未替换生产模型。摘要为
> `docs/evidence/onboard-policy-r25-3d-depth-gated-20261005.json`，不构成视觉泛化、Jetson、
> Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。

> **2026-10-05 OnboardAI execution seam and nearest-observed fix**：本机 RTX 4060 用 12,000 条
> synthetic bootstrap 样本训练了现有高层策略契约的扩展版 `onboard-policy-r4`（40 维输入，
> 增加 `target_class`、`target_color` 及 class/color match 结构化字段；不是另起一套飞控）。
> 初始 Mission 已绑定起飞段；软件回放随后验证“绿车类别匹配但颜色不匹配→左右搜索→红车匹配→记录 GPS→返航完成→汇报”五个模型任务阶段，
> 两次运动意图都经过 WGS-84、planner 和 PX4 Mission 重编译；r4 先校验并继承 r3 ONNX
> 权重，再仅扩展新增输入列，证据为
> [`docs/evidence/onboard-policy-chain-20261004-r4.json`](evidence/onboard-policy-chain-20261004-r4.json)，
> SHA-256=`da2a4043620c08e2a617930bb5c5be31ad190ad177f2f5185ca53a3ea0c6d16`，模型 SHA-256=
> `8ef33c3436b065065fff5b59c7cd3588900ce3374260940390c0dd460eeb7812`。
> 这是确定性软件回放和 synthetic 训练证据；尚不代表像素级颜色精度、Jetson、Pixhawk 实机、
> HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布。

> **2026-10-05 prompt-to-report executor**：新增 `OnboardAutonomousSortie`，把实时模型提案
> 与生命周期适配器串成一次无操作员步骤的任务：合格起飞 → 绿车不匹配后重规划搜索 → 红车
> 同目标确认 → 校准 N/E 偏移记录 GPS → 返航完成遥测 → 报告。新增回归验证了 `mission_start`、
> 返航请求和报告回执均通过注入的适配器回调完成，模型本身仍没有 MAVLink/arm/执行器权限。
> `scripts/run_onboard_policy_chain.py` 已改用这条执行器链；最新结果仍标记
> `software_sim_only=true`，不升级为外部 Gazebo 自动起飞、HIL、REAL、Jetson、室外 GNSS
> 或最终发布证据。最新回放文件为
> [`docs/evidence/onboard-policy-chain-autonomous-20261005-r2.json`](evidence/onboard-policy-chain-autonomous-20261005-r2.json)，
> SHA-256=`0bfe1baab5a20c232f81ec1ffa95d547026c61834cda4b1a2abecf993d216542`。

> 同日重新执行的软件回放 r3 仍得到完全相同的
> `search → match_target → record_location → return_home → report`，证据为
> `artifacts/evaluation/onboard-policy-chain-autonomous-20261005-r3.json`，SHA-256=
> `f6bb35c7692b1d2b0a8a4a26ec220b6337e4c4e78ad21b47b3e0f630bf2fc49b`。这是回放重执行，
> 不是外部 PX4/Gazebo 自动飞行或硬件证据。

> **2026-10-05 外部红车生命周期 r3 复核**：重新运行真实 Docker/PX4/Gazebo 红车场景后，
> 容器、RGB/Depth 传输、heartbeat、GPS 和清理均通过，但被动 probe 没有 arm 或启动 Mission，
> PX4 一直在地面；因此共享 `mark_takeoff_complete` 正确拒绝消费相机帧，实际推理结果数为 0，
> 没有产生红车命中、GPS 目标坐标、RTL 或报告。原始证据
> `artifacts/evaluation/external-red-car-actual-detector-20261005-r3.json`（109027 bytes，
> SHA-256=`5298c0a02e74c701fc9491ead12d187b253ae273e605265e6c15e5fa0be9d742`），摘要为
> [`docs/evidence/external-red-car-actual-detector-20261005-r3.json`](evidence/external-red-car-actual-detector-20261005-r3.json)。
> 本轮同时落地了显式的 disposable-loopback-SITL 生命周期适配器：只有
> `--execute --execute-mission-transactions --execute-autonomous-flight` 才会发送
> `AUTO.MISSION → ARM → 模型决定后 RTL → Home telemetry → REPORT`；默认被动入口仍不发飞控控制，
> 该新开关尚未宣称外部运行通过。

> **2026-10-04 actual Gazebo RGB → shared OnboardAI detector path**：在
> `gazebo_car_positive_control_001` 车辆正控场景中，固定 digest 的 PX4/Gazebo 外部软件
> SIM 真实抓取 2 帧 wire-v3 BGR24，仓库内隔离的 YOLO26n COCO 候选模型直接处理这两帧，
> 两帧均检测到 `car`（置信度约 `0.808`、`0.789`）；共享颜色适配器把它们判为
> `unknown`，因此两个事件都进入 `search`，没有把灰色车辆伪装成红车。两帧都经过
> `OnboardPerceptionObservation → OnboardRealtimeModelBridge → r4 OnboardAI`，planner
> 重新验证并生成受约束的搜索 Mission；Docker/PX4/Gazebo 清理通过。原始证据为
> `artifacts/evaluation/external-car-actual-detector-20261004-r2.json`，SHA-256=
> `099fbd987573ac68b1aa72f12533fe8e44aa0f5c57d4db52d86f03bb50199e58`，摘要为
> [`docs/evidence/external-car-actual-detector-20261004-r2.json`](evidence/external-car-actual-detector-20261004-r2.json)。
> 这一轮首次证明实际 Gazebo RGB 已进入共享检测/颜色/OnboardAI 边界；场景没有红色目标和
> depth companion，所以不宣称红车命中、GPS 目标落图、距离精度、Mission upload/permit、
> QGC 人工视觉、Jetson、HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布。

> **2026-10-04 actual-perception refresh r3**：颜色分类升级为优先使用检测框内高亮度/高色度
> 前景像素后重新执行同一车辆场景。新的两帧外部证据为
> `artifacts/evaluation/external-car-actual-detector-20261004-r3.json`，SHA-256=
> `3a8cb0d741a4e61c2a6ac4e034c59f101865b287fab979ada7caabc2662caf47`；两帧仍检测到
> `car`（约 `0.801`、`0.789`），颜色仍为 `unknown`，所以共享模型继续 `search`。这确认
> 改进后的颜色算法没有把当前非红车辆误判为红车；摘要为
> [`docs/evidence/external-car-actual-detector-20261004-r3.json`](evidence/external-car-actual-detector-20261004-r3.json)。

> **2026-10-05 actual Gazebo red-car RGB + Depth r2**：新增独立场景
> `gazebo_red_car_positive_control_001`，runtime 明确绑定 `front-camera` 与 `front-depth`，
> 不把 truth 注入运行时。固定 digest 的 PX4/Gazebo external SIM 实际抓取 2 帧 RGB 和 2 帧
> FLOAT32 Depth；真实 YOLO 候选模型检测到 `car`，共享颜色适配器输出 `red`、
> `target_color_match=true`，Depth 中位距离为 `5.057119846343994 m`，
> bearing=`-0.40285431095441143°`。RGB/Depth wire、PX4 GPS telemetry、容器 cleanup 全部通过。
> 原始证据 `artifacts/evaluation/external-red-car-actual-detector-20261005-r2.json` 的
> SHA-256=`bcf8e2f11c0003897801977e7d24f342930a983539a8ccd1e82e69d04da47341`，摘要见
> [`docs/evidence/external-red-car-actual-detector-20261005-r2.json`](evidence/external-red-car-actual-detector-20261005-r2.json)。
> 但外部 probe 仍为 failed：首帧从 `TAKEOFF` 直接进入 `match_target`，共享状态机拒绝跳过
> `SEARCH` 并 fail-closed；没有 Mission upload/readback/commit/permit，也没有提交 GPS 目标
> 坐标。该轮证明了真实红车感知+Depth 接入，不证明完整自动任务、Jetson、HIL、REAL、室外
> GNSS 或最终 `0.3.0`。

> **2026-10-05 起飞阶段门实现**：共享 `OnboardTaskStateMachine`、
> `OnboardMissionEventLoop` 与 `OnboardRealtimeModelBridge` 现在提供
> `mark_takeoff_complete`。该接口只接受带 `relative_home` 相对高度、合格 GPS 和合格精度的
> 实时 `ContextSnapshot`，达到门槛后才把任务阶段从 `TAKEOFF` 推进到 `SEARCH`；重复调用
> 幂等，Mission 文件或相机帧不会自行推进阶段。相关实时桥接回归已通过；尚未把未起飞的
> 红车 r2 运行结果改写为完整任务成功。

> 随后执行了不启动 Docker 的红车阶段门 preflight：结果为
> `planned_not_executed`，没有产生外部 SIM、HIL、REAL 或硬件证据。该 preflight 只确认场景、
> 模型和阶段门配置可解析；真正重跑必须先有 PX4 遥测确认起飞完成，再让实际 RGB/Depth 帧
> 进入共享状态链。

> 最新真正新执行的外部软件 SIM 是 Prompt → OnboardAI → PX4/Gazebo 的 VTOL
> 两架次闭环：`artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261004-current-r6.json`
>（原始证据 6,583,748 bytes，SHA-256=`524beb880bb6141131dd07445dcf801362f55dbefcb7c1af5f577833b4cd7d09`）。
> 同一容器还持久化了 3 帧 wire-v3 BGR24 RGB/CameraInfo/NavSat/Odometry 与 3 帧
> depth companion；摘要为 `docs/evidence/prompt-onboard-vtol-gazebo-20261004-r6.json`。

> 这条链路使用注册的 PromptEnvelope、共享 `SharedTaskPolicy`、原有 planner/Mission
> compiler、PX4 upload/readback/commit/permit、journal 和 cleanup。它证明的是软件
> 集成与证据绑定，不是任意自然语言任务生成，也不代表目标移动/丢失等事件重规划已经
> 通过外部验收；更不代表 Jetson/Pixhawk 实机、HIL、REAL、室外 GNSS、QGC 人工视觉、
> metric distance accuracy 或最终发布。

> 当前三机型 Mission matrix 是
> `docs/evidence/external-mission-matrix-20261005-current-parity-audit-r1.json`。
> 它是对保留历史文件的 current-parity 审计重绑定，不是新的三机型外部 SIM 重跑；固定翼、
> 多旋翼、VTOL、四模式 policy matrix、训练回执、sensor campaign 和 AI-flight parity receipt
> 都通过了 `python scripts/verify_current_progress_index.py --root . --check-sources` 的
> source revalidation。对应文件仍明确标记 `retained_file_audit`、
> `runtime_source_parity_verified=false` 与 `external_sim_rerun_performed=false`；下方更早段落
> 保留为历史 chronology。

> 当前 release-gate 过渡审计是
> `docs/evidence/release-parity-transition-20261004-onboard-model-provider-r7.json`；它只做
> source-bound parity rebind，`runtime_reexecuted=false`、`release_allowed=false`，不代表
> 新 core 下重新执行了 PX4/Gazebo、HIL、REAL 或室外 GNSS。

> **2026-10-04 fresh external PX4/Gazebo sensor run**：
> `artifacts/evaluation/onboard-gazebo-sensor-20261004-r1/acceptance.json`（94,711 bytes，
> SHA-256=`640dcce7a172519b06cc405ec59105f08950e9cc2b558caefcef3843d0b08e98`）在 pinned
> `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
> 容器中通过了 heartbeat、qualified GPS、RGB/Depth/NavSat/Odometry 传输和清理门；实际
> 持久化 3 帧 RGB 与 3 帧 FLOAT32 depth companion，wire payload hash 一致。此轮没有
> Mission、ARM 或物理输出，不能提升为 OnboardAI 事件重规划、HIL、REAL、Jetson、室外
> GNSS 或最终发布证据。

> **2026-10-04 外部 PX4/Gazebo AI flight response r2（当前新执行）**：固定 digest 的
> disposable `px4io/px4-sitl-gazebo` 重新运行成功；arm heartbeat、truth-free target、
> 488 个共享 AI steps、5 秒控制响应和实际 `GLOBAL_POSITION_INT` 位移门全部通过，150 个
> 位置样本，最大水平位移 `1.302856 m`，门槛为 `0.5 m`。第一次同范围运行因 arm 时序只
> 收到 `MAV_RESULT_TEMPORARILY_REJECTED`，已保留为失败证据，未覆盖。成功摘要为
> `docs/evidence/simulator-ai-flight-20261004-r2.json`，原始证据为
> `artifacts/evaluation/continuation-docker-ai-flight-20261004-r2.json`。这是外部软件
> SITL/Gazebo AI flight response，不是 HIL、REAL、Jetson/Pixhawk、户外 GNSS、视觉精度或
> 飞行中 Mission replacement 验收。

> **2026-10-04 外部目标丢失负向路径**：在同一固定 digest/场景中注入
> `target_refresh_dropout`，arm、target policy 和 AI steps 先通过，故障被真实观察到，
> `fail_closed=true`，容器清理通过；正常 flight gate 按设计拒绝，进程非零退出是预期结果。
> 负向摘要为 `docs/evidence/simulator-ai-target-dropout-20261004.json`。这证明的是
> 外部软件 SITL/Gazebo 的目标刷新故障安全门，不是正常 flight 通过，也不代表 HIL、REAL、
> Jetson/Pixhawk、户外 GNSS 或飞行中 Mission replacement 已验收。

> **2026-10-04 外部心跳丢失负向路径**：注入 `heartbeat_dropout` 后，arm、target 和
> AI bridge 在故障前正常，心跳故障被观察到，控制 authority 进入 `failsafe`，
> `fail_closed=true`，容器清理通过。正常 flight gate 按设计拒绝，摘要为
> `docs/evidence/simulator-ai-heartbeat-dropout-20261004.json`。这只证明外部软件
> SITL/Gazebo 的链路故障安全边界，不代表 HIL、REAL、Jetson/Pixhawk 或最终发布。

> **2026-10-04 外部飞行模式丢失负向路径**：注入 `flight_mode_loss` 后，arm、target 和
> AI bridge 在故障前正常，模式丢失被观察到，控制 authority 进入 `failsafe`，
> `fail_closed=true`，容器清理通过。正常 flight gate 按设计拒绝，摘要为
> `docs/evidence/simulator-ai-flight-mode-loss-20261004.json`。这只证明外部软件
> SITL/Gazebo 的模式故障安全边界，不代表 HIL、REAL、Jetson/Pixhawk 或最终发布。

> **2026-10-04 外部 GPS 质量退化负向路径**：注入 `gps_quality_loss` 后，故障触发后停止
> 转发后续 `GPS_RAW_INT`，不修改 PX4 数据、不放宽共享 GPS 阈值；AI 继续视觉 TRACK，
> 但不把旧 GPS 样本当成新的定位结果。`fault_observed=true`、`fail_closed=false`，容器
> 清理通过，正常 flight gate 按设计拒绝。摘要为
> `docs/evidence/simulator-ai-gps-quality-loss-20261004.json`。这证明的是当前策略在
> GPS 退化时的保守“继续跟踪但不宣称定位”边界，不代表 GPS fail-safe、HIL、REAL、
> Jetson/Pixhawk、户外 GNSS 或最终发布。

> **2026-10-04 外部 GPS 退化返航安全路径**：在同一固定 digest/场景中，GPS 退化后通过
> live bridge 控制面提交返航请求；共享 executor 返回 `failsafe`，原因是
> `return_gps_quality_insufficient`，`fail_closed=true`，容器清理通过。摘要为
> `docs/evidence/simulator-ai-gps-return-failsafe-20261004.json`。这证明软件
> SITL/Gazebo 的 GPS-gated RETURN 安全边界，不代表 HIL、REAL、Jetson/Pixhawk、户外
> GNSS 或最终发布。

> **2026-10-04 外部低电量负向路径**：注入 `battery_remaining=5` 后，共享 executor 进入
> `failsafe`，原因是 `home_vector_unavailable`；缺少合格返航向量时没有盲目发出返航控制，
> `fail_closed=true`，容器清理通过。摘要为
> `docs/evidence/simulator-ai-battery-low-failsafe-20261004.json`。这证明软件
> SITL/Gazebo 的低电量 fail-safe 边界，不代表真实电池、HIL、REAL、Jetson/Pixhawk、
> 户外 GNSS 或最终发布。

> **2026-10-04 外部低电量 HOME_POSITION→RETURN 基线路径**：随后在同一固定 digest 的
> fresh campaign 中观察到真实 PX4 `HOME_POSITION`，bridge 用它和观察到的
> `GLOBAL_POSITION_INT` 计算返航向量（`home_distance_m=0.0157409281`）。注入
> `battery_remaining=5` 后，共享 deterministic simulator baseline 输出并执行
> `RETURN`，原因 `battery_below_return_threshold`；`ai_steps=220`、`ai_rejected=0`，
> container cleanup 通过。该故障 campaign 按设计不满足正常飞行位移门，因此
> `normal_flight_gate_passed=false`。摘要为
> `docs/evidence/simulator-ai-battery-low-return-home-20261004.json`，原始证据为
> `artifacts/evaluation/continuation-docker-ai-battery-low-home-20261004.json`。
> 这证明的是 PX4 HOME_POSITION telemetry 已进入共享安全基线，不是 Jetson 学习模型推理、
> HIL、REAL、Pixhawk 硬件、室外 GNSS 或最终发布证据；当前 Jetson/4060 model-backed
> `OnboardDecisionProvider` 接口已落地，但真实高层策略 artifact 尚未接入。

> **2026-10-04 实时事件顺序修复**：`OnboardMissionEventLoop` 的历史事件重试现在只返回原始
> revision，不会覆盖最新 revision；`SimulatorClosedLoop` 只应用新的单调 revision，不会把任务
> observer 回退到旧 Mission，也不会在安全清除后被旧重试恢复。installer/safety 回调失败时，
> 模拟器会清除不可信 observer。相关 `OnboardAI`、模拟器、地形 parity 和进度索引回归已通过。

> **2026-10-04 Mission replacement transaction scaffold**：新增独立、父事务绑定的
> `MissionReplacementTransaction`。它先记录 pause/upload/commit/cursor/resume 意图，再调用
> transport；每一步都要求同源确认、完整 Mission readback hash、合法的非危险 handoff item
> 和精确 `AUTO.MISSION` 恢复证明。哈希链 journal 能识别篡改和不完整尾记录，`recover()` 只
> 描述人工恢复动作而不自动重发命令；fallback 不确定时进入 `RECOVERY_REQUIRED`。这为
> 外部 PX4/Gazebo 的飞行中重规划提供安全边界，但当前 transport 尚未接入真实 pause/hold/
> return 控制，因此不计为外部飞行中重规划验收。

> **2026-10-03 Prompt catalog hardening + current parity**：修复 fresh-process 导入循环，
> 新增 `tests/test_autonomy_import_order.py`；`PromptTaskResolver` 现在只接受显式注册的
> `prompt_id`，运行时快照递归冻结并绑定 hash，`prepare_from_prompt` 后的重规划不能替换
> 白名单外任务，decision/evidence 记录 `resolver_id` 与 resolution hash。当前 parity 已重新生成：
> `configs/release-parity.json` 为 12919 bytes、SHA-256=
> `3e12c3600eff9974d6441cb9777efc16cc3d0bdc1dc912eb32572aaf0758defc`，core hash=
> `538e53b616f0ccf992373ec5d13eb634946dffdde76e67db54187dc57bafafa8`。新 current-parity
> preflight 为 `docs/evidence/onboard-ai-preflight-20261003-r2.json`，通过的仍是
> Docker 计划/安全门与 truth-free 软件 fixture；没有重新把旧 external r2 运行绑定成当前 parity，
> 也没有新增 Jetson、HIL、REAL、硬件、室外 GNSS 或最终 `0.3.0` 发布证据。

> **2026-10-03 OnboardAI executor foundation（当前新增）**：新增
> `src/multidetect/autonomy/onboard_agent.py`，将一次 `PromptEnvelope`、实时
> `ContextSnapshot`、`SharedTaskPolicy`、Planner 重验证和现有 PX4 Mission 编译路径串成
> 同一个 SIM/HIL/REAL 编排骨架。目标移动、目标丢失和任务完成会重新选择/编译；GPS 退化、
> 电量不足、障碍风险和推理故障只生成 `RETURN/HOLD` 高层提案，不产生新的伪 Mission，也不
> 直接发送 actuator/arm/disarm。当前已通过 `tests/test_onboard_agent.py` 及 Planner/Mission
> 回归，证据为 `docs/evidence/onboard-ai-executor-foundation-20261003-r1.json`。该项仍只代表
> 自动化共享核心验证；没有把它写成外部 PX4/Gazebo 新运行、Jetson、HIL、REAL、硬件、室外
> GNSS、人工视觉或最终 `0.3.0` 发布证据。

> **2026-10-03 OnboardAI external PX4/Gazebo SIM r1（失败收口）**：启动了新的 pinned
> `px4io/px4-sitl-gazebo` disposable container，heartbeat/GPS 和 36,123 帧有效 telemetry
> 到达，但有 783 帧被严格 source identity gate 拒绝（source system/component 为 0），因此
> gate=`false`，AI 保持 disarmed 未取得运行权限。容器停止/删除和 parity 起止锁定均通过。
> 这不是通过证据；下一步先修复并复测 source-identity 处理。原始证据为
> `artifacts/evaluation/onboard-ai-external-sim-20261003-r1.json`，摘要为
> `docs/evidence/onboard-ai-external-sim-20261003-r1.json`。

> **2026-10-03 OnboardAI external PX4/Gazebo SIM r2（通过软件 SIM gate）**：在同一 pinned
> disposable container 中显式执行 SITL-only arm/disarm 和 truth-free target fixture；heartbeat/GPS
> 合格，992/1021 telemetry 帧被接受，2 次共享 AI 决策通过，目标策略输出 `track`，parity
> 起止一致，容器清理通过。未执行位移飞行，也没有读取 Gazebo truth；因此这只证明外部软件
> SIM 的实时 OnboardAI 输入/决策/控制适配链路，不代表相机 detector、距离精度、QGC 视觉、
> Jetson、HIL/REAL、硬件、室外 GNSS 或最终发布。证据为
> `docs/evidence/onboard-ai-external-sim-20261003-r2.json`。

> **2026-10-03 OnboardAI Docker/SIM preflight r1（当前新增）**：用当前 parity 执行了
> `run_simulator_docker_ai_acceptance.py` 的 Docker 安全计划、场景绑定和 truth-free AI
> bridge fixture。`preflight_gate_passed=true`、AI fixture 通过、parity 起止快照一致；
> 本次 `execution_requested=false`，没有启动 PX4/Gazebo 外部进程，也没有产生飞行、QGC
> 视觉、Jetson、HIL/REAL、室外 GNSS 或硬件证据。完整原始证据为
> `artifacts/evaluation/onboard-ai-preflight-20261003-r1.json`，摘要为
> `docs/evidence/onboard-ai-preflight-20261003-r1.json`。

> **2026-10-03 MultiQGC + JetsonAI 同链路入口（当前新增）**：组合 PX4/Gazebo/QGC
> 入口 `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在可选绑定 Jetson
> 等价 safety ONNX 与 manifest。模型经过现有 `build_production_perception`，与 live/Jetson
> 使用同一感知工厂；输出只作为目标/安全证据，不拥有飞控权限。使用说明见
> `docs/gazebo-qgc-jetson-ai-sim.md`。仓库内 COCO manifest 当前仍是 `quarantined`，因此
> 这一步不打开生产批准、HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布门。

> **2026-10-03 Gazebo vehicle image + candidate detector smoke（当前新增）**：在仓库自有
> `gazebo_car_positive_control_001` 场景中，pinned PX4/Gazebo external SIM 采集 3 帧
> 1280×960 RGB；首帧 PNG 显示 SUV 正向控制目标，当前隔离的 COCO raw YOLO candidate
>（CPUExecutionProvider，阈值 0.1）输出 `car` confidence `0.806`。完整 machine-side
> 证据为 `artifacts/evaluation/gazebo-car-image-sim-20261003-r2/acceptance.json`，索引为
> `docs/evidence/gazebo-car-image-sim-20261003-r2.json`。这只是图像输入与候选检测 smoke，
> 不代表 detector accuracy、生产批准、QGC 人工视觉、距离精度、HIL/REAL 或最终 release。

> **2026-10-03 Gazebo scene catalog（当前新增）**：新增 `docs/gazebo-scene-catalog.md`，
> 登记仓库现有场景和官方 PX4/Gazebo 候选 `baylands`、`lawn`、`walls`、`windy`、`aruco`；
> 外部场景在资源 SHA/许可证/topic binding 完成前不计入 release parity。

> **2026-10-03 Gazebo image sensor SIM r3（当前新增）**：使用 pinned PX4/Gazebo
> disposable container 对 `river_rescue_vtol_001` 真实启动相机与深度传感器，完成 3 帧
> 1280×960 `BGR24` RGB 像素和 3 帧 `FLOAT32_M` 深度 payload 的持久化、wire digest、
> RGB/Depth pair binding、GPS/odometry/camera-info 绑定与容器清理。首帧 PNG 预览位于
> `artifacts/evaluation/gazebo-image-sim-20261003-r3/rgb-frames/frame-000000.png`；完整
> machine-side 证据为 `artifacts/evaluation/gazebo-image-sim-20261003-r3/acceptance.json`
>（SHA-256=`c7df7787b3d389118d224c7b3822a85b26e045bd486b77eeb72737b7146eda6a`），简明索引为
> `docs/evidence/gazebo-image-sim-20261003-r3.json`。这证明有真实 Gazebo 图像/深度数据，
> 仍不宣称 QGC 人工视觉、detector、metric-depth 标定、户外距离精度、HIL、REAL、硬件或
> 最终 `0.3.0` 发布；`visual.status=not_claimed` 保持不变。

> **2026-10-03 current parity / QGC combined SIM r5**：新一轮 900 帧 PX4/Gazebo/QGC
> software-SIM 已完成，raw evidence 为
> `artifacts/evaluation/combined-sim-qgc-20261003T070847Z-42300/combined-evidence.json`
>（4,035,353 bytes，SHA-256=`f17b3701508a95c269b527827eb289b9c4ca8e26a1a920a01749eb611e759b02`），
> 摘要为 `docs/evidence/combined-sim-qgc-20261003-r5.json`。运行绑定当前
> 该历史运行当时绑定的 parity 为上一轮 `084e1d...`；本轮 OnboardAI executor 纳入核心
> parity 后，当前 `configs/release-parity.json` 为 12864 bytes、SHA-256=`e7fc84d1e6ef35c749887d20177f3211decdccd6728a2b60f4032fbe041d240a`，
> 因此不能把这次旧运行重命名成当前 parity 的新运行。其 integrated sensor stream、native video surface、machine toolbar smoke、精确 PID/HWND
> `SELECT_TRK`/SelectionAck/TrackStatus、VTOL Mission transaction 和 disposable-container
> cleanup 均通过。当前 release-gate audit 已切换到
> `docs/evidence/shared-policy-release-parity-policy-helper-20261003-qml-r7.json`；该审计是
> retained-files parity rebind（`runtime_reexecuted=false`），仍有 32 个 blocker，不能替代
> HIL、REAL、硬件、户外 GNSS 或人工视觉验收，最终 `0.3.0` 仍未发布。

> **2026-10-03 Depth Anything offline replay r1（当前新增诊断证据）**：使用仓库内 pinned
> `Depth Anything V2 metric outdoor-small` checkpoint 导出的 ONNX（SHA-256=`36b38a0b34f5431a9bbcbedca7d72b440006204910d13227bc844641b0b23e52`），
> 通过 CPU ONNX Runtime 重放 r2 外部 PX4/Gazebo SIM 的 3 个 RGB/Depth 配对帧。RGB/Depth
> 两份 manifest、每个 payload 和 pair binding 均重新验签；离线预处理已严格对齐
> `MetricDepthEstimator`（BGR→RGB、`INTER_CUBIC`、ImageNet mean/std、NCHW），并在模型原生
> dense 输出网格上对齐 Gazebo comparison reference。模型输入 `[1,3,518,518]`、输出
> `[1,518,518]`，有效比较像素 202,800，原始 RMSE=`2.49894642829895 m`、中位绝对误差
> `2.501652479171753 m`。该结果显示当前模型输出与 Gazebo `optical_axis_z_m` payload 存在
> 明显未校准尺度/场景差异，因此没有拟合 calibration、没有生成现场标定文件，也没有批准
> 生产模型。完整 receipt 为 `artifacts/evaluation/depth-model-sim-replay-20261003-r1/replay-receipt.json`
>（5,093 bytes，SHA-256=`d2f79dbd08cde62b8d1589d385c4d9af40d244bd88d1fbf3a005f0c2d160f16d`），摘要为
> `docs/evidence/depth-model-sim-replay-20261003-r1.json`（SHA-256=`929e7e2f30644dfb60b4bfbe26f000fa8653e3b262b380db69bd72c6ad3f0b5c`）。这只是 software-SIM 离线诊断，
> 不等于户外距离精度、HIL、REAL、硬件验收或最终 `0.3.0` 发布；`metric_depth_model_approved=false`
> 与 `final_release_allowed=false` 保持不变。

> **2026-10-03 fresh external PX4/Gazebo depth companion run r2（当前权威深度传输证据）**：使用
> 正确的 `river_rescue_vtol_001.scenario.json` index 和 pinned PX4/Gazebo image，真实启动并清理
> disposable 容器；MAVLink heartbeat/GPS telemetry、RGB/Depth synchronized transport 与
> `sensor_transport_gate` 全部通过。显式开启 `--sensor-depth-output-dir` 后，3 个
> `FLOAT32_M` depth payload（共 14,745,600 bytes）写入独立 sibling 目录；每帧重新构造并
> 校验 `MDGZDEP1` digest、RGB wire digest、时间戳、几何、topic、calibration ID 和 pair binding，
> 独立 depth manifest canonical SHA 与 raw-payload SHA 均通过。完整源证据为
> `artifacts/evaluation/depth-companion-sim-20261003-r2/acceptance.json`（94,504 bytes，
> SHA-256=`549f78fa54f468558cea0b90d64a0b44437d07426ed4672c783c9c28256c18e9`），简明索引为
> `docs/evidence/depth-companion-sim-20261003-r2.json`。这仍只证明 pinned PX4/Gazebo software
> SIM 的 RGB/Depth 传输与可重建 payload 持久化，不等于真实 metric-depth model calibration、
> 户外距离精度、HIL、REAL、硬件、人工视觉验收或最终 `0.3.0` 发布；`release_allowed=false`
> 与 `candidate_promoted=false` 保持不变。

> **2026-10-03 Depth companion persistence（当前源码增量）**：在现有 pinned Gazebo
> sensor transport 上增加了显式 opt-in 的 `--sensor-depth-output-dir`。它要求同时保存
> RGB frame set，并在 sibling 目录写入原始 `FLOAT32_M` depth payload 与独立的
> `gazebo-depth-companion-set.manifest.json`；每帧重新构造并校验 `MDGZDEP1` signed digest、
> RGB record digest、时间戳、几何、源 topic、标定 ID 和 RGB/Depth pair binding。RGB v2
> manifest 与 VTOL exact-directory consumer 未改变；默认仍关闭。新增 persistence、配对、
> canonical manifest 与篡改检测回归全部通过。该项只提供 software-SIM 传感器回放素材，
> 不等于 monocular metric-depth field calibration，不宣称户外距离精度、HIL、REAL、硬件或
> 最终 `0.3.0` 发布；当前 `0.3.0-pre.8`、`release_allowed=false` 保持不变。

> **2026-10-03 foundation audit 收口**：`scripts/verify_current_progress_index.py` 现在会重新打开
> `docs/evidence/v030-pre1-foundation-20261003-r7.json`，校验 receipt canonical/file SHA-256、
> 当前 parity、全部 foundation source hashes、八项 Runtime/Clock/Profile/Backend/Scenario/Task/
> Replay/Simulator checks 以及 truth-free/无 actuator/final-release fail-closed 边界。索引输出
> `v030_pre1_foundation_bound=true`；对应正向与 source-drift 负向回归已通过。该审计仍只证明
> 当前源码的软件基础，不替代 QGC 人工视觉、HIL、REAL、真实硬件、室外 GNSS 或最终 `0.3.0` 门。

> **2026-10-03 QGC 当前 Release 构建（本轮新增）**：在完整 MSVC x64 开发环境中对
> `build/windows-v0.3.0-pre.8-release` 做了增量 Release 构建，QML cache 重新生成，
> `MultiDetectUi.lib` 与 `MultiDetectGCS.exe` 均重新链接。当前 EXE 为
> `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe`，46884864 bytes，
> SHA-256=`6f0403cfb29ec6b13d0b5b684a1787327c85db6d91bc5b4662fde867581af37f`；
> 同一 EXE 已通过绝对路径 Qt deploy 生成 portable 目录
> `build/windows-v0.3.0-pre.8-release/portable-current-20261003-qml-hitfix-abs/bin/MultiDetectGCS.exe`，
> hash 与 Release EXE 一致，Qt/GStreamer 运行库存在。构建证据为
> `artifacts/evaluation/qgc-build-20261003-qml-hitfix.json`。这只证明源码构建与运行目录部署，
> 不宣称 QGC 人工视觉/Mission、HIL、REAL、硬件、室外 GNSS 或最终 `0.3.0` 发布；下一项
> 深度/距离验收仍需使用真实 metric-depth model calibration，不能把本次 software SIM machine
> smoke 当作室外定位精度证据。

> **历史增量（2026-10-03 QML delegate-hit r4）**：关闭残留的 Windows `TabTip`/Shell
> Handwriting Canvas 遮挡后，使用同一当前 Release EXE 与当前 `release-parity.json` 重跑 pinned
> PX4/Gazebo/QGC 组合验收。900 帧 integrated SIM、native video surface、toolbar/drawer machine
> smoke、精确 PID/HWND `SELECT_TRK`、SelectionAck/TrackStatus adoption、VTOL Mission
> upload/readback/commit/permit、420.016 秒 router 保持窗口与 container cleanup 全部通过；完整
> 源证据为 `artifacts/evaluation/combined-sim-qgc-20261003T021801Z-40300/combined-evidence.json`
>（3,716,766 bytes，SHA-256=`5f3c9f281787d567480729d25e4731c34f9753826c5c756b9a5bcff6a52d2925`），
> 简明索引为 `docs/evidence/combined-sim-qgc-20261003-r4.json`。证据仍只将 QGC 标为 machine
> smoke；`complete_qml_rendering_semantics_proved=false`、人工视觉验收未宣称。
>
> **此前权威增量（2026-10-03 QML delegate-hit r3）**：本轮修正了候选框命中检测对
> `QQmlListModel.itemAt()` 的错误调用，改为使用同一模型 `Repeater.itemAt()` 的实际 delegate
> 几何；随后重新构建 QGC，native `SELECT_TRK` 已在外部组合运行中收到并完成
> `selection_acknowledged → track_status_received → track_status_adopted`，PX4 Mission
> upload/readback/commit/start 也已进入成功路径。完整组合证据已通过并写入
> `artifacts/evaluation/combined-sim-qgc-20261003T013646Z-15924/combined-evidence.json`
>（3,531,198 bytes，SHA-256=`ee376b2fecc28409ed55ad156b7a690c3c9b2af8c0a18530ac24a07508049f8c`），
> 简明索引为 `docs/evidence/combined-sim-qgc-20261003-r3.json`。该证据现作为 r4 前的历史
> successful run 保留；当前权威组合证据以 r4 为准。
>
> **此前权威增量（2026-10-03 depthguard-r2-model-assets）**：本轮修正了两个会制造假一致的
> 问题。第一，Gazebo `optical_axis_z_m` wire sensor identity 不再被当作
> Depth Anything metric-depth model calibration；loader 与组合入口均 fail-closed，
> 原错误 identity 文档已隔离到 `artifacts/evaluation/`。第二，QGC 原生
> `SELECT_TRK` 命中判断优先使用实际渲染 delegate 的几何，而不是只依赖可能漂移的
> letterbox/归一化 bbox。此前组合运行在发送 SELECT 前记录 `stable_hit=false`，
> 因此不能算视觉验收；修正后尚未重新进行带真实模型标定的完整组合运行。
>
> 该历史段落记录的 `configs/release-parity.json` 为 12,749 bytes，SHA-256=
> `cb45e34c59463346d56991d489a7d0e838d7841a50f71887c73f2b5fd837892f`，
> `core_algorithm_hash=de997b7833d70bc7f424c210e4520ebe53d33c0aec20c9b039fb324e3bc1db41`，
> `ui_contract_hash=ddb3b9088cfbd411c963baea376e9de69b6c6f0a8064eeff8959c7b1fe745896`。
> 当前 source-bound VTOL sensor evidence 为
> `artifacts/evaluation/continuation-shared-policy-vtol-sensor-20261003-qml-r1.json`；
> 当前 fixed-wing/multirotor 深度证据已按仓库现有模型资产重绑定到
> `artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261003-qml-r2.json`
> 和 `artifacts/evaluation/external-multirotor-depth-shared-policy-20261003-qml-r2.json`；
> external mission matrix 为 `docs/evidence/external-mission-matrix-20261003-qml-r4.json`，
> retained-file audit 为
> `docs/evidence/shared-policy-release-parity-policy-helper-20261003-qml-r2.json`。
> AI-flight retained-layer receipt 为
> `artifacts/evaluation/simulator-ai-flight-parity-20261003-qml-r2.receipt.json`；
> 这些是保留证据的可重验重绑定，不冒充新的外部运行。当前审计仍为 32 blockers，
> `gate_passed=false`、`release_allowed=false`；没有 HIL、REAL、硬件、室外 GNSS、
> QGC 人工视觉/Mission 验收或最终 `0.3.0` 发布声明。

> **2026-10-03 fresh disposable PX4/Gazebo sensor transport run（当前新增）**：使用 pinned
> image `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
> 启动隔离容器，实际观察到 PX4 heartbeat、`GPS_RAW_INT`（fix 3、10 satellites），并由
> Gazebo wire-v3 采集 3 帧 camera/camera-info/NavSat/full odometry/covariance。证据为
> `artifacts/evaluation/continuation-docker-sensor-20261003.json`（92,854 bytes，SHA-256=
> `6f7ceec973fe260276079253644b69d11325f15354974b6336e3ba79f536be48`），帧 manifest 为
> `artifacts/evaluation/continuation-docker-sensor-frames-20261003/gazebo-bgr24-frame-set.manifest.json`
>（21,580 bytes，SHA-256=`05726346d46d82842c7a327ec1411a16abebbe6a18b0c8f1726731e209033614`），
> 容器日志 SHA-256=`03bd3737ddb082e3486447bc7d43934a2669b23d051a5f48cf27fa86d96664f6`。
> `container_runtime`、telemetry、sensor transport、pixel persistence 和 stop/remove cleanup
> 全部通过；3/3 原始 BGR24 payload 与 wire payload SHA 一致。该次运行是被动 GCS telemetry
> 传感器证据，没有 Mission/ARM/actuator 指令，也没有 SharedTaskPolicy task binding；不宣称
> QGC 人工视觉、HIL、REAL、真实硬件、室外 GNSS 或最终 `0.3.0` 发布。当前 `0.3.0-pre.8`、
> `gate_passed=false`、`release_allowed=false` 保持不变。

> **2026-10-03 fresh SharedTaskPolicy campaign + task-bound same-container wire-v3 capture（当前新增）**：
> `--capture-campaign-sensor-frames` 组合入口现在把第一条 sortie 的既有 live MAVLink
> fixed-wing barrier、同一 exact Docker container 的 3 帧 Gazebo wire-v3
> Image/CameraInfo/NavSat/Odometry、completion marker 和首架次任务 descriptor 串成一条可重验链；
> descriptor 同时绑定 `synthetic-east`、transaction id、plan/executable Mission/wire hash，随后
> campaign 继续 `synthetic-west`、降落并清理。当前 source-bound evidence 为
> `artifacts/evaluation/continuation-shared-policy-vtol-sensor-20261003-qml-r1.json`
>（6,473,051 bytes，SHA-256=`8f4fe4aa37a935d992766f2e66746b7b31f1af5f4258b42997e37e7f8233be76`）；
> gates、parity 起止快照、Mission journal/hash-chain、task binding、像素持久化和 exact-ID cleanup
> 均通过。当前 parity 文件 SHA-256=`cb45e34c59463346d56991d489a7d0e838d7841a50f71887c73f2b5fd837892f`，
> `core_algorithm_hash=de997b7833d70bc7f424c210e4520ebe53d33c0aec20c9b039fb324e3bc1db41`，
> `px4_mission_path_hash=5dc254410484f66bdcba798e4eb3b19baa682632abe38242e8060e5377800d07`。
> 这仍是 headless PX4/Gazebo 软件 SIM 证据：不宣称 QGC 人工视觉、HIL、REAL、真实硬件、
> 室外 GNSS 或最终 `0.3.0` 发布；该证据自身的 campaign gates 已通过，但全局 `release_allowed=false`
> 仍保持不变。

> **2026-10-03 source-bound parity rebind（当前索引校验已通过）**：r14 之后把
> `run_vtol_mission_sensor_capture.py` 纳入 `px4_mission_path` parity source，随后对保留的
> observed session、训练 artifact receipt、VTOL/parent/candidate campaign receipts、fixed-wing/
> multirotor depth evidence、三机型 vehicle matrix 和 AI-flight retained-layer receipt 做了
> 可恢复的 current 副本重绑定。深度证据使用当前模型 provenance 的 r2 文件；三机型矩阵为
> `artifacts/evaluation/shared-policy-vehicle-matrix-20261003-qml-r3.json`，AI
> retained-layer receipt 为
> `artifacts/evaluation/simulator-ai-flight-parity-20261003-qml-r2.receipt.json`。
> `python scripts/verify_current_progress_index.py --root . --check-sources` 与 648 项定向回归
> 通过。该步骤没有把 source-bound 重算冒充新外部运行；release audit 仍为 32 blockers、
> `release_allowed=false`，HIL/REAL、硬件、室外 GNSS、QGC 人工视觉和最终 `0.3.0` 仍未通过。

> **2026-10-03 release-gate current parity recomputation（当前权威）**：没有覆盖历史 report，
> 当前索引绑定的 report/manifest 为
> `artifacts/evaluation/release-gate-current-20260927-r12/release-gate-pre.8-current-20260927T005000Z-v6-bound-report.json`
> / `artifacts/evaluation/release-gate-current-20260927-r12/release-gate-pre.8-current-20260927T005000Z-v6-bound.json`
>（report 71,805 bytes，SHA-256=`a2fb4b0d84c3bce54c466df622bc09739b163d23163f6124a6b607daa1ad33fe`；
> manifest 23,654 bytes，SHA-256=`522727d78b9ff97fc5ed5f5f2a0f05ae83fdf7bbea3d19024cee934b4627ced5`），
> audit 为 `docs/evidence/shared-policy-release-parity-policy-helper-20261003-qml-r2.json`
>（5,545 bytes，SHA-256=`565ff0aefa87e9550ea623c6f9fdc62de226296078c9c30aeb9c1c437fccb21`）。
> audit 的四个 mode parity mismatch 已全部归零，`current_release_parity_bound=true`；同时
> 重新绑定的 external-SIM join 与 PX4 response matrix/campaign 均为 source-bound 派生物，
> 不声称旧 runtime 被重新执行。历史 release-gate field-link 使用
> `artifacts/evaluation/field-link-preflight-current-20260927-r2.json`，独立 current probe 使用
> `artifacts/evaluation/field-link-preflight-current-20260928-r2.json`，两者均
> `hardware_link_ready=false`。真实 blocker 仍为 32 个：synthetic replay accuracy/domain、
> field-link/hardware、QGC visual/Mission、HIL/REAL、室外 GNSS 和最终 release channel 继续
> fail-closed，`0.3.0-pre.8`、`gate_passed=false`、`release_allowed=false` 不变。

> **2026-10-03 shared task-policy runtime binding（当前权威）**：新增
> `src/multidetect/autonomy/task_policy_runtime.py`，把 SharedTaskPolicy 的
> select/abstain、policy artifact、ranker、airframe profile、planner/mission/wire hash
> 固定为跨模式只读 runtime binding。`run_observed_task_session`、确定性
> `SimulatorScenarioRunner` 和 VTOL campaign 都产出同一绑定；select 必须经过 planner
> 重验并绑定 Mission，abstain 不得绑定 Mission。绑定明确保持
> `mission_started=false`、`policy_applied_to_actuator=false`、`truth_free=true`，所以它是
> SIM/REPLAY/HIL/REAL UI 的状态数据，不是执行授权或飞控指令。新增回归覆盖 selected、
> abstain、profile mismatch 和 observed-session evidence。当前 parity 文件 SHA-256=
> `b870011d3e72b66d9b44372a697341410afbc1a84b07c4eecc12538108f3c543`，
> `core_algorithm_hash=1b25830a24bb28a89d9e864fde1af608355f3dfdc1cae9fb7b830223341c270f`，
> `task_contract_hash=72d786faddee41aa1de292d481eb0b1f285d44a01201db675d8c4a2acc4654ea`，
> `px4_mission_path_hash=1bf102d00e3a7961caf27b652fc14013b25197eccecdc5a7f2386d739fb50bc5`，
> `safety_policy_hash=034513452a20724f16b9fe49a44ed938386be5dd74d2b121f1c6797092ba254d`，
> `ui_contract_hash=948297805f4bd6cebacd9f6c2ff46146a43c8fba41af1a30cc942876e4a1578e`。
> 当前 operator-link/bridge/QGC C++/QML 增量 Release 构建也已通过，EXE 为
> `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe`（46,884,864 bytes，
> SHA-256=`7cef06c6c459a7b70582aabba6940cf156f33bba8cd29b5e05f3a4ae7a334b22`），构建证据为
> `docs/evidence/qgc-build-20261003-task-policy-runtime-r1.json`；这只证明 C++/QML 链接，未启动 EXE。
> 现有 C++ `MultiDetectOperatorProtocolSelfTest.exe` 也已重编并运行通过（exit 0），用 112-byte
> type 27 `SELECT` golden vector 验证 action、planner/Mission、安全和 display-only 字段；证据为
> `docs/evidence/qgc-protocol-selftest-20261003-task-policy-runtime-r1.json`。
> current-progress verifier 已通过；最新 release-gate report 为
> `artifacts/evaluation/release-gate-pre.8-current-20261003T011500Z-v6-bound-report.json`
>（71,941 bytes，SHA-256=`30094ca64c3dd8cb9973091d84a1a29c9493a81ec9b756d8d952fb662b44220c`），
> manifest 为 23,699 bytes、SHA-256=`df8f2eb80cebca4c57d1f15ebf193cec498c3d69ac4618fc29e5bcef8ffdd87c`，
> audit 为 `docs/evidence/shared-policy-release-parity-drift-20261003-task-policy-runtime-r7.json`
>（4,868 bytes，SHA-256=`2cc2314ba27ddfc2a339252409a9e0793ff5ba936fbb3565705d18b3574a2282`）。
> 这次只更新软件契约和 source-bound 索引，没有新增外部运行；当前仍为
> `0.3.0-pre.8`、`gate_passed=false`、`release_allowed=false`，没有 HIL/REAL、真实硬件、
> 室外 GNSS、人工 UI/Mission 验收或最终 `0.3.0` 发布声明。

> **2026-10-03 fresh SharedTaskPolicy external VTOL campaign r2（当前新增）**：使用当前
> `configs/release-parity.json`（起止快照均为 SHA-256=`b870011d3e72b66d9b44372a697341410afbc1a84b07c4eecc12538108f3c543`）和同一个
> `PolicyArtifact`，通过现有 `run_vtol_mission_sensor_capture.py` lifecycle 在 pinned
> PX4/Gazebo disposable container 中完成 `synthetic-east`、`synthetic-west` 两架次。
> 两架次均通过 Mission upload/readback/commit/permit、journal hash-chain、policy evidence
> 和 planner revalidation；容器 exact-ID cleanup 与 post-remove absence 均通过。evidence 为
> `artifacts/evaluation/continuation-shared-policy-vtol-20261003-r2.json`（32,219 bytes，
> SHA-256=`c61eab7de6788afffcf549d07673bd050bdc4e9145726e867eb33f60c3516c3b`），runtime-bound
> receipt 为 `artifacts/evaluation/shared-policy-continuation-vtol-20261003-r2.receipt.json`
>（canonical SHA-256=`63f5f8d7e6a832dedf1be789b6a9ad420f62267b259b70005d35ffe1c4dcd6c4`）。
> 这是新的 external software SIM Mission 证据；本轮没有启动 QGC、没有采集 wire-v3 相机帧，
> 所以不宣称人工视觉、HIL、REAL、真实硬件、室外 GNSS 或最终 `0.3.0` 发布。

> **2026-10-02 context-profile parity guard + source-bound r6（当前权威）**：`SharedTaskPolicy`
> 现在对带有 `ContextSnapshot.airframe_profile_hash` 的上下文执行 fail-closed profile hash 校验；
> 缺失 hash 仅保留 v1 兼容，存在且不匹配会拒绝策略决策。AI parity receipt builder 同时要求
> runtime snapshot 的 `bytes/file_sha256/hashes/release_label` 与当前
> `configs/release-parity.json` 精确一致；稳定但旧的快照只会降级为
> `runtime_parity_snapshot_verified=false`，不会升级成当前源码运行证明。对应 policy/AI stale
> snapshot 回归测试已通过。
>
> 当前 parity 文件 SHA-256=`13190f161b5f7aea54b2fb2c7663b572b3f0fb93ab4240c223fac22aec6beef3`，
> `core_algorithm_hash=fdfeb74e398879e70be0fd0ad367e1b1ec8712c1b9a5df717ccc718b22b0d4bd`，
> `task_contract_hash=2d50c2a87001f720788cbb4e1668610f802e750186785908a48a15eed2de6c6c`，
> `px4_mission_path_hash=02f8f33e024370cefa540bd717cefc6c49e0635f383ec31be2e6a34e430b6da2`。
> 当前 source-bound external join/revalidation、PX4 response matrix/campaign、AI receipt 均绑定
> `context-profile-r6`；本轮没有新增外部 runtime。release-gate report 为
> `artifacts/evaluation/release-gate-pre.8-current-20261002T145406Z-v6-bound-report.json`
> （71,911 bytes，SHA-256=`b95d9c14d9f1a40d3aaaa8c37ca210ab1a3bcfc9fa40f2ddb07c86f74168bf34`），
> manifest 为 23,683 bytes、SHA-256=`b4a8810995402a7f0cdde8386651c2fd3e17ea090a2c54c13355576452bca55f`，
> audit 为 `docs/evidence/shared-policy-release-parity-drift-20261002-context-profile-r6.json`
> （4,868 bytes，SHA-256=`5f4458ed9922479a7fb341b57bb9c5e1d41ec1a08f2c5a7e2bc344c3850eb0e4`）。
> 当前仍 `gate_passed=false`、`release_allowed=false`、32 blockers；没有 HIL/REAL、真实硬件、
> 室外 GNSS、人工视觉/Mission 验收或最终 `0.3.0` 发布声明。

> **2026-10-02 fresh external SIM r6 + parity receipt r8**：修复 disposable PX4/Gazebo 启动时
> `px4-param` 过早执行导致的 `PX4 server not running` race：仅对该启动态错误做 20 次、0.5 秒
> 间隔的 bounded retry，其他 preflight 错误仍立即 fail-closed；新增启动 race 回归测试。
> 修复后在固定 digest 的 `river_rescue_vtol_001` 容器中真实重跑 AI flight：armed、truth-free
> `TRACK`、目标刷新 `47/47`、最大间隔 `0.125s`、`GLOBAL_POSITION_INT` 位置样本 `182`、
> 最大水平位移 `1.981104m`、runtime/AI/target/flight gates 与 cleanup 全部通过。原始 evidence 为
> `artifacts/evaluation/simulator-docker-ai-flight-20261002-gnss-display-r6.json`（SHA-256=
> `8215bedc10ef0e8d5e03e489de324e980e495598f03160c56831fd62438fa1bd`），composite receipt 为
> `artifacts/evaluation/simulator-ai-flight-parity-20261002-gnss-display-r8.receipt.json`，
> tracked summary 为 `docs/evidence/simulator-ai-flight-parity-20261002-gnss-display-r8.json`。
> r8 明确 `same_release_parity=true`、`current_runtime_parity_proven=true`，但仍仅是 external
> 软件 SIM；不代表 Mission upload/commit、HIL、REAL、真实硬件、室外 GNSS、视觉精度或最终发布。

> **2026-10-02 historical/current field-link separation r8**：完整回归发现上一版 12:00 report
> 把 current r3 预检同时当成 historical release-gate field binding，导致历史/当前证据身份
> 不再分离。没有覆盖历史文件；改用保留的
> `artifacts/evaluation/field-link-preflight-current-20260928-r2.json` 作为 historical report，
> 继续把 `artifacts/evaluation/field-link-preflight-current-20261002-r3.json` 作为 current probe，
> 重新生成 13:00 report/manifest 与 audit r8。当前 report 为 71,899 bytes，SHA-256=
> `1281d38ab8454c36805c72c9718a5920cd27211c9585114ca6d3c0b105f220f8`；manifest 为 23,675 bytes，
> SHA-256=`8c75f8a31ed22208d025082b0ca84ca8008f79558552b609dc0e4e4705c85894`；audit 为
> `docs/evidence/shared-policy-release-parity-drift-20261002-gnss-display-r8.json`（4,868 bytes，
> SHA-256=`d485fa095dcdffaa38b406850fcbba6b9b8744b3956ba9a4d7f5f644fceddf59`）。历史/当前 field
> binding 已分离，audit generator 现在也会拒绝同文件或同 SHA 的 current/historical 绑定；当前仍
> `gate_passed=false`、`release_allowed=false`、32 blockers；新增完整回归的唯一失败已修复。

> **2026-10-02 read-only field-link refresh r3 + release-gate r7**：重新执行 3 个只读预检样本，
> Jetson SSH、GR01 MAVLink 和相机 RTSP 均超时；本机 UDP 绑定成功，但 `Ethernet` 选择到的
> `192.168.1.13/24` 不在期望的 `192.168.144.0/24`，因此 `hardware_link_ready=false`、
> `all_passed=false`，没有把现场链路写成硬件/HIL/REAL 证据。当前原始预检为
> `artifacts/evaluation/field-link-preflight-current-20261002-r3.json`（18,146 bytes，SHA-256=
> `91f1ed459990e373cdcae2c47daaa960ee9f56d78268cc0e19f3f1c5ef6dc87f`），interface diagnostic
> 仍为只读、无 readiness claim 的独立文件。同步生成的当前 release-gate report 为
> `artifacts/evaluation/release-gate-pre.8-current-20261002T120000Z-v6-bound-report.json`
> （71,676 bytes，SHA-256=`e30143f0982dfc11e0c1c449c34602a760d5f05db037239d79f81f1a44123d5a`），
> manifest 为 23,591 bytes，SHA-256=`ccefb4eefca1909b3dde521f79f288f6f1b78bdbcc2f2fdea72c1e23f956155c`；
> current audit 为 `docs/evidence/shared-policy-release-parity-drift-20261002-gnss-display-r7.json`
> （4,754 bytes，SHA-256=`8e31cd64bd642af056be0d4482b6742bed445df9723b3677b9f94fa52b2ac549`）。
> freshness 旧阻塞已清除，当前仍 `gate_passed=false`、`release_allowed=false`、31 blockers。
> 同时修复 audit generator 对 raw `verified` 与 projected `gate_passed` 两种 interface-diagnostic
> 语义的兼容处理：raw 必须重新验证，projection 存在时必须严格 hash/path/bytes 交叉匹配。

> **2026-10-02 source-bound refresh r28/r6**：上一轮 acceptance runner 增加了 PX4 启动 race 的
> bounded retry 后，旧 r27 matrix/campaign 的 `source_tools.acceptance_runner` hash 已不再匹配当前源码；
> 因此保留 r27 不覆盖，并从相同 retained source 重新生成 r28。matrix 文件 SHA-256=
> `39089a7c754746659e6aab328cea141aaa45f6834e02fe3fd8299dfed28391de`、canonical matrix SHA=
> `59feb1c40e8ab7a900a6fb3d432005737c8840fe0d4f6b2f23852c886d657463`；campaign 文件 SHA-256=
> `ba5d82fbb86816c936cca7f85fe3c8377280e647067c052209c2c89bfba39946`、canonical campaign SHA=
> `7b4abd99202b6d6b73019ae6ce150de90a634917a532dab2c5a5f1ed36203dfe`。两个 verifier 均通过，
> 这只是 source-bound 重算，不是新增外部运行。三机型 external join/revalidation 也更新到 r28，
> 随后生成 `artifacts/evaluation/release-gate-pre.8-current-20261002T092000Z-v6-bound-report.json`；
> report SHA-256=`f19607876ec83ee4194b596c2cdefcdc363da394a7b7e720b35d09fd67f22545`，manifest SHA-256=
> `e4d1bcba00b5e7c62ebefc40346efe008eb1e6daa7c4dca8279292b815e57c10`，当前 audit 为
> `docs/evidence/shared-policy-release-parity-drift-20261002-gnss-display-r6.json`，
> `current_release_parity_bound=true`，但整体仍 `gate_passed=false`、`release_allowed=false`、32 blockers。
> replay-set、field-link/硬件、人工视觉/Mission、HIL/REAL 与最终 release 门继续保持 fail-closed。

> **2026-10-02 PX4 response matrix/campaign 与 release-gate source-bound r27**：基于当前
> `configs/release-parity.json`（文件 SHA-256=`58f5ab09eb8f28a8351fdf520399d75162c85430236c46eb186429c2242cf019`），
> 从保留的 6 个正向 Docker/PX4/Gazebo response 和 9 个 fail-closed fault source 重新生成
> `artifacts/evaluation/px4-reported-position-response-current-parity-20261002-gnss-display-r27/`。
> matrix 文件 SHA-256=`361716a56c366d9f6ca98c49cf1434f545dba036b5ded2f539b0a1e73179f9b3`、
> canonical matrix SHA=`5891870ff6bd4f8969664e7ebb683140064bce7cd93a80165098525806c84bfd`；
> campaign 文件 SHA-256=`7edc1aa6215e2749474617a07aa0763ea92c57de79df5550dbde5258f147c657`、
> canonical campaign SHA=`20c55a6f0d802df98985a9b2b61aed4e51a430effc0b62299726e8799c86abd6`。
> 两个独立 verifier 均通过，且只重算/重绑定保留 source，不声称本轮新增外部运行。
> 随后 `run_current_release_gate_pre8.ps1` 生成 report-only：
> `artifacts/evaluation/release-gate-pre.8-current-20261002T052000Z-v6-bound-report.json`，
> 报告 SHA-256=`a21256c02bc2a9a035d467ca6dc222bd98fe74579bb9164ae4f3f1e609e5abbc`，
> manifest SHA-256=`bacd4af76502ed8917112d87cb2a91f1937ee4da12f711daf61ede8d103573c6`，
> 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；新的 audit
> `docs/evidence/shared-policy-release-parity-drift-20261002-gnss-display-r5.json`
> 已确认四个 mode 的 parity identity 当前一致（`current_release_parity_bound=true`）。
> replay-set 质量、field-link 新鲜度/硬件、人工视觉/Mission、HIL/REAL 与最终 release 门仍保持 fail-closed。

> **2026-10-02 当前 parity 绑定与 fresh external join（r3/r4/r5）**：本轮先重新生成
> `configs/release-parity.json`，因为加入 runtime snapshot 锁定后源码 parity 发生变化；当前文件
> SHA-256=`58f5ab09eb8f28a8351fdf520399d75162c85430236c46eb186429c2242cf019`，其中
> `px4_mission_path_hash=02f8f33e024370cefa540bd717cefc6c49e0635f383ec31be2e6a34e430b6da2`。
> 随后 fresh SharedPolicy VTOL campaign r3 在两架次中完成同一 Mission upload/readback/commit/permit
> 生命周期，起止 parity snapshot 稳定、cleanup 已证明；AI flight r4 也在同一当前 parity 起止点锁定，
> PX4 armed、truth-free target、flight response 与 cleanup 全部通过。新的 composite receipt
> `artifacts/evaluation/simulator-ai-flight-parity-20261002-gnss-display-r5.receipt.json` 已证明
> `same_release_parity=true`、`current_runtime_parity_proven=true`。这仍只是 `0.3.0-pre.8`
> 外部软件 SIM 的当前同版证据，不是 HIL、REAL、真实硬件、室外 GNSS、视觉精度或最终 `0.3.0`
> 发布证据；`candidate_promoted=false`、`final_release_allowed=false` 保持不变。

> **2026-10-02 current-progress source revalidation**：release parity 变化后，固定翼/多旋翼、VTOL
> retained-file receipts、runtime matrix、observed session、three-vehicle Mission matrix、训练
> artifact receipt 和 AI parity audit 均生成了不可覆盖的 source-bound 副本；原始运行文件与历史
> receipt 保留。`python scripts/verify_current_progress_index.py --root . --check-sources` 已完整通过，
> 当前审计为 `docs/evidence/shared-policy-release-parity-drift-20261002-gnss-display-r3.json`，
> 但 release gate 仍明确 `release_allowed=false`。

> **2026-10-02 SharedTaskPolicy Mission 与 AI flight 的当前 runtime parity join（r4）**：
> 新的 SharedPolicy VTOL campaign 在同一外部 PX4/Gazebo disposable 生命周期中完成两架次
> Mission upload/readback/commit/permit；运行起止均锁定当前 `configs/release-parity.json`，快照
> 稳定且 cleanup 已证明容器移除。SharedPolicy receipt 为
> `artifacts/evaluation/shared-policy-current-vtol-campaign-20261002-gnss-display-r2.receipt.json`，
> composite receipt 为 `artifacts/evaluation/simulator-ai-flight-parity-20261002-gnss-display-r4.receipt.json`。
> r4 已证明 `same_release_parity=true`、`current_runtime_parity_proven=true`，两层快照均为 true；
> 仍保持 `policy_applied_to_actuator=false`、`candidate_promoted=false`、`final_release_allowed=false`。
> 这只是同一 `0.3.0-pre.8` 外部软件 SIM parity 证据，不是 HIL、REAL、真实硬件、室外 GNSS、视觉精度
> 或最终 `0.3.0` 发布证据。

> **2026-10-02 external PX4/Gazebo AI flight r2 已通过**：在固定 digest 的 disposable
> `river_rescue_vtol_001` VTOL 容器中，使用现有共享 AI/控制路径执行 `--exercise-arm
> --exercise-target --exercise-flight-seconds 5`。补齐持续飞行参数后，PX4 实际观察到 armed；
> truth-free 目标注入进入 `TRACK`，5 秒持续刷新接受 `47/47`，最大刷新间隔 `0.125 s`，
> flight-response gate、runtime、GPS、sensor transport 和 cleanup 全部通过；本轮未请求 Mission
> upload/commit，因此不把 `mission_transaction_gate_passed=true` 解读成 Mission 交易已执行。原始本机证据为
> `artifacts/evaluation/simulator-docker-ai-flight-20261002-gnss-display-r2.json`，SHA-256=
> `4a27af6b7178a685dd479f9035579f713d306d1dc6817905bedb206e1e950d52`；可提交摘要为
> `docs/evidence/simulator-docker-ai-flight-20261002-gnss-display-r2.json`。这只是当前软件
> external SIM 的 flight-response 证据，不是 Mission upload/commit、HIL、REAL、真实硬件、室外 GNSS、视觉精度或最终
> `0.3.0` 发布证据；`candidate_promoted=false`、`final_release_allowed=false` 保持不变。

> **2026-10-02 AI flight runtime parity lock（r3）**：AI external acceptance 在运行开始和结束
> 各捕获 `configs/release-parity.json`，文件 SHA-256 都是
> `abba65fff6ef77e38d292b95ce523219a15c9de9ead7a218fa10c8f2235a7d7d`，五项契约 hash 也完全
> 一致；runtime snapshot gate 通过。PX4 armed、truth-free `TRACK`、目标刷新 `45/45`、最大间隔
> `0.25 s`、位移 `1.513558 m` 和 cleanup 均通过。摘要为
> `docs/evidence/simulator-docker-ai-flight-20261002-gnss-display-r3.json`。
> 但 SharedTaskPolicy Mission 层尚未有同等运行时起止快照，所以 composite join r3 仍明确
> `same_release_parity=false`、`current_runtime_parity_proven=false`；下一步必须重跑该 Mission
> campaign 并加同样锁定，不能把 AI 单层锁定升级成整条主线的同版运行证明。

> **2026-10-02 AI flight 与 SharedTaskPolicy retained-layers join（r2）**：使用现有严格工具
> `scripts/create_simulator_ai_flight_parity_receipt.py` 将当前 parity 绑定的 SharedTaskPolicy
> VTOL receipt 与 AI flight r2 证据做一致性审计。scenario manifest、VTOL airframe、planner
> revalidation、truth-free 边界和 cleanup 均一致；高层策略仍没有 actuator 权限。审计摘要为
> `docs/evidence/simulator-ai-flight-parity-20261002-gnss-display-r2.json`，但结果明确为
> retained-layers file audit：`same_release_parity=false`、`current_runtime_parity_proven=false`，
> 因为两次历史运行没有在运行时起止点锁定 parity 快照；不能把它写成同一版 runtime 已执行。
> 下一步必须先补运行时 parity snapshot，再重新生成 join；HIL/REAL、硬件、室外 GNSS 和最终
> `0.3.0` 发布门继续关闭。

> **2026-10-02 QGC 原生窗口启动观察（r1）**：从当前 Release 目录启动
> `MultiDetectGCS.exe --offline --simple-boot-test --allow-multiple --no-windows-assert-ui`，
> 观察到真实 `MultiDetectGCS` 窗口创建、响应，并由关闭请求正常退出。证据为
> `docs/evidence/qgc-runtime-window-smoke-20261002-r1.json`。由于 Windows GUI 进程调用返回
> 与子进程生命周期不能作为严格 simple-boot exit contract 的充分证明，索引明确保持
> `simple_boot_exit_contract_verified=false`、`manual_visual_review=false`；本轮没有连接飞控，
> 没有重跑 PX4/Gazebo、HIL/REAL、硬件或室外 GNSS，`release_allowed=false` 不变。

> **2026-10-02 QGC 增量构建完成（GNSS display r1）**：使用 Visual Studio 2022
> BuildTools Developer 环境，在 `build/windows-v0.3.0-pre.8-release` 中以 Ninja Release
> 并行 2 完成当前源码增量构建。EXE 为
> `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe`，大小 `46,877,184` bytes，
> SHA-256=`cddb0f08d5707b56139a1d69f535bef10607beae7e6229152990649c7470879f`，File/Product
> version=`0.3.0.0`。构建证据为
> `docs/evidence/qgc-build-20261002-gnss-display-r1.json`；本轮没有启动 GUI，
> 没有重跑 PX4/Gazebo、HIL/REAL、硬件或室外 GNSS，`release_allowed=false` 保持不变。

> **2026-10-02 GNSS 显示 fail-closed 收口（r1）**：QGC 实时目标落图和文字卡片现在
> 与保存快照使用同一条原子 GNSS 质量/身份门；GNSS 缺失、过期、弱 fix 或 sequence/epoch
> 不一致时只显示“不可定位”，不会保留坐标点或不确定性圆。新增回归为
> `tests/test_qgc_native_flyview_style.py`；UI parity 文件 SHA-256=`abba65fff6ef77e38d292b95ce523219a15c9de9ead7a218fa10c8f2235a7d7d`，
> `ui_contract_hash=b6d4a310ce9fde6df4ccdf3f9605bd1f1bd0155fda657ad4d9ca3f54ce197690`。
> source-bound transition 审计为 `docs/evidence/shared-policy-release-parity-drift-20261002-gnss-display-r1.json`
>（SHA-256=`bfef7c183cf410d3b9195de1a2e7d61187ca520c5950060d86c606d2305d3663`）；没有重跑
> PX4/Gazebo、HIL/REAL、硬件或室外 GNSS，`release_allowed=false` 保持不变。

> **2026-10-02 geolocation transport contract 收口（parity transition r2）**：修复
> `SensorTransportManifest.selection_mapping()` 已输出但 accuracy verifier 白名单未接纳的
> `scenario_manifest_hash` 漂移；现在该字段可选但若存在必须是合法 SHA-256，生成端和验证端契约一致。
> `tests/test_geolocation_survey.py`、`tests/test_geolocation_accuracy.py`、current-progress、
> AI receipt schema 与完整 `pytest` 均通过。由于这是 core source 变化，按只读保留证据原则重新生成
> `configs/release-parity.json`：文件 SHA-256=`7baac791e90e5226c23d22faf0d1b6ba800747fb292801758fafb7d6b09abed7`，
> `core_algorithm_hash=84085b3a68b975dc52ccf51e70e68dc933c592b9ba8af5845617e213cc6189a9`。
> 新索引审计为 `docs/evidence/shared-policy-release-parity-drift-20261002-geolocation-r2.json`
>（SHA-256=`804b11e271daf265b2db32cf1599c724871f86fc1402fd741031c343a1e08f58`），并保留旧 receipt/
> matrix/audit 文件；这是 source-bound 文件审计，不是新一轮 PX4/Gazebo 运行。`release_allowed=false`、
> `hil_real_evidence=false`、`outdoor_gnss_evidence=false`、最终 `0.3.0` 发布门继续关闭。

> **2026-10-02 advisor 上下文身份绑定（r6）**：`ContextSnapshot` 现在可以携带可选的
> `airframe_profile_hash`；旧 v1 context 仍兼容读取，训练 loader 会把 case hash 注入旧格式，
> 已有 hash 则必须与 case、策略和 bounded sequence reference 的输入一致，否则 fail-closed。
> 新 source-bound 报告 `artifacts/evaluation/sequence-proposal-independent-holdout-20261002-r6.json`
> （22,951 bytes，SHA-256=`6bfdde05e7b3e23b152a4191f6297eed325524eb44fb8bf886497c21a12c9ebd`）显示
> 12 cases baseline/reference `120/120`、objective tie `12`；这只收紧 advisor identity，未产生
> candidate gain，也不代表 external PX4/Gazebo、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0`。

> **2026-10-01 当前 source-bound 索引（r30/r32/r31/r26）**：在当前源码和 parity 下
> 实际运行 fixed-wing 与 multirotor 各一架 disposable PX4/Gazebo Mission；两架均使用同一
> SharedTaskPolicy/ranker、同一 Mission compiler/transaction、同一传感器 transport，均请求并
> 收到 2 帧 depth companion。重新计算并写回当前
> `configs/release-parity.json` 后，文件 SHA-256 为
> `84a240857821db3f97876578b53da24879c57196980eea19a566288dac83baa2`，其中
> `core_algorithm_hash=afa87110e5a638c4003b0b9760520a29709ae01a340a61112d9b4fbd6af51dfd`，
> `px4_mission_path_hash=91a867b1c89c68c50ac11e1dc6941cd0015171cb9338744adc95a63531389631`。
> current-progress 现在绑定 fixed-wing/multirotor 当前运行 r30、三机型矩阵 r32、VTOL 运行与 receipt r31、
> observed/runtime index r26、external join/revalidation r26、PX4 response matrix/campaign r26、
> handoff r3 与 release-gate audit r8。传感器 manifest 的 `scenario_manifest_hash` 与
> `ScenarioManifestIndex.canonical_hash` 逐字相等才允许进入 campaign，错误场景或错误 canonical
> 身份会 fail-closed。r30 是真实 software external SIM 运行；VTOL r31 是新的真实 software
> external SIM 运行；r32 matrix 是对三份当前 evidence 的 source-bound join。当前 verifier
> `--check-sources` 通过，
> 但 `gate_passed=false`、`release_allowed=false`，
> 仍不代表 HIL/REAL、室外 GNSS、真实硬件、人工 QGC 或最终 `0.3.0` 发布。

> **2026-10-01 external VTOL r31**：在当前 parity 下重新执行独立 disposable PX4/Gazebo
> VTOL campaign。`synthetic-east` 与 `synthetic-west` 两个任务均完成；Mission upload/readback/
> commit/permit、planner revalidation、policy identity、journal hash-chain、受限 evaluator、
> loopback 端口和 exact-ID container cleanup 全部通过。证据为
> `artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.json`，receipt 为
> `artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.receipt.json`。
> 这是软件 external SIM 重跑，不是 HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布证据。

> **三机型矩阵 r32 source join**：external Mission matrix
> `docs/evidence/external-mission-matrix-20261001-r32.json` 与 shared-policy vehicle matrix
> `artifacts/evaluation/shared-policy-vehicle-matrix-20261001-r32.json` 已切换到 r31 VTOL receipt，
> fixed-wing/multirotor 继续引用当前 r30 evidence。两份矩阵只证明三机型软件 external SIM 的同一
> Mission/compiler/transaction 和 policy identity 关系，不升级 HIL/REAL、室外 GNSS 或硬件结论。

> **同轮索引与 release-gate 审计**：`verify_current_progress_index.py --check-sources` 通过，当前进度
> 定向回归 `54 passed`；随后全量 Python 回归为 `5230 passed, 7 skipped`。本轮还修正了历史
> pair receipt 的隔离夹具、集成 SIM 的 RGB/depth 伴随帧以及 MAVLink 端口边界测试。release-gate 审计更新为
> `docs/evidence/shared-policy-release-parity-source-transition-20261001-r6.json`，明确区分旧的
> historical field-link 报告与当前只读 field-link probe；`gate_passed=false`、`release_allowed=false`
> 和 `0.3.0-pre.8` 保持不变。没有把历史报告改写成当前硬件/HIL/REAL 运行，也没有执行 commit 或 push。

> **2026-10-01 当前 parity 重绑定（r25）**：发现默认 release-gate wrapper 仍选择内部嵌有旧
> `cf5d…/core=9413…` 身份的“current-parity”文件；现从同一批不可变 external runtime/AI、
> PX4 response positive/fault source 重新计算 join、matrix、campaign 和独立 revalidation，全部绑定
> 当前 `release-parity.json`（文件 SHA-256=`5f5796d8bc784c24ea7e6fe98a4675e1842472df375ef6d524e44c9f4b7410e`）。
> 新 release-gate report 为 `artifacts/evaluation/release-gate-pre.8-current-20261001T110500Z-v6-bound-report.json`，
> `current_release_parity_bound=true`；这只是从保留 source 的可审计重算，不是新 PX4/Gazebo 运行。
> 32 个阻塞项仍来自 synthetic replay review、现场链路、人工视觉/Mission、HIL/REAL 和最终 channel，
> 因此 `gate_passed=false`、`release_allowed=false`、最终 `0.3.0` 仍未发布。

> **SharedTaskPolicy handoff identity（r1）**：新增
> `artifacts/evaluation/shared-policy-runtime-handoff-20261001-r2/handoff.json`，绑定当前 VTOL
> campaign receipt、`configs/evaluation/vtol-shared-policy-v1.json`、当前五项 parity 和 airframe。
> handoff verifier 通过（handoff SHA-256=`d95038e435ae913854b16d0a38c3987cdcd9efcec7429fba1dfe8c999d4ec546`），
> 但它明确是 `software_identity_fixture`：`hil_acceptance=false`、`real_acceptance=false`、
> `hardware_acceptance=false`、`flight_acceptance=false`，没有启动 HIL/REAL 或接触硬件。

> **r6 审计索引修复**：当前 release-gate audit 已从 r5 索引切换到
> `docs/evidence/shared-policy-release-parity-source-transition-20261001-r6.json`；r3/r4/r5 保留为不可变历史
> 记录。r4 使用同一份当前 report 和当前 parity 做 source-bound 校验，`current_release_parity_bound=true`，
> 仍不代表新一轮 PX4/Gazebo 运行，也不打开 HIL/REAL、室外 GNSS、真实硬件或最终发布门。

> **2026-10-01 序列参考器收敛（r1）**：将原先只存在于 `scripts/benchmark_task_sequences.py` 的
> 小规模 DFS 参考搜索提取为 `src/multidetect/autonomy/task_sequence_reference.py`，脚本现在委托
> 该库函数。目标显式命名为 `sum_priority_plus_one_then_count_then_minus_modeled_time_v1`，每条边
> 继续复用原任务选择器、RETURN 约束、预算状态推进和 Mission 编译器；输入最多 6 个任务，节点
> 超限直接失败，不输出部分最优。新增顺序不变性、GPS 不合格、运行时缺失、任务上限和脚本委托
> 回归。它是开发诊断，不接入 `SharedTaskPolicy`/`PolicyArtifact`、PX4 campaign 或 actuator，
> 不代表 learned model 收益、物理最优性、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0`。

> **同轮 parity 处理**：发现 checked-in `configs/release-parity.json` 与其显式 source list 的哈希
> 不一致后，已按当前 checkout 重新生成并同步索引中的当前 parity。历史 external Mission 证据未
> 被重写或自动升级；current-progress verifier 在旧证据无法匹配新源码身份时继续 fail-closed，
> 因此不能把这次静态重算写成新的 PX4/Gazebo 运行、HIL/REAL 或发布通过。

> **2026-10-01 序列参考独立留出（r2）**：对冻结的 `task-policy-independent-holdout-v1.json` 运行
> 新库化参考器，12 个输入的贪心/参考总奖励均为 `120/120`，改进案例 `0`，不超过贪心估算耗时的
> 改进也为 `0`。报告 `artifacts/evaluation/task-sequence-reference-independent-holdout-20261001-r2.json`
>（31,696 bytes，SHA-256=`26ef1ebbb3b8b6c7f4f1c0b2df8696dee5df924695797ab1572da48f575`）仅说明该
> 预算目标在这组独立软件留出上没有优于现有贪心；不晋升序列策略、不宣称物理或 HIL/REAL 收益。

> **2026-10-01 异质 development 训练复验**：使用预先声明的 36 组系数网格和 4/12 输入的
> development/independent holdout 对照重新评估现有 ranker。训练、validation、test 奖励均为
> parent=candidate `40/40/40`，独立 holdout 总奖励 parent=candidate `120`；candidate SHA 未变，
> 仍不晋升。这说明单纯扩大当前三特征线性系数搜索没有带来独立收益，下一步应改进任务序列/特征
> 信息，而不是继续重复同一参数搜索。记录为软件规划实验，不是物理、HIL/REAL、室外 GNSS、硬件
> 或最终发布证据。

> **2026-10-01 训练证据索引审计强化**：`verify_current_progress_index.py` 现在会重新打开并
> 交叉验证训练报告、独立 holdout、candidate ranker、训练 artifact、外部 PX4/Gazebo runtime
> evidence、campaign summary、receipt 与当前 release parity。文件 SHA、canonical artifact、
> parent/candidate reward、任务顺序、planner revalidation 或 cleanup 任一漂移都会 fail-closed；
> 新增 2 个篡改负向测试。该改动只增强证据可追溯性，不把软件 SIM 升级为 HIL/REAL、室外 GNSS、
> 真实硬件或最终 `0.3.0` 发布通过。

> **2026-10-01 训练 artifact 外部闭环（当前优先）**：将训练链生成的 candidate policy artifact
> 绑定到 VTOL airframe provenance 后，实际进入同一个 PX4/Gazebo disposable container 和同一个
> `MissionTelemetrySession`，连续完成 `synthetic-east -> synthetic-west` 两次 Mission。两次
> upload/readback/commit/permit、planner revalidation、policy artifact canonical SHA、journal
> hash-chain、router graceful stop 与 exact-ID cleanup 全部通过；外部证据为
> `artifacts/evaluation/shared-policy-trained-vtol-two-task-20261001-r1.json`，receipt 为同名
> `.receipt.json`。这证明训练产出的同一 ranker/artifact 可沿用现有外部 SIM Mission 路径，仍然
> 只属于 software external PX4/Gazebo SIM；receipt 明确是 `retained_files_audit`，不等于当前
> runtime parity、HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布。

> **2026-10-01 训练与独立留出复验（当前优先）**：重新运行现有 `train_task_ranker.py` 后，
> 有限参数搜索选中的 candidate 与 parent 完全相同；train/validation/test 奖励均无变化
> （26/28/35）。随后用冻结 candidate 运行 `run_shared_policy_campaign.py` 的独立 holdout：
> 训练输入 3 个、留出输入 12 个，输入哈希和场景族均不相交，parent/candidate 独立留出总奖励均为
> 120，planner revalidation 保持通过。由于没有独立收益，candidate 继续不晋升；这只是可复验的
> 软件规划 SIM 训练证据，不是动力学飞行、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布证据。
> 训练报告为 `artifacts/evaluation/task-ranker-training-20261001-r1/report.json`，独立对照为
> `artifacts/evaluation/shared-policy-independent-sim-20261001-r1.json`，均已写入 current-progress 索引。

> **2026-10-01 外部双任务闭环（当前优先）**：在固定 digest 的 PX4/Gazebo disposable
> VTOL 容器内，使用同一个 `MissionTelemetrySession` 和同一个 `SharedTaskPolicy` 连续完成
> `synthetic-east -> synthetic-west` 两个任务。两次 Mission 都完成 upload/readback/commit/permit、
> planner revalidation、policy decision identity、hash-chain journal 和 terminal state；同一
> container/session、router graceful stop、exact-ID cleanup 全部通过。运行证据为
> `artifacts/evaluation/shared-policy-current-vtol-two-task-20261001-r2.json`，receipt 为
> `artifacts/evaluation/shared-policy-current-vtol-two-task-20261001-r2.receipt.json`。
> 该项证明的是软件 external PX4/Gazebo SIM 的连续任务闭环，不是 HIL/REAL、室外 GNSS、真实
> 硬件、人工 QGC 或最终 `0.3.0` 发布；receipt 仍明确 `retained_files_audit`，当前 runtime parity
> 仍未宣称。

> **2026-10-01 共享策略证据收口（当前优先）**：fixed-wing/multirotor 的当前 depth Mission
> external run 为 r20，VTOL 当前 campaign 为 r22；三机型 Mission matrix、vehicle-policy matrix
> 和 observed session 已分别更新到 r23。它们共同证明同一 Mission transaction/compiler、SharedTaskPolicy
> 身份、planner revalidation、sensor/GPS 质量门和容器清理，但仍只是 external PX4/Gazebo SIM，
> 不等于 HIL/REAL、室外 GNSS、硬件、人工 QGC 或实飞。

> **证据语义修正**：从旧运行目录重新生成的 parent/candidate receipt 和 AI flight composite 现在
> 明确标记为 `retained_files_audit`；`runtime_source_parity_verified=false`、
> `same_release_parity=false`、`current_runtime_parity_proven=false`。只有重新执行并带有可靠
> source attestation 的运行才能升级为 current runtime parity；当前最终 release 继续关闭。

> 本轮验证：`verify_current_progress_index.py --check-sources` 通过，AI receipt/索引/文档/parity
> 与当前 vehicle-matrix receipt 定向回归 111 passed，Ruff 与 `git diff --check` 通过。

> **2026-10-01 源码身份校正（当前优先）**：对 `configs/release-parity.json` 重新按其显式
> `source_files` 重算后，当前 canonical parity 为文件 12,583 bytes、SHA-256
> `2669ff1bf17eb2fd823897bd5c89129986b57499606fbdd824f48fad9d07a957`，其中
> `core_algorithm_hash=255ca970488958f448860015d05d2fe84883736a16b45537a677a4271097296b`。
> 旧的 `9413bdf...`/`cf5d4626...` 只代表上一份 source identity，相关旧外部运行保持不可变，
> 不再被当作当前源码运行。新增的严格检查 `python scripts/verify_current_progress_index.py
> --check-sources-only` 已通过；普通 current-index 仍在等待其余历史 external evidence 逐项重跑或
> 明确标记为历史，因此最终 release gate 继续 fail-closed。

> 同轮在新源码 identity 下完整重跑三条 disposable PX4/Gazebo sensor-fault：image r16
>（60,058 bytes，SHA-256 `5451eb26e5a182e12affe6960801dca6995363d87892a87f5dee5d2a8f0e5aff`）、
> navsat r17（74,635 bytes，SHA-256 `9ebc615a9503533579de7dca8c3dcf1fb1186e02dda54752a2022cf1dfdd9d40`）、
> odometry r18（75,021 bytes，SHA-256 `186b14fa89a51a5208d73815f5122b281b0a1d50701685382edc2c1dbe660cf7`）。
> 三条均为真实 external PX4/Gazebo 运行：transport/runtime gate 通过、窗口内拒绝、动态源 cache/epoch
> 恢复、无 timeline overflow、容器 stop/rm 清理成功，且没有 flight-control command；仍不等同
> HIL/REAL、室外 GNSS、真实硬件、人工 QGC visual acceptance 或最终 `0.3.0`。

> 2026-10-01 标定工具收口：`scripts/validate_outdoor_depth_calibration.py` 新增独立 holdout
> 严格质量门，可要求最少独立样本并限制 MAE/RMSE/最大误差/相对 MAE；严格模式失败时仍写出完整
> 报告但返回非零，默认 advisory 兼容模式不变。当前没有真实现场/RTK/激光测距样本，因此这项
> 只证明软件验证链，不能打开室外标定、HIL/REAL、硬件或最终 `0.3.0` 发布门。

> 同轮新增 `scripts/verify_outdoor_depth_calibration_report.py`：Jetson 启动器可将验证报告与
> 当前 calibration document 的 SHA-256 绑定，并在显式设置
> `METRIC_DEPTH_REQUIRE_CALIBRATION_VALIDATION=1` 时要求严格 holdout gate 明确通过；advisory、
> 旧报告或文档字节漂移均 fail-closed。该项仍是软件部署证据，不是现场标定、室外 GNSS、
> HIL/REAL 或最终 `0.3.0` 证据。

> 2026-10-01 当前 response/3D/release-gate 收口：按当前 `release-parity.json` 重新执行了
> fixed-wing、multirotor、VTOL 的 PX4 `GLOBAL_POSITION_INT` response 正向运行，6/6 次均从
> `SimulatorTelemetryBridge.ingest_perception -> SimulatorAiExecutor -> SimulatorMavlinkControlAdapter
> -> MAVLink MANUAL_CONTROL -> PX4 SITL/Gazebo` 走到有效位置反馈，目标刷新、heartbeat、位置样本、
> 最小位移和容器清理均通过。当前 matrix 为
> `artifacts/evaluation/px4-reported-position-response-current-parity-20261001-r3/px4-reported-position-response-matrix-current-parity.json`
>（SHA-256=`24c26df15654c8cafbe17193a2faf33ef62f0efd0324f7509cedeaabbcc7a9cd`，内部
> `matrix_sha256=d8bed1a4dc094b64654c7e614715c26b8e022e1c841e2e02d204f146d7278031`），当前
> campaign 为 `artifacts/evaluation/px4-reported-position-response-current-parity-20261001-r5/px4-reported-position-response-campaign-current-parity.json`
>（SHA-256=`1b1f395be6b8e229b9da1c0b8b2058baf3d144a1f577366c74a8725cd5fb5c64`，内部
> `campaign_sha256=f33eeef5af6aa663797db892146099f922df7001e48567d5c37f52496c7cbe3e`）。campaign
> 的 9 个 fault 条目也已全部使用当前 scenario/SDF 重新执行，逐项通过 `runtime_gate`、故障观察、
> fail-closed 状态和容器清理；campaign verifier 现在还会拒绝 fault source 与当前 scenario
> manifest SHA 不一致的旧证据。

> 同轮以当前 SDF/Gazebo 模型重新执行 fixed-wing 与 multirotor 的 depth Mission source：
> `external-fixed-wing-depth-shared-policy-20261001-r19.json`（66,205 bytes，SHA-256=
> `8df71bc79ae4eca1bfc1ba857dd61ceb052f4589b46649bb8bf11d3817ee7fb9`）和
> `external-multirotor-depth-shared-policy-20261001-r19.json`（66,151 bytes，SHA-256=
> `3fc877bd99da42a73ec2953f6d0d4f155d3d33fe5d1daa850c9c195df9ee1787`）。两条都是真实
> Docker/PX4/Gazebo 2/2 sensor frames，Mission upload/readback/commit/permit、SharedTaskPolicy、
> planner revalidation 和 depth transport 全通过；索引 verifier 现已绑定这两份当前资产证据。
> 之前的 r18 仍保留为不可变历史，不再冒充当前 SDF 资产绑定。

> 当前 3D control evidence 已切到
> `artifacts/evaluation/simulator-3d-control-current-20261001-r1/`，VTOL/fixed-wing/multirotor
> 三份证据及 verifier 均通过；其 `external_runtime_gate_passed` 仍为 `false`/`not_claimed`，
> 所以这是确定性软件 3D 控制证据，不被写成 PX4/Gazebo 外部实飞证据。新的 release-gate
> manifest/report 为 `artifacts/evaluation/release-gate-pre.8-current-20261001T013000Z-v6-bound.json`
>（23,649 bytes，SHA-256=`c2d06d9af798aca84b9d146d756f242e0bd4d713765e1afc8730ddd23465fd0e`）和
> `...-report.json`（71,846 bytes，SHA-256=`daf3417fdeeea5a5d5966ca9b068b8d1e581fe6222d1bbdfc989469733b4f60e`）。
> 对应 audit receipt 为
> `docs/evidence/shared-policy-release-parity-policy-helper-20261001-depth-r21.json`；
> `verify_current_progress_index.py`、release-gate wrapper 和定向回归均通过，但 report 仍为
> `gate_passed=false`、`release_allowed=false`、32 个阻塞项。HIL/REAL、真实硬件、室外 GNSS、
> 人工 QGC visual acceptance、最终 `0.3.0` 发布仍未声明。

> 2026-10-01 当前权威收口（combined NavSat fault r4）：在修复汇总证据投影后，
> `artifacts/evaluation/combined-sim-qgc-20261001T000711Z-53364` 完成完整的 900 帧
> Docker/PX4/Gazebo/QGC 外部 SIM 运行；不可变源证据为
> `artifacts/evaluation/combined-sim-qgc-navsat-20261001-r4/combined-evidence.json`
>（5,118,982 bytes，SHA-256=`cef8676098119a6e545c77144b22da2b1256cbe95a6674d5b69b3a244f0f338a`）。
> `integrated_sim_qgc`、sensor transport、attached VTOL、完整 Mission 和严格 combined
> verifier 全部通过；RGB/depth/odometry/runtime ACK 为 900/900，Mission reached `0..6`，
> 观察到 fixed-wing、multicopter、land、terminal disarm，目标证据卡 370 张且全部
> read-only。NavSat fault 在 image 300 开始、持续 40 帧，窗口内 wire=0，丢弃 39 条源消息，
> `source_epoch=1`、cache clear、fresh recovery、603 条恢复帧和无 timeline overflow 全部通过。
> 严格 verifier 报告为
> `docs/evidence/combined-sim-qgc-navsat-fault-20261001-r4.verification.json`，不可变 receipt 为
> [`combined-sim-qgc-navsat-fault-20261001-r4.json`](evidence/combined-sim-qgc-navsat-fault-20261001-r4.json)。
> 为避免规范字段被大数组截断，combined wrapper 的每顶层字段投影上限从 20,000 提升到
> 200,000（仍有明确有限上限）；r4 的 `integrated_sim_qgc` 为 27,405 节点、`vtol_mission`
> 为 156,297 节点，均未截断。该证据仍严格限定为软件 external SIM：`operator_visual_acceptance`
> 未声明，`physical_performance_claimed=false`、`hil_real_evidence=false`、
> `outdoor_gnss_evidence=false`、`candidate_promoted=false`、`final_release_allowed=false`。

> 2026-10-01 已把 `image`、`navsat`、`odometry` 故障注入接入现有
> `integrated-sim-qgc` combined 生命周期：PowerShell wrapper 现在校验窗口必须留出真实的
> post-window frame、统一 fault source 大小写，并以扁平 argv 转发参数；CLI 从同一 Gazebo
> transport stats 捕获唯一的 `gazebo_sensor_stream_finished` 诊断，再复用现有严格 verifier，
> 缺诊断、重复诊断、身份/恢复/缓存 epoch 不一致都会 fail-closed。后端、CLI、wrapper 和
> 正向/负向路径回归共 `171 passed`（含 image 与 NavSat combined CLI diagnostic 通过/拒绝及长 timeline helper 回归），Ruff、`py_compile`、`git diff --check` 通过。当前 parity 已重算为
> `configs/release-parity.json`（文件 SHA-256=
> `cf5d4626d9f5f2593577e35b1aa339efe0ee84383b9f9c998181f9d459415bc5`，
> `core_algorithm_hash=9413bdf11fec917ade63300ac8ad4b5ad598829023a1157af2056b7b282a91dd`）；当前
> `scripts/gazebo_sensor_stream.py` SHA-256 为
> `4b1e36c71d622c85e1fc9643c4179fd702f407ca67740d208cdcb06ba88447d8`，
> r4 external combined evidence 已补入当前索引；随后跨 autonomy/mission/release/current-index
> 扩展回归为 `562 passed`。本轮新增的是 external Docker/PX4/Gazebo/QGC 软件 SIM 通过证据，
> 不提升 HIL/REAL、室外 GNSS、真实硬件、实飞、人工 QGC 或最终 `0.3.0` 发布门；`release_allowed=false` 保持不变。

> 2026-10-01 故障恢复 r2 在固定 digest disposable PX4/Gazebo 外部运行中重新验证三类
> 传感器源，并新增动态源 cache clear/epoch 边界。当前证据分别为
> `artifacts/evaluation/simulator-docker-acceptance-20261001-r13-fault-image.json`
>（2 帧，53352 bytes，SHA-256=`bfc9e4fcb641868e9c26338637da1c537f7f2dc1b1ba2d1990909d5050e409c4`）、
> `...r14-fault-navsat.json`（4 帧，75863 bytes，SHA-256=`463620b2055c8e44de695c42a7e9104a2405e818f37ba4aa059e40fcccb478f1`）和
> `...r15-fault-odometry.json`（4 帧，77963 bytes，SHA-256=`6d2ffa9ed84c2c7e0960bf6e61be1fa5202dc2586361f57f25d4116b1f0b74ac`）。
> 三条报告均 `gate_passed=true`，并验证 `reject_during_window_and_until_post_window_fresh_sample`、
> 首次丢弃回调清空动态源历史、`source_epoch=1`、恢复前显式等待新样本、窗口内零 wire、
> 单调 image index、连续 wire sequence、动态源时间戳严格晚于最后丢弃样本、恢复标记绑定到真实输出、
> timeline 不溢出以及 Docker stop/rm 清理无错误；这三份已执行 evidence 当时绑定的 helper 源码 SHA-256 为
> `d4a3823bb8852912364a935b809e5d994806dc10a143ff1a1e998ab1aacc287f`；旧的
> `20260927-r5/r8/r9` 仍保留，但只作比较基线；其中 r8/r9 的连续帧复用了同一旧 NavSat/Odometry
> 时间戳，不能再作为当前无缓存复用证明。相关自动化定向回归为 `102 passed`。这仍只是 external
> PX4/Gazebo 传感器流故障恢复，不等同 HIL/REAL、室外 GNSS、真实硬件、实飞、人工 QGC 验收或最终
> `0.3.0` 发布；`release_allowed=false` 保持不变。

> 同一轮当前索引的三机型 Mission matrix、vehicle-policy matrix、VTOL SharedTaskPolicy receipt
> 和 shared-policy pair 均已使用不可覆盖的 `depth-r18` 副本重新绑定当前 parity；
> `python scripts/verify_current_progress_index.py` 返回 `status=passed`。这些是证据身份与
> 可追溯性收口，不新增一次 Mission 飞行，也不改变 HIL/REAL、室外 GNSS、硬件或最终发布门。

> 2026-10-01 当前模型资产完成一次完整的 external PX4/Gazebo/QGC combined VTOL
> 验收：`artifacts/evaluation/combined-sim-qgc-20260930T204652Z-49500`。600/600 RGB
> 帧、600/600 depth companion、600/600 odometry timestamp、600/600 runtime metadata
> ACK/barrier 全部收齐，integrated sensor gate 与 VTOL Mission gate 均为 `true`；Mission
> reached seq `0..6`，观察到 multicopter、fixed-wing、landed、terminal disarm。Mission
> 用时 `152.688 s`，最大距 home `130.358 m`、最大相对高度 `23.526 m`。容器已停止并移除，
> 全部动态测试端口已释放。当前不可变摘要为
> [`combined-sim-qgc-vtol-pass-20261001-r1.json`](evidence/combined-sim-qgc-vtol-pass-20261001-r1.json)，
> SHA-256=`bfed41c776cd87fae58cd8db29909dbfe89cddeaa17ac01a927f29f54582a186`。
> 本轮同时把 Windows/Gazebo 深度流 deadline 从 `ceil(frames/2)+30` 调整为
> `max(240, ceil(frames*0.75)+60)`，并将 VTOL 深度子 link 设为有效质量/惯量近似为零的
> transport-only fixture，避免传感器挂载改变标准 VTOL 动力学。该结果是软件外部 SIM 证据，
> 不等同于 HIL/REAL、室外 GNSS、硬件性能、人工视觉签字或最终 `0.3.0` 发布；这些门继续关闭。

> 本轮模型资产更新后，索引 verifier 发现 fixed-wing/multirotor depth evidence 仍绑定旧的全量
> Gazebo bundle。旧 r11 文件保持不可变，新增 source-bound r12 副本
> `external-fixed-wing-depth-shared-policy-20261001-r12.json` 与
> `external-multirotor-depth-shared-policy-20261001-r12.json`，只重算当前 bundle 与
> sensor-binding identity，不声称重新执行这两条 Mission；当前索引已切换到 r12。

> 2026-09-30 当前索引已把 `shared_policy_current_vtol_campaign_20260928_r2` 的同一外部
> 运行目录重新绑定到当前 `release-parity.json`：新的不可变 receipt 为
> `artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-depth-r9.receipt.json`，
> canonical SHA-256=`5c82593e862132c29e96001dc6cd663cbe70bbddb49c37be99ad3697beaf7f92`。
> 三机型 shared-ranker 矩阵已改用该 VTOL receipt，生成
> `artifacts/evaluation/shared-policy-vehicle-matrix-20261001-depth-r10.json`，对应
> external Mission matrix 为 `docs/evidence/external-mission-matrix-20261001-depth-r10.json`。
> 新增 current-progress verifier 会拒绝旧 parity receipt 混入当前 VTOL 条目；这只是证据
> 可追溯性增强，不新增飞行、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布结论。

> 2026-10-01 当前模型资产已重新跑通两条 same-container external PX4/Gazebo 深度 Mission
> 证据链：fixed-wing 与 multirotor 都使用现有 `SharedTaskPolicy`、planner revalidation、
> PX4 Mission upload/readback/commit/permit 生命周期，以及同一容器内 2/2 RGB + 2/2
> `MDGZDEP1` depth companion。新源证据分别为
> `artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261001-r10.json` 和
> `artifacts/evaluation/external-multirotor-depth-shared-policy-20261001-r10.json`，当前索引
> 绑定不可覆盖的 r11 副本；两份证据均 `gate_passed=true`、`current_release_parity_bound=true`、
> `shared_policy_used=true`，并重新绑定当前 Gazebo 模型资产哈希
> `53c3db017228b3db4e8afd3b78706fda92fa8ee519e83a769729363a39d5e365`。旧 r7 证据保留为历史，
> 不再冒充当前模型的验收证据。

> 同日 `river_rescue_vtol_001` 的 headless external VTOL response profile r5 也完成了完整
> Mission + sensor gate：`docs/evidence/external-vtol-response-20261001-r5.json`，700 s 有界
> timeout 内确认 fixed-wing、multicopter、landed、terminal disarm，Mission reached seq
> `0..6`，最大距 home `275.388 m`、最大相对高度 `25.791 m`，深度 manifest/content/
> canonical/file/provenance 绑定均通过。该次不是 QGC combined 人工视觉验收；QGC、HIL/REAL、
> 室外 GNSS、硬件性能和最终 `0.3.0` 发布门仍保持关闭。

> 为修复 r4 的超时边界，Mission timeout 上限从 600 s 提升到 700 s（仍低于 router 的 720 s
> grace）；同时修复 `GazeboSimFrameProvenance.depth_geometry` 在内存中使用 tuple、JSON
> 重载后变 list 导致的 manifest content binding 漂移。新的 parity 文件 SHA-256 为
> `009a4f00cfd22b1f272adc4c9d9bfcc2826ce1c196fb6dd04d0e9bfed1b99048`，
> `core_algorithm_hash=a844cc66ce2a9c9dd47763ff5d9fbb53bc2c27eff3c82e69099c548f84b3be39`、
> `px4_mission_path_hash=811cf50caf83fcfdd829dc7fa60e3b0faf7fe1fa419c2fac94cdd9ea818e4490`。
> `verify_current_progress_index.py` 已恢复 `status=passed`，`release_allowed=false` 不变。

> 2026-10-01 完整外部 combined rerun 已留下可复核的 VTOL 动力学失败证据：
> `artifacts/evaluation/combined-sim-qgc-20260930T180857Z-57764` 的 attached VTOL gate 通过，
> `GLOBAL_POSITION_INT` 共收到 1036 个有效位置样本，PX4 观测到 `MISSION_CURRENT` seq 0→1→2，
> 但只确认 `MISSION_ITEM_REACHED` seq 0、1。车辆最高相对高度 19.44 m，随后向西/南偏移，
> 相对首点最大位移 79.5128 m，降到地面附近并停留；240 s 内没有 seq3、`VTOL_LAND` 或
> terminal disarm，Mission gate 和 combined gate 均为 false。独立摘要为
> `docs/evidence/combined-sim-qgc-vtol-failure-20261001.json`，源文件哈希已绑定；这证明
> 位置观测链可用，但当前 pinned VTOL→FW→航点路线/动力学没有完成，不降低验收门、不改写为
> Mission 通过，也不提升 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

> 同一次运行的 integrated audit 还记录到首个 `gazebo_depth` 候选为 `0.208943973 m`，因低于
> `0.4 m` 物理下限而拒绝；后续 `42.856834144 m` 候选虽然满足范围边界，但常量 synthetic
> 检测框没有目标身份绑定，因此不构成目标定位证据。

> 最新 parity/source-bound 收口：由于上述 VTOL observer 诊断字段属于 Mission path，
> `configs/release-parity.json` 已再次生成，文件 SHA-256 为
> `7d187ba0707c81e49d8aaef070218d5010657f346a11899bd474574379ff6519`，
> `core_algorithm_hash=3008b9020e774cd2f943f04e7e14d068900eef410fafe7a0e9c7b8e4f3cf3270`、
> `px4_mission_path_hash=55aaead247cec7d41d3a41a62a0834ea0e3ffad5364d776c74cb107772eae72f`。
> 当前索引绑定 `20261001-depth-r8`，fixed-wing/multirotor depth index 绑定 r6；历史副本不可变，
> `verify_current_progress_index.py` 返回 `status=passed`，`release_allowed=false` 保持不变。

> 2026-10-01 外部 combined SIM 诊断收口：短跑 audit 已确认 `gazebo_depth` 传输、RGB/depth
> frame 配对、ROI 有效像素和 freshness 均正常；候选被共享测距的物理边界拒绝，实测样本为
> `slant_range_m=0.20894397308456691 m`，而 `minimum_slant_range_m=0.4 m`，原因是
> 当前 synthetic HIL fixture 输出固定中心框，框覆盖了近处机体/自遮挡区域。没有降低物理下限，
> 也没有把该失败写成定位通过。`LiveMissionRunner` 现在额外输出去重的 depth-unavailable 与
> `direct_range_candidates` 诊断，记录 source、frame、age、绝对尺度、上下限和拒绝原因。
> `BufferedFrameSource` 同步转发可选 depth capability，避免缓冲层隐藏 Gazebo depth contract。
>
> 同轮修复外部 Mission 观测：`vtol_sitl_mission_acceptance.py` 在同一 disposable MAVLink
> 实例显式配置 `GLOBAL_POSITION_INT` 10 Hz，任务位移/航迹/着陆证据不再依赖 PX4 默认流。
> combined QGC wrapper 不再对常量 synthetic HIL 检测器强制 `--require-target-geolocation`；
> 它只验收 RGB/depth transport、共享测距链和原生 QGC adoption，目标落图/目标地理定位保持独立
> gate，只有显式要求时才进入 combined gate。VTOL、combined script、Live/Vision/depth/CLI、
> combined evidence、Ruff、PowerShell 解析和 current-progress index 定向检查通过；完整外部
> VTOL combined 仍未通过，未提升 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

> 2026-10-01 parity/source-bound 收口（历史快照）：上述共享源码变化已重新生成
> `configs/release-parity.json`，文件 SHA-256 为
> `573730de3a42c25d66bbc0c508e3eb8fe80a95b09539c37980d49e51c5ede572`，当前
> `core_algorithm_hash=3008b9020e774cd2f943f04e7e14d068900eef410fafe7a0e9c7b8e4f3cf3270`、
> `px4_mission_path_hash=a067f0f7aa3609f4301e37cef5a2fd9c2d213062ffe3835e16f667b077480d15`。
> 历史 evidence/receipt 未覆盖；当前索引、pair/vehicle/matrix、observed session 和 release
> audit 使用新的 `20261001-depth-r7` 绑定副本，fixed-wing/multirotor depth 索引使用 r5。
> `release_allowed=false`、`hil_real_evidence=false`、`outdoor_gnss_evidence=false`、
> `final_release_allowed=false` 保持不变。

> 2026-09-30 parity/source-bound 收口：重新绑定脚本已修正 Windows CRLF 与旧源副本混用问题，
> 当前 `external-mission-matrix`、fixed-wing/multirotor depth Mission、VTOL campaign receipt、
> shared-policy pair/vehicle matrix、observed session 和 release audit 均使用独立的
> `20261001-depth-r6` 后缀；历史 `depth-r4`/`r5` 文件保持不可变。当前 parity 文件 SHA-256 为
> `b3135a6fc20838e398ae75d16f8f4bc174a95a453987e8d7c84554a4a13fb303`，索引复核返回
> `status=passed`，索引/矩阵/深度回归、LF 写入回归，以及 VTOL/external SharedTaskPolicy
> campaign 相关 300 项测试均通过，Ruff 通过。该次只重建 source-bound identity，
> 没有重新启动 Docker/PX4/Gazebo/QGC；`release_allowed=false`、HIL/REAL、室外 GNSS、硬件和最终
> `0.3.0` 发布门继续关闭。

> 2026-09-30 外部组合深度闭环增量：`river_rescue_vtol_001` runtime manifest 现在显式声明
> `front-depth`，canonical channel SHA-256 更新为
> `582e05422fdac8e5934d80f92a5e77d080dfee39c6e1d65221e4ff4593dba5d4`。combined wrapper 的
> readiness 同时要求 RGB/depth image、两组 CameraInfo、NavSat 和完整 odometry publisher 稳定，
> integrated evidence 要求每个 RGB frame 都有匹配的 `MDGZDEP1` companion、topic、calibration ID
> 与 calibration SHA。目标落图验收现在可以接受经过上述 transport 身份绑定的 `gazebo_depth` direct
> source；真实 live audit 中嵌套的 GPS quality/sample 会被提升到证据卡输入并要求卡片本身
> `available=true`。terrain uncertainty 只有精确命中 `terrain_uncertainty_unbounded` 且存在新鲜、
> 已认证 direct depth 时才沿用已有 degraded advisory path，其它 stale/horizon/inconsistent 情况
> 继续 fail-closed。自动化定向回归通过；尚未重新执行完整 VTOL combined 外部运行，因此不宣称
> combined gate、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布通过。

> 本轮源码变化已重新生成 `configs/release-parity.json`：当前
> `core_algorithm_hash=a530221979ed2411af3468eb625ddaf7ef5fbd0679d2c883555f0b37c946b2bb`、
> `px4_mission_path_hash=58b94395805924cf78af386355be9952c2aaeb49c7404bcf11c73371450eb8fc`、
> `safety_policy_hash=4dd30efc6d3fb39831a6865ee43baecc692bd8cd8f41aed5032937827f482abb`、
> `task_contract_hash=16b6ddf85b7a34cc243b10cdd5a4e9b47edf16895b80c949339e10b1d49aa478`、
> `ui_contract_hash=97ac2cb86f50f9fadc01505f7ce51e468f43f55f544b107dd37dc44cc4085e6d`。
> 历史 receipt 不覆盖，发布标签仍为 `0.3.0-pre.8`、`release_allowed=false`。

> 2026-09-30 回放地图核对增量：原生 QGC 目标事件回放新增“落图预览”。只有通过 native
> manifest hash、GPS 坐标域和新鲜度门的历史事件才会以橙色点和 `1σ` 圆投影到当前主地图；
> 预览保持独立于 `MultiDetectState`，不选中实时目标、不写 Mission、不发送 Guided/飞控消息。
> 新清单或“清除落图预览”会清除该点，不可定位事件不会复用旧坐标。该项是 QGC 软件只读能力和
> 机器回归，不是人工视觉签字、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布证据。

> 2026-09-30 QGC Release 增量构建已完成：最新直接 EXE 为
> `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe`，46,876,672 bytes，
> SHA-256 `E0FFF035ED309CB73C260570C880C6817B0A7006F5F9B887364F881AA2A9C6B1`。
> 本次链接使用 VS 2022 x64 Developer 环境，以补齐 Windows SDK `runtimeobject.lib` 搜索路径；
> 这是软件构建产物，不是安装包，也不代表 PX4/Gazebo 外部验收、HIL/REAL、室外 GNSS、硬件或
> 最终 `0.3.0` 发布已通过。

> 同一 Release 已安装到独立便携目录
> `build/portable-replay-preview-20260930/bin/MultiDetectGCS.exe`；便携主 EXE 与直接 EXE
> 字节一致（46,876,672 bytes、SHA-256 `E0FFF035ED309CB73C260570C880C6817B0A7006F5F9B887364F881AA2A9C6B1`），
> `bin` 有 233 个文件且包含 `Qt6Core.dll`、GStreamer 和 `qoffscreen.dll`。该目录执行
> `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui` 退出码为 0，未输出
> stdout/stderr；这是无界面软件冷启动烟测，不替代人工 Fly View、HIL/REAL、室外 GNSS、硬件或最终发布验收。

> 2026-09-30 历史目标证据卡只读重建已完成：工具绑定旧 combined source
> `artifacts/evaluation/combined-sim-qgc-20260905T175019Z-64536/combined-evidence.json`
>（source SHA-256 `5031eefe75cf78d42aeccc04e990bb8f9975fbc6ebb12d4087f685c18652c8b6`）和其
> 5362-event integrated audit（SHA-256
> `1ba52e5f8a947f54c143262cebd3df99070ff7580026165407ddfc23cfeb6289`），按 session、连续
> sequence、单调时间、primary→range→geolocation 顺序、
> GPS 质量/新鲜度和字段一致性重建 336 张卡。独立 bundle 为
> `artifacts/evaluation/combined-target-card-reconstruction-20260930-r2/bundle.json`，
> 487863 bytes，SHA-256
> `abd3f4fd91e099213df1ae15a32131499d6f50c4552be302ec99bce3d211ad3e`；report 为
> `docs/evidence/combined-target-card-reconstruction-20260930-r2.json`，3709 bytes，SHA-256
> `04375026626680e88012c4c851d896f8c7c41edafaba84b23e697add7f5738da`。r2 修正了卡片 provenance
> 中 label causal sequence 的记录，旧 r1 产物保留但不作为当前索引；旧 source 字节未改变；
> 当前 parity 只作为工具上下文，未被写回历史证据。此轮
> `runtime_reexecuted=false`、`current_external_sim_acceptance=false`、`release_allowed=false`，
> 不提升 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

> 2026-09-30 Gazebo 深度 transport 已接入当前外部 adapter：x500、standard_vtol、rc_cessna
> 的仓库模型新增与 RGB 同位姿的 `R_FLOAT32` depth camera；`scripts/gazebo_sensor_stream.py`
> 在显式 depth contract 下订阅精确 scoped depth topic，并在每个 `MDGZSIM3` RGB record 后追加
> 一个 `MDGZDEP1` companion。companion 以整条 record SHA-256 绑定 RGB sequence、仿真时间戳、
> depth payload、完整校准文档 SHA、`registered_to_rgb`/`rgb_pixel_aligned` 和
> `optical_axis_z_m` 语义；主机 `GazeboSimFrameSource` 只保留当前 frame 的 depth side-channel，
> 按 `frame_id` 采样并转换为现有 `DirectRangeMeasurement(source=gazebo_depth)`。
> 缺 companion、未来/超过 100 ms 的 depth、错 topic/校准/registration 或坏 hash 均 fail-closed，
> RGB 默认路径保持原 wire-v3 不变。已通过 71 条 depth/helper/backend 回归和 LiveMissionRunner
> 全部现有回归；随后在 pinned `px4io/px4-sitl-gazebo` 容器的
> `urban_inspection_multirotor_001` 场景真实运行 1 帧：`gate_passed=true`、GPS/heartbeat/传感器
> transport 通过，`depth_enabled=true`、`depth_companion_count=1`，容器已停止并删除。证据为
> `artifacts/evaluation/depth-external-20260930.json`（45,121 bytes，SHA-256
> `2e9bffb70074548636558a48c11291df06dd4cd33763def0da3d8bd2e470924c`）。这只证明真实
> PX4/Gazebo depth transport；不证明 Mission 事务、QGC 人工视觉、HIL/REAL、室外 GNSS、硬件或
> 最终 `0.3.0` 发布门。

> 2026-09-30 同一 disposable PX4/Gazebo 运行时已进一步完成多旋翼组合验收：在当前
> `SharedTaskPolicy` artifact、现有 planner revalidation 和同一 Mission
> upload/readback/commit/permit 生命周期下，真实接收 2 帧 RGB + `MDGZDEP1` depth companion，
> GPS/heartbeat、深度 scoped topic、深度校准绑定、Mission readback hash 和清理均通过。
> 证据为 `artifacts/evaluation/external-multirotor-depth-shared-policy-20260930.json`（59,282
> bytes，SHA-256 `30157bf0f704b0b694056a0fdb75dd972c1a65be63d889ec2b70648fdaf85982`）；其中
> `gate_passed=true`、`mission_transaction_gate_passed=true`、`sensor_transport_gate_passed=true`、
> `shared_policy_used=true`、`planner_revalidated=true`、`depth_companion_count=2`，策略未接管
> actuator，`flight_started=false`、`physical_output=false`，容器已停止并删除，且运行绑定当前
> release parity。该证据关闭的是 external multirotor Mission + depth transport 的软件运行子门，
> 不等同于 QGC 人工视觉、HIL/REAL、室外 GNSS、硬件、实飞或最终 `0.3.0` 发布门。

> 上述首轮组合证据现仅保留为历史记录：审计发现它的 SharedTaskPolicy/Mission 使用了测试占位
> airframe hash `bbbb…`，而 live sensor transport 记录了另一个稳定机型 hash，因此不能作为
> 当前机型身份绑定证据。它没有被覆盖；当前索引改指向下面的 r2。

> 2026-09-30 组合验收 r2 已修正机型身份链：新的多旋翼 policy artifact、compiled Mission、
> `sensor_transport.airframe_profile_sha256` 和 Mission runtime attestation 全部绑定同一个实际
> external plan hash `5bd121275d285e27adef2a8f4b4242b481dffb8368ec4dd231089eeac8732d47`；sensor、
> Mission 与 Docker runtime container ID 也逐字段相等，executable Mission hash 与 wire/protocol
> hash 相等。证据为 `artifacts/evaluation/external-multirotor-depth-shared-policy-20260930-r2.json`
>（59,675 bytes，SHA-256 `af58d0650c8db7f9dfe2a3fd76fedf41775e9428ae78607d2c07bfdccd75ddac`）。
> r2 的 2/2 depth companion、GPS/heartbeat、SharedTaskPolicy、planner revalidation、Mission
> transaction、当前 parity 和清理均通过；`flight_started=false`、`physical_output=false`，策略未
> 接管 actuator。仍不代表 QGC 人工视觉、HIL/REAL、室外 GNSS、硬件、实飞或最终 `0.3.0` 发布。

> 2026-09-30 同一外部 runner 已扩展到固定翼：固定翼场景原先只有 RGB camera，虽然
> `rc_cessna_mono_cam` SDF 已提供同位姿 depth camera，但没有场景级 `depth_camera` 声明，
> 因而此前只能证明 RGB/ Mission，不能证明固定翼 depth。现在 runtime manifest 增加
> `forward-depth`，并更新其 canonical channel hash；新的 disposable 运行证据为
> `artifacts/evaluation/external-fixed-wing-depth-shared-policy-20260930-r2.json`（59,934 bytes，
> SHA-256 `a672e2f1f518d6f909c344d3ee337ab41021b34c1ff5526d46d65193d7a6fc06`）。本轮
> `gate_passed=true`、`mission_transaction_gate_passed=true`、`sensor_transport_gate_passed=true`、
> `shared_policy_used=true`、`planner_revalidated=true`，真实接收 2 帧 RGB 与 2 个
> `MDGZDEP1` companion；depth topic、`gazebo-fixed-wing-depth-v1` calibration、Mission、
> sensor 与 Docker container identity 交叉绑定，容器已清理。该证据仍只关闭 fixed-wing
> external software runtime 的 Mission + depth 子门；当前进度 verifier 会重新打开证据、场景
> runtime channel 和 `forward-depth` 声明并复算 canonical hash。`flight_started=false`、`physical_output=false`，
> 不代表 QGC 人工视觉、HIL/REAL、室外 GNSS、硬件、实飞或最终 `0.3.0` 发布。

> 测距语义已进一步收口：`GazeboDepthFrame` 必须声明 `ray_slant_m` 或
> `optical_axis_z_m`；后者必须同时提供 `fx/fy/cx/cy`，采样时按像素射线归一化为
> `ray_slant_m`，禁止把 optical-axis Z 直接冒充斜距。`GazeboDepthRangeSample` 只接受已归一化的
> `ray_slant_m`。这仍是独立的 truth-free Python seam，不代表 Gazebo transport 已接通。

> 2026-09-30 独立 holdout 软件 SIM 已重新执行：训练输入 3 条、独立评估输入 12 条，
> 输入 hash 和 scenario family 均不相交，planner revalidation 通过；parent/candidate
> ranker hash 仍为 `ebfedee6...`，candidate 未 promotion。证据为
> `docs/evidence/shared-policy-independent-sim-20260930.json`（SHA-256
> `e33da7ed...`），这仍是软件规划证据，不代表动力学、HIL/REAL、GNSS 或发布通过。

> 2026-09-30 parity 收口：当前源码已重新生成 `configs/release-parity.json`，
> `core_algorithm_hash=b6146277...`、`px4_mission_path_hash=58b94395...`、
> `ui_contract_hash=135fdfa4...`；旧 external receipt 保持不可变，新绑定写入
> `20260930-depth-r4` 后缀文件。当前 progress index、Ruff 与 `git diff --check` 已重新
> 对齐；发布门继续保持 `0.3.0-pre.8` / `release_allowed=false`。

> 2026-09-28 VTOL 状态边界修复：附加 evaluator 在 DISARM 后新增有界落地稳定屏障，
> 必须同时观测未解锁和 `MAV_LANDED_STATE_ON_GROUND` 才交接给后续 Mission evaluator。
> 同轮把 `VFR_HUD` 纳入 combined evaluator 显式 10 Hz stream，补齐能量 replay 的
> throttle 输入；相关回归与 Ruff 已通过，新的外部 combined 仍被 energy
> `load_or_receive_gap` 拦截，尚未生成通过证据。
> 随后重跑 `combined-sim-qgc-20260928T094934Z-24784` 在 Gazebo 主题就绪前容器 exit 137，
> 这是外部 Docker/Gazebo 资源失败，没有生成 Mission 证据。
> 当前宿主可用内存约 0.95 GiB，Docker 有 13 个其他长期运行容器；本项目不擅自停止或
> 重启这些容器，因此暂缓新的长时间 combined SIM。

> 2026-09-28 外部组合验收修复：combined wrapper 现在显式传递容器内 MAVLink 输入端口和带
> provenance 的 synthetic SIM 能量参数；QGC 工具栏 DPI 线程上下文采用同线程原始值恢复并有限重试。
> PX4/Gazebo/QGC、Mission 事务和能量连续性已实际跑通，但河流场景目标测距仍因
> `terrain_uncertainty_unbounded` fail-closed，未落假点；最终外部组合 gate、HIL/REAL、室外 GNSS、
> 硬件和最终 `0.3.0` 仍未通过。

> 2026-09-28 能量 replay 修复：重复 `GLOBAL_POSITION_INT` boot 时间戳不再制造伪造的回放失败，
> 真实时间回退仍然拒绝；相关回归已通过。

> 2026-09-28 后续收口：目标证据卡已接入 integrated SIM/QGC ranging acceptance 和
> combined-SIM verifier。每个可用定位样本现在必须 materialize 为带 `run_identity_sha256`
> 的 `TargetEvidenceCard`；verifier 会重新校验卡片 canonical schema、只读 claims、计数、
> 错误列表和 runtime identity。历史未配置 artifact 保持可读，新配置 acceptance 缺卡或身份漂移
> 会 fail-closed。相关定向回归已通过；这仍是软件/SIM 证据，不增加 QGC 人工视觉、HIL、REAL、
> 室外 GNSS、硬件或最终 `0.3.0` 发布证据。
>
> 独立 source-bound evidence：`docs/evidence/target-evidence-card-integration-20260928.json`，
> SHA-256=`42F0F1DCE4F43298D655F5D118B9EE34A47B2104ED5F55D9FB2F7222F7072974`；已登记到
> `docs/current-progress.json` 的 `target_evidence_card_integration_20260928`。

> 2026-09-28 继续补齐目标落图证据链：新增 `TargetEvidenceCard` typed contract，把同一
> `TargetEvent` 和 canonical WGS-84 `TargetGeolocation` 结果绑定为 hashable、只读卡片。只有
> GPS 合格且新鲜、测距可用、定位结果存在时才保留坐标；否则输出 `available=false` 与明确原因，
> 坐标字段保持 `null`。该项已通过独立回归，不改变当前 parity、也不宣称 QGC 人工视觉、HIL、
> REAL、室外 GNSS、硬件或最终 `0.3.0` 已通过。

> 2026-09-28 当前已把 `SharedTaskPolicy` decision identity 收口为一个共享纯校验函数
> `policy_decision_matches_selected_task()`，并接入 vehicle-neutral Mission 编译、observed session、
> 预算型软件 SIM 与 VTOL external campaign。合法 `ABSTAIN` 仍然不绑定 Mission 并走既有拒绝路径；
> 只有 `SELECT` 且 task id、policy artifact canonical hash、`truth_free=true`、
> `planner_revalidated=true` 全部一致时才允许继续编译/事务。相关 369 条跨模块回归已通过，
> 证据回归 85 passed。当前 `configs/release-parity.json` 的 `core_algorithm_hash=7be5f567...`，
> parity 文件 SHA-256=`7afcf0bbf94448a067c9ce1200a27de1cea7040d7f4bd35ca830bd02f30c1ebd`；当前 source-bound
> 证据为 observed session r20、runtime matrix r6、vehicle matrix r5、external Mission matrix r9、
> pair r4，release audit 为
> `docs/evidence/shared-policy-release-parity-policy-helper-20260928-r5.json`。这些都是合同/外部 SIM
> 证据，不增加 HIL、REAL、室外 GNSS、硬件或最终 `0.3.0` 发布证据。

> 随后用当前 wrapper 默认链生成 prerelease gate：
> `artifacts/evaluation/release-gate-current-20260928-r6/release-gate-pre.8-current-20260928T011500Z-v6-bound-report.json`。
> external runtime/AI join、PX4 response matrix/campaign 已全部重绑到当前 parity，四个 mode 的
> parity mismatch 已清零；当前 audit 为 `docs/evidence/shared-policy-release-parity-policy-helper-20260928-r5.json`，
> blocker 从旧报告的 45 收敛到 32。剩余 blocker 仍是 replay-set 部署域/审核、真实 field-link freshness/
> hardware、人工视觉/Mission、HIL/REAL 和 prerelease channel，`gate_passed=false`、
> `release_allowed=false` 保持不变。

> 同日对现场链路进行了新的只读三次 watch preflight：
> `artifacts/evaluation/field-link-preflight-current-20260928-r2.json`，3/3 样本失败，Jetson
> `192.168.144.20:22`、GR01 `192.168.144.11:5760`、Camera `192.168.144.108:554` 均超时；本机
> 选出的源地址仍为 `192.168.1.13`，不在要求的 `192.168.144.0/24`，所以
> `hardware_link_ready=false`；绑定的只读接口诊断为
> `artifacts/evaluation/field-link-interface-diagnostic-current-20260928.json`，确认 Ethernet 当前为
> `192.168.1.13/24`，没有 `192.168.144.0/24` 地址/路由。该探针没有修改网卡/路由、没有登录设备、没有发送 MAVLink 或飞控命令，
> 这是一份新鲜的阻断证据，不是 HIL、REAL、室外 GNSS 或发布授权。

> 2026-09-28 在当前 checkout 重新执行了完整的 VTOL SharedTaskPolicy external campaign：
> `artifacts/evaluation/shared-policy-current-vtol-campaign-20260928-r2.json`，固定 digest 的
> disposable PX4/Gazebo runtime 中按 `synthetic-east → synthetic-west` 完成 2 架次。两架次均经过
> `authorized → uploading → uploaded → readback_verified → committed → start_permitted`，router
> 记录 35 个 Mission 帧、14 个 Mission item、2 次 AUTO.MISSION mode change 和 2 个受限 control
> forward；policy artifact、planner revalidation、truth-free、journal hash-chain、exact-ID cleanup
> 与 post-remove absence 全部通过。独立 receipt 为
> `artifacts/evaluation/shared-policy-current-vtol-campaign-20260928-r2.receipt.json`，receipt
> canonical SHA-256=`dddfc55143ee676b311ac47aee5fbfdd26731a6c5a5aa97ee84cd0cf4219a81c`。
> 第一次用 240 秒 mission timeout 的尝试因 campaign 自身 600 秒任务预算被正确 fail-closed，
> 原始失败 evidence 保留；r2 使用合同允许的 600 秒并通过。这仍然是 headless external SIM，
> 不构成 QGC 视觉、HIL、REAL、室外 GNSS、硬件、实飞或最终 `0.3.0` 发布证据。

> 本轮代码与证据索引变更后的全仓回归已完成：
> `artifacts/evaluation/full-regression-policy-helper-20260928/pytest.xml`，JUnit 共 5138 项，
> `5131 passed / 7 skipped / 0 failed`，退出码 0，耗时约 217.28 秒，JUnit SHA-256=
> `88d63055f256cdfc109fab43f576d2f399f30cc0855b16901e847c8b39636033`。这只证明当前软件回归，
> 不替代 external PX4/Gazebo、HIL、REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

> 随后在当前 checkout 真实执行了一次 disposable PX4/Gazebo VTOL runtime：
> `artifacts/evaluation/simulator-docker-external-policy-helper-20260928.json`，固定 digest 镜像、
> `river_rescue_vtol_001` manifest、qualified heartbeat/GPS、只读挂载、loopback 端口、容器
> inspect 和 stop/remove 清理均通过；`flight_control_commands_sent=0`，不声明 Mission、HIL、REAL、
> 室外 GNSS、硬件或 QGC visual。随后用同一 scenario 重新执行 production AI target exercise：
> `artifacts/evaluation/simulator-docker-ai-external-policy-helper-20260928.json`，AI policy、target
> policy、truth-free perception 和 TRACK decision 均通过；arm/disarm 仅发生在 disposable SITL，
> 不接触真实 Pixhawk。三机型 runtime/AI join verifier 通过，结果为
> `artifacts/evaluation/simulator-external-evidence-policy-helper-20260928-r2.json`；固定翼/多旋翼
> 使用已有同 digest source evidence，VTOL 使用本轮新生成的成对 evidence，不能把该 join 写成三机型
> 同时新跑，也不能升级为 HIL/REAL 或实飞结论。

> 2026-09-27 当前代码新增 `SharedTaskPolicy` decision identity 的 fail-closed 校验：
> `run_observed_task_session` 现在同时验证 `action=select`、task id、policy artifact canonical hash、
> `truth_free=true` 和 `planner_revalidated=true`；任一字段漂移都会在进入任务执行前拒绝。对应
> `test_observed_shared_policy_rejects_decision_identity_drift` 已通过。由于核心文件变更，
> `configs/release-parity.json` 已更新为 `core_algorithm_hash=0cb232a71c3af5c53694858bfbec9d214b5e25932705d265cc2ad59b6a039c3e`，
> 当前 parity 文件 SHA-256=`421a210195de899bc32d0a02d1167bf4b448d77aaf46dc5bb7c66585ec1b882b`。
> 旧证据没有覆盖；当前证据以新的 `r19` observed session、`r5` runtime matrix、`r4` vehicle matrix、
> `r8` external Mission matrix 和
> `docs/evidence/shared-policy-release-parity-budget-decision-20260927.json` 为准。
> `verify_current_progress_index.py` 已通过，release 仍为 `0.3.0-pre.8` 且 fail-closed；这次重绑定
> 不增加 HIL、REAL、室外 GNSS、硬件或最终 `0.3.0` 证据。

> 预算型软件 SIM 现已与 observed session 对齐：`simulate_task_budget_episode()` 在使用
> `SharedTaskPolicy` 时先校验 airframe profile，再校验 `action/task_id/policy_artifact_sha256/`
> `truth_free/planner_revalidated`；决策身份漂移不会进入 Mission 编译。新增正向、profile mismatch
> 和 artifact drift 回归；这只是 fail-closed 合同增强，不是物理能耗或 HIL/REAL 性能证明。

> 本轮变更后的全仓回归已完成：
> `artifacts/evaluation/full-regression-shared-policy-decision-identity-20260927-r2/pytest.xml`，
> 共 5136 项，`5129 passed / 7 skipped / 0 failed`，退出码 0，JUnit SHA-256=
> `d348200827aa5ade774f26e28ce5cad6b73df825243a55ec1a19164c7b0bc16d`。旧的 5135 项回归仍保留
> 为历史记录；本结果才覆盖本轮 decision identity 校验、current parity evidence 和测试夹具迁移。

> 预算型 SIM decision identity 改动后的最新全仓回归已完成：
> `artifacts/evaluation/full-regression-shared-policy-budget-decision-20260927-r2/pytest.xml`，
> 共 5137 项，`5130 passed / 7 skipped / 0 failed`，退出码 0，耗时约 190.911 秒，JUnit SHA-256=
> `1316f8a1450ca7992200270bcbb9c6efd8709ec317104ecb10f258ebd05c6ddd`。仍只证明软件回归，
> 不替代 external PX4/Gazebo、HIL、REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

> 2026-09-27 共享策略已扩展到 fixed-wing/multirotor 的 vehicle-neutral external runner：
> `configs/evaluation/external-shared-ranker-v1.json` 与
> `configs/evaluation/external-shared-policy-b-profile-v1.json` 通过严格文件 hash 和
> canonical identity 加载。两次新的 pinned Docker/PX4/Gazebo Mission transaction
> （fixed-wing r12、multirotor r6）均在同一 `SharedTaskPolicy` 下通过，证据记录相同
> `ranker_sha256=ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`、
> `policy_artifact_sha256=41895895cdf8780cb117667e011dab975e1c4878807a3b6b234a8a5a0c5ad285`，
> 以及 `planner_revalidated=true`、`policy_applied_to_actuator=false`。配对 receipt 为
> `docs/evidence/external-shared-policy-pair-20260927-r2.json`，文件 SHA-256=
> `b3791703a00f75c6599f9d1673c24c0f308faf37a80a1eafeff55353fd8f7a12`，receipt canonical SHA-256=
> `995cf9c6046ed75fb6233f57d76b001c081d012f7b20179cb910759c1f7e913d`。该 receipt 只证明
> 两个 external-SIM adapter 使用同一高层策略；VTOL 的既有 r14 campaign 仍是独立的
> `SIM_EXTERNAL_CAMPAIGN` 证据，不把不同历史 artifact 哈希强行宣称为同一份模型。
> HIL、REAL、室外 GNSS 硬件、实飞、QGC 人工视觉签字和最终 `0.3.0` 仍保持 fail-closed。

> 随后已完成 VTOL 的同 ranker 重跑：从原 `vtol-ranked-routes` 派生出
> `configs/scenarios/vtol-shared-policy-routes.synthetic-campaign.json`，任务和 runtime
> 几何保持不变，只替换为 fixed-wing/multirotor 同一 canonical ranker
> `ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`。新的 VTOL
> `SharedTaskPolicy` artifact 仍按 `px4_vtol_gazebo_v1` profile 单独绑定；这是 profile
> 隔离，不是另起一套模型。两架次 external PX4/Gazebo Mission、policy evidence、journal
> hash-chain 和 exact cleanup 均通过。严格跨机型 receipt 为
> `artifacts/evaluation/shared-policy-vehicle-matrix-20260927-r3.json`，其
> `same_high_level_ranker=true`、`unified_policy_model_proven=true`，同时明确
> `same_policy_artifact=false`（artifact 按 airframe profile 绑定）。该证据仍只覆盖
> headless external SIM；HIL、REAL、室外 GNSS、实飞和最终 `0.3.0` 发布门不变。

> 同一 VTOL profile-bound artifact 已重新通过四模式 runtime contract matrix：
> `artifacts/evaluation/shared-policy-runtime-matrix-20260927-r4.json`，文件 SHA-256=
> `df5a7fe95b77153428b15152143c2079fb11fbf16076a6396ed95e625d4dcd1b`，artifact canonical
> SHA-256=`9a6ac988d5f4527f8903151dfd0947653d5d3b69781ec283b00c867348f6e27f`。REPLAY/SIM/HIL/REAL
> 四行共享当前 release parity、UI contract 和 airframe profile；四行均无
> `FLIGHT_CONTROL_OUTPUT`，没有启动 backend、没有发送 MAVLink。该项是运行 profile/合同软件
> 证据，不是 HIL、REAL、室外 GNSS 或实飞证据。

> 当前 observed session 已重新绑定同一 VTOL profile-bound artifact、同一 canonical ranker 和当前
> `configs/release-parity.json`：`artifacts/evaluation/shared-policy-observed-session-20260927-r18.json`
> （4343 bytes，SHA-256=`039c7c9295123076e06d4c1f1af85a8e5ecd7ae02c7f73705c71769572d0ad8b`）。
> session 在 truth-free ContextSnapshot 上按 `synthetic-east → synthetic-west` 完成两项任务；每一步
> 都重新通过 planner、保存 Mission plan 与 policy/ranker identity，deterministic repeat 通过，且当前
> index verifier 会重算 session canonical hash、campaign hash、artifact canonical hash 和 release parity。
> 该项没有启动 backend、没有发送 MAVLink、没有 actuator capability；它是软件 observed-session 证据，
> 不抵扣 external PX4/Gazebo、HIL、REAL、室外 GNSS、实飞或最终 `0.3.0` 发布门。

> 2026-09-27 当前工作区完成带 JUnit 归档的全量 Python 回归：
> `artifacts/evaluation/full-regression-shared-policy-final-20260927/pytest.xml`，
> 5135 tests，`5128 passed / 7 skipped / 0 failed`，退出码 0，耗时 348.29 秒，文件 SHA-256=
> `bbebd926aab571b3cc264076387b2138d025706b218ca02a3d80f50c1894b2dc`。这覆盖当前源码、共享
> 策略、Mission、运行时矩阵、外部 adapter 和进度索引；它仍只是软件回归，不构成 HIL/REAL、室外
> GNSS、硬件或最终 `0.3.0` 发布证据。

> 2026-09-27 三机型 external Mission 矩阵已收口：固定翼、多旋翼和 VTOL 的当前证据被
> `docs/evidence/external-mission-matrix-20260927-r7.json` 严格绑定到同一
> `ExternalPx4MissionExecutionAdapter`、Mission compiler 和
> `authorized → uploading → uploaded → readback_verified → committed → start_permitted`
> 事务路径。固定翼/多旋翼为 `SIM_EXTERNAL`，VTOL 为 `SIM_EXTERNAL_CAMPAIGN`；各自的
> protocol hash 与 PX4 readback hash 一致，源证据哈希、live GPS context、容器清理和
> `flight_started=false` 均由 receipt 记录。当前 receipt 同时明确
> `current_release_parity_bound=true`：固定翼 r10、多旋翼 r4 和 VTOL r10 都重新绑定当前
> `configs/release-parity.json`；固定翼/多旋翼是本轮新执行的 Docker/PX4/Gazebo Mission run，
> 不是旧报告重绑。receipt SHA-256=
> `b23394bbc73378f1e54fb2824d6830f3bb17d5a881f79aa1d873d059599444b3`。
> 固定翼/多旋翼的 vehicle-neutral 入口为
> `scripts/run_external_vehicle_mission_campaign.py`，两份新 report 都由同一
> `select_and_compile_task`/Mission compiler/transport runner 生成；每份 report 还绑定
> `task-binding.json`，重新验证 `planner_revalidated=true`、`truth_free=true` 和
> `policy_applied_to_actuator=false`，并在 receipt 中保留 `flight_started=false`、物理输出关闭和
> exact cleanup 结果。
> 独立复核命令为
> `python scripts/verify_external_mission_matrix_receipt.py --root . --receipt
> docs/evidence/external-mission-matrix-20260927-r7.json`。这只证明 external SIM Mission
> 事务闭环，不是 HIL/REAL、室外 GNSS 硬件、实飞、QGC 人工视觉签字或最终 `0.3.0` 发布。

> 2026-09-27 fixed-wing 真实 external Mission transaction 已通过：使用 pinned
> Docker/PX4/Gazebo runtime、ownership/heartbeat/GPS qualification 和 live GPS/context
> 绑定后，统一 `ExternalPx4MissionExecutionAdapter` 完成
> `MISSION_ITEM_INT` upload/readback/ACK/commit/permit；state path 为
> `authorized → uploading → uploaded → readback_verified → committed → start_permitted`，
> PX4 readback protocol hash 与编译 hash 一致，容器 stop/rm 成功。证据：
> `artifacts/evaluation/external-fixed-wing-mission-20260927-r8.json`，文件 SHA-256=
> `71cd82716d540e49ebfafae6709acd54c4e514456e05ccc52855fd5b27b1e7a8`。r7 的
> TAKEOFF pitch/LAND heading 规范化失败记录仍保留为诊断历史；r8 已通过协议规范化后的
> hash。该结果仍是 `SIM_EXTERNAL` Mission transaction，不是起飞、HIL/REAL、室外 GNSS
> 硬件或最终 `0.3.0` 发布证据。

> 2026-09-27 multirotor 真实 external Mission transaction：使用 pinned Docker
> PX4/Gazebo image、当前 `urban_inspection_multirotor_001` world 和共享
> `ExternalPx4MissionExecutionAdapter` 实际完成 ownership/heartbeat/GPS qualification、
> `MISSION_ITEM_INT` upload/readback/ACK/commit/permit；state path 为
> `authorized → uploading → uploaded → readback_verified → committed → start_permitted`，
> readback protocol hash 与 compilation hash 完全一致，live GPS 与预编译 context 的 WGS-84
> 偏差约 0.047 m，容器 stop/rm 成功。证据：
> `artifacts/evaluation/external-multirotor-mission-20260927-r2.json`，文件 SHA-256=
> `918703b5c1d6715481e8f988cfc4c2e5bb07e80b7d5a6f733c9bd2b69017dd1b`。这是
> `SIM_EXTERNAL` Mission transaction evidence，不是起飞、HIL/REAL、室外 GNSS 硬件或最终
> `0.3.0` 发布证据。

> 2026-09-27 fixed-wing external Mission diagnostic（r7 历史记录）：Docker/PX4/Gazebo 容器、ownership、
> heartbeat/GPS 和 cleanup 均通过，但 Mission readback 被正确 fail-closed；PX4 将 TAKEOFF
> `param1=12°` 回读为 `0`，并将 landing heading 规范化为 `270.0`，导致 wire hash 不一致。
> 证据 `artifacts/evaluation/external-fixed-wing-mission-20260927-r7.json` 明确记录失败；该
> 诊断已由 r8 的 PX4 协议规范化修复，仍保留原始失败证据，不能将 r7 当作通过。

> 2026-09-27 外部 Mission bridge 增量：新增
> `scripts/run_external_px4_mission_transaction.py`，把预先由共享
> `PlannedMissionCompilation` 生成的任务、truth-free `ContextSnapshot` 和对应 runtime
> safety policy 接入 Docker acceptance 的 Mission hook。`--execute` 时仅在 ownership 与
> heartbeat/GPS qualification 之后打开 MAVLink，调用 `ExternalPx4MissionExecutionAdapter`
> 完成 `MISSION_ITEM_INT` upload/readback/commit/permit，然后由父 acceptance 统一 stop/rm；
> 不选新任务、不 arm、不切 mode、不启动飞行。dry-run CLI 和缺少输入时的 fail-closed 回归通过。
> 当前仍没有 fixed-wing/multirotor external campaign、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。

> 2026-09-27 Docker acceptance Mission hook 增量：`run_acceptance()` 现在可在同一
> 个已通过 ownership 检查、真实 heartbeat/GPS qualification 的 disposable PX4/Gazebo
> 容器内调用 `mission_transaction_runner`。hook 接收 inspect record、telemetry 和
> loopback endpoint，可接入 `ExternalPx4MissionExecutionAdapter` 的统一
> `MISSION_ITEM_INT` upload/readback/commit/permit；返回值若不是 `gate_passed=true`、
> `flight_started=false`、`physical_output_enabled=false`，本次 acceptance fail-closed，
> 但仍执行 stop/rm 清理。新增正向和越界回归通过。该增量只是生命周期接线与安全边界，
> 尚未运行 fixed-wing/multirotor 外部 Mission campaign，也没有增加 HIL/REAL、室外 GNSS
> 或最终 `0.3.0` 证据。

> 2026-09-27 Docker inspect attestation builder 增量：新增
> `attest_owned_px4_container()`，只读解析 Docker inspect 的 container identity、digest image、
> expected model/purpose、bridge/非 privileged/无设备/无 host mount、精确 loopback control port，
> 并要求调用者单独提供 qualified telemetry。公开 API 的加入使 parity 重新生成：文件 12,308 bytes、
> SHA-256=`3a957a1501e44cc9908307632b43102a0e36192304208dec182006afa30425a2`，
> `px4_mission_path_hash=d408a581e7c93f11f507374780a437e330aba8c709dd501a8ea372e0aea53448`。
> 当前仍未启动/停止容器，也未宣称 fixed-wing/multirotor external flight。

> 2026-09-27 attestation 导出与 parity 最终同步：新增 `ExternalRuntimeAttestation` 的公开
> 导出后重新生成 canonical parity，文件 12,308 bytes、SHA-256=
> `bf96fc52ea8ba3c9554c9ba216017330e85e8cbb1efdf932fb8cc767760f4545`，
> `px4_mission_path_hash=4981dd9e362e7567c5027ccd2ec9c9f325e4ccfad651585aa7b7fea09fa23668`；
> release audit 更新为 `r34`，仍为 prerelease fail-closed。

> 2026-09-27 外部 runtime attestation 增量：`ExternalRuntimeAttestation` 现在严格绑定
> 64-hex container id、digest-pinned image、vehicle/profile、scenario manifest SHA-256、
> ownership checks、qualified telemetry、truth-free 和 hardware-control-disabled 条件。
> 未提供 attestation 时，Mission transport 即使协议通过也保持 `external_runtime_attested=false`；
> 只有 attestation 全部通过才会输出 `SIM_EXTERNAL`/`external_runtime=true`。新增 fake-MAVLink
> 正反例回归通过。此次 parity 文件 12,308 bytes、SHA-256=
> `511aef90c258735fff7512c5471c222cf17926bcd5914530bba5ee3e4251b332`，
> `px4_mission_path_hash=08004d9ae6d480e75a304a2d69380e9ab7fdbd00ff90ddeb2762a53af0e02207`；
> 仍未声称真实 Docker ownership、fixed-wing/multirotor external flight、HIL/REAL、室外 GNSS
> 或最终 `0.3.0`。

> 2026-09-27 外部 Mission transport 增量：新增
> `src/multidetect/px4_external_mission.py` 与
> `ExternalPx4MissionExecutionAdapter`。它接收已经由外部编排器完成 ownership/telemetry
> 检查的 MAVLink connection（ownership 是调用者责任，本层不证明），只执行统一 compiler 产物的
> `MISSION_COUNT → MISSION_REQUEST_INT → MISSION_ITEM_INT → ACK → INT readback → commit → permit`
> 事务；不创建/删除容器、不切换模式、不 arm、不发送 actuator。固定翼和多旋翼 fake-MAVLink
> 回归通过，且验证 legacy `MISSION_REQUEST` 会 fail-closed。该 transport 已纳入
> `px4_mission_path` parity；当前 parity 文件为 12,308 bytes、SHA-256=
> `2153b45a6c4dbe2e08c1371c7c13351cdb2537bc22c6ab5d210c21ee2e781dc9`，
> `px4_mission_path_hash=6234f6020084de7cf87d4abca402c0153391eb7827a25ccbf6d44613e1eff54b`。
> fake connection 结果明确 `external_runtime_attested=false`；当前 release audit `r32`
> 保留旧四 mode parity drift。这仍然不是 fixed-wing/multirotor Docker 外部 Mission flight evidence；HIL/REAL、室外 GNSS、
> candidate promotion 和最终 `0.3.0` 继续关闭。

> 2026-09-27 当前 parity 重绑：campaign loader 的 vehicle-neutral runtime parser 改动属于
> `px4_mission_path` source set，已重新生成 `configs/release-parity.json`（12,155 bytes，文件
> SHA-256=`2bca522a77809a346e7b6e5bb5b4c0259749e55858838e5f28fbfcf01c23eff5`，
> `px4_mission_path_hash=b0f7e0b92a0419fcdcec3c3239435199f252a978f71f39b6e99fa829a0668186`），
> 并以当前只读 field-link 预检重建 release audit `r29`、observed session 和 campaign receipts。
> 这是 source-bound 证据重绑，不是重新运行 PX4/Gazebo；`current_release_parity_bound=false`、
> HIL/REAL、室外 GNSS、candidate promotion 和最终 `0.3.0` 仍保持关闭。

> 2026-09-27 任务输入边界增量：`load_campaign_definition()` 现在接受显式
> `vehicle_class`，并把同一严格 campaign task/runtime 文档送入对应机型的
> `parse_mission_compile_runtime()`；默认值仍为 `vtol`，现有外部 CLI 行为不变。针对
> `vtol`、`fixed-wing`、`multirotor` 的解析回归已通过，但这只是 vehicle-neutral 输入契约，
> 不等同于 fixed-wing/multirotor 外部 PX4/Gazebo Mission lifecycle 已完成。

> 2026-09-27 车辆执行边界增量：新增 `src/multidetect/autonomy/mission_adapter.py` 的
> `MissionExecutionAdapter` 协议和确定性 `InProcessPx4MissionExecutionAdapter`。它复用同一个
> planner/compiler/`MISSION_ITEM_INT` journal transaction，对 `vtol`、`fixed-wing`、`multirotor`
> 做 profile、adapter identity 和上下文一致性校验；该适配器是 software-only，不打开 socket、
> 不 arm/disarm、不产生 actuator output。它为后续独立 PX4/Gazebo vehicle adapter 提供边界，
> 但不把确定性事务回归写成三机型 external SIM、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。

> 2026-09-27 外部 SIM 复跑：使用当前 `release-parity.json`、同一 `SharedTaskPolicy` artifact 和现有
> PX4/Gazebo Mission lifecycle，完成新的 VTOL parent campaign `r14`（2/2 tasks）。共享策略选择、
> planner revalidation、Mission transaction/journal/hash-chain、exact-ID cleanup 与 post-remove absence
> 均通过；receipt 为 `artifacts/evaluation/shared-policy-external-parent-20260927-r14-policycapture-parity-r9.receipt.json`。
> 这是 headless external SIM 的 source-bound 运行证据，`policy_applied_to_actuator=false`；不构成 QGC
> 人工视觉、HIL/REAL、室外 GNSS、实飞、candidate promotion 或最终 `0.3.0` 发布证据。

> 2026-09-27 三机型 Mission transaction 回归：重新运行
> `scripts/run_px4_airframe_transaction_acceptance.py`，`vtol`、`fixed-wing`、`multirotor` 均通过同一
> `compile_executable_mission` → `in_process_mission_item_int` → journalized upload/readback/commit/permit
> 路径。证据为 `artifacts/evaluation/px4-airframe-transaction-20260927-r2.json`，三机型均为
> deterministic SIM、`physical_output_enabled=false`；这补齐了软件层跨机型路径，但仍不是三机型外部
> PX4/Gazebo telemetry、HIL/REAL、室外 GNSS 或实飞证据。

> 2026-09-27 架构增量：新增 `src/multidetect/autonomy/mission_campaign.py` 的 vehicle-neutral
> selection/compiler seam。它把 truth-free `ContextSnapshot`、现有 `SharedTaskPolicy`（或兼容的
> legacy ranker）、planner revalidation 与既有 PX4 Mission runtime compiler 串成一个可注入的核心，
> 只返回 hash-bound `CampaignTaskBinding`，不拥有 MAVLink、arm/disarm、permit 或 actuator authority。
> `tests/test_mission_planning.py` 已对 `vtol`、`fixed-wing`、`multirotor` 三种 deterministic runtime
> 通过同一 seam 回归，并验证低 GPS fix 时 `abstain` 且不绑定 Mission。现有 `SimulatorScenarioRunner`
> 与 `scripts/vtol_selected_task_campaign.py` 的 selection callback 也已复用该 seam；VTOL PX4/Gazebo 外部 acceptance 仍保留为专用 adapter，fixed-wing/multirotor 的
> 外部 telemetry/lifecycle 尚未实现或声明，不能把这组三机型软件编译回归写成三机型外部飞行证据。

> 2026-09-27 parity rebind：由于上述 seam 属于共享 core identity，已重新生成
> `configs/release-parity.json`（12,155 bytes，文件 SHA-256=`71a807f6b4c9a5bdbbe3cdace2d06aec2dc68dc7b71623377e9190665204ba6e`，
> `core_algorithm_hash=97efa609d6ef27c2b69249aee4dc249f2631313d7ca7fc96c42f2452baee5087`，
> `px4_mission_path_hash=8945b1bace35af487ab15b96ac246ba88f9956ac326b27252bd3b30bcb754496`），并对已有
> observed-session、campaign receipt、AI receipt 做 source-bound rebind。旧外部 report 的四 mode
> parity 尚未用该新 core 重新执行，因此当前 release-gate audit `r27` 的
> `current_release_parity_bound=false` 是有意保留的阻断，不是把旧运行冒充新运行；最终 `0.3.0`、
> candidate promotion、HIL/REAL、室外 GNSS 与硬件门均保持关闭。

> 2026-09-27 算法增量：自主 planner 已从“整条路线减去风速模长”升级为逐航段 WGS-84 初始方位 + 风三角预算。
> outbound、centerline、return 每段分别投影 along/cross wind；采样 centerline 会按声明长度保守缩放，顺风不奖励超过静风规划空速，
> 侧风超过规划空速或前向进度不足时直接 `PlannerError`，不再用最低速度掩盖不可行航段。新增
> `tests/test_autonomy_planner.py::test_estimate_uses_directional_wind_triangle_and_fails_closed`，定向 planner 回归与 Ruff 均通过。
> 本次变更更新了 `configs/release-parity.json`：文件 SHA-256=`00e0d8cdc963e1f6536c5ddbce1f850f6baaaa22b2b4cffc0f6216897b20df90`，
> `core_algorithm_hash=3007e2909b1cb4ecb46e19ea09ca31666bc6ed29153131621f69d0914e45d1ea`。observed session 已重算为
> `shared-policy-observed-session-20260927-r13.json`，campaign receipt 为 `policycapture-parity-r7`，AI parity receipt 为
> `simulator-ai-flight-parity-20260927-r6.receipt.json`。当前 release-gate audit 为 `r25`，仍为 32 个阻断；
> 四个 mode 的 parity 已由当前 report-only wrapper 绑定，`current_release_parity_bound=true`，但三机型完整 external
> mode matrix 尚未按新 planner 全部重跑，不会把单一 VTOL campaign 冒充为三机型验证。
> 在该 planner 变更首次登记时尚未重新执行 PX4/Gazebo；当时没有把旧外部运行冒充新 planner 验证，`candidate_promoted=false`、最终 `0.3.0` 仍未发布。

> 2026-09-27 source-bound rebind：从既有 PX4/Gazebo 原始 JSON 重新生成并验证
> `artifacts/evaluation/terminal-continuation-v181/px4-reported-position-response-matrix-v181-current-parity-pre8.json`、
> `.../px4-reported-position-response-campaign-v181-current-parity-pre8.json` 与
> `.../simulator-external-runtime-ai-join-v181-current-parity-pre8.json`，三者均绑定当前 parity 且 verifier 通过。
> 这一步只证明摘要重算和身份一致性；没有把新的方向风 planner 变更冒充成一次新的 PX4/Gazebo 外部运行，
> `new_planner_external_runtime_reexecuted=false`，HIL/REAL、室外 GNSS、硬件和最终发布门保持关闭。

> 2026-09-27 当前 planner 外部实跑：使用固定 digest 的 disposable PX4/Gazebo VTOL runtime 和同一
> `SharedTaskPolicy`/Mission 生命周期完成 2/2 sorties（`synthetic-west`、`synthetic-east`）。两架次的
> `planner_revalidated=true`、policy artifact canonical/file SHA-256 匹配、Mission journal/hash-chain/commit/permit
> 和 exact-ID stop/rm/post-remove absence 全部通过；证据为 `artifacts/evaluation/planner-external-rerun-20260927-r1.json`。
> 该 campaign 的 runtime input canonical SHA=`5fad0b1110e5123d4a803b00ea49c8e17649c835466c650ac23fe3afc82d56f1`，
> 配置风为 north `3.0 m/s`、east `-1.5 m/s`、gust `2.0 m/s`；两架 Mission report 都绑定该 runtime input hash，
> 因此这是带有非零风配置的 VTOL planner 外部 SIM 运行，而不是静风结果。三机型完整 external mode matrix 仍未重跑，
> QGC 视觉、HIL、REAL、室外 GNSS、真实硬件和最终 `0.3.0` 发布仍未声明。

> 2026-09-27 风场绑定独立复核：新增只读 `scripts/audit_external_planner_wind_binding.py`，从当前
> scenario manifest 重载 runtime channel，逐架次重开 Mission evidence，核对 runtime input SHA、
> `ContextSnapshot` canonical SHA 以及 north/east 风值一致。当前 2/2 VTOL sorties 通过，审计文件为
> `artifacts/evaluation/planner-external-rerun-20260927-r1-wind-binding-audit.json`，SHA-256=
> `b1d156e333a93b2fc3594d35ca6d0275b52a62e4bae41eda8852593db6fbd008`。该审计仍不构成风场物理标定、
> HIL/REAL、室外 GNSS 或最终发布证据。

> 2026-09-27 增量：新增外部 Gazebo 传感器故障注入契约。`gazebo_sensor_stream.py`、
> `GazeboSimFrameSource`/`GazeboSimBackend` 与 Docker acceptance 现在支持按收到的图像序号定义的有限窗口，
> 可注入 `image`、`navsat` 或 `odometry` 丢帧；窗口身份进入 sensor binding hash，结束诊断必须证明确实丢帧、
> 窗口结束后有新源恢复、wire-v3 帧恢复且 helper 正常退出。`camera_info` 保持不可注入，避免静态缓存被误判为恢复。
> 本轮已完成并核验三次 disposable PX4/Gazebo 外部故障实跑：image 2 帧、NavSat 4 帧、odometry 4 帧；均实际观察到丢帧、窗口后源恢复、wire-v3 恢复、helper 正常退出和容器清理。短窗口不足的 NavSat 证据仍按失败保留。

> 本轮重新生成并绑定 `configs/release-parity.json`：文件 SHA-256=`00e0d8cdc963e1f6536c5ddbce1f850f6baaaa22b2b4cffc0f6216897b20df90`，
> core algorithm hash=`3007e2909b1cb4ecb46e19ea09ca31666bc6ed29153131621f69d0914e45d1ea`；observed session 为
> `shared-policy-observed-session-20260927-r13.json`，campaign receipt 为 `...policycapture-parity-r7.receipt.json`，
> AI parity receipt 为 `simulator-ai-flight-parity-20260927-r6.receipt.json`。`verify_current_progress_index.py` 返回
> `passed`；当前 release-gate audit 为 `r25`，报告与 manifest 已重新绑定当前 parity，`current_release_parity_bound=true`，但
> `release_allowed=false` 且仍有 32 个阻塞。审计同时保留旧 report 中的历史 field-link 快照，并单独绑定当前 r2 只读快照；
> `candidate_promoted=false`、HIL/REAL/室外 GNSS 均保持未声明。

> 本轮全仓回归为 `5081 passed, 7 skipped, 0 failed`，Ruff 与 `git diff --check` 也通过；另有真实 disposable PX4/Gazebo 外部 sensor fault acceptance：
> `image` 2 帧、`navsat` 4 帧、`odometry` 4 帧窗口均 `gate_passed=true`，分别记录于
> `simulator-docker-acceptance-20260927-r5-fault-image.json`、`...r8-fault-navsat.json` 和
> `...r9-fault-odometry.json`。
> navsat 的 2 帧短窗口证据 r4/r6/r7 均按失败保留，证明恢复不足时 fail-closed；没有将短窗口失败改写为通过。
> 此外此前软件 SIM 故障矩阵、外部 headless SIM、AI flight-response 和策略闭环证据
> 仍按各自 evidence 文件的原边界解释，不能替代 QGC 人工视觉、HIL、REAL、硬件或最终 `0.3.0` 发布门。

> 多步任务决策参考基准已按当前 checkout 重新运行：
> `artifacts/evaluation/task-sequence-reference-20260927-r1.json`（37,568 bytes，SHA-256=`39d9e7fa40c87e06eb52b44cb5d7471afd34986528966379bc7ac8f55eb96f5f`）在 9 个训练/验证/测试案例中贪心 reward `89`、精确参考 `90`，仅 1 案例通过增加预计耗时换取额外 reward；同贪心耗时上限下 reward 没有提升。
> 独立留出输入 `configs/training/task-policy-independent-holdout-v1.json` 的参考报告
> `artifacts/evaluation/task-sequence-reference-independent-holdout-20260927-r1.json`（28,233 bytes，SHA-256=`8a669b9f2647214ba59a3652d1badc75c6205e505dd37daaf5dd8bd441646a19`）为贪心/精确 reward `120/120`、改进案例 `0`、同耗时改进 `0`。该轮仍不宣称 learned weights、独立模型收益、物理性能或最终发布；生产策略继续使用现有 SharedTaskPolicy/PX4 Mission 路径。

> 现场链路只读复核已在当前工作站重新执行：`artifacts/evaluation/field-link-preflight-current-20260927-strict.json`
> 为 11,298 bytes、SHA-256=`91d244bc9b769512d514fc3db47c47c00404fbc06c3808d7eab3d75b3f02439c`；绑定的接口快照
> `artifacts/evaluation/field-link-interface-diagnostic-current-20260927.json` 为 4,379 bytes、SHA-256=`67ee1b4df6f08623763138ab6e08ed0e8be4d4ab4578677553330551ae247d4b`。
> 当前 `Ethernet` 仍为 `192.168.1.13/24`，目标 `192.168.144.0/24` 无候选接口，Jetson/GR01/RTSP
> 两样本 TCP 均不可达，route source 均落在 `192.168.1.13`；`hardware_link_ready=false` 保持。本轮使用
> 项目定义网段的只读接口快照与直接 Python 预检生成证据，没有修改网卡、路由、DNS 或飞控状态。该证据仍不是
> GNSS、HIL、REAL、室外硬件或最终 `0.3.0` 发布证据。

> 新增确定性软件 SIM 故障覆盖与恢复证据：`artifacts/evaluation/simulator-scenario-matrix-fault-coverage-20260927-r2.json`，
> 文件 17,724 bytes，SHA-256=`ba01e7daeb493f5be440e0ea6cd20346a832eebf690f8cb4a64c150f34c63edc`，canonical
> SHA-256=`5547483bdb16a5d7b7d2c0fc9421005d1d4bc9695b44039b335612e12204c9f7`。fixed-wing、VTOL、
> multirotor 共 3 个场景用同一 runner/AI/arm/watchdog 路径，预算从 4 帧自动延长到 2612 帧，所有声明的
> 时序故障 coverage ratio 均为 `1.0`；有限故障在无 AI 控制的同 manifest baseline 对照中全部恢复，
> 无限 battery step 明确标记为不期待恢复，重复运行 hash 一致；独立 verifier 通过。该证据仍是
> truth-free 软件 SIM，不是外部 Gazebo/PX4、QGC 视觉、HIL、REAL、室外 GNSS、硬件或最终 `0.3.0` 发布证据。

> 将该矩阵逻辑与 shared-policy composite receipt、VTOL Mission campaign、sensor capture、release parity、
> Docker acceptance 和 flight model 合并回归后为 `624 passed`；`verify_current_progress_index.py`、
> matrix verifier、Ruff 和 `git diff --check` 均通过。

> 当前审计重绑（2026-09-27）：最新 `configs/release-parity.json` SHA-256 为
> `00e0d8cdc963e1f6536c5ddbce1f850f6baaaa22b2b4cffc0f6216897b20df90`，
> PX4 Mission path hash 为 `0b5230f70199c019f291117e13f95900ddf330f7978b2cfa3913399b41b92372`。
> 当前 observed session 为 `artifacts/evaluation/shared-policy-observed-session-20260927-r13.json`，
> 当前 parent/candidate receipt 为本页上方列出的 `shared-policy-external-20260927-r13-policycapture-parity-r7`
> 文件，
> release-gate audit 为 `docs/evidence/shared-policy-release-parity-drift-20260927-r25.json`；本轮用当前
> parity 重新生成 external-runtime/AI join、PX4 reported-position response matrix/campaign，并将
> replay-set 路径绑定到当前 positive fixture 后生成 `release-gate-current-20260927-r10/` report-only
> release-gate。报告与四个 mode 的 parity 均已绑定当前值，`current_release_parity_bound=true`；剩余 32 个阻塞来自
> replay-set 正式部署覆盖、旧 report 中现场链路快照过期、真实现场链路、QGC 人工视觉、Mission、HIL/REAL 硬件和最终
> release channel，不能由软件或 headless SIM 冒充。当前进度 verifier
> 会实际读取三份外部 fault evidence，校验 source、丢帧数、恢复帧、helper 退出码、telemetry、清理、
> safety 和硬件/视觉未声明边界。
> 文档下方早先的 r4/r9/r6 路径仅保留为历史流水，不覆盖以上当前索引。

> 最新外层 `run_vtol_mission_sensor_capture.py` campaign 已在同一固定 PX4/Gazebo disposable
> runtime 中实际完成 2 架次：`artifacts/evaluation/outer-policy-capture-20260927-r1.json`，
> evidence SHA-256=`498dcd3db823e65545dbf46001617a3a042dc4cffc16bddd0c6c7c6606b4dd4a`，
> `gate_passed=true`。外层 CLI 实际加载并传递同一个 policy artifact，planner 重验为 true，
> 两个 `.policy.json` 均声明 `policy_applied_to_actuator=false`；exact-ID cleanup 和
> post-remove absence 通过。receipt 为
> `artifacts/evaluation/outer-policy-capture-20260927-r1.receipt.json`，SHA-256=
> `72ca43b7c7c4a81289732a30a1c466707414787ed22319944ce7d3e60da247e1`。这是 headless
> external SIM 证据，不是 QGC 视觉、HIL、REAL、室外 GNSS 或实飞验收；`same_ui=false`/
> `qgc_visual_acceptance=not_claimed` 明确保留该边界。

> 重新执行的 disposable PX4/Gazebo sensor transport 已通过：
> `artifacts/evaluation/simulator-docker-acceptance-20260927-r3-execute.json`（74,913 bytes，
> SHA-256=`cbd37d5886f5efa715bc017c1fe5e96cdb2d87f0779a81df2c3fcdbb127fda4b`）。固定 digest
> 镜像实际观察到 `HEARTBEAT`、`GPS_RAW_INT`（fix 3、10 satellites），按 calibration 的
> `1280×960` 几何采集 3 帧 BGR24，raw bytes 与 wire payload SHA 一致，NavSat/odometry/covariance
> 通过同一时间关联；容器 stop/rm 清理通过，flight-control command 为 0。首次用错误的
> `160×90` 请求被严格拒绝并单独保留为 `...20260927-r2-execute.json`，证明尺寸契约 fail-closed。
> 这仍是 truth-free headless external SIM，不是 QGC 视觉、HIL、REAL、室外硬件 GNSS 或实飞验收。

> 同一固定 PX4/Gazebo runtime 的生产 AI target exercise 也已通过：
> `artifacts/evaluation/simulator-docker-ai-acceptance-20260927-r1.json`（123,958 bytes，
> SHA-256=`cbc1fb2995cb9ff7f35ca2b79c8bc1506a154ad92448284b34e278bf525aa115`）。真实 telemetry、
> production `SimulatorAiExecutor`、truth-free perception seam、TRACK decision、shared loopback
> `MANUAL_CONTROL` 和 watchdog/failsafe 均通过；AI policy steps 为 2，target exercise accepted，
> exact container cleanup 通过。该 disposable SITL 记录不声明 PX4 actuator response、QGC 视觉、
> HIL/REAL、室外硬件 GNSS 或实飞；其中 ARM/DISARM 与 AI 控制只发生在临时容器内。

> 在同一生产 AI 路径上继续执行短时 flight-response exercise：
> `artifacts/evaluation/simulator-docker-ai-flight-20260927-r1.json`（672,712 bytes，
> SHA-256=`5c1a963c3fa40847961002b4336b5c5bfb504eb77c269a743d29bfb6b1e300d4`）。运行约
> `4.296s`，收集 `99` 个 `GLOBAL_POSITION_INT` 样本，观测到 `0.10077m` 水平位移（本轮门槛
> `0.1m`），AI step/loopback 控制输出为 `367/367`，flight-response gate 与清理均通过。
> 该结果仍是 truth-free disposable SITL/Gazebo 响应证据；不声明真实 PX4 actuator、QGC 视觉、
> HIL/REAL、室外 GNSS 或实飞效果。

> 已将上述低层 AI flight-response 证据与同一场景的高层 SharedTaskPolicy Mission 证据做成分层
> composite receipt：`artifacts/evaluation/simulator-ai-flight-parity-20260927-r3.receipt.json`，
> 文件 SHA-256=`aead88e5d9cd9f8455c03e6ea98b540439af05be22a87bb2c2f69c14979fa665`，canonical
> receipt SHA-256=`37f6c32a494db57aa36c702d11f92ae6a46a49010de63848d04b5b42846fadc9`。它验证了
> 当前五项 release parity、同一 scenario manifest、同一 airframe profile identity、truth-free
> 边界、planner 重验和 PX4 Mission path 复用；同时明确高层策略只负责任务/Mission 选择，低层
> AI flight response 是独立证据，`policy_applied_to_actuator=false`。这仍不声明 HIL、REAL、
> 室外 GNSS、真实硬件或最终 `0.3.0` 发布通过。

> 该 composite receipt、observed-session 索引绑定后的定向回归为 `612 passed`，覆盖新 receipt、schema、VTOL Mission、sensor
> capture、current-progress/repository docs、release parity、Docker acceptance 与 flight model；
> `verify_current_progress_index.py --root .` 也通过，当前 `release_allowed=false` 保持不变。

> 在此基础上新增了外部 campaign 的 GPS fail-closed 回归：当 shared policy 收到不合格的
> `gps_fix_type` 时，只产生 `TaskSelectionRejected(no_feasible_task)`，不创建 Mission evidence，
> 不进入 Mission transaction，且 telemetry session 的 Mission 计数保持为零。该测试验证的是
> 软件安全边界，不是室外 GNSS 或硬件验收。

> receipt 的结构契约已单独落到 `configs/schemas/simulator-ai-flight-parity-receipt.schema.json`，
> 并由 `tests/test_simulator_ai_flight_parity_receipt_schema.py` 使用 Draft 2020-12 独立校验；
> 这只是证据格式完整性，不会把软件 SIM 证据升级为 HIL/REAL 或室外硬件验收。

> 四模式 shared-policy matrix 已升级为绑定当前 `configs/release-parity.json` 的五项身份：
> core algorithm、PX4 Mission path、safety、task contract 和 UI contract 会被逐模式封存并比较；
> 当前 evidence 为 `artifacts/evaluation/shared-policy-runtime-matrix-20260927-r2.json`，
> `same_policy_artifact=true`、`same_airframe_profile=true`、`same_release_parity=true`、
> `same_ui_contract=true`。这证明的是跨模式软件身份一致性，不是 QGC 画面验收；
> `ui_visual_acceptance=not_claimed` 保持不变。

> 本轮新增的 PX4 receive-only capture 绑定了可选 `SharedTaskPolicy` artifact：采集开始前验证
> policy 文件 SHA-256、canonical policy identity 与 Mission airframe profile；启用策略时 receipt
> 将其作为第七个 `policy_artifact` source 封存，旧六源 receipt 仍兼容。该绑定只证明证据身份闭包，
> 不授予 actuator 权限，也不把 receive-only capture 升级为 HIL、REAL 或实飞验收。

> QGC 原生界面机器采集已在当前仓库可执行文件上完成：
> `artifacts/evaluation/qgc-native-style-sim-20260927-r4/qgc-native-style.png`，
> PNG SHA-256=`41b9a28e58e32b135f317da91a2f2e1ddca9a0c11224ffea27395cea74a5dfda`，
> `process-observation.json` SHA-256=`af76397933b1b6c56909efb9ed47d1489cb7ac279f15ac297603cb463c24aca0`。
> 该次使用远程虚拟桌面实际工作区 `819×566`，精确 QGC 客户区为 `804×528`，外部遮挡清理后
> `capture_gate_passed=true`。为支持这种真实低分辨率工作区，`capture_qgc_native_style.ps1`
> 已把可选下限从 `1024×768/640` 放宽为 `640×480`，默认 `1600×900` 不变，并修正 Win32
> `RECT` 宽高检查。机器采集仍明确为 `operator_visual_acceptance=not_claimed`、
> `qml_rendering_proved=false`、`desktop_capture_gpu_surface_proved=false`；截图中的
> `offline UI validation mode` 提示框没有被伪装成视觉验收通过，HIL/REAL/室外 GNSS/最终发布仍未声明。

> 最新全仓回归（时钟/最终审计修复后）：`5052 passed, 7 skipped, 0 failed`；JUnit 为
> `artifacts/evaluation/full-regression-sensor-clock-final-20260927/pytest.xml`，
> 738,000 bytes，SHA-256=`d11e834e1f55c1c702a923f20dbe0f28e6f80597bbc6637554a39be4239fc7df`。
> 该全仓回归发生在本轮 mission-current 时序修复之前；修复后的相关 VTOL/Mission、索引、
> 文档与 parity 定向回归为 `551 passed`，并已单独记录，不把旧全仓数字冒充为修复后全仓结果。

> 当前外部 VTOL campaign 接入已落到现有 Mission 生命周期：`SharedTaskPolicy` 的 artifact/airframe
> 身份、truth-free observed context、planner 重验、编译 plan 一致性、abstain fail-closed、逐架次
> policy evidence 和旧 ranker-only 兼容路径均已覆盖。定向回归为 `517 passed`，JUnit 为
> `artifacts/evaluation/shared-policy-vtol-campaign-binding-20260927/pytest.xml`（93,445 bytes，
> SHA-256=`f94ae9998501d52935e0f6f7944469dd9fb044c0c9d41087d5afd2088e695ac3`）。该条目证明
> 软件接口与 fixture 回归，不宣称本次新执行了 PX4/Gazebo 外部 campaign，也不升级为 HIL、REAL、
> 室外 GNSS 或最终发布通过。

> 最新 parent/candidate 外部 VTOL campaign 已在同一固定场景、同一 PX4/Gazebo 镜像、同一 router/
> Mission 生命周期下完成：parent `artifacts/evaluation/shared-policy-external-parent-20260927-r13.json`
> 与 candidate `artifacts/evaluation/shared-policy-external-candidate-20260927-r13.json` 均为
> `gate_passed=true`，各完成 2 架次，exact-ID 容器清理和 receipt 均通过。两份 artifact 的
> canonical policy SHA-256 都是 `98b1474912ec031a53818d84c7190f415e68a965901bf91ffd6d92dc98dcadc2`，
> 文件 SHA-256 都是 `b38036c414f8520da01927d5f70a0f94a4d537db6837b4d1ae6d831284507206`；
> 当前 parity 重绑后的 parent/candidate receipt 分别为
> `artifacts/evaluation/shared-policy-external-parent-20260927-r13-policycapture-parity-r4.receipt.json`
>（5,516 bytes，SHA-256=`0a882ea166fd5b520be9daa1b107a6f2803402966fc5ad4c69f6327f7b30db50`）和
> `artifacts/evaluation/shared-policy-external-candidate-20260927-r13-policycapture-parity-r4.receipt.json`
>（5,540 bytes，SHA-256=`2b497956956cfb76f796ffad4b3ea3ae11ce0fbc64841a4d54d652cf0fb7a444`）。
> 本次还验证了第二架次在提交并进入 AUTO.MISSION 后、ARM 前重置 mission current；这只证明
> truth-free headless 外部软件 SIM 的任务闭环，不证明 candidate 有性能收益，也不宣称 QGC 视觉、
> HIL、REAL、室外 GNSS、实飞或最终 `0.3.0` 发布通过。

本文件与 `docs/current-progress.json` 的当前索引更新时间已重绑至 `2026-09-27T00:00:00Z`；旧历史条目仍保留，但不作为当前权威状态。

> 当前源码对应的 Windows Release portable 目录已重新安装为
> `build/windows-v0.3.0-pre.8-release/portable-current-20260927-r1`。增量 Ninja 构建报告
> `no work to do`（本轮没有 C++/QML 源码变化），但完整 portable 依赖闭包已重新安装；主 EXE
> 为 46,875,136 bytes，SHA-256=`41ea7aaf830b406aadbcbb17bd7e5e1e22c1a08f20dcba002885e0153cf6765b`。
> 运行 `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` 退出码为 `0`；
> 8 个 C++ 自检全部退出码 `0`，其中 Python 生成的 44-fragment depth-grid fixture 与 C++
> 重组往返通过。机器运行报告为
> `artifacts/evaluation/portable-current-20260927-r1/runtime-smoke.json`（1,966 bytes，
> SHA-256=`bd0704e0cc0da2926941022d110f15585ded4a4167993e5321f704d60729f5aa`），自检报告为
> `artifacts/evaluation/portable-current-20260927-r1/selftests.json`（1,408 bytes，
> SHA-256=`f2b484621d25d4b9a90d433f1a98eadd7733e401b0c54c0f155441a94003e8a6`）。这仍是
> prerelease 软件/portable 运行证据，不是 QGC 人工视觉、HIL、REAL、室外 GNSS 或最终发布通过。

> 同一新 portable 目录的原生 QGC 客户区采集也通过机器门：
> `artifacts/evaluation/qgc-native-style-portable-current-20260927-r1/qgc-native-style.png`，
> PNG SHA-256=`6e45fc75cff1eda47e59dc2cac562b47042192463abf259bb57e5149755e4fa5`；观察报告
> SHA-256=`26fbf2c6b0d1eda964b7042b999fe6540c5e146b4724e31634c7c668c51880d1`。该报告绑定的
> EXE 正是上述 portable 目录、SHA-256=`41ea7aaf830b406aadbcbb17bd7e5e1e22c1a08f20dcba002885e0153cf6765b`，
> `capture_gate_passed=true`、`external_occluders_after=[]`；人工视觉验收仍为
> `operator_visual_acceptance=not_claimed`，没有把离线提示框或机器截图升级成 QGC 视觉通过。

> 2026-09-27 release-gate 重新绑定：使用当前 `configs/release-parity.json` 重新生成
> `artifacts/evaluation/release-gate-current-20260927-r1/` 下的 external-runtime/AI join、
> revalidation、PX4 reported-position response matrix 和 response campaign，随后生成
> `artifacts/evaluation/release-gate-current-20260927-r4/` 的 report-only manifest/report。
> 新报告 SHA-256=`b6aec28942f5db31fdd9ffad77f419dbeb941cb7b67a89352929db198aed3239`，
> `gate_passed=false`、`release_allowed=false`，但当前 parity 已绑定；审计摘要为
> `docs/evidence/shared-policy-release-parity-drift-20260927-r15.json`（32 个阻断）。这次
> 只修复了历史软件证据漂移，未把 replay accuracy、现场硬件链路、人工 QGC、HIL/REAL 或
> prerelease 渠道升级为通过。

当前索引可用以下只读命令复核：`python scripts/verify_current_progress_index.py --root .`。
它会重新计算 release parity、release-gate audit、shared-policy 3×3 canonical report 和当前
field-link preflight/interface diagnostic 的 bytes/hash，并确认 release 仍 fail-closed；不会修改
证据或发布状态。

### 当前 parity、现场链路与 replay 评估索引（2026-09-27 00:00 UTC）

- 已将 Gazebo wire-v3 BGR24 sensor manifest 接入 geolocation replay/accuracy 证据链：每个选中的
  replay event 必须命中唯一 hash-verified sensor frame，报告同时绑定 manifest 文件 SHA、canonical
  SHA、wire version、pixel format、frame IDs 和 truth-free provenance。定向 geolocation/replay 回归
  `55 passed`，不把 headless sensor transport 升级为 QGC 视觉、HIL/REAL、室外 GNSS 或飞行验收。
- 因 campaign Mission evidence 新增 sensor transport provenance/time-window 绑定，当前
  `configs/release-parity.json` 已重算为 12,101 bytes，文件 SHA-256=
  `acfc438d4812568102659d7a91aac46c4fb10d024bd852318dea1f92431c1f55`，
  `core_algorithm_hash=93a63722f20e216690328a6c1aeaf211c5f58210c8d3e32bec75bee5af2378c3`，
  `px4_mission_path_hash=d19599d18b591e5e76d192c461947019ba91af378c59e531b9eba058b7607a55`。
- observed session 已重新执行为 `artifacts/evaluation/shared-policy-observed-session-20260927-r4.json`
  （4,392 bytes，SHA-256=`f71682573833b3155c3bec7a472226ec64174b9ccf7a1dab6567c38e294f2d03`）；
  parent/candidate receipt 已重新绑定到 `artifacts/evaluation/shared-policy-campaign-receipts-20260927-r9/`，
  release-gate audit 已刷新为 `docs/evidence/shared-policy-release-parity-drift-20260927-r6.json`。
  审计摘要同时保留 release-gate 报告内的历史 field-link 绑定，并新增当前只读
  `artifacts/evaluation/field-link-live-20260927-r1/preflight-bound.json`（4,641 bytes，
  SHA-256=`f04230469f72b01b23306d241f1a809cb7bc66380a58c076998a1ef98aca592f`）及其接口诊断
  （4,380 bytes，SHA-256=`e405f2744e3e24249ad18da915a5b14600ec75392d4fb1f40edcb239b5278f1d`）。
  这样当前现场状态不再由 9 月 25 日旧预检冒充；Jetson/GR01/Camera 仍不可达，硬件链路仍为
  `hardware_link_ready=false`。
  新 receipt 仍是旧外部 campaign 的 legacy sensor-disabled receipt；sensor-enabled receipt 现在要求
  sealed manifest、唯一任务→帧映射及 Mission 同一 clock window，旧 r3 预采集帧不会被冒充为同一架次。
  `verify_current_progress_index.py` 已通过，最终发布仍保持 fail-closed。
- 最新全仓回归已通过：`5051 passed, 7 skipped, 0 failed`；JUnit 证据为
  `artifacts/evaluation/full-regression-20260927-sensor-audit-reopen/pytest.xml`（737,842 bytes，
  SHA-256=`e566d65d753b7b019f690196caaddeb25a33ec0089f5b165733cf87bb9e7369e`）。这只是当前源码的
  软件回归，不等价于 HIL、REAL、室外 GNSS、实飞或最终发布门通过。
- 新做了一次当前工作站只读 field-link preflight：
  `artifacts/evaluation/field-link-live-20260927-r1/preflight.json`（3,887 bytes，
  SHA-256=`c11c241c535fda2f5adacb65087084c9956130b0c86a8380ee4e872b1b175a68`）。Jetson、GR01、
  Camera TCP 均不可达，系统选出的源地址仍为 `192.168.1.13`，不在期望的
  `192.168.144.0/24`；`hardware_link_ready=false`、`route_checks_passed=false`。全程只读，
  未修改网络、未发送飞控指令，因此 HIL/REAL/室外 GNSS 门继续保持 `not_claimed`。

- 本轮新增的 positive replay fixture 已正式绑定到当前 release-gate 诊断；当前报告时间为
  `2026-09-26T08:49:00Z`，但仍明确是 prerelease、fail-closed，未被伪装成新的硬件或发布验收。

- `src/multidetect/autonomy/task_session.py` 新增 SharedTaskPolicy 的 airframe-profile fail-closed
  校验：运行时 profile 与 policy artifact 不一致时直接拒绝 observed session；对应回归测试已通过，
  `configs/release-parity.json` 已重算为 `core_algorithm_hash=c4ddf12a...9befee`，并将
  `scripts/vtol_selected_task_campaign.py` 与 `scripts/vtol_sitl_mission_acceptance.py` 纳入
  `px4_mission_path` parity；文件为 12,101 bytes，SHA-256=
  `fc244c16b96673f18977febcccdef2fea4e0c18915514369eca256128b3a0154`，PX4 Mission path hash=
  `06864ceabc4e9e578671fead857ab902d38a91425a8374d749aeb9abad0c877f`。
- 重新生成了同一共享策略 observed-session 与 parent/candidate campaign receipt，使它们重新绑定
  当前 parity。新的 receipt 目录为
  `artifacts/evaluation/shared-policy-campaign-receipts-20260927-r8/`；这只是证据身份刷新，
  没有改变 candidate 未晋级、HIL/REAL/室外 GNSS 和最终发布均未通过的结论。
- 独立 observed-session evidence 已重新生成并加强校验：每一步都重新调用共享策略、重新编译
  planner 结果并与保存的 Mission plan 做精确比较；当前 `planner_revalidated=true`，证据文件为
  `artifacts/evaluation/shared-policy-observed-session-20260927-r3.json`（4,392 bytes，SHA-256=
  `eacb229f3d9f0075ebe850c1614931c5a344d39cce6ea6e132defe2db48dc333`）。
- 已对 2026-09-20 的 parent/candidate 外部 campaign 生成统一 receipt：每个 receipt 都绑定两个
  Mission report、两个 transaction journal、两个 `.policy.json` 的文件 SHA-256、Mission hash、
  策略 decision hash、planner 重验标记、SIM runtime descriptor/airframe/scenario identity，
  以及外层 runtime evidence 的 exact container cleanup 和 post-remove absence。当前 receipt 位于
  `artifacts/evaluation/shared-policy-campaign-receipts-20260926-r5/`；它仍不证明 HIL/REAL、
  室外 GNSS 或最终发布。
- receipt 现已显式绑定 `configs/release-parity.json` 的五个身份：core algorithm、PX4 Mission
  path、safety policy、task contract 和 UI contract；当前 parent/candidate receipt 位于
  `artifacts/evaluation/shared-policy-campaign-receipts-20260926-r5/`，索引验证器会拒绝 parity
  文件、bytes、SHA-256 或任一 contract hash 漂移。
- 新增 `scripts/verify_shared_policy_runtime_handoff.py`：读取 source SIM campaign receipt、目标
  `live-runtime-observation-mission-binding`、实际 policy artifact 和 release parity 文件，交叉绑定
  policy/ranker、稳定 airframe、Mission identity、五项 parity 与输出锁；目标模式只能是 HIL/REAL，
  身份不一致时 gate 为 false。对应正向与篡改回归在 `tests/test_shared_policy_runtime_handoff.py`，
  这只是 SIM→HIL/REAL 的 provenance/handoff 校验，不宣称 HIL、REAL、实飞或室外 GNSS 已通过。
- `release_gate.py` 与其 schema 已接入 `shared_policy_runtime_handoff`：prerelease 可以明确记录 handoff
  缺失并保持 fail-closed，最终 release channel 则强制要求 hash-bound SIM→HIL/REAL handoff；当前报告
  为 `artifacts/evaluation/release-gate-positive-replay-20260926-r1/`，45 个阻断，且旧模式/外部证据的
  `px4_mission_path_hash` 漂移被显式记录为 `current_release_parity_bound=false`。
- `create_release_gate_manifest.py` 与 `run_current_release_gate_pre8.ps1` 均提供
  `--shared-policy-runtime-handoff`/`-SharedPolicyRuntimeHandoff` 入口，生成路径不再需要手工编辑 JSON。
- 修复了 `scripts/verify_shared_policy_runtime_matrix.py` 直接从 PowerShell/终端运行时缺少 `src/`
  导致的 `ModuleNotFoundError`：CLI 现在自举仓库路径，不依赖 editable install 或外部
  `PYTHONPATH`。清洁环境子进程回归已通过，并生成四模式矩阵证据
  `artifacts/evaluation/shared-policy-runtime-matrix-20260926.json`（3,026 bytes，SHA-256=
  `a307adfacee2a642b7b7b5d9af4890bc1431fdd855bf61a0f09f918534909fcf`）。该矩阵只证明
  REPLAY/SIM/HIL/REAL 共用 artifact 与稳定 airframe identity，backend/MAVLink/actuator 均未启动，
  不升级 HIL/REAL、硬件、室外 GNSS 或最终发布门。
- 当前只读现场预检：`artifacts/evaluation/field-link-refresh-20260925-r1/preflight.json`，15,759
  bytes，SHA-256=`2c1c1cb29cc022201e3aa33b7a64b2208e85ae8653d82c427fa6b2d3669e5f67`；接口诊断为
  `artifacts/evaluation/field-link-refresh-20260925-r1/interface-diagnostic.json`，4,383 bytes，
  SHA-256=`da0c6c8ccb9072259bbe2fe20e3d6908305b454a8809d730d600cb256fd71f41`。
- 本次 gate 重新评估已将该现场预检判为 stale；Ethernet 仍为 `192.168.1.13/24`，目标
  `192.168.144.0/24` 无活动接口/路由，Jetson、GR01、Camera 连续 3 次超时；因此
  `hardware_link_ready=false`、`route_checks_passed=false`，全程只读且未发送飞控指令。
- 本轮实际执行了一个独立 Docker/PX4/Gazebo disposable external SIM：
  `artifacts/evaluation/simulator-docker-acceptance-20260926-r2-execute.json`，25,015 bytes，
  SHA-256=`98ead8e3269b651d308df74229753458ec8cfd6be780735d70578cf36836ed32`。固定镜像
  `px4io/px4-sitl-gazebo@sha256:bcf8278e...dc8ef8` 启动成功，真实 `HEARTBEAT` 与
  `GPS_RAW_INT`（fix 3、10 satellites）通过，容器 stop/rm 清理成功；只发送维持 MAVLink
  流的 GCS heartbeat，未发送 Mission/ARM/actuator。该证据仍是 headless external SIM，
  不代表 QGC 视觉、HIL、REAL、室外硬件 GNSS 或最终 release。
- 之前的独立 Docker/PX4/Gazebo disposable external SIM 证据仍保留为：
  `artifacts/evaluation/docker-external-refresh-20260926-r1.json`，46,919 bytes，
  SHA-256=`a643221d765dbad8c0c4e4db63d758add1e796a667fdf79eecbadf04b3dc47dd`。固定镜像
  `px4io/px4-sitl-gazebo@sha256:bcf8278e...dc8ef8` 启动成功，真实 `HEARTBEAT` 与
  `GPS_RAW_INT`（fix 3、10 satellites）通过，容器 stop/rm 清理成功；只发送维持 MAVLink
  流的 GCS heartbeat，未发送 Mission/ARM/actuator。该证据仍是 headless external SIM，
  不代表 QGC 视觉、HIL、REAL、室外硬件 GNSS 或最终 release。
- 随后用同一 `vtol-ranked-routes.synthetic-campaign.json`、同一 runtime profile、同一
  policy artifact 和同一 PX4 Mission/router 路径完成了 parent/candidate 外部任务闭环重跑：
  parent evidence 为 `artifacts/evaluation/shared-policy-external-parent-20260926-r1.json`
  （30,198 bytes，SHA-256=`8609e7acf61146b591d43fbb6131ac553fc6784db8d3b84876c8ef228315356b`），
  candidate retry evidence 为 `artifacts/evaluation/shared-policy-external-candidate-20260926-r2.json`
  （30,233 bytes，SHA-256=`b58753aa42bafab3d20bc92ef7f10c664fa1513084f59a8d35386d8de2897799`）。
  两边都完成 `synthetic-west`/`synthetic-east` 两架次，Mission report、transaction journal、
  policy decision、planner revalidation 和 exact-ID container cleanup 均通过；只读 receipt 位于
  对应 `.receipt.json` 文件，并已绑定当前 release parity。candidate 的第一次尝试因外部
  PX4/Gazebo 时序出现 `mission_item_reached_ahead_of_current_sequence` 而 fail-closed，证据保留为
  `artifacts/evaluation/shared-policy-external-candidate-20260926-r1.json`，未被当作通过。
  parent/candidate artifact SHA 相同且没有独立性能指标收益，因此 candidate 不晋升；这仍是
  headless SIM，不代表 QGC 视觉、HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布。
- 当前 wrapper 报告：
  `artifacts/evaluation/release-gate-positive-replay-20260926-r1/release-gate-pre.8-current-20260926T084900Z-v6-bound-report.json`，
  74,454 bytes，SHA-256=`98231ec1a7a04e49b200aeb4a9c6d7c554e1f952fce7c7de77e8ecd55cc7fe34`；manifest
  SHA-256=`847de603b59ed3ecc1f3c9ea59e48e0fa138d6110e67e9c19102245b86d59f66`。报告生成成功但
  `gate_passed=false`、`release_allowed=false`，45 项阻断原因仍保持 fail-closed；旧模式/外部证据的
  `px4_mission_path_hash` 漂移已明确记录。
- parity 绑定、geolocation survey coverage contract 和 field evidence coverage policy 更新后的全仓
  回归已用退出码 `0` 完成：`5043 passed, 7 skipped, 0 failed`，命令为
  `python -m pytest -o addopts= --disable-warnings -q`，日志为
  `tmp-full-post-field-coverage-policy.log`。这只是
  软件回归，不升级 HIL/REAL、真实硬件、室外 GNSS 或最终 `0.3.0` 发布门。
- `verify_current_progress_index.py` 现在对当前 report/manifest 使用强制存在性校验，并交叉验证
  report 内容、audit blocker 列表、四模式 parity、progress bytes/SHA；同时要求 progress、release-gate
  原始报告、release-gate replay manifest/accuracy、field-link preflight、interface diagnostic 四方的
  resolved path/bytes/SHA 一致，并检查 calibration verification 的 manifest binding。基础
  文件校验先执行，便于区分文件自身漂移和跨证据绑定漂移；缺失 artifact 或路径错绑都会直接失败，
  不再出现“文件不存在但索引返回 passed”的 fail-open 路径。旧的 2026-09-08 strict 文件已在
  audit 中标记为 `historical_strict_report_status=stale_provenance_not_current_parity`。
- 新增并绑定正例 replay evaluator fixture：
  `artifacts/evaluation/replay-set-positive-fixture-20260924/replay-set.manifest.json`，4,981 bytes，
  SHA-256=`6bee2b239aff59bb284c35bbe73f1ef66bda2ae3d95091268bdf0604938b2e9e`；对应 accuracy report
  12,208 bytes、SHA-256=`9c438775b0c87ae69591383cebed902cd9857614de6ee158796369cb5b46f613`，readiness
  report 11,145 bytes、SHA-256=`a86b99ab47bc0c127e5c7c43de57860e7935ccd9acc0460e8598b70cf9122e7f`。
  五个正例分层 (`day/night/haze/reflection/smoke_only`) 的 truth/prediction 覆盖均通过，报告可
  重算且 manifest/canonical hash 一致；另有 report-only calibration 与独立 verification，故
  可复核阈值计算绑定，但来源仍是 `synthetic_demo`、未独立标注复核，故
  `manifest_gate_passed=false`、`accuracy_gate_passed=false`、`gate_passed=false`。这只推进评估器
  和数据契约，不替代真实部署记录。

- 现场 replay→truth 的 WGS-84 报告新增可选 survey coverage contract：记录 replay range 与独立 truth
  中填写的 `survey_range_m`/`survey_slope_deg` 的距离/坡度 regime、分离阈值和样本数；通过
  `--minimum-distance-regimes` /
  `--minimum-slope-regimes` 显式启用覆盖门，缺少多距离或多坡度证据时 fail-closed。默认值为 0，
  因而兼容历史报告；survey template 现在会生成这两列供现场填写，coverage 经过 schema 和 verifier
  的形状、排序、分离度及门状态重验；accuracy report 与 survey template 的 Draft 2020-12 schema
  实例验证均为 0 个错误。
- field-evidence bundle 现在也绑定同一 coverage policy：当最终现场调用显式要求距离/坡度 regime 时，
  accuracy stratum 必须携带相同的最小 regime 数与分离阈值，并且实际 coverage 达标；策略值同时写入
  `policy`、`bindings` 和 bundle identity。默认仍为 0，旧的两字段 policy 报告保持可读。这是证据
  完整性门，不是室外 GNSS、HIL、REAL 或实飞通过。
  这只是现场证据完整性工具，不代表已经获得真实 GNSS、最终安装外参、HIL/REAL 或实飞通过。

### 现场链路只读刷新（2026-09-20 00:46 UTC）

- 新 preflight：`artifacts/evaluation/field-link-refresh-20260920/preflight.json`，SHA-256=
  `bace73d1444e881c85e42018ae1477315121f0392bb491f744a2394bfe2ed7d8`。
- 新接口诊断：`artifacts/evaluation/field-link-refresh-20260920/interface-diagnostic.json`，
  SHA-256=`5cdae1d350692b592ed5cc78bd60cff23f3a5df1f34fa80d85604b312f673d04`。
- 结果仍为 `hardware_link_ready=false`、`route_checks_passed=false`、`tcp_reachable=false`；
  当前活动 Ethernet 为 `192.168.1.13/24`，目标 `192.168.144.0/24` 无接口/路由，Jetson、
  GR01、Camera 均超时。此次操作完全只读，没有修改地址、路由或设备状态。

### 现场链路只读刷新（2026-09-24 05:43 UTC）

- 已重新执行 `run_field_link_preflight.ps1` 的 VerifyOnly/只读路径，并生成当前 evidence：
  `artifacts/evaluation/field-link-refresh-20260924/preflight.json`，15,751 bytes，SHA-256=
  `8c0bb53d7ab1c3360c569c3138d589a0e77632a210f1537ae087fc191fea6e99`；接口诊断为
  `artifacts/evaluation/field-link-refresh-20260924/interface-diagnostic.json`，4,380 bytes，
  SHA-256=`9e8ef850d0952e807b09c3f7b2217d40ac21c0834890441d487c118a830a09a1`。
- 当前 `Ethernet` 仍是 `192.168.1.13/24`，目标 `192.168.144.0/24` 没有活动接口或路由；Jetson、
  GR01、Camera 的 3 个 TCP 端点连续 3 次超时，路由选择也全部落到 `192.168.1.13`。因此
  `hardware_link_ready=false`、`route_checks_passed=false`，但报告本身通过只读结构校验。
- wrapper 默认 field-link 输入已切到这份 2026-09-24 evidence，release gate 不再依赖 9 月 20 日的
  过期 preflight；硬件未接入这一真实阻断仍保持 fail-closed。此次没有修改地址、路由、DNS 或设备状态。
- 用当前默认输入重新执行 release-gate wrapper（只指定输出目录与评估时间），报告为
  `artifacts/evaluation/release-gate-field-refresh-20260924/release-gate-pre.8-current-20260924T055046Z-v6-bound-report.json`，
  73,483 bytes，SHA-256=`c963a4d72e516cc11c4892a2d1fddb24eaeb8e33f573bebd3152018fc871ec35`。
  wrapper 退出码 `0` 是报告生成成功，不是发布通过；报告的 field-link freshness 在该评估时刻通过，
  但 `hardware_link_ready=false`，整体 `gate_passed=false`、`release_allowed=false`，31 项阻断原因仍在。

### 外部 PX4/Gazebo runtime refresh（2026-09-20 01:05 UTC）

- 使用本地固定 digest 镜像 `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`，
  对 `river_rescue_vtol_001` 执行了一次真实 disposable Docker SIM；不是 dry-run。
- evidence：`artifacts/evaluation/docker-external-refresh-20260920.json`，25,023 bytes，
  SHA-256=`1869b6af2033079ed59727dafa904aad6f31f9b8f3c17808d81fa76200f3516e`。
- 实际观察到 PX4 heartbeat 与 `GPS_RAW_INT`：fix type=`3`、satellites=`10`；telemetry/runtime
  gate 均通过，truth-free=true。容器停止/删除均成功，flight-control commands sent=`0`。
- 这轮只证明 headless 外部 SIM runtime/telemetry；hardware、QGC 人工视觉、HIL、REAL 和室外
  GNSS 仍是 `not_claimed`，不改变最终 `0.3.0` 发布门。

### SharedTaskPolicy parent/candidate 外部成对 campaign（2026-09-20）

- 使用同一 `vtol-ranked-routes.synthetic-campaign.json`、同一稳定 VTOL airframe identity、同一
  PX4 Mission path 和同一 router 安全规则，分别执行 parent 与 candidate 的真实 disposable
  PX4/Gazebo campaign。
- parent evidence：`artifacts/evaluation/shared-policy-refresh-parent-20260920.json`，
  SHA-256=`f6232d5927eff2d3acb795c4266dbbba85a0317b643872ef41dc4d973268fa72`；candidate evidence：
  `artifacts/evaluation/shared-policy-refresh-candidate-20260920.json`，SHA-256=
  `1c04a1b3fc5c612d0907eb4767426c663d274d90efdc44e3b4f92a0f982625bd`。
- 两轮均 `gate_passed=true`，各完成 `synthetic-west` 与 `synthetic-east` 两架次；Mission
  upload/readback/commit/permit、policy evidence、planner revalidation、router 和 exact-ID
  cleanup 全部通过，cleanup failures 均为 `0`。
- 两个 artifact 的 ranker/canonical identity 相同，说明当前训练搜索仍未产生新权重收益；这轮
  证明 shared-policy 外部执行可重复，不证明 candidate 性能收益，也不晋升 candidate。
- 该 campaign 仍是 headless SIM：QGC 人工视觉、硬件、HIL、REAL、室外 GNSS 和最终 `0.3.0`
  发布门继续 `not_claimed`/blocked。

### 训练 artifact runtime profile 绑定 CLI（2026-09-20）

- `scripts/train_task_ranker.py` 已增加显式 `--airframe-profile-sha256`，真实生成报告：
  `artifacts/evaluation/policy-artifacts-cli-runtime-bind-20260920/report.json`，SHA-256=
  `12690313a7a0b8b2ddbd976aa0fe2288ac94ce86b19f684cad9ee84e3dd9a1f3`。
- 本次输出记录 `airframe_profile_hash_source=explicit_cli`，绑定当前稳定 VTOL identity；训练
  和 holdout 评估仍使用数据集 identity，避免把 runtime profile 覆盖误当训练数据身份。
- 24 项 CLI/shared-policy/index/docs 回归通过；candidate 仍未晋升，版本继续 `0.3.0-pre.8`。

### 当前 checkout 完整回归（2026-09-20）

- 当前工作区完整 pytest 已以退出码 `0` 完成：`5016 passed, 7 skipped, 0 failed, 0 errors`，共 `5023`
  项，耗时 `194.41s`。
- JUnit 证据：`artifacts/evaluation/full-regression-20260920-runtime-matrix/pytest.xml`，大小
  `732,877` bytes，SHA-256=`50138dbdf63bc958d9ac2441a05abf9c44afe75e05eb6e93cd98f8e32965ae85`。
- 这是当前 checkout 的软件回归证据；7 个 skip 仍按测试自身的环境条件保留。它不等价于 QGC 人工视觉、
  HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布验收，当前 release gate 继续为 `false`。

### PolicyArtifact 独立 loader 校验（2026-09-20）

- 对 runtime 绑定训练输出的 parent/candidate artifact 做了独立严格加载、文件 SHA-256、canonical SHA-256
  和稳定 VTOL airframe identity 复核；证据为
  `artifacts/evaluation/policy-artifact-independent-loader-20260920.json`，大小 `1,331` bytes，SHA-256=
  `ba0fa339042351dc0eedc096be52bd18c2212be1344c656ac0ac34cbad666814`。
- 该 evidence 同时绑定源训练报告 `report.json` 的 SHA-256=
  `12690313a7a0b8b2ddbd976aa0fe2288ac94ce86b19f684cad9ee84e3dd9a1f3`，避免独立 loader 记录与训练
  输出脱节。
- 两个 artifact 的 canonical SHA-256 均为
  `de39b730fd2acf3dc6f2fc75cfe201b576e7dd74d1e13834a9bcbc56aeea34d7`，文件 SHA-256 均为
  `bece2bde9f781ded4269bb5b546fd48c9886718f47d4f9f4df25540e80cf6e3b`；runtime identity 和 loader
  contract 均通过。由于 canonical identity 相同，candidate 仍未晋升。

### SharedTaskPolicy RuntimeMode 矩阵校验（2026-09-20）

- 使用真实 runtime-bound candidate artifact 运行
  `scripts/verify_shared_policy_runtime_matrix.py`，覆盖 `replay/sim/hil/real` 四种模式；证据为
  `artifacts/evaluation/shared-policy-runtime-mode-matrix-20260920.json`，大小 `3,026` bytes，
  SHA-256=`bd1f653aa458f7c6ae5f6aebaffab69b34d2ebf58afd18d3dec826c947232574`。
- 四种模式共享同一个 policy canonical SHA-256 和稳定 airframe profile hash；REPLAY/SIM/HIL/REAL
  profile 均没有 `FLIGHT_CONTROL_OUTPUT`，本次没有启动 backend，也没有发送 MAVLink。
- 这是 profile/contract 软件验证，不是 HIL、REAL、室外 GNSS 或实飞证据；最终发布门继续关闭。

### 确定性软件 SIM 场景矩阵（2026-09-20）

- 使用现有 `SimulatorScenarioRunner` 对三份仓库场景 manifest 执行真实软件闭环：
  `coastal_patrol_fixed_wing_001`、`river_rescue_vtol_001`、`urban_inspection_multirotor_001`，覆盖
  fixed-wing、VTOL、multirotor 三种模式。每个场景使用固定时钟和同一 AI/authority 路径重复运行。
- 证据：`artifacts/evaluation/deterministic-scenario-matrix-20260920.json`，13,300 bytes，
  文件 SHA-256=`80f2be5647812c65a7ea7d3a40fbfe6adc4c0e85684d6ea71c4581761c56666b`；文档内部
  canonical SHA-256=`434d67a485c26091b27be5e0295554053ba7d7df2c11e54cedf6eff26394c655`。
- `deterministic_gate_passed=true`、`repeatability_checked=true`、`truth_free=true`；故障覆盖要求开启，
  三个场景均自动延长到 `2611` 帧，所有声明故障的 coverage ratio 均为 `1.0`，重复运行哈希完全一致。
- 这是确定性 Python 软件 SIM 证据，证明当前场景 runner 的重复性与故障注入覆盖；外部三维运行、QGC
  人工视觉、硬件、HIL、REAL、室外 GNSS 和最终 `0.3.0` 发布门仍为 `not_claimed`/blocked。

### SharedTaskPolicy 连续观察任务会话（2026-09-24）

- 新增 [`run_shared_policy_observed_session.py`](../scripts/run_shared_policy_observed_session.py)，直接调用
  现有 `run_observed_task_session`、`SharedTaskPolicy`、Mission 编译器和 `MissionOutcomeMonitor`。
  固定时钟下连续选择并完成 `synthetic-west`、`synthetic-east` 两个任务；第二轮计划使用第一轮
  完成后的新上下文哈希，最终 pending 集合为空。
- 证据：`artifacts/evaluation/shared-policy-observed-session-20260924.json`，4,282 bytes，文件
  SHA-256=`b108fd484f425cd563c7f27de321e3497dee8e8857bb6d0acafabecf3418adf4`。两次完整会话的
  canonical session hash 相同，`all_tasks_completed=true`、`deterministic_repeat_passed=true`；证据还
  绑定了当前 `configs/release-parity.json` 的 bytes、文件 SHA-256 和五组 parity hash。
- 每个步骤都绑定同一个 policy artifact canonical/file SHA、ranker SHA、plan SHA、executable Mission
  wire hash 和 completion outcome；`truth_free=true`、`backend_started=false`、`mavlink_sent=false`、
  `actuator_capability_granted=false`。
- 这是观察会话的软件闭环证据，不是 HIL、REAL、室外 GNSS、人工 QGC 或真实飞行验收；最终发布门继续关闭。

### Replay-set readiness 独立重算（2026-09-20）

- 使用当前绑定的 SIM/Gazebo replay manifest 和 accuracy report 独立执行
  `scripts/check_replay_set_readiness.py --report-only`；新报告为
  `artifacts/evaluation/replay-readiness-refresh-20260920.json`，大小 `13,309` bytes，
  SHA-256=`b6f308109e102dc23846b6772895a61bc90363e08b4b042ebf84c6c88f8a44d1`。
- readiness 仍为 `gate_passed=false`：6/6 strata 是 `synthetic_demo`，reviewed entries=0，
  deployment source=false，positive truth/prediction coverage=false，accuracy review 未确认。
- 该结果把真实缺口重新绑定到当前证据，不把历史 synthetic replay 当作部署域精度或最终发布依据。

### 版本与发布门复核（2026-09-20）

- 直接读取 product-version：release_label=0.3.0-pre.8、python=0.3.0rc8、channel=prerelease。
  numeric=0.3.0 是数值版本字段，不代表最终版本已发布。
- product version、release gate、manifest、progress docs 定向回归：105 passed，5.46 秒。
- 本轮全量 pytest stdout 到达 100%，stderr 为空；日志保存在
  `artifacts/evaluation/terminal-continuation-v181/pytest-full.stdout.log` 与同目录 stderr 日志。
  该后台运行没有持久化退出码，因此不把它记录为 exit 0 的全量验收。
  pytest lastfailed 缓存含旧条目，不能据此判断本次失败或通过；后续完整验收须保存退出码与 JUnit。
- 未修改版本、未晋升候选、未发布最终 0.3.0；HIL/REAL、人工视觉及现场硬件门未关闭。

### 外部评估身份与数值边界复核（2026-09-20）

- evaluator 现在要求 scenario manifest、stable airframe 和 parent/candidate campaign identity
  都是非空、格式严格的 64 位 hex SHA-256；缺失、非法或全空身份不再被算作“一致”。
- verified task list 现在要求非空、字符串非空、task id 不重复；空列表、残缺项和重复 task
  不再成为有效完成任务数。能耗窗口继续拒绝空列表、非有限值、负值和总和溢出。
- 完整仓库回归实际退出码为 `0`：`5006 passed, 7 skipped, 0 failed`，耗时 `168.94s`。
  JUnit：`artifacts/evaluation/repeat-identity-full-regression-20260920.xml`，SHA-256=
  `0e087d5b5eedea50518f05a6e0316a4694740493accccf05f8c4ea03f3a3f28a`。
- 评估器/发布门/进度文档定向回归：`135 passed`；Ruff 与 `git diff --check` 通过。

### 外部重复评估报告去重（2026-09-20）

- 修复重复传入同一报告或换名副本可能凑满 3×3 次数的问题。评估器现在强制检查全部输入
  文件 SHA-256 唯一性，不受可配置 invariant 列表影响；跨 parent/candidate 的同内容也拒绝。
- 新增四个 fixture 测试：独立内容正例、重复路径、换名副本、跨组重复；8 项评估器测试通过。
- 六份既有原始报告重新计算通过，唯一内容计数 parent=3、candidate=3、combined=6。
  新报告 `artifacts/evaluation/shared-policy-external-repeat-distinct-reports-20260920.json`，
  SHA-256=`894411e40f24ccc6a65ea63372bf49e81f58ee6209740dc0d76523f3aa18aa84`。
  模型均值仍为 parent 4.8322001333 Wh、candidate 4.6188801333 Wh；candidate_promoted=false。
- 内容去重不是统计独立性或来源真实性证明；本次没有新增外部飞行，没有物理/HIL/REAL 验收。

### portable UI smoke fresh record（2026-09-20）

- 重新执行当前 portable `MultiDetectGCS.exe --simple-boot-test`，退出码为 `0`，stdout/stderr
  均为 `0` bytes，QML 初始化错误数为 `0`。
- 本轮证据记录：
  `artifacts/evaluation/terminal-continuation-v180/portable-ui-boot-smoke.json`，记录 SHA-256=
  `82d1c966dcfd6e11a991f1500410b3d9a82b0fda51c58d16618f8dfa9b5875f3`；EXE SHA-256 仍为
  `41ea7aaf830b406aadbcbb17bd7e5e1e22c1a08f20dcba002885e0153cf6765b`。
- 该记录只证明软件启动与 artifact 完整性；`operator_visual_acceptance`、硬件、HIL、REAL、
  室外 GNSS 仍明确为 `not_claimed`。

### 当前 portable UI 软件 smoke（2026-09-19）

- 当前 portable UI artifact 为
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，大小
  `46,875,136` bytes，SHA-256=`41ea7aaf830b406aadbcbb17bd7e5e1e22c1a08f20dcba002885e0153cf6765b`。
- 使用同一 artifact 执行 `MultiDetectGCS.exe --simple-boot-test`，进程退出码为 `0`。这是软件
  启动/完整性证据，不是人工 Fly View、同一 UI 视觉验收，也不是 HIL/REAL 或实飞证据。

### 现场链路 preflight 刷新与 gate 重绑（2026-09-19 16:15 UTC）

- 重新执行只读接口诊断和 route/TCP preflight：当前仍只有 `192.168.1.13/24`，目标
  `192.168.144.0/24` 没有活动接口，Jetson/GR01/Camera 均超时；新 preflight
  `artifacts/evaluation/field-link-preflight-20260920-current.json` SHA-256=
  `548df1df0dc7123b4c13a8b5f03b4c92ecab7fc3c9c6319e523a2c2717a22b8e`，interface diagnostic
  SHA-256=`82b2695b9a02c8e25e13534055c416e2bf640717faf47f0f85afb06f27950363`。
- 用该 fresh evidence 重绑 gate：报告位于
  `artifacts/evaluation/release-gate-pre.8-current-20260919T161500Z-v6-bound/release-gate-pre.8-current-20260919T161500Z-v6-bound-report.json`，SHA-256=
  `d6a13a39d03c3b42190293e82affbbb1facb705c98fab77a1aaca1ae8509afe5`。freshness blocker 已清除，
  真实 `hardware_link_ready=false`、人工 UI、HIL/REAL 与最终发布 blocker 保留。

### Release-gate 无参数默认链 smoke（2026-09-20）

- 真实执行 `run_current_release_gate_pre8.ps1` 无参数证据链（仅指定新输出目录和时间戳）时，发现
  external runtime/AI join、revalidation 与 PX4 reported-position matrix/campaign 的默认值仍指向
  未带 `-rerun` 的旧文件；已统一切换到当前 parity-bound 的四份 `*-rerun.json`。
- 修复后无参数 smoke 成功生成 manifest/report，wrapper 退出码为 `0`；旧 parity drift 不再出现。
  当前 gate 仍为 `gate_passed=false` / `release_allowed=false`，因为现场 preflight freshness 超过
  900 秒且 `hardware_link_ready=false`，同时人工 UI、HIL/REAL 和最终 channel 门仍未通过。
- 随后将无参数默认 field-link 输入切换到
  `artifacts/evaluation/field-link-refresh-20260920/preflight.json`，并再次执行 smoke；本次输出
  `stale=false`、`hardware_link_ready=false`、wrapper 退出码 `0`。默认入口与当前 refresh preflight
  已一致，现场链路仍按真实硬件不可用状态 fail-closed。
- 该历史 release-gate 索引曾绑定这次无参数 smoke 报告；当前索引已更新为 2026-09-24 报告：
  `artifacts/evaluation/release-gate-default-smoke-20260920-r4/release-gate-pre.8-current-20260920T005000Z-v6-bound-report.json`，SHA-256=`2fae69150b66c7bbcd20fa4085bd9fc60baab15e99f27ffd4e4b6ed5797988df`。

### Release-gate 默认证据绑定修正（2026-09-19）

- `scripts/run_current_release_gate_pre8.ps1` 的无参数默认 field-link 输入已改为版本化的
  `artifacts/evaluation/field-link-preflight-20260919-current.json`；旧 v33 preflight 只保留为历史
  provenance，不会再被无参数入口静默选中。
- wrapper PowerShell 解析、release-gate、manifest、field-link 和文档一致性定向回归全部通过；
  当前 gate 仍按真实 `hardware_link_ready=false` fail-closed，版本继续 `0.3.0-pre.8`。

### 现场链路只读 preflight（2026-09-19）

- 当前工作站只读接口诊断已完成：`Ethernet` 为 `192.168.1.13/24`，目标现场网段为 `192.168.144.0/24`；`Ethernet 2` 与 Wi-Fi 均断开，匹配接口数为 `0`。
- 新鲜 preflight [`field-link-preflight-20260919-current.json`](../artifacts/evaluation/field-link-preflight-20260919-current.json) 绑定了接口诊断 SHA-256=`639086e712c2a6278fee4d24ad73a252ef019f0cf17ac97a34cdb557f3fc99bc`；Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 均不可达，路由源均落在 `192.168.1.13`，因此 `hardware_link_ready=false`。
- 该结果是当前机器的真实只读现场状态，不是 HIL/REAL 证据，也没有修改网络配置。下一步现场动作是接入并选择 `192.168.144.0/24` 对应的物理接口，然后重新执行同一 preflight；在接口未接入前，release-gate 的 field-link 门继续 fail-closed。
- 对当前 `Ethernet` 运行了 `prepare_field_link.ps1 -VerifyOnly`：只看到 `192.168.1.13/24`，没有添加 `192.168.144.250/24`，`changed=false`。验证记录为 [`field-link-address-state-verify-20260919.json`](../artifacts/evaluation/field-link-address-state-verify-20260919.json)，SHA-256=`2e20bb587d469bb641269040fd44e1d6169086da72ead6dc48e3b6ec9be879c2`。

### 当前 parity 绑定的 release-gate 重算（2026-09-19）

- 完整 pytest 回归退出码为 `0`；当前 parity 变更后，默认 release-gate 先发现旧 simulator external join 与 PX4 reported-position matrix/campaign 的证据身份过期。
- 已按当前 `configs/release-parity.json` 重新生成并通过 verifier：external runtime/AI join、三机型 reported-position matrix、reported-position repeatability/fault campaign；随后 release-gate 使用这些新路径重算并退出 `0`。
- 当前 release-gate 仍为 `gate_passed=false`、`release_allowed=false`，但剩余 blocker 已收敛为 replay-set accuracy/deployment review、现场 field-link freshness/hardware、四模式人工视觉/Mission、HIL/REAL hardware/control、产品 channel 与 numeric final version；不再是上述软件证据 parity drift。
- 新 gate report：`artifacts/evaluation/release-gate-pre.8-current-20260919T001000Z-v6-bound-report.json`，SHA-256=`fe83ff8cfea11d8089bfbb262e50c04f40b08fce9d4fb6f63ddbca6f20db8d4a`，仍为 `0.3.0-pre.8`。
- 随后用新鲜 field preflight 重新绑定 gate：`artifacts/evaluation/release-gate-pre.8-current-20260919T002000Z-v6-bound-report.json`，SHA-256=`d85bd90c74be75205d7cc3d6c73fa7129249b6b4b9456cbbc565389e501c9527`。旧的 stale-preflight blocker 已清除，blocker 数从 `32` 收敛为 `31`；剩余现场 blocker 是真实的 `hardware_link_ready=false`。

### Post-mission 能耗窗口 3×3 外部 SIM（2026-09-19）

- parent b/c/d 与 candidate a/c/d 共 6 次有效 pinned PX4/Gazebo disposable SIM campaign 全部通过顶层 gate；每次包含两个 sortie，Mission transaction、policy artifact、planner revalidation 和 cleanup 均通过。candidate b 的一次 `selected mission completion not proven` 失败 evidence 保留在独立路径中，没有混入性能均值。
- 新 evaluator [`shared-policy-external-repeat-evaluation-3x3-sim-energy-20260920-current-v2.json`](../artifacts/evaluation/shared-policy-external-repeat-evaluation-3x3-sim-energy-20260920-current-v2.json) 已从当前 checkout 的六份 2026-09-19 原始 campaign evidence 重新生成，SHA-256=`894411e40f24ccc6a65ea63372bf49e81f58ee6209740dc0d76523f3aa18aa84`。post-mission receive-window 模型均值为 parent `4.8322001333 Wh`、candidate `4.6188801333 Wh`，candidate 优势 `0.2133200000 Wh`，超过预声明的 `0.1 Wh` margin；`metric_ready=true`、`minimum_run_counts_met=true`、`invariants_met=true`、`promotion_ready=true`，但 `candidate_promoted=false` 仍由系统强制保持。报告同时绑定 parent=3、candidate=3、combined=6 个互异内容 SHA-256。
- 该指标的有效性明确是 `complete_receive_window_model_only`，使用 `causal_envelope_midpoint_model_v1`；它不是物理校准能耗，不代表 HIL/REAL、室外 GNSS 或实飞收益。版本仍为 `0.3.0-pre.8`，最终 `0.3.0` 继续 blocked。

### 修复后 parent/candidate 外部 SIM 重复（2026-09-19）

- 在 aggregate router 时间预算修复后，parent 与 candidate 各完成一轮可比较的两任务 pinned PX4/Gazebo disposable SIM；两次顶层 `gate_passed=true`，router 配置上限均为 `1440s`，Mission frame/control 转发均为 `35/2`，cleanup failures 均为空。
- parent 仍选择 `synthetic-low → synthetic-high`，candidate 仍选择 `synthetic-high → synthetic-low`；这确认两个 policy artifact 进入了同一外部 Mission 路径并产生不同选择结果。证据见 [`shared-policy-parent-candidate-external-sim-repeat-20260919.json`](evidence/shared-policy-parent-candidate-external-sim-repeat-20260919.json)。
- 这建立了干净的 parent/candidate 执行重复性，但运行耗时受外部 SIM 时序影响，不能从这一对样本推出因果性能收益；`independent_candidate_gain_proven=false`、`candidate_promoted=false`。same UI、HIL/REAL、室外 GNSS 和最终 `0.3.0` 仍未通过。

### 外部 SIM 多轮晋升评估器（2026-09-19）

- 新增 [`shared-policy-external-repeat-v1.json`](../configs/evaluation/shared-policy-external-repeat-v1.json) 与 [`evaluate_shared_policy_external_repeats.py`](../scripts/evaluate_shared_policy_external_repeats.py)：要求至少 `3` 次 parent 与 `3` 次 candidate，固定场景/稳定机型/campaign 身份/PX4 Mission 路径，所有 gate 与 cleanup 通过，并且预声明性能指标在每次运行中都存在。
- 当前真实样本只有各 `1` 次；两边 `completed_task_count` 都为 `2`，评估器输出 `promotion_ready=false`、`candidate_promoted=false`。router wall-clock duration 不作为性能指标，避免把外部时序噪声误判成 AI 收益。证据见 [`shared-policy-external-repeat-evaluation-20260919.json`](evidence/shared-policy-external-repeat-evaluation-20260919.json)。
- 该配置随后用于完成 3×3 外部运行；在指标收益被证明前不晋升 candidate，不发布最终 `0.3.0`。

### 外部 SIM 3×3 评估完成（2026-09-19）

- 已完成 3 次 parent 与 3 次 candidate 的真实 pinned PX4/Gazebo disposable SIM。6/6 顶层 gate 通过、6/6 cleanup clean，场景 manifest、稳定 airframe、campaign identity 与 bounded PX4 Mission path 全部一致。
- 预声明的 `completed_task_count` 结果为 parent `[2, 2, 2]`、candidate `[2, 2, 2]`；candidate 没有达到配置要求的正向 margin `+1`，因此评估器保持 `promotion_ready=false`、`candidate_promoted=false`。证据见 [`shared-policy-external-repeat-evaluation-3x3-20260919.json`](evidence/shared-policy-external-repeat-evaluation-3x3-20260919.json)，SHA-256=`f1ee03be797b48f37c1de2e68087c45f63382c190723564d2a036aff3b484a26`；汇总内同时绑定了六份输入 evidence 的文件 SHA。
- 3×3 证明了该共享策略外部 Mission 生命周期的重复可执行性，但没有证明 candidate 的任务质量、能耗或时延收益；下一阶段必须补充真正有区分度且预先声明的任务质量/能耗指标，再考虑任何晋升。

### 外部 SIM 规划能耗指标诊断（2026-09-19）

- 评估器曾尝试沿着每架次 `mission_evidence → completed[].evidence_path → planning_energy.newly_consumed_wh` 重算能耗；源码审计确认该字段在 Mission transaction 创建前由 `energy_projection()` 生成，属于选择时的累计前缀，不是完整架次结束后的能耗。现已切换到 post-mission receive-window 模型值；旧 3×3 evidence 没有该字段，因此新评估仍保持 `metric_ready=false`、`promotion_ready=false`，不报告伪精度均值。重算证据见 [`shared-policy-external-repeat-evaluation-3x3-sim-energy-20260919-r2.json`](evidence/shared-policy-external-repeat-evaluation-3x3-sim-energy-20260919-r2.json)，SHA-256=`08f9eadc3784fa33b889e3beace6be678df2b6d7f666e33781b169054a3b483a`。
- 已生成交叉顺序 campaign：parent 强制 `high→low`，candidate 强制 `low→high`。parent 交叉运行和 candidate retry 均通过并 cleanup clean；candidate 第一次出现 `selected mission completion not proven`，失败 evidence 保留但不计入性能样本。下一步应先补完整 post-mission energy window，再做顺序平衡后的指标比较。
- 已补 post-mission energy window 审计链：成功 mission evidence 现在可绑定 collector 的 append-only telemetry，检查 receive 起止边界、PX4 boot 连续性和无丢样本；通过时输出 `causal_envelope_midpoint_model_v1` 的模型窗口值及 envelope。该值明确不是物理校准能耗，也不授权下一任务；新的 3×3 外部 SIM evaluator 已达到模型收益门，但 candidate 仍未 promotion。

### 多任务外部 router 时间预算修复（2026-09-19）

- 修复外层 capture 将多任务 aggregate router 生命周期无条件截断到 `600s` 的问题：单任务默认上限保持 `600s`，多任务由生命周期 owner 传入任务预算总和加清理余量，并受 `3600s` 有界配置上限约束。
- 新增 router/capture 回归验证 aggregate `duration_seconds` 与 `duration_limit_seconds` 的绑定；当前 parity 已按源码重新生成，文件 SHA-256=`d1ad01240cef9b9a839742154bde2c98b13f3523389cffa6e1669dcefaea41cc`。
- 这只是时间预算和生命周期正确性修复，不等于 candidate 性能收益、HIL/REAL 或室外 GNSS 验收；版本继续 `0.3.0-pre.8`。

### Parent 独立重复样本审计（2026-09-19）

- 为建立可比较的重复样本，追加了三次 parent 外部 SIM 尝试；两次暴露任务允许时长 600 s 与外层默认 30 s 的参数不匹配，第三次显式传入 600 s 后确实转发了完整 Mission/control frame，但外层 router 600 s 上限到期，未形成干净 `graceful_stop`。
- 三个容器均通过 exact-ID cleanup 并已移除；这些尝试不计入性能样本，也不改变既有 parent/candidate 各一轮的结论。证据见 [`shared-policy-parent-repeat-attempts-20260919.json`](evidence/shared-policy-parent-repeat-attempts-20260919.json)。
- 当前仍需在明确的多任务总时限和一致的外部启动条件下取得干净重复，才能讨论 candidate 收益；`candidate_promoted=false`，版本继续 `0.3.0-pre.8`。

### Parent/Candidate 外部 SIM 对照（2026-09-19）

- 基于同一两任务 campaign 生成了不同 ranker/artifact 的 parent 与 candidate；两次真实 pinned PX4/Gazebo disposable SIM 顶层 `gate_passed=true`，完整 Mission journal、policy evidence 和 cleanup 通过。
- parent 顺序为 `synthetic-low → synthetic-high`，candidate 顺序为 `synthetic-high → synthetic-low`；说明两个 artifact 确实进入了同一 Mission 路径并产生不同选择结果。
- 当前只有各一轮 parent/candidate，运行时间与能耗受外部 SIM 时序影响，尚不能证明独立 candidate 性能收益，也不能晋升 candidate。证据见 [`shared-policy-parent-candidate-external-sim-20260919.json`](evidence/shared-policy-parent-candidate-external-sim-20260919.json)。

### 共享策略成对外部 SIM（2026-09-19）

- 同一场景、同一 campaign、同一 policy artifact 进行了两次真实 pinned PX4/Gazebo disposable SIM；两次顶层 `gate_passed=true`，Mission journal、policy evidence 和 exact-ID cleanup 全部通过。
- 两次结果用于 repeatability，不是 parent/candidate 性能比较：没有加载 learned weights，candidate 没有独立收益证据，`candidate_promoted=false`。
- 该 capture 是 headless SIM，因此 `same_ui=false`；UI/HIL/REAL 既定门仍未通过。证据见 [`shared-policy-external-sim-pair-20260919.json`](evidence/shared-policy-external-sim-pair-20260919.json)。

### 共享策略单任务外部 SIM 验收（2026-09-19）

- 使用 pinned `px4io/px4-sitl-gazebo` 实际启动 disposable PX4/Gazebo 容器，加载同一 `SharedTaskPolicy` artifact，完成一架次 `synthetic-east`。
- 顶层 evidence `gate_passed=true`；Mission 已观察到 permit 后 arm、AUTO Mission、起飞、着陆和最终 disarm；Mission journal、policy evidence 和 exact-ID cleanup 均通过。
- 首次运行暴露既有 telemetry energy-window 缺口；将 PX4 SITL 状态流从 4 Hz 提升到 10 Hz，freshness threshold 不变，第二次完整外部 SIM gate 通过。
- 证据摘要：[`shared-policy-external-sim-20260919.json`](evidence/shared-policy-external-sim-20260919.json)。这不是 parent/candidate 性能比较，未加载 learned weights，未晋升 candidate。
- HIL/REAL、室外 GNSS、物理性能和最终 `0.3.0` 发布门仍未通过；版本继续 `0.3.0-pre.8`。

### 稳定 airframe identity 修复（2026-09-19）

- 发现并修复一个会阻断跨运行复用的身份问题：旧 `airframe_profile_sha256` 把含随机端口/运行 wiring 的 `stack_plan_sha256` 纳入 hash，导致相同 VTOL 机型每次启动都产生不同策略身份。
- 新增 `multidetect.simulator.stack.airframe_profile_hash()`：保留 stack hash 在运行 evidence 中，但从稳定 airframe identity hash 排除它。两个不同端口的 Docker stack plan 已实测得到相同 airframe hash。
- Docker acceptance、VTOL capture、Mission 回归和 Ruff 通过；当前 shared-policy 外部
  parent/candidate 闭环证据已绑定 Mission、journal、policy decision、planner revalidation
  和容器清理，但 candidate 没有可晋升的独立收益；因此仍不晋升、不宣称 HIL/REAL/室外 GNSS。
- 在同一固定 PX4/Gazebo 场景上进一步执行了真实 sensor transport：
  `artifacts/evaluation/simulator-docker-acceptance-20260926-r3-sensors.json`（75,447 bytes，
  SHA-256=`bae33494f09696cb433db3bf88793a81bceedb966c509190439e5332eaa6da9f`）。3 帧真实
  BGR24 已持久化，camera calibration、NavSat、odometry、covariance 和时间关联均通过，
  原始像素 SHA 与 wire payload 匹配；manifest 为
  `artifacts/evaluation/simulator-docker-acceptance-20260926-r3-sensor-frames/gazebo-bgr24-frame-set.manifest.json`。
  这仍是 truth-free headless SIM sensor evidence，不是 QGC 视觉、HIL、REAL 或室外硬件 GNSS 验收。


### 外层策略身份贯通（2026-09-16）

- 外层 capture CLI/API 已接受 `--policy-artifact` 与 `--policy-artifact-file-sha256`，启动前加载并校验 ranker/机型身份；同一文件及 hash 传给 campaign 子进程，父进程策略对象传入首次和清理后独立审计。
- 清理后复验同时检查 artifact 文件未变；缺失配对参数或没有 campaign 的策略输入在 Docker/运行目录操作前拒绝。
- 489 项既有 Mission/capture 回归通过，新增参数传递/拒绝测试通过。尚未执行携带 artifact 的完整外部 PX4/Gazebo campaign；下一步补完整生命周期故障注入并执行成对外部验证。
- 版本保持 `0.3.0-pre.8`，无候选晋升、HIL/REAL/室外 GNSS 或最终发布结论。下方 2026-09-12 条目是历史进度。

### Campaign CLI 与独立策略审计（2026-09-12）

- `scripts/vtol_selected_task_campaign.py` 新增 `--policy-artifact` / `--policy-artifact-file-sha256` 成对参数。文件 SHA-256 与 canonical SHA-256 分开记录；严格 loader 在打开 telemetry session 前检查文件、ranker 和 airframe 身份，错误不回退旧策略。
- `validate_campaign_result(..., shared_policy=...)` 接受调用方独立提供的预期策略，逐架次根据报告 context 重算 decision，核验 `.policy.json`、Mission 绑定及 planner 标记，并记录策略证据文件 SHA-256。缺少独立策略时拒绝带共享策略声明的报告。
- CLI 正向/错误路径 7 项通过；Mission/capture 回归 489 项通过；parity 与进度文档回归 34 项通过。均为自动化/fixture 验证，不是新的外部 SIM 飞行证据。
- 下一步：将外层 `run_vtol_mission_sensor_capture.py` 的完整 CLI/lifecycle 接入 artifact 输入并传给子进程和独立审计器，再执行成对外部 campaign。当前仅内层 campaign CLI 和审计 API 已就绪，未宣称外层一键闭环完成。
- 未训练新权重、未晋升候选、未发布最终版本；版本继续 `0.3.0-pre.8`，外部 SIM/HIL/REAL/室外 GNSS 验收仍待证据。

### 外部 campaign 共享策略接口（2026-09-12）

- `run_selected_task_campaign(..., shared_policy=...)` 现在复用 `SharedTaskPolicy.decide()`；旧 ranker-only API 保留，两参数同时传入会明确拒绝。
- 校验 artifact 的 airframe hash、decision identity、truth-free 与 planner revalidation；abstain 不进入 Mission 编译，编译后的 plan 必须与选择结果一致。
- 继续使用现有 telemetry、Mission transaction、completion 和 cleanup 路径。每架次新增 `.policy.json` 关联 decision、ranker 和 Mission hash，不改写原始 Mission 验收报告。
- campaign 回归 62 项、扩展定向回归 291 项通过。这是 Python 接口/fixture 验证，并非实际 PX4/Gazebo 运行；CLI artifact 加载与成对外部 campaign 尚待接入。
- 本轮未训练新权重、未晋升候选、未发布；HIL/REAL、室外 GNSS 和最终 0.3.0 门保持未通过。本机 PATH 未发现 Docker，不代表全部远程资源不可用。


## 当前状态（2026-09-12）

- 已完成：高层自主任务策略的 hash-bound 共享契约。`PolicyArtifact` 绑定 ranker、父模型、训练数据、
  airframe、Mission contract 和 safety policy；`SharedTaskPolicy` 在 SIM budget episode、observed
  HIL/REAL session 和训练评估中复用同一决策边界。输入明确 `truth_free`，输出只允许 feasible task 或
  `abstain`，选择后回到原 planner 做可行性重验，不持有 actuator、arm/disarm 或飞控授权。
- 已通过：自主规划、预算 episode、observed session、训练 CLI、release parity、进度文档和 Ruff 定向回归。
  软件契约证据见 [`docs/evidence/shared-policy-contract-20260912.json`](evidence/shared-policy-contract-20260912.json)。
- 当前 parity：`core_algorithm_hash=8f1fbbc7a6688d6a955faf71e71bcd9ba1147ae1d0dfeb007ed2f74689a53bd2`，
  `task_contract_hash=4137ce9a79d2ca3b5d39963abae4571853423899ccd5901241258ef282eca58`；
  `configs/release-parity.json` SHA-256=`4bed80a7417b6d26c02ae223b3b63dcd1dcf92f05636038abab9772229696c4c`。
- 尚未完成：learned weights、candidate promotion、新 artifact 的独立外部 SIM 复验、HIL/REAL、室外 GNSS、
  硬件验收和最终 `0.3.0` 发布。版本仍为 `0.3.0-pre.8`，发布门保持 fail-closed。

- 高层策略已进入真实软件 SIM seam：`SimulatorTaskPolicyBinding` 执行 `SharedTaskPolicy.decide()`，通过原 planner 重验后调用 `compile_planned_task_mission_from_runtime()`，生成并绑定 `ExecutableMission`；GPS/可行性失败走 `abstain`，不绑定 Mission。策略仍不输出 actuator，Mission 启动保持显式可选。
- 新增独立 synthetic SIM campaign：训练与评估输入 SHA、scenario family 均不重叠；training 数据 SHA 为
  `fcd1693881c1c7e2d51e738d8126b0388e161e62a5ea07b0ecbbbb69ca5fb6e6`，independent holdout SHA 为
  `9cbb52fe5c690fe970d9bd3aa39b5c0b618fe221f0b54f7af8e12da8f2d7be75`，证据见
  [`shared-policy-independent-sim-20260912.json`](evidence/shared-policy-independent-sim-20260912.json)。
  parent/candidate ranker SHA 均为 `ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`，
  无收益提升，`candidate_promoted=false`。
- `SimulatorScenarioRunner` 已支持可选 `SharedTaskPolicy` 身份绑定：严格核对 artifact/ranker/airframe SHA，
  证据见 [`shared-policy-sim-runner-binding-20260912.json`](evidence/shared-policy-sim-runner-binding-20260912.json)。
  当前 runner 只记录高层策略 provenance；`planner_revalidation_required=true`、
  `planner_revalidated=false`、`policy_applied_to_actuator=false`，低层 actuator policy 保持独立。
- 本轮覆盖回归为 `215 passed`，runner/campaign 增量回归为 `100 passed`，Ruff 通过；当前 parity 已按源码重新生成，
  `configs/release-parity.json` SHA-256=`ecd49dc4d718231b2d919ef0d9bd062972f5c5cbd84b584dbb55f93ad1f095ef`，
  core hash=`56e4620d81d8b9095a34a1134661a258ab0afbfd4070567e20d287dfe54d8a73`。
- 独立 campaign 仍是确定性 Python planning SIM，不是 PX4/Gazebo 外部 SIM、HIL、REAL、室外 GNSS 或硬件验收；最终发布继续阻断。

最新（2026-09-08）：带分段计时的新外部camera D通过像素/Mission门并精确清理，exit0；本次能量接收窗口完整，合成配置条件能量包围2.6115676–2.6120076Wh，非物理误差界。28934循环：pump/shadow/other累计82.920/7.099/1.194s，单次最大73.837/9.339/7.612ms。4470唯一帧匹配、206歧义，最大解码至处理76.067ms；未复现C的秒级积压。仅加诊断而未改调度，不能宣称修复或确定因果，应继续重复运行/负载证据验证。固定偏移仍不相容，训练同步标签/全模式发布未通过。原报告SHA和计时见current-progress.json，仍0.3.0-pre.8。

最新（2026-09-08）：Mission observer增加同域分段计时loop_timing，记录pump、shadow及其他循环阶段次数/总耗时/最慢32区间；使用Collector时钟，不声称CPU或网络时延。199项相关测试通过，包含真实observer旧/QPC两域计数与身份验证、上限和副本隔离。终态尾部采样不计入该诊断。未改变消息处理顺序/调度或1.5s门限，旧报告不补造字段；下一次外部运行采集新诊断后再决定接收与处理是否需分离。仍0.3.0-pre.8。

最新（2026-09-08）：camera C帧哈希诊断5800唯一匹配、323歧义。能量停止相邻油门对在路由解码间隔0.5001722s，Collector间隔1.4861834s，后帧解码至处理1.024175s；因此该缺口不只是上游消息间隔。全程唯一匹配最大处理延迟5.1890008s。心跳对哈希重复不强行匹配。不能据此区分socket队列、调度和任务处理成本；下一步需接收循环/状态处理分段计时。证据docs/evidence/qpc-camera-receipt-gap-20260908.json，原数据未改，无新外部运行，不放宽1.5s阈值。

最新（2026-09-08）：独立审计QPC camera C：像素门通过不变，但完整能量窗口不合格。回放在receive=24.1651958s停止，心跳年龄1.5555387s、油门1.5166356s超过1.5s，位置接收间隔0.1791879s；没有任务结束边界或完整能量值，不可用于完整能量训练。Clock1429条记录在ARM/完成两端严格包围，间隔89.114ms/108.8499ms；仍不声称源同步/事件时间界，training_label_eligible=false。修复Clock审计仅支持campaign的KeyError，单任务必须匹配初验与最终Mission哈希且清理后复验及Mission门通过，60项相关测试通过。原成功/失败报告均未重写，新Clock审计路径与指标见current-progress.json。当前仍0.3.0-pre.8。

最新（2026-09-08）：新固定镜像外部QPC单任务图像运行C通过当前门，进程exit0，3帧实际Gazebo BGR24各3686400字节，完整VTOL Mission、采集区间、事务日志、清理后复验均通过；逐帧文件哈希独立复核，容器精确清理后不存在。B此前因缺合格续航输入在创建事务前失败并清理；C显式使用合成能量配置100Wh/36W/324W/初始100Wh，非物理标定。A为dry-run。C的Clock固定偏移区间不相交，源同步与训练时间标签仍未证明，不能将像素门通过当作能量覆盖/全模式验收。所有原报告保持，路径/哈希见current-progress.json的qpc_camera_external_update。版本仍0.3.0-pre.8，未操作真实硬件或QGC。

最新（2026-09-08）：完整外层QPC图像生命周期fixture通过，覆盖父子session传参、ready等待、Clock注入采集、父生成complete、Mission区间及清理后重读复验；旧域路径同测。445项相关回归通过。解除QPC图像临时迁移禁用，单任务可选--host-clock perf_counter，--capture-clock对单任务是可选诊断；campaign仍要求capture-clock。未改变所有权/飞行状态/文件身份检查，不声称源时钟同步。外部新Gazebo运行仍待执行，所有当前生命周期进程/容器均为测试替身。JUnit SHA见current-progress.json。仍0.3.0-pre.8，无硬件操作。

最新（2026-09-08）：外层等待器可接收父身份；采集起止/complete/timing与清理后复验已传递selected_clock及身份。validate_sensor_capture从磁盘重读manifest并将manifest/各帧绑定调用方身份，内部一致且哈希有效的错误父会话仍拒绝。241项相关测试通过（含旧生命周期和QPC父验收正反例）。QPC单任务图像入口保护仍保留，尚需完整QPC生命周期测试、CLI联动及新外部运行。无新SIM/硬件运行，仍0.3.0-pre.8；JUnit为artifacts/evaluation/outer-pixel-clock-20260908.xml。

最新（2026-09-08）：_capture_gazebo_sensor_transport可注入HighResolutionSystemClock，backend与manifest封存共用该Clock身份；封存前校验全部帧，跨session或旧帧伪装QPC时拒绝且不写manifest。默认旧入口不变。325项生产/消费者/外层相关回归通过，含真实backend+内存wire进程fixture落盘/封存；不是外部Gazebo实跑。JUnit及SHA见current-progress.json的pixel_clock_seal_update。外层运行器仍待传Clock、ready等待、采集起止、complete、sensor provenance和Mission验收联动接线；CLI保护未解除，无新外部运行，仍0.3.0-pre.8。

最新（2026-09-08）：检测与定位消费者共用validate_frame_set_clock，在验证manifest哈希后绑定顶层captured_at_clock/host_clock_identity及所有帧；缺省顶层保持旧域，显式QPC必须完整身份一致。拒绝混域、缺失/null、跨session及分辨率变化。89项相关测试通过，包括两种消费者QPC正常路径及真值隔离。帧集哈希只证明完整性，不认证来源；外层父采集身份绑定、manifest生产者与Clock传递仍待接线。核心指纹更新3d4fec82…，历史报告对照另存pixel-consumers-parity-audit-20260908.json；JUnit路径/哈希见current-progress.json。无新外部运行，仍0.3.0-pre.8。

最新（2026-09-08）：共享Gazebo backend在注入HighResolutionSystemClock时，像素provenance标记host_perf_counter并保存不可变身份键值；导出的JSON身份为独立副本。默认旧源输出不变，Gazebo sensor_stamp仍是独立源时钟，captured_at_s仍为主机wire解码后读数，不是曝光时刻。50项backend测试通过（含两域实际read路径、遥测和像素一致性、身份副本隔离）。检测/定位消费者及外层采集接线仍待迁移，未开放QPC图像入口。核心指纹更新310c03f5…，新增历史报告对照`docs/evidence/pixel-clock-parity-audit-20260908.json`，旧报告未改且不覆盖当前核心。JUnit路径/哈希见current-progress.json的pixel_clock_source_update。无新外部运行，版本仍0.3.0-pre.8。

最新（2026-09-08）：Mission observer向complete校验传入实际QPC身份；父Mission验收核对ready/complete与所有区间样本的调用方时钟域，拒绝裸数值可比较但身份矛盾的证据。424项相关测试通过，含真实observer函数旧/QPC两域路径及父验收ready/complete/interval篡改。共享Gazebo backend的像素provenance仍硬编码旧域，检测/定位消费者也检查旧标签；必须连同这些消费者迁移，不能只开放外层入口。未新外部运行，版本仍0.3.0-pre.8。见`docs/evidence/capture-clock-consumers-20260908.json`。

最新（2026-09-08）：图像ready/complete屏障校验API支持调用方指定QPC身份，拒绝缺失/空身份、跨session、分辨率改变及旧标签伪装；缺省仍解释为旧monotonic域。416项相关测试通过，含QPC下原有30项飞行安全拒绝条件与两端身份篡改检查。尚未迁移全部生产者、等待器、区间与provenance消费者，CLI图像屏障限制保留；不能声称完整图像链路可用。本轮无外部运行，0.3.0-pre.8不变。证据：`docs/evidence/capture-clock-domain-20260908.json`。下方全量结果早于本轮屏障改动。

最新（2026-09-08）：QPC接线、接收诊断、父终态能量验收及poll哈希复用后的全量Python测试结束：4332项，4325通过、7跳过、0失败/错误，退出0，439.680s。实现文件在测试期间未修改。7项均为Windows符号链接创建权限不足，不能算通过。JUnit及当前核心/配置哈希见`docs/evidence/qpc-integration-full-20260908.json`。这证明本地软件回归结果，不替代完整QPC campaign、图像屏障、源时钟映射或SIM/HIL/REAL验收；仍为0.3.0-pre.8。

最新（2026-09-08）：连续负载间隔与唯一匹配间隔分开。C最大连续油门间隔2.251s，对应路由解码间隔1.663s、Collector末端pump间停顿0.413s，根因不能单归队列。剖析发现等待poll重复任务哈希，已复用本次snapshot结果而不设持久缓存；1000次调用哈希次数2000→1000，本机单次cProfile0.351→0.229s，不声明任务级加速或秒级延迟已修复。239项相关测试通过，核心指纹及历史验收对照更新；无新外部运行。证据`docs/evidence/poll-hash-reuse-20260908.json`。

最新（2026-09-08）：新QPC campaign C完成两任务并精确清理，原外层gate为true，但独立能量审计发现第二任务196.775s处负载过期、结束窗口不完整。已补父验收每sortie能量窗口检查；当前验证器重审原C报告明确失败，未改原报告。新增帧SHA绑定审计：累计11593条唯一匹配、767条歧义不猜测、无缺失，观测到最大约1.659s路由解码后处理延迟；尚不能细分socket队列/调度或据此断言B根因。前后6探针跨289.376s不支持固定偏移模型，两个Clock接收窗口覆盖但不声称源时钟同步。406项相关测试通过。证据`docs/evidence/qpc-receipt-campaign-20260908.json`；当前完整QPC验收仍未通过。

最新（2026-09-08）：为定位B接收缺口，QPC campaign路由器增加有上限的原始帧哈希+用户态解码/转发时间记录，复用父时钟身份；Collector能量样本加入帧哈希及pump进入/上次退出时间。400项相关测试通过，含实际本机UDP转发字节一致性/访问策略回归、ManualClock区分pump间停顿与接收等待、异常清理。未运行新外部campaign、未补写旧数据、未改变1.5s阈值，根因仍未确定。下次运行需用帧哈希关联两端，重复哈希必须标为歧义；用户态时间不能冒充内核接收/PX4发送时间。证据`docs/evidence/telemetry-receipt-diagnostics-20260908.json`。

最新（2026-09-08）：无图像屏障campaign已接通父子进程QPC源/session传播，父探针/JSONL同域，sortie完成及清理后核对父身份。403项相关测试通过。新外部A运行暴露终态flight漏身份字段，已修复并用实际observer返回值回归；新B运行通过该身份检查，但第二任务在事务创建前因累计能量load_or_receive_gap拒绝。首任务约21.585s处心跳/油门年龄1.533/1.529s，位置接收仍连续，尚不能归因发送、路由或调度；未放宽1.5s阈值。两个容器都精确清理，失败原始报告未改。完整QPC campaign仍未验收通过，下一步诊断负载消息接收间隔。证据`docs/evidence/mission-clock-cli-20260908.json`。

最新（2026-09-08）：在线累计能量消费者已支持有身份的QPC历史；会话内换域、换身份、换原点会原子拒绝，原能量不受失败更新影响。身份校验从探针脚本移入共享Clock，审计器/探针/能量消费者复用。132项相关测试及212项Mission/Collector/文档/一致性回归通过。核心指纹更新为`379622f2fb8a432d2fff9d33b1ad1a77f8f523f13e142bf5c3bed7928e185f1f`，历史E7报告仍不覆盖当前核心；新增对照记录而未改旧报告。证据`docs/evidence/cumulative-energy-clock-20260908.json`。外层父子进程与图像屏障仍待接线，无新外部Mission、无全量重跑。

最新（2026-09-08）：能量窗口审计支持有身份的QPC接收域，严格绑定window/flight/observation/history四方source、session、implementation和resolution；拒绝缺失、空身份和用旧标签伪装QPC。114项相关测试通过，CLI仓库外运行已验证；旧campaign-clock-c的mission-001重新审计通过，仍是旧时间域证据而非新QPC任务。记录见`docs/evidence/energy-window-clock-20260908.json`。累计能量消费者、父子进程接线及图像屏障仍待迁移；未重跑全量，未开启新外部任务或实机。

最新（2026-09-08）：父进程探针/Clock JSONL支持注入同一HighResolutionSystemClock，逐行及报告绑定source/session，wait_after要求同域HostClockStamp。独立审计重算探针摘要并核对Mission/JSONL身份，拒绝旧新混用。235项相关回归（11项新增）通过。固定无网络容器6次QPC真实探针RTT0.5961–0.8924ms、条件偏移区间宽0.593672ms、跨度4.5581ms，无零RTT；精确清理。仍不是整段漂移/源时钟同步，外层CLI、子进程传参和图像屏障尚待接线。证据`docs/evidence/qpc-capture-binding-20260908.json`；未重跑全量。

最新（2026-09-08）：Mission事件/等待/协议截止时间已统一Collector时钟，Session可注入Clock，终态尾段严格校验高分辨率history身份，窗口与完成报告保存源身份。8项新增测试及370项相关回归通过；修正旧测试在Collector创建后倒拨时钟的初始化顺序，未放宽回退检查。Session初始化失败关闭连接。父进程探针/JSONL和采集屏障仍待迁移，高分辨率与旧屏障组合在控制动作前拒绝；无CLI开关、无新外部SIM、未重跑全量。证据`docs/evidence/mission-clock-injection-20260908.json`。

最新（2026-09-08）：TelemetryCollector时钟注入已实现，所有接收/elapsed/心跳/等待截止时间共用now_s；高分辨率观测拒绝外部裸now_s，报告/能量/电池历史保留时间源身份。9项新增测试及362项相关回归通过，默认SystemClock不变。Mission上层仍待迁移，未增加外部切换开关、未新SIM运行、未重跑全量。证据`docs/evidence/collector-clock-20260908.json`。

本轮全量终态：4210 passed / 7 skipped / 0 failures/errors，353.499秒，进程exit0已观察；JUnit SHA及大小见`docs/evidence/high-resolution-clock-20260908.json`。下方“全量进行中”为中间状态。此次全量覆盖当前新增Clock和前几轮探针/审计代码，但不是新外部Mission验收。

最新（2026-09-08）：共享Clock新增可注入HighResolutionSystemClock及HostClockStamp，同源/同会话整数纳秒差值，跨域/跨会话/回退拒绝；SystemClock/ManualClock及外部默认路径未切换。16项Clock测试（12项新增）及90项相关检查通过，全量回归进行中。clock.py原未纳入core parity，现已加入，core更新为327daf18…；旧E7报告SHA复核后另存新对比审计，仍不覆盖当前core/task，不重写历史。迁移边界与顺序见`docs/host-clock-migration.md`。

最新（2026-09-08）：探针改为单连接READY握手后逐项请求/序号验证，原逐次exec保留作对照。发现本机Python3.12 monotonic是GetTickCount64，分辨率15.625ms；记录分辨率并按显式“读数误差不超过该值”条件扩展区间，分辨率不是精度认证。相同无网络容器6次对照，逐次exec条件区间宽259.580998ms，长连接27.095559ms；长连接host读数全相同，零RTT是量化受限，不是零延迟。首次CRLF问题已复现修复，两次容器均精确清理；235项相关回归通过。没有新Mission或全量回归。证据`docs/evidence/persistent-clock-probe-20260908.json`。下一步评估统一高分辨率主机时钟及迁移契约，不单独替换探针时钟来混用Mission域。

最新（2026-09-08）：采集器开始/结束各加入3次主机→同容器monotonic只读探针，保留因果请求区间与原始整数时间。恒定偏移/等速假设下求交集，不相容则返回null，不取平均制造同步。223项相关回归通过。无网络临时容器6次真实查询成功并精确清理：RTT218–344ms，条件偏移交集宽213.842163ms，仅1.5秒观测；Docker exec逐次启动开销明显，不能宣称高精度同步。尚未用新探针重跑完整Mission，旧c报告不含探针，不补写。证据`docs/evidence/clock-offset-probe-20260908.json`。下一步评估长连接探针降低观测区间，再验证整段任务漂移。

最新（2026-09-08）：新增 `audit_mission_clock_receipts.py`，从显式SHA绑定父报告、原始Clock及逐项Mission，检查来源/序列/整数时间/主机接收单调性并报告严格事件前后样本和最大间隙。campaign-clock-c两份原报告独立重放通过；west边界接收间隙94/94ms，east203/203ms；整段最大主机间隙750ms、Gazebo real间隙756.226384ms。不是100ms固定采样保证，也不是事件源时间误差界。35项相关测试通过，本轮未重跑全量；旧全量结果不覆盖新增审计器。证据 `docs/evidence/clock-receipts-20260908.json`。

本轮全量JUnit已核验：4149 passed、7 skipped、0 failures/errors，339.263秒。原执行句柄未恢复，不声称已观察到全量进程退出码；文档/parity 34项、Ruff及diff检查通过。以下“全量仍在运行”是中间记录。

最新（2026-09-08）：Mission 同容器原生时钟采集已接入 `--capture-clock`。campaign-clock-c 双任务完成、逐项 journal 校验及清理后完整性通过，outer exit0；2396 条记录覆盖两项任务的主机接收窗口。模拟时间推进216.932秒、Gazebo real推进248.294505336秒，窗口比率0.8736882828；不是跨时钟同步或物理标定。186项相关回归通过；全量回归结果待终态。A/B失败确认是过早订阅使PX4误判Gazebo已启动，已改为先验证真实publisher再订阅。证据：`docs/evidence/mission-clock-20260908.json`。版本仍为0.3.0-pre.8，发布门槛未全部完成。

最后更新：2026-09-08（v198 工作树）

最新：新增gazebo_clock_capture.py，使用固定镜像已确认的gz.msgs.Clock读取sim/real/system及独立容器接收monotonic_ns，拒绝缺失字段/回退/序列错误，保留暂停语义。有界无网络空世界实际采到20条、sim跨度19ms，主机重放一致，命名临时容器已自动清理。52项相关回归通过（11项新增）；不是任务窗口时钟对齐、不是飞行倍率/标定。下一步接入同一Mission容器全窗口采集。证据`docs/evidence/gazebo-clock-20260908.json`。

最新：规划执行审计新增接收归属的PX4 boot区间包络与boot/host比率，覆盖不足输出null、禁止外推。新tail两项重放：west主机124.172秒对应boot区间92.192–92.272秒（0.742454–0.743098）；east主机106.422秒对应80.704–80.760秒（0.758339–0.758866）。不能混用为同单位训练标签，也不等于Gazebo实时倍率或物理时钟同步。41项相关回归通过（7项新增），本轮未重跑全量。证据`docs/evidence/clock-window-20260908.json`。

本轮全量4126 passed / 7 skipped / 0 failures / 0 errors，280.230秒，exit0；文档后34项检查、Ruff/diff通过。JUnit身份见terminal-tail证据。

最新外部验证：campaign-tail-a同会话west→east两项完成，observer耗时123.797/106.015秒，ARM→完成124.172/106.422秒，原终态时间未挪动。两项尾段后原始历史独立重放均覆盖起止边界，合成窗口包络约2.7025772–2.7033772Wh及2.2563316–2.2568916Wh；不是电池实测或物理误差界。父进程逐项完整journal检查及清理后完整性通过，outer exit0；容器20b9668e3a2b6de9fe69d5d37158ce8087f89400567045905cb12796005ee413已精确清理。证据`docs/evidence/terminal-tail-20260908.json`。下面“新SIM待确认”为实现时历史状态。

最新实现：Mission终态后新增有界观测尾段（默认2秒，最多256次pump），保留原completed_host_monotonic_s与duration，不将尾段时间计为任务耗时。只接纳终态之后、当前观察时刻以内的有效1/1 GLOBAL_POSITION_INT；等边界、未来、错误来源/字段拒绝。报告post_terminal_tail明确收到结束候选样本或超时/适配器缺失，不自动声明完整能耗或授权下一任务。203项相关测试及170项外层测试通过。新隔离模拟与全量回归结果待终态确认；旧u原报告不改。

最新：新增planning/execution只读审计，独立输入文件SHA绑定、文档hash与任务/计划/完成结果关联校验，当前原规划器重算历史u两项计划一致。估算228.156/228.153秒，主机ARM到完成窗口104.218/109.703秒；不能跨时钟或忽略保守预算语义据此拟合修正系数。两项能量重放均缺结束边界样本，窗口能耗保持null。34项相关测试通过（8项新增），未重跑全量。下一步补终态后有界观测尾段，不改终态时间、不补造历史数据。证据`docs/evidence/planning-gap-20260908.json`。

最新：参考基准v2增加收益/完成数/估算时间Pareto前沿及不超过贪心耗时的对照。原9例收益89→90，但同耗时上限收益仍89；新增12例异质诊断收益120→120（每例2任务因时长上限排除），配对仅将任务时长上限统一600秒后每例4任务初始可行，12例仍147→147。未找到支持替换排序器的新证据。114项相关测试通过（4项新增）；全量未重跑。新增场景是相关合成回归，非独立留出。证据`docs/evidence/sequence-frontier-20260908.json`。下一步优先改善场景/状态与模型依据，而非为证明重构有益继续调参数。

最新实验：多步精确参考基准已实现，仍调用原规划/编译/预算状态推进；9例总收益贪心89、参考90，仅1例提升（5→6，两个较小任务替代一个高优先级任务）。共283节点，最多65节点/例；本机单次搜索累计0.263秒、最大0.059秒/例。最多6任务、节点超限失败，不输出部分最优。110项相关测试通过（8项新增，包含独立排列枚举核对与超限/不合格输入/覆盖拒绝）。证据`docs/evidence/task-sequence-reference-20260908.json`。支持局部多步规划评估，不足以推荐全面重写；未部署、未新外部飞行、非独立留出。

本轮全量4099 passed / 7 skipped / 0 failures / 0 errors，331.461秒，进程exit0；文档更新后34项文档/parity检查及Ruff/diff检查通过。JUnit大小与SHA记录于ranker-faults证据。

架构重审：按当前要求，不再把旧模型/算法/模块结构视为不可改变。新增`docs/autonomy-architecture-reassessment.md`，区分目标、安全/现实约束、软实现选择和未验证假设；推荐下一步先用小规模多步精确参考量化贪心差距，而非默认继续调线性权重。该替代规划器尚未实现，端到端重写收益尚无证据，不提前宣称更优。

最新增量：冻结上一轮候选，新增evaluate_task_ranker_faults.py，9个原场景×9种故障共81项共享规划输入拒绝检查实际通过。覆盖GPS fix/精度、围栏、相机、感知、地图、DEM、电池和续航；每例要求正常对照可完成任务，故障后零完成、全部任务ID及拒绝原因一致。13项CLI/故障回归通过（6项新增）。这是输入级预算评估，不是传感器断流、飞行中失效恢复或PX4故障注入；未训练/晋升新候选，未放宽安全规则。证据：`docs/evidence/ranker-faults-20260908.json`。

当前增量：训练CLI与显式9例合成预算数据集已交付，每个train/validation/test分组3例。实际搜索父候选及8组参数，selected_trial=0，保留原零系数基线；三组收益分别26/28/35，候选与父候选相同，无性能提升、无候选晋升。96项相关测试及全量4093 passed / 7 skipped / 0 failures / 0 errors（206.404秒）通过。证据：`docs/evidence/budget-training-cli-20260908.json`。分组是相关旋转几何家族，不证明真实泛化；此次未执行新的外部动力学、HIL、REAL或GUI验收。保持0.3.0-pre.8，release blocked。下面“尚无数据集/CLI”属于历史记录。

本轮全量4086 passed / 7 skipped / 0 failures / 0 errors，204.389秒；证据budget-fit-20260908。34项文档/parity检查及全仓Ruff通过。

最新：预算episode接入同一可选TaskRanker并记录身份；新增task_training有界参数搜索/独立评估API，必须提供显式父候选，先评估父候选、同分保留父候选，按完成priority+1收益/任务数/估算耗时排序。89项相关测试通过。测试验证刻意差父候选可恢复基线，并拒绝重复输入/评估重用训练输入；不代表超越项目基线。尚无项目训练数据集、CLI或留出性能结论。core parity更新，旧外部u证据不覆盖新预算训练API。见`docs/evidence/budget-fit-20260908.json`。

最新外部u轮通过：未训练候选实际执行synthetic-west→synthetic-east，分别103.937秒/109.422秒完成。两项及汇总ranker hash一致；父进程按同一候选重算顺序/编译，完整journal哈希链、授权绑定、序列、提交/启动终态及清理后完整性均通过；精确容器已清理，exit0。证据`docs/evidence/campaign-u-20260908.json`。这证明候选执行链，不证明学习收益、物理标定或HIL/REAL/UI验收；下一步训练/留出评估使用同一TaskRanker扩展。

本轮全量4084 passed / 7 skipped / 0 failures / 0 errors，178.929秒；证据campaign-ranker-20260908。34项文档/parity检查与全仓Ruff通过。

最新：campaign配置可嵌入严格ranker定义，子进程使用同一TaskRanker，汇总及逐项记录规范hash；父进程按同一配置重算候选选择/编译并验证身份，负向audit同样支持候选。332项相关测试通过，新增10项（7项带候选的执行器fixture、3项父进程候选复核；后者journal校验为替身）。新增vtol-ranked-routes.synthetic-campaign.json，反转优先级仅用于未训练集成测试。尚未外部运行该候选，不声明训练或性能提升。见`docs/evidence/campaign-ranker-20260908.json`。

本轮全量：4074 passed / 7 skipped / 0 failures / 0 errors，185.462秒；证据task-ranker-20260908。34项文档/parity回归与全仓Ruff通过。

最新：新增可选TaskRanker数据加载器，要求外部提供文件SHA-256、最多64KiB、严格JSON/有限系数/固定base与feature集合。零系数与原排序一致，非零系数仅重排可行任务并经过动作重校验。run_observed_task_session可选接入，每步记录ranker_sha256；默认行为不变。87项相关测试通过（10项新增），外部campaign/UI尚未接线，未训练/加载生产checkpoint。core/task parity重新生成并对旧发布报告独立比对，历史证据不覆盖新候选路径。见`docs/evidence/task-ranker-20260908.json`。

本轮全量4071项：4064 passed / 7 skipped / 0 failures / 0 errors，189.203秒；证据task-policy-contract-20260908。跳过项不计通过。

最新：新增共享task_policy观测/动作API，观测从原选择器的可行集生成，动作绑定观测hash并在解析时重算可行性；默认动作与原排序/计划一致，可显式不选择。44项规划器测试通过，新增12项契约回归。未接运行循环、模型加载器或训练。新增文件已绑定core/task parity，两组hash改变；旧外部t等证据只覆盖当时实现，不证明新API端到端通过。见`docs/evidence/task-policy-contract-20260908.json`。

最新外部t轮：第一项完成133.656秒，第二项预算不足拒绝、事务未创建。audit --require-energy从两轮原始历史累计重放，与消耗/续航/上下文hash逐项一致；累计合成消耗2.5244368Wh、剩余30.9755632Wh。容器精确清理通过。外层成功门仍false/exit1，独立负向audit exit0；无物理标定、抓包、训练或HIL/REAL声明。证据`docs/evidence/campaign-t-20260908.json`。接下来进入共享策略观测/动作契约、候选加载与扰动评估；不得另建与实机分离的策略。

以下为历史增量记录，其中“尚未外部运行/重放”的状态仅对应当时版本；当前结论以本段和campaign_t_update为准。

最新全量回归：4052 passed / 7 skipped / 0 failures / 0 errors，231.346秒；见rejection-energy-input证据。覆盖累计重放审计器与当前Mission报告实现；34项文档/parity回归通过。

最新实现：Mission报告新增planning_energy_input，保存投影前上下文及实际输入历史；选择失败后仍保留planning_energy。拒绝audit新增--require-energy，以同一新累计器顺序重放两轮历史，核对消耗、续航、上下文hash、历史前缀与采集起点。322项相关回归通过（新增5项重放/篡改测试）。本轮尚未新外部运行，旧s报告缺少原始历史，不能补写旧证据或声称其能量已独立重放。证据`docs/evidence/rejection-energy-input-20260908.json`。

最新外部负向：s轮初始33.5Wh显式合成能量，第一项完成（92.235秒），第二项synthetic-west因续航预算不足被共享选择器拒绝。原报告事务未创建、journal不存在、条目数0、无控制命令，容器精确清理。独立audit从原上下文重算选择与原因一致；317项相关回归通过。外层成功门仍false/exit1（未完成所有任务），负向验收单独记录。第二项原始能量历史未保留，未独立重放其积分；不声称抓包证明、物理标定或训练。见`docs/evidence/campaign-s-20260908.json`。

本轮全量回归：4041 passed / 7 skipped / 0 failures / 0 errors，210.942 秒。JUnit 身份见 selection-rejection 证据；此结果覆盖本轮源码，下面 4006 passed 为历史结果。

最新增量：连续任务无可行候选时，以 TaskSelectionRejected 保存原规划上下文、canonical hash 和逐项拒绝原因，并由原 Mission 失败报告写出。311 项相关回归通过，覆盖低电量/低续航拒绝的上下文身份及异常报告序列化；外部低能量负向仍未运行，不能据此声明第二项在 PX4 上未上传。证据：`docs/evidence/selection-rejection-20260908.json`。保持现有规划器与模型，不修改权重，不提升最终发布状态。

最新：r轮不同路线synthetic-east→synthetic-west实际双任务通过，已提交wire hash不同；92.031s/100.157s，第二项规划剩余98.4116556Wh，固定home一致，容器清理和清理后复核通过。310项回归通过，新增3项覆盖不同wire坐标与低电量/低续航停止。外部低能量拒绝尚未运行。证据`docs/evidence/diverse-campaign-20260908.json`。

最新外部证据：q轮同会话双任务通过，synthetic-high→synthetic-low，95.015s/120.578s，home和采集器起点一致，第二项规划剩余97.508046Wh。9719段可用历史重放无gap（合成耗能5.0623828Wh），父进程重算选择/编译/journal并在清理后复查通过。307项相关测试通过。证据`docs/evidence/campaign-q-20260908.json`；旧o/p失败保留。不声明相机采集、真实电池标定、训练、HIL/REAL或最终发布。

最新：Docker外层campaign入口已接，304项相关测试通过。实际m/n两轮均完成第一任务但未完成第二任务：m为回读current标记不符，n为连续能量覆盖gap。两个精确容器均清理。已补受保护游标归零及复用会话前置检查持续采集，最新源码尚未外部复测。双任务门仍未通过；证据`docs/evidence/campaign-docker-20260908.json`。

最新：连续任务模块CLI与严格配置加载已实现，新进程--help可用；297项相关回归通过，6项新增。候选/runtime全部来自显式文件，同一能量会话按顺序执行并汇总。Docker外层生命周期入口仍未接入，实际双任务未验证。证据：`docs/evidence/campaign-cli-20260908.json`。

最新：连续已选任务Python驱动已实现（scripts/vtol_selected_task_campaign.py），每轮调用共享选择/编译和原Mission执行。291项相关测试通过，新增5项使用模拟执行器的编排回归；实际PX4/Gazebo双任务未运行，外层CLI尚未接入。见`docs/evidence/selected-campaign-20260908.json`。

最新接线：run_mission_acceptance可接selection_provider，投影能量后选择任务，复核后保留原编译对象进入原事务路径；策略与时间预算不能被回调扩大。286项相关测试通过，4项新增。外部连续任务驱动/任务后采样仍待完成，无本轮外部或全量结果。见`docs/evidence/selection-provider-20260908.json`。

最新入口：已选任务编译结果可经build_selected_acceptance_documents复核后绑定原SIM事务文档；不重写选定任务，不刷新上下文。282项相关测试通过，6项新增；外部连续任务驱动尚未使用该入口，本轮没有外部复测或新全量。证据：`docs/evidence/selected-compilation-binding-20260908.json`。

最新全量回归：4006 passed / 7 skipped / 0 failures / 0 errors，185.467秒；JUnit大小与SHA-256已记录在session-home证据中。跳过项不计为通过。

最新增量：同会话规划home已固定，飞机位置仍取新观测；健康active会话才能绑定。276项父子回归通过，全量结果见`docs/evidence/session-home-20260908.json`。本轮不声明外部双任务；上一轮l的外部证据属于修改前单任务源码，不能升级为本轮双任务证明。

最新：外层能量参数已接通并在Docker前验证；修正上下文时间取整造成的因果比较误拒绝。272项父子脚本回归通过。外部l轮共享规划单任务、固定翼相机采集与精确容器清理通过，任务81.859s；规划输入99.95784Wh/999.5784s来自显式合成配置，不是真实续航。k轮失败原样保留。证据：`docs/evidence/shared-energy-external-20260908.json`。以下“外层未转发”是历史记录；双任务与训练仍待完成。

最新增量：验收子脚本已接通显式能量 CLI（五项全有才启用累计会话）；新增5项回归，脚本102项通过、ruff通过。外层 orchestrator 尚未转发，新外部运行及全量未执行。下面“CLI未接”的记录保留为上一轮历史。证据：`docs/evidence/energy-cli-20260908.json`。

外部验收规划路径纠偏：发现原脚本手工拼接 PlanStep，现改为共享 plan_task；不再用 mission_timeout 作为剩余续航。观测续航须来自1/1新鲜电池消息；可复用会话支持显式 energy_profile+initial_remaining_wh，累计状态在原上下文/计划哈希绑定前投影。新增12项回归，相关292 passed。协议测试明确1800s合成续航/600s任务预算，另保留低预算拒绝；未改共享规划阈值。尚无新外部运行/全量；CLI与orchestrator未接显式能量配置，PX4续航缺失时旧命令现在会在上传前拒绝，不能沿用j轮通过结论。
见 [共享规划接线证据](evidence/external-shared-planner-20260908.json)。外部双任务仍未验证。

复用会话的成功报告写入/输出现在通过现有工作线程辅助执行，主线程持续独占接收；任务占用直到报告输出完成才释放，发布或接收异常会阻止下一任务并等待工作结束。能量历史新增显式容量：单任务默认8192不变，复用会话默认65536，上限131072；超限仍导出丢弃数量，累计重放拒绝截断，不宣称无限连续。新增9项回归，相关264 passed；尚未新外部双任务/全量回归。
见 [发布与容量证据](evidence/session-publication-capacity-20260908.json)。下方“报告输出暂停接收”为修复前状态。

外部任务生命周期拆分：MissionTelemetrySession 持有一次 loopback 连接/TelemetryCollector，run_mission_acceptance 可借用；成功后保留采集起点和历史，失败后禁止复用，连接由外层显式关闭。容器仍由原 orchestrator 管理，任务来源/所有权/协议门保留；已有证据路径拒绝覆盖。7 项新增回归、相关238 passed，包含原函数失败写报告并 SystemExit(1) 的清理验证。未运行外部双任务；报告输出期间接收与8192条历史上限仍需处理，未重跑全量。
见 [会话生命周期回归](evidence/mission-session-lifecycle-20260908.json)。本轮未新增实机或训练结果。

累计能量通过 project_context 接入既有 ContextSnapshot：仅 SIM 传感器适配，复用原任务循环/planner/PX4 编译与完成监控；只替换电量和假设续航，不刷新时间戳或位置/GPS/围栏/感知质量。上下文和最后有效能量时钟样本均要求现有 1.5s 时效，拒绝未来、换会话、机型/home改变；失败不提交累计状态。10项新增回归、相关84 passed，双任务回调 fixture 已验证第二项绑定第一项后的能量快照。未进行外部连续双任务，未重跑全量、未训练；调用方仍须绑定同车会话与 collector-relative Clock，不构成来源认证。
见 [观测适配回归证据](evidence/sim-energy-context-20260908.json) 与
[当前共享核心身份比较](evidence/sim-energy-context-parity-20260908.json)，历史 E7 仍失配且未重跑。

同一外部模拟会话的累计能量状态已加入现有 energy_replay：CumulativeSimulationEnergy 复用原 replay/flight_model 功耗函数，初始 Wh 显式绑定首个完整样本，追加历史计算增量，重复输入不重复扣账。修改旧样本、缩短历史、换会话/时钟起点、截断或时效缺口均拒绝且不改变已接受状态；解除武装不补能，归零导出未满足能量需求，不声称掉电动力学。新增8项回归，相关55 passed。j记录两批重算（非两次任务）：10Wh测试初值→8.7754996→7.8331452Wh，重复新增0Wh。当前仅进程内状态，尚无磁盘恢复、实时多任务接线或补能入口；全量未重跑。
见 [累计状态证据](evidence/cumulative-energy-20260908.json)。共享核心哈希已更新，
[当前与历史门的比较](evidence/cumulative-energy-parity-20260908.json) 不等于新 E7 运行；最终版仍阻断。

本轮窗口能耗版本全量：3943 passed / 7 skipped / 0 failed/errors，192.581s。
见 [全量报告身份](evidence/window-energy-full-20260908.json)；跳过不计通过，未新增 HIL/REAL/UI 验收。

窗口能量增量已实现：复用 SimulationEnergyProfile.consumed_wh 与因果左侧负载，严格内部区间计入下界，接触/跨越接收边界的区间计入上界；相同接收时刻也保守视为边界，不插值两种时钟。完整前缀积分与原 replay 结果交叉核对，缺失窗口端点不产生区间。18 项相关测试通过。j 原始记录重算为 2.1126148–2.1132548Wh，边界量 0.00064Wh（3 区间、0.064 boot 秒），不是全前缀 2.1668548Wh。仅在测试功耗/接收归属假设下成立，不是物理误差界限，不授权下一任务。
见 [窗口能耗重算证据](evidence/window-energy-envelope-20260908.json)，本轮未新增外部模拟。

j 轮任务/画面采集与清理通过（任务 101.594s）。5584 条能量输入因果前缀 5069 区间、101.380s PX4 boot 时间无时效中断；ARM 请求至完成接收窗口 18.047–120.750s，两端由前缀样本包围（boot 11400/107404ms）。新增只读 audit_mission_energy_window.py 从原始绝对边界重算，不信嵌入通过标记，不以全前缀能耗冒充窗口能耗。7 项新增回归，相关 16 passed。物理同步、窗口能量、下一任务授权仍未声明。
详见 [j 轮证据与复核命令](evidence/mission-window-audit-20260908-j.json)。本轮未重跑全量。

任务时间窗口已显式记录：能量历史输出实际 collector 的宿主单调时钟起点，任务报告保存 permit 后 ARM 命令前至接受落地/解除武装观测的窗口，并保存飞行监控自身起点。缺失、非有限、布尔或倒序边界不生成相对窗口。新增 10 项测试，相关 240 passed；这只是接收时间绑定，仍不声称 PX4 boot 对时或完整能量覆盖。旧 i 轮缺少这些字段，不回填推断；需要新外部记录。

外部 i 轮已通过：任务 84.547s、画面采集门通过且精确 ID 容器清理成功；4433 条输入因果重放覆盖 4023 区间/80.456s PX4 boot 时间，没有 1.5s 空档停止。测试 profile 积分 2.5506572Wh，不是物理标定或全任务边界证明，仍不授权下一任务。h 轮此前未收到启动心跳而失败，原报告保留并已清理。见 [本轮证据](evidence/preparation-telemetry-20260908.json)。

准备阶段接收修复：四条既有参数命令与任务文档生成在单个工作线程执行，主线程继续独占 collector.pump；参数设置完成后重新取得上下文。子进程原 10s 超时保留，任何退出均等待工作结束，防止准备操作与容器清理交错。新增 3 项线程/错误回归，相关 230 passed；未改变模型、参数值或任务安全门。此次变更后的全量回归未重跑，之前 3921 passed 属于前一版源码。

本轮全量：3921 passed、7 skipped、0 failed/errors，197.852s；Ruff 与 diff whitespace 检查通过。
报告身份见 current-progress.json 的 protocol_telemetry_update.full_suite；七项跳过不计为通过。
证据见 [协议遥测回归](evidence/protocol-telemetry-20260908.json)。未进行新外部模拟、实机或 UI 验收。

任务协议等待已接入现有 collector.pump，上传请求、回读 COUNT/ITEM 等待期间的遥测进入同一记录器；无关消息不刷新超时、不消耗协议重试。新增 3 项回归，相关 227 passed，Ruff 通过。尚未外部重跑，不能断言已消除 g 轮 1.516s 空档，也未完成全任务能量覆盖或策略训练。

最新因果重放：新增 energy_replay，复用原能量 profile 的积分函数，以区间左侧已知状态按 PX4 boot dt
积分，1.5s 时效/错源/时钟回退/截断拒绝。相关 112 passed。g 轮原始记录仅覆盖 131 区间、2.62s，
随后宿主接收间隙 1.516s、负载年龄 1.719s 而停止；0.0262Wh 是测试假设下的已覆盖前缀，不是全任务能耗。
见 [因果重放证据](evidence/causal-energy-replay-g-20260908.json)，旧完整回归属于此次改动前。
当前核心身份见 [新 parity 比较](evidence/energy-replay-parity-20260908.json)，下方旧哈希为历史。

最新 g 轮实测：任务与采集门通过，130.484s completed、容器清理；6066 条输入，0 无效/外来源/截断。
GLOBAL_POSITION_INT 5508 条（boot 时间无倒退/重复）、HEARTBEAT 112 条、VFR_HUD 446 条（油门 0–93%）。
没有 SYSTEM_TIME；boot 首尾差 111.416s，宿主接收差 136.953s。不能混用两时间域，Gazebo 对时尚未证明。
见 [g 轮输入证据](evidence/energy-input-20260908-g.json)，能量积分/连续任务/训练仍未完成。

最新接收器增量：外部 PX4 武装/油门/启动时钟独立原始历史，记录来源、无效字段与截断数量。
相关 223 passed；尚未证明 PX4/Gazebo 时钟同步，未据此积分能量或启动连续任务。
此前全量 3898 passed 属于这次采集字段改动之前，本轮未重跑全量。

最新全量回归：3905 项中 3898 passed、7 skipped、0 failed/errors，203.460s；全仓 Ruff 通过。
7 项均因 Windows 无符号链接创建权限，未计作通过，相关符号链接安全路径仍需合适环境复验。
运行期间未修改源码；见 [当前能量/场景全量证据](evidence/energy-scenario-full-20260908.json)。
该结果不替代外部能量、UI、HIL/REAL 或 E7；下方“未重跑全量”为历史阶段记录。

最新兼容修复：启用能量 profile 时，电量突降故障从每帧当前电量扣减，不再输出旧固定值。
20% → 故障观测 10%；底层耗至 10% → 观测 0%；故障结束且底层剩 5% → 观测 5%，不免费补能。
相关 81 passed，未启用 profile 保持历史行为。当前外部 wire 有模拟时间但无 armed/throttle，
需要有来源/时效的状态关联，尚未接通外部能量；见 [兼容验证](evidence/energy-fault-parity-20260908.json)。
当前核心 `7a2a629bc840afd6618bc560d5d67eacda642c5319a8a0425f88d55ad7fee7bc`，下方哈希为历史阶段。

最新场景接线：runtime 通道的 vehicle.energy_profile 经严格解析、机型哈希绑定后，由现有
ScenarioRunner/ClosedLoop 传给原飞行模型；输入保持无目标真值。新增 8 项回归，相关 144 passed。
配置改动未更新通道规范 JSON 哈希会拒绝，相同磁盘输入复现相同结果；外部 Gazebo 仍待接线。
当前 core/task 身份见 [场景能量 parity](evidence/scenario-energy-parity-20260908.json)，
旧发布报告两组哈希均失配，未重跑 E7。下方“场景待接入”等及旧核心哈希为历史阶段。

最新能量实现：现有 DeterministicFlightModel 可显式启用 SimulationEnergyProfile，按运动步长和油门
积分功耗；跨架次保留电量，解除武装不补电，显式补能要求地面/静止/未武装并记入事件。
既有 battery_remaining_pct 输出使用该状态，完整能量假设与估计续航写入模型证据；参数未物理标定。
相关 57 passed。外部 Gazebo 后端、场景配置与连续任务接线仍待完成，不以本地模型测试替代。
当前核心为 `37b075fcd5b802d92d004626059508be2b2770a9a49e9c7dbafdab067266efba`；
见 [新核心与旧门比较](evidence/energy-parity-drift-20260908.json)，下方核心哈希/待实现描述为历史阶段。

能量观测增量：现有 VTOL 接收器新增有界电池历史（8192 条），保留原始单位、哨兵、来源和截断数。
电量不再接受布尔/字符串/小数的强制转换；相关 212 passed。剩余续航估算和外部连续任务仍待完成。
f 轮外部任务通过，88.578s completed、42 条电池样本、0 截断、容器清理成功。电流 -1、累计电量 0、
能量 -1、剩余时间 0；这些不是零耗电证明。容器版本探针绑定的 PX4 源码显示无电流传感器、
按时间放电、解除武装内部电量复位。因此需跨任务持久能量状态，不能以落地电量恢复当免费补能。
见 [f 轮能量观测与源码身份](evidence/battery-history-20260908-f.json)。

最新一致性更新：4 个增量决策/预算模块已进入共享核心 parity，当前核心哈希为
`772e9af48a96adea63645a25e954c1d2f3d3c1d152ca63016172e74abce9d0a0`。
旧 v197 发布报告在 replay/sim/hil/real 四模式均与当前核心失配，原报告和 v196 重算证据保持不变；
32 项是旧报告的历史诊断数量，不代表新核心完整门诊断。当前 E7 尚未重跑，仍禁止正式发布。
见 [当前与历史 parity 比较](evidence/release-parity-drift-20260908.json)。下方待绑定描述为历史状态。
本轮 parity/进度/发布门相关测试 120 passed、0 skipped、0 failed（7.465s），Ruff 与 diff 检查通过；未重跑全量。

## 当前开发方向：现有模型增量扩展

- 用户要求在模拟环境训练自主任务决策与航线策略，SIM/HIL/REAL 共用同一候选模型组合。
- 在现有感知模型和共享 planner 基础上增加功能，保留旧接口与能力，不另建平行系统。
- 权重微调须核验并记录父 checkpoint 身份；现有感知权重不等于已有学习型航线权重。
- 下一步增加任务排序、航线选择及重规划建议，旧功能回归和新增收益分别验收。
- 已增加离线共享核心 API `select_next_task`：复用原 planner 校验/估算/计划，按优先级、耗时、距离、ID
  确定性排序，保留不可行候选拒绝原因；不修改原有 `plan_task` 或感知权重。
- planner 回归 32 项通过（含 7 项新增选择器测试）；尚未训练、接入 UI/模拟循环或绑定新增模块到发布 parity。
  返回的是独立完整任务的候选，不是可串接执行的多任务航程；历史实机与发布门状态不变。
- 详细约束见 [同一策略模型训练](sim-policy-training.md)。

### 增量预算模拟循环

- 最新 e 轮外部验证通过：3 帧、340 条旁路事件，116.234s completed，1.140s 消息差异在 1.5s 内确认。
  容器清理并确认不存在；飞后电量 72%，剩余续航缺失，下一任务继续关闭。
- 只读模拟电池参数为 SIM_BAT_DRAIN=60、SIM_BAT_MIN_PCT=50；未核验单位/动力学，未改配置。
  先前 50% 尾部记录不能当固定电量或真实能耗标定；见 [e 轮证据](evidence/freshness-confirmation-20260908-e.json)。
  单轮成功尚未完成多场景、连续任务、训练、发布 parity 或实机验收。以下为历史阶段记录。

- d 轮新外部任务门通过，但原 250ms 旁路等待超时：current 晚约 406ms；原始报告保留在
  [d 轮证据](evidence/order-confirmation-20260908-d.json)，容器清理成功，剩余续航仍缺失。
- 已移除单次样本推定的 250ms，改为复用现有反馈时效 1.5s，确认必须在该预算内到达，重复不延时；
  c/d 实际记录重放均 completed，新增 406ms/1.49s 边界场景，修改后相关 99 passed。
- 调整前全量 3862 passed / 7 skipped / 0 failed，216.351s；调整后未重跑全量，见
  [时效确认重放](evidence/confirmation-freshness-replay-20260908.json)。最新实现仍待新外部运行，未开启连续任务。

- 已为结果监控层增加有界确认：只暂存比当前项大 1 的同源到达消息，最多 1 条，250ms 内必须由 current
  确认；重复不延时，远跳/错源/超时仍拒绝。原严格跟踪器不变，收到确认后用处理时刻递交，
  `reconciliations` 分别记录原接收和确认时刻，不覆盖原始事件。
- 用 c 轮 285 条实际事件离线重放，31ms 顺序差异确认后最终 completed（101.141 秒）；新增 4 项测试，
  反馈与适配回归 74 passed。新外部运行仍待验证；续航来源缺口尚未解决，不能开启连续任务。
- 证据见 [消息顺序重放](evidence/mission-order-replay-20260908.json)，原 c 轮失败报告未改。

- 最新外部 c 轮（2026-09-08）：任务/采集门通过、容器清理；后任务电量 50%，五类消息同源且新鲜，
  但 BATTERY_STATUS 没有有效 time_remaining，禁止用 maximum_duration 替代剩余续航。
- 严格旁路本轮 failed：31.016 秒收到 reached=1 时最新 current=0，31 毫秒后才收到 current=2。
  上一轮 completed 仍是历史事实，但尚不能认定旁路稳定；多任务执行继续等待有界时序处理和续航来源。
- 新增电池/GPS 时间戳与来源、后任务观测导出；6 项新增回归，相关测试共 227 passed。
  详见 [后任务输入实测](evidence/postflight-observation-20260908.json)，未声称训练或连续任务完成。

- 新增共享 `run_observed_task_session`：现有选择/编译 → 后端回调供给监控器消息 → completed 加新鲜快照 →
  重选下一任务。下一快照来自后端，不使用预算能耗推演。回调仍负责原有事务/许可，循环不授予启动权限。
- 新增 7 项测试，mission planning 共 23 passed：双任务绑定新快照；提前返回、超时、缺快照、过期和
  机型身份变化均停止后续任务。双时钟域检查防止不同运行时快照混用；失败后保留未完成 ID。
- 当前为回调式循环集成测试，外部 Gazebo 多任务后端尚未接入；单任务外部证据不等同多任务闭环。

- 外部无界面 VTOL 模拟已验证（2026-09-08）：第二次任务与采集门通过，3 帧保存，旁路 269 条事件
  判定 completed，耗时 86.421 秒；由原报告重放，逐事件接受结果和最终 outcome 完全一致。
- 首次运行被旧像素门仅接受 manifest v1 拦下，已兼容生产器 v2 的共同像素字段，未知/非整数版本拒绝。
  162 项采集测试通过；两次容器均清理，未启动 QGC 或连接实机。原失败证据保留。
- 文件身份与命令见 [外部模拟旁路摘要](evidence/sim-outcome-shadow-20260908.json)。
  下方“待外部运行”为历史状态；当前待任务循环消费、策略训练及新模块 parity/总发布门接入。

- VTOL 外部模拟适配脚本的接收循环已挂接只读旁路监控，并传入实际编译 Mission；总报告新增
  `shared_outcome_shadow`，保存消息字段/接收时间/接受情况/结果。旧验收与控制指令不变。
- 28 项 VTOL 适配回归通过：旧缓存状态成功、原消息缺字段时，新判定仍失败，不继承旧成功。
  尚未启动新 Gazebo 任务；下方“接收循环未接入”为历史进度，当前待外部运行验证及任务循环消费。

- 原始 MAVLink 生命周期适配已加入结果监控器，严格解析两类消息、分别保留时戳并校验同源；
  末项到达前缓存被清除，旧半条状态不会被新消息刷新。新增 11 项，反馈回归累计 36 passed，
  含 pymavlink 编码/解码。外部 Gazebo 接收循环尚未接入，不能认定网络或任务执行已验收。

- 新增共享只读 `MissionOutcomeMonitor`：沿用 PX4 反馈跟踪器，末项到达加后续同源新鲜落地/解除武装才完成；
  超时/失败锁定，旧落地状态与迟到消息不计成功。新增 10 项测试，反馈模块 25 项通过。
  API 尚未接入 Gazebo 或替换预算推进；没有外部模拟执行新证据。

- 新增 `simulate_task_budget_episode`：每轮复用选择器和既有 PX4 Mission 编译器，以 ManualClock
  推进预计耗时、扣减续航与电量，返航后用新快照重选；电量不自动恢复，资源不足时停止。
- 返回每步原始/新快照、编译结果及最终拒绝原因；三机型测试验证计划绑定与多轮状态，新增 5 项测试，
  mission planning 回归 16 项通过。初始上下文不修改；错误编译输入拒绝。
- 这是独立 RETURN 任务的预算预测，非 Gazebo 动力学/执行验收；耗电是未标定恒定率近似。
  未训练模型、未连接实机；UI、模型加载、新模块发布 parity 绑定及新总门重算仍待完成。

## v198 现场输入可用性实测

- 当前控制机只读预检返回 1：Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机
  `192.168.144.108:554` 各一次 0.5 秒探测均超时；路由选择源地址 `192.168.1.13`，不在要求的
  `192.168.144.0/24`。本机没有该子网地址，串口枚举为空；不能推断设备关机，只能确认本轮不可用。
- 本地 14580/14581 UDP 可绑定，只说明端口可用，不说明远端已连接；未改地址/路由、未发送飞控命令。
- 当前 E7 replay 集只有 30 帧、精度/召回率未定义、必要正向场景缺少真值与预测。`artifacts/**/*.json`
  按 attestation/campaign.plan/field.replay/deployment.replay 文件名检查未找到匹配项；不是对其他目录或
  其他命名的真实录制做穷尽性断言。
- 实测命令、报告哈希及缺失输入写入 [现场可用性摘要](evidence/field-readiness-v198.json)。下一步需要
  接入实际现场网络/设备，或提供已有六分层真实录制及来源证明；人工视觉和 HIL/REAL 门仍需真人/设备。
- 本轮没有新 E7 总门重算，顶层 32 条阻断仍对应 v197 报告。不得把重复软件回归代替缺失的现场输入。

## v197 当前四模式 E7 集成

- 使用 `create_release_mode_evidence.py` 从实际源文件重新生成四模式 envelope，再通过
  `run_current_release_gate_pre8.ps1` / manifest writer 集成 v196 四份新报告；未手改 gate/status。
- SIM envelope 改为与 combined gate 相同的 20260905T175019Z-64536 源记录，不再沿用旧的 20260902 fallback。
  HIL 的软件验收来源及 REAL 未通过的 preflight 仍不等于实机验收，各模式人工/Mission/实机状态均未提升。
- 新 manifest 的四模式五组 parity 与当前源码一致，strict 退出码 2；发布仍为 `false / false`。
  阻断诊断从 45 条降到 32 条，消除的是过期派生报告的诊断，不是完成了 13 项现场验收。
- [当前审计摘要](evidence/release-gate-audit-v197.json) 保存完整原因、模式状态和 manifest/report/strict 身份。
  当前报告为 73,395 bytes / SHA-256 `AC58C3A9ADB27630337C030960DBA436035CABAE967DE38B9EC3E641CBF09397`。
- 下方 v195/v196 的 pending、45 条和旧 parity 描述为历史状态；当前以本节及 JSON 顶层状态为准。
- 新产物位于 `artifacts/evaluation/terminal-continuation-v197/`。未启动模拟器或连接实机，未重建 EXE；
  下一步仍需取得六分层 replay 精度/覆盖、当前现场链路、四模式人工视觉和 HIL/REAL Mission 验收证据。
- 回归 101 passed、0 skipped/failed（8.106 秒），lint/format/diff 检查通过；JUnit 身份见
  `validation.v197_e7_integration`。精确生成命令保存在审计摘要的 `commands`；重跑时须换新输出目录/Stamp。

## v196 历史 SIM 原始记录重算

- 从 v176 报告绑定的原始文件重新计算三机型位置响应矩阵（3 条）、响应 campaign（6 条正向、9 条故障）
  和 external runtime/AI join（3 机型各 2 份源记录），生成器及独立复核入口全部返回 0。
- 原始记录的当前 SHA-256 与历史绑定一致；没有覆盖旧报告，没有通过直接替换哈希生成新结论。
- 新输出目录为 `artifacts/evaluation/terminal-continuation-v196/`。四份输出的大小/哈希、源文件清单及
  当前 parity 均写入 [可移植重算摘要](evidence/sim-recomputation-v196.json)。
- 这是离线源记录重算，不是新 Docker/Gazebo 或 HIL/REAL 运行。尚未把四份新报告集成到完整 E7 manifest；
  `release_status` 保留 v195 实测的 45 条阻断，不提前扣减。
- 下一步生成当前四模式 evidence envelope，并用 manifest writer 绑定新报告、保留未验收状态后重跑 E7。
- 回归 `29 passed / 0 skipped / 0 failed`，lint/format/diff 检查通过；JUnit 命令、大小与哈希见
  `validation.v196_sim_recomputation`。运行时代码未改，没有重建 EXE或重跑全量回归。

重算入口（源路径取 v176 报告；输出必须使用新的目录，不覆盖历史产物）：

- matrix：`python scripts/run_px4_reported_position_response_matrix.py --source <每个 entries.source_path> --release-parity configs/release-parity.json --out <新矩阵路径>`，重复三次 `--source`。
- campaign：`python scripts/run_px4_reported_position_response_campaign.py --positive <每个 positive_runs.source_path> --fault <每个 fault_runs.source_path> --release-parity configs/release-parity.json --out <新 campaign 路径>`，正向六项、故障九项。
- external：`python scripts/verify_simulator_external_evidence.py --vtol-runtime <路径> --vtol-ai <路径> --fixed-wing-runtime <路径> --fixed-wing-ai <路径> --multirotor-runtime <路径> --multirotor-ai <路径> --parity-json configs/release-parity.json --out <新 join 路径>`。
- 独立复核：分别运行 `scripts/verify_px4_reported_position_response_matrix.py <新矩阵路径>`、
  `scripts/verify_px4_reported_position_response_campaign.py <新 campaign 路径>`，以及
  `scripts/verify_simulator_external_evidence.py --join <新 join 路径> --expected-release-label 0.3.0-pre.8 --out <新 revalidation 路径>`，入口均使用 Python。

## v195 发布门当前证据审计

- 用当前 evaluator 重算最新保存的 v176 release manifest：exit 2，`gate_passed=false`、
  `release_allowed=false`，45 条阻断诊断。该数量不是 45 个独立任务；原先固定的 31 已不代表当前状态。
- 四种模式彼此一致的历史 parity 不等于当前源码身份：task contract、safety policy、PX4 Mission path
  三组哈希均已过期。当前报告明确标记 `current_release_parity_bound=false`，不重写历史输入。
- 主要缺口：deployment replay 精度/场景覆盖、现场链路超时和未就绪、PX4 response matrix/campaign
  与 external SIM join 派生证据过期、四模式人工视觉与 Mission 验收、HIL/REAL 实机控制验收。
  prerelease/version 阻断是预期行为，不能提前改为 release 来消除诊断。
- 可移植的诊断摘要已进入 [发布门审计证据](evidence/release-gate-audit-v195.json)，含完整 45 条原因、
  当前 parity、逐模式差异、执行命令和原报告字节身份。原报告 76,323 bytes / SHA-256
  `8F422BB3275103422CFD34B8FC7B2C033F0CB3B4D193C1A03CC7D1223F61FDD0`。
- 进度测试改为校验实际审计摘要，不再写死阻断数量；原报告存在时还检查其哈希、原因和逐模式差异。
- 下一步先从原始绑定记录重新生成过期的 SIM 派生证据，再重跑 E7；真实现场与人工门仍需独立验收。
- 本轮发布门、manifest、parity 与进度测试 `114 passed / 0 skipped / 0 failed`（8.703 秒），lint、format、
  diff 检查通过；JUnit 身份见 `validation.v195_release_gate_audit`。本轮只改审计文档和进度测试，
  没有修改运行时代码；下方 v194 全量结果按原迭代保留，不冒充本轮新跑的全量回归。

## v194 PX4 receipt 跨目录复核

- 已修复 v193 验证器依赖原采集绝对路径的缺陷：仅将历史路径用于角色/统一来源根目录校验，
  然后从接收目录打开相对路径。整包搬运无需重写 summary 或 receipt。
- 聚焦回归 `30 passed / 0 skipped / 0 failed`，覆盖原目录离线、六角色目标文件缺失/等长篡改、
  Windows/POSIX 历史路径、模式与来源/角色错配。证据文件、哈希和命令见 `validation.v194_receipt_relocation`。
- 当前 parity SHA-256 为 `DEEB60230395BE3E4B8F63F5511A014FB6A1D2F48C81D0450C7A677B41D94C8F`；
  PX4 Mission path hash 为 `105F5B3DB4638211E1A4FEBCC59127A936C58048C433C8C82DB2235DBF1A99C8`。
  下方 v193 及更早的哈希/测试保留为历史，不代表本轮源码。
- 操作步骤见 [PX4 采集包搬运](px4-capture-receipt-transfer.md)。回执证明字节完整性和摘要声明，
  不证明作者真实性，也不重跑源文件语义 join。现场 HIL/REAL、飞行与最终发布门仍未通过。
- 文档 CLI 已用生产 capture 入口加内存 provider 生成的 sidecar 整包复制复核：exit 0、六源文件通过、
  所有 acceptance claims 为 false。回执为 1,924 bytes / SHA-256
  `514F2A48CF5BF48A61B93B2016BA6D9A134662167DBA035AE9BF6C4F8CE575C8`；命令与路径见
  `validation.v194_receipt_transfer_cli`，没有实际连接 Pixhawk。
- 纠正原 `working_base` 中不准确的 v193 commit 字符串，本轮基线以实际 HEAD
  `e0a6562cc8c5493c252edfc2b46350e5e3ed749b` 记录。
- 全量回归：`3,805 passed / 7 skipped / 0 failed / 0 errors`，3,812 tests，215.535 秒；7 项为
  当前环境未执行的符号链接相关测试，不能计为通过。JUnit：
  `artifacts/evaluation/terminal-continuation-v194/full.xml`，558,681 bytes / SHA-256
  `EB97D3637E83FAEF0C23F5BAAB33DABAAEAA859F74E5EA6D1082C84EF23BF7D3`。
  `ruff check`、`ruff format --check`（1,577 files）、`compileall` 均通过。没有修改 C++/QML，没有重建 EXE。

## v193 本轮已收口（PX4 capture receipt 与现场搬运闭包）

- `multidetect.px4_runtime_capture` 新增可选 `--capture-summary-out`：采集成功后以原子写入保存原始
  `px4-runtime-capture` 摘要；摘要不含密钥，也不改变 receive-only provider、Mission feedback 或 join 语义。
- 新增 [`src/multidetect/px4_runtime_capture_receipt.py`](../src/multidetect/px4_runtime_capture_receipt.py)、
  `multidetect-px4-runtime-capture-receipt` 和 `-verify`，以及
  [`configs/schemas/px4-runtime-capture-receipt.schema.json`](../configs/schemas/px4-runtime-capture-receipt.schema.json)。
  receipt 固定列出六个角色：Mission、RuntimeObservation、RuntimeProfile、Mission feedback、
  endpoint/vehicle identity、live join；同时记录 capture summary 的相对路径、字节数和 SHA-256。
- builder/verify 会重开同一 `base_directory`，拒绝符号链接、越界/重复/碰撞路径、摘要或任一源文件篡改，
  且只接受 completed capture、feedback/identity/join 全部通过和五项 acceptance claim 全为 false。
  产物是可搬运的软件证据闭包，不升级真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、飞行或最终发布门。
- [`scripts/run_px4_runtime_capture.ps1`](../scripts/run_px4_runtime_capture.ps1) 现在在 capture 返回零后自动生成
  `capture-summary.json` 与 `capture-receipt.json`；capture 失败时不继续生成 receipt，并原样传播 native exit code。
- v193 聚焦回归为 `39 passed / 0 skipped / 0 failed`，全量回归为 `3,787 passed / 7 skipped / 0 failed`
  （3,794 tests，203.18 秒）。焦点 JUnit 为
  `artifacts/evaluation/terminal-continuation-v193/px4-runtime-capture-receipt-focused-v193.xml`，5,533 bytes /
  SHA-256 `5819FABFE6CC8E01B5DD127F6CD45C41747B181AF83F3AF26C36138D634F7A16`；全量 JUnit 为
  `artifacts/evaluation/terminal-continuation-v193/full-rerun-v193.xml`，555,788 bytes /
  SHA-256 `C06571E0CBEC31D1B8065156A4F7B1751065E1646C88A68047A8E828CD0FE884`。当前 parity 已刷新为
  10,811 bytes / SHA-256 `5E7DAB745DB86193CB43DEE186ADB818214414449B6A70C50D498009AF5F621F`，
  PX4 Mission path hash 为 `F2DEB4CB83D50C1C70F06556DC6C7AD58344CBEEB12FB7ED9EE00FA3CD88EDCE`。

## v192 本轮已收口（Windows field capture wrapper）

- 新增 [`scripts/run_px4_runtime_capture.ps1`](../scripts/run_px4_runtime_capture.ps1)：从脚本自身解析仓库根目录，
  自动选择 `ground-control/MultiDetectGCS/.venv`、仓库 `.venv` 或 PATH 中的 Python，并调用同一个
  `multidetect.px4_runtime_capture` 模块；现场不需要手抄长命令或依赖当前工作目录。
- wrapper 完整转发 `hil|real`、串口/UDP endpoint、baud、system/component ID、stale/duration/poll 边界、
  `AUTOPILOT_VERSION`/ranging 请求、三个 sidecar 输出路径和 `--overwrite`。REAL 必须给 signing identity
  hash 或 signing-key 环境变量名，HIL 明确拒绝两者；HMAC/signing 原文只由 Python 子进程读取环境变量，
  不进入 PowerShell 参数列表，也不改变网卡、路由或飞控状态。
- 增加 PowerShell parser、转发契约、密钥边界、模式 fail-closed 和 parity 断言；wrapper 与 capture 模块、
  schema、确定性 generator 一起纳入 PX4 Mission path hash。当前 v192 的现场入口仍只生成 sidecar 和软件
  join，不自动声称真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、飞行或最终 `v0.3.0` 通过。
- v192 聚焦回归为 `97 tests / 96 passed / 1 skipped / 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v192/px4-runtime-capture-wrapper-focused-v192.xml`，15,216 bytes /
  SHA-256 `D4C68D5C6139E4B0920FD911DDB39EDFD6E6CF78B08E712C0727A1C1CDA9B63C`；全量回归为
  `3,783 passed / 7 skipped / 0 failed`（3,790 tests，197.147 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v192/full-rerun-v192-final.xml`，555,245 bytes /
  SHA-256 `2C48EB74343AF0A8E5F7358CC1B94297426078FA5B8C51603E520A1B159A2BF9`。docs/parity、ruff、
  format、compileall、PowerShell parser 与 diff check 均通过。wrapper 变更后的当前 parity 为 10,690 bytes /
  SHA-256 `C4B0272564FE365AC95497D8D339C190549909EC37D4F20E4F3F5165BB4911F9`，PX4 Mission path hash 为
  `DA5C8EDF57A2F8A80867DEE4B85B4238916F563D171FCF1A0DC674CDB99B3AF8`。
- 现场调用模板（在仓库根目录 PowerShell）：
  `& .\scripts\run_px4_runtime_capture.ps1 -BaseDirectory <capture-root> -Mission mission.json
  -RuntimeObservation runtime-observation.json -RuntimeProfile signed-profile.json -RuntimeMode hil
  -Endpoint <serial-or-udp-endpoint> -RuntimeProfileKeyId <KEY_ID>`；先在当前进程设置
  `$env:MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY`，不要把密钥写进命令历史。

## v191 本轮已收口（PX4 receive-only runtime sidecar capture）

- 新增 [`src/multidetect/px4_runtime_capture.py`](../src/multidetect/px4_runtime_capture.py) 与安装式命令
  `multidetect-px4-runtime-capture`：在同一个 bounded Pixhawk read-only provider session 中采集 v2
  `PX4 Mission feedback`、endpoint/vehicle identity sidecar，并立即重开并重跑 v190 的 live observation /
  Mission / signed RuntimeProfile 五源 join。输入和输出均要求位于同一 `--base-directory`，拒绝符号链接、越界
  路径、输入/输出复用和 profile/runtime mode 漂移；所有 JSON 原子写入。
- 传输计数从兼容性的 `messages_transmitted` 总数拆为
  `telemetry_request_messages_transmitted`、`application_messages_transmitted` 和
  `flight_control_messages_transmitted`。`AUTOPILOT_VERSION` 等有界身份请求可被记录而不会被误判为飞控输出；
  任一应用或飞控输出仍会让 feedback/profile/join 门 fail-closed。旧 v1/v2 报告没有分桶字段时继续按保守规则
  将非零总数视为应用输出。
- `--request-autopilot-version`（默认开启）让现场采集可拿到完整 PX4 firmware identity；REAL 模式仍要求调用者
  通过环境变量或内存参数提供 signing identity hash，HMAC/profile key 只从环境或调用者内存读取，绝不写入报告。
  现场入口示例：
  `multidetect-px4-runtime-capture --base-directory <capture-root> --mission <mission.json>
  --runtime-observation <live-observation.json> --runtime-profile <signed-profile.json> --runtime-mode hil
  --endpoint <serial-or-udp-endpoint> --runtime-profile-hmac-key-env <KEY_ENV>
  --runtime-profile-key-id <KEY_ID> --request-autopilot-version`。
- 新增 [`configs/schemas/px4-runtime-capture.schema.json`](../configs/schemas/px4-runtime-capture.schema.json)
  和 [`scripts/generate_px4_runtime_capture_evidence.py`](../scripts/generate_px4_runtime_capture_evidence.py)。
  v191 确定性证据在 `artifacts/evaluation/terminal-continuation-v191/summary-v191.json`：正向 receive-only capture
  的 feedback/join/schema 均通过，telemetry 请求 `2`、应用输出 `0`、飞控输出 `0`；负向应用输出样本被 feedback
  与 join 同时拒绝。该证据使用 bounded in-memory provider，不连接真实 Pixhawk、串口、GNSS 或飞机。
- v191 聚焦回归为 `54 tests / 53 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v191/px4-runtime-capture-focused-v191.xml`，9,118 bytes / SHA-256
  `CB77D5255B3708B600B63339D876BAE0DB9127E0A34F0CBB96D9BA6813A04307`。`ruff check`、`ruff format --check`、
  `compileall` 与 `git diff --check` 均通过；完整回归为 `3,779 passed / 7 skipped / 0 failed`
  （3,786 tests，187.779 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v191/full-rerun-v191-final.xml`，554,650 bytes / SHA-256
  `CD69D10146C6A361CF25DDD5905A13F9AFB640DA44999F36E8861A993322DBA9`。当前 parity 为 10,645 bytes / SHA-256
  `7BB1D0252C93A5177E0380E219CB285F2FF123746FEE71DB358399B657840787`，PX4 Mission path hash 为
  `1758A3A6AC52C01260E432ADC005E5AC9993978359F7243A281A02645B98DD97`。
- v191 当时只关闭“现场采集入口缺失”和“telemetry 请求误判为输出”这两个软件缺口；不把合成 provider 升级为真实
  HIL/REAL、室外 GNSS、人工 Fly View、飞行或最终 `v0.3.0` 发布通过。下一步是把现场生成的 sidecar 原样带回，
  用同一 CLI 重跑 join，再执行三机型 Mission feedback、GNSS/测距和人工视觉验收门。

## v190 本轮已收口（live RuntimeObservation × PX4 Mission × RuntimeProfile × endpoint/vehicle identity join）

- 新增 [`src/multidetect/live_runtime_observation_mission_binding.py`](../src/multidetect/live_runtime_observation_mission_binding.py)：
  以同一 `base_directory` 显式重开并哈希 live `RuntimeObservationResult`（含嵌套
  `RuntimeRunReport`）、可执行 `PX4 Mission`、v2 `PX4 Mission feedback`、签名 `RuntimeProfile` 和
  `runtime-endpoint-vehicle-identity` sidecar；拒绝符号链接、越界路径、空/超大/重复 source、模式/哈希/
  heartbeat/endpoint/airframe 漂移和任一输出解锁。HIL/REAL 的 profile HMAC 只从调用者内存或环境变量读取，
  报告不落密钥；身份 sidecar 固定 receive-only、无飞控输出、`output_locked=true`。
- 新增安装式命令 `multidetect-live-runtime-observation-mission-bind`，成功和错误输出分别由
  [`live-runtime-observation-mission-binding.schema.json`](../configs/schemas/live-runtime-observation-mission-binding.schema.json)
  约束；endpoint/vehicle 身份 sidecar 由
  [`runtime-endpoint-vehicle-identity.schema.json`](../configs/schemas/runtime-endpoint-vehicle-identity.schema.json)
  约束。`src/multidetect/release_parity.py` 已将 joiner、证据生成器和两个 schema 纳入 PX4 Mission path。
- [`scripts/generate_live_runtime_observation_mission_binding_evidence.py`](../scripts/generate_live_runtime_observation_mission_binding_evidence.py)
  用真实 `LiveMissionRunner.from_runtime_backend` 两帧 bounded HIL producer、同版签名 profile、可执行 Mission、
  v2 feedback 和 identity sidecar 生成可重开软件证据：join 报告 6,340 bytes / SHA-256
  `CDF38A290AF39986BA64DB4791349693D7F02B1E88F351951711386C54951C42`，25 项检查全部通过，schema errors `0`；
  负向模式篡改被拒绝（identity sidecar 的 REAL signing identity 缺失、模式不一致和 output lock 均关闭 gate）。
  五个 join source 为 live observation 1,403 bytes / `1659D846E6AA3C5AD0441FB1772202DBCE6247C06D994A16E3765C4E60138BE0`、Mission 795 bytes /
  `9FA908D84C5C9BF85330C5CC4AF983CEFE133B5FF2837C48EE243FD19DE5F70C`、feedback 4,254 bytes /
  `06D613785990F03E16D9BEB0FF254EBA34D45C2B292500608E66B36F91B76815`、profile 727 bytes /
  `00969A4FE6DF6ACB66C2F20BFD02AE4B81295B920A0D129775665B380E7BC416` 和 identity 1,470 bytes /
  `4F51C73A24E899D5B0296B59BD4E26B5081FAE94B20CA2A83BD8F87F2DDF82C2`；live 内嵌 RuntimeReport 536 bytes /
  `4D74129933FDA1086361A56438496221741C17637BB54E0FA174DC63EA02FA3` 作为同源嵌套证据。
- v190 定向回归为 `145 passed / 2 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v190/live-runtime-observation-mission-binding-focused-v190.xml`，
  20,037 bytes / SHA-256 `3B029752BCDAB4AFC4F63BCDC84A5C348A0393D671B16C82CDEC7704502B38D3`。
- 全量 Python 回归为 `3,770 passed / 7 skipped / 0 failed`（3,777 tests，180.87 秒）；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v190/full-rerun-v190-final.xml`，553,421 bytes / SHA-256
  `66FA6C5AA8CE96F3BC1755D452071AB9C206DDC9DA16F5C33DCB7BECB7B3A95E`。
  v190 evidence 当时绑定的 `configs/release-parity.json` 为 10,482 bytes / SHA-256
  `98177D06A88565D5FFDCE97F265CFF573CCC1256F5DE4A64C737DAEE72BE8435`，PX4 Mission path hash 为
  `7C4DDE6A78ECE45886F72AD8F28815B275C2614089C109FCEFEE095AD894EA6D`。
  本轮只证明 software sidecar join、schema、hash/身份/锁输出和 fail-closed 逻辑；不把合成 HIL 证据升级为
  真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、飞行或最终 `v0.3.0` 发布通过。下一步是用真实设备采集
  同版 profile/Mission/feedback/endpoint sidecar 并重跑此 join，再继续执行现场验收门。

## v189 本轮已收口（profile-bound live RuntimeObservation producer）

- `LiveMissionRunner.from_runtime_backend` 现在直接拥有 live producer 的完成报告和类型化观测
  结果入口：`runtime_run_report` 记录同一 descriptor/input hash、实际处理帧 ID、共享时钟起止时间、
  `stream_exhausted` 与 `max_frames`；`build_runtime_observation_result()` 在 HIL/REAL 模式下从该报告
  派生 canonical receive-only result。无界 live 流被用户中止时不伪造终止原因，报告保持不可用。
- `multidetect live-camera` 增加 `--runtime-report`、`--runtime-observation-result` 和
  `--runtime-observation-include-hash`。输出要求签名 profile-bound HIL/REAL runtime；先构造并校验
  observation，再原子写入 result/report，避免安全门失败留下孤立 sidecar。
- live producer 暴露分离后的计数：`messages_transmitted` 是兼容性的总数，
  `telemetry_request_messages_transmitted` 单列 telemetry/autopilot/stream 请求，
  `application_messages_transmitted` 与 `flight_control_messages_transmitted` 单列应用/飞控输出。
  Pixhawk read-only provider 的 telemetry 请求不会被误报为零发送；观测结果构造器拒绝任何已知应用/飞控
  输出、可控硬件或飞控能力开启的 provider。
- 新增 [`scripts/generate_runtime_observation_live_producer_evidence.py`](../scripts/generate_runtime_observation_live_producer_evidence.py)：
  用真实 `LiveMissionRunner.from_runtime_backend` 路径生成 HIL 软件 pair。结果 1,419 bytes / SHA-256
  `2FADB5B4D6A5E041DBE35430EE03E4CB7549955B2F757F50F9FE243A87DBC01B`；RuntimeReport 544 bytes /
  SHA-256 `F4D7B8E2E61AA2B7972F38BBEBA9F8857424116316F7DFB83565B5CDDCB5CFE2`；schema errors `0`、
  round-trip `true`、binding `gate_passed=true`，负向应用输出 guard 按预期拒绝。Telemetry 总数为 `2`，
  其中请求 `2`、应用 `0`、飞控 `0`，`read_only=true`、`output_locked=true`。
- 聚焦回归为 `307 tests / 306 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v189/runtime-observation-live-producer-focused-v189.xml`，
  41,264 bytes、SHA-256 `07BF0AEE56F35D75389D310C9A36ACDDD676CC114055E6E6CA043E48A496160A`。
  全量回归为 `3,770 tests / 3,764 passed / 6 skipped / 0 failed`，365.571 秒；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v189/full-rerun-v189-final.xml`，552,187 bytes、SHA-256
  `691416301B89CBDFBBC91EC3617AE67AB9B37B07242C68CD3241684A51D6922A`。
- 本轮已更新 release parity（PX4 Mission path 与 safety policy hash）并提交实现；仍未重编译 portable EXE。
  这些是 software runtime identity/安全边界证据，不代表真实 Pixhawk、室外 GNSS、HIL、REAL、人工 QGC
  或实飞验收；最终 `v0.3.0` 继续保持阻断。下一步是把 live 结果与同版 RuntimeProfile、PX4 Mission
  feedback、endpoint/vehicle identity sidecar 做显式 join。

## v188 本轮已收口（RuntimeObservation builder CLI）

- 新增安装式命令 `multidetect-runtime-observation-build`（实现入口
  `src/multidetect/runtime_observation.py:main`）：输入现有 `RuntimeRunReport` sidecar、指定
  `--runtime-mode hil|real` 和 `--out`，即可生成与 sidecar 同源的 canonical receive-only result；
  `--include-hash` 可额外写入 result `sha256`，不会改写原 sidecar。
- CLI 对错误路径保持非零退出，并通过同一 `load_runtime_observation_result` 严格重开；它只构造
  `RuntimeObservationResult`，不连接 Pixhawk、不发送 telemetry/control 消息，也不把 covariance
  capture 的 telemetry request 计数转换成零发送声明。
- 新增 [`scripts/generate_runtime_observation_cli_evidence.py`](../scripts/generate_runtime_observation_cli_evidence.py)：
  v188 HIL/REAL 两个 CLI pair schema errors 均为 `0`、重开为 `true`、binding `gate_passed=true`。
  HIL result 1,392 bytes / SHA-256 `C344CDB4A31A71841AA512A69CE40DDBC4092BEC96BFFE8A601485B00B27535D`；
  REAL result 1,398 bytes / SHA-256 `EB6E7A23F86108696BD0BC7826D0C4BB4A07C49A5B75D74490F30F25FE4069A4`。
- 定向 `tests/test_runtime_observation.py tests/test_runtime_report_binding.py tests/test_runtime_report_binding_set.py`
  为 `39 tests / 38 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v188/runtime-observation-cli-focused-v188.xml`，6,274 bytes、SHA-256
  `F82133782267F4556DF51EFC5E742C5871740C36A2331CF12F98DCDBC0C53DFD`。本轮仍不改变 portable EXE、
  硬件、室外 GNSS、真实 HIL/REAL、人工 Fly View、实飞或最终 `v0.3.0` 发布状态。

## v187 本轮已收口（typed RuntimeObservationResult 生产入口）

- 新增 [`src/multidetect/runtime_observation.py`](../src/multidetect/runtime_observation.py)：以同一份
  `RuntimeRunReport` 为唯一来源，类型化生成 `hil_runtime_observation_finished` /
  `real_runtime_observation_finished` 结果；descriptor/input hash、处理帧 ID、数量、终止字段和内嵌
  canonical report 不再由现场 producer 手工复制。`write_runtime_observation_pair` 同时写 result 与
  sidecar，`load_runtime_observation_result` 会严格重开并拒绝身份、报告或安全字段漂移。
- receive-only 字段由 builder 固定为 `observation_only/read_only/receive_only=true`、
  `messages_transmitted=0`、`flight_control_enabled=false`、`hardware_outputs_present=false`、
  `output_locked=true`；明确区分 telemetry-request capture 与零应用发送的观测结果，避免把现有
  Pixhawk covariance 采集器的 telemetry 请求数误记成控制输出或反向冒充现场验收。
- 新增 [`configs/schemas/runtime-observation-result.schema.json`](../configs/schemas/runtime-observation-result.schema.json)、
  [`scripts/generate_runtime_observation_builder_evidence.py`](../scripts/generate_runtime_observation_builder_evidence.py)
  和 `tests/test_runtime_observation.py`。v187 HIL/REAL 两个软件 pair 均 schema errors `0`、binding `gate_passed=true`；
  篡改 `runtime_input_hash` 的负向 pair `gate_passed=false`，失败检查为 `input_hash_matches`。证据摘要位于
  `artifacts/evaluation/terminal-continuation-v187/summary-v187.json`。
- 定向 `tests/test_runtime_observation.py tests/test_runtime_report_binding.py tests/test_runtime_report_binding_set.py`
  为 `38 tests / 37 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v187/runtime-observation-focused-v187.xml`，6,139 bytes、SHA-256
  `851CC04C0C75C00CE0C17815994FB1463D4EE455C82AD0DE5486A4552D2CC4DE`。本轮仍为 Python/schema/docs-only，
  全量回归为 `3,760 passed, 6 skipped, 0 failed`（3,766 tests，293.085 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v187/full-rerun-v187-final.xml`，551,664 bytes、SHA-256
  `9EC0FC399BCA4422390D2A5E5BD89ED08FB275E82286BF6503A53EFC76856BC5`。本轮仍为 Python/schema/docs-only，
  portable EXE 未重编译；硬件、室外 GNSS、真实 HIL/REAL、人工 Fly View、实飞和最终 `v0.3.0` 继续保持
  `not_claimed` / 阻断。

## v186 本轮已收口（HIL/REAL RuntimeReport 观测适配器）

- `runtime_report_binding` 新增两个严格事件适配器：`hil_runtime_observation_finished` 和
  `real_runtime_observation_finished`。它们按事件固定 runtime mode，重新读取 descriptor/input hash、
  processed frame count/IDs、bounded termination，并要求结果内嵌的 `RuntimeRunReport` 与 sidecar canonical
  mapping 完全一致；未知事件仍保持 fail-closed。
- HIL/REAL 观测结果必须显式声明 `observation_only/read_only/receive_only=true`、
  `messages_transmitted=0`、`flight_control_enabled=false`、`hardware_outputs_present=false`、
  `output_locked=true`。新增 `output_safety_contract` 单绑定检查并同步两个 binding schema；发送计数、
  事件/模式漂移、缺少 `max_frames` 或内嵌报告都会关闭 gate。
- 新增 [`scripts/generate_runtime_report_binding_observation_evidence.py`](../scripts/generate_runtime_report_binding_observation_evidence.py)，
  在 `artifacts/evaluation/terminal-continuation-v186/` 生成四模式 `4/4` 软件绑定、REPLAY/SIM 子集和
  `messages_transmitted=1` 负向报告：正向集合 14,785 bytes / SHA-256
  `34F3B877B6C6DC77A751C1064AB32EA2F5AA2AF0DA6A4AC5C5307946B4A7273D`，schema errors `0`；负向集合
  `gate_passed=false`、`output_safety_contract=false`，schema errors `0`。
- 定向 `tests/test_runtime_report_binding.py tests/test_runtime_report_binding_set.py` 为 `28 passed, 1 skipped, 0 failed`；JUnit
  位于 `artifacts/evaluation/terminal-continuation-v186/runtime-binding-focused-v186.xml`，4,929 bytes、SHA-256
  `6C89C4A23DE25B928D0B1F2C8059BC69CFE0F7EDD597CBDA10DFF63709344834`。全量回归为 `3,751 passed, 6 skipped, 0 failed`
  （3,757 tests，342.241 秒），JUnit 位于 `artifacts/evaluation/terminal-continuation-v186/full-rerun-v186-final.xml`，
  550,454 bytes、SHA-256 `F2EAC63E24F34B2A9B34A3D1F7C48E32636F9360FB6E997CCA7E1480F915A02B`。
- 本轮为 Python/schema/docs-only，未重编译 portable EXE。合成 HIL/REAL 只验证接口和安全合同，
  `hardware/HIL/REAL/flight/manual visual` 仍 `not_claimed`，E7 与最终 `v0.3.0` 继续阻断。

## v185 本轮已收口（RuntimeProfile → PX4 Mission feedback 六源绑定）

- 新增 [`src/multidetect/px4_mission_feedback_profile_binding.py`](../src/multidetect/px4_mission_feedback_profile_binding.py)
  与 `multidetect-px4-mission-feedback-profile-bind` CLI：显式重开 v184 RuntimeProfile sidecar 集合和
  `px4_mission_feedback_binding` 六源报告，按 `REPLAY → SIM → HIL → REAL` 模式策略对账，再逐叶检查
  `HIL/REAL × fixed-wing/multirotor/VTOL` 的 profile kind、签名 key、runtime binding、receive-only 与锁输出。
- 新增 [`px4-mission-feedback-profile-binding.schema.json`](../configs/schemas/px4-mission-feedback-profile-binding.schema.json)，
  成功与结构错误均有固定 schema；输出保留 profile set/feedback source bytes+SHA-256、模式策略、六个叶子结果、
  reasons 和 software-only claim boundary。profile set 的通用模式 sidecar 不被错误地当作六个空军架构文件，
  只绑定策略身份；六源 reviewer 继续负责每个 airframe 的实际 profile/mission/report 哈希与身份。
- 4 项定向回归覆盖六叶成功绑定、profile set 不完整、叶子 signer key 漂移的 fail-closed 以及 CLI 错误 schema；相关
  25 项回归为 `24 passed, 1 skipped`，清理后再次全量为 `3744 passed, 6 skipped, 0 failed`（3,750 tests，337.783 秒）。本轮仍不启动
  runtime、不连接 PX4、不改变 QGC C++/QML 和 E7，硬件、室外 GNSS、HIL/REAL、人工 Fly View 与实飞验收仍保持原门禁。

## v184 本轮已收口（RuntimeProfile sidecar 集合预检）

- 新增 [`src/multidetect/runtime_profile_binding_set.py`](../src/multidetect/runtime_profile_binding_set.py) 与
  `multidetect-runtime-profile-set-verify` CLI：显式重开每个 `REPLAY / SIM / HIL / REAL` 的 RuntimeProfile
  JSON sidecar，按固定模式顺序检查 profile kind、签名策略、`key_id`、观察到的 endpoint/vehicle/firmware/
  airframe/signing identity，以及 `flight_control_enabled=false` / `output_locked=true`。
- HIL/REAL 的 HMAC key 只从调用者内存或环境变量读取，不写入输出；路径必须在 `base_directory` 内且拒绝符号链接、
  空文件、重复 mode 和重复 profile source。集合门允许用 `required_modes` 做 REPLAY/SIM 软件子集预检，默认仍要求
  四模式；缺少 HIL/REAL 时 `complete=false`，不会将 profile 身份记录升级为现场、硬件、HIL/REAL 或飞行验收。
- 新增 `runtime-profile-binding-set.schema.json` 和 9 项定向回归；完整四模式合成 profile 仅证明软件签名/身份/锁输出
  契约，保留 [`px4_mission_feedback_binding.py`](../src/multidetect/px4_mission_feedback_binding.py) 的六源现场门与
  E7 `false / false`。定向为 `8 passed, 1 skipped, 0 failed`；全量为 `3740 passed, 6 skipped, 0 failed`
  （3,746 tests，317.402 秒），JUnit 位于 `artifacts/evaluation/terminal-continuation-v184/`。Python-only 改动未重编译
  portable EXE。

## v183 本轮已收口（多模式 RuntimeReportBinding 集合校验）

- 新增 [`src/multidetect/runtime_report_binding_set.py`](../src/multidetect/runtime_report_binding_set.py) 与
  `multidetect-runtime-report-set-verify` CLI：把每个运行模式的结果 JSON 与 `RuntimeRunReport` sidecar
  作为显式 pair 重新打开并绑定，固定 `REPLAY → SIM → HIL → REAL` 顺序，拒绝重复模式、跨模式复用、
  base directory 外路径、未知字段和任一单项错误。每条 pair 复用 13 项单绑定检查，集合层再检查模式覆盖、
  声明一致性、结果/报告路径唯一性、source 唯一性和全体通过条件。
- `configs/schemas/runtime-report-binding-set.schema.json` 同时约束成功与结构错误事件。v183 软件证据以
  2/2 `REPLAY + SIM` pair 通过；集合报告 8,035 bytes / SHA-256
  `52C6DAC94B3529C1C25782280D1A94DA6E17CC098AD7A494B0C73604B6B0144C`，schema errors 为 0；定向回归
  `8 passed, 1 skipped, 0 failed`（9 tests；Windows symlink 权限导致的预期 skip），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v183/runtime-binding-set-focused-v183.xml`，2,232 bytes / SHA-256
  `44A5EF5766847D1504CD3AAC69C7C5F0E93E6D81A2F9373149EFA27C6E235D9A`；全量 Python 回归为
  `3732 passed, 5 skipped, 0 failed`（3,737 tests，279.743 秒），JUnit 546,831 bytes / SHA-256
  `915357977330EF96CD990DA87AF134A1130997FA6F6204D9345AB798947C471B`。
- 默认四模式集合仍明确保持 `gate_passed=false`，缺少 `HIL` 与 `REAL`；集合校验只证明 software runtime
  identity，不把 Replay/SIM 证据冒充 HIL、REAL、硬件或飞行验收。Python-only 改动未重编译 portable EXE，
  当前可执行文件与 v180 绑定身份保持不变；E7 仍为 `false / false`。

## v182 本轮已收口（绑定器错误输出 schema 化）

- `multidetect-runtime-report-verify` 在输入 JSON 损坏、报告无法读取或契约异常时，现在输出完整的
  `runtime_report_binding_error` 文档：13 项检查统一为 `false`，`result/runtime_report/expectations` 明确为 `null`，
  并保留结构化 `error.type/message` 与稳定 `reasons`；退出码仍为 `1`，不会把失败当成通过。
- `configs/schemas/runtime-report-binding.schema.json` 现在同时约束成功和失败两种输出，失败路径也可由同一
  JSON Schema 校验；损坏 JSON fixture 的 schema 校验通过，绑定门保持 `false`。
- v182 错误证据位于 `artifacts/evaluation/terminal-continuation-v182/`：输入 11 bytes / SHA-256
  `2F021715A3272E09152B7FDD6ABCA9A71CD53F67389855C522BFA4D754D9EE08`；错误报告 1,201 bytes / SHA-256
  `29E7923EE8F20C6617E08538E0962F4C0A84F442AA599510401A3ECA7C3AEC15`，schema errors 为 0。
- 定向回归为 `44 passed, 0 skipped, 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v182/runtime-binding-focused-v182.xml`，6,452 bytes，SHA-256
  `F431DD257A58DA62B6CE5D74529FB0867C0647A4D5FAFBD30B439FD853DF5191`；全量 Python 回归为
  `3724 passed, 4 skipped, 0 failed`（3,728 tests，385.28 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v182/full-rerun.xml`，544,825 bytes，SHA-256
  `3B104184FFDCC0F2611BF369BCA6DDC0B3D2C50F8E61453786857819F3C57702`。本轮仍只增强 software 证据输出；E7、
  室外 GNSS、硬件、HIL/REAL、人工 Fly View 与实飞门保持 `false / false`。

## v181 本轮已收口（真实 integrated Replay 结果格式兼容绑定）

- `runtime_report_binding` 现在同时读取当前生产结果的嵌套 `result.processed_frames`/报告门，和历史
  integrated Replay 产物的顶层 `processed_frames`、`frame_ids`、`natural_stream_exhaustion` 形状；两种形状都必须
  重新绑定 descriptor/input hash、处理帧数量/ID、自然耗尽和无飞控/硬件输出边界，缺字段仍 fail-closed。
- 帧 ID 字段不再静默过滤非字符串、空值或重复值；一旦结果声明了 malformed ID，绑定报告直接失败，避免旧产物
  被“部分解析后通过”。`integrated_replay_finished` 与 `integrated_replay_qgc_finished` 共用同一适配器。
- v181 历史 2 帧 integrated Replay 证据绑定通过：结果 1,276 bytes / SHA-256
  `9A5A8827D9891DC33E763CD6EEA2E904AC61C9C91B02F9772C3A87791C1B7588`；RuntimeRunReport 507 bytes / SHA-256
  `8B6DF9AD8106EA9A83A49BD5C1D9281EA24604F7969649141001A7FDE8433906`；绑定报告 2,410 bytes / SHA-256
  `5CA09D94B667388A537121C3DFCD9F7988A2EDDD31C425AE3F466819D608D33C`，`gate_passed=true`。
- 定向回归为 `43 passed, 0 skipped, 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v181/integrated-runtime-binding-focused-v181.xml`，6,332 bytes，
  SHA-256 `7B8C379AD496BDC0952A238C479F6197F7298B26496A93A19113855A0993C437`；全量 Python 回归为
  `3723 passed, 4 skipped, 0 failed`（3,727 tests，306.38 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v181/full-rerun.xml`，544,704 bytes，SHA-256
  `76008B1C63BD8175159289646ACC70251B9DF89D4417DB11CF4C415357D1EC67`。本轮仍只锁定 software 证据身份；E7、
  室外 GNSS、硬件、HIL/REAL、人工 Fly View 与实飞门保持 `false / false`。

## v180 本轮已收口（运行结果与 RuntimeRunReport 独立绑定）

- 新增 [`src/multidetect/runtime_report_binding.py`](../src/multidetect/runtime_report_binding.py) 与
  `multidetect-runtime-report-verify` CLI：重新打开结果 JSON 和 `RuntimeRunReport` sidecar，拒绝重复键、软链接、
  非对象和损坏/篡改报告，并按已知事件严格核对 runtime mode、descriptor/input hash、处理帧数量/ID、终止语义、
  integrated Replay/QGC 内嵌报告以及 SIM 的 `truth_free`/`closed` 状态；未知事件保持 fail-closed。
- `SimulatorScenarioRunResult.to_mapping()` 现在公开生成 sidecar 所使用的精确 `runtime_input_hash`，审核者不需要
  重建场景输入即可把结果和报告绑定。该字段只扩展 SIM software 结果，不改变 `px4_mission_path` parity、QGC C++/QML
  二进制或任何飞控输出路径。
- 真实 2 帧 `river_rescue_vtol_001` SIM 证据绑定通过：结果 17,161 bytes / SHA-256
  `3883CED727A1ED73F09F63FFC222808406EFB8B46A93B44810385DE136604195`；RuntimeRunReport 542 bytes / SHA-256
  `BF6904C29DC673BFB2BE70C6BA4FFEF8378F240BF553B628BA2545ABB42E304E`，canonical SHA-256
  `31B18485D212AF0ABE04C1C9DDE1F6F1CE96F5CA6C0985E64A69D3A78A1A4A7E`；绑定报告 2,431 bytes / SHA-256
  `7E5C96466580E36BD038199D1B23FD8C7852B3C758710AA4328FE15705109A9B`，`gate_passed=true`。
- v180 定向回归为 `41 passed, 0 skipped, 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v180/runtime-report-binding-focused.xml`，6,062 bytes，SHA-256
  `0FCA9E28E44C47FE8BC42B9B0D3A9CC5F874B325B37584DE14ECF7975063B493`；全量 Python 回归为
  `3721 passed, 4 skipped, 0 failed`（3,725 tests，311.097 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v180/full-rerun.xml`，544,434 bytes，SHA-256
  `D6D12065420C8C64CE91379FBB5411E7DC7DA3F8EB5C9036207807DFE1FE575C`。这轮先锁定 software 证据身份；硬件、
  室外 GNSS、HIL/REAL、人工 Fly View 与实飞门仍未通过，不改变 E7 `false / false`。

## v179 本轮已收口（integrated Replay/QGC sidecar 对账）

- `integrated-replay-qgc` 的 CLI 回归现在挂接真实 `RuntimeRunReport`，在本地 RTSP/metadata 同步 ACK 完成后
  写出 `--runtime-report` sidecar，并核对结果内嵌报告、canonical hash 与 loader round-trip；QGC 入口不再只是
  解析参数，而是覆盖完整报告消费路径。
- 报告仍绑定 `frame-0001`/`frame-0002`、descriptor/input hash、`stream_exhausted=true`、`max_frames=null`；
  integrated Replay、QGC、Live、Runtime 定向回归为 `140 passed, 0 skipped, 0 failed`。
- 定向 JUnit 位于 `artifacts/evaluation/terminal-continuation-v179/integrated-replay-qgc-focused.xml`，19,230 bytes，
  SHA-256 `B4B6620511FE02F01D07177695CFFA28124E18A2502802AE9D7539E4FB813F88`；v179 全量 Python 回归为
  `3711 passed, 4 skipped, 0 failed`（3,715 tests，269.676 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v179/full-rerun.xml`，543,119 bytes，SHA-256
  `EDF8454297EF15BDA74213EDF5DD18C9110C9A4E8A61D6E1A4A998AD3F3DFFAD`。这是 QGC 入口补强，不改变
  QGC C++/QML 二进制，也不提升室外 GNSS、真实硬件、HIL/REAL、人工视觉或实飞门。

## v178 本轮已收口（integrated Replay 自然耗尽与 RuntimeRunReport）

- `ReplayFrameSource` 为有限 recording 增加独立的 `FrameSourceExhausted` 结束信号；`LiveMissionRunner`
  将其记录为 `camera.stream_exhausted`，保留直接 `read()` 的历史 `RuntimeError` 兼容性，避免把自然结束和
  相机读取故障混在一起。
- `IntegratedReplaySession` 现在只接受完整 recording：逐帧核对实际消费 ID 顺序、frame count、descriptor/input
  hash 以及 source exhaustion，再生成与 SIM/HIL/REAL 共用的 hash-bound `RuntimeRunReport`。兼容传入的
  `max_frames == frame_count` 会归一化为 `null`，报告明确 `complete=true`、`termination_reason=stream_exhausted`。
- `integrated-replay` 与 `integrated-replay-qgc` 支持 `--runtime-report <path>`，结果 JSON 嵌入同一 canonical
  report，sidecar 原子写入并通过 loader round-trip；v178 定向回归为 `140 passed, 0 skipped, 0 failed`。
- v178 证据位于 `artifacts/evaluation/terminal-continuation-v178/`：runtime report 507 bytes，SHA-256
  `8B6DF9AD8106EA9A83A49BD5C1D9281EA24604F7969649141001A7FDE8433906`，canonical SHA-256
  `F72FBCEE791A97DBAA701B5FAC2C25FA9535359D61A575532B7BF49EAE93CF78`；run summary 1,276 bytes，SHA-256
  `9A5A8827D9891DC33E763CD6EEA2E904AC61C9C91B02F9772C3A87791C1B7588`；focused JUnit 19,230 bytes，SHA-256
  `6CB22CBD107BF23E0191801DE02AFB215A328F324726721A0DCBCF4A043A4C76`。
- v178 全量 Python 回归为 `3711 passed, 4 skipped, 0 failed`（3,715 tests，236.371 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v178/full-rerun.xml`，543,117 bytes，SHA-256
  `736FBAB2D45161703BDC256C5758D636EDEA33CDB2C0DE7E536E3FCD8D052A3B`；该轮只增强 software/Replay/QGC
  证据链，不改变 QGC C++/QML 二进制，也不提升室外 GNSS、真实硬件、HIL/REAL、人工视觉或实飞门，E7 仍按
  report-only 规则阻断。

## v177 本轮已收口（SIM RuntimeRunReport sidecar 与 bounded-prefix 对账）

- `SimulatorScenarioRunResult.runtime_run_report` 复用 Replay/HIL/REAL 的统一 `RuntimeRunReport` 契约，绑定
  manifest runtime channel hash、SIM `RuntimeDescriptor`、稳定的 `sim:<scenario>:<index>` 帧 ID 和共享
  `ManualClock`；场景 evidence 的 `frame_budget_complete` 仍表示预算跑满，sidecar 则明确
  `stream_exhausted=false`、`termination_reason=frame_limit_reached`，避免有限 SIM 前缀被误报为自然完整流。
- `multidetect.simulator.scenario_runner` 新增 `--runtime-report <path>`，通过同一原子 writer 生成可独立加载的
  JSON sidecar。v177 CLI 产物为 3 帧 VTOL、`complete=false`、`max_frames=3`、报告 canonical hash
  `A8F02A03A8695484A1EF5759D360C908BDE49CC06264A12F050B1B4251BBF014`；报告文件位于
  `artifacts/evaluation/terminal-continuation-v177/simulator-runtime-report-v177.json`，574 bytes，SHA-256
  `11B084DAF84F9B7BF2861A253F6311D10EB051A576AA0D32BC0197F57DC32669`。
- 同一 CLI 输出的 scenario evidence 位于
  `artifacts/evaluation/terminal-continuation-v177/simulator-run-v177.json`，19,797 bytes，SHA-256
  `A786EF286E6F49A29E92B5035129E0A5F51827AD60CEEA3A7A57A4AD9A64F478`；stderr 为 0 bytes，报告加载复核通过。
- `tests/test_simulator_scenario_runner.py` 新增 sidecar 属性、持久化往返和 CLI 写入回归；v177 场景 runner 定向
  回归为 `31 passed, 0 skipped, 0 failed`，JUnit 为
  `artifacts/evaluation/terminal-continuation-v177/scenario-focused-rerun.xml`（4,747 bytes，SHA-256
  `D8B5660FE07718F15785631B527295788DAEEE7D788D77C3440B3F44DE8B1A89`）。该更新只增强 truth-free software/SIM 记录，不改变 QGC C++/QML
  二进制，也不提升室外 GNSS、真实硬件、HIL/REAL 或实飞门。
- v177 工作树全量回归为 `3710 passed, 4 skipped, 0 failed`（3,714 tests，233.629 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v177/full-rerun.xml`，542,975 bytes，SHA-256
  `6DC8B44644D659929D60162B506D2F178ED4694B7E148597902424F8B4F366F6`；Ruff、format、compileall 与
  `git diff --check` 作为同一收口门复核。

## v176 本轮已收口（运行时完成报告与确定性前缀阻断）

- `RuntimeRunReport` 现在把自然耗尽与有界前缀分开记录：`stream_exhausted=true` 才表示完整流，传入
  `max_frames` 的运行会保留已处理帧但明确标记 `complete=false`、`termination_reason=frame_limit_reached`；
  不再把“刚好处理到上限”误报为完整 Replay/SIM 运行。
- 新增 `configs/schemas/runtime-run-report.schema.json` 以及 `write_runtime_run_report()`/
  `load_runtime_run_report()`：报告使用 canonical JSON 与 SHA-256，原子写入，严格拒绝未知字段、重复 ID、
  派生计数/时长/终止原因篡改和错误的 persisted hash；descriptor hash 与 backend input hash 仍绑定在同一身份边界。
- `runtime.py`、schema、parity source list 与回归测试已更新；RuntimeBackend 专项回归覆盖完整流、有界前缀、
  持久化往返、schema 校验和 tamper fail-closed。
- 当前 `configs/release-parity.json` 为 10,185 bytes，SHA-256
  `C3F9362479B8DF39C7126CDA5B84A573E86D907079391DFC82A505650D90F42B`；`px4_mission_path_hash` 为
  `727CDF63FC97FA8DA5D642274620FF3D6009C406912FF9B9A0D40E3685132CB8`。三机型 external join/revalidation 与
  PX4 response matrix/campaign 均重新绑定并 `gate_passed=true`，report-only E7 位于
  `artifacts/evaluation/terminal-continuation-v176/release-gate/`，仍为 `gate_passed=false`、
  `release_allowed=false`、32 条详细 blocker。
- 完整 Python 回归为 `3708 passed, 4 skipped, 0 failed`（3,712 tests，228.68 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v176/full-rerun.xml`，542,691 bytes，SHA-256
  `6678934D4FFB91F0F9FA331D0F5E193424DAA68DDB6169ADCDE416103E408830`；Ruff、format、compileall 与
  `git diff --check` 继续作为收口门。
- 增量 Windows Release 构建已完成（Ninja 报告 `no work to do`）：Release 与 portable EXE 均为
  `46,875,136` bytes、SHA-256 `41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`；
  `MultiDetectTargetEventReplayStoreSelfTest.exe` 退出 0 并输出 `PASS`，portable
  `--simple-boot-test` 退出 0，stdout/stderr 均为 0 bytes（SHA-256 均为
  `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855`）。
- 当前仍是 `v0.3.0-pre.8` prerelease。运行时报告和 software/SIM 测试不等同于室外 GNSS、真实硬件、HIL/REAL、
  人工 Fly View 或实飞验收；现场 portable 长回放对账仍是下一道人工门。

## v175 本轮已收口（全量摘要一键复制与 parity 重绑）

- Fly View 回放弹窗新增“复制全量摘要”：只在 native replay store 已加载且完整性校验通过时启用，复制
  bundle/session、开始时间、总事件/媒体数、可定位/有效测距/有媒体/唯一目标统计、捕获时长、媒体字节和完整性状态；
  复制仍是只读剪贴板操作，不落地图、不选目标、不创建 Mission、不发送飞控指令。
- v175 继承 native/Python 完整事件流质量统计与最多 512 条显示上限；QGC 静态契约与回放专项回归为
  `45 passed, 0 skipped, 0 failed`，portable offscreen `--simple-boot-test` 退出 0 且 stdout/stderr 均为 0 bytes；
  native replay self-test 通过。
- Release/portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,875,136 bytes，SHA-256 `41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`；
  Release 与 portable 文件字节一致。
- 当前 `configs/release-parity.json` 为 10,129 bytes，SHA-256
  `1C937FFDD4D0D59267A5DF79A240E8101431613CAD0BE17533B2FFF6ECC33EBC`；core hash 保持
  `155A5011AA27042D587DD50AE29236A02DB4E7F42864CCF2607CB21AAC626D0A`，UI hash 更新为
  `46629AC7EC0AF1DC03B9D4D01B5EC56F959CF6C2C5EC27E59EBF76CD1090ECB9`。
- v175 external join/revalidation、PX4 response matrix/campaign 均 `gate_passed=true`，文件分别为
  5,492/565/6,985/26,120 bytes，SHA-256 为 `CBFAE8D4A08A97A1007FBCAE3FDE6F82D94816A77878AF6C17E9C21BCC4FD6AD` /
  `EE2C4F7D032315027C109D60E83EF36C0FD8B15408F8A83A99A027A308B8F702` /
  `8960871D28A7934D27C11C19C9F3ECA5844EF89F2BDBEE49E5348BA6F2CC73E3` /
  `3999F21F379B0B1EF46972413564A6D025390068CCD53D8A61DB093A33A06139`；matrix/campaign canonical SHA 为
  `051C9FA04794AFD8A74F40AA5EBA638983129FD137B53B41B5DE9776EAEFD52B` /
  `86E6318530784A3770F8A4AEC6B2E1E6E5C8BCD0525BD87B201809E3C764B5EE`。
- report-only E7 已重算到 `artifacts/evaluation/terminal-continuation-v175/release-gate/`，manifest/report 为
  23,729/73,786 bytes，SHA-256 `4EF6E491199AADEF899D31DA3E02DAEFCC677A8C19CFAC58B62411B5226F5FF4` /
  `7941392E1A51C1119A9C7661A22ED23E3B64D4D5D852A213CE4F1C35B58062DA`；E7 仍为
  `gate_passed=false`、`release_allowed=false`、32 条详细 blocker（仓库基线保持 31）。
- 本轮没有提升室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞状态；现场链路继续 fail-closed。
- 完整回归随后完成 `3704 passed, 4 skipped, 0 failed`（3,708 tests，195.301 秒），JUnit
  `artifacts/evaluation/target-event-replay-v175-full-rerun.xml` 为 542,180 bytes、SHA-256
  `233628DA0C949A2F84E0690FF293E9F642D7D33BB64785E51316AA031B332BBC`；Ruff、format、compileall 与
  `git diff --check` 通过。

## v174 本轮已收口（全量回放质量摘要与 parity 重绑）

- `TargetEventReplayBundle` 与原生 `MultiDetectTargetEventReplayStore` 现在都从完整、已验证的事件流计算质量摘要：可定位事件数、有效测距事件数、有媒体事件数、唯一目标数、首尾捕获时长和哈希绑定媒体字节数；QML 仍只显示最多 512 条事件，避免长回放被截断后误读总量。
- Fly View 回放筛选同时显示“匹配总数”和“当前显示数”，四种筛选（全部、可定位、有效测距、有媒体）均使用 native 全量计数；事件明细、复制证据、地图/任务/飞控边界保持 v172 不变。
- C++ self-test、Python/native 跨语言回放测试、QGC 静态契约测试、portable offscreen simple boot 均通过；v174 定向回归为
  `45 passed, 0 skipped, 0 failed`，完整回归为 `3704 passed, 4 skipped, 0 failed`（3,708 tests，181.948 秒），JUnit
  `artifacts/evaluation/target-event-replay-v174-full-rerun.xml` 为 542,180 bytes、SHA-256
  `F891C6155A01A275AE96372F2DF38487483A8FA4CFC57A339F1EDBA9D8AD9FAA`；Ruff、format、compileall 和
  `git diff --check` 通过。
- Release/portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,875,136 bytes，SHA-256 `4E468DD936CD7F938E7373B0500294B12DDA9E1942FA9D9412D8803AB49E457A`；native replay self-test 为
  209,408 bytes，SHA-256 `69A860CE4A5ED7A94E66849FA0FA0CB86C658CCF9CAC29FF7B7099910BAF1195`。
- 当前 `configs/release-parity.json` 为 10,129 bytes，SHA-256
  `01001D1DFB2B043D03DAAD50CC88DABC669B90831FDA760D08C25937FF20836D`；core hash 为
  `155A5011AA27042D587DD50AE29236A02DB4E7F42864CCF2607CB21AAC626D0A`，UI hash 为
  `1A7F75ECB38A9880F88046214E97A5DE5BDA78A7BA0494943604BE2105244C71`。
- v174 重新绑定的 external join/revalidation、PX4 response matrix/campaign 均 `gate_passed=true`，文件分别为
  5,492/565/6,985/26,120 bytes；同一 parity 下的 report-only E7 位于
  `artifacts/evaluation/terminal-continuation-v174/release-gate/`，manifest/report 为 23,729/73,786 bytes，
  SHA-256 `D6AB3FD2477408196A8AA304BEE6FC1AB74B45550373B807C46EB304BEB56228` /
  `B58787B85F0FA33B35EABFD32E01C62F4FD2D204E60482A07016F9FD491432E6`；E7 仍为
  `gate_passed=false`、`release_allowed=false`、32 条详细 blocker。
- 本轮没有提升室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞状态；现场链路仍按 fail-closed 记录。

## v173 本轮已收口（当前 parity 重绑与 E7 report-only 重算）

- 以当前 `configs/release-parity.json`（10,129 bytes，SHA-256
  `A12A5490E73ED52266A44F447DFD87895CD837476895AFFF2EFD43CAAE09C827`，UI
  `82FA274FB167812803C39FF7E260ACF334BB903436D04BB5E653879D806C0FDC`）重新生成并独立复核三机型 external
  runtime/AI join、PX4 `GLOBAL_POSITION_INT` response matrix 与 repeatability/fault campaign；三份 gate 均通过。
- 新证据位于 `artifacts/evaluation/terminal-continuation-v172/`：external join/revalidation 为 5,492/565 bytes，
  PX4 matrix/campaign 为 6,985/26,120 bytes；对应 source SHA-256 和 matrix/campaign canonical SHA 已写入机器状态。
- report-only E7 已在同目录 `release-gate/` 重算：manifest/report 为 23,729/73,786 bytes，SHA-256
  `6E1D31983EF6C74155D9E6FD818BD86B2CDD7956C32D33A8000F0A3BAAEC8A52` /
  `1913AD13F27157434F0D71AC574E2E9F58090F233BE55F54088E93BCDA078234`；当前
  `gate_passed=false`、`release_allowed=false`、32 条详细 blocker（仓库基线仍为 31）。
- 本轮只是 parity/证据重绑，不改变 v172 的只读回放功能或任何硬件能力声明；现场网卡仍为 `192.168.1.13/24`，
  Jetson/GR01/相机未连通，室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 和实飞状态继续未声明。

## v172 本轮已收口（回放证据明细、筛选与复制）

- 原生 `MultiDetectTargetEventReplayStore` 继续作为唯一读取入口：事件媒体摘要现在包含 `kind`、相对路径、
  encoding、bytes、完整 SHA-256 与可选宽高；`source_frame_id`、机体 AGL 和航向等元数据在 C++ 侧按可空语义、
  有限数和范围严格校验，失败仍清空已加载状态。
- Fly View 回放弹窗增加“全部/可定位/有效测距/有媒体”四种只读筛选；每个事件可一键“复制证据”，复制文本包含
  事件/目标/捕获时间、源帧、车辆姿态、GPS、测距和完整媒体身份，仍不落图、不选目标、不创建任务、不发送飞控。
- C++ self-test、Python 创建的真实 bundle 由 native `--load` 跨语言复核、portable offscreen simple boot 均通过。
  v172 定向回归 45 tests：`45 passed, 0 skipped, 0 failed`；JUnit `artifacts/evaluation/target-event-replay-v172-focused.xml`，
  6,772 bytes，SHA-256 `FFC03FE6EB989B4119B8209A0753BAA6C87D78F20587D4B6B989591483A328AD`。
- 文档/parity/style 定向回归另有 72 tests：`72 passed, 0 skipped, 0 failed`；Ruff、format、compileall 与
  `git diff --check` 通过。
- 全量回归 3,708 tests：`3704 passed, 4 skipped, 0 failed, 0 errors`，耗时 194.025 秒；JUnit
  `artifacts/evaluation/target-event-replay-v172-full-rerun.xml`，542,180 bytes，SHA-256
  `31D7D474F947B1E4F648CCBC4C62404FCE10C44DEADDE83E16C3B1E156C78AF0`。
- Release/portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,869,504 bytes，SHA-256 `A1C52D9130386D8FAE4316EE23FD00D68E2141E6A385A7844229CB8680A79412`；
  native replay self-test 为 202,240 bytes、SHA-256 `7BD2F93FBA5889D548BB65F5F5E81AA780FE47747593439BA8B4805FDBA0F837`。
- `configs/release-parity.json` 当前为 10,129 bytes，SHA-256
  `A12A5490E73ED52266A44F447DFD87895CD837476895AFFF2EFD43CAAE09C827`。v171 派生证据与 E7 report-only 当时曾待
  重绑，已由 v173 在当前 parity 下重新生成；E7 仍 `gate_passed=false`、`release_allowed=false`，仓库基线 31 blockers 不变。
  现场网卡仍为 `192.168.1.13/24`，目标网段 `192.168.144.0/24`，Jetson/GR01/相机未连通；本轮没有提升室外 GNSS、
  真实硬件、HIL/REAL、人工 Fly View 或实飞状态。

## v171 本轮已收口（原生目标事件回放只读查看器）

- 将已有 `multidetect.target-event-replay.v1` 证据包接入原生 QGC Fly View：选择 `manifest.json` 后由
  `MultiDetectTargetEventReplayStore` 在 C++ 侧一次性校验 manifest、`events.jsonl` 与 `media/` 闭合目录，
  再以只读 `QVariantList` 投影到 QML 时间线；校验失败会清空状态，不保留半加载数据。
- 原生校验器拒绝重复 JSON key、未知字段、非有限数、路径逃逸/符号链接、时间/序列不连续、GPS 质量或新鲜度
  不合格、测距区间不合法、媒体额外文件和 bytes/SHA-256 不匹配；事件与媒体路径均限制在 manifest 所属目录。
- Fly View 目标卡片新增“载入回放/查看回放”入口和只读弹窗，显示 bundle/session、事件数量、目标类别、捕获时间、
  GPS、距离/方位、视频/深度引用及校验状态；弹窗没有落图、选目标、任务、飞控或物理释放调用。
- `MultiDetectTargetEventReplayStoreSelfTest` 通过，覆盖有效包、篡改媒体和重复 key 的 fail-closed 路径；Python
  `create_target_event_replay.py` 生成的真实 bundle 由 C++ self-test `--load` 跨语言加载通过。
- v171 定向回归 45 tests：`45 passed, 0 skipped, 0 failed`；JUnit `artifacts/evaluation/target-event-replay-v171-focused.xml`，
  6,772 bytes，SHA-256 `EAC8F4C1E543E517E90314060CA65A998AD00826DBC67A68598087C2518E9520`。Ruff、format、compileall 与
  `git diff --check` 通过。
- 全量回归 3,708 tests：`3704 passed, 4 skipped, 0 failed, 0 errors`，耗时 184.441 秒；JUnit
  `artifacts/evaluation/target-event-replay-v171-full.xml`，542,180 bytes，SHA-256
  `8074DBAD5781888E518F7A0826F30B9CDD34F4F654F0B2CE698754DA89CE2DC6`。
- Release portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,863,360 bytes，SHA-256 `589E33ADAB2C7D1DBC58524BCAE3474D18743A6E0D970FC4C01CF3F187201717`。
  `MultiDetectTargetEventReplayStoreSelfTest.exe` 退出 0；portable `--simple-boot-test` 退出 0，stdout/stderr 均为
  0 bytes，QML 初始化错误数 0。
- `configs/release-parity.json` 已重新生成：10,129 bytes，SHA-256 `D99F244ACFA708D16CC40019CA0ED8E8FCFCE827BFD250983A5FAC1A716ED23C`；
  UI `CC9841A00BF6B92F05C68C6CB26921EC8163F829A223B32B9C42131C55E5AADF`，core/task/safety/PX4 四组 hash 保持当前绑定。
- 派生证据也从原始 source 按 v171 parity 重算：external join/revalidation 为 5,492/565 bytes（SHA-256
  `9EE018D6B483511922FCE578E1B3E6E556B19C9488AC2A0B2E6C88FB774012FF` / `8D72ED39C260275CAA6E616FB7825A865CDE4ED3EF5E4062F0294EBBB671708A`），
  PX4 response matrix/campaign 为 6,985/26,120 bytes（SHA-256
  `1D67697C85CDEF0A1F4F3F37AE331EECCD62D0922323D8D87DD08D42A484ADF6` / `6766516C2D6A8E99A00947D8F76922D143A751FD0E49857E06BC5D0F669AA545`）。
- E7 report-only 已在新输出目录 `artifacts/evaluation/terminal-continuation-v171/release-gate/` 重新生成；manifest/report
  为 23,729/73,786 bytes（SHA-256 `B1CBCD26061925787C34FA02405818833CE9A1D80D4609F3967505558B62745E` /
  `5E7EC0DF188AB1E093B196B4E936E296EF0DCBDD73BDB8C1EE11A892780986E0`），当前仍 `gate_passed=false`、
  `release_allowed=false`，现场 freshness 超时与尚未完成的真实/人工门保持显式阻断。
- E7 仍为 `gate_passed=false`、`release_allowed=false`；仓库基线保持 `31 blockers`，本次 report-only 因现场报告
  新增 freshness 超时而列出 32 条详细原因。现场网卡仍是 `192.168.1.13/24`，目标网段
  `192.168.144.0/24`；Jetson `192.168.144.10:22`、GR01 `192.168.144.25:5760`、相机 `192.168.144.108:554` 仍未连通。
  本轮没有提升室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞状态；当前查看器只读证据回放，不产生飞控指令。

## v170 本轮已收口

- Fly View 目标定位卡片现在直接读取 `MultiDetectState.targetObservationRecordingQualitySummary.current_status`，
  与诊断弹窗共用同一原生 recorder 摘要；卡片显示当前/上次录制状态、定位率、当前连续不可定位、最近采样断档和
  `target_not_locatable` / `range_invalid` / `gnss_unqualified` / `latest_sampling_gap` 原因。
- 没有录制样本时状态为中性 `WAITING`；录制停止后仍显示“上次定位质量”，不把历史摘要冒充当前采样。质量行只读，
  不改变 GPS/测距 fail-closed、目标坐标与 `1σ` 圆、不进入 PX4 Mission、飞控或物理释放路径。
- QGC custom 静态回归覆盖属性、四级状态映射、告警原因映射和主卡片绑定；Release/portable EXE 已重链并完成依赖部署，
  46,751,744 bytes、SHA-256 `9675AC36032CC990F1F27ACD638F1B4D1D0397F34D9B43810A95D0BCD0D2D263`。
- 定向回归 89 tests：`88 passed, 1 skipped, 0 failed`，耗时 2.194 秒；JUnit 14,124 bytes、SHA-256
  `C56C01C84072E0EC1B51F09FA6C9FCE90E37A85F530FCF034BFB0779E91F79B9`；Ruff、format、compileall 与 diff check 通过。
- 全量回归 3,707 tests：`3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 183.031 秒；JUnit 542,030 bytes、
  SHA-256 `3B21593CB2277F1762D70C2287B34309EC615EE3456BC6D9DECDE9736AC7B3E1`。
- portable offscreen simple boot 退出 0，stdout 0 bytes，stderr 879 bytes、SHA-256
  `EBB0811E76009EAA70CEC8B19F52B3C2B3232340FAE78CDD7CC67A505644D7F9`，QML 初始化错误数 0。
- v170 重新生成并独立验证 external join/revalidation、PX4 三机型 response matrix、6 正例/9 故障 campaign、
  现场只读 preflight/interface diagnostic 和 E7 report-only；现场接口仍为 `192.168.1.13/24`，目标网段为
  `192.168.144.0/24`，Jetson/GR01/相机均超时。E7 仍为 `gate_passed=false`、`release_allowed=false`、
  `31 blockers`，本轮不提升室外 GNSS、硬件、HIL/REAL、人工视觉或实飞状态。
- 当前派生证据均留在 `artifacts/evaluation/terminal-continuation-v170/`：external join/revalidation 为
  `5,491/565` bytes（SHA-256 `7380455757770BC76C22FBF1C53FBC18B1AE37A97022E431B0F801A491BDCDDD` /
  `BABB9B571A399CAC270938FB4923AB7C5480F6A9DBBCA36A701FD415807B6824`）；PX4 matrix/campaign 为
  `6,985/26,120` bytes（文件 SHA-256 `8D26F7633D5DD353FC9CDC93413334432458F7087F43E278013AA9FF9D90C7A1` /
  `32D5B33F38290CE6B99EFA3B37A1A9C5D7723B197D96A44C4CC9FCB599E2CBBC`）；现场 preflight/interface 为
  `4,668/4,403` bytes（SHA-256 `83C33525C1DF94E696FA876837E4F07F125F0023F1A99749C794574431043574` /
  `AAED1FCF57471A76B745CAFD1192A4D089F781D800A5764C4F72AB53605E09C4`）。

## v169 本轮已收口

- 原生录制器的实时质量摘要新增 `current_status`：按当前连续状态给出 `WAITING`、`HEALTHY`、
  `WARNING`、`CRITICAL` 四级结果，并暴露当前连续不可定位/测距异常/GNSS 不合格计数、最近采样间隔、
  断档告警原因和严重级别阈值；历史质量分段与停止后的 manifest 绑定离线 verifier 保持不变。
- Fly View 诊断弹窗新增当前状态颜色、中文状态文本和实时计数；质量状态仍是只读 advisory，任何质量告警
  都不会生成坐标、任务或飞控输出。
- C++ self-test 覆盖 `WARNING → CRITICAL → HEALTHY` 恢复路径；安装式 verifier 独立复核 4 样本会话，
  2/4 可定位、定位率 `0.5`、最长连续不可定位 `2`、最大断档 `2,000 ms`，报告 7,795 bytes、SHA-256
  `580898F32543B8EBAEE78E0099DDD46AACAC878066DA221AB3D431B1325C4BD4`。
- Release/portable EXE 已按 v169 重链并完成依赖部署：46,751,232 bytes、SHA-256
  `5BC3761AE4C13C28EBE6BB34691F6EB350CA9EDEF96376DF0DDD03DBE73C5A3A`；portable offscreen simple boot
  退出 0，stderr 879 bytes、SHA-256 `D2BE8F83902FF9AF48F105685EF17B4571075F826E6A0126603DCBC670D41F81`。
- 定向回归 86 tests：`85 passed, 1 skipped, 0 failed`，耗时 2.412 秒；JUnit 13,696 bytes、SHA-256
  `8721CA481C38579F34E4916C05D1F496461B1A037999B6D0F267C924DD807135`。最终全量回归 3,707 tests：
  `3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 182.008 秒；JUnit 542,030 bytes、SHA-256
  `B0010AE56D61353559AF5C200E28D0A7310DF7650962FBDEA35406D18487B8D1`。
- parity 9,956 bytes、SHA-256 `FB645D12D0CF0DD4CCEFF68CFC0D15D1E4BCFC25FD574301C4790D61D3F94A05`；
  UI contract 为 `6C6BECB3C9890EAF3360FFBECADC2371923B0DE5014723CB8DC911DDE47CCC15`，其余四组不变。
- 三机型 external join/revalidation、PX4 reported-position matrix、6 正例/9 故障 campaign、只读现场链路
  和 E7 已按 v169 parity 重算并绑定。现场接口仍为 `192.168.1.13/24`，不在目标 `192.168.144.0/24`，
  Jetson `192.168.144.10:22`、GR01 `192.168.144.25:5760`、相机 `192.168.144.108:554` 均超时；E7
  仍为 `gate_passed=false`、`release_allowed=false`、`31 blockers`。本轮不提升室外 GNSS、硬件、HIL/REAL、
  人工视觉或实飞状态。

## v168 本轮已收口

- 原生录制器现在在每次成功保存快照后同步维护只读实时质量摘要：样本/可定位数量、定位率、最长连续
  不可定位、观测时长、采样间隔/断档、三类质量分段和 producer epoch 切换都可直接供 QML 使用。
- Fly View 诊断弹窗新增“录制质量 · 实时只读”区域与质量时间线；红/黄/橙分别表示不可定位、测距
  无效和 GNSS 不合格，详细列表保留分段范围与 epoch 切换数量。停止录制后的正式结论仍由 manifest 绑定
  的独立离线 verifier 生成，实时 UI 不自封为最终证据。
- C++ self-test 生成 4 样本会话，覆盖 2 个连续不可定位样本、2,000 ms 断档、三类各 1 个质量段与
  1 次 epoch 切换；安装式 verifier 独立复核后 `gate_passed=true`。报告 7,780 bytes、SHA-256
  `EB06060134414907BBDAB327553958A28FEB7AF2E7FCB22D9B96D9D2C65221BB`。
- Release/portable EXE 已重链并完成依赖部署：46,746,112 bytes、SHA-256
  `F74F3D6FCFC8C044745A2B75A8ED07FD4CEBD41BDE2BD1239E00F9FB40C9A9FA`；offscreen simple boot 退出 0。
- 定向回归 86 tests：`85 passed, 1 skipped, 0 failed`。parity 9,956 bytes、SHA-256
  `DE1686C5844B11D30D2AA532FE701FA55E9E909C221CA621650FCBA6C1E5F997`；UI contract 为
  `47A6E679A127FCF7E80A3723D85A8756BD50CA87CD4B546B5A4D2D537855DB3`，其余四组不变。
- 最终全量回归 3,707 tests：`3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 182.514 秒；JUnit
  542,030 bytes、SHA-256 `B15A4B123DBF024311C3DB5E3A74B8F19A6C08115839AA09FD44090B71B23B04`。
- SIM/PX4 派生证据、现场只读探测和 E7 已按 v168 重算。现场仍是 `192.168.1.13/24` 错路由且三端点
  超时；E7 保持 `false / false / 31 blockers`。功能继续为 advisory-only，不提升室外 GNSS、硬件、
  HIL/REAL、人工视觉或实飞状态。

## v167 本轮已收口

- manifest 绑定 session 报告新增观测总时长、采样间隔最小/最大/平均值、manifest 期望间隔、容差、断档
  事件和违规数量；无需人工翻查数千份快照即可发现采集停顿或丢帧区间。
- 新增连续质量分段：`non_locatable`、`range_invalid`、`gnss_unqualified` 分别记录开始/结束样本、UTC、
  本地时间、持续时间、样本数、诊断原因、测距状态和 GNSS 原因；epoch 切换也记录前后 epoch 与发生点。
- 新增 `--maximum-non-locatable-run` 与 `--maximum-sampling-gap-ms` 两个可选门，超过允许的连续不可定位
  样本数或最大采样间隔时会保持报告可审计但 `gate_passed=false`。
- 安装式 CLI 对 v165 C++ 双快照目录以定位率 1.0、最大不可定位连续数 0、最大间隔 1,000 ms、零质量
  异常段通过严格门；报告 3,349 bytes、SHA-256
  `BF033208CA17673E7F1D6F6ECB6701FDEB89A67555B772748473423CE3617471`。
- 定向回归 90 tests：`88 passed, 2 skipped, 0 failed`。parity 9,956 bytes、SHA-256
  `9F0583CAD05B62E14D82C832209FB2D1EBEEDDC6A61E7BA90DAA928C114EC817`；UI contract 为
  `38269F81C6FAFE0E2A47ADD5FE874336BA7E9E1EB6C6F2CA25C0FE35A655138D`，其余四组不变。
- 最终全量回归 3,707 tests：`3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 188.534 秒；JUnit
  542,056 bytes、SHA-256 `2C91D31119AD87B16987A59675C179E0AB1922E19091FA70193B47DDB01B6178`。
- SIM/PX4 派生证据、只读现场链路和 E7 已重算；现场仍从 `192.168.1.13/24` 错路由且三端点超时，
  E7 保持 `false / false / 31 blockers`。本轮不重链 EXE，不提升真实硬件、室外 GNSS 或实飞状态。

## v166 本轮已收口

- 为原生录制 manifest 增加闭合 Draft 2020-12 schema、严格 Python verifier、仓库 wrapper 和安装式
  `multidetect-target-observation-recording-manifest-verify`。验证拒绝重复/未知字段、非有限数、符号链接、
  路径逃逸、重复引用、bytes/SHA-256 不符、时间区间/顺序错误及未被 manifest 引用的额外快照。
- session verifier 自动识别单目录中的 manifest；`--require-recording-manifest` 强制目录必须完成 manifest
  校验，并把 manifest 路径、bytes、SHA-256、session ID、停止原因、样本数和目录闭合状态写入会话报告。
  零样本 manifest 可以作为结构有效的短会话记录，但不能通过需要实际诊断样本的 session 门。
- 安装式 CLI 对 v165 C++ 实际会话验证通过：manifest 报告 2,288 bytes、SHA-256
  `A6C4F145B4C8DDBED6D3049667588A34EC601D8372D731CDB8767EA59E4D15AC`；manifest 绑定会话报告
  2,832 bytes、SHA-256 `93F6A85D1D662B4744FF17A6B3F1750CECCE022766849538A52C35B9789133EB`。
- 定向回归 84 tests：`82 passed, 2 skipped, 0 failed`；两个 skip 仍是 Windows 当前权限下的符号链接测试。
- parity 为 9,956 bytes、SHA-256
  `0D74691C5655ACB1AE49EB99AA29B303A21C29FD3ECC23007696DB719DC935E3`；UI contract 更新为
  `F472E0188120B71A9B8FA33588BA91957EDDF9BF6FC4F1157F09CA7FBBE279B5`，其余四组不变。
- 派生 SIM/PX4、只读现场链路和 E7 已按 v166 parity 重算；现场仍从 `192.168.1.13/24` 错路由且三端点
  超时，E7 保持 `false / false / 31 blockers`。本轮仅增强离线证据完整性，不重链主 EXE，也不提升
  人工、室外 GNSS、硬件、HIL/REAL 或实飞状态。
- 最终全量回归 3,701 tests：`3697 passed, 4 skipped, 0 failed, 0 errors`，耗时 269.920 秒；JUnit
  541,038 bytes、SHA-256 `2B9EFE6FC0E4579AB487E6D27485B0B56189E29C083E5E9A818E36DF01964AAA`。

## v165 本轮已收口

- 原生 QGC 新增目标定位诊断连续录制：Fly View 可一次开始/停止，以 `1 Hz` 保存严格 v163 快照，单次
  最多 3,600 份；UI 显示活动状态、样本数与会话目录，应用退出、采集失败和上限到达都会执行明确收口。
- 每次录制使用唯一 `target-observation-session-*` 目录；快照沿用原子保存，正常停止以原子替换写入
  `multidetect.target-observation-recording-manifest.v1`，记录停止原因及每份相对路径、bytes、SHA-256。
  异常退出时已完成快照仍保留，不能伪造一个“已完成” manifest。
- C++ recorder self-test 真实生成 2 份快照和 manifest；安装后的
  `multidetect-target-observation-session-verify` 独立复核得到 2/2 可定位、定位率 1.0、门通过。会话报告
  2,272 bytes、SHA-256 `4E463C50144C83E40A50B472F97539D9C7B522683F81CEAB1AF2C146F6F344F5`。
- Release/portable EXE 已重链：46,718,976 bytes、SHA-256
  `8F600A9CE8D1C4E8AF1A63721F16E52A67E56715A81B2EE7D469AED62C1BAB6D`；portable offscreen simple
  boot 退出 0，无 QML 初始化失败。
- parity 为 9,750 bytes、SHA-256
  `149E83944D7E90331A9940665C14D5EAB9387533C49F2308C1027A7903A11496`；UI contract 更新为
  `1B14A62D40FDA9C76EC37C9E789A25D2123DB4DFF489CB61CF263888CB1B7EA1`，其余四组未变。
- 派生 SIM/PX4 证据与 E7 已按 v165 parity 重算。只读现场探测确认系统仍从 `192.168.1.13/24` 路由，
  Jetson/GR01/RTSP 三端点超时；E7 保持 `gate_passed=false / release_allowed=false / 31 blockers`。
  录制功能固定 advisory-only、飞控关闭、物理释放关闭，不提升人工、硬件、室外 GNSS、HIL/REAL 或实飞状态。
- 最终全量回归 3,693 tests：`3689 passed, 4 skipped, 0 failed, 0 errors`，耗时 196.262 秒；JUnit
  539,793 bytes、SHA-256 `16793FE3E14EFB14F9D3CFA8404EB048678A6D5507E40D666ABB4CE1BD1E264C`。

## v164 本轮已收口

- 新增闭合 `multidetect.target-observation-diagnostics-session.v1` 会话报告、严格 verifier、仓库 wrapper
  和安装式 `multidetect-target-observation-session-verify`。输入可为显式快照或诊断目录；目录只读取标准
  命名 JSON，拒绝空集合、重复解析路径和符号链接。
- 每份文件先完整经过 v163 单快照 verifier，再按 `local_now_ms` 确定性排序。会话拒绝时间重复/回退、
  同 producer epoch 的 sequence/`sentAtMs` 回退以及 retired epoch 重新出现；不同 epoch 的远端时钟不做
  错误的跨时钟比较。
- 报告逐样本绑定绝对路径、bytes、SHA-256、生成时间、定位状态、失败原因与原子身份，并汇总定位率、
  最长连续不可定位区间、epoch 切换、失败原因、range validity 和 GNSS reason。`--minimum-samples` 与
  `--minimum-locatable-rate` 不满足时 `gate_passed=false`。
- 安装后 CLI 消费实际 C++ v163 输出形成 1 样本跨语言会话报告：1,371 bytes、SHA-256
  `787AA4D96CFE89DE03E480E2EC988F59A83F56D750F6AF688E3B81497B98E781`，定位率 1.0、门通过；
  连续性正/负路径由多样本测试覆盖。
- 定向回归 `76 passed, 2 skipped`；JUnit 12,991 bytes、SHA-256
  `D8334CC318D597183C107884622839B7866BC516DD21170B70AC65885141EBE5`。两个 skip 都是 Windows
  当前权限下的符号链接创建测试。
- parity 为 9,571 bytes、SHA-256
  `FCEE7CDC23D14E49017CC5EAF8D046021B785F4526BF6D704D458BACA94F0DEB`；UI contract 为
  `DB582F452060E8AD33717DA4B4147BBB05168555AB2D182EE61A3327D2D08127`，其余四组未变。
- 派生证据和 E7 已按 v164 parity 重算。现场仍是 `192.168.1.13/24` 且三端点超时，E7 保持
  `gate_passed=false / release_allowed=false / 31 blockers`。本轮未重链 EXE，也未提升人工、硬件、
  室外 GNSS、HIL/REAL 或实飞状态。
- 最终全量回归 3,693 tests：`3689 passed, 4 skipped, 0 failed, 0 errors`，耗时 208.55 秒；JUnit
  539,819 bytes、SHA-256 `B325B2157890E27BCD71EF2DEAF29DE61DB769918E9C72ECDE9C8AA0B3AA83C8`。

## v163 本轮已收口

- 新增闭合的 `target-observation-diagnostics.schema.json` 与严格离线 verifier；仓库 wrapper 和安装后的
  `multidetect-target-observation-diagnostics-verify` 使用同一实现。输入限制为普通非符号链接文件、最大
  1 MiB，并拒绝重复 JSON key、`NaN/Infinity`、指数溢出得到的非有限数和未知字段。
- verifier 不信任 producer 的派生结论：会重算三路 `producerEpoch/sequence/sentAtMs` 原子身份、源年龄、
  本地接收年龄、`status_fresh`、坐标省略和定位条件。结构有效的 fail-closed/启动期快照仍可留档；使用
  `--require-locatable` 时不可定位快照返回非零，且任何失败都保持飞控和物理释放为 false。
- C++ 原生 self-test 新增 `--emit`，从实际诊断实现原子写出 JSON。该文件为 2,164 bytes、SHA-256
  `10CD3085B24130FA32965A063378EB49B3BFDFF02230B88F86FABB5E76A15E39`，仓库 verifier 与安装后 CLI
  均通过 `--require-locatable`；验证报告 746 bytes、SHA-256
  `F29C25BD3EFE969E17701ABE39D40773C42C1BF0E3B9B1D8B95FE4800E28BB61`。
- 定向回归为 `63 passed, 1 skipped`；skip 仅为 Windows 当前权限不允许创建测试符号链接。JUnit 9,974
  bytes、SHA-256 `7B64B69FE49432E3BF6036F965AC6F0C645E10CD64574A31F2D88E2D226AA330`；Ruff、
  compileall 与 diff check 通过。
- 当前 parity 为 9,362 bytes、SHA-256
  `C5E7ACF50EE3EF1E7B7FF377F04FA767B3240CBB21A0B16E65416BC94BBD9ECC`；UI contract 为
  `A437B8855BD6570F31C21C66AB85CF25661318CEAE62A61B58720550EF66A75A`，其余四组 hash 未变。
- external join/revalidation、response matrix、6 正例/9 故障 campaign、只读现场探测和 E7 已按 v163
  parity 重算。现场仍为 `192.168.1.13/24`，Jetson/GR01/RTSP 超时，`hardware_link_ready=false`；
  E7 保持 `gate_passed=false / release_allowed=false / 31 blockers`。
- 本轮没有修改或重链主 QGC，portable EXE 继续使用 v162 已验证的 46,704,640 bytes、SHA-256
  `ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`。人工视觉、室外 GNSS、
  HIL/REAL、真实硬件与实飞状态没有升级。
- 最终全量回归 3,679 tests：`3676 passed, 3 skipped, 0 failed, 0 errors`，耗时 194.79 秒；JUnit
  536,802 bytes、SHA-256 `F9FCCF6AF34849B7BF8D24F3E1F628C1C06771241DAEF2608309C0693629B990`。

## v162 本轮已收口

- Fly View 新增“目标定位诊断 · 只读”原生弹窗；打开时冻结当前诊断 JSON，避免阅读期间由实时状态变化
  拼出混合快照。机器 reason code 映射为中文现场原因，并显示原子身份、目标/源帧、测距与 GNSS
  质量、源年龄和本地接收年龄。
- 坐标显示继续由诊断文档的 `coordinates_included` 单向控制；失败状态明确显示“不可定位 · 坐标已强制
  省略”，不会从 QML 状态旁路恢复经纬度。弹窗可复制或原子保存同一 schema JSON，保持 advisory-only。
- Release/portable EXE 字节一致：46,704,640 bytes，SHA-256
  `ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`；offscreen simple boot
  退出 0，stdout/stderr 均为 0 bytes，无 QML 初始化错误。
- v162 parity 为 9,177 bytes、SHA-256
  `4EEFE598F7BEDCA631A0583DC1AB5AB0494A9C2BB89FCD98043E3C76A52E6D6F`；UI contract 更新为
  `C230725F927941B102AC3FEE6A24D7A2A6D1E1A00117A8B5B86ECA4AB4AE8829`，其余四组 hash 未变。
- 外部三机型 join/revalidation、response matrix、6 正例/9 故障 campaign 与 E7 已按 v162 parity 重算。
  只读现场探测仍为 `192.168.1.13/24`，Jetson/GR01/RTSP 超时，`hardware_link_ready=false`；E7
  正确保留 `false / false / 31 blockers`，人工视觉、室外 GNSS、HIL/REAL、真实硬件和实飞均未通过。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 272.67 秒；JUnit
  533,660 bytes、SHA-256 `0D16ED23B6849934FAF176BD6039BFAACAE94B0D11A3066EA1968F1015C41C56`。

## v161 本轮已收口

- 新增 `MultiDetectTargetObservationDiagnostics`：把 range、target geolocation、GNSS、producer epoch
  摘要、三路原子 envelope 身份、源年龄和本地接收年龄汇总成
  `multidetect.target-observation-diagnostics.v1`；Fly View 卡片可一键复制或原子保存。
- 定位诊断严格复核三路同 `producerEpoch/sequence/sentAtMs`、同目标/源帧、合格 GNSS、合格测距、
  有效坐标/1σ 以及 1500 ms 源/接收新鲜度。失败快照仍保留结构化原因供现场排错，但强制
  `locatable=false`、`coordinates_included=false`，并删除经纬度与 1σ，不能形成假落点。
- 新原生 self-test 覆盖有效原子三元组、GNSS 不合格、sequence 错配、接收过期、失败快照坐标删除和
  原子文件保存。连同 operator protocol、target evidence、producer epoch、44-fragment depth-grid 和
  RTSP wire，共六项原生 self-test 通过；focused QGC/parity 45 项、Ruff lint/format 通过。
- Release/portable 主 EXE 为 46,702,592 bytes，SHA-256
  `729BEDA95DD42E70538D854181113BD09D2414EED4D787AAA11529D1F55C05A0`；portable offscreen simple
  boot 退出 0，stdout 0 bytes，stderr 220 bytes、SHA-256
  `B7E0728E90A02F3FE1FF8FC9C1CB46B53BA296050981F4F8975A32C27FCA03A3`，无 QML 初始化错误。
- `release-parity.json` 为 9,177 bytes、SHA-256
  `72840CB9F1D3FE85A3509087B5331F4B11EDDBF131CB772919CC38CAC94471BC`；UI contract 更新为
  `3BC3C5B2E983A0D25F385FFAAEDEA163C83640D750684DEAA0F3853C7D282DB1`，core/task/safety/PX4 未变。
- 派生 SIM/PX4 证据与 E7 已按最终 v161 parity 重算。最新只读现场探测仍为
  `192.168.1.13/24`，不在预期 `192.168.144.0/24`；Jetson/GR01/RTSP 三路超时，
  `hardware_link_ready=false`。E7 正确保留 `false / false / 31 blockers`；人工 Fly View、室外 GNSS、
  HIL/REAL、真实硬件和实飞均未通过。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 273.29 秒；JUnit
  533,660 bytes、SHA-256 `8443ED203DC647B65EF0D7C4723FD5022BB791B02E746056D021635A756F929C`。

## v160 本轮已收口

- 新增原生“生产者 epoch 诊断”弹窗，以最新事件优先的方式展示最多 64 条生命周期/事故事件，并明确
  显示健康摘要、total/retained/dropped、当前 epoch、三类累计事故计数和最近切换/事故时间。
- 事件 journal、诊断 JSON 和文件保存抽为 `MultiDetectProducerEpochDiagnostics`；卡片和弹窗均可一键
  复制 JSON 或保存 JSON。保存使用同目录临时文件 + flush/close + rename 的原子提交，成功文件位于
  `AppLocalDataLocation/diagnostics`；失败原因进入现有 operator error 状态。
- 新原生 self-test 覆盖零事件 `WAITING`、64 项上限与 dropped 计数、newest-first、JSON round-trip 和
  原子保存。连同 operator protocol、target evidence、44-fragment depth-grid 和 RTSP wire，共五项原生
  self-test 通过；focused QGC/parity 回归 45 项与 Ruff 通过。
- Release/portable 主 EXE 为 46,665,216 bytes，SHA-256
  `DE323538B410D6264825B071A083620471A4D726921FD25F10CB859476DB1BAE`；portable hidden/offscreen
  simple boot 退出 0，stdout 0 bytes，stderr 220 bytes、SHA-256
  `19AD74FE74B1C4A09CB408506D7523CFF73B9B1134157A8180820CBF8AC4054F`，无 QML 初始化错误。
- `release-parity.json` 为 8,992 bytes、SHA-256
  `B5CCB1AA12CEB4D661F940D5FE814A5A6F21EEC9DFD861C1D8DE4EC56F139E2C`；UI contract 更新为
  `02033DDF7414F042DCC47AF0B02E175C9C620BF3C4FC1F679028F2CFD7C5093A`，core/task/safety/PX4 identity 未变。
- external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 与 E7 已按最终 v160 parity
  重算并复验。只读现场探测仍从 `192.168.1.13` 错路由，Jetson/GR01/RTSP 3/3 超时；E7 仍为
  `gate_passed=false`、`release_allowed=false`、31 blockers。人工 Fly View、室外 GNSS、真实硬件、
  HIL/REAL 与实飞仍未通过。
- 修正前的第一次全量运行暴露 3 个暂态失败：进度 JSON 与 terrain truth 仍绑定旧 parity；重新生成 parity、
  重建 portable、重绑派生证据并更新文档后，最终全量回归 3,662 tests 为
  `3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 292.93 秒。JUnit 533,634 bytes、SHA-256
  `F63533BB049166A87F9109C8B6E6B19C30673C4069210D5285D7864755776DA6`。

## v159 本轮已收口

- QGC controller 新增 producer epoch 生命周期/事故时间线：初始化、生产者重启、退休旧包拒绝和切换容量
  耗尽均记录事件索引、发生时间、incoming/previous/active epoch、健康级别/原因与累计事故计数。
- 时间线最多保留 64 条，并分开记录 total/retained/dropped；诊断环形淘汰不影响 32 项 retired epoch 的
  绝不淘汰安全状态机。“目标落图”卡片可一键复制 `multidetect.producer-epoch-diagnostics.v1`，零事件
  `WAITING` 状态也可以导出；JSON 固定声明 advisory-only、飞控关闭和物理释放关闭。
- focused QGC/parity 回归 45 项、Ruff lint/format、三项关键 native self-test 和 portable offscreen
  simple boot 均通过。Release/portable 主 EXE 为 46,659,072 bytes，SHA-256
  `7DA7F882ECB51EEA4DDF1D79FF5F9EBB52E8BD3B77F0DBEEAC1B23EFFB95E375`；功能提交为
  `1e95d6eadcb06dadb5970682dcd442a0a2c9c03f`。
- `release-parity.json` 为 8,815 bytes、SHA-256
  `2FB2AE51AD9E5D5011D51C9F27379936F811C3A371EA76D82334B735B8024CAE`；UI contract 更新为
  `049B1A6B1C26F80D2627DA7DB9DE2A8D733EDE1B3E4DAC972F27D411065505F4`，core/task/safety/PX4 identity 未变。
- v159 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign、只读 field-link 与 E7
  已按当前 parity 重算。控制机仍从 `192.168.1.13/24` 错路由，Jetson/GR01/RTSP 3/3 超时；E7 保持
  `gate_passed=false`、`release_allowed=false`、31 blockers，未修改网络、飞控、载荷或受保护服务。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 342.548 秒；JUnit
  533,635 bytes、SHA-256 `C96C2069787576D8A2118A8C16A1D691E54AC6259A14E76ABEB7A6DCF1B2A139`。
  人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞仍未通过。

## v158 本轮已收口

- QGC controller 将 producer epoch 健康锁存为 `WAITING / HEALTHY / NOTICE / WARNING / CRITICAL`；
  `MultiDetectState.qml` 投影健康级别、原因和最近事故时间，“室外测距质量”以中文和颜色直接区分状态。
- `MultiDetectObservationEpoch` 的 retired 集合改为固定 32 项且不淘汰。容量耗尽时返回
  `CapacityExhausted`，不切换 active epoch；原生测试覆盖填满容量、第 33 次切换 fail-closed，以及最早
  retired epoch 仍不能复活。该保护要求 QGC 重启后才能接受更多 session 切换。
- 目标证据升级为 `multidetect.target-evidence.v6`，持久化健康级别/原因、三类计数、切换与事故时间；
  v1-v5 只读兼容，字段类型或级别/计数/时间不一致的 v6 记录 fail-closed。
- focused Python 回归 144 项通过；operator protocol、target evidence store、replay RTSP wire 三项关键
  native self-test 均退出 0。Windows Release/portable 主 EXE 均为 46,650,880 bytes，SHA-256
  `8EBAB7D38113418582AAC12A226BBDFAF988EF30D3E7FCC927222B58E2332A69`；功能提交为
  `3c04724d91ca40a7ce287d214a32995fbbedc8b2`。
- portable hidden/offscreen simple boot 退出 0；stdout/stderr 均为 0 bytes，无 QML 初始化错误。
- `release-parity.json` 为 8,815 bytes、SHA-256
  `DAD414BDEF9B25EBF770A0CC9FF1EF4068914916DF2D8FD496282629BB8F2D37`；UI contract 更新为
  `74F616ABFC39A52AAF189A5030F2215F955EF426765C219E3CB5EF28CE46AFD3`，core/task/safety/PX4 identity 未变。
- v158 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 与 E7 已按当前 parity
  重算。只读现场探测仍从 `192.168.1.13/24` 路由，Jetson/GR01/RTSP 3/3 超时；E7 仍为
  `gate_passed=false`、`release_allowed=false`、31 blockers。未修改网络、飞控、载荷或受保护服务。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 264.284 秒；JUnit
  533,660 bytes、SHA-256 `331FD13D6E928168856ACB7C2C0D31EC0CAF56F49A733AEFB4ABFAEE6A7AAAE9`。
  人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞均未通过。

## v156 本轮已收口

- 类型 `26` 新增必填非零 `producer_epoch`，紧凑 body 扩展后整个 application payload 恰好 128 bytes，仍为
  单个 MAVLink `TUNNEL`。`LiveOperatorBridge` 每次启动默认生成新的随机非零 epoch，同一进程发布的 range、
  target geolocation 与 GNSS 原子三元组共享它。
- QGC 新增 `MultiDetectObservationEpoch` 状态机。新 epoch 到达时先原子清空旧三类可见状态、时间戳、source
  replay guards 与类型 10/22/25/26 sequence gates，再采纳新三元组；旧 epoch 被记为 retired，迟到包永久拒绝。
  原生回归覆盖 producer A、切换 producer B、迟到 A 被拒且 B 仍保持 active。
- QML 要求 range/geolocation/GNSS 同时具有相同的正 epoch、sequence 与 `sentAtMs` 才允许落图/保存；新证据为
  `multidetect.target-evidence.v5` 并保存 `producer_epoch`，v1/v2/v3/v4 只读兼容。变化仍是 display/advisory-only。
- focused Python/QGC static 回归 133 项通过，Ruff 通过；operator protocol 与 target evidence store 原生自测通过。
  Windows Release 与 portable 主 EXE 均为 46,639,616 bytes，SHA-256
  `8BDDA7ABB9DF806C5B18DA0CFDD4169CFF1A556DD062B3784F6A09D8074DACF5`；功能提交为
  `cc0e3f0412b64c6b4450f1496bb1a4f71654f30d`。portable hidden/offscreen simple boot 退出 0；stderr
  1,009 bytes、SHA-256 `A51779C621AEDD42B2D38FA63C07B6F4D7A77240784DCFAECF3AD1DBA71F9873`，无 QML 初始化错误。
- `release-parity.json` 为 8,815 bytes，SHA-256
  `CEB14C70D5FB37085534B62C703DD76C305F4A9C866BD855250932056FF44BB1`；UI/task contract 更新为
  `431E8279302C88283718C7722FCC71BAFC9500AF0A5730B290EFBDEF55F62D54` /
  `7F8721C1561F5CBEB7A3CC41A7CDE2FACE1B58F1EEE8B33976A5F35AEF8BCD5C`，core/safety/PX4 identity 未变。
- v155 派生文件在 parity 变化后保留为历史证据；v156 external join/revalidation、三机型 response matrix、
  6 正例/9 故障 campaign 与 E7 已按当前 parity 重算。最新 E7 仍为 `gate_passed=false`、
  `release_allowed=false`、31 blockers；人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞均未通过。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 310.24 秒；JUnit
  533,660 bytes、SHA-256 `79A05CF85681A6964998A77B04F7AEE0B6B0E193D18FFA5A595FF8286ADE9445`。

## v155 本轮已收口

- 新增认证 operator protocol 类型 `26` `TARGET_OBSERVATION_STATUS`，把 range、target geolocation 与
  `GNSS_STATUS` 压入一个 124-byte application payload，可装入单个 MAVLink `TUNNEL`。三者必须共享
  `produced_at_s`，range/geolocation 的 target、frame 与 capture identity 必须一致；可用坐标必须绑定
  qualified GNSS。缺失、错帧、身份或质量矛盾、编码/发送失败均整包零发布，指纹不推进并保留重试。
- QGC C++ decoder 在认证、重放、sequence、source freshness 之后验证类型 26 的全部 registry、范围、
  坐标、GNSS canonical quality 与 fail-closed 不变量；controller 只会一次性采纳三个嵌套 map，并给它们
  注入同一外层 sequence/`sentAtMs`。QML 保存门要求三者均标记 atomic 且 sequence/发送时刻完全相等。
- 本地目标证据升级为 `multidetect.target-evidence.v4`；新写入只接受类型 26 原子三元组，v1/v2/v3
  保持只读兼容并在历史卡片区分旧语义。该观测固定 display/advisory-only，不启用飞控或物理释放。
- selected 回归、Ruff、Ruff format、compileall 与 `git diff --check` 通过；operator protocol、target
  evidence store、44-fragment depth-grid Python→C++ HMAC roundtrip、replay RTSP wire 四项原生自测均退出 0。
  portable `--offline --simple-boot-test` 退出 0；日志 1,001 bytes、SHA-256
  `131508B6705306BD09D6F8889CEDE9B30DCDB37B1526BD742EA2CF1DFB3EC220`，无 QML 初始化错误。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 280.19 秒；JUnit
  533,634 bytes、SHA-256 `45E3B03A225840ED747520006B7B458F7F533A7AA0489941540C28FDB085ECF0`。
- Windows Release/portable EXE 为 46,633,984 bytes，SHA-256
  `788E82B4BF8198EFC8ECD720E2E1A883899D7ADD65DE1276A0221FD61D35AC8F`。功能提交为
  `dc801caca24ff56b7eeb22c330c465e955562e34`。
- `release-parity.json` 为 8,735 bytes，SHA-256
  `289FD7BD12E12B3931D5AEDD3B5C94A34EF5559480F338B6AA139C9D07184CFF`；UI/task contract 为
  `3BA8008A73ED19BD9C5B774A322989A841263B947A395C977A660C55176BEBD7` /
  `2CDB5814C09DFAD6609B60C71AE9AFFE1AD5FFCEE63F5E4964590799F34C254B`，其余三组身份未变。
- 当前 parity 的 external join/revalidation、三机型 response matrix 与 6 正例/9 故障 campaign 已重算通过。
  新鲜只读 field-link 仍为 `192.168.1.13/24` 错路由且 Jetson/GR01/RTSP 3/3 超时；E7 manifest/report
  SHA-256 分别为 `97CE95947B99F2FC0D9633B53A9347ECEB237EE1467A9DB052A2E7A7FDAD6767` /
  `996F595DE8E07EB41F63A1EDB887637DC8ABB0B5ACD6A5197D597C549E93CCEF`，结果保持
  `gate_passed=false`、`release_allowed=false`、31 blockers。没有修改网络、受保护服务、飞控或载荷；
  室外 GNSS、人工视觉、真实硬件、HIL/REAL 与实飞均未声明通过。

## v154 本轮已收口

- Jetson operator bridge 在目标定位到期时把 `GNSS_STATUS`、range 与 target geolocation 作为同一生产帧组
  校验：三者必须存在且 `produced_at_s` 精确相同，可用目标定位还必须配合 qualified GNSS；缺失、错帧或
  质量矛盾均 fail-closed。发送顺序固定为 GNSS → range → target geolocation，只有三条全部成功才推进
  指纹，部分发送失败会保留整组重试。独立 GNSS 在无目标定位或绑定错误时仍按 cadence 发布。
- QGC controller 仅在认证、重放和 sequence 门通过后，把 target geolocation 自己的 envelope sequence 与
  `sentAtMs` 注入状态。保存门要求 target 与 GNSS 的 `sentAtMs` 精确相等；新 JSONL 使用
  `multidetect.target-evidence.v3`，同时保存两侧 provenance。v1/v2 保持只读兼容，历史卡片分别标明无质量
  证据、仅同时间质量证据或 v3 同帧绑定。三消息 transport 本身仍明确不声称原子事务。
- focused 回归 `119 passed`，Ruff 与 `git diff --check` 通过；native operator protocol、target evidence store、
  depth-grid 44-fragment Python→C++ HMAC roundtrip、replay RTSP wire self-test 全部退出 0。
- 最终全量回归 3,659 tests：`3657 passed, 2 skipped, 0 failed, 0 errors`，耗时 287.23 秒；JUnit
  533,204 bytes、SHA-256 `A0A328B7814B21C22ECFDCB8530BF097B2CBB00F2BCDD53CF3EBAB68F2132DEC`。
- Windows Release/portable EXE 为 46,603,264 bytes，SHA-256
  `02522C5661803381FD3B8E4E54267CC6BCD17B9769C1C8E2BE00894EBB470CB2`；hidden simple boot 退出 0，
  日志只有既有 Qt FFmpeg 与 point/pixel-size warning，没有 QML 初始化错误。
- `release-parity.json` 为 8,735 bytes，SHA-256
  `83B6B8202924C97A650E8F91253E4CF7020355C6ADAC770B39D6B314A21AA1EE`；UI/task contract 分别更新为
  `AE570E81FE7A31E695D3659869B6B77621CD3052306B260ADC855F55A4BFB1C1` /
  `DA902E0E0E4011545C02C04043369BB02E576AF524CEAD77D2D7858BF82C3CDC`，其余三组身份未变。
- 当前 parity 的 external join/revalidation、三机型 response matrix 与 6 正例/9 故障 campaign 已重算通过；
  新鲜 field-link 仍是 `192.168.1.13/24` 错路由且 Jetson/GR01/RTSP 3/3 超时。E7 manifest/report SHA-256
  分别为 `E2BB25233C8FA77D9945718670FBA3E8CD72E1B7EDD41474FD897E4EC5DF6E8E` /
  `73FE81647CA63AB67DF04F360630D0EE8646AE13B7287E13DF73EE1E3EF0A075`，结果保持
  `gate_passed=false`、`release_allowed=false`、31 blockers。没有修改网络、受保护服务、飞控或载荷；
  室外 GNSS、人工视觉、真实硬件、HIL/REAL 与实飞均未声明通过。

## v153 本轮已收口

- 原生 QGC 的本地目标证据从 `multidetect.target-evidence.v1` 升级到 v2。保存按钮和 C++ durable store
  同时要求：目标坐标/1σ 合格；独立 `GNSS_STATUS` 已认证且状态合格；fix `>=3`、卫星 `>=6`、
  H/V accuracy 分别 `<=6/10 m`、position healthy、reason 为 `gps_qualified`、来源年龄 `<=1.5 s`。
  任一字段缺失、类型错误、非整数 sequence/时间或越界都在写文件前 fail-closed。
- controller 只在原有认证、replay、sequence 门通过后把 envelope `sequence` 与 `sentAtMs` 注入 GNSS
  诊断状态；v2 快照保存这两个字段及 fix、卫星、H/V accuracy、health、reason、source age。它们是与
  坐标快照同时保存的原始诊断 provenance，不把两个独立消息夸大为原子采样绑定。
- 旧 v1 JSONL 保持可读，历史卡片明确显示“旧版 v1 记录，无独立质量证据”；新 native
  `MultiDetectTargetEvidenceStoreSelfTest` 覆盖 v1 兼容、弱 GNSS/字符串布尔零写入拒绝、合法 v2 写入和
  v1/v2 混合重载，退出码 0。operator protocol 与 replay RTSP wire self-test 也退出 0。
- focused QGC 静态回归 `21 passed`，Ruff、compileall 和 `git diff --check` 通过。功能提交为
  `fe39400fc12d0de4e4e57695b465d140303b1296`；最终全量回归 3,658 tests：
  `3656 passed, 2 skipped, 0 failed, 0 errors`，耗时 344.610 秒；JUnit 533,070 bytes、SHA-256
  `5D5A078561A479D9AF278D27D049F202535D03016DB9913E95274018CCC38DFA`。
- Windows Release/portable EXE 为 46,600,192 bytes，SHA-256
  `8225BF9F41426E473F23BC74BF038E576A88EA3556E1A7DD883F1B6C036909BB`；hidden simple boot 退出 0，
  stderr 只有 Qt FFmpeg 与 point/pixel-size warning，没有 QML 初始化错误。
- `release-parity.json` 的 UI contract 更新为
  `6230337df9585b90b256ae64ad016b2397a9f51ddacbaf1bae99acac1c66d14e`；其余四组身份未变。当前 parity
  的 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 均重新派生并通过。
- 新鲜 3 样本 field-link 只读复查仍为 `192.168.1.13/24` 路由到目标，Jetson/GR01/相机全部超时；
  `hardware_link_ready=false`。E7 manifest/report SHA-256 分别为
  `71B515320E147F580E84E2AAE20B214B3028642EADF8568BE0FCE757D0F4C45D` /
  `B05C4C5BECE818BEF9DDBD66C389C32059A9695E09DD951E7559886DF99195A9`，结果保持
  `gate_passed=false`、`release_allowed=false`、31 blockers。网络配置、受保护服务、飞控和载荷均未修改。

## v152 本轮已收口

- 新增独立 `GNSS_STATUS`（operator wire type `25`）：Jetson 在目标尚未选择、range 尚未形成时也按默认
  `5 Hz` 发布 GNSS position health、fix type、卫星数、水平/垂直精度、样本年龄和 canonical reason。
  消息刻意不含 latitude/longitude，避免诊断面变成另一条坐标来源。
- domain/codec 对 `fix>=3`、卫星 `>=6`、水平精度 `<=6 m`、垂直精度 `<=10 m`、年龄 `<=1.5 s`
  和 position healthy 使用与 `AdaptiveRangingPolicy.assess_gps_quality` 相同的 fail-closed 语义；伪造
  qualified 的弱 fix 在 Python 和 QGC C++ 两端均拒绝。
- live bridge、MAVLink tunnel 与 UDP session 完成独立 cadence/sequence/peer-change 接入；构建失败只丢弃
  可选诊断包并写 audit，不阻断感知主循环。Jetson 部署验证也检查 type 25 encoder/cadence 是否存在。
- 原生 QGC controller 只在认证、重放和 sequence 门后采纳状态，1 秒无新包即清空；Fly View toolbar
  “室外测距质量”新增 GPS/GNSS 与 GNSS 详情，即使无 target 也能显示精确不可定位原因、fix、卫星数、
  H/V accuracy 和年龄。该链固定 display-only，不开放飞控或物理投放。
- 相关 277 项 Python/QGC 静态回归通过；全仓回归为 `3656 passed, 2 skipped, 0 failed`（3658 tests，
  374.669 s）。Ruff、Ruff format、compileall 与 `git diff --check` 通过；native operator protocol
  self-test 和 replay RTSP wire self-test 均退出 0。release parity 已按当前源码重新生成。
- Windows Release 与 portable EXE 均为 46,587,392 bytes，SHA-256
  `2686D5EFAAB375E08C41C5AD8050C0BB9807C33309F52D1102AB774E96EF61A9`；portable hidden simple boot
  退出 0。构建物路径为
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`。
- 本轮只关闭软件链“选目标前看不到原始 GNSS 质量”的缺口；人工 Fly View、室外 GNSS 精度、真实
  Jetson/相机/Pixhawk/GR01、HIL/REAL 与实飞仍为 `not_claimed`，E7 继续为 31 blockers。
- 当前 parity 的 external join/revalidation、三机型 response matrix 和 6 正例/9 故障 campaign 已重新
  派生并全部通过；随后用新鲜只读 field-link 报告重算 E7。manifest SHA-256
  `678755799EB890BD650D955F2B811ED2E453FA3D75B4C4C3374E55F729157A82`，report SHA-256
  `A5947B5B9ED0837C2D65161A18CD83D26FCA06D5FE67D27B135377AF4EC50876`，结果仍是
  `gate_passed=false`、`release_allowed=false`、31 blockers。

## v151 本轮已收口

- 对当前台式机执行 3 样本、协议级、路由级 field-link 只读预检。18,143-byte 报告 SHA-256
  `976B0F3E17214274EC79E62F556F5BB00A8F89A37BDC02A7D088B9893269ACC3`；4,378-byte 接口诊断 SHA-256
  `380C2B9F7C246A42A95DB14F7285B53B0B0C44233D04EB054A0508511D43BA95`；独立 verifier 以
  `--allow-unready` 退出 `0`，证明报告结构与哈希链可读，不代表硬件 ready；
- 3/3 样本的 Jetson SSH、GR01 MAVLink 和相机 RTSP 传输/协议探测全部超时；操作系统为三个端点选择的源地址均为
  `192.168.1.13`，不属于要求的 `192.168.144.0/24`。因此 `route_checks_passed=false`、
  `hardware_link_ready=false`，接口诊断也明确 `selection.status=missing` / `candidate_count=0`；
- 用该新鲜现场失败证据和 v150 当前 parity 派生证据重绑 E7：manifest SHA-256
  `9B5C614BE53793E15D5086FA44301BC99530E39DD791D662FFBF642FF055718D`，report SHA-256
  `BD102087CEA1095796A188C79D0A467847BCE987347D6C945264FACC35321D5B`。E7 仍为
  `gate_passed=false` / `release_allowed=false`，阻断项从 32 降至 31；只消除了旧现场报告过期阻断，
  `real_field_link.passed=false` 没有提升；
- 本轮没有修改网络配置、飞控或载荷，没有修改 C++/QML，因此无需重建 EXE。版本仍为 `0.3.0-pre.8`；硬件、
  HIL/REAL、室外 GNSS 与飞行验收继续保持 `not_claimed`。

## v150 本轮已收口

- 新增 GR01 review trust registry schema v1、实现与 repository wrapper；registry 用独立渠道保存的文件 SHA-256 作信任锚，
  为 `source_operator` / `reviewer` 登记 principal ID、identity、key ID、Ed25519 公钥、全局/逐 key 有效期及吊销状态；
- portable review schema 升级到 v5；`review` / `verify` 不再接收裸公钥作为信任根，而是重开同一 registry 并核对包外
  SHA-256。operator key 在 source attestation 时点和 review 时点都必须有效，reviewer key 在 review 时点必须有效；
  未来 attestation、过期/吊销 key、identity/key/public-key mismatch 以及角色复用均 fail-closed；
- clean feature commit `fb5e8e4069a35dc53b010c48e4ee6c1bc70084f4` 的 trust-registry + review 软件 smoke
  `22 passed`；JUnit 3,267 bytes、SHA-256 `771C2CBB3DABEF6DBE14BDBFF7DA867D2D2E4D867A6D6F202EEF0A29203369BF`；
- safety parity 更新为 `E179F406...FE0030DE`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  已按当前 parity 重算并通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,652 tests：`3650 passed, 2 skipped, 0 failed, 0 errors`，耗时
  276.482 秒；JUnit 532,203 bytes、SHA-256 `213DC40ADE45E01502E96B8E284DBE1E8FB77068819D105B45A3745467FE601B`。
  真实 registry 分发、真人身份映射、第二机复核、现场 GR01 session、HIL/REAL 与飞行验收仍待完成。

## v149 本轮已收口

- 新增 source attestation schema v1：现场 operator 用独立 Ed25519 key 签署 inventory 的 bytes/SHA-256/canonical hash、
  bundle ID 与 bundle-content hash；seed 只从环境读取，detached attestation 强制写在 inventoried session 目录外；
- portable review schema 升级到 v4：第二机必须用包外预登记 operator 公钥验源，再由 reviewer key 签署 review；operator/
  reviewer 的 identity、key ID、公钥三项必须全部分离，错误 operator 公钥、source signature 篡改和角色复用均零输出失败；
- claims 只证明两把指定 key 的 possession、source inventory 和 review receipt 完整性；两个人的现实身份仍固定为未验证，
  target environment、完整 E7、网络、硬件、HIL/REAL 与飞行验收均未被软件 fixture 提升；
- clean feature commit `4e3585c74857a2f156e1baab3f797418218cb95f` 的双签名软件 smoke `6 passed`；JUnit 1,127 bytes、
  SHA-256 `7CFE1EFE6F145665422C548A01BF4FBF3AF983BDF51D90705995821DE61A324F`；
- safety parity 更新为 `B900403A...936B6F35`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  已按当前 parity 重算并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,645 tests：`3643 passed, 2 skipped, 0 failed, 0 errors`，耗时
  287.945 秒；JUnit 531,218 bytes、SHA-256 `931052CC4487CBD16DD9CFE9C8676D6271761ABD2CA0CE350BAB485823E61050`。
  真实 operator/reviewer 公钥登记、真实第二机双签和现场 GR01 session 仍待完成。

## v148 本轮已收口

- portable review schema 升级到 v3：第二机必须从环境读取 32-byte Ed25519 seed，receipt 绑定 reviewer、key ID、
  public key、signed-payload SHA-256 和 64-byte signature；私钥不会进入命令、回执或日志；
- 独立 `verify` 不信任 receipt 内嵌 key，强制使用包外固定公钥；错误公钥、signature tamper、claim tamper、缺失或
  非法 seed 均 fail-closed，其中 key 错误在任何 transfer/review receipt 写入前零输出；
- claims 明确 `reviewer_key_possession_verified=true`、`reviewer_human_identity_verified=false`，只证明密钥持有和
  回执完整性，不伪造真人身份、target environment、完整 E7、网络、硬件或飞行验收；
- clean commit `1bcd492d08e1e9a61b1b93659a6f4a3c376800bf` 的软件签名 smoke `5 passed`；JUnit 1,009 bytes、
  SHA-256 `F93E70227DA0C83BEE6F24DB96A81E012C46C4A88A1A5E97ED26702208A9BF2F`；
- safety parity 更新为 `F86FF499...97FA569F`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  已按当前 parity 重算并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,645 tests：`3643 passed, 2 skipped, 0 failed, 0 errors`，耗时
  270.522 秒；JUnit 531,218 bytes、SHA-256 `144ACD03F4D678CD9E9A9D6E3631570D438D993F7934A3D054F6AA2076A25209`。
  真实 reviewer key 登记、真实第二机签署与现场 GR01 session 仍待完成。

## v147 本轮已收口

- portable review schema 升级到 v2，`review` 必须显式接收独立渠道保存的 inventory SHA-256；实际 inventory
  哈希在任何 transfer/review receipt 写入前核对，mismatch 时零输出并 fail-closed；
- receipt 绑定 expected/actual inventory SHA-256 与 `inventory_hash_matches_expected=true`，独立 verifier 重算；
  现场 producer 在 snapshot 后打印可信哈希，必须与 session 复制介质分开保存才构成包外信任锚；
- 该 SHA-256 不等于数字签名或操作者身份认证，也不重验目标环境、完整 E7 传递来源、网络或硬件。clean commit
  `089028559e827e63dbb2008536920ccf0c2c6a2c` 的软件 smoke `2 passed`；JUnit 498 bytes、SHA-256
  `94231E01A2881164580802F3BC6BD95E68D8185C19B891EA007A5E3FB1C4F39B`；
- safety parity 更新为 `8BA76D61...ADBD4A0D`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  均重算并独立通过。E7 保持 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,643 tests：`3641 passed, 2 skipped, 0 failed, 0 errors`，耗时
  272.363 秒；JUnit 530,841 bytes、SHA-256 `8DA38949FA44806ABE294DBA2FC051500557DBD75CB83A3B9A09CE7B19DC903E`。
  真实 GR01 session、独立渠道哈希传递、第二机 review/签字仍待现场完成。

## v146 本轮已收口

- 新增 `gr01_release_review.py`、repository wrapper 与 closed review schema：现场 final record 先经过原有强 verifier，
  再把 plan、handoff、bench、E7 manifest/report 与 record 共 6 文件写入逐字节 transfer inventory；
- `run_gr01_release_session.ps1` 只有在五源 record 独立验证成功后才执行 `snapshot`，因此输出目录直接具备跨机搬运清单；
  inventory/transfer/review 输出均 write-new，复制包拒绝增删改、symlink、路径逃逸和 case collision；
- 第二机 `review` 不依赖 origin/target 绝对路径或现场 secret：重开五源 hash/bytes，执行 portable handoff verifier、
  GR01 bench verifier，核对 plan→handoff→bench/E7 配置、target Git/parity 投影和 manifest/report GR01 绑定；专门回归
  证明单独篡改 record projection 后即使重建通用 inventory，内部五源投影不一致仍 fail-closed；
- review receipt 固定 `target_environment_reverified=false`、`canonical_e7_sources_recomputed=false`，并保持
  no-hardware/no-flight/no-control/no-payload。clean commit `da585dc14be11cb6f383f16b3eb06ac56349146e` 的软件跨机
  fixture 通过；JUnit 356 bytes、SHA-256 `F26FBD3750858FEDB603FC818C6354829D8DA34F009B9EC46B7E4D9AC83E225F`；
- safety parity 更新为 `2E3B5C4A...87A6A3C`；external join/revalidation、三机型 matrix、6 正例/9 故障 campaign
  均重算并独立通过。E7 保持 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,642 tests：`3640 passed, 2 skipped, 0 failed, 0 errors`，耗时
  267.29 秒；JUnit 530,699 bytes、SHA-256 `3C38DCAAA153B253080834D357BB1856073EAA4661BE037CEC0F26314348D194`。
  该 fixture 没有连接网络/硬件，不构成 GR01 或第二台真实现场机验收。

## v145 本轮已收口

- `run_gr01_release_session.ps1` 新增 `-SourcePlan`，可把开发机预生成 plan 逐字复制到现场 session；输入必须为
  regular non-reparse file，复制前后 SHA-256 必须一致，`-PlanOnly -SourcePlan` 冲突在创建 artifact 前拒绝；
- handoff consumer 新增 `match` 门：先按 `--check-environment --require-ready` 严格复验目标 source/secrets，再将
  source plan 的 endpoint、hardware ID、canonical CIDR、五项阈值、两个 secret 变量名和 configuration digest 与现场
  CLI 参数逐项对账；该门固定在 GR01 bench/UDP 前，任何 drift 均 exit 1；
- clean commit `e1a821ee7a7265979d63cfeb1dbce1da743029b4` 无网络导入 smoke：origin plan 3,059 bytes、
  SHA-256 `02F4A3C4430ECB277B56B6889A12010A8B1E5E37EC1F29DABDE1B8016B5456A2`；target handoff
  3,109 bytes、SHA-256 `1325C9F19DF0F5A51A2B20D4246A168648BD5B0D4ECFD0C3E6865EFE263ED116`；
  故意把现场端口从 14580 改为 14581 时 wrapper exit 1，目录只有 plan/handoff、没有 bench；原参数 match exit 0；
- safety parity 更新为 `64628035...517A6`；external join/revalidation、三机型 matrix、6 正例/9 故障 campaign 均重新
  绑定并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,637 tests：`3635 passed, 2 skipped, 0 failed, 0 errors`，耗时
  287.622 秒；JUnit 530,041 bytes、SHA-256 `89F04B8656D485F942AAC9B0260C2712FEE8BB1255E401EC6A09A01957C6A7F8`。
  smoke 使用临时占位 secret，仅验证软件门，不接触网络/硬件，不构成 GR01 验收。

## v144 本轮已收口

- GR01 final session 升级为 schema v3，将 source plan、target handoff、hardware bench、E7 manifest 与 E7 report
  作为五份 distinct/hash-bound source；最终 record 新增 target host/repo/Python/output、configuration digest、Git HEAD、
  source-equivalent、ready、environment-reverified 与 plan/bench/E7 configuration-match 投影；
- consumer 不再把 origin absolute paths 当目标机执行条件，而是严格运行 handoff verifier 的
  `--check-environment --require-ready` 等价语义，重开 handoff 指向的同一 plan，并对账 plan SHA/bytes、Git、release
  label、safety hash 与现场配置；origin plan 可以 non-ready，只有目标 handoff ready 才可授权执行；
- `run_gr01_release_session.ps1` 在任何 bench/UDP 前按 plan → handoff create → handoff strict verify 顺序执行，新增
  `TargetHostId`，最后显式把 `--handoff` 传给 session creator；handoff 或五源任一漂移均不能生成成功 record；
- clean commit `a4d43234fec86f1d587e3e0e62ef6de9b76c44a0` 的无网络 smoke 为 source-equivalent/structurally valid，
  两个现场 secret 缺失使 handoff non-ready；专门 relocated-origin 五源测试通过，但它使用软件 fixture，不是硬件 session；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 独立通过；E7
  保持 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,633 tests：`3631 passed, 2 skipped, 0 failed, 0 errors`，耗时 249.884 秒；JUnit
  529,356 bytes、SHA-256 `7CA6FF563DD7F2993CC364972BCF026CBC71CBF55AB588FB91778494E52B9C7C`。
  本轮不改 C++/QML，不重建 EXE，不产生网络、硬件、射频、HIL/REAL、飞行或最终发布声明。

## v143 本轮已收口

- 新增 `gr01_release_handoff.py`、repository wrapper 与 closed schema，把 source plan 转换成目标机专属、
  write-new 的 portable handoff receipt；来源 plan 可保留另一台机器的绝对执行路径，不再要求两机 checkout 同路径；
- handoff 在目标机重算 tracked-clean、Git HEAD、canonical release parity、四个执行工具身份和 parity source
  verification；只允许目标 output 位于目标 repository 内，源码或工具不同即 `source_equivalent=false`；
- source plan repository/parity/tool identity 现在逐字段闭合验证；非法 Git/SHA/path/bytes/type 在写 receipt 前直接拒绝；
- receipt 绑定 source plan 的相对路径/bytes/SHA、portable configuration digest、目标 repo/Python/output 路径、
  host ID 和两个 secret 的名称/present；不序列化 secret 值，固定 no-network/no-hardware/no-control claims；
- clean commit `3b8a9d5ac4ab9a2f4d7206dd2f25a950c76ba2b5` 的无网络 smoke 得到
  `source_equivalent=true`、结构复验 exit 0；两个现场 secret 缺失使 `ready_to_execute=false`，严格复验 exit 1。
  plan 3,021 bytes、SHA-256 `71FC66BC2F8614CA5021AE2CA9A636848475C5237A6A6A1D0E0E3D62DC86F58B`；handoff
  3,288 bytes、SHA-256 `1FF368668E757747B50D2CD18F308D3CB26E4C692A3090AAF6075B8234B8E962`；未接触网络或硬件；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 均独立通过。
  E7 继续 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,631 tests：`3629 passed, 2 skipped, 0 failed, 0 errors`，耗时 277.38 秒；JUnit
  529,121 bytes、SHA-256 `B68E93EEA55B0631B6D11179F634FB25361E53C186660276329BF0C0281025D4`。
  本轮不改 C++/QML，不重建 portable EXE，不产生硬件、射频、飞行或最终发布声明。

## v142 本轮已收口

- GR01 final session 升级为 schema v2，并将 bench 前通过的 `gr01-release-session-plan.json` 作为第四份
  write-new 来源绑定；record 保存 plan 相对路径、bytes/SHA-256、Git HEAD、release label、safety hash 和 ready/source 状态；
- session consumer 重开 plan 并运行 canonical verifier，要求 source-bound plan 本身 ready；随后逐项对比 plan 与实际
  bench/E7 的 host、port、hardware ID、source CIDR、round trips、loss、P95、retry 和 evidence max-age；
- PowerShell producer 在创建最终 record 时显式传入 `--plan`；plan、bench、manifest、report 任一漂移，或计划参数与
  实际证据不一致，都不能生成/复核成功 session；新增 configuration-drift 回归；
- 干净提交 `46845165b0d6fc870399406bcd1614defc4d71fb` 无网络 smoke 的 Git/parity/tool 来源均通过，两个
  secret 缺失使 `ready_to_execute=false`；plan 为 3,021 bytes、SHA-256
  `0C6520FB0CB71B579141BBEEE4B62044A7FEC43EDCF5090945BAA96230E3CAB5`，未接触网络或硬件；
- canonical parity 为 7,857 bytes、SHA-256
  `70FB339ACF17BB1FF61008C4B323422278F6F606C0CD2698B54982E38E1FC87D`，safety hash 为
  `0F797612E61B2ECBB798849BCD8CAE5A00EDAFDDA1936193C27B37B1039B1F51`；四份派生证据独立通过。
  E7 继续 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,623 tests：`3621 passed, 2 skipped, 0 failed, 0 errors`，耗时 159.54 秒；JUnit
  527,922 bytes、SHA-256 `1FA7FCD5F6320B819F7AAA1C0A3E833FFB99D2C5AE4BF0D0035E953B46B88C46`。
  本轮不改 C++/QML，不重建 portable EXE，不产生硬件、射频、飞行或最终发布声明。

## v141 本轮已收口

- GR01 plan 升级为 schema v2：除现场参数和 secret presence 外，新增 Git HEAD、tracked-dirty、canonical
  `release-parity.json` 来源复算，以及 producer、CLI shim、独立 verifier、plan module 四个工具的 bytes/SHA-256；
- 新增 `gr01_release_plan.py` 与 installed-tree-independent verifier。verify 会重开工具和 parity，重新计算来源身份；
  `--check-environment` 复核 secret 是否仍存在，`--require-ready` 同时拒绝 diagnostic/non-ready plan；
- 实际 session 在任何 UDP bench 之前先 write-new 生成 plan，并立即执行
  `verify --check-environment --require-ready`。源码 dirty、parity stale、工具漂移或 secret 缺失都会在网络前失败；
- 干净提交 `abca8465d53ea525b545380ff06e40ca922a81da` 的无网络 smoke 成功复核来源，但因两个 secret 均缺失而
  `ready_to_execute=false`；计划 SHA-256 为
  `2F88627DFBA4FA02014EF15B3768583BB347081BE3FE0E3A6B85D8961B5D1332`，未接触网络或硬件；
- canonical parity 为 7,857 bytes、SHA-256
  `4C62CE8B5E2A6687F9E29DEA5EC28888B4635BB508891AF7A247CE1CCC9D7F8B`，safety hash 为
  `C05D32D7EF7F37A72E03001BE86DD190D56930A48926BE97CC2B78DF9942C8F8`；四份派生证据独立通过。
  E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,622 tests：`3620 passed, 2 skipped, 0 failed, 0 errors`，耗时 155.35 秒；JUnit
  527,791 bytes、SHA-256 `05D64EBDA25BDFAA8F26DE5AA780B9070A5DD36B2AF3167231DD1C31E4DC8F35`。
  本轮不改 C++/QML，不重建 portable EXE，也不产生硬件、射频、飞行或最终发布声明。

## v140 本轮已收口

- `run_gr01_release_session.ps1` 新增 `-PlanOnly/-PlanOutput`。预演验证 non-loopback literal IPv4、source CIDR、
  hardware ID、阈值、repo-contained 输出路径、Python/CLI 可用性和两个 secret 环境变量的“名称+是否存在”；
  secret 值不进入命令参数、输出或日志；
- plan 使用 write-new JSON；closed schema 固定四步后续操作、`network_contacted=false`、
  `hardware_observed=false`、`hardware_flight_acceptance=not_claimed` 和全部 no-control/no-release 位。缺 secret 时
  正常生成 `ready_to_execute=false` 的诊断回执，不启动 bench、不创建 session 目录；
- 新增 repository-source CLI shim，现场 producer 不再依赖 editable-install；shim、producer 与 plan schema 均进入
  `safety_policy` parity。canonical parity 为 7,758 bytes、SHA-256
  `4FB09C34E7925D43B3A55926FF543D8A42716320C613E71948D2A2BE8AA70402`，safety hash 为
  `C3F6D82749779D8AD1E7484AC237DC2E92078C396F75288BAB476C7969AFD059`，其余四组不变；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 均独立通过；
  E7 继续 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,616 tests：`3614 passed, 2 skipped, 0 failed, 0 errors`，耗时 204.33 秒；JUnit
  527,019 bytes、SHA-256 `9E65D248BA8167721784769AF91BA3417D3CF106A0EEDF5CA78C47EF0A46EB8E`。
  全仓 Ruff、82 份 schema、源码 compileall 和 diff check 通过。本轮没有接触硬件，没有修改 C++/QML，
  不重建 portable EXE。

## v139 本轮已收口

- 新增 `scripts/run_gr01_release_session.ps1`：真实现场只需一个命令，即按固定顺序执行 hardware GR01 bench、
  `--require-passed` 独立复核、freshness-bound E7 manifest/report 和 session consumer 复核；每轮输出目录及最终
  `gr01-release-session.json` 均拒绝覆盖，任一步失败都不会出现成功记录；
- secret 只通过两个环境变量名称传入，值不进入命令行、JSON 或日志；host、port、hardware ID、expected source
  CIDR、样本数、丢包、P95、retry 与最大证据年龄均显式绑定，受控 release-gate 参数不能被 extra args 覆盖；
- 新增 `gr01_release_session.py`、独立入口与 closed schema。consumer 重开 bench/manifest/saved report，重跑
  GR01 verifier 和 canonical E7 evaluator，核对 source SHA/bytes、freshness、GR01 子门投影及 no-control/no-release
  claims；record 被篡改或任一来源漂移都会 fail-closed；
- producer、consumer、schema 与入口全部纳入 `safety_policy` parity。canonical parity 为 7,655 bytes、SHA-256
  `E138A41DE32CAD23DE554EB0AC21AE95C4D7B004498509CB434CAEEF44400575`；仅 safety hash 更新为
  `AE4636175DA3603C23F58500DD7FC7BAFF619CD533D07E60CBA8222E968BB7CC`，其余四组不变；
- 新 parity 下三机型 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign 均独立通过。
  E7 重算仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 仍
  `present=false/passed=false`；没有运行真实 session、没有硬件/飞行声明；
- 最终全量回归 3,615 tests：`3613 passed, 2 skipped, 0 failed, 0 errors`，耗时 154.16 秒；JUnit
  526,870 bytes、SHA-256 `C9F8B4ADC5B826C499851DD6A69A6E27F864FC8E9756068318D71427FC84DEF8`。
  全仓 Ruff format/lint、仓库源码 compileall 与 diff check 全部通过。本轮未改 C++/QML，portable EXE 不重建。

## v138 本轮已收口

- `real_gr01_operator_link` 新增 `observed_at_utc`、`report_timestamp_epoch_s` 与 `max_age_s` 三字段原子绑定；
  writer 默认只接收 900 秒内的报告，拒绝过期、无时区、非有限时间和超过 5 秒容差的未来时间；
- evaluator 使用整个 E7 报告共用的 canonical `evaluated_at_utc` 重算 age，时间字符串与 epoch 不一致、投影缺一、
  源报告时间漂移、复评时已过期全部 fail-closed；历史 prerelease 记录仍可读取，但最终 `0.3.0` 明确要求 freshness
  三字段完整存在，不能永久复用旧台架报告；
- Windows wrapper 新增 `-RealGr01OperatorLinkMaxAgeSeconds`（默认 900，范围 0..86400）并传给 manifest CLI；部署
  证据身份现在由 hardware ID、non-loopback endpoint、expected source CIDR、实际 source address 和采集时间共同组成；
- release parity 仍为 7,454 bytes，SHA-256
  `5179C34077DC61CF0A0BBEB530AA8A08A28CEB53FC327E5CADB66D70FD9A8723`；release-gate schema 变更令
  PX4 Mission path hash 更新为 `91CFD7816000A26BF04BEA11703247DDED3A4B07AAC7797CEBC9BD5161E746E5`，其余四组不变；
- 三机型 external join/revalidation、reported-position matrix 与 6 正例/9 故障 campaign 均从不可变 source 按新
  parity 重算并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，且
  `real_gr01_operator_link.present=false`，没有伪造现场报告；
- 首轮 strict 全量回归发现 annotation UI 测试的 `Popen` stdout/stderr 管道未关闭，产生 1 个
  `ResourceWarning`；测试改用 `communicate()` 并含 timeout/kill 回收，连续定向复测通过。最终全量 3,608 tests：
  `3606 passed, 2 skipped, 0 failed, 0 errors`，JUnit 238.449 秒、525,966 bytes、SHA-256
  `3EE31F54061D0CDC7F976687F87FA47B67B342907C6C38E8727D6D7513575B75`；Ruff format/lint、compileall、schema
  与 diff check 全部通过。本轮未改 C++/QML，portable EXE 不重建。

## v137 本轮已收口

- E7 新增独立 `real_gr01_operator_link` 顶层记录；manifest writer 只接受通过 strict verifier 的
  `gr01_bench_passed` 硬件报告，并绑定报告相对路径、SHA-256、字节数、GR01 身份、non-loopback endpoint、
  expected source CIDR、实际 source address、ACK/timeout/attempt 守恒、时延/丢包、HMAC、MAVLink2 签名、
  双向确认与全部 no-control 位；
- evaluator 每次从 manifest 重新打开源报告、复核哈希/字节数、重跑 canonical verifier，并逐字段对账；未知
  字段、投影篡改、源文件漂移和软件 loopback 冒充 REAL 都 fail-closed；最终 `0.3.0` release channel 要求该门
  单独存在且通过，不能由 `real_alert_link` 告警 ACK bench 替代；
- `run_current_release_gate_pre8.ps1` 新增可选 `-RealGr01OperatorLinkBench`；当前没有真实 GR01 bench，因此
  current manifest 明确 `present=false/passed=false`，没有生成硬件通过声明；该要求只在最终 release channel
  强制，当前 prerelease E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；
- release-gate schema 进入 PX4 Mission parity，当前 parity 文件 7,454 bytes、SHA-256
  `74854DE43D5BF772AAAFE9DA4B7359603BA9FC7F20B37F9612772EE1D9CCCC09`，PX4 hash 为
  `AA38DB69E111FB429DABCE57CF4585D9069F172FBE3759321EF843F5F45808A7`；三机型 external
  join/revalidation、reported-position matrix 与 6 正例/9 故障 campaign 已按新 parity 重算并独立通过；
- 最终全量回归 3,604 tests：`3602 passed, 2 skipped, 0 failed, 0 errors`，JUnit 时间 240.937 秒、
  525,403 bytes、SHA-256 `669E08AE1821BEB602B45651558DFB101B83F2D3BB9A2EADA3315C2C4600E86A`；
  全仓 Ruff format/lint、compileall、schema 和 diff check 通过。本轮未改 C++/QML，portable EXE 不重建。

## v136 本轮已收口

- GR01 目标选择/ACK hardware bench 现在强制 `--expected-source-cidr`；CLI 先读取 OS route 选择的
  canonical IPv4，再把同一地址显式 bind 到实际持续 UDP session。源地址不在声明网段时，往返循环不启动，
  `transmission_attempts=0`；
- 新增 strict `gr01-link-bench` schema 与离线 verifier，独立重算 endpoint、source route、ACK/timeout/attempt
  守恒、丢包、P50/P95 形状、阈值、HMAC/MAVLink2/双向标志、结果字段和全部 no-control 位；伪造 route flag、
  计数或 `passed` 不能通过；
- `operator_udp.py`、GR01 bench、CLI、schema 和 verifier 均进入 `safety_policy` parity。Phase 6 的软件验收契约
  已完成，但“经真实 GR01 双向 IP 实测”继续未勾选；该变更没有发送飞控或载荷指令，也不构成射频/现场通过；
- 首次全量回归发现且只发现进度 JSON 尚未同步新 parity（`3596 passed, 2 skipped, 1 failed`）；该文档一致性
  失败已按设计阻止虚假收口；同步后最终全量回归 3,599 tests 为
  `3597 passed, 2 skipped, 0 failed, 0 errors`，耗时 250.04 秒。JUnit 524,720 bytes、SHA-256
  `68467B9949C7CA0A5203045F26C37682839A6D93713ECD59115FE450D0C8D7C2`。
- 新 safety parity 下已从原始不可变 source 重算三机型 external join/revalidation、reported-position matrix 与
  6 正例/9 故障 campaign，四项 verifier 均通过；E7 重新生成后仍为 `gate_passed=false`、
  `release_allowed=false`、32 blockers，GR01/G20、现场数据和人工验收没有被软件证据替代。

## v135 本轮已收口

- 新增 strict `alert-link-topology` 契约，将 `gr01_direct` 与 `v6x_fallback` 明确区分；直连 endpoint 必须位于
  声明 source CIDR，所有地址采用 canonical non-loopback IPv4，任何控制/投放位都固定关闭；
- V6X 回退必须同时提供并逐字节 SHA-256 复核 GR01 直连失败、PX4 SITL 路由、拆桨 V6X 台架三份相对路径证据；
  缺失、越界、不可读或哈希漂移均在 UDP 发送前失败；
- alert-link bench 升级 v5，`--topology` 原子提供 endpoint、port、sender/receiver、hardware ID 与 source CIDR，
  冲突 CLI 参数会被拒绝；报告内嵌规范化 topology 和 canonical SHA-256，独立 verifier 重算绑定；
- release manifest 只接受 v5 REAL alert-link 报告，历史 v1-v4 仍可独立复核；topology 源码/schema 已加入
  `safety_policy` parity。该软件契约没有证明 GR01/V6X/G20 现场链路已通。
- 最终全量回归 3,592 tests 为 `3590 passed, 2 skipped, 0 failed, 0 errors`，耗时 242.489 秒；JUnit
  523,829 bytes、SHA-256 `7DBB937B24033EBB17F1BFCFA862E1A651624F7F65D5265A038046EE432168A5`。
- 新 parity 下重算的三机型 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign
  均经独立 verifier 通过；E7 重跑保持 `gate_passed=false`、`release_allowed=false`、32 blockers。

## v134 本轮已收口

- `AlertDeliveryReceipt` 新增 `source_address`；真实 UDP transport 在 bind/connect 后、send 前读取 socket
  `getsockname()`，规范化为 IPv4，并在它与配置 `source_host` 不同时直接失败；
- retry publisher 暴露最后一份已通过 receipt 校验的结果；alert-link bench v4 保存每个成功 ACK 的实际 sender
  source、观察数和完整性。hardware 只有全部成功样本都观察到同一 route/bind 地址才通过；
- 新 release manifest 只接受通过的 v4，历史 v1/v2/v3 仍可独立复核。当前错网段负测仍为 0 transmission，
  v4 另明确记录 0 个运行时 source observation；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 和 6 正例/9 故障 campaign 均已重算并
  独立复核；E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers。
- 最终全量回归 3,584 tests 为 `3582 passed, 2 skipped, 0 failed, 0 errors`，耗时 242.754 秒；JUnit
  522,614 bytes、SHA-256 `6735DE1B6A3FB85351D17F46CA9BAFECEE09DE1978F6E791D6D365663BC7C71C`。

## v133 本轮已收口

- `UdpAcknowledgedAlertTransport` 新增 canonical IPv4 `source_host`；配置后每次发送使用的实际 UDP socket 都先
  `bind(source_host, 0)`，不再只依赖另一个预检 socket 对下一跳路由的预测；
- alert-link bench 升级 schema v3，同时记录 `transport_source_address` 与
  `transport_source_address_bound`。hardware 只有 route source 属于 expected CIDR 且 sender socket 固定同一地址
  才进入 ACK loop；v1/v2 历史报告仍可复核，新 release manifest 只接受通过的 v3；
- 当前控制机真实负测的 route/transport source 均为 `192.168.1.13`，不属于 `192.168.144.0/24`，所以 bench
  退出 1、离线 verifier 退出 0、`transmission_attempts=0`；这不是实链通过；
- 新 parity 下 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign 均重算通过；E7
  重跑保持 `gate_passed=false`、`release_allowed=false`、32 blockers。
- 最终全量回归 3,582 tests 为 `3580 passed, 2 skipped, 0 failed, 0 errors`，耗时 242.077 秒；JUnit
  522,327 bytes、SHA-256 `4E61645B598CA28AAF1A8BEEF13C7130C73E6CFD31D75B9F4C9A5D80FDD6853A`。

## v132 本轮已收口

- authenticated alert-link bench 升级为 schema v2；`--hardware-mode` 除 non-loopback endpoint 和 hardware ID
  外，必须显式提供 `--expected-source-cidr`，报告保存 canonical CIDR、OS-selected local source address 与
  `source_route_verified`；
- route selection 使用未发送数据的 UDP connect/getsockname，在第一个认证告警前完成。源地址缺失、无法解析或
  不属于指定网段时不进入 ACK 循环，`transmission_attempts=0`，但失败报告仍能由 strict verifier 重算；
- 当前控制机负测实际得到 `local_source_address=192.168.1.13`，不属于 `192.168.144.0/24`；bench 退出 1、
  离线 verifier 退出 0（合法失败）、0 transmission。它只证明 fail-closed，不是 GR01 实链证据；
- release manifest writer 只接收 v2 REAL bench，并 hash-bind source CIDR/address/result；evaluator 继续读取历史 v1
  记录。alert transport、bench 与 schema 新增到 `safety_policy` parity；全量回归 3,577 tests 为
  `3575 passed, 2 skipped, 0 failed, 0 errors`，耗时 264.26 秒。现场 ACK/GR01/V6X/G20 仍未通过。
- 新 safety/PX4 parity 下已从既有不可变 source 重算三机型 external join/revalidation、reported-position matrix
  与 6 正例/9 故障 campaign，四项 verifier 均通过；E7 manifest/report 随后重跑，仍明确为
  `gate_passed=false`、`release_allowed=false`、`release_blocked_reasons=32`，没有把软件派生证据提升为现场验收。

## v131 本轮已收口

- 在当前 Git 对象上创建独立 detached worktree 后发现 Windows checkout 会把 204 个 QGC Python 文件恢复为
  CRLF，使主工作树已通过的 Ruff baseline 无法复现；新增 `.gitattributes` 对 `*.py`/`*.pyi` 强制 LF。第二个
  全新检出实测 1,524/1,524 format check、lint、compileall 和零 diff 全部通过；
- manual validation 原先只安装 `.[dev]`，干净环境会先缺 `pymavlink`，随后缺 OpenCV/NumPy/ONNX/requests。
  workflow 现安装 `.[dev,vision,pixhawk,security,model-tools]`，`requests>=2.32` 已进入 dev extra 与 `uv.lock`；
- `artifacts/` 按仓库政策不进入 Git。依赖真实本地 replay/evidence/隔离候选模型的测试现在只在对应文件存在时
  执行；干净检出中以带原因的 skip 保留边界，既不报假 FileNotFoundError，也不把未运行的资产集成检查记作 pass；
- VTOL capture ready marker 现在用已序列化的 6 位 monotonic 时间作为内部比较基准，消除了 exact echo 因小于
  1 微秒 round-down 被拒绝的跨解释器不确定性；该测试在 Python 3.13.7 下连续复现失败后已修复并通过；
- Windows Python 3.13.7 新 venv 按 manual workflow 顺序完成 editable install、`pip check`、compileall、全套
  pytest、Ruff lint/format：3,574 tests 为 `3559 passed, 15 skipped, 0 failed, 0 errors`，耗时 218.17 秒；
  JUnit 524,739 bytes、SHA-256 `864688E1B025B104E0F281D0A329ADE959C6B55BEBDAD5237CC12CA39B8C1790`。
  15 个 skip 均为 Windows 能力或 Git 外本地资产边界；GitHub-hosted 3.11/3.12/3.13 仍待手动运行。
- 主工作树 Python 3.12 保留本地 replay/evidence/quarantined ONNX candidate 后再次跑满 3,574 tests：
  `3572 passed, 2 skipped, 0 failed, 0 errors`，耗时 239.95 秒；JUnit 521,236 bytes、SHA-256
  `F7E7EED42C151B7D987A5554B035B17E48ABA13167254BF0A1F9B9A8DD037084`。这证明新增 skip 条件不会绕过
  资产存在时的严格集成检查。

## v130 本轮已收口

- Ruff 0.16.4 首次扫描 433 个未格式化文件（ground-control 205、src 106、tests 98、scripts 24）；全仓
  格式化后 282 个文件形成 canonical Git 差异，其余为换行归一。3 个 formatter 产生的 101 字符生成式已做
  等价拆行；最终 `python -m ruff format --check .` 为 1,523/1,523，lint、compileall 和 diff check 通过；
- `release-parity.json` 随 4 个受影响契约组重建，UI contract 未变。由六份不可变外部 runtime/AI source
  重算三机型 join/revalidation，由既有真实 PX4/Gazebo source 重算 reported-position matrix 与 6 次正例、
  9 次故障 campaign；四份派生证据均经独立 verifier 通过，不构成新的外部运行或硬件声明；
- E7 使用新 parity 与 v130 派生证据重跑，仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；
  QGC EXE/UI hash 未改变，真实 Jetson/Pixhawk/相机、人工视觉、HIL/REAL、实飞和最终发布仍待现场完成；
- 最终全量回归 3,572 tests：`3570 passed, 2 skipped, 0 failed, 0 errors`，耗时 220.80 秒；JUnit
  520,969 bytes、SHA-256 `2E214E2D7BA697FE531BF4762047CE0BF2AD7278575C2699DF2F02ED2D6CE060`。

## v129 本轮已收口

- 部署器先用 `ssh -G` 解析显式 user/host/port，再从 `KnownHostsFile` 通过 `ssh-keygen -F` 只提取该目标的
  可信 key，写成不可覆盖的每次部署 `.ssh-host-keys` snapshot；scp/ssh 同时指定该 snapshot 并排除 global
  known_hosts，整个远端操作前后再次比较 snapshot SHA，发生变化会删除刚写的 install receipt 并失败；
- installation receipt/Schema 升级 v3，绑定 resolved host、snapshot artifact reference 和排序唯一的 SHA256
  fingerprints。独立 Python verifier 不依赖 ssh-keygen，自行解析 base64 key、重算 fingerprint，并复核明文或
  OpenSSH `|1|salt|HMAC-SHA1` hashed-host token；错主机、snapshot 篡改、重复/伪造 fingerprint、revoked marker、
  未知 key type、注释/空行均 fail-closed；
- 当前真实 `C:\Users\admin\.ssh\known_hosts` 没有 `192.168.144.20`：实际非 package-only 负测在 scp 前以
  `No trusted host key exists` 停止，且 host snapshot、remote output、install receipt 均不存在。负测目录只保留
  dirty-source package/tar 供诊断，不构成远端安装证据；
- 全量回归 3,572 tests：`3570 passed, 2 skipped, 0 failed, 0 errors`，耗时 258.54 秒；JUnit
  520,970 bytes、SHA-256 `FB3D32C97F9A1D7E95DD0FC7B02C85F610042AA063D0C19221E89F2EBA955C0E`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、Windows OpenSSH option smoke、diff/parity 通过；共享
  哈希/QGC EXE 未改变，真实 Jetson/hardware/HIL/REAL/release 未通过，E7 仍有 32 blockers。

## v128 本轮已收口

- `scripts/deploy_jetson_evidence_capture.ps1` 新增显式 `RemoteUser`（默认 `jetson`）和 `SshPort`（默认 `22`）；
  scp/ssh 使用同一精确 `user@host` 与端口，修复直接传 IP 时 OpenSSH 误用 Windows 本机用户名的问题；
- 两段 OpenSSH 调用均强制 BatchMode、`StrictHostKeyChecking=yes`、`UpdateHostKeys=no`，因此未预置可信
  host key、身份变化或需要密码交互时直接失败，不会静默接受或改写主机密钥；任何凭据均不进入源码/回执；
- installation receipt/Schema 升级为 v2，新增 `remote_user` 与 `ssh_port` 严格绑定；独立 verifier 拒绝非法用户名、
  布尔/越界端口。真实 PowerShell `-PackageOnly` 开发烟测生成 6,905,344-byte tar，SHA-256
  `6AF0FE9EDED3B9132C75945718670A77E79C283A8953FA9A28B8D1721A7174A6`，独立复核 242 members、231 files，
  并实证 `INSTALL_RECEIPT_EXISTS=False`；该包 `source_dirty=true`，仅作开发烟测，不是正式部署证据；
- 全量回归 3,566 tests：`3564 passed, 2 skipped, 0 failed, 0 errors`，耗时 271.65 秒；JUnit
  520,094 bytes、SHA-256 `8B5B93EF1F70641AA16100E35B81BD7484CC968AEA9278F401EA891563B8A905`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、diff check 与 parity 通过；五组共享哈希/QGC EXE 未改变，
  真实 Jetson 安装、hardware、HIL/REAL、外场和 release 仍未通过，E7 仍有 32 blockers。

## v127 本轮已收口

- `scripts/deploy_jetson_evidence_capture.ps1` 现在保存完整 SSH 输出；仅在 SSH 退出码为 0、唯一
  `JETSON_EVIDENCE_TOOLS_READY=<exact target>` 与唯一 `JETSON_EVIDENCE_RUN=` 同时存在后，才原子写入安装回执；
- installation receipt 绑定 package receipt bytes/SHA、archive SHA、source Git HEAD、deployment ID、host、远端
  目标、runtime Python、remote output bytes/SHA 和六项远端检查。SSH 失败只保留
  `failed-remote-output.txt`，`-PackageOnly` 不产生安装回执；
- 新增 `multidetect-jetson-evidence-install-verify`、clean-checkout wrapper 和 closed Schema；独立 verifier 会重新
  打开 package receipt/tar 与远端输出，拒绝 package-only 冒充安装、身份漂移、输出篡改、marker 重复/缺失、
  检查失败，以及 sudo/service restart/hardware/release 越界声明；
- 全量回归 3,563 tests：`3561 passed, 2 skipped, 0 failed, 0 errors`，耗时 269.32 秒；JUnit
  519,572 bytes、SHA-256 `A86DAFAE06F727CD2C62AC3ADC5E188E5C7C77EBDA19CF628584DD5F23301EC7`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、diff check 与 parity 重生成通过。当前控制机仍无飞机网段，
  因而没有伪造真实 install receipt；remote install、hardware、HIL/REAL 和 release 仍未通过，E7 仍有 32 blockers。

## v126 本轮已收口

- 新增 `multidetect-jetson-evidence-package-verify`、clean-checkout wrapper、Python strict verifier 与 closed JSON
  Schema；接收方不依赖 PowerShell 生成器的自报结果，直接重开 package receipt 和相邻 tar；
- verifier 流式复核 archive bytes/SHA、source manifest SHA、全部登记源文件 SHA 和闭合文件集合，拒绝绝对/
  `..` 路径、重复/大小写冲突、symlink、hardlink、device/special entry、额外/缺失文件和安全声明提升；
- 正式 v125 包独立通过：240 tar members、229 个受保护源文件、6,668,784 源字节，路径/闭合集合/全部 hash
  均为 true；仍明确 `remote_install_verified=false`、hardware/release false；
- 全量回归 3,552 tests：`3550 passed, 2 skipped, 0 failed, 0 errors`，耗时 260.93 秒；JUnit
  518,004 bytes、SHA-256 `6872C45E81708A11619387F10F2446DE31EC6FE1A968ED796814D2DA741B0155`；
  独立正式包验证报告 735 bytes、SHA-256 `D629E3770F4FB3581C73A78B13BCA46ABD02C923F311F706DE141ABB41F98239`；
  Ruff、compileall、diff check 与 parity 重生成通过，QGC portable 未改变。

## v125 本轮已收口

- 新增 `scripts/deploy_jetson_evidence_capture.ps1`：打包完整 `src/multidetect`、产品版本和 v124 recorder，
  保存 Git HEAD/dirty、逐文件 source manifest、archive bytes/SHA-256 和不可发布声明；
- 生成后先列举安全路径、解包到临时目录、229/229 重算 SHA 并检查闭合文件集，只有
  `package_self_verified=true` 才允许上传；远端再执行相同 manifest 校验、compileall、Bash syntax 与模块 import；
- 远端落位采用独立 `/home/jetson/Multi-Detect-evidence-tools/<deployment-id>` staging→atomic move，已存在版本
  拒绝覆盖；无 sudo、无 systemctl、无服务重启，`-PackageOnly` 可在断开飞机网段时生成可审计包；
- 干净 `14b1010...` 上正式包 6,880,256 bytes、SHA-256
  `6D2D557726ED0EF213CD8411DC83F399C14888C3A5F311BE0AACAA7933F2F49E`，229/229 自复核；
  当前控制机源地址仍为 `192.168.1.13`，不在飞机 `192.168.144.0/24`，所以未声称真实 Jetson 安装；
- 全量回归 3,545 tests：`3543 passed, 2 skipped, 0 failed, 0 errors`，耗时 273.15 秒；JUnit
  517,008 bytes、SHA-256 `2C60726D0F2AC2EB14D3A1BE7C5889CDB6B300CB386E7CB4855BA0AE67DEA096`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、diff check 和 parity 重生成通过，QGC portable 未改变。

## v124 本轮已收口

- `scripts/record_jetson_rtsp_evidence.sh` 从单独录像升级为完整现场证据会话：每次创建独立 timestamp/session root，
  依次执行 H.265 RTSP stream-copy、v3 严格 manifest、camera-only raw replay 转换和 v123 transfer inventory；
- source URI 仍只从环境变量读取；会话根已存在时拒绝覆盖，任何阶段失败都保留部分原始证据并打印恢复位置；
  成功只输出 session root 与 inventory 路径，不启动 detector、QGC、飞控或载荷控制；
- 支持 `SESSION_ROOT`、`FRAME_STRIDE`、可选 `FRAME_LIMIT`；真实 Git Bash `bash -n` 与组合专项测试通过；
- 全量回归 3,544 tests：`3542 passed, 2 skipped, 0 failed, 0 errors`，耗时 275.09 秒；JUnit
  516,859 bytes、SHA-256 `2831EB324A468072DDE72170AF742B24BF3D7D9B58EF15B04F1482CA6B57AE16`；
  全仓 Ruff、compileall、diff check 和 parity 重生成通过，parity/QGC portable 均未改变。

## v123 本轮已收口

- 新增 `multidetect-evidence-transfer` 和 clean-checkout wrapper：`snapshot` 在 Jetson 侧生成不可覆盖的 closed
  inventory，逐文件绑定 portable relative path、bytes、SHA-256、总数、总字节与 canonical bundle hash；
- `verify` 在 Windows 复制目录上重新枚举完整文件集并逐项比较，拒绝缺失、增项、内容/大小变化、symlink、
  大小写冲突和把验证回执写入被验证目录；验证回执独立保存源 inventory 身份与目的端完整清单；
- inventory 与 verification 各有 strict verifier 和 JSON Schema；两者固定 `hardware_acceptance=false`、
  `release_ready=false`，目录改名不影响内容验证，仍不替代真实设备、人员或外场验收；
- 全量回归 3,544 tests：`3542 passed, 2 skipped, 0 failed, 0 errors`，耗时 260.53 秒；JUnit
  516,859 bytes、SHA-256 `5593142E374B2A05F90A2EA1EC3DD17EFBAC15BAC39355C84514537B740C3281`；
  全仓 Ruff、compileall、diff check 与 parity 重生成通过，parity 字节未变化，QGC portable 未重建。

## v122 本轮已收口

- RTSP v3 manifest 在视频位于清单目录内时写规范相对引用；清单+容器从 Jetson 复制到 Windows 后无需改 JSON，
  原 canonical hash、container bytes/SHA 与 full probe 仍可重算；目录外视频才保留绝对引用；
- raw replay 的 source evidence binding 为 manifest/video 各写 `path_base=recording_root|absolute`；同盘时优先
  相对 raw root，使“RTSP 清单+容器+raw replay”整个父目录可搬迁，跨盘无法相对化时明确退回绝对绑定；
- verifier 以搬迁后的 raw root 解析权威引用；summary 里的旧显示路径只作历史信息，raw manifest 固定从当前 root
  打开，所有 hash/bytes/canonical/frame checks 保持。测试实际移动完整目录后再次通过全链复验，并拒绝 summary 篡改；
- 全量回归仍为 3,537 tests：`3536 passed, 1 skipped, 0 failed, 0 errors`，耗时 258.60 秒；JUnit
  515,775 bytes、SHA-256 `EA5F7A260DAE01DED9F28CBEFC2E534EB7C74E99027CDFECE5FDE8CA110112AF`；
  全仓 Ruff、compileall 和 parity 重生成通过，五组共享契约与 QGC portable 未改变，E7 仍为 32 blockers。

## v121 本轮已收口

- `record-rtsp-evidence` 清单由 v2 升级为 closed-schema v3：采集调用前后取真实 UTC、保留单调时间窗，
  将视频 bytes/SHA-256、全帧媒体 probe、stream-copy/EOS/无控制声明纳入 canonical hash；输出路径规范为绝对路径，
  URI 和凭据仍不写入文件；
- 新增 `multidetect-rtsp-to-replay`、`src/multidetect/rtsp_evidence_replay.py` 和 clean-checkout wrapper；命令先
  严格重开 v3 清单及原视频，再把清单的 capture start 自动作为 raw replay UTC 起点，输出目录已有时拒绝覆盖；
- video converter 在解码前后各计算一次源 bytes/SHA-256，拒绝证据不符或转换途中改变；summary 绑定 source
  manifest/file/capture window 并带 canonical hash，转换完成后 verifier 再重开 RTSP source、summary、raw manifest、
  event stream 和全部 PNG；
- tracking-review 对 v2 保持历史兼容，新 v3 必须通过同一严格 verifier。真实 OpenCV 容器端到端及篡改负例已覆盖。
  camera-only 输出仍固定未知 GPS/位置、未证明人员/设备/地点、`release_ready=false`；E7 32 blockers 未改变。
- 全量回归 3,537 tests：`3536 passed, 1 skipped, 0 failed, 0 errors`，耗时 254.10 秒；JUnit
  515,775 bytes、SHA-256 `BD7D0B2718E1584685019DCD7D8BFA807DDA16AD239481F5252651681385E1AD`；
  全仓 Ruff、compileall、schema parse 与 parity 重生成通过，parity 字节未变化。

## v120 本轮已收口

- `create_video_replay_recording` 的 summary/`recording-summary.json` 新增 `source_attestation_handoff`：从重新
  打开的 raw manifest 派生准确 start、minimum end、latest event，列出仍须操作员确认的五项 provenance；
- `multidetect-deployment-recording-attestation` / wrapper 新增 `--derive-times-from-recording`，避免手抄 UTC；
  该开关不推断现场真实性，stratum、captured-by、device、site、local/RTSP 仍全部必填；
- 自动时间和显式时间互斥；video converter 的 `1970-01-01T00:00:00Z` camera-only 默认锚会被标记
  `attestation_time_eligible=false`，attestation writer/verifier 均拒绝把它升级为物理现场来源；
- 专项回归覆盖纯派生不落证明、显式人员字段要求、RTSP 证明、占位时间拒绝和 video→GNSS join 不回退。
  真实现场采集、人员审核、HIL/REAL、E7 32 blockers 与 QGC portable 均未改变。
- 全量回归 3,530 tests：`3529 passed, 1 skipped, 0 failed, 0 errors`，耗时 271.99 秒；JUnit
  514,802 bytes、SHA-256 `79EBC8B57C6471AFC2EFE3458258A34489F8E1BAEDD2EE2D4726A6964B8B82C0`；
  全仓 Ruff、compileall 和 diff check 通过。

## v119 本轮已收口

- 新增 `multidetect-deployment-recording-attestation` 和 clean-checkout wrapper；每个物理现场 raw recording
  必须写入不可覆盖的 `deployment-recording-source.json`，记录 canonical stratum、采集人、设备、地点、
  local/RTSP 来源与 UTC 起止，并绑定 recording manifest 的 bytes/SHA-256/recording hash；
- verifier 要求起点与 recording 一致、终点覆盖最后 frame/telemetry，重开 event/image 全链；closed schema、
  strict JSON、canonical hash、非 symlink 和 write-new 语义拒绝模糊或事后覆盖；
- deployment intake 把证明加入逐文件 inventory 与 receipt 摘要，field campaign 缺任一证明会停在
  `awaiting_source_attestations`，不会进入标注；
- `ReplaySetManifest` 新增 `source_attestation` artifact reference；builder/loader 强制 deployment 条目绑定
  同目录、同 stratum、同 recording 的证明并重新哈希，直接自报 source kind、旧无证明清单、移动或篡改均失败；
- lab/synthetic 历史清单继续兼容。证明是完整性绑定的人为来源声明，不是硬件签名、可信时间戳或独立外场
  验收；真实六分层采集、标注、模型评审和 E7 32 blockers 均未改变，共享 parity/QGC EXE 不重建。
- 全量回归 3,527 tests：`3526 passed, 1 skipped, 0 failed, 0 errors`，耗时 269.24 秒；JUnit
  514,344 bytes、SHA-256 `FF3FF9F0F0A8944A4EF6DA883D7F50393206C3C325A30716655590C9303B3883`；
  全仓 Ruff、compileall、schema parse、release parity 重生成与文档门通过，parity 文件逐字节未变化。

## v118 本轮已收口

- `multidetect-field-replay-campaign` / `scripts/run_field_replay_campaign.py` 新增 `annotation-ui`：启动前
  fresh 重开不可变 plan、模型哈希、intake receipt 和 annotation workspace，只有 authoritative stage
  为 `annotation_required` 才把当前固定路径交给 v117 工作台；
- campaign status 新增 `annotation_workbench_argv`，可以直接复制运行；尚未完成 intake/workspace、已完成
  review、证据漂移或其他 stage 都不能借该入口修改标注；
- stdout 先输出 `field_replay_annotation_workbench_authorized`，绑定 campaign ID、dataset ID/version、
  plan canonical hash 和 stage，再由工作台输出随机 loopback URL、`loopback_only=true` 与 `hardware_io=false`；
- 回归覆盖参数解析、默认 UI 命令、intake-ready 与 annotation-finalize-ready 拒绝，以及真实子进程随机端口
  启动、HTTP 页面读取和终止；这仍不代替真实人员视觉/交互、标注或复核；
- 全量回归 3,521 tests：`3520 passed, 1 skipped, 0 failed, 0 errors`，耗时 220.230 秒；JUnit
  513,503 bytes、SHA-256 `FCA8182DF1650B2FB2CAB2D4F076DF52903155369542116B06BCA019B6DA848A`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML、portable EXE 与 E7 32 blockers 均未改变。

## v117 本轮已收口

- 新增 `multidetect-replay-annotation-ui` 和 `scripts/run_replay_annotation_workbench.py`：本机浏览
  已验证 intake 图像，在 canvas 上拖拽多类别 bbox，逐帧保存 positive/negative，支持前后导航、状态计数、
  清除未保存框和独立 reviewer 提交；底层复用 v116 原子 annotation/review API；
- HTTP 服务仅允许数值 loopback 地址，默认 `127.0.0.1:8765`；随机 mutation token、CSP nonce、无 CORS、
  256 KiB 请求上限、严格 JSON（重复键/非有限数/额外字段拒绝）共同保护 mutation；
- 每次页面读取图像都会重新验证 intake/workspace、frame ID、相对路径、目录 containment、非 symlink、字节数
  与 SHA-256；服务启动事件固定 `loopback_only=true`、`hardware_io=false`，不访问相机、飞控或执行器；
- HTTP 生命周期回归覆盖页面/CSP、真 PNG、无 token、重复键、额外字段、正例、五个负例与最终 review binding；
  内嵌 JavaScript 已用 Node `--check` 验证。真实人工视觉/交互验收仍待现场执行；
- 全量回归 3,519 tests：`3518 passed, 1 skipped, 0 failed, 0 errors`，耗时 265.846 秒；JUnit
  513,238 bytes、SHA-256 `22AB545FB5BEB86FBB54766ACEE1CB806CEB8361ECE57EF59D59227A42E811FA`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML、portable EXE 和当前 E7 32 blockers 均未改变。

## v116 本轮已收口

- 标注安装入口和 clean-checkout wrapper 新增 `annotate`、`review` 命令：frame ID 精确寻址，positive
  接受一个或多个 `LABEL:X1:Y1:X2:Y2`，negative 明确无目标，全部修改均做 intake/assignment 复验、
  annotator 锁定、并发内容检查和同目录原子替换；
- inspect 新增下一未标帧的 stratum/frame/image 身份；campaign 的 `next_argv` 直接指向该帧，全部标完后
  切换成 review 命令，不再要求人工裸改 reviewer 字段；
- workspace schema v2 新增 `review_content_hash`，绑定 assignment、全帧 annotation、annotator、reviewer
  和 UTC timestamp；复核后实际改标会撤销 review，文件外篡改或 reviewer 换人会被 fresh inspect 拒绝；
- v1 reviewed workspace 继续保持历史可读/finalize；新 campaign 强制 v2 + review binding，拒绝把 v2 降级成
  无内容绑定的 v1 后继续自动流水线；
- 全量回归 3,518 tests：`3517 passed, 1 skipped, 0 failed, 0 errors`，耗时 235.59 秒；JUnit
  513,092 bytes、SHA-256 `D8ECBFD450D0B168F22533CA57D72BF562675117B091318D1E34EADA2948B7DC`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML、portable EXE 和当前 E7 32 blockers 均未改变。

## v115 本轮已收口

- 新增不可覆盖的 `field-replay-campaign.json` 计划和 `create/status/advance` CLI，把六个真实来源目录、
  固定模型/manifest、推理参数、accuracy 门限和 calibration grid 全部做 path/hash/bytes/canonical 绑定；
- 自动推进 intake、annotation workspace、finalize、正式 ONNX runtime predictions、reviewed accuracy、
  reviewed threshold calibration 和独立 fresh verification；真实记录缺失或人工标注/复核缺失时停在明确 stage，
  不生成正向 claim；
- annotator、annotation reviewer、accuracy reviewer、threshold reviewer 强制四人互异；重新打开 campaign
  会逐层复验 receipt/report/model，手工替换或篡改继续 fail-closed；
- synthetic HIL ONNX 六分层端到端测试已实际走到 `complete`，并覆盖 pending、模型篡改、CLI 和幂等恢复。
  这只证明编排软件可运行，现场六分层采集和人工审核仍未执行；
- 全量回归 3,515 tests：`3514 passed, 1 skipped, 0 failed, 0 errors`，耗时 208.81 秒；JUnit
  512,680 bytes、SHA-256 `7484ECD43DA22A8B2E903DF539EFDDEEAFDAECAEB65673658DC568BA227E0D50`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML 和 portable EXE 未变，当前 E7 仍为 32 blockers。

## v114 本轮已收口

- E7 calibration verification 结果新增聚合 `promotion_ready`：只有 report 与当前输入重新计算都明确为 true
  才成立；历史缺失、任一 pending 或不一致都不能获得该状态；
- prerelease 仍允许读取和绑定 `promotion_ready=false` 的确定性诊断，不破坏阈值探索和历史证据；
- final release 在 v113 provenance 门之外进一步要求 promotion-ready，封死“完整性通过但阈值未独立批准”仍能
  进入最终版的缺口；专项测试实际构造未 reviewed calibration，证明 prerelease 可读而 release 明确阻断；
- 当前 parity 未变化；E7 重算新增诊断字段后仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，
  calibration 的 `promotion_ready=false`、`provenance_complete=false` 与现有 SIM fixture 一致；
- 全量回归 3,512 tests：`3511 passed, 1 skipped, 0 failed, 0 errors`，耗时 197.330 秒；JUnit
  512,248 bytes、SHA-256 `C4B5020705A7501B433EABE7DBA80DB9DACBE696DD5DA3D0A6BD4DA159460795`。

## v113 本轮已收口

- calibration verifier 新增 `report_provenance_complete` 与 `recomputed_provenance_complete`，分别记录文件声明
  与按当前输入重新计算后的 provenance；
- E7 writer 将两项布尔值与 verification artifact、calibration report、replay manifest 的 raw/canonical hash
  一起绑定，evaluator 每次重新打开并复算后输出汇总 `provenance_complete`；
- v111 及更早的报告/verification/E7 绑定缺少新字段时仍可在 prerelease 读取，统一投影为 false，不伪造来源；
- 最终 release channel 现在要求 calibration verification 存在、通过且 provenance 完整；该要求不替代独立
  accuracy evidence，也不让 synthetic HIL 模型获得生产批准；
- synthetic HIL ONNX 测试贯通 reviewed truth → runtime prediction receipt → reviewed accuracy → calibration → E7，
  并覆盖旧 artifact 兼容、正式版阻断与篡改拒绝；共享 parity 因 release-gate schema 改动已重算。
- 从既有不可变 runtime/AI、三机型正向与故障源重算 external join/revalidation、PX4 matrix/campaign 后，
  当前 parity 的 E7 wrapper 完整执行，仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；
- QGC C++/QML 与 UI parity 未变，EXE 不嵌入 E7 Schema/parity 文件，因此不重复构建 portable；旧机器证据保留，
  新派生证据只重绑当前共享契约，不宣称重新执行 Gazebo、硬件或飞行。
- 全量回归 3,511 tests：`3510 passed, 1 skipped, 0 failed, 0 errors`，耗时 192.488 秒；JUnit
  512,114 bytes、SHA-256 `20660335E255D76787B9797E2EBF6219392FE6C161F76C61284B53066DF4DC83`。

## v112 本轮已收口

- 将 v110 prediction receipt 验证抽成 evaluator/calibration 共用函数；每次都会重开 annotation、raw、truth、
  model/manifest、六分层 predictions、replay manifest 和 receipt，并返回同一套 model/run binding；
- prediction binding 新增 `production_approved`，与 runtime inference 和 synthetic HIL 分开记录；候选或 synthetic
  模型不会因为完成推理而被描述为生产批准；
- calibration API、wrapper、安装主 CLI 新增成对的 `--prediction-run-receipt` 与 `--accuracy-report`；只给一个会
  fail-closed，完整输入会先执行 readiness fresh recomputation，再核对 accuracy 的 model/run binding；
- calibration report 新增 prediction receipt、accuracy 文件、manifest/canonical、model artifact 等哈希绑定及
  `provenance_complete`；旧报告仍可重算，但该字段为 false，保持 prerelease 历史兼容；
- verifier 可从报告恢复两条路径，也支持复制工作区时显式 override；替换一个仍能独立自洽的 accuracy report
  也会使 calibration deterministic recomputation 不同而失败；
- synthetic HIL ONNX 端到端测试实际完成 reviewed 六分层 truth、runtime predictions、reviewed accuracy、
  calibration、schema validation、CLI parity、独立 verify 和 tamper rejection；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 214.725 秒；JUnit
  511,970 bytes、SHA-256 `E9AD6C579FD88B3953195EB9207FAB775EAED6B2E3BDCD997600AB334B8D3B49`；
  全仓 Ruff、compileall、schema parse 与 diff check 通过。

## v111 本轮已收口

- 修复安装主命令与仓库 wrapper 的能力漂移：`multidetect evaluate-replay-set` 现在同样公开
  `--prediction-run-receipt`，并原样转交 v110 evaluator verifier；
- synthetic HIL ONNX 端到端回归先生成真实 runtime predictions/receipt，再分别调用 wrapper 与主 CLI，
  两者都必须返回相同 receipt canonical hash；以后删参或漏转发会直接失败；
- 未修改 detector、测距/定位、QGC、共享 parity 或飞控路径，因此不重建 portable EXE；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 192.917 秒；JUnit
  511,970 bytes、SHA-256 `C0E68ED6656C4AA59D7FEEB00D3BCF9C8328787E325FA68C527F26A1638EB637`；
  全仓 Ruff、compileall 与 diff check 通过。

## v110 本轮已收口

- accuracy evaluator 新增可选 `prediction-run.receipt.json` 输入；计算指标前调用 prediction 独立 verifier，
  复验 annotation/raw/truth/model/predictions/new manifest 全链，而不是只信 accuracy 调用参数；
- model artifact SHA、model ID 和 model version 从已验证 receipt 派生；调用者若同时提供冲突值会 fail-closed，
  防止另一模型或另一轮 predictions 被拼进同一份精度报告；
- accuracy report 新增 receipt 字节 SHA、canonical hash、model manifest SHA、runtime inference 与 synthetic HIL
  事实绑定；closed schema 已同步，未使用 receipt 的旧评估路径保持兼容；
- readiness fresh recomputation 会取出并再次验证同一 receipt，再比较完整 `prediction_run_binding`；因此静态
  accuracy JSON 即使自改模型字段，也不能绕过新鲜重算；
- CLI、报告写入和 synthetic HIL ONNX 端到端测试已覆盖 receipt→evaluator→readiness 链及冲突拒绝；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 191.635 秒；JUnit
  511,970 bytes、SHA-256 `AE8EA9C38A226E570DCED72A5C9722F71236B731DDC34166B8C039FFE347F145`；
  全仓 Ruff、compileall 与 diff check 通过。

## v109 本轮已收口

- 新增固定模型 replay prediction runner：先复验 v108 annotation bundle 与 model manifest/artifact，再复用正式
  `ReplayFrameSource`、`OnnxNx6Detector`/`OnnxRawYoloDetector`、`JsonlPredictionWriter`，没有离线算法分叉；
- 六个 canonical strata 严格按 recording 帧序生成 predictions，模型类别 canonical 化后必须与 reviewed replay
  完全一致，未知输出类别、帧缺失/重复/错序均 fail-closed；
- 输出采用不可覆盖 sibling temporary；bundle 内复制并绑定固定模型，保留原 annotation manifest，另生成包含
  predictions 的新 manifest 和 canonical receipt；receipt 锁定模型身份/hash、输入/NMS/threshold、provider、
  runtime 版本、逐层文件 hash/帧数及 P50/P95/maximum/total latency；
- 独立 verifier 重开 raw/intake/workspace/truth/原 annotation manifest/model/manifest/predictions/新 manifest 全链；
  test double、真实 runtime、production approval、synthetic HIL 和 accuracy/release claims 明确分离；
- installed CLI、clean-checkout wrapper、closed receipt schema 与 4 项回归已落库；实际 synthetic HIL ONNX 集成
  smoke 已经通过 OpenCV、ONNX Runtime、letterbox、Nx6 adapter 和 `fire→flame` 规范化；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 194.679 秒；JUnit
  511,970 bytes、SHA-256 `9BC817EB6611FAD799302D9EAC66658E8ABBCB45CF48D6A6373339B0963C75F9`；
  全仓 Ruff 与 compileall 通过。

## v108 本轮已收口

- 从 v107 hash-bound intake 生成逐帧 annotation workspace；每帧默认 `unannotated/objects=null`，未处理帧
  不能自动成为负例；
- positive 强制至少一个 class-bound、有限且归一化的正面积 bbox，negative 强制空对象；未知字段、重复 key、
  NaN/Infinity、类别越界和非法 bbox 均 fail-closed；
- assignment hash 绑定 intake/dataset/classes/frame/image，但 finalizer 还会从 intake 重新推导并逐项比较，
  因此修改 frame ID、stratum、image path/hash/尺寸后重算自报 hash 也不能通过；
- finalize 要求 annotator/reviewer 分离和 UTC review timestamp，在 sibling temporary 原子导出六份 truth、
  reviewed workspace、review receipt 与 deployment replay manifest；verify 重开 raw/intake/workspace/truth/manifest
  并做语义和逐字节对账；
- create/inspect/finalize/verify CLI、installed entry point、两份 closed schema 与 4 项回归已落库；所有输出继续
  `predictions_complete=false`、`accuracy_validated=false`、`release_ready=false`，不替代模型回放和精度评审；
- 全量回归 3,506 tests：`3505 passed, 1 skipped, 0 failed, 0 errors`，耗时 177.668 秒；JUnit
  511,437 bytes、SHA-256 `445FFF48AC1B2A99051EF6490C96D6FCECD96130870A12414992D50ACD5ABE99`；
  全仓 Ruff 与 compileall 通过。

## v107 本轮已收口

- 新增 deployment replay-set intake：六个 strata 必须显式绑定六个不同 raw recording，拒绝目录复用与
  recording hash 复用，不靠目录时间或通配符猜来源；
- staging 只复制 raw manifest、events 和被引用图像，不携带源目录中的旧 ground truth/predictions；逐文件
  SHA-256/字节数、recording identity、frame count 和完整 artifact inventory 全部绑定到 canonical receipt；
- 新目录采用 sibling temporary + 完整回验 + rename，拒绝覆盖、symlink、路径逃逸、重复 JSON key、NaN、
  receipt/claim/artifact 篡改；失败会清理临时目录；
- receipt 明确固定 annotation/prediction 为 pending，四项 review/accuracy/release claims 全为 false；后续人工
  标注与模型输出仍须走现有 manifest/evaluator/readiness，不产生部署批准；
- 新增源码 API、installed CLI、clean-checkout wrapper、closed JSON Schema 和 5 项回归。全量回归 3,502 tests：
  `3501 passed, 1 skipped, 0 failed, 0 errors`，耗时 176.837 秒；JUnit 510,892 bytes、SHA-256
  `452AC95871E4746943EDD900A60DA9B545E3403193DC75FAE96988D194EC57A8`；全仓 Ruff 与 compileall 通过。

## v106 本轮已收口

- E7 replay-set evaluator 现在从已绑定 manifest/accuracy report 调用 canonical readiness checker，输出
  hash-bound、fresh-recomputed 的逐 entry/stratum 统计、检查、原因和下一步动作；
- readiness 是嵌套诊断投影：不以自身缺少 accuracy report 否决已经通过的独立 threshold calibration，
  同时在读取/重算异常时仍 fail-closed；专项回归覆盖 pending manifest 和 calibration-only 兼容路径；
- 当前 SIM fixture 复算为 6 entries、6 strata，文件和 prediction streams 完整，但 6/6 为
  `synthetic_demo`、6/6 `annotations_reviewed=false`，正向 strata 没有正真值/正预测；readiness 记录
  37 条原因与 7 个下一步，顶层 E7 保持 32 个真实 blockers；
- 五组 release parity 未改变，QGC C++/QML 未改变，因此不重绑 SIM 摘要、不重建 portable EXE；
  全量回归 3,497 tests：`3496 passed, 1 skipped, 0 failed, 0 errors`，耗时 323.705 秒；JUnit
  510,223 bytes、SHA-256 `FE6A5E094765BF4A5E46EA29A2013D1773E7D2E1631018548E8362F91BC0C4E7`；
  全仓 Ruff 与 compileall 通过。

## v105 本轮已收口

- 修复 `run_current_release_gate_pre8.ps1` 固定默认链的历史漂移：将缺少 CameraInfo K/P/R/D 与完整
  odometry 语义的旧 combined source 换为当前 900 帧 acceptance，并绑定当前 release-mode envelope；
- 从六份不可变 external runtime/AI 原始记录重算 join/revalidation，从三机型正向源重算 reported-position
  matrix，并从 6 次正向与 9 次故障注入源重算 campaign；全部绑定当前 release parity 且独立 verifier 通过；
- 按当前 Gazebo model/root 字节重新生成 fixed-wing、multirotor、VTOL deterministic 3-D control records，
  清除 v56 model SHA 漂移；不宣称外部 3-D、人工视觉、硬件或实飞验收；
- wrapper 仅指定新输出目录/时间戳即可生成 E7 manifest/report，结果为 32 个真实 blocker，且不再因旧
  parity/model hash 异常退出；默认仍固定路径、拒绝 mtime/通配选择，现场报告过期时继续 fail-closed；
- 本轮不改 release parity，也不改 C++/QML，因此不重建 portable EXE；全量回归 3,497 tests：
  `3496 passed, 1 skipped, 0 failed, 0 errors`，耗时 145.857 秒；JUnit 510,223 bytes、SHA-256
  `69A47ECAA68596D342876978CB22C2C60E35F71C4178F0CAC199476D1EC39F06`；全仓 Ruff 与 compileall 通过。

## v104 本轮已收口

- release manifest writer 从 canonical field-link verifier 生成 `real_field_link.address_state_bound`，E7
  无需下钻源 JSON 即可看见 REAL 链路是否携带地址 apply provenance；
- evaluator 每次重开 field-link 报告及其嵌套地址回执，将实际 binding presence 与 manifest 投影对账；
  顶层布尔篡改、嵌套地址源漂移均 fail-closed；
- prerelease 保留旧 manifest 可读性；最终 `0.3.0` 必须显式携带该投影，但允许值为 false，以兼容由
  DHCP、预置静态地址或其他受控机制配置的现场网络，避免把特定 helper 错当唯一部署方式；
- release-gate schema/writer/evaluator 纳入 PX4 Mission parity，hash 更新为 `7CD5CD2B...B32402F`；
  本轮没有生成硬件、HIL、REAL 或实飞接受声明；全量回归 3,497 tests：
  `3496 passed, 1 skipped, 0 failed, 0 errors`，耗时 172.340 秒；JUnit 510,223 bytes、SHA-256
  `6EB9E2180B44649CB0F7C23ACF5C1ABE9C490B27AC349694463F829FBCB6F90D`；全仓 Ruff 与
  compileall 通过。

## v103 本轮已收口

- field-link 预检 CLI 与 Windows wrapper 新增可选地址回执绑定：只接受成功 `apply` 证据，并要求显式
  interface alias、目标地址/prefix、父级 expected source CIDR 与最多 300 秒的新鲜度；
- 预检报告保存回执路径、字节数、SHA-256、schema、接口和地址身份；canonical verifier 每次重新打开
  源回执，按父预检观测时间复验，保证归档报告仍可确定性验证；
- 源文件变化、binding 篡改、软链接、自引用、what-if 冒充 apply、策略时限放宽、地址越界、父 CIDR
  缺失及 interface diagnostic alias 不一致全部 fail-closed；地址回执不替代远端 endpoint probes，也不
  单独升级 `hardware_link_ready`；
- 当前机器只执行未绑定的只读 wrapper smoke，仍为 Ethernet `192.168.1.13/24`、飞机网段端点不可达、
  `hardware_link_ready=false`；没有 apply 地址、触发 UAC 或生成 HIL/REAL/flight 接受声明；
- 安全 parity 更新为 `D5B15BF4...FDBC87E`，其余四组 hash 不变；新增 7 项地址/绑定回归；全量回归
  3,496 tests：`3495 passed, 1 skipped, 0 failed, 0 errors`，耗时 112.331 秒；JUnit 510,081 bytes、
  SHA-256 `D6665848313861046A7C2FD496D68178C28F87A66758D81C80FBF155094C007F`；全仓 Ruff 与
  compileall 通过。

## v102 本轮已收口

- 新增 closed schema `field-link-address-state.schema.json`，统一 plan/verify/what-if/apply 四类 PowerShell
  输出；`prepare_field_link.ps1` 现在写入固定 schema identity；
- 新增 `field_link_address_state.verify_field_link_address_state`、仓库 CLI wrapper、安装后 console entrypoint
  与根包 lazy export；复验过程只读，不接触网卡或远端设备；
- verifier 严格拒绝未知/重复字段、重复 JSON key、NaN/Infinity、非 canonical IPv4、重复地址、非法路由、
  错误 plan change/elevation、安全标志、rollback 动作/命令、目标接口/IP/prefix 和 stale/future 时间；
- apply 只有持久化地址状态与 postcondition 一致且成功时才过 gate；plan/verify/what-if 可结构通过，但
  `--require-applied` 必须失败。真实生成的三类非变更文档均已由 CLI 独立复验；本轮未修改主机网络；
- schema、producer、verifier、CLI 与实现全部纳入安全 parity，`safety_policy_hash` 更新为
  `10F115B1...DE7E8A`，其余四组 hash 不变；新增 9 组回归覆盖 schema、语义重算、篡改、时效与 CLI；
  全量回归 3,489 tests：`3488 passed, 1 skipped, 0 failed, 0 errors`，耗时 174.813 秒；JUnit
  509,138 bytes、SHA-256 `295860BFF1C36FB49C005AB2B3B2459D7F89BB26DFAC221617497E2E927A5BCE`，
  全仓 Ruff 与 compileall 通过。

## v101 本轮已收口

- 地址 helper 用 `$appliedChange` 记录 `ShouldProcess` 后实际执行的 add/remove；`-WhatIf` 明确输出
  `mode=what-if`、`changed=false`，且无需管理员权限，不再把 dry-run 误记成主机变更；
- 正常 apply/remove 后重新枚举精确目标地址。add postcondition 要求恰好一条匹配地址且
  `SkipAsSource=true`；remove 要求零条。审计 JSON 记录是否检查/是否通过，失败时命令非零退出；
- 本机真实非提权 `-WhatIf` 已验证：计划添加 Ethernet `192.168.144.250/24`，没有修改网络，审计
  记录 792 bytes、SHA-256 `08ACD59D...58E94`；
- 因 field-link helper 已纳入统一安全身份，`safety_policy_hash` 自动更新为
  `46324CC0...3694AF`；UI/core/task/PX4 Mission 四组 hash 不变，仍未产生硬件/HIL/REAL/实飞接受；
  全量回归 3,480 tests：`3479 passed, 1 skipped, 0 failed, 0 errors`，耗时 115.741 秒；JUnit
  507,924 bytes、SHA-256 `26BCF986E3CBFAD9481BF7005227E7861E76419A55E7A47461AE802691BE9307`，
  全仓 Ruff 与 compileall 通过。

## v100 本轮已收口

- `prepare_field_link.ps1` 新增非提权 `-PlanOnly`：在修改网卡前输出拟执行动作、是否需要提权、目标
  IPv4/prefix、secondary-only/`SkipAsSource` 约束、明确不变更 gateway/DNS/explicit route 以及精确
  rollback 命令；`-VerifyOnly` 同样可生成结构化只读状态；
- `-Output` 使用 UTF-8、write-new、flush-to-disk 语义保存审计 JSON，拒绝覆盖已有文件；输出路径会被
  精确传递给提权子进程。实际 apply/remove 仍要求管理员权限，本轮没有触发 UAC 或改变主机网络；
- 当前机器计划证据确认 Ethernet 仍为 `192.168.1.13/24`，拟添加的是
  `192.168.144.250/24` secondary address，`would_change=true`；只读 verify 与 3-sample field preflight
  继续显示飞机网段无可用 source route，Jetson/GR01/RTSP 均未就绪，`hardware_link_ready=false`；
- 将 field-link 接口诊断、地址准备、PowerShell/Python runner、canonical verifier 和聚合实现全部纳入
  `safety_policy` parity。安全 hash 更新为 `792296E8...C7B74`；其余四组 hash 不变。本轮没有 QGC
  C++/QML 变化，因此不重建 EXE，也不提升 HIL/REAL/hardware/flight 声明；全量回归 3,480 tests：
  `3479 passed, 1 skipped, 0 failed, 0 errors`，耗时 157.868 秒；JUnit 507,924 bytes、SHA-256
  `E04959F235BA971BDF7A66F654ADAD99DA9E0DD65C32947EE250FEE33CDF7FA9`，全仓 Ruff 与 compileall 通过。

## v99 本轮已收口

- `write_field_covariance_journal_index` 先生成稳定快照、write-new 输出，再从磁盘执行完整 index verifier；
  成功返回值来自 verifier，不再信任未落盘内存对象；
- 复验后使用同一 `indexed_at` 重建第二份当前快照并与首份逐字比较，捕获 build→write 窗口内新增
  session/probe、campaign 前移或证据内容漂移；
- 输出仍拒绝覆盖；只有本轮确认新建的 index 才会在复验失败时删除，journal、receipt、session、probe 和
  预存输出均不修改；测试覆盖写后 blocked probe 篡改和写后新增 probe 两条回滚路径；
- `px4_mission_path_hash=4BDEFEC0...BCC1A`，其余四组 parity 未变化。全量回归 3,480 tests：
  `3479 passed, 1 skipped, 0 failed, 0 errors`，耗时 101.403 秒；JUnit 507,924 bytes、SHA-256
  `EC912331EA105664692CAC0241E14D8989618A8D0DB302455654125376ADE1E6`；全仓 Ruff 与 compileall 通过。
  本轮无 QGC C++/QML 改动，未重建 EXE；真实 HIL/REAL/hardware/flight 门仍未通过。

## v98 本轮已收口

- 将 `blocked_probe_count` 从裸计数升级为 closed-schema `blocked_probes[]`：逐项绑定文件路径/SHA、online
  evidence digest、观测时间、HIL/REAL 模式、机型与失败原因；count 必须与数组长度一致；
- 每个未被成功 session 引用的 probe 必须通过 archived online preflight 独立复验且
  `gate_passed=false`；伪 JSON、篡改证据和未引用的 passing probe 均 fail-closed；
- index verifier 重开 blocked probe 并重算完整记录，删除、替换或漂移后旧 index 不再通过；成功 session
  引用的 probe 也被强制限制在同一 journal 目录；
- index 构建前后均要求 receipt/workspace 专属 active capture lease 不存在，并双读 campaign status、session
  文件集、probe 文件集及文件 SHA；采集中或构建中发生 report/artifact 漂移时拒绝混合快照；
- `px4_mission_path_hash=F24E5BAE...521A0`，其余四组 parity 未变化。全量回归 3,478 tests：
  `3477 passed, 1 skipped, 0 failed, 0 errors`，耗时 101.246 秒；JUnit 507,642 bytes、SHA-256
  `E617EB9F2B7227635604A37F0F807B4388F9EA2952E4A4752BDAD00FC8C7A464`；全仓 Ruff 与 compileall 通过。
  本轮无 QGC C++/QML 改动，未重建 EXE；真实现场/HIL/REAL/flight 证据仍为空。

## v97 本轮已收口

- 关闭“成功 session 已 write-new，但进程在 lease 正常 release 前崩溃”的审计断链：死亡 owner 的 active
  lease 经精确 recovery 后，成功 session 仍可由 journal index 入册；
- 每份 session 必须恰有一个 lease 审计源：正常路径为 `released-leases/<lease-id>`，异常恢复路径为
  `recovered-leases/<lease-id>`；两者同时存在或均不存在都 fail-closed；
- recovered 路径会独立重开并验证 `lease.json` 与 `recovery.json`，重算文件 SHA 和 canonical digest，核对
  lease ID、lease digest、完整 receipt 及 session 内嵌 lease；缺失、替换、篡改或重新签 digest 后的错误
  receipt/lease/hash 均拒绝；
- index record 的 closed schema 记录 archive kind、lease path/hash 和可空 recovery path/hash，并用条件约束
  强制 released 记录无 recovery、recovered 记录必须具备 recovery；`lease-status` 增加 recovered 数量；
- `px4_mission_path_hash=72CBC380...6FB4`，其余四组 parity 未变化。全量回归 3,474 tests：
  `3473 passed, 1 skipped, 0 failed, 0 errors`，耗时 100.000 秒；JUnit 507,101 bytes、SHA-256
  `2B0494192B80F67E859E18153FC5DE2D490737DCB192F73B6F2D60DB10803466`；全仓 Ruff 与 compileall 通过。
  本轮没有 QGC C++/QML 变化，因此没有无意义重建 EXE；真实现场断电/HIL/REAL/flight 门仍未通过。

## v96 本轮已收口

- 修复 v95 active lease 的两个断电窗口：acquire 先在非阻断 `.lease-candidates/<lease-id>` 中 write-new、
  fsync 完整文档，再原子 rename 整个目录到 active path；release 先原子移出 active path，再保留到
  `released-leases/<lease-id>`，不存在“空 active 目录永久卡死”状态；
- candidate 残留只计入 `nonblocking_candidate_count`，不会夺取 capture authority；managed candidate、
  released、recovered 目录全部拒绝 symlink；
- released lease 不再销毁。journal index 对每份 session 重开其 released `lease.json`，校验文件 SHA、
  receipt/campaign/leaf/digest，并要求与 session 内嵌 lease 完全一致；缺失或替换均 fail-closed；
- recovery 在写完 `recovery.json`、尚未 archive 时中断后可以安全重试，但必须使用相同精确 lease ID 与
  相同 reason；不同请求、已有目标 archive 或活/未知 owner 都拒绝；
- 当前机器完成新鲜只读 field-link 探测：Ethernet 为 `192.168.1.13/24`，飞机网段
  `192.168.144.0/24` 无候选接口/route，3/3 样本未就绪；报告结构与绑定独立复验通过，但
  `hardware_link_ready=false`，没有提升任何硬件声明；
- `px4_mission_path_hash=FD77EAC9...20BBDC`。全量回归 3,468 tests：
  `3467 passed, 1 skipped, 0 failed, 0 errors`，耗时 99.785 秒；JUnit 506,030 bytes、SHA-256
  `85CBAA7D6890737943A6A5A93CAD06E51C920941EE1B62552A2EC51CECEB7D15`；全仓 Ruff 通过。

## v95 本轮已收口

- 新增 `field_covariance_journal` 的原子目录 lease；`run-next` 在 online probe/capture/record 全事务外持锁，
  同一 receipt/workspace 的第二进程在采集启动前 fail-closed；成功、blocked probe 和正常异常路径均仅在
  lease 文档仍逐字一致时释放，锁被篡改则保留；
- `lease-status` 使用同机只读进程状态判断。Windows 走 `OpenProcess + GetExitCodeProcess`，不发送信号；
  远端 hostname、权限/状态未知或仍存活 PID 都禁止恢复；
- `recover-lease` 要求精确 32 位 lease ID 和书面原因，且 owner 必须被证明死亡；恢复会把原 lease 与
  closed-schema、digest-bound recovery 一起原子归档到 `recovered-leases/<lease-id>`，不静默删除证据；
- 成功 session 嵌入完整 lease，并在事后复验时交叉核对 receipt/workspace/campaign/leaf/digest；新增
  `journal-status/verify-index`，严格对账 session `1..N` 连续前缀与 campaign 已验证 reports，拒绝断号、
  重复、乱序、跨 receipt、路径逃逸和 campaign 回退，同时统计未进入成功 session 的 blocked probes；
- journal/session 两份 schema 与实现纳入 PX4 Mission parity，`px4_mission_path_hash=71A7B6D0...E20CEA`。
  真实硬件未连接，未生成现场 lease/session/index 或任何 HIL/REAL/hardware/flight acceptance。
- 全量回归 3,464 tests：`3463 passed, 1 skipped, 0 failed, 0 errors`，耗时 97.762 秒；JUnit
  505,454 bytes、SHA-256 `106EDE42C9C57A04EF6D30C864B2D876E2DF3923B82C07CEED40FE7E0AEF4FDC`；
  全仓 Ruff 通过。

## v94 本轮已收口

- 新增 `field_covariance_session run-next/verify-record`：每次从 v93 receipt 重验本机 workspace，锁定唯一
  `next_leaf`，只执行一叶 Pixhawk covariance 现场事务；
- `capture-next --probe-report-out` 先 write-new 保存 online observation，再从磁盘按默认 300 秒时效、当前
  leaf、source/parity、mode binding、identity 和 query budget 独立复验，通过后才启动 capture subprocess；
- blocked probe 保留诊断文件但 `capture_started=false`，不产生成功 session；成功 capture 会重开正式
  report，并将 receipt/workspace/campaign/probe/report/进度绑定进 closed-schema、digest-bound record；
- archived probe verifier 只放松“仍为 next leaf”条件，保留其余完整重算；旧 record 在后续叶推进后仍可
  验证，但归档 probe 不能再次授权 capture；
- 修复 v92/v93 生命周期冲突：移动介质/receive 输入仍精确限制 20 文件；收到的本机 workspace 只允许
  campaign 声明且正式验证通过的报告路径，未声明文件或失败报告仍 fail-closed；
- status output 不能覆盖 campaign/source/report/probe/binding。session schema/module 纳入 PX4 Mission parity，
  `px4_mission_path_hash=F8E8F4E0...4ACCE1`。真实设备未连接，未生成现场 session 或 acceptance。
- 全量回归 3,456 tests：`3455 passed, 1 skipped, 0 failed, 0 errors`，耗时 100.186 秒；JUnit
  504,362 bytes、SHA-256 `870BA2CB7B98CE62782AD5CE6FB24B4DD6B3E2E320D25351BBF49C1854732072`；
  全仓 Ruff 通过。

## v93 本轮已收口

- `field_covariance_workspace receive` 先重验移动介质 bundle，只复制 manifest 声明的精确 20 文件到目标
  同盘临时目录，再重验 workspace digest、当前 parity、相对路径 campaign 和六叶 source preflight，最后
  原子发布本机副本；
- destination/receipt 均为 write-new，拒绝覆盖、目标位于 source 内、receipt 位于 source/destination 内和
  symlink；回执写失败会删除本轮刚发布的 destination，源介质不变；
- 新增 closed-schema、canonical-digest-bound 的外部接收回执及 `verify-receipt`。拔除源介质后仍会重开
  本机 destination、逐文件 hash、campaign、parity 与 preflight；源绝对路径只作为事务 provenance；
- 回执核对 6 leaves、18 sources、20 JSON、下一条本机 preflight argv，且 live identity、covariance、
  hardware、HIL、REAL、flight 全部固定为 false；现场仍必须重新 probe/capture；
- receipt schema/module 已纳入 PX4 Mission parity，`px4_mission_path_hash` 更新为
  `6B5AB2EF...F85F02`。当前未接入物理介质或真实 Pixhawk，没有生成实际接收回执或 acceptance。
- 全量回归 3,445 tests：`3444 passed, 1 skipped, 0 failed, 0 errors`，耗时 96.462 秒；JUnit
  502,750 bytes、SHA-256 `D3DCDE46F027DD67C86D437AE879FAA11F14A4480C308B0F2F74CCDC49F7A31D`；
  全仓 Ruff 通过。

## v92 本轮已收口

- 新增 `field_covariance_workspace export/verify`，导出 HIL/REAL × 三机型精确六叶的 18 份 Mission、
  Mission feedback、signed runtime profile 原始字节，并生成只使用相对路径的 portable campaign；
- 导出前与临时目录原子发布前均运行既有六叶 source preflight。搬迁回归把完整目录复制到第二个根、删除
  原导出目录，再次加载 campaign 并执行真实 preflight，证明验证不依赖原绝对文件路径；
- 闭合 schema/manifest 绑定 20 文件全集、逐文件 size/SHA、六叶顺序、endpoint/baud、源 campaign
  fingerprint、当前 release parity 和 canonical workspace digest；额外文件、内容漂移、路径逃逸、ADS、
  反斜杠、symlink 与 parity 漂移均 fail-closed；
- bundle 明确不携带 online probe、covariance report 或 acceptance。接收机必须用本机连接重新运行
  `preflight -> probe-next -> capture-next`；endpoint/飞控/firmware/签名身份不会被便携层静默改写；
- `px4_mission_path_hash` 更新为 `B30A0DAE...AF266B`。当前仍没有真实六叶输入和串口设备，因此没有
  生成物理 handoff bundle 或任何 HIL/REAL、hardware、flight 通过声明。
- 全量回归 3,439 tests：`3438 passed, 1 skipped, 0 failed, 0 errors`，耗时 94.448 秒；JUnit
  501,885 bytes、SHA-256 `8714CCA8795DD428D9BEFB4323005E639B77D23445B895E5B1B8649418E46065`；
  全仓 Ruff 通过。

## v91 本轮已收口

- `probe-next` 输出升级为 `multidetect.pixhawk-covariance-online-preflight/v2` 闭合 observation，加入 UTC
  观测时间、canonical evidence SHA-256 和固定为 false 的 release/hardware/HIL/REAL/flight claims；
- source binding 覆盖 campaign、Mission、Mission feedback、runtime profile、online schema、campaign runner、
  covariance mode-binding authority 与当前五组 release parity；
- 新增 `verify-probe`：重开当前 campaign，要求相同 leaf 仍是 `next_leaf`、六叶 source preflight 仍全绿，
  并重算 authoritative mode binding、采样时序、firmware response、查询/TX 预算、reasons 与 gate；
- 文件加载默认 300 秒过期；未来时间、digest 篡改、未知字段、source/parity 漂移、campaign 前移和 symlink
  全部 fail-closed。该在线记录仍绑定本机绝对路径且不跨机复用；v92 仅搬迁静态六叶 workspace，接收机
  必须重新生成在线记录；
- `px4_mission_path_hash` 更新为 `792FFFBE...100780`。当前没有真实 campaign 配置、串口/PnP 设备或
  `field-inputs`，所以没有运行真实 probe/capture，也没有生成 HIL/REAL、hardware 或 flight 通过声明。
- 全量回归 3,430 tests：`3429 passed, 1 skipped, 0 failed, 0 errors`，耗时 93.046 秒；JUnit
  500,693 bytes、SHA-256 `EF040571787F7AAD5759548B60EC8CE66EB21E3E878F830AE36D060436D0E241`；
  全仓 Ruff 通过。

## v90 本轮已收口

- 新增 `field_covariance_campaign probe-next`：先要求 v89 六叶 source preflight 全绿，再只打开当前
  `next_leaf` 的 Pixhawk transport；
- probe 复用 covariance capture authoritative `build_mode_binding`，逐项核对 configured/resolved
  endpoint、HEARTBEAT 六元身份、当前 firmware hash、transport/qualified link、operational
  qualification、read-only 与 hardware-control-disabled；
- ranging stream 请求保持关闭；若 firmware 未被动广播，只允许最多三次
  `MAV_CMD_REQUEST_MESSAGE(512)` / `AUTOPILOT_VERSION(148)` 查询，并强制 TX 总数等于 attempts；
- 正式 `capture-next` 会再次运行 online probe；online gate 失败时明确输出 `capture_started=false`、
  `capture_verified=false`，不创建目录、不调用采集 subprocess；成功/失败都关闭 provider；
- online report 的 release/hardware/HIL/REAL/flight claims 固定为 false，它是现场操作状态而非 E7 证据。
  `px4_mission_path_hash` 更新为 `C7C86A1F...982AB218`。真实硬件当前不可用，因此没有伪造通过结果。
- covariance campaign/binding/capture 专项 62 项通过；全量回归 3,419 tests：
  `3418 passed, 1 skipped, 0 failed, 0 errors`，耗时 95.171 秒；JUnit 499,148 bytes、SHA-256
  `32D6BDC009476B67C1117A137E981642570A9004F4137626C1E66F8F180CAD53`；全仓 Ruff 通过。

## v89 本轮已收口

- `field_covariance_campaign preflight` 一次重开六叶 Mission、feedback v2 与 runtime profile，不访问
  transport，输出固定顺序的 `ready` / `blocked` 清单和三份 source SHA-256；
- 每叶核对当前机型 adapter ID/hash、HIL/REAL mode、configured endpoint、baud、feedback gate、
  receive-only/zero-transmit、runtime signature/profile gate、observed identity、flight-control disabled 与
  output locked；
- `capture-next` 在启动采集 subprocess 前强制要求全部六叶 READY，dry-run 也携带完整预检结果；源文件
  检查后发生变化会在下一次重新打开时被拒绝；
- 24 个 campaign runner 专项测试通过，覆盖六叶正例、八类错配、post-check source tamper、CLI 失败码和
  blocked 状态下 subprocess 零调用；`px4_mission_path_hash` 更新为 `3D3A77F6...CA52C95`；
- 全量回归 3,415 tests：`3414 passed, 1 skipped, 0 failed, 0 errors`，JUnit 498,573 bytes、
  SHA-256 `C370E39F2C7B2C93E609AFA5931E50603BED63D3DFECB2F68C0B78A105790E3C`；全仓 Ruff 通过；
- 这只关闭现场启动前静态配置缺口。当前 Pixhawk 在线 HEARTBEAT/firmware identity 仍由正式 capture
  验证；真实六叶 HIL/REAL、硬件、室外 GNSS、人工 QGC 与实飞均未声明，最终版本继续阻断。

## v88 本轮已收口

- 新增 `field_covariance_campaign` CLI 与严格 Draft 2020-12 配置 Schema，固定 HIL/REAL × 三机型六叶；
- `status` 从当前 source 重验每个已有报告并显示唯一下一叶；`capture-next --dry-run` 可在打开串口前
  审阅完整命令，正式调用每次只采一个叶子；
- 报告沿用 write-new 不覆盖；既有失败报告会中止 campaign。采集进程即使退出 0，runner 仍检查报告
  schema、source/parity、Mission/profile/feedback/link/firmware，失败则返回 1；
- 六叶通过后 `verify` 直接调用 v87 E7 builder，并生成完整 E7 CLI 参数；状态快照可原子更新，但不被当作
  release evidence；
- 提供 `configs/pixhawk-covariance-field-campaign.example.json`，Schema 与 runner 纳入 PX4 Mission parity。
  当前缺真实现场 source 与硬件，因此未执行 HIL/REAL 采集，最终版本保持阻断。
- `px4_mission_path_hash` 更新为 `E3371122...89B83`；全量回归 3,403 tests：
  `3402 passed, 1 skipped, 0 failed, 0 errors`，JUnit 496,778 bytes、SHA-256
  `DA2B4104B657A64051DB879E75B1C5DCD5B3C8ABDA90C02D242915A069BBA25A`；全仓 Ruff 通过。

## v87 本轮已收口

- 新增 `pixhawk_covariance` E7 六叶矩阵，覆盖 HIL/REAL × fixed-wing/multirotor/VTOL；预发布可缺省，
  一旦提供即 authoritative，最终 release 必须完整提供；
- 每叶不信任摘要字段，而是调用 schema-v3 observation verifier 重建报告，并重新打开 Mission、feedback、
  runtime profile；compact E7 记录与当前 source exact compare；
- binding 固定 runtime/airframe、report/source hash、Mission adapter、endpoint/baud、HEARTBEAT、firmware、
  current transport/qualified link、read-only/hardware-control-disabled 与 `px4_mission_path_hash`；
- manifest builder/API/CLI、严格 JSON Schema、evaluator、final-release blocker 和重复/篡改/source-drift
  回归已接线；parity 变化后旧 v86 SIM 报告按设计被共享 leaf verifier 拒绝；
- 真实六叶 HIL/REAL covariance 尚未采集，所以这轮关闭的是 E7 软件消费缺口，不宣称硬件、室外 GNSS、
  HIL/REAL 或实飞通过，最终 `v0.3.0` 保持阻断。
- `px4_mission_path_hash` 更新为 `AADF6672...41715`；全量回归 3,391 tests：
  `3390 passed, 1 skipped, 0 failed, 0 errors`，JUnit 494,984 bytes、SHA-256
  `A7FA11B6D5236372B0BC051B092E03F47AD899F25C95751C58F7ADA00C002A88`；全仓 Ruff 通过。

## v86 本轮已收口

- 新增严格 Draft 2020-12 covariance observation schema v3：顶层、record、diagnostics、request、
  heartbeat、firmware、HIL/REAL binding、checks、source binding 与 claims 全部闭合；
- writer 在原子写入前执行 Schema 验证，verifier 读取后先做 Schema 验证，再做 canonical digest、
  当前源码/source hash 与 derived checks/claims 的完整重建；
- 报告只保留 16 项门禁相关 diagnostics；motion/vertical/mission 等内部状态不再冻结进证据契约；
  tuple reasons 统一为 JSON array，缺 covariance 的失败报告仍能生成并被 verifier 复核；
- Schema path 与 SHA-256 直接进入 source binding，Schema 文件加入 PX4 Mission parity；`pixhawk`
  optional dependency 显式包含 `jsonschema>=4.20`；
- 当前 digest-pinned PX4 SITL 实采 223 条 ODOMETRY、223 条有效 covariance、保存 113 条记录；
  schema v3 写前/读后验证及独立 verifier 全部通过，容器已清理；
- Schema 22,231 bytes、SHA-256 `76DD77A2...A3B95`；证据 80,848 bytes、文件 SHA-256
  `98C7C297...AB02C`、evidence SHA-256 `C0579775...29A80`；
- `px4_mission_path_hash` 更新为 `6B1C9B22...F43D8`。HIL/REAL 实机、硬件、室外 GNSS、人工
  QGC 与实飞继续保持未通过，最终版本仍阻断。
- 全量回归 3,386 tests：`3385 passed, 1 skipped, 0 failed, 0 errors`；JUnit 494,307 bytes、
  SHA-256 `52AD5A17D952F5BA23A71631D9F4DAE59B43FF2C83AB461570ADC94590049376`；全仓 Ruff
  通过，历史 format 债务保持 412 个文件。

## v85 本轮已收口

- covariance report 新增 `transport_link_current`、`qualified_link_current` 与
  `autopilot_version_current_session` 三项硬门，拒绝断链缓存、未通过 operational qualification
  或不属于本次采集时窗的 firmware 身份；
- HIL/REAL `mode_binding` 同步加入 provider transport、qualified link 与 qualification 三项检查，
  即使 Mission/profile/firmware hash 匹配，当前链路不合格也不能形成现场 covariance 证据；
- 六个负回归覆盖 SIM 报告与 HIL binding 的 stale/unqualified 状态；相关 Pixhawk/capture 专项
  101 项通过；
- 当前源码在 digest 固定 PX4 SITL 独立进程实采 220 条 ODOMETRY、220 条有效 covariance、保存
  111 条记录；新增三门均通过，发送严格为 `3×7+1=22`，verifier 通过且容器已清理；
- 证据文件 81,010 bytes，SHA-256 `B2A1462F...DC3C8`，evidence SHA-256
  `23A37D0F...A3BC9`；firmware identity 为 `397A4F3B...73081`；
- `px4_mission_path_hash` 更新为 `7D18DFA5...5F514`。HIL/REAL 实机、硬件、室外 GNSS、人工
  QGC 和实飞声明保持未通过；严格 covariance JSON Schema 作为下一独立迭代。
- 全量回归 3,379 tests：`3378 passed, 1 skipped, 0 failed, 0 errors`；JUnit 493,089 bytes、
  SHA-256 `63351A22D8074BAD318A2796A8429CAEE70A22815189E0E3008D9903DF0CC5F7`；全仓 Ruff
  通过，历史 format 债务保持 412 个文件，没有因本轮增加。

## v84 本轮已收口

- Pixhawk provider 新增默认关闭的有界 firmware identity 请求：绑定 HEARTBEAT 后通过
  `MAV_CMD_REQUEST_MESSAGE(512)` 请求 `AUTOPILOT_VERSION(148)`，每个机体身份最多 3 次、5 秒退避，
  合法响应到达后立即停止；
- heartbeat 五元身份变化会清除旧 firmware，并重置该新身份自己的请求预算；默认 receive-only 模式
  继续零发送，整个路径不触碰参数、模式、Mission、执行器或 stream rate；
- covariance observation 要求 identity request 实际发生、response observed、firmware hash 合法，且
  总发送严格等于 `stream attempts×7 + identity attempts`；三次仍无响应则 fail-closed；
- 当前源码/当前 parity 的外部 pinned PX4/Gazebo 实采 207 条 ODOMETRY、207 条有效 covariance、保存
  112 条记录；PX4 第一次请求即返回 firmware，发送计数为 `3×7+1=22`，verifier 通过并清理容器；
- firmware identity hash 为 `8f527860...fdc81`，raw UID 没有进入报告；`px4_mission_path_hash` 更新为
  `3f118e57...4069c`。HIL/REAL 实机、硬件、室外 GNSS、人工 QGC 和实飞声明保持未通过。
- 全量回归 3,373 tests：`3372 passed, 1 skipped, 0 failed, 0 errors`；JUnit 491,817 bytes、
  SHA-256 `0CBB1A09D4D21F774A174078CAD68AABA686C32BA9AC709F6520F59ABCE4A996`；全仓 Ruff 通过，
  历史 format 债务仍为 412 个文件，没有混入本轮功能提交。

## v83 本轮已收口

- covariance observation 升级为 schema v2；HIL/REAL 采集必须给出可执行 Mission、PX4 Mission
  feedback v2 和签名 runtime profile，不能再用孤立 `--runtime-mode` 标签冒充现场证据；
- 每次现场采集都会重新验证三份来源，并把 configured/resolved endpoint、baud、HEARTBEAT 五元身份、
  当前 `AUTOPILOT_VERSION` firmware hash、read-only、hardware-control disabled、output locked 与
  flight-control disabled 绑定到 canonical `mode_binding`；任一漂移 fail-closed；
- SIM 仍只允许隔离 local `udpin`，且禁止携带 HIL/REAL 身份材料；HIL/REAL 会拒绝 `udp`、`udpin`、
  `udpout` 等所有受保护 14550 端点；
- 外部 pinned Docker/PX4/Gazebo v2 实采 110 条 `ODOMETRY`、110 条有效 covariance、保存 83 条记录；
  最终 `1σ=0.504°/0.501°/6.981°`，独立 verify 通过，容器已清理；
- `px4_mission_path_hash` 更新为 `5a4c280f...af93`。这里只关闭现场采集的身份/防伪工具缺口；真实
  HIL/REAL、硬件、室外 GNSS、人工 QGC 和实飞仍未声明，E7 仍为 32 blockers。
- 全量回归 3,369 tests：`3368 passed, 1 skipped, 0 failed, 0 errors`；JUnit 491,247 bytes、
  SHA-256 `CE988ADBD106AD029427957C2722C413673D1EA9A986E76351EED278AC187F54`；全仓 Ruff 通过。
  format 检查仍是 412 个历史文件的独立基线迁移债务，本轮没有把机械格式化混入功能提交。

## v82 本轮已收口

- Pixhawk 只读 ingress 请求并解析 MAVLink `ODOMETRY` 331 的 21 项 pose covariance，提取姿态 3×3
  block，要求 finite、正方差和 PSD，并使用 `0.3°/0.3°/1.0°` 显式下限；
- covariance 必须来自已确认 HEARTBEAT 的同 system/component、`LOCAL_NED` parent、合法 child frame，
  且与当前姿态相差不超过 250 ms；无效新样本会清空旧 uncertainty，不复用假精度；
- `BODY_FRD` child frame 的 twist 不再映射为 north/east，避免机体系速度污染导航速度；
- 外部 pinned Docker/PX4/Gazebo 实采 64 条 `ODOMETRY`，64 条 covariance 通过，保存 47 条记录；最终
  raw angular variance 为 `6.9689e-05/6.8983e-05/1.6177e-02 rad²`，派生 `1σ` 为
  `0.478°/0.476°/7.287°`，source 为
  `mavlink_odometry_pose_covariance_local_ned_with_configured_floor`；
- 采集报告以 canonical SHA-256 绑定 provider、采集/验证脚本和 release parity，并从 raw records 与
  diagnostics 重建全部 checks/claims；SIM gate 通过、容器清理通过，HIL/REAL/硬件/室外/实飞仍未声明；
- `px4_mission_path_hash` 更新为 `16e8427a...e1930`。E7 仍为 32 blockers，最终版本仍阻断。

## v81 本轮已收口

- fixed-wing、multirotor、VTOL 各以 pinned Docker/PX4/Gazebo 外部进程采集 3 帧 `MDGZSIM3`；
- 三份 runtime/sensor gate、逐帧重构、record hash、frame-set manifest 与容器清理全部通过；
- 最终 roll/pitch/heading sigma 与来源进入 frame provenance 和 latest telemetry，验收器从原始 wire
  covariance 重算后逐项比较，来源或数值篡改会在 detector 前拒绝；
- 三类机型的原始姿态 covariance 实测均为 9 项全零，manifest 记录 nonzero count=0；系统应用
  `0.3°/0.3°/1.0°` 配置 floor 并明确标注来源，不声称收到 estimator covariance；
- `core_algorithm_hash` 更新为 `ed102767...53f`。E7 仍为 32 blockers，现场声明不变。

## v80 本轮已收口

- `MDGZSIM3` 固定 276-byte header 完整携带 Gazebo pose covariance 的姿态 3×3 block，并纳入 record hash；
- producer/backend 双端要求矩阵 9 项 finite、对称、正半定；缺失 36 项源矩阵或 malformed wire 拒绝；
- fixed-axis X/Y/Z covariance 经数值 Jacobian 转为 NED/FRD Euler `1σ`，配置 floor 在变换后应用；
- gimbal lock 使用 `180°/90°/180°` 保守预算；原始矩阵和实际 sigma/source 均进入逐帧证据；
- producer/backend 新增到 `core_algorithm_hash`。外部 Docker/PX4 v3 帧已在 v81 三机型重采；E7 仍为 32 blockers。

## v79 本轮已收口

- `VehicleTelemetry`/Replay/raw replay 可携带完整 roll/pitch/heading `1σ` 与来源，历史全缺失记录不漂移；
- Live 优先消费遥测 uncertainty，整组缺失时明确标记 `live_configured_fallback`，部分或无来源元组拒绝；
- VI、RGB-SLAM 与主 ranging 使用同一 pose resolver，避免三条链各自静默吃 dataclass 默认值；
- `RangeSolution` 和 operator status 记录实际 sigma/source；Gazebo wire-v2 当时丢弃 covariance 的缺口
  已明确保留给下一 wire 版本，并在 v80 由 wire-v3 关闭，不把消息名称误报为已接入实测协方差。

## v78 本轮已收口

- 确认杆臂均值只在 ranging NED offset 应用一次，WGS-84 投影没有重复叠加；
- 将每个姿态/mount/像素 `±1σ` 样本升级为 ray 与 camera-offset 成对几何端点；
- camera slant、DEM 交点、ground range 与 bearing sigma 共用端点传播，姿态扰动同时旋转杆臂；
- 新增短距离大杆臂负回归：旧 ray-only 模型会返回与零杆臂相同 sigma，新模型明确区分。

## v77 本轮已收口

- `field-evidence-bundle.schema.json` 的 `bindings` 改为严格、无未知字段的机器合同；
- 八项输入 hash/bytes、时间策略、target-event、recording、mount 与 calibration bindings 均有显式类型；
- pending 与 complete 报告均经 Draft 2020-12 验证，任意额外 claim 会被 schema 拒绝；
- verifier 仍重开所有来源并比较重算结果；结构通过不能代替来源真实性或 HIL/REAL 验收。

## v76 本轮已收口

- target-event replay bundle 新增 contained `camera-calibration.json`，manifest 绑定 calibration ID、
  bytes 与 SHA-256，加载时重新哈希并运行完整相机标定语义校验；
- capture CLI 支持 `--camera-calibration` 与 manifest 相对路径，现场采集无需手工计算哈希；
- field evidence 的 target-event 层现在必须携带标定，并与 camera-mount survey 的 calibration ID/SHA-256
  跨证据一致；缺失、篡改或同 ID 不同内参全部 fail-closed；
- target-event manifest schema 纳入 shared core parity。本轮仍不产生 HIL/REAL、硬件或发布声明。

## v75 本轮已收口

- 新增 `equipment-record` CLI 和严格 schema，自动绑定证书文件 hash、issuer、有效期、设备身份与精度；
- survey 重新打开 calibration record 和嵌套证书，机器检查测量时间窗口、设备 ID、角度/线性精度；
- 过期记录、错设备、证书文件事后变化全部 fail-closed，任意 hash 文件不再等同有效设备校准；
- schema 纳入 shared task parity；真实证书签发真实性仍需现场复核，不由自动 fixture 代替。

## v74 本轮已收口

- `template` 原子生成 5–1000 行 canonical raw CSV，默认拒绝覆盖和 symlink；
- `validate-raw` 在正式 survey 前检查 hash、列、数值、范围、最小样本数和 ID 唯一性，并输出严格
  `camera-mount-measurement-preflight.v1` 诊断；
- blank template fail-closed、填充后 preflight pass、源码树 CLI round-trip 和 schema 回归均覆盖；
- 预检 schema 纳入 task parity，且所有输出继续明确不构成 hardware/release/HIL/REAL 接受。

## v73 本轮已收口

- 原始 mount-survey CSV 变成唯一统计数据源；固定 v1 header，严格解析 sample ID 与六个有限数值；
- CLI 删除分离的 `--sample` 输入，独立 verifier 重新读取同一 hash-bound CSV 并重算；
- Python API 若显式 samples 与 CSV 任一字段不同，`samples_match_raw_measurements=false` 并 fail-closed；
- shared task parity 已重新生成。该收口不改变真实测量/HIL/REAL/硬件/最终发布状态。

## v72 本轮已收口

- 新增物理 camera mount / GNSS-to-camera lever-arm survey 生成器、严格 schema、源码树 CLI 和独立
  re-hash/recompute verifier；三类输入均 hash-bound，至少 5 次重复测量且 surveyor/reviewer 必须不同；
- field evidence bundle 新增第七个 `camera_mount` 强制层，并保存 survey、aircraft、camera、GNSS antenna
  与 calibration SHA-256 绑定；缺失、篡改、模拟值、声明缺失或 uncertainty 低报全部 fail-closed；
- survey 与七层 bundle 已纳入 shared `task_contract_hash`。自动 fixture 仅验证合同，不关闭真实测量、
  HIL/REAL、硬件或最终发布门。

GitHub 审计基线：`origin/main`，提交 `b5c3f10baf4cc9febaa3a10103752f202f3b0611`；本轮功能基线为
本地 `main` 提交 `5fb7303722a3ccd6d8b5df1a3de388e72490e4f2`（当前文档提交前领先远端 56 个提交）。

产品版本：`v0.3.0-pre.8`（最终 `v0.3.0` 尚未发布）

机器可读镜像：[current-progress.json](current-progress.json)

## 结论先行

v71 已在干净提交 `73429e505d33ca5c645be3d3d0b6cda667f33c5f` 上重跑完整
PX4/Gazebo/QGC Combined SIM 生命周期；source 明确记录同一 HEAD、`dirty=false` 和空 Git status
摘要，独立 verifier 44/44 通过。原生 QGC 的真实目标点击继续按
QML 候选框几何映射到 exact PID/HWND，发送一次 `SELECT_TRK`，验证 ACK 与后续 TRACKING 状态均与
同一 command/target 相关，再让该 operator target 驱动共享测距和目标定位。相机安装俯角同步修正为
35°，GPS/DEM/相机地面射线链恢复可观测区域；测距 contribution wire 权重改为总预算量化，避免极小
VIO 权重与主来源四舍五入后总和超过 1 而间歇性拒绝 packet。当前 parity 的权威 900 帧 run 中，
832 份 range 和 832 份 geolocation status 全部发出，336 帧合格落图、496 帧明确不可定位、0 transport
error，Combined、VTOL attached 与 7-item Mission 均通过。

当前仓库已经形成统一的感知、跟踪、测距、目标落图、QGC、数字孪生、任务规划、PX4 Mission、
operator link、安全和证据框架。v59 已让三组各 5 帧的实际 Gazebo BGR24 像素经过同一候选 ONNX、
共享语义地面锚点、DEM/GNSS 测距和 WGS-84 投影；三个场景覆盖约 `6.20/12.17/20.15 m` AGL、
`8/15/25 m` 目标距离和 `0/4.00/7.97 deg` 坡度，均由独立 verifier 重现。对应近地平线输入 5/5 帧
都拒绝生成坐标。v60 又把 high-slope evidence 的测距、坐标和 1σ 经签名 operator link 送入现有
原生 QGC。v61 在同一 HIL 进程内持续刷新 type 10/22，按 exact PID/HWND 采集无外部遮挡的目标地图
与证据卡片机器截图，并把证据与当前五组 release parity 严格绑定。v62 又让固定翼、多旋翼和 VTOL
通过同一 AI/MAVLink 路径驱动 disposable PX4/Gazebo，以 `GLOBAL_POSITION_INT` 证明实际位移，并将
可重放的三机型矩阵正式接入 E7。v63 又完成每机型两次正常响应与三种显式故障，共 15 份外部
PX4/Gazebo 源报告，并把 repeatability/fault campaign 接入 E7。v64 新增 source-bound QGC 人工视觉
review 合同、12 项 checklist、0 假点负控和独立 verifier。v65 已把它接入 E7，并强制要求
REPLAY/SIM/HIL/REAL 各自独立的机器来源与人工记录；当前仍是 0/4，人工签字和现场证据未完成。
v66 进一步把机器捕获抽成四模式共用的 schema-v2 契约，绑定实际 EXE、parity、run、exact PID/client
rect、遮挡和 PNG；历史 PX4 报告仅保留 HIL 兼容，REPLAY/SIM/REAL 不再伪装成 HIL 格式。v67 修正
Gazebo Harmonic 的传感器时间语义：CameraInfo 是会话级静态标定，NavSat/Odometry 仍受 1.5 秒动态门；
短暂 transport callback 错序只丢弃不安全图像。v68 又把实际 CameraInfo K/P/R/D、PLUMB_BOB 与仓库
标定逐字段绑定，并将语义状态带入 runtime/frame-set。v69 再由独立 verifier 强制该状态、sensor 完整
计数与逐帧 odometry。当前权威 900 帧联合 SIM 安全丢弃 6 张不新鲜动态图像，并以增强 verifier 44/44
通过；900 个有效帧全部取得 QGC post-adoption type-24 ACK。人工记录仍为 0/4。

最终版本仍然阻断。当前 E7 report-only 结果为 `gate_passed=false`、`release_allowed=false`，共
32 条 blocker，主要来自部署域数据/模型精度、现场链路、四模式人工 QGC/Mission、HIL/REAL、
三机型硬件控制和 prerelease 版本面。候选 YOLO 模型仍为 `quarantined`，不是生产批准模型。

## v71 干净 Combined 基线（v72 回归沿用）

- 在干净 `73429e5` 上完成新的权威 source
  `combined-sim-qgc-20260905T175019Z-64536`：259,353 bytes，SHA-256
  `5031EEFE75CF78D42AECCC04E990BB8F9975FBC6EBB12D4087F685C18652C8B6`；source 自证
  `git_head=73429e5`、`dirty=false`。
- 900/900 sensor、RTSP、signed type-23、post-adoption type-24 和 full odometry 通过；832 份
  range/geolocation 中 336 份可落图，496 份按质量门明确 withheld，0 operator transport error；
  最大 frame-to-acceptance `609 ms`，最大 ACK-to-metadata skew `235 ms`。
- exact PID/HWND 上第一次命中 `flame` 候选框；`SELECT_TRK` ACK、TRACKING 和 QML TRK 绑定同一目标。
  VTOL Mission 完成 MC/FW/MC、land/disarm，最大离家 `226.949 m`、最大相对高度 `26.078 m`；容器、
  9 个动态端口与受保护 14550 状态均正确清理。
- 独立 verifier 44/44：2,139 bytes，SHA-256
  `0C27451F88A0FD3655740523525D97BAE98D8D981EE0A3CC60DB8178B9A1136D`。
- v82 全仓回归为 3,358 tests：`3357 passed, 1 skipped, 0 failed, 0 errors`；JUnit 为 489,660 bytes、
  SHA-256 `e9306292...f81d9`、suite time 94.158 s；全仓 Ruff 通过。
- 该 run 只把 v70 的 current-parity dirty 机器证据提升为 clean-HEAD software/SIM provenance；人工
  视觉仍为 0/4，真实 Jetson/Pixhawk/相机、室外 GNSS/RTK、HIL/REAL 和实飞继续阻断最终版本。

## v70 本轮已收口

- 原生 QGC Fly View 候选目标支持 source-bound 点击，`SELECT_TRK` ACK、TRACKING 状态和 QML
  `TRK` adoption 必须属于同一 command/target；取消仍保持单击状态机语义。
- active operator target 进入共享 multimodal ranging；GPS/测距合格时发布 WGS-84/1σ，任何质量门
  不合格时发布明确不可定位状态，不落假点，也不产生飞控或载荷指令。
- Gazebo VTOL 相机与标定统一为 35° 俯角；range contribution 使用 254 单位总预算量化，保留每个
  非零来源且保证解码权重总和不超过 1。主来源约 0.99875、VIO 约 0.00125 的回归已覆盖。
- 当前 parity 真实 source `combined-sim-qgc-20260905T172856Z-51520`：258,942 bytes，SHA-256
  `FE45B4DCAAD69AEFA7871609A58D33723547C1B46B5A708DBF1A00E495DC59E0`；900/900 frame/type23/type24，
  831/831 range/geolocation status，112 个合格坐标，0 transport error，最大端到端 ACK 609 ms。
- 原生 QGC 点击第一次命中，相关目标 `flame` 进入 TRK；native QQuickItem pixels、transport-bound
  visible surface 和 toolbar smoke 均通过。VTOL Mission 最大离家 `134.918 m`、相对高度 `24.116 m`，
  MC/FW/MC、land/disarm 完整；容器移除、9 个测试端口释放、14550 不变。
- portable EXE 为 46,579,712 bytes，SHA-256
  `FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`。
- 当前 HEAD 全仓 3,317 tests：`3316 passed, 1 skipped, 0 failed, 0 errors`；JUnit 482,309 bytes，
  SHA-256 `67CBBE1C25256FA4619B375819748BFD9605CD4A2BC8F898CB7D7042DF5DE203`；Ruff 全仓通过。
- 以上仍是 dirty-worktree、current-parity 的 software/SIM 机器证据，不声明人工视觉、真实硬件、
  室外 GNSS/RTK、HIL/REAL 或实飞；最终 `v0.3.0` 继续阻断。

## v69 本轮已收口

- independent combined verifier 新增 `camera_info_intrinsics_semantics_validated`、
  `sensor_frames_complete`、`odometry_frames_complete` 三个硬检查；旧 pending/missing/tampered 结构均失败。
- combined runner 在写最终 source 前也强制 exact CameraInfo semantic status，防止 runner 根门掩盖传感器
  语义缺失；E7 fixture 已升级到同一契约。
- 当前权威 source `combined-sim-qgc-20260905T152520Z-75420` 来自干净 `46920e1`：900/900 sensor、
  RTSP、type-23、post-adoption type-24、full odometry；6 张 stale dynamic 图像被拒绝，0 张旧帧发出。
- source 255,256 bytes / SHA-256
  `9CAB1BC405571C2F287D1545D77B7EA1F7C4A56CC163AB76E1402CB362DB121B`；增强 verifier 2,139 bytes /
  SHA-256 `1EEFD154DF164F307D62EF1CFFCC7479E343B8E3FC6A7017A9BBE61EB19502D0`，44/44 checks 通过。
- Mission 最大水平距离 `130.616 m`、最大相对高度 `23.857 m`；PiP 第 1 次精确命中；容器移除、
  9 个动态端口释放、14550 不变。
- 当前 HEAD 全仓 3,309 tests：`3308 passed, 1 skipped, 0 failed, 0 errors`；JUnit 481,013 bytes、
  SHA-256 `3AF69527D05CCC985502C195D09DDE44793D7280111B04410B8B2DCB3C3661C6`；ruff 全仓通过。
- 最终版本仍阻断：四模式人工视觉 0/4、HIL/REAL、真实硬件、室外 GNSS/RTK 和实飞均未完成。

## v68 本轮已收口

- `gazebo_sensor_stream.py` 对实际 `CameraInfo` 的 width/height、K/P/R、distortion model 与 D 执行
  fail-closed 语义验证；标准 pinhole 零位、R identity、零 stereo translation 与仓库标定任一不符即失败。
- `GazeboSimBackend`、CLI、standalone/combined runner 均要求显式 camera calibration。canonical 标定 hash
  进入 sensor binding identity，验证状态进入 runtime result 与 persisted frame-set source attribution。
- 真实三帧诊断 `gazebo-camera-semantics-20260905T150106Z.json` 证明 K/P/R/D；Gazebo float 序列化的
  最大焦距差约 `3.53e-5 px`，故 intrinsic 使用 `1e-4 px` 明确容差，R/D 保持 `1e-12`。
- QGC PiP 原生点击增加最多 8 次的有界精确重试；每次仍同时验证坐标、QGC PID/HWND ownership 和
  foreground。本轮真实运行 `pointer_position_attempts=1`，不是靠放宽 ownership 通过。
- 权威 source `combined-sim-qgc-20260905T150510Z-27780`：900/900 sensor、RTSP、signed type-23、
  post-adoption type-24 和 full odometry；7 张 stale dynamic 图像被拒绝，0 张 stale frame 被发出。
- source 256,194 bytes / SHA-256
  `E800968AB63BF847CA53BE7798EAD3B2501B731E958D76AB5E8EAC447A999471`；独立 verifier 41/41，
  verifier SHA-256 `3D58600C9B57ED981C651DE79CCA90F0FED275096E9FD5CA416B104016E51078`。
- 7-item Mission 完成 upload/readback/commit/START_PERMITTED 和 VTOL 全生命周期；最大水平距离
  `129.057 m`、最大相对高度 `22.082 m`。容器移除、9 个动态端口释放、14550 ownership 不变。
- 当前 HEAD 全仓 3,306 tests：`3305 passed, 1 skipped, 0 failed, 0 errors`；JUnit 480,349 bytes、
  SHA-256 `0414978C7C13B2726CCA9CB94153F018D5AC16B8A399748255E52F9C08DA64AA`；ruff 全仓通过。
- CameraInfo K/P/R/D 机器语义子门已关闭；目标测距/落图并未在本次 900 帧联合 run 中重新配置。
  四模式人工视觉仍为 0/4，HIL/REAL、硬件、室外 GNSS/RTK 和实飞继续阻断最终 `v0.3.0`。

## v67 本轮已收口

- `CameraInfo` 保持正时间戳、因果性和逐帧 width/height 绑定，但按 Harmonic 实际行为视为会话静态标定；
  NavSat/Odometry 的 1.5 秒动态时效限制没有放宽。
- 独立 worker 短暂错序时丢弃无法绑定新鲜动态遥测的 image；若 gap 持续，helper 仍由统一 deadline
  fail-closed。新增计数分别记录 stale dynamic/NavSat/Odometry rejection。
- 中间失败 source `combined-sim-qgc-20260905T141805Z-19928` 在 532 emitted frames 暴露 NavSat
  `7,000,000,000 ns` 落后，门正确失败、容器和端口清理完成；它保留为根因 provenance。
- 最终 source `combined-sim-qgc-20260905T142324Z-32372`：900/900 sensor、RTSP、signed type-23、
  post-adoption type-24；4 张 stale dynamic 图像安全丢弃，VTOL attached、7-item Mission、QGC toolbar、
  router 和 cleanup 全部通过。
- source 255,480 bytes / SHA-256
  `FD386165212A68C6E2D053A0D58B05D796565AB716CB088439F0A6756EDC1090`；独立 verifier 2,006 bytes /
  SHA-256 `C195FFC92E88F53C54C4AC07C62891BD3806C13E97B47692F895DB9988DEAD60`，41/41 checks 全绿。
- 四模式 machine-binding adapter 对该 source 的 SIM 绑定检查 19/19 通过；将同一 source 指定为 HIL 时
  只在 `runtime_mode_matches` 失败，跨模式重标负控有效。该检查没有创建人工 accepted record。
- 全仓 3,294 tests：`3293 passed, 1 skipped, 0 failed, 0 errors`；JUnit 478,731 bytes、SHA-256
  `8261544D868E9222A22D7E5EFE5E1F437B19ECEFF5DB1567ECC02C3C38F617CD`。
- 该机器门没有配置 target-geolocation acceptance，CameraInfo K/P/畸变语义仍 pending；四模式人工
  视觉、HIL/REAL、硬件、室外 GNSS/RTK 和实飞不因本轮结果提升。

## v66 本轮已收口

- `capture_qgc_native_style.ps1` 与仓库 JSON Schema 定义 schema v2 并强制显式 runtime mode；报告绑定
  run ID、实际 QGC
  EXE identity、canonical parity identity/五组摘要、exact PID/client rect、遮挡状态和 PNG identity。
- 单记录 verifier 使用 native-v2 / legacy-HIL 双适配器；新报告直接覆盖四模式，旧 PX4 event 仅允许
  HIL。mode/PID/rect/遮挡/PNG/EXE/parity/UI contract 任一不一致都会关闭记录。
- HIL acceptance wrapper 已传入明确的 `hil`、同一 run ID 和 canonical parity。当前仍未产生任何人工
  accepted record，因此 E7 仍为 0/4、32 blockers、最终 `v0.3.0` 继续阻断。
- 全仓 3,286 tests：`3285 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,631 bytes，SHA-256
  `15F37922F866E1FCFBEDE011A461185333E25244B70447629955363635356C78`。Ruff、compileall、schema、
  PowerShell parser 与 parity 重算通过；parity 文件前后同为
  `289C1A3DD638C2A503ABEA44EE3F87564BB38D1232FB1428E92EA004180D060B`。

## v65 本轮已收口

- 人工记录新增强制 `runtime_mode`，且必须与绑定 machine report 一致；旧 v61 图只属于 HIL，不能
  冒充 REPLAY/SIM/REAL。
- E7 manifest writer/evaluator/schema/CLI/wrapper 已接入 `qgc_operator_visual_acceptance` 四模式集合；
  每次评估会重新打开记录和全部源文件，复算 review digest、source identity 与 verifier 结果。
- 集合必须按 `replay/sim/hil/real` 覆盖 4/4；重复 mode、重复记录、复用 machine report 或相同
  screenshot、文件篡改和任一缺失均 fail-closed。当前 accepted records 为 0/4，因此集合
  `present=false/passed=false`，总发布门继续关闭。
- release-gate schema 改动使 `px4_mission_path_hash` 更新为
  `F0804FEB609A9CBBBCE6E3932ED77BD7852C9AF71BDB612A76BBD56EAD12247A`；matrix、campaign、external
  join/revalidation 与当前 E7 已从不可变源按新 parity 重建并独立复验通过。
- 全仓 3,282 tests：`3281 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,082 bytes，SHA-256
  `C893DDEDEE6A07F659F354C0C3DD13214DFA81014F08EEC380F977ED4C77EE7A`；Ruff、compileall、全部 JSON
  Schema、PowerShell parser、E7 wrapper 与 campaign/matrix verifier 通过。

## v64 本轮已收口

- 新增 `multidetect.qgc_visual_acceptance` 和 `multidetect-qgc-visual-review` CLI，绑定 v61 exact
  PID/HWND machine report、原始 PNG、实际 QGC EXE、当前 `ui_contract_hash` 与 geolocation 负控。
- 12 项人工 checklist 覆盖单一原生 surface、地图点、1σ 圆、经纬度、类别、距离、方位、更新时间、
  复制、快照、记录和只读声明；缺项、false 或非 accepted decision 均关闭 gate。
- 负控必须至少 5 帧均有检测、全部 range invalid、全部包含预期 fail-closed reason 且 0 个假坐标；
  当前 v58 负控 source-binding 预检为 5/5 检测、0/5 坐标。
- 新增严格 JSON Schema、内容完整性摘要、独立重开 verifier 和 7 项定向回归。全仓 3,276 tests 为
  `3275 passed, 1 skipped, 0 failed, 0 errors`；JUnit 476,289 bytes，SHA-256
  `CA7A4F84146518BE23B47F9DFB0375BE58F77EF01846829375BFC3A323FD8881`。当前没有 accepted 人工 record，
  因此 `qgc_manual_visual_acceptance=not_claimed`，E7 blocker 数不变。

## v63 本轮已收口

- 固定翼、多旋翼、VTOL 各完成 2 次正常 reported-position 响应；位移 P50/P95 分别为
  `36.414368/38.702007 m`、`2.446236/3.593912 m`、`3.951534/4.101479 m`。
- 三机型分别执行 `target_refresh_dropout`、`heartbeat_dropout`、`flight_mode_loss`，共 9 次真实
  Docker/PX4 负向运行；每次 runtime gate 保持通过，对应状态分别为
  `target_refresh_not_sustained`、`heartbeat_continuity_failed`、`flight_mode_loss_observed`，全部
  `fail_closed=true`。
- 新增 campaign builder/verifier/schema，重开 6 份 positive 与 9 份 fault 源报告并复算 P50/P95、
  源哈希和 campaign digest。当前 campaign SHA-256 为
  `BCD78748FD9D727E0E9C91510B7F82BE6AF3BDFEEA72A0022755FC1F6643CD81`。
- E7 新增 `px4_reported_position_response_campaign`；当前 matrix/campaign 均
  `present=true`、`passed=true`，总体仍有 32 个真实 blocker。
- canonical release parity 文件 SHA-256 为
  `E329C0BCFDC9673044D5E44D328B955B00902A97C6FCD545878EB31BDDF0B7FE`，
  `px4_mission_path_hash=4B5A95F3A89F35557820FD530A105B011EC0095E8E84F24701291AC29DA802DD`。
- 全量回归 3,269 tests：`3268 passed, 1 skipped, 0 failed, 0 errors`；JUnit 475,433 bytes、
  SHA-256 `66B5910ED572B909D8533A09E683AB434DC70798BF62EFB3B80246A82686C199`；全仓 Ruff、compileall、
  campaign/matrix 独立复验和 JSON Schema 校验均通过。

## v62 本轮已收口

- 固定翼、多旋翼、VTOL 的外部 PX4/Gazebo 动态响应全部通过；8 秒窗口内最大水平位移分别为
  `33.872547 / 1.171039 / 3.784929 m`，PX4 position samples 为 `262 / 223 / 245`。
- 三者都使用 `SimulatorTelemetryBridge → SimulatorAiExecutor → SimulatorMavlinkControlAdapter →
  MAVLink MANUAL_CONTROL → PX4 SITL/Gazebo`；目标更新无拒绝、POSCTL 无 mode-loss、heartbeat 连续。
- 新增 source-bound matrix builder/verifier 与 schema。矩阵会重开三份报告、scenario manifest、当前
  release parity 和三个工具源码，拒绝真值消费、pose 注入、非 pinned image、残留容器、硬件声明或
  任一机型/刷新/心跳/位移门失败。当前 matrix SHA-256 为
  `7124F746870F8CC2ADCF7A78A385EBE8190320CBAB67D972D437CAAC902C00CA`。
- E7 writer/evaluator/schema/CLI 和当前 wrapper 已加入
  `px4_reported_position_response_matrix`；当前绑定 `present=true`、`passed=true`，总门仍有 32 条真实
  blocker，未提升硬件、HIL、REAL、人工视觉或实飞声明。
- `release-parity.json` 已刷新，`px4_mission_path_hash` 为
  `B79C941AF889BAB50B2F42E674EC6662921233926E1C9271A65CEB9FC2702E55`；external join 与 E7 均已按
  新身份重建。
- 全量回归 3,261 tests：`3260 passed, 1 skipped, 0 failed, 0 errors`；JUnit 474,112 bytes、
  SHA-256 `746E1897E4E59BEC2CAF2BACA16592470C1FD224633815F704DAE62DC824341B`；全仓 Ruff 与 compileall
  通过。

## v61 本轮已收口

- HIL 驱动现在在授权完成后按 `0.2 s` 周期有界刷新 evidence-derived type 10/22，并在发送前严格
  校验 canonical `release-parity.json` 的版本和五组 hash；任何旧 UI/core/task/safety/Mission 身份
  都会 fail-closed。
- 原生视觉采集支持附着已经运行的精确 `MultiDetectGCS` PID，核对 exact HWND/client rect、单一原生
  surface、目标尺寸和外部遮挡恢复；QGC 的验收专用延迟退出仅由显式环境变量启用，默认行为不变。
- 最新 pinned PX4 SIH + 原生 QGC acceptance 通过：132 个认证 metadata、type 10/22 各 47 个、
  60 次定位刷新、14 个 PX4 heartbeat；禁止写入、物理输出和 14550 端口均保持为 0/不变，所有测试
  资源清理完成。acceptance SHA-256 为
  `BF9E9856F91244161CDB38E8FE5DDA9BC6972A0BFC17E989A427BDAA296A2861`。
- exact PID `4900` 的机器截图为 2,580,889 bytes、SHA-256
  `86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3`；画面包含目标点、1σ、类别、
  距离、方位、更新时间、帧摘要和复制/保存/记录入口。该项只声明机器采集通过，
  `operator_visual_acceptance=not_claimed`。
- QGC Release/portable 已从当前源码重建且字节一致：46,576,640 bytes、SHA-256
  `62F832239C375998E8431985DA02BB063ABEBA69BDB551E46527D2EFD97F747F`。
- 全量回归 3,257 tests：`3256 passed, 1 skipped, 0 failed, 0 errors`；JUnit 473,487 bytes、
  SHA-256 `E5D307C7F4AC57A4198358FBF84CFD034B27C20F415F2D1B7BF8F49850CB0837`。

## v60 本轮已收口

- QGC HIL 驱动新增可选 evidence intake：只接受 schema v2、`gate_passed=true` 且 display-only 的
  Gazebo geolocation evidence，保留其文件 SHA、内部摘要、场景和 release identity；失败/不完整输入
  在任何网络发送前拒绝。
- 最后一帧实际融合结果被重绑定到同一实时 target/frame，作为 type 10 `RANGE_STATUS` 与 type 22
  `TARGET_GEOLOCATION_STATUS` 通过既有 MAVLink 2 签名 localhost operator link 进入原生 QGC。
- 实际 pinned PX4 SIH + 原生 QGC acceptance 通过：40 个认证 metadata、type 10/22 各 1 个、PX4
  heartbeat 6 个；forbidden/arm/Mission/FTP/physical output 均为 0，14550 不变，端口、容器和进程
  全清理，无新 crash dump。证据 SHA-256 为
  `9E40159EEC767CD8F1AEF106BCC7E48ED9C00BC0DDFC7C1E4CECC7D2A060D053`。
- 全量回归 3,255 tests：`3254 passed, 1 skipped`；JUnit 473,177 bytes，SHA-256
  `0BD69A52C24CC8EC1438988CBB54A2223DFC656FB10D31F6C80D18B0DA725BE5`。Ruff、compileall、diff check
  通过；临时 parity 与 canonical SHA 相同。QGC C++/QML、五组 parity 和 EXE 字节均未改变。
- 这是 evidence→wire→native state 的机器验收，不冒充地图点、1σ 圆、卡片/快照的人工视觉验收，
  也不提升 E7、真实 GNSS、硬件 HIL/REAL、实飞或最终版本状态；E7 仍为 32 blockers。

## v59 本轮已收口

- 新增 mid/high slope 两套完整 scenario/runtime/truth/expectations、GeoJSON、DEM 和 SDF 资产；自动测试
  对 channel hash、DEM/SDF/truth、WGS-84、坡面法向、billboard 尺寸和相机投影逐项对账。
- disposable PX4/Gazebo Docker 分别采集两组各 5 帧实际 camera/NavSat/Odometry，Docker acceptance
  均通过并安全 stop/rm；对应定位 evidence 均 5/5 落图，独立 verifier 都
  `passed=true/reproduced=true`。
- v58 定义的矩阵现 `gate_passed=true`：正向 `3/3` 场景、`15/15` 帧，高度/距离/坡度档全部满足；
  负向 `1/1` 场景、5/5 检测、0/5 坐标。三个正向工况的最大水平误差分别为
  `0.674040 / 1.175656 / 1.855370 m`，1σ 和 range CI95 coverage 均为 `1.0`。
- Gazebo helper 只在首个有效 Odometry 与首帧之前有界丢弃最多 8 个出生点速度毛刺；第 9 个或进入
  有效流后的相同异常仍立即 fail-closed。Docker capture 异常路径会先关闭 backend 并保留 helper stderr。
- 本轮未改变五组 release parity；临时重建文件与 canonical parity 逐字节一致。QGC C++/QML 未改，
  没有重复构建内容相同的 EXE。
- 全量回归 3,251 tests：`3250 passed, 1 skipped, 0 failed, 0 errors`；JUnit 472,512 bytes，
  SHA-256 `9F62B10AA176760E9F4CB8C4431CAF62EA9E28748D22C42C749D7C942DD100E1`。全仓 Ruff、
  compileall 与 diff check 通过；唯一 skip 仍是 Windows symlink 能力项。
- E7 尚未接入 v59 matrix，仍为 32 blockers；software/SIM 结果没有提升候选模型、QGC 人工视觉、
  室外 GNSS、硬件、HIL/REAL、实飞或最终版本状态。

## v57 仓库状态重整与定位复核

- 以远端 `origin/main` 最新提交为基线重新盘点源码、README、CHANGELOG、路线图、配置、测试和
  本地证据，不再依赖已丢失的聊天历史。
- 修复远端已提交但不可运行的 Gazebo 定位证据 verifier：补齐仓库内路径解析、SHA-256 格式校验、
  去时间戳确定性结果摘要和公开导出；复核时会重新打开 scenario、frame-set、模型和 manifest，
  重跑推理/测距/定位并比较当前五组 release parity。
- 新增 verifier 正向复现与“共享核心任意字节变化后旧证据失效”的回归测试。
- 将 `target_image_anchor.py` 纳入 `core_algorithm` parity。检测框到地面接触点的语义锚点现在与
  shared ranging/WGS-84 一起参与四模式一致性身份。
- 删除最新提交中无 `model.config`、无 `model.sdf`、无引用的 v2 孤立重复纹理。该文件可从 Git
  提交 `b5c3f10` 恢复；删除后 Gazebo 正式 model-root 回到 11 个已绑定文件。
- 重新生成 `configs/release-parity.json`，并从六份不可变原始外部 runtime/AI 记录重建当前 parity
  的三机型 join 与独立 revalidation。这里没有冒充一次新的外部 Gazebo 或硬件运行。
- 重跑正向和近地平线负向定位证据，两份 verifier 都从当前输入完整复现。
- 重跑 E7 report-only：仍有 32 条真实 blocker，版本和物理能力没有被 software/SIM 结果提升。
- 全量 Python 回归：3,233 tests，`3232 passed, 1 skipped, 0 failed, 0 errors`；唯一跳过为当前
  Windows 账户缺少 symlink 能力。JUnit 为 469,702 bytes，SHA-256
  `2FD79F5349A56DBDB72EB6AD1A77653707310D141F99CF996A36FFA3A23A9B6F`；全仓库
  `python -m ruff check .` 通过。
- 全仓库 `python -m ruff format --check .` 仍报告 410 个历史未格式化文件：`ground-control` 205、
  `src` 95、`tests` 86、`scripts` 24，其中 31 个属于 parity 绑定的 Python 源。本轮没有把格式迁移
  混入功能/证据修复，因为这会制造大面积无关 diff 并使当前绑定证据失效；现有 manual validation
  workflow 的 format step 在独立基线迁移完成前仍会失败。
- 将原 10,602 行 README 原样迁入 `docs/history/`；新的 README 只承担稳定入口和快速上手。
- 新增本进度页、机器可读状态和仓库更新规范，保证以后每轮结果进入 Git 文档而不是只留在聊天里。

## 当前验证证据

下表中的 `artifacts/` 文件是本机验收制品，默认受 `.gitignore` 排除；表内同时保存文件摘要和
内部确定性摘要。源代码仓库可以验证契约与脚本，但新的 clone 若没有配套模型/帧集，不能重放这些
本地制品。

| 证据 | 结果 | 关键指标 | 文件 SHA-256 |
|---|---|---|---|
| `gazebo-car-geolocation-positive-control-evaluation-v58-pre8-v4.json` | `gate_passed=true` | schema v2；5/5 检测与落图；最大/均值误差 `0.674040/0.651476 m`；AGL 均值 `6.195847 m`；range `8.000000 m`；坡度 `0 deg` | `FA8E526CC26683CF7638A33C82D5AECC379A5B55975BAA71DBECE55B959ED35A` |
| `gazebo-car-geolocation-positive-control-verification-v58-pre8-v4.json` | `passed=true` / `reproduced=true` | evaluator/source/parity 全部重开复算；无原因项 | `DC9DA51413113E5A935C55AFDED742834FD192BDFFBE92F846ED71A3525231C4` |
| `gazebo-car-near-horizon-negative-evaluation-v58-pre8-v2.json` | `gate_passed=false`（预期） | schema v2；5/5 检测；0/5 坐标；理由为 `target_ray_does_not_intersect_ground_safely` | `AA7A7BB6AAF8469D5FC0D25E9C28412C331D6F96A8BFD26243DDDAE767455895` |
| `gazebo-car-near-horizon-negative-verification-v58-pre8-v2.json` | `passed=true` / `reproduced=true` | 允许失败门后完整复现 fail-closed 结果 | `BE75F0ACA71C2B970EAF355BE9AD53AC09D1468CD5AE99540BA54406EE207F75` |
| `gazebo-mid-slope-geolocation-evaluation-v59-pre8-v1.json` | `gate_passed=true` | 5/5 落图；最大/均值误差 `1.175656/1.159927 m`；AGL `12.168137 m`；range `15 m`；坡度 `4.004173 deg` | `28A25CCAE2E8AAA1CB9E8028C156A6DEE0898E4C7F1948C08A89F48682487987` |
| `gazebo-mid-slope-geolocation-verification-v59-pre8-v1.json` | `passed=true` / `reproduced=true` | 完整 source 重放；无原因项 | `9969120F597129144612D28D31BBB1AB550AF6EEB16C27F400A8BFE411A6D95F` |
| `gazebo-high-slope-geolocation-evaluation-v59-pre8-v1.json` | `gate_passed=true` | 5/5 落图；最大/均值误差 `1.855370/1.841585 m`；AGL `20.149371 m`；range `25 m`；坡度 `7.969610 deg` | `CC3B670248BEE4F3224927AEDAE0D5A13D5301E5106DC2BA66C4C1FEF90025F6` |
| `gazebo-high-slope-geolocation-verification-v59-pre8-v1.json` | `passed=true` / `reproduced=true` | 完整 source 重放；无原因项 | `857A5AA71D938A5B247C212474C53BB32291A2D28308A01E8D920207935CE1A2` |
| `gazebo-geolocation-regime-matrix-v59-pre8-v1.json` | `gate_passed=true` | 正场景/帧 `3/3`、`15/15`；高度/距离/坡度档 `3/3`、`3/3`、`3/2`；负场景/帧 `1/1`、`5/5` | `A6D33E43B26B805939B6A14BB76C5C13CD8B9AC7EBD41A727BAFA8AB76022F35` |
| `gazebo-high-slope-geolocation-evaluation-v61-pre8-v2.json` | `gate_passed=true` | 当前五组 parity；5/5 落图；供 v61 HIL 持续刷新 | `548905E36A076240F3C4DEC6DC09540831456E175AC4A372434827D1A5F7BBE4` |
| `px4-sitl-qgc-geolocation-native-visual-v61-pre8-v8.json` | `gate_passed=true` | 认证 132；type 10/22 各 47；刷新 60；PX4 heartbeat 14；exact PID/HWND capture；写入/物理输出 0 | `BF9E9856F91244161CDB38E8FE5DDA9BC6972A0BFC17E989A427BDAA296A2861` |
| `native-visual-v8/qgc-native-style.png` | machine capture passed；人工签字未声明 | exact PID 4900；client `1585×862`；单一原生 surface；采集后外部遮挡 0 | `86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3` |
| `px4-sitl-qgc-geolocation-operator-acceptance-v60-pre8-v2.json` | `gate_passed=true` | QGC 认证 40 包；type 10/22 各 1；PX4 heartbeat 6；写入/物理输出 0；14550 不变 | `9E40159EEC767CD8F1AEF106BCC7E48ED9C00BC0DDFC7C1E4CECC7D2A060D053` |
| `simulator-external-runtime-ai-join-v57-current-parity-pre8.json` | `gate_passed=true` | 三机型六份历史不可变 source 在当前 parity 下重算 | `BAF6FC33DB0E39C758420B2DE3573EAB9A2751721476D25CACF5EBC2D2199D3B` |
| `simulator-external-runtime-ai-revalidation-v57-current-parity-pre8.json` | `gate_passed=true` | `source_recomputed=true` | `3706D8BE96F4AB74BB68679C6C2640848908769D03F01A07BE5D8B4701444CCE` |
| `field-link-preflight-live-20260907-v155-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；三条路由源均为 `192.168.1.13`，不在飞机网段 | `740479B51DA65F0564C5436FEF9CA6C006775375AA5A20642A484A7988AEC8D7` |
| `simulator-external-runtime-ai-join-v155-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v155 parity 重算 | `C4F7EEECD3EB2AD1FB5CA693DF09826876E480B2AB4A069DCDB15BCC15157E29` |
| `px4-reported-position-response-matrix-v155-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `7693C40A84D39339E73CA28F49E0596C13E199A5EF01115534654F9AC900AEC9` |
| `px4-reported-position-response-campaign-v155-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `112EF4627A2CB1F66953EB30BD3D85DD4EB52AD0E816AAF8A6B074BF82E998E2` |
| `release-gate-pre.8-current-20260906T192400Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers | `996F595DE8E07EB41F63A1EDB887637DC8ABB0B5ACD6A5197D597C549E93CCEF` |
| `field-link-preflight-live-v156-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；路由源仍为 `192.168.1.13` | `FE782E46396A28E8E15DCF6983B2C1C36EEA4C28F91F393809BBA88FEDE87B57` |
| `simulator-external-runtime-ai-join-v156-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v156 parity 重算 | `6DA1C013A78616B3FCE433145E8F1A86B785F8E7AF8F6443430DF05A0A2E0743` |
| `px4-reported-position-response-matrix-v156-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `9A4AA992E64735E0401B5768D4E6759C24001AD8A017C77A66CCA85DB2518610` |
| `px4-reported-position-response-campaign-v156-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `FE96FC6051570BEF85270D538A650CD9B61EBB2B72515CBE1563EAC7CFD5CAE9` |
| `release-gate-pre.8-current-20260906T201700Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers；当前 v156 parity | `2E69421F6446D6064B692AD0580B43C5D9CF3BCEA3AF8944C918CB2CA1D0C543` |
| `field-link-preflight-live-v157-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；路由源仍为 `192.168.1.13` | `49C25699A416A266C83362798049F9C8FEAF3D9A0C9BF57A8EE364D962DC053B` |
| `simulator-external-runtime-ai-join-v157-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v157 parity 重算 | `E293377814A455F03841110D7F51349775CEFDFACFACF83FF30A0D5BA76EDC7B` |
| `px4-reported-position-response-matrix-v157-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `0670435A201876DD75A0DCF51E3CF0C29E8BFD65091F929B614C6EA02E8F7CCE` |
| `px4-reported-position-response-campaign-v157-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `334C7C4131740C8869F97459F43F6BC93FE2C8C03B636A092078A8ED584E84F2` |
| `release-gate-pre.8-current-20260906T202947Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers；当前 v157 parity | `AB4E9ADEC704735281F6ACA7CDB99824AA586F0ABA98C77020984EBFC033E3E3` |
| `field-link-preflight-live-v158-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；路由源仍为 `192.168.1.13` | `E23786D4725F2B1923D69FA5A138B19E18AD495A776ADD1D2AA1C8D905E12B3A` |
| `simulator-external-runtime-ai-join-v158-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v158 parity 重算 | `6882A353E480CA0CAA9982044C8FBAC4404489AA149BCE853F87EA681A43E652` |
| `px4-reported-position-response-matrix-v158-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `AC9CF24B004F6F8EE9517B49EEAF3EB4CD3D2BA8864FDA729705850FD5806EE7` |
| `px4-reported-position-response-campaign-v158-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `AD0B3D53B80FCED8683453A249DEB746C9E4E6EC6D5062EFE0890D2757732E76` |
| `release-gate-pre.8-current-20260906T205843Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers；当前 v158 parity | `55644F69519C1B17C55F1AC446C11EDBA1F63D86C0D8871802E57B2B931F7E4E` |

正向 evidence 内部摘要为：

- `evidence_sha256=9f626d06842748fda7f90a84c3fa4f7442e4687d9f18c2980c61246875467d00`
- `deterministic_result_sha256=2f30b9a869b12e63b7d8de31d741c3a83e8274938c72c5dc8a74705ae1262d4d`
- verifier `verification_sha256=91ce81015f5a51f6fb32d198bfb0b8f75f07c3215a6256900ee980c84d9a12d1`

负向 evidence 内部摘要为：

- `evidence_sha256=71bf863749c40270838a795753407e554e4e6fb0cfd9056bfa40f38206e8e0b9`
- `deterministic_result_sha256=7c1694f06f029fd60c5c2a506f45fe0def509db483c5fe5cea57daee7fc200f5`
- verifier `verification_sha256=56ac67565d1490080b7ab1e9907e4c255e23fc0b571a29d08c2a7cd77686a390`

v59 矩阵内部摘要为：

- `evidence_sha256=d542d951c22e96857d1d3acb2013d35ab8fb5a03f3096b101736d1cfc6cb2f5c`
- `deterministic_result_sha256=e892f16550b7349efcbdc6360b6151129ff8f7ddb59555f597b5dfb79fc54716`
- matrix evaluator source `ba30f792508890f00d4c1ad4de60fcece2f3bdf809760db47fa4cdf45b8b3a94`

## 当前共享契约身份

`configs/release-parity.json`：8,735 bytes，文件 SHA-256
`289FD7BD12E12B3931D5AEDD3B5C94A34EF5559480F338B6AA139C9D07184CFF`。

| 契约组 | 当前哈希 |
|---|---|
| UI contract | `3ba8008a73ed19bd9c5b774a322989a841263b947a395c977a660c55176bebd7` |
| Core algorithm | `1c9f173385cd21975096ed67991228d04ecb10c1277d4634420d3a4da6865e3e` |
| Task contract | `2cdb5814c09dfad6609b60c71ae9affe1ad5ffcee63f5e4964590799f34c254b` |
| Safety policy | `a142109e93584194cf9a276cd6c430105d03baba3c3114cec44a19a532955fcc` |
| PX4 Mission path | `df1cab8dd37c203cd8a86c6cee714cdc8650c3cd626376b2584d322ef2b6f4bf` |

## 功能成熟度矩阵

| 子系统 | 已实现/已自动验证 | 当前有效边界 | 仍需完成 |
|---|---|---|---|
| 输入与运行模式 | 本地/RTSP/Replay/Gazebo；REPLAY/SIM/HIL/REAL profile 与五组 parity | 软件契约和模拟输入 | 四模式同屏人工验收与真实 profile 证据 |
| 感知与模型治理 | ONNX provider、manifest、raw/post-NMS、候选隔离、延迟/精度工具 | v56 COCO car 仅 quarantined SIM candidate | 部署域 fire/smoke/safety 模型、数据集、评审和阈值 |
| 多目标跟踪 | 多目标池、短时恢复、ReID、遮挡/丢失/重获、TRK/LCK/TGT | 自动化与软件/SITL | 真实复杂场景身份精度与 G20/QGC 触控验收 |
| 测距 | metric depth、相机地面射线、多源融合、DEM/GNSS、时间/运动/残差不确定性 | v59 实际 Gazebo 像素；三档坡高/距离；advisory-only | 真实外参与 outdoor survey |
| 目标落图 | WGS-84、1σ 圆、GPS/RANGE fail-closed、v6 GNSS-bound 本地证据与 epoch 诊断 JSON | v1-v5 只读兼容；v6 保存原子观测健康审计；三正场景 15/15 落图、近地平线 0/5 假点 | 人工视觉签字与真实 GNSS |
| MultiDetectGCS | 原生 Fly View 覆盖、状态、目标地图、operator protocol、portable build | v160 portable/simple boot/五项 native self-test 通过；原生 epoch 弹窗与原子 JSON 保存 | 四模式人工视觉对等与 E7 绑定 |
| 数字孪生 | PX4/Gazebo、三机型、场景/地图/DEM、wire v3 covariance、故障、watchdog | 三机型外部 wire-v3 实采；x500 position response；三档定位 | estimator 非零 covariance；fixed-wing/VTOL 对等动态响应 |
| 任务与控制 | 平台无关 planner、PX4 Mission 编译/反馈、Mode 3 动态权限 | 测试/SIM/HIL 框架；物理输出默认锁定 | 六份 HIL/REAL×三机型 feedback 与真实飞控验收 |
| Jetson/Pixhawk/链路 | Jetson profile、RTSP bench、只读 MAVLink、V6X 参数备份、G20/GR01 认证协议 | 软件/localhost 与部分只读现场快照 | 实机 Jetson provider、相机、GR01、V6X 同时在线验收 |
| 载荷安全 | 独立授权、双反馈、惰性 HIL、超时/重放/状态不一致 fail-closed | 无物理输出的 HIL | 经评审真实 controller transport 与受控地面台架 |
| 发布门 | E7 schema、证据绑定、recompute、严格/报告模式 | 当前 31 blocker，发布拒绝 | 全部现场/模型/视觉/Mission/HIL/REAL 门关闭后才发 v0.3.0 |

## 本机可运行 QGC

当前 portable 主程序：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

- 大小：`46,875,136` bytes
- SHA-256：`41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`
- 当前 portable 已包含 v175 的全量 replay 摘要与一键复制、v174 质量统计、v172 媒体/车辆明细、
  producer epoch 诊断、evidence v6 与全部此前 QGC C++/QML 变更；native self-test 和 offscreen simple boot 均通过。
- 该文件位于被忽略的本机 `build/`，不是 GitHub 下载资产；运行时必须保留完整 portable 目录。

## 当前 E7 阻断分类

当前 31 条 blocker 可归为：

1. replay-set 没有完整部署域覆盖、精度批准和人工评审；
2. REAL field-link 报告新鲜但 `hardware_link_ready=false`：当前接口不在飞机网段，Jetson/GR01/相机均超时；
3. REPLAY、SIM、HIL、REAL 的人工 QGC visual 和 Mission path acceptance 未全部通过；
4. HIL/REAL 缺真实 hardware acceptance 与 real-control acceptance；
5. PX4 Mission feedback 六叶矩阵未完成；
6. 部分被引用 artifact 自身仍为失败门；
7. 产品仍是 `prerelease`，版本面不能伪装为最终 `0.3.0`。

## 下一步优先级

### 仓库维护：Ruff format 基线已迁移

v130 已用独立机械提交完成 1,523 个 Python 文件的 format baseline，并重建 parity、三机型派生证据和 E7。
剩余验证仅是 GitHub-hosted Python 3.11/3.12/3.13 manual workflow 实际运行，不再存在本地 format backlog。

### P0：原生 QGC 目标落图与证据卡片人工签字

v61 已将 high-slope evidence 的坐标、1σ、类别、距离和方位经签名协议持续送入原生
MultiDetectGCS，并完成 exact PID/HWND 动态地图/证据卡片机器采集。下一步由人工检查同一证据画面并
使用 v64 checklist 生成 accepted record；独立 verifier 通过后再经 manifest 接线评审接入 E7。当前
负向 source-binding 预检保持 5/5 检测、0 假点。v67 combined-SIM source 已能被通用 adapter 识别为
真实 SIM runtime，但它的 toolbar/Mission 截图没有配置 target-geolocation acceptance，不能直接充当
包含地图点/1σ/证据卡片的人工记录；SIM 模式仍需专门运行该定位画面。

### P0.5：把 typed RuntimeObservationResult 接入真实 HIL/REAL producer

v187 已把 result/sidecar 的生产字段收拢到不可变 builder，并验证正向 pair 与 input-hash 篡改负向。
下一步接入 `capture_pixhawk_odometry_covariance.py` 的现场适配层时，必须保留 telemetry request 计数，
另设 application/flight-control message counter；只有后者为零且 RuntimeProfile、Mission feedback、
endpoint/vehicle identity 同版绑定，才能生成 v186 receive-only 结果。不能把 telemetry 请求数抹成零，
也不能用软件 fixture 关闭硬件/HIL/REAL 门。

### P1：三机型动态对等（机器子门已完成）

fixed-wing、multirotor、VTOL 已完成同一持续目标刷新、authority/watchdog、MANUAL_CONTROL 与 PX4
reported-position 路径；v63 又完成每机型双重复与三故障 fail-closed。剩余工作属于人工、硬件和实飞门，
不能用 SIM campaign 替代。

### P2：部署域模型和 replay-set

使用 v107 intake 显式导入 day/night/haze/reflection/smoke-only/no-fire 真实录像，再用 v108 workspace 完成
独立人工标注与 review receipt；用 v109 runner 在该真实 reviewed bundle 上运行固定 fire/smoke 模型，并继续
运行 person/vehicle 安全模型，测量 per-class precision/recall/false alarms/misses/latency，完成独立评审后再改变
model status。当前只完成 runner 与 synthetic HIL runtime smoke，没有 field receipt 或部署域 accuracy 结论。

### P3：真实硬件与室外定位

在 Jetson Orin Nano、实际 RTSP 相机、Pixhawk V6X 和 GR01 同时在线时采集 freshness/readiness；使用
实测相机内外参、lever arm、boresight sigma、真实 GNSS/RTK 和多距离/多坡度 survey truth 完成精度门。

### P4：HIL/REAL 与最终发布

补齐 HIL/REAL × fixed-wing/multirotor/VTOL 六份签名 profile/Mission feedback，完成四模式同 UI、
同核心、同契约、同安全、同 Mission 的人工验收。E7 strict 只有在 blocker 为 0 且版本通道切到
`release` 后才允许发布 `v0.3.0`。

## 2026-10-04 OnboardAI r3 任务链软件回放

- [x] 新增 `onboard_navigation.py`：模型的 N/E 偏移先用 WGS-84 投影，再经过现有 planner、
  multirotor PX4 compiler 和 geofence/高度/能量门；adapter 强制要求 compilation、plan hash、
  wire hash 与新 Mission 一致，旧 Mission 不能通过改 ID 冒充模型航迹。
- [x] 新增 `onboard_task_state.py`：adapter 记录 `search → match_target → record_location →
  return_home → report` 阶段。`record_location` 只有在 GPS fix、水平精度、目标匹配分数、
  目标方位和距离均合格时才生成 WGS-84 目标坐标；返航必须再收到独立的返航完成事件，
  不允许把“记录位置并请求返航”伪装成已返航。
- [x] 使用 4060 训练的 `onboard-policy-r3.onnx` 完成确定性软件回放，证据为
  [`docs/evidence/onboard-policy-chain-20261004.json`](evidence/onboard-policy-chain-20261004.json)，
  模型 SHA-256=`617b666fd6daf538421ba8ce4af4aa1332bd882e0aec6c4d8ba8115fee16786d`，两次移动
  Mission 均 `planner_revalidated=true`，任务阶段顺序为
  `search → match_target → record_location → return_home → report`，并记录 3 次安全请求。
  证据还保留每次模型的 N/E/高度/速度意图；N/E/高度已进入 waypoint 重编译，但速度尚未
  写入 PX4 Mission wire item，因此当前不宣称速度控制已在飞行器上执行。
- [ ] 该模型仍是 synthetic bootstrap，输入是感知模块提供的结构化 `target_match_score`，
  不是像素级红/绿识别；Jetson TensorRT、真实数据独立评估、外部 PX4/Gazebo 同模型事件重规划、
  HIL/REAL、室外 GNSS 与最终 `0.3.0` 发布门均未完成。

## 状态解释规则

- “有代码”只表示 implemented。
- “测试通过”只表示 automated verification。
- “SIM 通过”只表示相应 software/SIM 子门。
- “HIL 通过”必须有真实或明确界定的 HIL 设备/传输证据。
- “REAL/生产批准/实飞通过”必须由独立现场证据和 E7 严格门支持。
- 未写明的能力一律不从更低层证据推断。
# 2026-09-28 progress note

- Energy replay rejection now reports bounded receive/heartbeat/throttle freshness diagnostics while remaining fail-closed. Thresholds and release gates are unchanged; this only makes an external SIM failure actionable.
## 2026-09-30 继续推进

- vehicle-neutral Mission adapter、fixed-wing/multirotor campaign wrapper、shared-policy campaign 与当前进度索引定向回归均通过；shared policy 仍只负责任务选择和 planner revalidation，不拥有 actuator 权限。
- Docker 只读预检可用，但宿主当前可用内存约 1.1 GiB，已有 13 个长期运行容器；本轮不启动新的长时间 PX4/Gazebo campaign，避免生成资源级失败而污染运行证据。
## 当前基础验收（2026-10-03，v0.3.0-pre.1 foundation 在 pre.8 当前源码上的重验）

- [x] 新增 `scripts/verify_v030_pre1_foundation.py`，按目标逐项重验 RuntimeMode 四值合同、
  共享 `ManualClock`、Observation `RuntimeBackend`、hash-bound Scenario Manifest runtime channel、
  truth-free 核心任务契约、profile/mode 隔离，以及确定性 Replay 和 Simulator。两次独立生成的
  canonical receipt 字节完全一致；当前证据为
  `docs/evidence/v030-pre1-foundation-20261003-r5.json`（6,442 bytes，文件 SHA-256=
  `83776ef29c75f9fb72c1f92e808f2e635a464462df78ad20bbef529276f317d7`，canonical SHA-256=
  `3c83ec6b91d9f4cf72bdf671cebb069b085c1f3683694d97812384339d193865`）。
- [x] receipt 绑定当前 `0.3.0-pre.8` parity/source，`gate_passed=true`、`truth_free=true`、
  `policy_applied_to_actuator=false`、`flight_control_enabled=false`；回归为
  `tests/test_v030_pre1_foundation.py`，不触碰飞控、Mission transport 或硬件。
- [ ] 这只是 pre.1 基础合同在当前源码上的软件重验，不关闭 QGC 人工视觉、HIL、REAL、真实硬件、
  室外 GNSS 或最终 `0.3.0` 发布门；`candidate_promoted=false`、`final_release_allowed=false` 保持。

## 2026-10-04 Prompt → OnboardAI → Gazebo/PX4 闭环

- [x] `scripts/vtol_selected_task_campaign.py` 现在把带注册目录的 `PromptEnvelope` 接入
  `OnboardAIExecutor.prepare_from_prompt()`；OnboardAI 和 campaign 共用显式 Mission ID，随后仍
  必须经过原有 planner、Mission upload/readback/commit/permit、journal 和 cleanup 路径。
- [x] `scripts/vtol_sitl_mission_acceptance.py` 将真实 airframe profile hash 放进 live
  `ContextSnapshot`；独立 capture 审计会重建 Prompt resolution，并核对 resolver、resolution、
  selected task、plan、executable Mission 和 wire hash，未知注册 Prompt/aircraft fail-closed。
- [x] 新增双架次集成测试：`tests/test_vtol_sitl_mission_acceptance.py` 全部 301 项通过；Ruff
  与 `git diff --check` 通过。
- [x] 新鲜无传感器外部运行 r5：
  `artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-20261004-current-r5.json`
 （32,900 bytes，SHA-256=`240b05d909d3541ffdab918848625d5fbb6b2029e1e703d3b805c74ad042681c`）。
  两架次 `synthetic-east`/`synthetic-west` 完成，Prompt provenance、OnboardAI resolution、
  planner revalidation、Mission journal/hash-chain、受限路由控制和 exact-ID cleanup 全部通过。
- [x] 新鲜带图像外部运行 r6：
  `artifacts/evaluation/continuation-shared-policy-vtol-prompt-onboard-sensor-20261004-current-r6.json`
 （6,583,748 bytes，SHA-256=`524beb880bb6141131dd07445dcf801362f55dbefcb7c1af5f577833b4cd7d09`），
  同一容器在首架次 fixed-wing barrier 后采集 3 帧 wire-v3 BGR24 Image/CameraInfo/NavSat/
  Odometry，并持久化 3 个 FLOAT32 depth companion；像素/载荷哈希、时间绑定、任务/Mission
  descriptor、cleanup 全部通过。索引为 `docs/evidence/prompt-onboard-vtol-gazebo-20261004-r6.json`。
- [ ] 该 Prompt 目前是“已批准 prompt ID → 静态任务目录”解析，不是任意自然语言任务生成；
  `OnboardAIExecutor.step()` 的实时突发事件重规划尚未接入这条外部 campaign；QGC 人工视觉、
  Jetson/Pixhawk 硬件、HIL、REAL、室外 GNSS 与最终 `0.3.0` 仍未宣称。

## 2026-10-05 红车任务：外部 PX4/Gazebo 实时推理续接

- [x] 真实 Gazebo RGB/Depth 帧现在绑定同一帧的 GPS、roll/pitch/heading、相机标定、帧时间戳和
  frame hash；目标投影使用完整的“相机射线 → 机体 FRD → NED”三维链路。只有完整姿态和有效
  metric depth 才能产生目标 N/E，不能再用“只有航向角”的平面近似制造 GPS 点。
- [x] 目标确认仍由共享适配器负责：连续不同 observation、严格递增时间、同轨迹 IoU 和合格
  detector/color/depth 才能进入 `MATCH_TARGET → RECORD_LOCATION`；低置信度候选只能帮助继续
  搜索，不能落图、返航或报告。PX4 `MISSION_ITEM_INT.current` 的动态执行游标也已与 canonical
  Mission hash 分离，原始游标留在 evidence，协议 hash 保持稳定。
- [x] 修复实时搜索门控：`SEARCH/INSPECT_TARGET` 的移动重规划使用候选门 `0.25`，非搜索重规划
  仍使用 `0.55`，避免“目标暂时丢失时模型想左右搜索”被误当成高风险飞控动作而提前 HOLD。
  新增回归测试证明 `0.30` 的搜索提案可以重新编译并绑定 Mission，但仍不会直接获得 actuator 权限。
- [x] 新鲜外部运行 `artifacts/evaluation/external-red-car-autonomous-r28-structured.json`：
  Docker/PX4/Gazebo、qualified GPS、ARM、起飞高度门、20 个传感器帧、19 个有效 RGB/Depth 处理、
  1 个起飞前缓存帧拒绝，以及 20 次 Mission upload/readback/commit/permit 事务均通过；容器清理和
  journal 也通过。实际 YOLO 在约 22.07–22.67 m 处识别到 `car` 并由共享颜色适配器判定为 `red`，
  但原始 detector confidence 仅约 `0.01–0.02`，低于可执行的校准门 `0.25`，所以状态保持
  `SEARCH`，没有伪造目标 GPS、RTL 或 REPORT。这是预期的 fail-closed 结果，不是完整任务通过。
- [ ] 当前真正的性能阻塞已经明确为“小目标/远距离 detector calibration”，不是 PX4 Mission、GPS、
  Depth 或颜色链路。下一步在 4060 上用同一 YOLO 架构对保存的 Gazebo/航拍帧做增量训练和独立
  holdout；在达到多帧、误报、距离和部署域门槛前，不降低 `0.25`，也不 promotion 模型。

这轮仍只证明自动化测试和外部软件 SIM 子门；没有关闭 Jetson Orin/Pixhawk 硬件、HIL、REAL、室外
GNSS、人工 QGC 视觉或最终 `0.3.0` 发布门。

## 2026-10-05 红车任务：v2 detector 与完整外部 sortie

- [x] v1 训练候选先经过原始帧复测，发现数据生成缺陷：原目标留在背景中却没有标签，导致 v1
  在保存的近距离 Gazebo 原帧上反而不出 `car`。没有降低安全阈值，而是修正生成器：背景样本先
  对源目标做 inpaint，另有 20% 正样本保留原始目标位置并正确标注；仍从原有 YOLO26n 权重增量
  训练，没有另起一套模型。
- [x] v2 数据集审计通过：`600 train / 120 val`，每个 split 10% background，全部 648 个框
  均为 COCO `car` 类；manifest SHA-256=`6528e9d4e63a0e9320a802ec4986a8d84516c3a31cd33385fd9980e39c0e0827`。
  v2 最佳 checkpoint SHA-256=`1512179d87cc621269247643f04c1ac51c815a918e35e5ffe40fee9e26416aee`，
  raw ONNX SHA-256=`1b91ba4b912efd63b047242b4196fb339fca72205bec6b019056bf1e7f1c6c27`；候选仍未 promotion。
- [x] 独立保存的近距离原始帧复测：parent `0.80`、v1 无有效 car、v2 两帧约 `0.966`，且框位置
  与 parent 一致，证明修复的是标签回归而不是简单放宽门限。
- [x] 外部 r29 使用同一 PX4/Gazebo Docker、同一 OnboardAI policy 和同一任务链，替换 detector 为
  v2：20 个真实 RGB 传感器帧（1 个起飞前缓存帧拒绝），6 个有效目标观测，`car` 置信度约
  `0.891–0.912`，距离约 `22.23–22.39 m`，颜色 `red`、match score `1.0`；连续确认后记录 GPS。
  6 次 Mission 事务、起飞、AUTO.MISSION、RTL、返航到家、REPORT 均通过。完整摘要见
  [`docs/evidence/external-red-car-autonomous-20261005-r29-v2.json`](evidence/external-red-car-autonomous-20261005-r29-v2.json)，
  原始 evidence SHA-256=`eed5a6c5f32a1850e0fb9eb4d23948292af7950b30f67610d8a0e2dadcf8b162`。
- [ ] 这证明的是 truth-free 外部软件 SIM 的完整任务闭环，不是 Jetson/Pixhawk 实机、HIL、室外 GNSS、
  实际飞行性能或最终 `0.3.0`。v2 仍是 quarantined candidate，必须继续做独立场景、误报、距离
  误差、部署后端和硬件验收，`candidate_promoted=false`、`final_release_allowed=false` 保持。
