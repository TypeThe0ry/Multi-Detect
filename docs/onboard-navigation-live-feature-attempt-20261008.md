# OnboardAI：现场特征采集失败，继续补齐真实行动反馈

目标仍是一次任务描述后，AI看当前画面、结合任务与记忆决定下一步，执行后再观察，
必要时改变搜索方向、避障或返航。Gazebo和Jetson应运行同一套判断与行动接口。
这一轮不是新的训练成功，也不是自主飞行通过。

后续[实际飞控诊断](onboard-teacher-px4-diagnostics-20261008.md)已保存版本和17参数，
此前原事件CRC解码另闭合；容器日志超限仍失败，不掩盖现场feature加载失败或新训练
缺口。当前模拟选择MAVLink手动输入、容量未知，需明确自主输入和有来源预算后再采行动。

最新独立owned Gazebo R3并行观察已保存6RGB／6Depth和5943遥测，0orphan／0overflow，
最后9类消息新鲜，但飞控并非STANDBY，仍拒绝；108新／联合366项CPU测试与实际运行
分开。中位延迟改善但P95升至223.39ms，未称实时通过。R1失落包类型不定，R2／R3
电池time_remaining=0及VFR空速NaN都保留原值，不回写旧回合或冒充飞行通过。见
[真实教师观察阶段](onboard-teacher-gazebo-observation-20261008.md)。没有新模型加载、
训练或教师飞行。

## 最新：单架次停留／转向与原始遥测入口

三路子智能体继续并行开发、独立复核和真实环境接线调查。新增
[教师Mission编译](../src/multidetect/autonomy/onboard_teacher_mission.py)让一次起飞的多个
航段分别声明停留时间和yaw；只改变WAYPOINT的hold／yaw字段，保留原共享planner与
多旋翼传输路径，补计移动、升降、转向和停留预算。新82项CPU测试、含邻接回归154项
通过，独立82项通过。它是可重新编译的命令草案，不是已飞过的轨迹、模型动作或自动批准。

新增[原始遥测接收入口](../scripts/onboard_teacher_telemetry_capture.py)只允许一个接收者，
先按来源隔离并持久化原报文和解码字段，再更新自己的合法来源缓存／交给消费者。外来
来源仍按原顺序落盘，但不污染这份缓存。完整四元数、boot时间和CURRENT／REACHED
到达顺序不被改写；收到reached仍不能当作停稳。22项CPU测试作者和独立复跑通过，
与held／interval／planner／multirotor联合248项通过。实际SIM venv的pymavlink小CPU
编码→解码→采集核查7个构造报文，5个合法返回、2个外来只记录，逐字节保持；不是网络
遥测或Gazebo飞行数据。慢fsync的接收和可分发时间分别记录，上层仍必须检验freshness。

[本轮软件回执](evidence/onboard-teacher-authoring-and-raw-telemetry-software-20261008-r1.json)
封存四份新来源、准确测试范围与剩余门。真实接线调查发现，旧完整VTOL验收含force-arm
和VTOL专有门，通用acceptance工厂会自造test-only PASS／grant；新x500教师入口不能
整套调用它们。必须使用真实检查、明确自有软件SIM意图和正常解锁，复用上传／回读／
commit／permit／journal。旧collector会先缓存外来消息，所以新增reader必须直连唯一
receive，不能先经过它的未过滤cache，也不能把底层mavutil.messages当同等可信上下文。

尚需实际航线净空、合格当前GPS／电池／home、时钟及EKF原点关系、真实稳定端点、
专家审核和训练准入。原interval对REACHED先于CURRENT会拒绝；实际发生时保留失败
或不完整，不能重排报文来凑通过。没有新增教师飞行、训练更新、授权或默认模型替换。

## 实际发生了什么

已有候选AI此前实际处理了8帧新的Gazebo三维画面，8次视觉语言融合和动作头计算
正常结束，但全部输出STOP。已有208条特征与11个动作头的CPU诊断又表明，动作头
并非恒定只输出STOP；这仍没有解释现场8帧的结果，因为当时没有保存可复算的特征字节。

为查明这个差异，新R3实验尝试在同一次推理中保存真实特征和原始六个输出，保持原
模型、动作头、归一化、输入与质量门不变。**这次实际尝试在精确加载流程完成前发生
原生崩溃**，没有进入Gazebo相机采集，没有新增RGB、特征、融合推理、动作头输出或
飞行命令。失败计划、源码、stdout、stderr与supervisor回执已原样封存。

外层命令退出1；受监督子进程退出`3221225477`（Windows `0xc0000005`），不是600秒
watchdog超时。Windows Application事件1000、记录268134指向同一模型进程的
`c10.dll`。其位置与实验准入的PyTorch来源一致，但没有完成整个传递DLL身份闭合。

观察到主机内存压力；另一个小规模CPU测试可以正常执行BF16复制和2048→6线性层。
两项事实都不能证明完整模型加载的故障原因，也不能当成“已修好”。日志最后一个
warmup恢复事件之后仍可能有reader清理、动作头加载、参数身份与冻结检查，不能仅凭
最后一行断言是`from_pretrained`内部崩溃。下一次需要原生栈与持久阶段记录，不盲目重跑。

[实际失败回执](evidence/onboard-navigation-live-feature-attempt-20261008-r3.json)绑定新计划、
四份源码／测试、supervisor、ownership与日志，并保留系统事件和小CPU检查的准确范围。
这次没有启动新的Gazebo容器；模型与launcher进程已退出，13个其他运行容器保持不变。
旧相机16次、动作头3000次更新与LoRA280次更新不因失败重置。

## 并行补齐了什么

1. **同一次推理的特征记录。** 保存2048维实际CPU特征、原始六输出、对应画面／请求／
   模型身份与后端接受或拒绝结果。记录不再调用一次模型，不生成文字，也不授予飞行权。
   R3入口26项软件测试通过，但其实际加载失败，不能把软件成绩写成采集成功。

2. **不用重跑大模型的现场审计。** 新CPU工具可以在8份完整现场记录存在时复算同一
   动作头，把特征贡献和bias分开比较。19项软件测试在独立进程通过；目前R3没有
   worker-report或现场特征，所以没有实际运行这项审计，更没有得到现场统计结果。

3. **一次Mission中的真实行动区间。** 新记录器保存父Mission唯一提交、航段激活、
   到达和实际稳定窗口，关联前后画面与完整姿态。72项软件测试通过。父Mission通常
   早于后续航段的画面上传，不能伪造一个新的“每航段发送时间”来满足旧逐步合同。

[两项软件回执](evidence/onboard-teacher-interval-and-live-audit-software-20261008-r1.json)明确
区分软件例子与真实采集。尝试把所有137项测试混在一个进程中曾失败：5项被正确的
“审计进程不得已导入Torch”门拒绝，另1项暴露旧记录器比较访问时间导致的误报。
因此不宣称137项联合通过，也不为了测试通过取消生产隔离门。

旧记录器的访问时间误报已在[新v2文件](../scripts/onboard_navigation_feature_capture_v2.py)
修复：只比较大小、修改时间与文件身份，仍读取真实字节并校验SHA。原20项回归保留，
新增6项覆盖访问时间变化允许、真实大小／修改时间／文件身份变化拒绝，以及等长内容
修改恢复mtime仍被SHA拒绝；新26项CPU测试作者及独立进程通过，见
[新v2软件回执](evidence/onboard-navigation-feature-capture-software-20261008-v2.json)。v2合同明确变更，旧R3和v1审计
消费者仍只消费原版本，不能把新修复偷偷套用到历史失败实验。

## 为什么还不能直接开始新训练

我们需要“AI／教师要求怎样移动”和“飞机实际上怎样移动”同时有证据，而不是用
预定航点代替已执行结果。现在真实教师轨迹仍未采集，专家审核与训练准入也未完成。

当前中间航点是fly-through，没有逐段稳定停留和明确yaw动作；航点reached不等于
飞机已经停稳。下一项教师采集要使用新审查、重新绑定的Mission航段接口，一次起飞，
按真实seq记录每段行动与后继画面，不能每个小动作重复整架次起降。

还要闭合原RGB、完整四元数、相机时间／PX4启动时间／主机时间与坐标原点之间的关系，
保留上传、readback、commit、permit、真实mode／arm／in-air和原始遥测来源。
只验证端点稳定不能证明连续路径安全、目标已经达成或执行因果。

## 下一步按什么顺序推进

### 新加载诊断入口与实际资源预检

[新入口](../scripts/diagnose_onboard_navigation_exact_load.py)保留原R3加载配置和冻结loader，
默认只准入；`--preflight-only`只查来源与实际Windows内存，只有`--execute`才加载。
执行路径会持有faulthandler日志文件、逐条fsync保存有界阶段和内存快照；观察器失败也
不能跳过已进入上下文的清理，最终身份或清理失败不能发布成功。44项CPU测试作者与
独立进程通过。它只观察外部阶段，不自证原生退出、PID终止、内部每项digest或watchdog。

实际发出了[新R4计划](../configs/training/onboard-navigation-exact-load-20261008-r4.json)，
随后只做资源预检，native1／passed=false：可用物理内存6,054,506,496B（约5.64GiB），
可用提交额度9,442,697,216B（约8.79GiB），低于计划的8／16GiB。阈值是本次明确的
保守拒绝策略，不是已证明的模型容量要求或R3崩溃原因。0次新模型加载，没有执行目录、
原生栈或新的模型结果；没有关闭其他程序。见[实际预检回执](evidence/onboard-navigation-exact-load-preflight-20261008-r4.json)。

资源变化后可以重做同一计划的只读预检；它不是启动命令：

```powershell
& 'D:/Projects/Multi-Detect/.venv-vlm-navigation/Scripts/python.exe' -I -B -X utf8 'D:/Projects/Multi-Detect/scripts/diagnose_onboard_navigation_exact_load.py' --plan 'D:/Projects/Multi-Detect/configs/training/onboard-navigation-exact-load-20261008-r4.json' --plan-sha256 13acc35469ff35a8f55050fdbb366c1d98526a64be306ca469fa7f9831a5ecd4 --preflight-only
```

### 独立监督器已实现；新实际只读预检仍拒绝

[NEW监督器](../scripts/supervise_onboard_navigation_exact_load.py)的51项作者和独立CPU
测试通过，包含真实Nav虚拟环境启动器／worker、超时、溢出、孙进程和清理失败。
Windows虚拟环境的实际worker PID与Popen启动器不同；新路径先暂停启动器、纳入
自有Job，再恢复并核实际父链／映像／Job成员，最后才允许Python site和冻结入口运行。
两阶段和最后来源门共享600秒期限，不扫描或终止用户进程。当前Windows的native
resume接口已做软件验证，不作所有平台保证；此前事后加入worker的WinError5小CPU
失败另行保留，不能改写成成功或计为大模型加载。

root实际调用新监督器做了一次R4只读预检，outer／worker均native1：physical
5,418,819,584B（约5.05GiB）、commit8,676,368,384B（约8.08GiB），仍低于原8／16GiB
策略门；0load、无execute输出、无faultstack。launcher35800／worker29668确属自有
Job且已退出，capture线程关闭，没有watchdog或overflow。通用outer错误包含“未干净
结束”，这里只是native非零的门，不能据此声称进程残留；实际stdout保存资源拒绝原因。
原回执source-final字段仍为false，因为拒绝跳过成功路径；另一个只读过程重新核来源
与56项inventory一致，未改写原回执。独立原始报告／13项小binding／namedPID审核
native0，不是预检通过。见[软件与实际预检回执](evidence/onboard-navigation-owned-supervisor-preflight-20261008-r1.json)。

仅只读预检的调用如下；output必须换成全新的目录，不能覆盖上述r1结果：

```powershell
python scripts/supervise_onboard_navigation_exact_load.py --plan configs/training/onboard-navigation-exact-load-20261008-r4.json --plan-sha256 13acc35469ff35a8f55050fdbb366c1d98526a64be306ca469fa7f9831a5ecd4 --output-dir D:/Projects/Multi-Detect/artifacts/diagnostics/onboard-navigation-exact-load-20261008-r4-supervisor-r2 --preflight-only
```

资源通过后才考虑新的受监督加载诊断，不取消门或重复盲跑。Controller当前health／typed
plan提交不可达，没有派远程模型任务。后续现场worker和audit仍要显式消费v2记录合同，
旧R3与此次预检结果不改写；真实教师collector／飞行／数据审核和新训练仍未完成。

先用新的加载诊断入口定位原生故障，再实际保存同一次推理的现场特征，做零训练复算。
同时补齐一次起飞后的三维教师行动反馈：横移、升降、转向、不同距离、受阻恢复和不同任务。
经过来源／专家／世界分组审核后，才以相同输入和预算比较原底座、旧warm与新候选，
扩展三维学习动作输出，并用完整未见world验证；不是只继续增加同类训练步数。

公开模型对照已核到SmolVLA、MiniVLA、AeroVLA及WorldVLN等路线，见
[官方来源与迁移边界](onboard-pretrained-navigation-search-refresh-20261008.md)。小型VLA可
作独立对照，大型世界模型可考虑离线教师；没有证明它们能直接部署到本机8GB或Jetson，
也没有下载、运行或训练第二套模型。现有路线可以替换，但须用同条件的质量、资源和
迁移验证决定，不能仅凭公开演示更换正在飞行的模型。

默认策略引用、`0.3.0-pre.8`、release parity与发布门未改变。Jetson、HIL／REAL、室外
GNSS、真实飞行和最终`0.3.0`仍未通过；STOP仍不等于返航、降落、找到目标或任务完成。
