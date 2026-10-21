## [Unreleased]
- 2026-10-21：MissionExecutor 现在支持 VTOL 混合搜索：搜索步骤使用本步骤的固定翼和多旋翼搜索器，接近、检查、返航等步骤保持多旋翼；切换步骤时重置混合状态，转换拒绝会退避且不消耗预算。
- 2026-10-20 晚：按第二轮代码审查修了 6 处：多步骤任务共用的搜索策略每步重置（否则第二个计数步骤秒结束，`edf05b4`）；只有明确的覆盖完成才算完整覆盖（同上）；混合 VTOL 搜索只用于单一找目标任务，带任务执行器时退回多旋翼悬停作业（否则永远不会切回多旋翼接近，`0d7a77e`）；llama-server 的 Job Object 补上 64 位句柄签名、启动失败时释放（`1e049bb`）；计数只有覆盖完成且无待确认目标才不是下限（`3bb8090`）；任务摘要把耗尽步数预算的计数列为未证实完整搜索（`671690d`）。
- 2026-10-20：SIM 任务套件全量回归（15 类）抓到并修复 3 个问题：llama-server 孤儿检查的命令拼接错误让策略每次启动都崩（`c53c390`）；SIM 地面事件时间线评估监督器时没传位置，安全类任务都以“缺返航几何”提前返航（`26417d8`）；带监督器的计数记录无法写日志（`258aa98`）。另记录慢模型无判断的原因（`3a636ed`）。修复后 14/15 类符合预期，数消防栓 2/3（远处单次被身份模型否定）（docs/sim-mission-suite-regression-20261020.md）。
- 2026-10-20：Windows 下 llama-server 绑定 Job Object，父进程退出时自动清理；启动前只清理本仓库 artifacts 模型的孤儿进程，并对其他端口占用者快速报错。
- 2026-10-20：更新操作手册，说明固定翼与 VTOL 作业方式、计数覆盖收尾、定位修正、地面站签名、Orin 上板命令及所有者验收限制。
- 2026-10-20：SITL 复核两项新功能：大型目标单视位置按典型尺寸补到中心（行人等高而窄的物体不动），w0031 消防车误差从 5.8–7.3 m 降到 3.2 / 2.7 m；eVTOL 混合搜索在候选被拒后回到固定翼、从中断处继续割草机航线，w0016 救护车误差 0.94 m，均着陆。
- 2026-10-20：混合 VTOL 回切仅受可判决的新候选与未耗尽的正向 verdict 阻塞，低高度拒绝后退避 3 步且不消耗回切预算；默认 MC 搜索行为不变。尚未 SITL 验证。
- 2026-10-20：混合 VTOL 搜索在候选被拒并完成 MC 搜索驻留后可回切 FW，继续原固定翼割草机航线；可见候选、待回访位置会抑制回切，每个任务最多回切 3 次，默认驻留 3 步，默认 `vtol_search_mode: "mc"` 不变。
- 2026-10-20：单视 ground 定位现在会对已知的大目标按任务典型尺寸沿底部射线推到预期中心；关联锚点不变，锥桶等小目标跳过修正，fused 多视角结果不重复平移（尚未 SITL 验证）。
- 2026-10-20：计数任务现在跑完一整轮覆盖就收尾并回家（`coverage_complete`），不再一律耗尽步数预算、把数量标成下限；同时修了两处“同一锥桶反复接近”（新航迹编号落在已计数位置上时不再当新候选，远处只按位置判断，`f19d95c`、`6ab8e7b`）。SITL 两架次都 3/3、0 误报，约 90 步结束并回家。
- 2026-10-20：查清 SITL 偶发“PX4 收不到心跳”的根因：hatchback / ambulance 模型的材质把贴图写成 Fuel 在线网址，Gazebo 每次加载世界都去下载，网络慢就卡住、PX4 等不到世界而退出。缓存构建现在把这些网址改成本地贴图（`aba95e1`），PX4 等 Gazebo 世界放宽到 180 s，采集失败时保留容器日志。另修 Codex 审查发现的 4 个问题（VTOL 回切按转换约束检查、转换中不重发、被墙钟截断的悬停记为超时、仿真时钟在时间戳缺失时不再停走，`8af6dcf`）。复测：固定翼 w0016 两架次 1.7 / 6.4 m，eVTOL 4+1 / 尾座式 / 混合搜索各一架 3.1 / 4.8 / 1.7 m，全部着陆；计数（余量圈逃逸 + 不确定度去重）两架次 3/3、0 误报、回家。

- 2026-10-20：为能悬停的 VTOL 增加可选 `vtol_search_mode: "fw"` 混合搜索：固定翼割草机搜索发现焦点后回切多旋翼接近/确认，默认 `"mc"` 行为不变（尚未 SITL 验证）。
- 2026-10-20：计数和区域搜索完成一轮覆盖后结束并继续后续计划步骤，默认搜索路线重启行为保持不变。
- 2026-10-20：VTOL SITL acceptance Docker 启动路径统一使用 SIM 传感器参数并移除 Gazebo 磁力计系统。
- 2026-10-20：固定翼跨圈重新识别时继承同一位置的多视角观测，跨多个航迹 ID 继续融合定位，并隔离同框目标。
- 2026-10-20：新增 Jetson Orin 一键 SSH 公钥部署、安装、基准测试和结果回收入口，默认使用 r18 身份模型并开启步间三角化。
- 2026-10-20：eVTOL（4+1 复合翼、尾座式）第一次在有目标的生成世界里跑通机上闭环“找目标 → 确认 → 返航降落”：能悬停的 VTOL 现在全程在多旋翼模式下用多旋翼那套机上 AI 搜索、接近、确认，确认后交给 PX4 返航并垂直降落（之前被当成固定翼：接近时一段 22 m 地飞过头，悬停命令被执行成绕 50 m 大圈，确认后会降落在目标旁）。真实仿真修复后 7 架次（w0031 消防车、w0016 救护车）全部确认正确目标，误差 1.7–6.1 m，6 架次着陆（1 架次仿真冻结）。途中查出并修好：尾座式相机悬停时其实朝地平线上方 20°，档案矩阵与投影函数的约定相反；尾座式地面投影把高度符号用反，位置永远为空；VTOL 起飞后先转固定翼再转回，会爬升 30 m、漂出 80 m。尾座式因相机朝天，只能在 90–120 m 外确认（docs/fixed-wing-vtol-sim-flight.md）。
- 2026-10-19 晚：仿真采集与固定翼确认规则再修三处。① 采集器的原语超时改按 PX4 仿真时钟计（每个原语最多再等 180 s 墙钟）：本机负载高时仿真只跑到实时的 0.22 倍，计数架次里三分之一的 15 m 航段在 20 s 墙钟超时前还没停稳，而悬停抖动与修复前相同，说明不是 EKF 修复的副作用（`8977db1`）。② 机上健康监测进入悬停或恢复时，标签里没有检测数，采集器曾因此崩溃，现在容错（同一提交）。③ 固定翼过顶时的单次确认改为“同一位置所有航迹的真实确认票多于拒绝票才算”（`e6b84c7`）：上一版只要有拒绝就不准单次确认，结果在 w0016 上挡住了对真救护车的正确确认，飞了 11 km 都没确认；新规则下，公交车误确认（1 比 1）仍被拦住，真救护车（2 比 1）能确认。另外，SITL 一律用 `D:\Projects\mdwt\_scratch\sitl.lock` 原子锁串行：机上 llama-server 固定用 9941 端口，两个架次同时启动会互相冲突。
- 2026-10-19：固定翼姿态偏差查到根因并修好（仿真侧）：镜像里 Gazebo 磁力计把 roll 和 pitch 对调了（倾斜 15–40° 时偏 20–40°），加上 PX4 默认按 110 ms / 100 ms 补偿 GNSS 和空速延迟，而仿真里没有延迟。现在仿真改用 PX4 自带的磁力计并把这两个延迟设为 0；同一航线航向误差从 +1.9°（p95 6.3°）降到 -0.1°（p95 1.1°），真机参数不变（`a44ae78`、`1edb294`）。采集器新增 `--keep-px4-ulog`。计数复验暴露两个缺陷并修复（`17807a8`）：① 最近视角下锥桶底部被画面截断时永远确认不了，飞机悬停了 100 步，现在会后退重新取景，悬停超过上限就放弃该焦点；② 搜索策略按过时的指令航向飞航段和裁剪边界，飞机被主机转过头后会飞出搜索区（实测离家 395 m，搜索区半径 150 m），现在实测航向偏差超过 5° 就重新同步。同框修复确认有效（2 架次 0 误报，其中一架 3/3）。远目标固定翼场景（救护车距 home 102 m）第 1 架次第 19 步确认，误差 4.5 m，着陆。修复后的复测排队中（docs/fixed-wing-vtol-sim-flight.md、docs/slow-system-r16-result.md）。
- 2026-10-19：地面站 Ed25519 任务签名接通：用微软 vcpkg 从源码构建了 MSVC 版 OpenSSL 3.6.5（`D:\Projects\mdwt\vcpkg`），构建脚本新增 `-OpenSSLRoot` / `MULTIDETECT_OPENSSL_ROOT`，并把 FindOpenSSL 的头文件和库固定到这个目录（之前缓存的是 MSYS2 的 MinGW 版本，根本没链上）；签名自测连同与 Python 实现一致的已知签名一起通过，`libcrypto-3-x64.dll` 随便携包分发。pre.9 便携包 `build/windows-v0.3.0-pre.9-release` 已用它重建。另查清固定翼定位偏差的来源：检测框是对的，是 PX4 EKF2 姿态在长时间大坡度盘旋中漂移（航向约 +3.7°、俯仰低 1.5–3.9°），真值投影和机上地面点都用了这份姿态；仿真侧根因（疑为生成世界没设磁场）排查中。
- 2026-10-19：固定翼接近与返航（待 SITL 复核）：查清“飞过头后丢目标”是每步 ≤27 m 的重定位到点后 PX4 绕点盘旋、前视相机被甩开，“确认后绕 home 30 多步”是返航转向步同样变成绕圈、永远进不了 3 m 到达半径。现在候选目标改飞边经过目标的顺时针过顶盘旋（rc_cessna 30 m：R≈90 m，每圈正对目标约 5.7 s），焦点跨盲圈保留；确认后直接交给 RTL 返航降落（`9b62c10`，docs/fixed-wing-vtol-sim-flight.md）。多旋翼行为不变。
- 2026-10-19：固定翼第一次在有目标的场景里跑通机上闭环：rc_cessna 跑道起降，在派生成固定翼版本的生成世界（找红色消防车）里 3 架次都确认了正确目标、都真实着陆，位置误差 5.7 / 8.2 / 11.1 m（2/3 在 10 m 判定内）。修了两个真问题：所有机型都按多旋翼的相机下俯角投影（固定翼相机是平视的），以及每步按当前高度下发航点导致高度一路掉到 18 m（docs/fixed-wing-vtol-sim-flight.md）。
- 2026-10-19：计数 / 搜索再修两处：① 尺寸感知搜索会降到约 8 m 看小物体，在计数场地低于 7.2 m 的房子，前向航段被判撞障碍、搜索提前结束——现在搜索高度不低于最高已知障碍 + 2 m（`39faa59`）；② 同一个锥桶被数两次：两条航迹开局同框（相距 6 m），其中一条后来被跟踪器挪到另一个锥桶上，“同框过就永远是两个物体”的豁免让它又被计了一次——现在同框证据只在两条航迹此后都没挪开（且当时两框不嵌套）时才算数，计数与告警去重都改了（`513e856`）；46 个计数/监视录像回放：4 处多计变成去重，没有变少计的。另查清测试世界 w0039 每次“误确认”的其实是场地里另一个行人（任务“找到行人”同样描述她），判分器一直记为 task_equivalent，不是身份判断错（文档已更正）。
- 2026-10-19：0.3.0-pre.9 发布流水线第一次真正跑通到发布门（运行目录 `artifacts/release-pre9-runs/20261019-final`）：版本号、parity、静态检查、仿真安全门、Docker 仿真验收、Windows 地面站构建与签名自测全部通过，发布门报告如实为 `release_allowed=false`。途中修了流水线自身的 7 处问题（`&&` 只拆第一段、静态检查放在证据重绑之前、地面站构建排在需要它的联合验收之后、Docker 验收不认识模型缓存挂载、回放夹具没人生成、签名自测在 MSVC 环境外编译、MSVC 构建误用 MSYS2 的 MinGW OpenSSL），以及两处早就存在的验收规则问题（探针场景重复 ID、世界文件引用仓库自带模型被拒）。签名自测第一次真正跑起来就抓到一个真实缺陷：**Windows 地面站因为设置存在注册表里，地面指令序号的锁文件目录不存在，序号永远取不到**（`8fe6329`）。仍需所有者：联合 PX4/Gazebo/QGC 验收要在不锁屏、没人操作的桌面上重跑（本机两次都拿不到前台窗口）；这台机器没有 MSVC 版 OpenSSL，本次地面站构建不带 Ed25519 签名（真机签名上传会按设计拒绝），需要安装 MSVC 版 OpenSSL 后重建；证据索引重绑到 pre.9 进行中。
- 2026-10-19：机上身份模型换成 r18（r17 的修复数据 + 全量回放 r12 训练集）：真实飞行集身份准确率 92.2%（r12 92.8%）、干扰物拒绝 97.4%（r12 94.9%），当前候选框口径 89.3% / 100%（r12 78.5% / 90.5%）；闭环找目标成功率相同、误确认少一次。计数任务里查出并修复一个搜索问题（`2c56f70`）：搜索航段被障碍挡住后直接结束，已判“值得靠近”但跟丢的目标不再回看——现在结束前回访一次；r18 + 回访数锥桶两次都 3/3。小目标复查开关试过无收益，保持关闭。评测工具在推理服务崩溃时会立即停止而不是卡死或产出错误指标（`4d083ae`）（docs/slow-system-r16-result.md）。
- 2026-10-18：机上默认开启步间三角化（`919fda2`，地面高度 sigma 默认 5 m）。依据：机上地面模型故意设高 2 m 时，4 个测试世界 × 2 架次闭环，位置误差中位 3.8 m → 2.0 m、平均 4.1 → 2.8 m，两组都 8/8 成功、0 误确认；平地 6 个世界复验成功率不变，平均误差多约 0.05 m。复验中一次误确认是把红色加油站招牌认成停车牌（身份问题，与测距无关）。连续视频试采（6 架次、2,875 帧，先修好了视频模式动作期间一帧不录的问题 `001b595`）：平地上早期 RGB-SLAM / 视觉惯性平均 3.8–4.0 m，不如地面相交的 2.3 m，只有 10–20 m 近距更准（0.3–0.4 m，样本少），暂不接入机上（docs/continuous-video-eval-plan.md）。
- 2026-10-18：步间三角化闭环验收（6 个测试世界 × 2 架次）：10/17 的回退修复后，3–13 m 的大误差全部消失，成功率与不开三角化相当（8/12 vs 9/12，差的一架是没搜到目标）。但查出修复把三角化门得太死——有地面模型时只能偏离地面交点 0.1 m，等于关掉；地面不可信时又把所有门都关了。改为视差门始终生效、允许修正量随地面高度不确定度放大（`4ce3715`）：39 次录像回放，地面正确时不比地面交点差，地面被当成高 5 m 时通过视差门的帧误差 6.6 m → 0.75 m（只有约四分之一的帧通过，整体改善有限）。另修好终验“坡地近似专项”——原来注入的高度偏置没有任何代码读取。地面偏 2 m 的闭环对比已排队；三角化仍默认关闭。
- 2026-10-18：慢模型 r16（r15 数据 + 77 个易混物视角架次，真实候选框样本这次用上了）：当前候选框口径 v3 集身份准确率 95.3%、干扰物拒绝 95.7%（r12 78.5% / 90.5%）；真实飞行集干扰物拒绝 77%（r12 95%），**不替换 r12**。查清 r15/r16 的共同退步全部来自一对类别——任务“找皮卡”、候选是 SUV（204/212 次新增误接受），其余世界 r16 拒绝 96.5% 已超过 r12。根因两个：反事实样本只教“是”、从不配对“任务说 X、看到 Y → 否”；补采世界没继承测试集的留出类别，SUV/皮卡漏进训练。已修复（`d228ee9`），r17 按修复后的数据重训（docs/slow-system-r16-result.md）。另：清掉全仓 ruff 报错（1,258 → 0，pre.9 发布静态检查的前提），顺带删掉一个被合并截断、从未调用的函数。
- 2026-10-18：慢模型 r15 训练完成，结论是**不替换 r12**（docs/slow-system-r15-result.md）。好的一面：当前候选框口径下极小目标（<12 px）真目标召回 46% → 92%，身份准确率 78.5% → 90.1%；问题：真实飞行评测集上干扰物拒绝 95% → 73%（集中在 16–32 px 的中等候选），又一次变得太爱说“是”。原因是补采数据几乎都是“找到目标”的画面，中等大小的易混反例太少。下一步用同一批世界专门补采易混物体视角的反例再训。流水线打通过程中修了 10 处问题（世界划分、跨仓库数据路径、热启动检查、GGUF 转换依赖、评测路径等），以后可直接复用。
- 2026-10-18：固定翼/eVTOL 全机型在真实仿真里回归通过（7 种：advanced_plane 滑跑、rc_cessna 滑跑/手抛/弹射、4+1 标准 VTOL、尾坐式、倾转旋翼；起飞→盘旋→返航→降落全部真实完成，结果表见 docs/fixed-wing-vtol-sim-flight.md）。手抛/弹射用 Gazebo 力脉冲近似（仿真里的手抛更像“推一把+短距滑跑”，如实标注）。机上 AI 现在可以开固定翼：闭环里策略输出的高层指令交给 PX4 执行，固定翼搜索改为按相机覆盖和转弯半径规划的割草机航线（之前沿用多旋翼的 15 m 小步和原地转向，安全包络又按 15 m 把每段截断成原地盘旋，飞机只会绕圈；现在 40 步飞了约 3 km 航线）。移动目标世界修好：原来的 Gazebo actor 根本不会被相机渲染，改为普通模型由容器里按仿真时钟驱动的脚本移动，行人/车辆世界真实验证通过（可见 91%/100%，位置误差中位 4.5/16 px）。r15 采集 76/80 完成，训练流水线已启动。
- 2026-10-17：固定翼/eVTOL 仿真飞行打磨。`advanced_plane` 在真实 PX4/Gazebo SITL 里完整跑通“滑跑起飞 → 盘旋 → 返航 → 在 home 跑道降落”。途中查出并修复四个问题：① 飞机没有测距仪，PX4 默认找不到地形就中止降落、在 30 m 上空盘旋，固定翼机型档案改为关闭地形估计与地形中止（真机同样要这样设）；② 固定翼/VTOL 的重定位、盘旋、起降命令把本地坐标“米”直接当经纬度、把相对高度当海拔发给 PX4——仿真里因世界海拔约为 0 没坠机，但重定位其实飞向几千公里外，真机会按海拔下降。现在统一以 PX4 home 换算成经纬度+海拔，没有 home 地理坐标就拒绝发命令；③ 机上运行时的固定翼模式白名单与实际上报的模式名不一致（盘旋时会被误判为“失去导航模式”），已对齐；④ 降落任务改成标准的“降落起点 → 盘旋降高 → 着陆”，并重试切换任务模式直到 PX4 接受。冒烟判定也改严：必须起飞后着地、RTL 按真实模式或 PX4 文本判断、飞出 1.5 km 判失败。另：r15 采集有 35 个架次失败，原因是合并固定翼代码时把 x500 订阅的 Gazebo 模型名改错（画面一直收不到）和上一次尝试的容器还占着端口，两处都已修复并补采；tailsitter 相机改为一个固定安装矩阵（PX4 对 tailsitter 始终按多旋翼机体系报告姿态，已用真实飞行姿态核对）。`quadtailsitter` 也在真实仿真里跑通“垂直起飞 → 转固定翼 → 盘旋 → 返航 → 转回多旋翼 → 降落”，途中修了两处：返航和转换的 ACK 几乎同时到达、后到的把先到的覆盖（改为按命令号记录）；PX4 不接受以命令形式发的 VTOL 降落（改用普通降落命令，真机代码同样修正）。
- 2026-10-17：快系统候选框换成 YOLOE v3（训练数据是上一版的 2.3 倍，加入易混物体场地的飞行画面）：在 888 帧测试世界上，候选召回 81.4% → 91.2%，画面里不到 8 像素的极小目标 57% → 79%，训练中没当过目标的停车牌 +29.5 个百分点；每帧候选数不变、误报更少、耗时不变（docs/yoloe-v3-proposer-round.md）。机上默认、Orin 基准和导出已切换；慢模型轮次评测暂留旧版以保证和 r12/r13/r14 可比。训练中途两次因本机内存被其他程序占满而中断，靠新增的检查点续训自动恢复。
- 2026-10-16：单目测距复用（飞机只有一个相机、没有测距仪，用户选定方案 B2，见 docs/onboard-compute-plan-options.md）。离线评估 30 个仿真录像、1,274 个目标帧（docs/onboard-ranging-reuse-eval.md）：现在用的“相机视线与地面相交”在地面高度准确时 P95 5.3 m，地面高度偏 5 m 时变成 24–33 m；新的“步间多视角三角化”（每走一步拍一帧，同一目标的几条视线交会）不依赖地面高度，P95 4.45 m，计算每次不到 0.1 ms；早期 RGB-SLAM / 视觉惯性测距需要连续视频，现有步进录像评不了；单目深度只在 10 m 以内准。已接入机上（都默认关闭）：步间三角化 + 按地面不确定度自适应融合，用于确认报告位置、计数去重、接近/绕飞中心和任务摘要，不影响跟踪关联和身份判断（`step_triangulation`）；近距单目深度只在估计距离 < 10 m 的接近/下降/近距绕飞阶段、每步最多一次、不与慢模型同时跑（`near_depth`），小样本回放里它还没带来改善，保持关闭等更多数据。另修复早期模块两处问题：视觉惯性测距会接受“未来时间戳”，单目深度把光轴深度当成斜距。
- 2026-10-16：查清“真背包被拒”的原因：1,900 次真实飞行判断里，46 次被拒的真背包全部来自同一个场地世界，候选框只有约 10×13 像素——是“太小看不清”，不是“不认识背包”；r14 虽然少漏判 36 次，但多误判 75 次（主要是把 SUV 当成皮卡 3 → 72 次），所以身份判断继续用 r12。下一轮 r15 改为补采多世界的小背包、按候选框大小分层验收、补成对难反例，不再复制正样本（docs/slow-system-r15-data-plan.md）。新增可选的“小框拒绝先飞近再看一次”（`small_reject_recheck_px`，默认关闭）：离线看，12 px 阈值会把 58 个真目标拒判和 142 个干扰物拒绝改为飞近复查，代价需要实飞验证后再决定是否开启（docs/small-reject-recheck.md）。
- 2026-10-16：收尾与整理：不指定模型时，机上策略和任务回归套件默认改用当前生产组合（身份判断 v2-r12、规划 v2-r14），旧配置行为不变；发布一致性清单补入 pre.8 之后新增、已在真实运行路径上的约 60 个文件（任务包、安全包络、健康门、PX4 预检、地面站签名等），为 0.3.0-pre.9 做准备（正式版本号和证据要在真实 SIM/QGC 验收时重建）；机上策略文件拆出默认配置、候选词表和 YOLOE 候选框模块（只搬移，规划/身份提示词逐字节不变）；mover 世界验证加了无需 Docker 的冒烟测试，避免再因导入错误白跑一次仿真；标注工作台测试在本机高负载时请求超时（每次请求都会重新校验整套数据，负载下 5–9 秒），把测试超时放宽到 30 秒。YOLOE v3 候选框模型（数据量为 r3 的 2.3 倍，1280 分辨率）训练中，训练脚本支持从检查点续训，本机内存被挤占导致中断后会自动等内存再续。
- 2026-10-16：机上“规划模型 / 身份模型”分开（可选 `planning_model`，默认关闭不变）：起飞前用 v2-r14-multi 把任务文字编成计划，然后完全退出、释放显存，再启动 v2-r12-multi 负责飞行中的身份判断；同一时刻只有一个模型在内存里，适合 Orin Nano 8GB。规划模型部署时不加载视觉投影器，已实测与评测设置结果完全一致（80 条难任务 61.25%，两种方式逐 token 相同）。规划失败会自动退回用身份模型编计划，计划不合法则不起飞。地面站：任务包和任务级指令由地面站签名（Ed25519，C++ 与 Python 规范字节已用同一组测试向量交叉核对），指令序号跨重启持久化、不回绕、加文件锁防止多开重复。
- 2026-10-16：慢模型 v2-r14-multi（只温和加权背包/消防栓/邮箱，复制样本 187 → 40 条）：真实飞行身份 90.8%（r12 92.8%、r13 76.4%），干扰物拒绝 87.8%（r12 95.0%），真目标召回 94.4%（r12 90.4%）；任务计划 80 条难任务完全正确 60%（r12 44%）。结论：身份判断仍是 r12 最好，任务计划是 r13/r14 更好，两者在一个模型里难兼得。由于机上只在起飞前编一次计划，下一步改为“起飞前用规划模型编计划 → 卸载 → 换身份模型飞行”，同一时间只占一个模型的内存，适合 Orin Nano 8GB。
- 2026-10-16：一批安全与可靠性改动合入（每项都经过独立审查、多轮修复）：真机 PX4 起飞前检查（参数按类型读写、断链/遥控/低电量失效保护分开核对、只写需要改的参数且失败回滚）；任务签名加固（重启不丢防重放状态、真机必须用签名任务包、伪造指令再多也不会挡住真指令）；地面链路加固（固定地面站地址、防重放状态落盘、按来源限流）；健康监控（慢模型掉线先悬停、超时返航，模型服务交给系统自动重启）；可选的深度障碍源（实验功能，默认关闭）；按地点去重时不把 3 m 外的同类物体算成一个；故障演练：用严格标准（看故障后 2 秒内飞机的真实状态，而不是“曾经发过一次指令”）跑 16 个故障场景，先发现运行时把飞行交还给 PX4 返航时会出现没有设定点的空窗，改为“确认 PX4 已切换前一直发悬停指令，返航切不过去就改降落”，16/16 通过；仿真新增 6 种光照（清晨/傍晚低太阳、阴天、黄昏、薄雾）和同场景换光照的测试集。
- 2026-10-16：慢模型 v2-r13-multi（为治“真背包被拒”，把背包/消防栓/邮箱等正样本按类别最多复制 4 倍）结果：任务计划明显变好（60 条任务完全正确 77% → 87%，80 条更难任务 44% → 58%），但在 1,900 次真实飞行判断上身份准确率 92.8% → 76.4%、干扰物拒绝 95.0% → 58.4%（377 个原本拒绝正确的干扰物被改判为“是”，只换来 63 个真目标的改善）——复制正样本让模型变得过于爱说“是”。结论：机上继续使用 v2-r12-multi；下一轮只对背包温和加权，并为这几类补“长得像的反例”。另：评测发现我之前把 3 个旧评测目录归档到 NAS，导致真实飞行评测集缺图（748 条无法读取），已从 NAS 恢复并重跑，上述数字是恢复后的结果。训练脚本加了显存上限、批次级重试和每 10 步保存，续训不再因为本机内存/显存被别的程序挤占而丢进度。
- 2026-10-16：质量保障：新增固定回归闸门 `python scripts/run_onboard_gate.py`（约 800 个机载自主/任务规划测试，只用 CPU，不抢训练用的 GPU；依赖真实线程计时的测试默认跳过，空闲时用 `--with-timing` 单独跑），每次合并前必跑；两个依赖本机模型/素材缓存的测试移出闸门，一个在高负载下偶发失败的起飞计时测试改为直接检查“切 OFFBOARD 前已有指令流”。评测运行器更可靠：失败的架次可归档后补飞（`--retry-failed-episodes`）、架次完整性校验、GPU 单任务锁、链路错误与模型答错分开统计。慢模型续训修复两处 Windows 问题：续训时检查点文件被内存映射导致下次无法覆盖保存；显存/内存瞬时不足时按批次重试而不是整个训练崩溃。
- 2026-10-15：慢模型 v2-r12-multi（LoRA 秩从 4 提到 16，加入易混物体训练场地的难例）：在 1,900 次真实飞行中的判断上，身份判断 84.0% → 92.8%，干扰物拒绝 74.7% → 95.0%；“皮卡与 SUV”（两类都从没训练过）从 209 次错 167 次降到 0 次，“背包与电箱”从 47 次全错降到错 9 次；手写任务计划 97% 完全正确。新问题：真背包被拒绝 46/78 次（训练数据里背包正样本太少），已按类别平衡数据并补飞 8 个三目标场地，训练 v2-r13-multi 中。
- 2026-10-15：为 Jetson Orin Nano 8GB 提速：判断“是不是目标”时只需生成 1 个 token（其余答案格式已被约束固定），准确率不变（96.8% / 真实飞行 92.8%），开发机上每次判断从 0.45 s 降到 0.14 s，已在闭环飞行中验证（配置 `fast_identity`）。快系统检测器可导出 ONNX 并在 Orin 上直接用 TensorRT 运行，机上不需要 PyTorch；基准脚本已按 Orin Nano 8GB 更新。估算与预算见 docs/orin-nano-feasibility.md。
- 2026-10-15：完全自主、通讯可选：起飞前装入“任务包”（任务、地点、风险区域、作业范围、时间/电量预算、风险等级、断链策略），缺东西就拒绝起飞；断链后继续还是返航由操作员按风险选定，飞机只能更保守；任务包和地面指令带签名，防伪造和重放；机上按电量估算何时必须返航（仿真电池数据不能标定，需在真机标定）；任务结束自动写中英文任务摘要，没有链路时结果存在机上、恢复后下传。无通讯场景已加入仿真回归套件。中文操作员手册：docs/operator-manual.md。
- 2026-10-15：执行与感知修正：小目标候选框过滤门槛从 6 像素可调到 2 像素（小目标召回 66% → 92%，不增加检测开销）；同一物体被跟踪成多个 ID 的问题用地面位置重识别修复（ID 数减 62%）；监视覆盖整个区域；时长按真实时间计；计数被预算截断时标明是下限；地面投影支持高程图/测距仪（坡地上平地假设会偏 16–71 m）；真机运行时骨架（20 Hz 指令流，慢模型思考 1.5 s 时飞机保持悬停）。另有 80 条新规划评测任务（含 8 条应拒绝的）和单文件飞行复盘页面。
- 2026-10-15：为 Orin 省算力：同一个物体常被慢模型反复判断（一个电箱一次飞行被问 18 次）。新增可选的“按地点记住判断”（默认关闭）：同一地点已有 3 次一致判断就沿用，最终确认和计数仍必须重新判断。在 181 次已记录飞行上回放可少问 20%，结论不同的只有 1.1%。易混训练场地上用当前模型飞了 24 次（成功 8、误确认 7），这些画面正在做成难例训练 v2-r12-multi。
- 2026-10-15：查清巡逻 / 监视误报的来源：同一个电箱每次都被一致地判成背包（一次飞行里 18 次“是”、0 次“不是”），所以“多次说是才告警”的投票（`3f51e74`，按地面位置合并同一物体的多个轨迹）只能拿召回换精度，去不掉它；训练集里也从来没有“候选是电箱、任务要背包”的样本。为此新增 12 个训练用小物体场地世界，每个目标旁边都放上易混的东西（背包↔电箱/纸箱、锥桶↔消防栓/邮箱等，`ec3deee`），接下来用当前模型在这些世界里飞、把它认错的画面按真值标成难例再训练。另修复任务套件把“可接受的替代计划”误算成失败的统计 bug（`163f8b6`）：r8 通过 13/16，r9 11/16。
- 2026-10-15：找到“数一数”被编成“巡逻”的真正原因：约束解码的语法要求必填参数先写，而模型训练时参数按字母顺序写，计数的“最多数到几个”（max_count）排在最前却是可选的，写不出来，模型只好写成巡逻的“圈数”（loops）；跟随被编成监视也是同一原因。修正语法后不用重训，当前机上模型 v2-r8-multi 的计划完全正确率 83% → 90%（30 条手写任务）、70% → 77%（60 条更难的任务），全部合法（`51dbe95`）。r8 任务套件补跑：消防栓数到 2/3、监视背包召回 2/3 但有 3 个误报。
- 2026-10-15：新一版慢模型 v2-r10-multi 反而变差（计划完全正确 83% → 50%）。原因是新一批“任务文字 → 计划”训练数据把旧模板整个替换掉了，里面没有绕拍检查、监视、测绘、单独返航和单独降落，模型于是把“现在返航”编成监视、把计数编成巡逻。改为新旧模板混合（plan-sft-v4，生成时检查每种技能都出现），v2-r11-multi 训练中；机上仍用 v2-r8-multi。另修复训练脚本续训的 bug：显存不足中断后续训会训练一个多余的适配器、保存的却是全零的那个（`f1f6133`）。
- 2026-10-15：机上只需一个模型：v2-r8-multi 同时会“判断是不是目标”和“把一句话拆成任务计划”，身份判断 94.1%（此前最好 92.8%），手写任务计划 83% 完全正确、100% 合法。端到端仿真：说“数一数场地里有几个交通锥，然后返航”，飞机自己编计划、数到 2/3 个、无误报并返航；说“找到邮筒，从四个方向拍照检查，然后返航”，找到（误差 0.11 m）、绕拍 4 张并返航。测试世界找单个目标 6/12（按语义 8/12），与之前最好持平。执行器补齐测绘、巡逻、监视、跟随四种技能（与 Codex 协作）。
- 2026-10-15：计数任务限定在场地范围内搜索（`a6ec583`），飞到最近位置时按“是不是”而不是“够不够近”确认（`88dff00`，可配置 `f3539a3`）。4 个紧凑计数场地共 12 个物体：数到 4 个、定位误差 0.05–0.34 m、误报 1 个，还没有一个场地数全。同一版本在 6 个测试世界上找单个目标 8/12（按任务语义 10/12），新规则在这里没被用到，多出的成功来自飞行间的随机波动。
- 2026-10-14：飞机能把一句自由的任务文字（中文或英文）拆成任务计划（`fb138e1`）：在 30 条手写任务上计划全部合法，70% 的技能顺序完全正确，约 1.5 秒。修复执行器被内层闭环 120 步上限提前结束的问题（`66e16cd`）；计数任务在仿真里完整跑通（找到并定位到 1/3 个锥桶，没有误报，覆盖面积是瓶颈）。按物体尺寸降低搜索高度（`e032663`）在测试世界上没有带来提升（5/12，按语义 8/12）。
- 2026-10-14：新增机上任务执行器（`9c95ae3`）：按任务计划逐步执行“搜索 / 计数 / 绕飞检查 / 前往 / 悬停 / 返航 / 降落 / 记录”，报告只来自实际观测。仿真里“找到目标 → 绕飞 4 个视角拍照确认 → 返航”在两个测试世界完整跑通（SUV、邮筒，定位误差约 0.5 m）。另加“换角度再看”（`2288a4c`）：搜索一段时间仍没找到时，回到只从一侧看过、被拒绝的候选的对侧再判断一次，针对停车标志从背面看是灰色牌子的情况。
- 2026-10-14：慢系统用机上快系统的真实候选框做难例训练（v2-r4-prop，`6871877`），机上问单个候选时只允许“接近 / 确认 / 拒绝”并固定焦点（`4166335`、`c552714`）。同一测试集（Q5_K_M）身份准确率 85.5% → 92.8%，干扰物拒绝 73.1% → 86.6%，单次约 0.44 s。闭环 6/12（按任务语义 8/12），加油站牌误确认消失，成功架次定位误差 0.29–0.57 m。剩余问题：SUV 被当成皮卡；停车标志从背面看是灰色八角牌，被以“不是红色”为由拒绝（需要换角度再看）。另加反事实任务与“留出类完全不进训练”选项（`4414a14`），后者使测试变难、单独效果待消融。
- 2026-10-13：机载自主闭环在 6 个测试世界（目标类别训练中都没当过目标）上实飞评测：只给任务文字，飞机自己搜索、接近、确认、记录位置并返航，全程不读真值。修了三处主机问题后，成功率 2/12 → 6/12（按任务语义 8/12），未确认 9/12 → 1/12，成功架次定位误差 0.35–2.22 m。修复内容：轨迹锚定到地面坐标，转向后不再丢 ID（`df7ec5f`）；接近时保证目标留在画面内，不再飞过头（`2006aa2`）；焦点被拒后不再原地悬停（`7c1aaa7`）。剩下的错误都是慢模型分不清相近类别（SUV/皮卡、加油站牌/停车标志）；另发现部分世界任务文字有歧义（“找到人”而场景里有两个人），打分已单独标出（`f44c3a1`）。详见 `docs/onboard-loop-sim.md`。
- 2026-10-13：慢系统改为“VLM 判断是不是目标、主机按画面大小决定要不要再靠近”。VLM 只判身份时准确率 92–98%（只用色块世界训练的 r5 在真实外观世界上也有 97.9%）；加上主机尺寸门控（候选框短边 ≥ 20 px 才确认）后，端到端决策准确率 91.3%，确认召回 100%、精度 96%（`406ba97`）。
- 2026-10-13：用 Gazebo 分割相机生成像素级检测标注（`4652e9f`），小物体标注对齐大幅改善：停车标志标注 IoU 0.02→0.83、行人 0.35→0.65。重新采集 80 个 episode（campaign-20261012-v2-r3-seg）。闭集 YOLO26n 测试集 mAP50 0.422→0.768；开放词汇 YOLOE 微调后已见类 0.769、训练中完全没见过的类 0.168（零样本 0.102），未见类需单独降低置信度阈值（`2a954e7`）。
- 2026-10-12：仿真世界从纯色方块换成 39 个真实外观的 CC0 模型（车辆、行人、锥桶、邮箱等），生成 40 个新世界，测试集保留训练中没当过目标的类别（`265f3c6`）；修复 9 个资产在 Gazebo Harmonic 下渲染全黑的问题（`0fd0c4e`）；按障碍物遮挡剔除看不见的目标标注。
- 2026-10-12：慢系统加入候选目标放大裁剪图和“事件焦点”输入，去掉了一处标签泄露；色块世界测试准确率 62%→89.2%。llama.cpp 量化部署：Q4_K_M 掉点明显，合并后 Q5_K_M 85.6% / 1.26 GB、Q8_0 1.83 GB，4060 上单次约 0.5 s（Orin 未实测）（`9ae8167`）。
- 2026-10-12：新增多用途任务计划合约：13 个技能（找、计数、巡检、巡逻、测绘、跟随、监视、投送等），严格校验，投送默认关闭（`72f285e`）。
- 2026-10-11：latest-wins 帧服务器按 PX4 仿真时间对齐图像和位姿（`29d893e`，`0630a41`），gen_w0025 实测 |sim skew| 为 0 ms，之前 p99 为 376 ms；数据集构建和行为克隆加载会跳过时间偏差过大的步。
- 2026-10-11：修复 Windows 管道关闭死锁（`b842609`）；结束时原地降落，不再触发 RTL 爬升（`addfe1c`）；容器 JSON 日志限制为 5 MB × 2（`a169832`）；Docker WSL 磁盘镜像从 140 GB 压缩到 70.8 GB。
- 2026-10-11：程序化生成 40 个仿真世界（28 训练 / 6 验证 / 6 测试，`3dd3c30`），使用规范化通道哈希（`41be3d5`），真值高度改为相对世界原点（`4a50736`）。
- 2026-10-11：专家教师改为朝目标水平方位接近（`232b351`），起飞高度取最高障碍物顶部 + 4 m，并检查航段与障碍物是否相交（`98716cd`）；修复 gen_w0025 在 5 m 高度撞障、横向航段时相机看不到前方的问题。
- 2026-10-11：COCO / 红车 YOLO26n 在 Gazebo 画面上的召回为 0/11（检测率报告与真值自动标注，`42b293c`），因此新增仿真真值自动标注、数据集构建、YOLO26n 微调和留出世界评估流程（`525aa32`）。
- 2026-10-11：policy v2 闭环 rollout、DAgger 标签和基线对比框架（`1157a1f`）；仅 SIM，尚未做训练后的策略评估和晋升，默认 r33 与版本 `0.3.0-pre.8` 不变。
- 2026-10-10：`29d893e` 之前的 Gazebo image frames 是 FIFO backlog，和 pose 时间错位；`urban-explore-r12`、`urban-expert-r1..r5` 以及更早的 Codex Gazebo image evidence（包括 8/8 STOP shadow 结果）不能支持结论。latest-wins frame server 加入 sim-time 对齐后修复该问题；`aligned-explore-r1` 的最大 `|sim skew|` 为 44 ms。

- 2026-10-10：新增 SIM 教师数据采集与离线审计分离的首个完整 Gazebo PX4 SITL 闭环记录。urban-explore-r12 含 5 步、10 张 RGB、10 张深度、17789 行 MAVLink；审计时长 118.484 s，位置误差均值/最大值 0.114270/0.217683 m，偏航误差均值/最大值 2.497954/5.485699°。保留 r1-r11 控制链修复历史；仅 SIM，不声明 HIL、真实硬件、户外飞行、训练或晋升。

- 2026-10-09：收尾教师架次基础阶段，删除未被引用的 `docs/current-progress-*.json` 快照（`f124fcf`）。原条目写入时编码损坏，细节以该提交为准。

- 2026-10-08：NEW提交前缀／原计划与当前执行重验／source-derived机体包络及静态环境查询；actual spawn-only对照初始抬高15cm后自然落稳model约.067m，足点8cm垫面差−.186µm。root／child0、4RGB／4Depth／5222原包与清理闭合；保留R1 canonical hash误配失败及原−6.44cm重叠。导航与15静态碰撞重算通过；独立审阅修复RX误用main-only writer、原时刻50ms误门和known提交批次复用问题，最终715项联合CPU通过。未ARM／飞行／新增训练或晋升；完整资源预算及真实Grant仍待实际整合，r33、canonical及pre.8不变。

- 2026-10-08：NEW同期源窗actual native0：4RGB／4Depth、5819原包、37查询／74输出，33来源窗口与owned清理闭合；实际加载15碰撞体和原始四帧导航坐标链一致性重算通过。R1窗前HOME范围误拒保留，R2窄scope修正不改原包；纯源审计不造live Context／origin／Grant。正常ARM／Mission／一次RTL执行入口通过CPU协议验证，force参数0，纠正heading与当前模式健康语义；尚未实际ARM／飞行／新增训练，默认r33／canonical／pre.8不变。

- 2026-10-08：NEW独立开发入口：静态SDF全路线碰撞检查、source-bound ENU配置风审计、唯一RX/send Mission INT façade；不造PASS／Grant、不改原资源flag。当前源码实际只读Gazebo/PX4运行root／child0，18命令／36输出、16合格地面快照、2RGB／2Depth与4356原包，来源窗口和owned清理核验；晚采非同窗、heading_good_for_control=false不升格资格。646联合CPU回归与Ruff通过，原点／净空许可和行动训练仍待闭合，默认r33、pre.8及旧canonical共享路径不变。

- 2026-10-08：NEW source-recomputed SIM预算／保守能力与v2多旋翼教师Mission准入；保留未知空速和原观测时间，无检测分支的采集路线不再要求perception；GCS255/190上传／回读严格绑定实际sender，旧250/191兼容。三智能体并行补查MiniCPM-Robot／MolmoAct2／GR00T；无新模型／训练／ARM／上传／飞行／晋升，当前代码parity与旧运行证据分开。

- 2026-10-08：NEW Context v2未知物理量／来源与保守能力检查，旧模型／Mission显式版本隔离；458定向＋122文档／索引／回执回归共580 CPU测试通过。actual一次性SIM操作员R2退出0，16合格地面快照、2RGB／2Depth／4428原包；八项保护不变，R1失败保留。新公开模型查验与当前parity静态重绑定已整理，未ARM／飞行／模型训练／晋升或正式发布。

- 2026-10-08：NEW V3双journal批次、独占RX-send与并行有限相机窗口，108新／联合366
  CPUtests通过；独立审查修复close异常跳过join、主次错误覆盖与部分API计数丢失。
  actual R3保存6RGB／6Depth／5943原包、0orphan／0overflow、57快照，3helper native0；
  native1/context-refused，45次实际STANDBY门拒绝、12次skew、3次输入未齐，不冒飞行。
  实测189.68条／秒、中位10.08ms，P95却升至223.39ms，不称实时通过；owned清理与26
  来源闭合，旧R1/R2、默认模型、版本与发布门保留，没有新模型加载／训练／飞行。

- 2026-10-08：NEW V2 packet-first typed capture＋入口共90新CPUtests／联合258通过。
  实际R2保存2RGB／2Depth／1057遥测（0orphan），4228源文件独立SHA核；root/child1
  context-refused，原observer skew错误与陈旧HOME/BATTERY、续航0、空速NaN分开记录。
  owned清理通过、19源前后不变。实测串行36.43ms／25.81条每秒，V3双journal并行方案
  尚未实现；原件、默认模型和发布门保留，0新训练／飞行。

- 2026-10-08：三个子智能体并行完成NEW owned-x500会话／原件绑定观察快照／仅观察入口，
  168定向CPUtests通过；修首次UDP peer、慢I/O时效、风源漂移、封存及清理异常。
  actual R1容器启动／native1，在相机前拒非JSON投影；保存1合法入站原包，未保存失败包，
  不定具体class或续航零值。ownedJob空／容器stop-rm0及absence核，12源前后不变；
  另建v2原packet先落盘与可逆类型编码，原失败与默认引用／版本／发布门保留。

- 2026-10-08：NEW独立native模型加载监督器51作者／独立CPUtests通过，修复Windows
  venv launcher身份与Job接管竞态。实际R4只读预检native1资源拒绝／0load，physical
  5.05GiB／commit8.08GiB，自有进程树与capture关闭；原stdout／非零状态与旧toy失败保留，
  不取消门、不关闭用户程序，不当OOM根因或模型加载修好，Controller不可达未派remote。

- 2026-10-08：NEW held/yaw教师Mission编译82新／邻接154／独立82 CPUtests通过，
  只改WAYPOINT hold/yaw、补计完整新增预算；NEW唯一接收者原遥测入口22作者／独立、
  与既有组件联合248 tests通过，当前pymavlink七CPU构造包编码解码采集兼容。外来来源
  仅原序记录，不进入合法cache；不造自动PASS/grant，不宣称真实teacher飞行或训练。

- 2026-10-08：已有208特征／11头／502raw实际CPU容差重放闭合native0，反证恒定STOP
  或有利STOP偏置，不定因live八帧（无featurebytes）；新三维执行轨迹合同与原文件投影
  bundler实现，分开proposed／approved／submitted／outcome及actualpose，metadata／UUID／
  raw数值／输出SHA门闭合。三组件160 root及独立CPUtests通过，0optimizer／飞控；
  尚无新teacher轨迹或训练准入，默认／版本／发布门不变。

- 2026-10-08：精确候选与保存动作头实际接入新Gazebo相机，native0 `5afbe8`：8帧均完成
  视觉语言融合、动作头计算与协议检查，全部输出sequence_stop，不代表返航或任务成功。
  参数／head／归一化／运行时来源前后闭合，飞行命令、训练更新、文本生成均0；首次
  BatchFeature容器拒绝完整保留，新R2浅适配保持原Tensor身份与四字段门。新loader／plan／
  worker150项CPU测试、R2适配37项与独立37项通过。默认、质量晋升、Jetson与发布门不变。

- 2026-10-08：Gazebo共享导航输入实际闭合native0：8份1280×960 RGB、8份配对深度、
  8个Actor请求，QPC同一时钟、十源身份和原helper恢复通过，owned stop/rm0、命令0。
  三组件120 root CPU fixtures／Ruff与独立37项通过，独立逐payload／wire／RGB／Actor／
  request审核68,812,800B。地面只观察，不加载模型、不训练或飞行，不宣称实时；新
  三维教师数据与动作覆盖计划写入仓库，默认／版本／发布门不变。

- 2026-10-08：新增实际Gazebo wire到同NavigationActor的输入桥、共享request的preloaded
  学习动作后端；83 CPU fixtures／Ruff／独立review通过，修复最终绑定校验耗时门。
  原文与RGB→四prefix融合→保存head＋TRAIN归一化，不造token、confidence或固定尺度；
  组件不认证模型、不授飞行权，新独立采集env不改训练env。真实采集与模型推理另行验收。

- 2026-10-08：208真实candidate特征缓存实际native0／单GPU退出，矩阵精确重载及独立
  逐行SHA／共享prefix审计通过并封存终态。固定CPU配方实际完成11×200＝2200更新、
  8800四阶段WAL／502raw／11头与TRAIN归一化精确重载，最终loss均改善；LOEO动作
  46／208、joint5且均STOP，导航质量仍不合格。同TRAIN两final不证明新数据帮助，
  旧head800合计3000、LoRA280另账，默认模型／飞行／发布门不变。

- 2026-10-08：稳定缓存纯CPU训练入口46root fixtures／Ruff与独立只读核实通过，封存
  software-r1；严格消费root闭合提取终态＋外部SHA，固定11heads各200更新，8800四阶段
  WAL／502raw后评分，train-only归一化，旧800／280继承。尚未消费实际缓存或运行本轮
  导航训练，不把2200设计上限当实际消耗或质量通过。

- 2026-10-08：candidate208真实缓存入口完成38新CPU用例＋36doc/parity共74root通过，
  Ruff／独立静态审查通过并封存software-r1；Windows实际绑定、闭合prefix、partial拒绝及
  实际runtime来源门均验证。锁定计划后本机单GPU session58872启动，尚无终态、完整208
  实际缓存或稳定配方导航训练成功证据，不冒充已通过。

- 2026-10-08：并行接入old86＋new122共208条TRAIN的真实candidate特征缓存与稳定训练
  入口，锁定九整段episode／两个scene及逐条CPU prefix身份，不混dev59或NEW82。
  Controller资源预检无合格在线GPU节点，转本地单模型；尚未完成新真实提取或训练，
  旧800head／280LoRA不重置，默认模型、飞行和发布门不变。

- 2026-10-08：新增独立stable CPU动作头组件，train-only归一化、小lr AdamW、最终更新后
  真loss改善门、累计四阶段失败计账、实际FP32字节缓存、head＋norm精确保存重载；
  64新fixtures＋36doc/parity=100root通过，Ruff／独立全文review和stdlib诊断通过，软件
  回执封存。尚无新真实feature提取或导航训练，不重置800头／280LoRA、不改默认或飞行。

- 2026-10-08：CPU122 r2修复可选Arrow导入后实际native0，122个正常来源会话／四prefix与
  full response-only元数据对旧记录exact、prepared整文件byte-identical、结束来源不变；
  自有finder清理／原finders保留、Arrow及datasets absent、CUDA未初始化。99软件tests、
  Ruff和独立终态审计通过并封存CPU admission-r2。只证明输入准入，尚未训练或授权候选。

- 2026-10-08：动作头修复版真实native0完成145融合特征、800次小头更新、231原始预测、
  保存精确重载与来源闭合；三份纯文件独立审查通过并封存actual-r2。开发59仅2STOP正确，
  LOEO29action／0joint，四head pre-step loss由4–6震荡至321–424，质量失败。预算不重置，
  正在修复稳定优化／train-only归一化和真实feature缓存，不接飞控、不替换默认模型。

- 2026-10-08：NEW CPU122 official输入入口59root tests／Ruff／独立审查通过；实际122条
  prefix／full response-only元数据及verified sessions、46source／10processor／21runtime闭合，
  但末尾可选模块禁门拒绝，native1／准入false，失败及后验审核封存。不追认native0；
  局部Arrow隔离小探针native0，新的v2保留禁门和逐样本元数据parity，正在实现。

- 2026-10-08：跨old86／NEW82／NEW122真实290原PNG strict及RGB解码、568 Actor引用闭包、
  12episode既有group10component分析native0；exactRGB及episode重复／跨train-dev冲突0，
  gate true。46root tests／Ruff、独立54小source＋5Pillow／保存identity图重算通过，封存
  dataset-overlap-r1。不做近似独立性、officialholdout、processor或训练准入宣称。

- 2026-10-08：动作头r1入口＋组件163项CPU软件测试、Ruff／独立runtime及配对统计审查通过；
  首次真实加载native1，实际参数身份准确但提前PEFT导入缓存safe_open，底座流式观测门拒绝。
  features／raw／head四计数全0，worker退出，封存loader-failure-r1；新v2仅修导入顺序，
  累计head预算仍800、base／LoRA已用280不重置。不声称训练、实时或飞行通过。

- 2026-10-08：NEW122真实有界CPU source审核native0，37root tests／Ruff／独立
  47source＋10runtime＋122opaquePNG与6pose几何复算通过；6独立TRAIN episode，
  116successor＋6STOP、108straight4left4right6stop，原83,617,855PNGbytes、实测853×480。
  仅786432B prefix，205visited与六compact-record身份准确，保留原值/pointer的派生
  selection不冒405MB全文。封存indexed-training-supervision-r1及人话说明；未混旧
  数据／未processor或model训练／未赋执行授权，默认model／version／release不变。

- 2026-10-08：NEW59真实模型推理native0，原23train未入；raw先持久化后评分，真实
  分析与独立复算／source／59prefix-token观察通过。动作16／59、metric0、STOP0／2，
  dev-val15／17而dev-test1／42；59格式合法不冒充质量，forward条件误差5.5m不含38
  wrong-kind，5.826s不冒充E2E实时。封存actual-evaluation-r1；不晋升或接PX4。
  并行获取6条TRAIN路线12原payload共83,692,236B，native0／49tests／独立全字节
  digest复核通过，封存indexed-route-payloads-r1；122仍为声明，schema／RGB／pose／
  typed训练准入待独立CPU审核。默认reference／version／执行／发布门不变。

- 2026-10-08：267训练及保存后独立重载均actual native0，v2真实终态auditor与独立审计
  通过；34 exact5快照、1068 WAL、534 draw、四267与累计280闭合，86条raw／token／actor／
  prefix逐条一致，封存完整terminal r2。模型动作仍27／86、STOP0／3，不晋升；NEW59
  精确计划发出并实际GPU运行，尚无闭合模型结果。补官方小型／连续动作模型查验与人话状态，
  不改默认reference／version／飞行或发布门。

- 2026-10-08：新增独立original59小结果分析入口，作者／root各56项测试、Ruff与
  只读审查通过；精确闭合report／plan／raw／scores／typed82身份，排23train、固定17＋42，
  从raw＋gold重算并拒绝summary不一致。按class／partition／macro／非forward5／STOP2
  和metriccoverage展示，raw与预算合格候选配对分栏；数学前进3m参照无虚构token／延迟。
  missing-report实际native1且无新分析目录；封存
  docs/evidence/onboard-navigation-original59-analysis-software-20261008-r1.json，补充人话评估说明。
  当前仍无actual59模型成绩，不改冻结训练／评估、默认模型、版本或飞行／发布门。

- 2026-10-08：独立terminal audit v2修复实际PEFT五文件格式缺口，作者／root各50项
  针对性测试、Ruff与只读审查通过；认证冻结r1后仅扩展private snapshot门，34 README
  明确绑5438B／真实SHA并最终重验，未知extra仍拒绝，其余WAL／参数／重载／source门保留。
  sealed r1及首次误拒失败保留，metadata不伪称旧receipt或质量证据；实际active CLI
  退出1、没有audit输出或live WAL读取。封存
  docs/evidence/onboard-navigation-recovery267-terminal-audit-software-20261008-r2.json；
  candidate86／fresh reload／完整phase仍待真实终态，默认模型、版本、飞行／发布门不变。
  同步封存当前默认reference绑定诊断；旧receipt嵌套SHA的相邻两位差异及失败原样保留，
  当前protected文件与本轮前置记录一致，未读取ONNX权重或推断历史差异起因。

- 2026-10-08：实际NEW267更新全部完成并保存final adapter；独立112F32／802816有限
  参数真实保存摘要、1068行immutable prefix四计数267、旧13＋新267＝280及四文件
  before／after SHA通过。进程仍执行candidate86，无train终态／fresh reload／完整
  source-end／质量结论；封存 docs/evidence/onboard-navigation-recovery-v2-checkpoint267-20261008-r1.json。
  同时发现34个真实PEFT目录exact5包含README，旧terminal audit exact4 fixture会误拒；
  native1准备失败保留，sealed r1不改，新增v2明确绑定README及其当前／结束SHA，不忽略extra。
  默认模型、版本、执行／实时／硬件／最终发布门不变。

- 2026-10-08：新增NEW267终态标准库审计入口与测试，最终50项root全回归、Ruff与
  全文独立审查通过；34快照／112FP32原序payload／1068WAL／534draw及四组86 raw重算，
  strict source／reload闭合，warm完整SHA与外部runtime只读边界。活动phase实际CLI
  退出1、未创建审计目录、不读live WAL；未运行实际完整终态审计，文件audit不替代
  root实际native／PID证明。封存
  docs/evidence/onboard-navigation-recovery267-terminal-audit-software-20261008-r1.json；
  质量、默认模型、版本、实时／飞行／硬件／发布门保持不变。

- 2026-10-08：新增NEW59离线评估入口与测试，作者／主智能体83项native0，Ruff及
  独立审查通过；固定17＋42开发样本、排除23train，共享Actor／prefix-only精确加载，
  实际generate单次观察并恢复hook，raw先持久化后gold评分，通用3D／yaw／stop／unknown
  与cap192／30秒合作式预算分栏。当前完整终态回执缺失时CLI实际退出1，未创建输出或
  第二模型worker；仅软件验收，实际59预测为0。封存
  docs/evidence/onboard-navigation-original59-evaluation-software-20261008-r1.json；
  不改变冻结训练、旧r2产物、默认模型、版本、quality／实时／飞行／发布门。

- 2026-10-08：NEW82独立桥接实际source-only转换native0（858d15），生成82条typed样本，
  保持23／17／42内部开发角色；62项测试、Ruff及独立终态审计542846通过。
  201仓库来源／六实际Pillow-YAML入口前后不变，正常完整验证退出后才发布样本；
  原任务／当前及最近过去RGB进入Actor，姿态／future／gold隔离，79 successor＋3 STOP。
  独立审核202小输入SHA，5大文件仅stat，不重解码pixel identity；旧86数据SHA不变，
  新旧sample ID无交集。封存 docs/evidence/openfly-original82-supervision-20261008-r1.json；
  新数据仍无left／多样尺度，不接旧四类准入，不宣称training-ready／holdout／模型／飞行。

- 2026-10-08：独立NEW267 v2用真实constructor／warmup schema修复r1准入缺陷，
  100项定向测试、实际CPU准入及独立只读报告审核通过，GPU已实际进入训练更新。
  checkpoint-008已保存；独立标准库审核112个FP32张量、802,816有限元素和
  32行不可变阶段前缀，实际参数摘要与保存回执相符且不同于原warm模型。
  封存 docs/evidence/onboard-navigation-recovery-v2-checkpoint008-20261008-r1.json；
  仅证明已保存8步及累计旧13＋新8，不封存live journal，不表示267步／candidate86／
  fresh reload／质量／实时／飞行通过。默认模型、版本与发布门不变，r1失败保留。

- 2026-10-08：NEW closed CPU86实际终态native0（4b11a4），86正常退出并最终SHA验证的
  单样本会话、完整metadata／prepared字节与旧记录相同，196来源前后不变，178观察；
  独立只读audit 82b83d通过，55小binding重散列，不重复processor／GPU或大型资产。
  NEW267源计划／admit-cpu native0（b78266）；GPU加载后constructor无pid触发验收
  KeyError，实际native1（de2022），没有反向预检／optimizer／journal／候选。
  旧13成本＋本次0不变，r1源码／测试／计划／报告保留，独立v2修schema及实际报告测试。
  封存 docs/evidence/onboard-navigation-closed-cpu86-20261008-r1.json 与
  docs/evidence/onboard-navigation-recovery-loader-failure-20261008-r1.json；非质量／实时／
  SIM-REAL／硬件／发布验收。学习型head替代方案另有文档，未实现或实测加速。

﻿- 2026-10-08：原TRAIN三场景82原图的离线动作几何／视觉检查实际native0（e31da0），
  79项即时successor＋3零STOP通过；body-FRD米／右转正弧度转换复用已审代码，未知dt不补速度，
  STOP不是降落或任务完成。旧86＋新82原RGB全部解码，6场景对按RGB／dHash／均色规则
  未发现重复边；182仓库／资产binding前后相同，27项测试／Ruff及独立只读audit b17e35通过。
  不是完整Pillow／PyYAML运行时封存，未创建样本或晋升开发划分；新82仍为75前进、2右转、
  各1升降、3停止，无左转、平移全3m／转向全30°，不宣称泛化或多样尺度能力。
  证据 docs/evidence/openfly-original82-geometry-visual-audit-20261008-r1.json；保留旧失败13账，
  新CPU86／NEW267入口仍在实现审核，没有GPU重启、默认模型替换、飞控指令或发布门放行。

﻿- 2026-10-08：closed-sample实际官方processor CPU诊断session49894终态native0（eddaac）；
  5个原TRAIN样本固定legacy_owned→closed_sample各一次，共5对／10条编码，不是all86重编码。
  192个来源worker前后不变，10条实际metadata／actor／四prefix-full tensor records／
  response-only labels／target token counts与已保存prepared一致，5次NEW closed session正常
  exit＋final SHA verified后才返回完整metadata；成功返回的validation images已关闭，
  partial decode在tuple返回前的cleanup仍未证明。CUDA始终未初始化，无model／GPU／optimizer。
  完整样本CPU encoding观测均值old 32.58145062s／NEW 16.74383808s，固定顺序、每模式一次，
  nested inclusive wall time不作相加；不是公平benchmark、camera全链或独立vision／prefill／decode。
  93条边界观测及process-lifetime OS peak只作资源诊断，commit不等于驻留，未证明stage峰值、
  memory benefit或旧MemoryError根因。163项actor／supervision／helper／host、51项profiler
  与63项NEW267预算纯软件tests各native0；fixture测试和本次实际官方processor观察分开记录。
  独立只读标准库audit 4d3445 native0验证14小binding、192当前大小、10 metadata、46组LIFO
  stage pairs＋finish、5 closed exits／CUDA false及resource PID／lifetime peaks；未重编码或大来源hash。
  NEW267 coverage-first预算合同已实现（70／66／66／65，旧13另账，累计≤280），但实际来源绑定
  schedule／新trainer／GPU恢复均未开始，不是exact resume或训练授权。sealed回执保持不动；
  旧13次失败、172条baseline、82原PNG和历史null原样保留，质量／holdout／晋升／飞行／发布仍false。
  `docs/evidence/onboard-navigation-closed-sample-cpu-profile-20261008-r1.json`。
- 2026-10-08：streamed完整GPU流程session39122已关闭，train worker与supervisor均native1；
  CPU／完整baseline各native0保持成立。正式optimizer四阶段计数attempted／returned／
  synchronized／completed各13，52个journal事件精确对应冻结schedule前13draw，
  last phase completed、uncertain false但280计划未完成；四类backward-only preflight各无更新。
  下一次streamed输入source validation的__exit__→bound.verify→1MiB stream.read发生MemoryError，
  不是CUDA OOM报告。sources_after为null、来源后验校验未完成，不是已证明source drift；
  retention／system commit根因未证。未保存candidate、未评估／fresh reload、无训练后参数digest。
  独立标准库audit 4348ba native0核验journal／process chain／argv／chrono／failed-finished／
  overall终态；sealed回执14小binding复核通过，无模型／GPU重跑或5GB来源重散列。
  失败与13次真实更新原样保留，无自动retry／预算reset／冻结plan改动；成功baseline、82原PNG
  和历史null快照不重写，默认reference／parity、质量／晋升／飞行／执行／发布门仍不变。
  `docs/evidence/onboard-navigation-streamed-training-failure-20261008-r1.json`。
- 2026-10-08：Stage 2离线实际提取OpenFly原TRAIN三route的82个原始embedded PNG，
  session89352终态native0，56,041,231B保留原encoded bytes，env26／16／23为23／17／42帧；
  均RGB8、853×480，owner完成CRC／单zlib stream／scanline bytes／Pillow decode和82输出重验，
  174repo／10显式CPU runtime来源前后不变。35项CPU fixture测试通过，不代替模型或飞行验收。
  独立标准库audit df7986 native0逐核82字节SHA／shard-row-frame-path绑定及PNG结构、
  三pose小文件publisher digest与82原metadata；未重跑Pillow／Parquet／GPU或5GB来源散列。
  首次r1 admission native1原样保留：7个yaw 0.0被JS重序列化为int0，未创建输出；
  仅新建保持Python原JSON类型的r2 plan恢复，未放宽source equality或改已执行源码／测试。
  六个sealed binding、default reference／release parity复核；FRD动作几何／visual split／
  independent holdout／样本／training-ready／质量／晋升／Gazebo闭环／飞行／执行与发布门仍未通过。
  不合并旧86数据或改冻结GPU流程，所有旧阶段与失败保留，release仍0.3.0-pre.8。
  `docs/evidence/openfly-parquet-original-images-20261008-r1.json`。
- 2026-10-08：streamed完整baseline两轮各86条真实GPU推理已被supervisor接受，child native0；
  53024→28644→16492进程链／argv／chrono及唯一finished事件绑定通过。180来源前后不变、
  全chat metadata与CPU相同，BF16底座和112个FP32原LoRA后验digest未变，baseline无optimizer。
  base严格JSON／动作／尺度0／0／0；原LoRA为69／55／0，69个合法输出全部forward，
  left／right／stop均无正确动作。55条matching-forward尺度误差97m×42／7m×13，无质量晋升。
  两轮实际generated.elapsed_s均值7.396002s／6.342779s、p95 8.350901s／9.159137s；
  H2D后计时含generate／sync／suffix D2H／decode，不是CPU／camera全链或拆分prefill延迟。
  独立标准库audit native0重算172原raw诊断、actor／prefix／target SHA，26小binding及bounded
  checkpoint headers通过，无processor／GPU／模型重跑或5GB来源重散列。旧partial和真实失败保留。
  后续train已请求，但本快照train final report／pipeline completion仍缺，native与更新计数未知，
  不把baseline完成写成280训练／fresh reload／holdout／实时／飞行通过；默认与全部发布门不变。
  `docs/evidence/onboard-navigation-streamed-baseline-phase-20261008-r1.json`。
- 2026-10-08：实际取得OpenFly原TRAIN三条完整Parquet shard与各自pose／scene config，
  session59955终态native0；九个文件共56,091,088B与发布者digest匹配，实际15次成功HTTP
  response、总attempt上界54。27项CPU fixture测试通过，不把替身测试当官方PNG有效性。
  owner终态重验161repo＋5显式runtime＋9outputs，root后续再次九文件字节核验native0；
  独立标准库复核四binding／九payload及保存来源数组，不重跑Parquet schema或大来源散列。
  原TRAIN的env26／16／23分别23／17／42行metadata与原pose／稀疏annotation精确对齐，
  三scene尺度比为1，无单位猜测／重缩放／dt推断。完整shard含图像bytes已下载／散列，
  但image列未materialize／decode／extract，图像有效性／visual holdout／FRD动作映射／
  样本创建／training-ready仍未通过。旧缺RGB失败、元数据快照与冻结86样本模型流程保留；
  不是模型／Gazebo闭环／机载／PX4／硬件验收，默认模型与所有晋升／发布／执行门不变。
  `docs/evidence/openfly-parquet-acquisition-metadata-20261008-r1.json`。
- 2026-10-08：完成六个预训练无人机导航候选的官方资料与发布者元数据研究，只有3DG／WorldVLN
  核到公开非gated权重；10个文件的LFS大小／SHA不是本地下载验收，发布精度体量均超8GiB。
  3DG为5×3waypoint，初始GT方向、外部DINO／depth停止与GT episode终止不能冒充truth-free搜索；
  WorldVLN及AutoFly论文模型在地面服务器，AutoFly匿名权重401不等于证明不存在。
  VLA-AN的NX16GB30W机载推理2–3Hz与异步闭环只是论文报告，不是本仓8GB或端到端实测。
  新增精确OpenFly原TRAIN三条Parquet路线元数据绑定，阶段native0、26项测试／Ruff通过；
  五个小binding与保存156来源前后数组复核，计划56,091,088B、上限134,217,728B。
  当前快照payload下载0B、schema／RGB提取／样本／holdout／training-ready均未通过；
  未来授权获取另立结果，旧缺RGB真实失败与冻结GPU流程／历史保留。无模型下载／GPU／训练／
  默认模型改动或质量／硬件／执行／发布晋升。研究与路线元数据回执分别为
  `docs/evidence/onboard-pretrained-navigation-model-research-20261008-r1.json`、
  `docs/evidence/openfly-parquet-exact-route-metadata-20261008-r1.json`。
- 2026-10-08：streamed GPU流程已写parent base的86条实际推理结果（LoRA disabled），
  forward／left／right／stop为70／6／7／3；原严格JSON／动作／尺度均0/86，token cap reached0。
  标准库独立后验审核native0：重算全部diagnose，target SHA／actor identity／四prefix记录
  全部匹配保存CPU元数据，13个小binding核验；无模型／processor重跑或5GB来源重散列。
  实际generated.elapsed_s均值7.396002s、p50 7.298781s、p95 8.350901s，不是几十秒日志间隔。
  计时H2D之后开始，含generate／sync／suffix D2H／decode，不含CPU输入全链，未拆prefill／decode。
  无live camera验收；86条计时均超5s只提示条件性freshness冲突，不放宽阈值或修原输出。
  该回执封存时warm-start86轮未完成、baseline／pipeline native仍null，phase源重验与后验参数
  检查未终结，280训练未开始。不是LoRA完整对照／训练／质量通过，旧失败和所有历史保留。
  `docs/evidence/onboard-navigation-streamed-base-round-20261008-r1.json`。
- 2026-10-08：新增导航请求／结果协议与预加载实验backend，158＋89项纯Python替身测试通过；
  联合15项流式CPU回执元数据检查、8项文档检查共270项，native0，8个小文件binding重验。
  9个adapter状态负例先red后green，拒绝持续LoRA状态漂移；Ruff五文件通过。
  1295条warnings来自pytest旧AST API，不是模型告警。仅用共享actor prefix、不读gold，
  token suffix／原始输出／耗时／身份／新鲜度门通过软件验证，不声称真实processor或模型推理。
  精确loader／独占worker／硬deadline尚未实现；无真实Torch／GPU／optimizer／飞控／PX4。
  独立GPU流程最后仅parent70/86 active，终态未知、280次更新未开始；旧失败和冻结来源保留。
  默认r33 reference／release parity／0.3.0-pre.8未变，质量／晋升／硬件／执行／发布门全部关闭。
  `docs/evidence/onboard-navigation-inference-software-20261008-r1.json`。
- 2026-10-08：完成OpenFly multiscene r2 CPU dry-run，native0、117来源含plan前后相同，
  46项测试通过，311条旧Pillow helper弃用警告独立保留；Ruff／scoped diff检查通过。
  计划4场景／6路线／168raw帧，旧env18 train的86帧已验；新env26 train23、env16 val17、
  env23 test42帧共82仍缺，3个scene config与3个episode tree尚缺，无新样本／holdout／training-ready。
  r1首次报告与精确plan／tool／test输入快照保留；封存工具／r2计划不改，后续公开数据获取另立步骤。
  本条只记录历史preflight，不改写为下载完成或优化／模型／质量／飞行通过；默认与发布门保持。
  `docs/evidence/openfly-multiscene-acquisition-preflight-20261008-r2.json`。
- 2026-10-08：新增streamed导航continuation入口、测试与独立新计划，不改冻结r1来源；
  单样本按需重编码、不保留完整86样本tensor缓存，287项软件检查通过。实际CPU阶段
  worker24168／launcher40156／supervisor53024身份核验，native0、180源前后不变，
  全86完整chat编码通过；prepared-inputs与原CPU验证字节相同。当前baseline已编码86样本，
  GPU生成进行中，baseline／pipeline终态仍未知，不声称推理完成、280次更新或保存后重载通过。
  原r1主机MemoryError及报告保留，生产backend、默认parity、质量／硬件／发布门不改；
  独立审计native0重开180来源，无drift；只核对元数据，未重算tensor／label hash或实测live驻留。
  CPU阶段证据：`docs/evidence/onboard-navigation-streamed-continuation-cpu-20261008-r1.json`。
- 2026-10-07：新增独立实验用均衡导航 continuation CLI 与新280步计划，warm-start原LoRA
  权重但新建optimizer，不恢复旧optimizer／scheduler／RNG；238项定向软件检查通过。
  实际CPU-only完整chat编码覆盖86样本，supervisor／worker native0、178源前后不变，
  最长524总token／71响应token；该CPU验证没有模型加载、推理、反向或参数更新。
  完整pipeline终止native1：baseline worker 3872虽已精确装入原BF16底座与FP32 LoRA，
  但在编码进度20样本后，冻结pilot的tensor_record／numpy.tobytes触发主机MemoryError，
  不是CUDA OOM；没有推理、optimizer或280步训练开始，不当精确加载为训练／质量通过。
  原r1失败、plan与六个实现来源冻结保留；另建streamed r2入口／测试／计划，尚无runtime通过。
  生产导航backend、默认r33／COCO／parity／0.3.0-pre.8、质量／飞行／硬件／发布门不改；
  旧阶段与失败报告原样保留。CPU回执路径
  `docs/evidence/onboard-navigation-balanced-continuation-cpu-20261007-r1.json`。
- 2026-10-07：实现新的共享导航actor输入基础，官方本地Qwen3VLProcessor在CPU验证
  全部86个保留样本，两入口identity及四个实际tensor字节完全一致，native0、129源前后不变。
  TF4.57.6／Torch2.6.0+cu124，最长453token／每图46080pixels，未超过4096／50176预算；
  CUDA未初始化，无模型加载、推理或optimizer。独立只读核验重新散列来源、重算actor identity
  并核对保存tensor记录／预算，native0；没有二次运行processor。这里只验证保留离线RGB投影，
  不声称独立实时捕获、旧训练prefix字节一致或SIM／REAL通过。新trainer、导航live backend和
  280次参数更新尚未实施；旧失败、train内strict／action1/4及metric0/4、原100步预算保留。
  默认r33／COCO／parity／0.3.0-pre.8、飞行与质量／硬件／最终发布门不变。
  [共享输入证据](docs/evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json)。
- 2026-10-07：已保存导航LoRA在独立GPU worker完成精确恢复，112个FP32张量、原4个
  输入与生成token完全一致，worker native0、恢复更新0次。原训练CUDA失败native1和原
  supervisor的Windows启动器／worker PID失败native1保留。新增真实进程链只读身份检查
  native0，独立340文件字节审计native0；PID门未删除，不重训、不改旧报告、不做飞行。
  新标准库均衡抽样器16项CPU检查通过，两次实际生成同SHA；四类各70draw、共280draw，
  86unique全覆盖、121源重验。只有抽样表准备，无新actor／训练计划／280次参数更新，
  原100步预算未变。训练内strict／action仍1/4、metric0/4；所有质量／硬件／发布门不变。
  `docs/evidence/onboard-navigation-exact-reload-20261007-r1.json`、
  `docs/evidence/onboard-navigation-balanced-schedule-20261007-r2.json`、
  `docs/onboard-navigation-quality-next-20261007.md`。
- 2026-10-07：现有 Qwen 导航 LoRA 完成一轮 86 次真实更新，344 条四阶段日志与原计划
  样本顺序一致；冻结 BF16 底座前后不变。保存的 112 个 FP32 LoRA 张量通过标准库独立
  原始字节 digest 复算，与训练后参数一致。最终同进程 fresh 底座重载在第 498 次读取的
  CUDA transfer 遇 OOM，native／三层 supervisor 均退出 1；原失败不改写为通过。
  正开发独立 fresh-process、无 optimizer 的保存后重载验证，不重复训练或扩大预算。
  四个训练内 probe 的结构／动作仅 1/4，尺度 0/4；正确 forward 类别仍把 3 m 输出成 10 m。
  无独立 holdout，无质量晋升、飞行或发布验收；版本仍 0.3.0-pre.8。机器进度保留旧失败。
  `docs/evidence/onboard-navigation-trained-adapter-20261007-r2.json`、
  `docs/onboard-navigation-execution-gap-20261007.md`。
- 2026-10-07：新 thin importpath 入口先在子进程装入 repository/src，修复原 early-import
  ModuleNotFoundError；原失败与 native 1 独立保留。298 项联合 CPU 检查通过后，真实
  load-only 完整读取 625 个 BF16 底座张量／4,255,064,064 payload 字节并附加 FP32 LoRA，
  原生及三层验收 native 0；59 源不变，独立审核通过。仅证明完整初始化，不当训练／推理
  或质量验收，缺失 optimizer 日志计数仍 null。没有改变全局环境、飞行默认或发布门。
  `docs/evidence/onboard-navigation-full-load-only-20261007-r2.json`、
  `docs/evidence/onboard-navigation-early-import-failure-20261007-r1.json`。
- 2026-10-07：新增标准库头部／逐张量读取入口，reader82项、wrapper70项、联合236项CPU检查
  与独立审核通过。原计划新load-only两个header均成功，但tensor payload为0，随后CUDA
  OutOfMemoryError约3.96GiB申请；模型／supervisor退出1、独立留证审核退出0，55源不变。
  原模型／精度／数据／86步预算不改，原失败独立保存；正实现仅子进程关闭可选整块缓存预热。
  未初始化、推理、反向或更新，journal计数null，不当训练成功；无晋升、飞行或发布门变化。
  `docs/evidence/onboard-navigation-streaming-load-only-20261007-r1.json`、
  `docs/research/onboard-navigation-allocator-warmup-20261007.md`。
- 2026-10-07：独立load-only复现原生访问异常，真实worker PID匹配Windows事件；堆栈定位
  torch/storage.py权重切片，底座尚未返回、LoRA尚未附加，26文件前后不变。失败证据独立封存。
  50项CPU读取探针和44项显式pread兼容入口检查通过；两条真实CPU读取对照均native0／gold一致。
  原0.4.5环境保留，新0.8.0环境仅用于声明的读取兼容实验，不改训练计划、模型或预算。
  小切片正确不等于完整加载／训练完成，物理与发布门不变。
- 2026-10-07：启动现有Qwen导航LoRA首轮GPU试训；封存86步／一轮计划，入口50项CPU检查。
  模型加载后原生工具退出1，相关Windows事件为torch_cpu.dll／0xc0000005，report／events／
  adapter缺失；原计数未知，不填零、训练未完成、OOM未证实，原失败与八源码快照保留。
  新增标准库进程外supervisor及load-only诊断，40项CPU检查与独立复核通过；绑定本次plan、
  确定性样本顺序与四阶段日志，拒绝向封存模型／数据／review目录写输出；不改底座、预算或默认。
  三名子智能体并行实现、诊断与官方Qwen／OpenFly／Cosmos研究，选型写入仓库；未做新飞行。
  `docs/onboard-openfly-navigation-pilot.md`、`docs/onboard-pretrained-model-options-20261007.md`、
  `docs/evidence/onboard-navigation-native-load-failure-20261007-r1.json`。

- 2026-10-07：新增OpenFly固定原train下载器、source-only导航动作契约与逐步数据构建器。
  3条完整AirSim路线／86原RGB，raw action与next pose核验；聚合标签14处错位不作gold。
  实际下载／构建／独立审核native0，142定向CPU检查；Prompt与169 actor图片输入exact，
  没有未来／姿态／答案泄漏。同scene 1组／alltrain86，无升降／独立holdout，只批准
  有预算的Qwen导航LoRA试训，不是质量／飞行／晋升。依赖源与旧七文档快照；默认、
  版本、parity和所有硬件／室外／最终发布门不改。
  `docs/onboard-openfly-navigation-pilot.md`与`docs/evidence/onboard-openfly-navigation-dataset-20261007-r2.json`。

- 2026-10-07：新增真实Gazebo retained-wire VLM回放与独立CPU审计入口；RTX 4060固定
  Qwen基座两帧生成native0／9.015秒，实际180224像素、HOLD／空计划。CPU消息、四tensor
  与token解码exact／31文件不变／native0，51软件测试通过。保留原时间与原failed架次；
  结构通过不认可安全状态／目标缺席断言，不升级live、动作训练或飞行证据。公开导航
  模型／数据与准入规格独立记录；下一轮审核原动作轨迹后增量训练Qwen。旧阶段文档
  精确快照、旧receipt与r33／COCO／parity不改，失败LoRA不晋升，版本及发布门保持。
  `docs/onboard-live-vlm-retained-replay.md`与`docs/evidence/onboard-live-vlm-retained-replay-20261007-r1.json`。

- 2026-10-07：新增Gazebo RGB／同源部分GPS/odom／原文Prompt的VLM shadow入口，
  bounded request与长episode分离，逐请求raw/metrics/rejection源绑定，相同画面不去重。
  官方processor CPU复现并修复真实grid预算，提交／写盘／清理失败保partial。联合444项
  软件测试443pass/1symlinkskip/native0，十文件Ruff与实际无进程预检0；三agent并行审查
  完成。worker离线/no_eligible_node，未执行新capture/GPU/训练/飞行；baseline和VLM
  Prompt权限分离，默认r33/COCO/parity、失败LoRA、版本与发布门不改。
  `docs/onboard-gazebo-live-shadow.md`与`docs/evidence/onboard-gazebo-live-shadow-development-20261007-r1.json`。

- 2026-10-07：新增源固定目标的空中 continuation 航段：当前位置→目标→有限驻留，
  无 TAKEOFF／LAND；往返、垂直、等待与降落余量开发参数只可收紧。原 task 全文、
  full runtime、原 context／Mission／journal／transaction 与 host 编写声明逐项绑定；
  armed／in-air／AUTO.MISSION／HOME／实测位置／MIS_TKO_LAND_REQ 接收准入。
  实际 GCS target 和执行 current 位先核验再仅在 readback 内容比较副本归一；旧 upload
  wire 与原接收字节不改。23 文件联合 911 passed／native0，独立54／146软件测试通过。
  Linux Snap Docker launcher 在 --version 即拒绝、本地 daemon 查询超时；collector0
  不冒充probe2通过。真实替换transport、父子链、取消／等待返航、在线producer、
  Gazebo飞行与动作训练仍未完成；默认r33、失败LoRA、版本与硬件／发布门不变。
  说明 `docs/onboard-continuation.md`；封存回执
  `docs/evidence/onboard-continuation-development-20261007-r1.json`。

- 2026-10-07：新增源 VLM 意图→较新执行状态的软件准入链；原目标固定 WGS-84，
  有效计划从新位置重投影，保留原 RGB／raw／时间和独立 source／execution hash。
  当前高度／GPS／围栏／资源／完整 wire Mission 净空与任务身份重验；ready-only CAS
  单次消费，普通新 RGB 不取消推理，pending 不造 HOLD。独立审查未知高度／爬升／
  起飞-降落-loiter 覆盖反例均修；默认无真实覆盖来源，在线执行仍拒绝。
  消费瞬间过期与普通新观察解 arm 采用 host-only 取消协议，保留当前 Mission；source
  真过期保留 fresh latest 队列。遥测 receive／processing 时钟分离，原时间不改写。
  receive-only collector 留 proposed／accepted／executed／completed 原始因果回执，
  新状态接受保留 host 重投影；真实回流、collector revision 联合与在途 continuation
  未完成。远端旧 artifact／stdout 实际下载精确核验：collector0≠probe2／Docker1。
  无新增 GPU 训练／生成、Gazebo 飞行、硬件、晋升、发布；冻结旧证据与默认模型不改。
  详见 `docs/onboard-vlm-runtime.md`。

- 2026-10-07：新增真实图文 v2 合同、可取消的 persistent process worker、ready-only
  provider/scheduler；host 封存 request identity，实际 RGB／Prompt／context／事件与审核
  receipt 入模型，缺失信息不补。pending 不提交 adapter、普通 RGB 只排 latest，scope／
  receipt 变化退休旧进程；legacy 5000 m 障碍占位不当测量。软件夹具完成一次共享
  executor/planner/compiler 重规划；RTX 4060 原生两例 native0、2 strict、7.06／6.05 秒，
  CPU processor tensor／raw token／parser 重验一致，缺导航状态均 HOLD／空计划。
  源观测对最新执行状态准入、任意 Prompt、真实执行 receipt 与动作训练仍未完成。
  原 LoRA native1/独立 reload native0、默认 r33／COCO／parity 不改；未晋升或发布。
  说明 `docs/onboard-vlm-runtime.md`；回执 `docs/evidence/onboard-vlm-runtime-20261007-r1.json`。

- 2026-10-07：完成 Qwen3-VL-2B 事实 grounding LoRA 实验：110 个实体 RGB 图文样本独立
  全量审核，26/41/43 保持整段隔离；RTX 4060 实际执行一轮 26 次更新，保存 adapter，
  基座／视觉主体实际字节未变。84 父／84 子配对生成严格接口合格 1→73、全字段正确 1→61；
  原始可见性字段诊断 BA 72.48%→79.05%，但遮挡正确 22/26→16/26、目标 ID 84/84→73/84，
  不把格式收益当视觉可靠性，不晋升。原生任务最终在 adapter 重新加载字节检查失败退出 1；
  原失败报告及所有 raw/token 保留。新增独立 fresh-base／FP32-default 加载入口，实测退出 0，
  112 参数及固定 val 首例输入／token／raw 一致，基座不变、0 更新，不重训或改写原失败。
  无动作训练、Mission、transport、世界模型运行或硬件／实飞证据；r33／COCO／parity 不变。
  说明：`docs/onboard-vlm-grounding-training.md`；回执：
  `docs/evidence/onboard-vlm-grounding-training-20261007-r1.json`。

- 2026-10-07：共享图文／Prompt／scope／unknown context／证据记忆开发接口接入公开
  Qwen3-VL-2B，独立环境RTX 4060实际生成8例，native0；7原始回答合同合格、1误用response
  原样拒绝。均HOLD／空飞行计划，遮挡／重现和未知飞机状态有语义错误。
  单帧8.43–13.69秒、6帧19.82秒，peak allocated5.403GiB；无tokens／历史截断。
  修正录制身份漏绑定和条件ID侧信道；独立审查179绑定／148文件，raw tokens解码一致。
  VLM未微调，世界模型未运行，r33／COCO／parity不变，不发飞控、不晋升、不获硬件门。
  回执：`docs/evidence/onboard-vlm-native-shadow-20261007-r1.json`；说明：
  `docs/onboard-vlm-shadow-status.md`。

- 2026-10-07：沿r33主体新增8次观察的历史策略候选，统一训练／运行时的顺序、真实dt、
  origin reset和padding。搜索等待采样但不重复上传Mission。实际权重迁移和四输入ONNX导出
  退出0，真实CPU ORT的128组原始输出和12次连续带会话输入验证通过；仅为未训练扩展。
  正式入口尝试当前3-D staging因缺动作审核回执退出1，未创建优化器或候选目录。
  r33引用／COCO／候选晋升／Gazebo、Jetson、HIL/REAL、室外及发布门不变。
  同帧确认／记录和返航／报告改用新观察；成功Mission与任务回执后才重置跨任务历史，
  reset失败进入recovery。319项代码及工具、116项证据／文档／索引回归通过，源码parity
  重算通过；保留原始release/mode证据，不把retained audit当外部SIM重跑。
  回执：`docs/evidence/onboard-history-policy-development-20261007.json`。

- 2026-10-07：完成r4混合实体三维视觉微调：原train连续帧与近远课程合并为832/256/256，
  验证clip参与checkpoint选择、test不进optimizer；RTX 4060训练12轮继承708/708参数项，
  训练／导出及6个原生对照进程退出0。远静态33/5/16→24/2/25、旧静态27/8/24→28/5/23，
  远连续11/18/24→10/6/25、旧连续6/2/6→2/0/10；误报减少但召回和同ID恢复门失败，不晋升。
  新合并器修复pending封存缺口；观测审计403可读、303身份合格，urban r1 seed错配退出2。
  并行核验5个VLM及3个世界模型来源家族，补多模态/记忆/typed意图路线，不把研究当已推理。
  回执：`docs/evidence/onboard-visual-replay-increment-20261007-r4.json`；详情见`docs/research/`。

- 2026-10-07：完成r3远距离／低视角视觉增量：576张实体3-D图全量审计后，从r2继续
  30轮微调并导出raw ONNX。新静态TP/FP/FN 12/3/37→33/5/16；新连续1/0/34→11/18/24；
  旧静态总体命中不变但partial命中16→14、误报增加，旧失败连续0/0/12→6/2/6。新旧连续各2次ID切换，同ID恢复0，
  不晋升、不覆盖COCO/r33。另复用现有trajectory parser新增episode历史窗口，43项测试和
  独立审查通过；未接时序编码器或执行时序训练，不从遮挡计划造飞行动作标签。
  本轮是Blender离线开发，不是Gazebo/PX4飞行、Jetson、HIL/REAL、GNSS或最终发布。
  回执：`docs/evidence/onboard-far-visual-increment-20261007-r3.json`、
  `docs/evidence/onboard-sequence-input-development-20261007.json`。

- 2026-10-07：完成第二轮实体3-D像素增量训练：576张真实渲染图、384/96/96独立分区，
  从r1继承708/708参数，在RTX 4060完成30轮。同96张留出图TP/FP/FN从4/25/47改善为
  27/5/24；48帧连续渲染的test段仍漏掉全部12个可见帧，没有跟踪／重捕，r2不晋升。
  只读诊断指向远距离、低视角、小目标和边缘视野的联合覆盖缺口，阈值不降。新增初始训练／
  导出身份绑定、实际SDF几何重算及mask严格校验；239项相关回归通过。原COCO/r33不变，
  Blender离线训练不冒充Gazebo/PX4飞行、Jetson、HIL/REAL、GNSS或最终发布。回执：
  `docs/evidence/onboard-sdf-blender-curriculum-training-20261007-r2.json`。

- 2026-10-07：真正使用实体三维像素推进 OnboardAI 视觉训练。新增 SDF→Blender RGB／可见
  mask／YOLO 数据导出和封闭验 hash 的训练入口；120 张图按三套 world 分为 72/24/24，沿用
  YOLO26n 权重完成 RTX 4060/CUDA 20 轮微调，形成 `search_target` 辅助候选。传统 raw ONNX
  在现有推理接口的 24 张留出图上 TP/FP/FN=20/0/0，CPU 检测 p95=16.52 ms；42 项专项
  测试及独立审查通过。修复验证原地融合造成的导出失败，从同一 best.pt 重新加载导出而非重训。
  原 COCO 检测器和 r33 策略不变；单原型、简单静态背景和无完全遮挡不足以证明真实泛化。
  另记录本机 Docker 空间故障及集群 Docker profile 权限错误；未删镜像/卷、未重启 Docker。
  这是 Blender 离线开发证据，不是 Gazebo/PX4 飞行、Jetson、HIL/REAL 或最终发布通过。

- 2026-10-06：完成 OnboardAI r87 实时自主转移增量训练（从 r86 继续，保持同一 40/20 ONNX 契约），
  加入实时阶段转移、目标匹配后记录/返航、目标不匹配搜索、GPS/电量/障碍/推理故障保护和左右对称
  3-D 几何课程。RTX 4060/CUDA 共 572,000 行、32 epochs；独立 guarded=`22/22`、方向/距离=`22/22`，
  但 raw=`18/22` 相对 r86 回归，候选隔离、r33 reference 不变。没有真实 RGB/Depth/telemetry episode、
  reviewed trajectory 或外部 Gazebo/PX4 runtime，不代表 Jetson/Pixhawk/HIL/REAL、室外 GNSS 或最终 0.3.0。
  同时将 Docker stale-socket 预检改为动态提取错误路径，并新增精确路径可回滚恢复入口；Windows
  WinError 1920 仍阻塞外部运行时，未删除镜像、容器或卷。

- 2026-10-06：继续从当前 r33 reference 做 OnboardAI 几何增量微调，保持同一 40/20 ONNX 契约，
  使用仓库中性实体 SDF 3-D 场景、连续 bearing/range 几何课程和 RTX 4060/CUDA 训练。第二轮
  共 121,000 行、35 epochs；独立连续导航方向 MAE=`11.05°`、20° 内=`256/300`，但距离
  MAE=`126.998m`，仍未达到晋级门，候选隔离、r33 reference 不变。训练仍是结构化 3-D
  teacher，rendered RGB 行、reviewed trajectory 和独立 test split 均为 0；红车/绿车只保留
  历史适配器回归，不是 Gazebo 训练目标。没有声称外部 Gazebo、Jetson、Pixhawk、HIL/REAL、
  室外 GNSS 或最终 `0.3.0`。证据：`docs/evidence/onboard-policy-r33-range-focused-cuda-20261006.json`。

- 2026-10-06：真实 3-D OnboardAI 训练 gate 新增独立 `test` episode/transition 要求；只有
  train/evaluation 的轨迹会 fail-closed，防止把同一批 3-D episode 同时当训练和最终评估。
  新增 gate 回归通过，当前 candidate/reference/release 边界不变。

- 2026-10-06：完成 r73 几何加权 OnboardAI 候选微调，继续从 r33 父模型、40 维 SIM/Jetson 共用契约和
  实体 SDF 主线训练。独立语义 holdout=`7/7`，但结构化 3-D holdout task/flight/compatible=
  `0.8767/0.8789/0.9578`，几何 angle/range MAE=`68.91°/191.39 m`，候选拒绝，r33 reference 不变。
  同时真实 3-D 训练门新增 calibrated NED geometry rows 要求：缺少 CameraInfo、姿态或 heading 的
  RGB/Depth 帧不能进入 `training_ready` 微调；没有外部 Gazebo、Jetson、Pixhawk、HIL/REAL 或室外
  GNSS 证据。

- 2026-10-06：完成一次真实的 RTX 4060/CUDA OnboardAI r72 中性 3-D smoke 增量训练，继续使用
  当前 r33 的 40 维输入、任务/飞行动作头、实体 SDF manifest 和父模型蒸馏。独立语义 holdout
  为 `7/7`，但 900-case 结构化 3-D holdout 降到 task/flight/compatible=`0.8789/0.8833/0.9656`，
  泛化失败 `bearing-right`，几何诊断 angle/range MAE=`75.14°/151.32 m`，所以 r72 已拒绝、
  r33 reference 不变。没有 RGB/Depth episode、外部 Gazebo、Jetson、Pixhawk、HIL/REAL 或室外
  GNSS 证据；完整记录：`docs/evidence/onboard-policy-r72-neutral-3d-distilled-smoke-20261006.json`。

- 2026-10-06：新增真实 3-D OnboardAI 训练预检回执，扫描 122 份历史 RGB manifest，当前主线
  `capture_eligible_count=0`、`training_ready_count=0`。Docker daemon、Gazebo/PX4 CLI 不可用，
  所以预检没有启动外部仿真、没有写入训练集，也没有用红车/绿车示例替代中性实体 3-D 资产；回执：
  `docs/evidence/onboard-3d-training-preflight-20261006-r2.json`。

- 2026-10-06：继续在现有 OnboardAI 上增量训练 r33。修复匹配目标 east 回归固定为正值的标签
  偏置，补齐 TAKEOFF/SEARCH/INSPECT/MATCH/RECORD/RETURN/REPORT/HOLD/LAND 生命周期样本，并加入
  按目标距离与 bearing 计算的对称导航锚点；锚点按混合 group 切分，避免左右样本整组落在单一
  分区。r33 使用 RTX 4060、同一 40 维契约和仓库多旋翼/VTOL/固定翼 3-D Gazebo manifest，
  固定 22-case guarded=`22/22`、导航方向=`22/22`，900-case 结构化 3-D holdout
  task/flight=`0.976667/0.977778`。训练源仍无 reviewed RGB/Depth episode、test split=`0`，
  没有启动外部 Gazebo/PX4；候选未 promotion，不代表 Jetson/Pixhawk/HIL/REAL、室外 GNSS 或
  最终 `0.3.0`。证据：`docs/evidence/onboard-policy-r33-balanced-bearing-3d-20261006.json`。

- 2026-10-06：继续在现有 OnboardAI 上训练中性 3-D 课程候选。r70 因独立结构化 holdout 退化被拒绝；
  训练器新增可选父模型蒸馏约束，r71 从 r69 增量训练后独立任务 holdout=`7/7`、结构化 3-D
  holdout task/flight/compatible=`0.9944/0.9878/0.9933`。r71 仍未 promotion：没有真实 Gazebo
  RGB/Depth/CameraInfo/PX4 episode，匹配状态几何诊断未通过，当前 reference 仍为 r33。

- 2026-10-06：继续从 r65 增量训练现有 OnboardAI r66，保持同一 40 维输入、任务/飞行动作头、
  SIM/未来 Jetson 共用入口和独立安全边界。RTX 4060 训练 112,600 行、20 epochs，加入左右
  bearing 对称样本、有限 navigation sign loss 与 geometry loss；两个独立 structured holdout
  的 task=`0.995833/0.993333`、flight=`0.991667/0.990000`、compatible=`0.993333/0.993333`，
  22-case 泛化经 guarded、方向、距离检查均为 `22/22`。r66 仍未 promotion，r33 reference 不变。
  当前没有 reviewed Gazebo RGB/Depth episode，`rendered_rgb_depth_rows=0`；不代表 Gazebo/PX4、
  Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。证据：
  `docs/evidence/onboard-policy-r66-bearing-symmetry-20261006.json`。

- 2026-10-06：从当前 r33 父权重继续训练 OnboardAI r50，保持 40 维输入、任务/飞行动作头、
  SIM/Jetson 共用入口和安全边界不变。RTX 4060 训练 172,400 行、48 epochs；独立结构化
  holdout=`7/7`，通用 guarded=`22/22`、导航方向=`22/22`，900 行三类实体 3-D manifest
  结构化 sensor holdout task/flight/compatible=`0.9867/0.9856/0.9833`。r50 的 5 个通用
  案例仍依靠 compatibility/phase safety mask，且 `rendered_rgb_depth_rows=0`、没有真实
  Gazebo 连续 episode，因此候选未 promotion，r33 继续是 SIM 与未来 Jetson 的唯一 reference。
  证据：`docs/evidence/onboard-policy-r50-geometry-direction-candidate-20261006.json`，
  SHA-256=`22cbbe958eae3977dc633444423fa6fac6408c29497b8ae2501b9e60b168e3c9`。

- 2026-10-06：重新打包当前工作树并由 ClusterYourCodex 选择在线 Linux `nuc` 复核 3-D 采集
  前置条件。快照包含 879 个源码、配置、实体 SDF 和 r33 reference 文件；Docker CLI 能执行，
  但 `docker info` daemon probe 仍以退出码 `1` 失败，后续 stderr 捕获任务没有启动容器。
  本机 Docker Desktop 进程存在但 `desktop-linux` server pipe 不存在、WSL `docker-desktop`
  为 stopped；没有生成 RGB/Depth/PX4 telemetry，不宣称外部 Gazebo 通过。证据：
  `docs/evidence/onboard-3d-cluster-preflight-20261006-r2.json`，SHA-256=
  `6e9c0ddd2475a291f782636f39b078e33aeb0211e2e15af661a701894146a004`。

- 2026-10-06：按“先训练现有 OnboardAI、再做真实 3-D Gazebo”的主线修正训练教师。
  中性目标、INSPECT/FOLLOW、MATCH/FOLLOW 样本现在把距离特征、bearing 与 N/E 位移绑定到
  同一几何量纲；多角度 bearing 锚点使用独立 source group，避免方向重复泄漏。真实 RGB/Depth
  数据集构建器改为复用 CameraInfo/相机标定、同帧姿态、米制 Depth 和现有 `_target_rays`
  NED 变换，不再写死 north/east 标签。r46/r47 仍未 promotion，r33 reference 不变；当前
  仍无 training-ready 的真实 Gazebo RGB/Depth/telemetry 数据，最终发布/HIL/REAL/Jetson 边界不变。

- 2026-10-06：新增 r37 开发候选，继承 r33 的同一 40 维 OnboardAI 权重，以较低学习率和更强
  左右对称样本训练三类 3-D 模型/场景的结构化 sensor-contract 行。RTX 4060 完成 57,700 行、
  35 epochs，独立 3,000-case holdout task/flight/兼容输出=`0.979333/0.984333/0.993333`，
  generalization raw=`19/22`、guarded=`22/22`。r36 因 bearing-right 回归被拒绝，当前引用仍
  保留 r33。r37 无 rendered RGB/Depth 行、审核轨迹、pixel test 或外部 Gazebo 重跑；未 promotion，
  不代表 Jetson/Pixhawk、HIL/REAL、室外 GNSS、实飞或最终 0.3.0。证据：
  `docs/evidence/onboard-policy-r37-3d-model-teacher-20261006.json`。

- 2026-10-05：继续训练同一套 OnboardAI，新增 `--rendered-3d-dataset` 与 `--rendered-3d-repeat`，
  把多旋翼、VTOL、固定翼实体 Gazebo world 的真实 RGB/telemetry 帧接入 r17 增量微调。r22
  使用 RTX 4060 训练，3-D 结构化 holdout 为 task/flight=`0.984444/0.981111`，但仍是未
  promotion 候选；新增渲染数据集 `artifacts/training/onboard-rendered-3d-mainline-v1.json`。
  这些结果不是 Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布证据。

- 2026-10-05：收紧 OnboardAI 训练就绪门槛。训练回执、label manifest、raw recording manifest
  和五类 planner/Mission proof artifact 现在都必须在数据集目录内，并逐项用实际文件 SHA-256
  绑定；`../` 路径、目录外绝对路径、被篡改的 proof 文件都会 fail-closed。RGB payload 还必须
  与同一 raw replay frame 逐帧相等，避免把独立但不匹配的传感器文件拼成“连续 3-D 轨迹”。
  这只改善训练证据完整性，不把当前短时 3-D plumbing 数据升级为训练完成、Jetson/Pixhawk、
  HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。

- 2026-10-05：基于现有 r23 权重在本机 RTX 4060 做了一轮 r25 3-D RGB/Depth 增量候选训练，
  仍使用同一 40 维 OnboardAI 契约，没有另起飞控模型。候选模型 SHA-256 为
  `803554f474c78a7094021bd6e4f7b6f85292818ca7ce399e142bf7a83593dd05`；训练源是仓库现有
  `onboard-rendered-3d-depth-v2.json` 的 9 条 RGB/Depth plumbing 行，明确使用
  `allow_unreviewed_rendered=true`。训练集 task/flight 为 `0.997628/0.997570`，evaluation 为
  `0.873393/0.869512`，独立 test 行数为 `0`，所以候选保持 quarantined、未替换生产权重、未
  promotion。完整摘要见 `docs/evidence/onboard-policy-r25-3d-depth-gated-20261005.json`；
  这不是视觉泛化、Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。

- 2026-10-05：OnboardAI r18 继续从 r17 增量训练，并新增 `--gazebo-3d-manifest` 训练入口。
  入口读取仓库主线的实体多旋翼、VTOL、固定翼 world/载具和 RGB/Depth/PX4 telemetry/airspeed
  传感器契约，把不同 3-D 飞行工况转换成同一套 40 维 OnboardAI 结构化观测；不再把 billboard
  或红/绿示例当作模型身份。r18 在 RTX 4060 上使用 35100 条样本、独立 holdout `7/7`，22
  个泛化用例 guarded `22/22`。该结果仍是合成结构化训练，不是像素级 Gazebo 渲染、Jetson、
  Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布证据；摘要见
  `docs/evidence/onboard-policy-r18-gazebo3d-sensor-20261005.json`。

- 2026-10-05：新增 `gazebo_red_car_positive_control_001` 独立红车正控场景，并把真实
  PX4/Gazebo wire-v3 RGB 与 FLOAT32 Depth 接入同一 OnboardAI 感知链。实际候选 YOLO 模型
  检测到 `car`，共享颜色适配器判定 `red`，目标颜色匹配通过，Depth 测得约 `5.057 m`，
  RGB/Depth 配对、PX4 GPS telemetry 与 cleanup 通过。首帧因仍处于 `TAKEOFF` 而试图直接
  进入 `match_target`，共享状态机按设计拒绝跳过 `SEARCH` 并 fail-closed；因此没有伪造
  完整自动任务、Mission 提交、GPS 目标坐标、Jetson、HIL、REAL、室外 GNSS 或最终 `0.3.0`
  发布证据。摘要：`docs/evidence/external-red-car-actual-detector-20261005-r2.json`。

- 2026-10-05：为共享 `OnboardTaskStateMachine`、`OnboardMissionEventLoop` 和
  `OnboardRealtimeModelBridge` 增加 telemetry-gated `mark_takeoff_complete` seam。只有
  `relative_home`、合格 GPS、精度在门限内且实际相对高度达到门槛时，任务阶段才会从
  `TAKEOFF` 进入 `SEARCH`；重复调用幂等，计划文件和相机帧不能伪造该阶段。Gazebo、HIL
  和 Jetson/Pixhawk 后续共用这个接口。

- 2026-10-04：将实际 Gazebo wire-v3 RGB 帧接入共享 `OnboardFramePerceptionAdapter`。
  车辆正控场景中的两帧实际画面现在经仓库内 YOLO26n 候选检测、颜色判断和可选深度采样，
  再进入同一 `OnboardPerceptionObservation → OnboardRealtimeModelBridge → OnboardAI`
  路径；检测到车辆但颜色不是红色时保持 `search`，不会伪造红车命中。新增适配器单测、
  外部 detector 运行开关和证据摘要；颜色分类随后升级为优先使用高色度前景像素，并以
  `external-car-actual-detector-20261004-r3.json` 重新执行验证。候选模型仍为 quarantined，
  当前证据不代表红车命中、GPS 目标落图、Jetson、HIL、REAL、室外 GNSS 或最终 `0.3.0`。

- 2026-10-04：修正 PX4 `HOME_POSITION` wire 解码并接入 live bridge。正确处理 MAVLink 1 的
  52-byte core、MAVLink 2 的尾部裁剪和 `time_usec` extension；外部低电量 campaign 现在
  能用实际 PX4 HOME_POSITION 与观察位置计算返航向量。新增 `OnboardDecisionProvider` /
  `OnboardModelProposal`，Jetson/4060 可以对实时事件提出高层响应，固定层只做 GPS、围栏、
  返航向量和 Mission 边界的安全否决。当前低电量 RETURN 证据仍来自 deterministic
  simulator baseline，尚未接入高层模型 artifact，不宣称 Jetson、HIL、REAL、硬件、室外
  GNSS 或最终 `0.3.0`。

- 2026-10-03：新增共享 `OnboardAIExecutor` 编排层，把一次 `PromptEnvelope`、实时
  `ContextSnapshot`、`SharedTaskPolicy`、Planner 重验证和 PX4 Mission 编译绑定到同一条
  SIM/HIL/REAL 路径。目标移动/丢失/任务完成会重规划；GPS、电量、障碍和推理故障会
  fail-closed 输出 `RETURN/HOLD` 提案。新增正向与安全事件回归，并将模块纳入 core parity；
  本轮没有宣称 Jetson、HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布。

- 2026-10-04：完成 Prompt → OnboardAI → PX4/Gazebo 外部闭环。注册的 PromptEnvelope 通过
  `OnboardAIExecutor` 解析到批准的静态任务目录，和共享 planner、Mission hash、journal、
  upload/readback/commit/permit 共用一条路径；r5 两架次通过，r6 在同一容器采集 3 帧
  wire-v3 RGB/CameraInfo/NavSat/Odometry 与 3 帧 depth companion。新增独立审计会重建
  resolution 并核对 resolver/plan/executable/wire hash。当前仍不是任意自然语言任务生成，
  也不宣称实时突发事件重规划、QGC 人工视觉、Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终
  `0.3.0` 发布。证据索引：`docs/evidence/prompt-onboard-vtol-gazebo-20261004-r6.json`。

- 2026-10-03：QGC QML delegate 命中修正后的最新 external combined SIM 复跑成功。关闭残留
- 2026-10-03：完成 pinned PX4/Gazebo image sensor SIM r3。真实 disposable container
  采集并持久化 3 帧 1280×960 BGR24 RGB 与 3 帧 FLOAT32_M 深度，RGB/Depth digest、
  pair binding、GPS/odometry/camera-info provenance 和 cleanup 全部通过；新增首帧 PNG
  预览及 `docs/evidence/gazebo-image-sim-20261003-r3.json` 索引。该项是 machine-side
  Gazebo 图像/深度证据，不代表 QGC 人工视觉、detector、户外距离精度、HIL/REAL、硬件或
  最终 `0.3.0` 发布。

- 2026-10-03：完成 `gazebo_car_positive_control_001` RGB-only image SIM r2。采集 3 帧
  1280×960 BGR24，首帧通过当前隔离 COCO raw YOLO candidate smoke，输出 `car` confidence
  `0.806`；新增 `docs/evidence/gazebo-car-image-sim-20261003-r2.json` 和
  `docs/gazebo-scene-catalog.md`。该项不代表 detector accuracy、生产批准、QGC 人工视觉、
  户外距离精度、HIL/REAL、硬件或最终 `0.3.0` 发布。

- 2026-10-03：新增 opt-in Gazebo RGB/Depth companion persistence。`--sensor-depth-output-dir`
  要求 paired RGB 输出，在独立目录写入原始 `FLOAT32_M` payload 和 canonical
  `gazebo-depth-companion-set.manifest.json`，并重构校验 `MDGZDEP1` digest、RGB wire
  digest、时间戳、几何、源 topic、calibration SHA 和 pair binding。现有 RGB frame-set v2
  与 VTOL exact-directory contract 保持不变；该项只代表 software-SIM sensor provenance，
  不宣称户外标定、物理距离精度、HIL/REAL、硬件或最终 `0.3.0` 发布。

- 2026-10-03：使用正确的 scenario index 完成 fresh pinned PX4/Gazebo depth companion
  外部 SIM r2；MAVLink/GPS telemetry、3 帧 RGB/Depth synchronized transport、原始
  `FLOAT32_M` payload persistence、`MDGZDEP1` digest 重验、pair binding、canonical
  manifest 和容器 cleanup 全部通过。证据索引为
  `docs/evidence/depth-companion-sim-20261003-r2.json`；仍不宣称 metric-depth model
  calibration、户外距离精度、HIL、REAL、硬件或最终 `0.3.0` release。

- 2026-10-03：新增 Depth Anything offline replay diagnostic。对三帧已封存的 RGB/Depth
  companion 重新验签，并严格复用 `MetricDepthEstimator` 的 `INTER_CUBIC`、ImageNet
  normalization、NCHW 与模型原生 dense output grid 后使用 pinned outdoor-small ONNX CPU
  推理；raw RMSE=`2.49894642829895 m`。
  该结果仅用于暴露未校准尺度差异，不生成现场 calibration、不批准生产模型，也不宣称户外
  距离精度、HIL、REAL、硬件或最终 `0.3.0` release。摘要为
  `docs/evidence/depth-model-sim-replay-20261003-r1.json`。

  Windows `TabTip` 遮挡后，当前 `0.3.0-pre.8` Release EXE（SHA-256=
  `6f0403cfb29ec6b13d0b5b684a1787327c85db6d91bc5b4662fde867581af37f`）与当前 parity
  （SHA-256=`cb45e34c59463346d56991d489a7d0e838d7841a50f71887c73f2b5fd837892f`）绑定的
  pinned PX4/Gazebo/QGC 组合运行通过：900 帧 integrated SIM、native video surface、
  toolbar machine smoke、精确 PID/HWND `SELECT_TRK`、SelectionAck/TrackStatus adoption、
  VTOL Mission transaction、420.016 秒 router 窗口和 disposable container cleanup 均通过。
  源证据为 `artifacts/evaluation/combined-sim-qgc-20261003T021801Z-40300/combined-evidence.json`
  （3,716,766 bytes，SHA-256=`5f3c9f281787d567480729d25e4731c34f9753826c5c756b9a5bcff6a52d2925`），
  简明索引为 `docs/evidence/combined-sim-qgc-20261003-r4.json`。该结果仍只代表软件
  external SIM/machine smoke；不宣称人工视觉、HIL、REAL、真实硬件、室外 GNSS 或最终
  `0.3.0` 发布，发布门仍有 32 个 blocker。

- 2026-10-03：在完整 MSVC x64 环境中重新构建 `0.3.0-pre.8` QGC Release，纳入本轮
  QML 实际渲染 delegate 命中修正；生成的 Release/portable EXE SHA-256 均为
  `6f0403cfb29ec6b13d0b5b684a1787327c85db6d91bc5b4662fde867581af37f`。该记录只代表构建与
  deploy 成功，不代表 QGC 人工视觉/Mission、HIL/REAL、硬件、室外 GNSS 或最终 `0.3.0` 发布。

- 2026-10-03：完成 fresh pinned PX4/Gazebo/QGC 组合复跑（900 帧传感器尾段）。
  精确 PID/HWND 的 native `SELECT_TRK` 已完成 SelectionAck、TrackStatus 与 operator-tracked
  adoption；VTOL Mission upload/readback/commit/permit、router 收尾和 disposable container
  cleanup 全部通过。完整源证据为
  `artifacts/evaluation/combined-sim-qgc-20261003T013646Z-15924/combined-evidence.json`，
  简明索引为 `docs/evidence/combined-sim-qgc-20261003-r3.json`。QGC 仍仅计 machine smoke，
  不宣称人工视觉、HIL、REAL、硬件、室外 GNSS 或最终 `0.3.0` 发布。

- 2026-10-03：depthguard-r1 修正深度标定契约与 QGC 原生目标命中路径。Gazebo
  `optical_axis_z_m` wire identity 不再可作为 Depth Anything metric-depth calibration；
  loader、PowerShell 组合入口和回归测试均 fail-closed。QGC `SELECT_TRK` 现在优先根据
  实际渲染 target delegate 几何命中，避免 letterbox/PIP 坐标漂移造成首次点击
  `stable_hit=false`、根本没有发送 SELECT 的假“通信失败”。当前 parity SHA-256 为
  `0dd9394b477260852ebd4828a20df2f0760f6c1cbbfa4423972d4223a70ad027`，核心 hash 为
  `de997b7833d70bc7f424c210e4520ebe53d33c0aec20c9b039fb324e3bc1db41`；随后发现并修正
  fixed-wing/multirotor 深度 evidence 与当前 Gazebo SDF 资产的 hash 漂移，生成
  `depthguard-r2-model-assets` retained-file rebind；仅完成代码与证据重绑定，32 个
  release blocker、QGC 视觉/Mission、HIL/REAL、硬件、
  室外 GNSS 与最终 `0.3.0` 门继续 fail-closed。

- 2026-10-03：在 pinned `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8` disposable 容器中完成
  一次新的 external-SIM sensor transport run：实际收到 PX4 heartbeat/GPS_RAW_INT（fix=3、
  10 satellites），采集 3 帧 camera/camera-info/NavSat/full odometry/covariance wire-v3，
  3/3 原始 BGR24 payload 与 wire payload SHA 一致，容器 stop/remove cleanup 通过。证据为
  `artifacts/evaluation/continuation-docker-sensor-20261003.json`（SHA-256=
  `6f7ceec973fe260276079253644b69d11325f15354974b6336e3ba79f536be48`），帧 manifest 和
  container log 也已写入 current-progress 索引。该运行是被动 telemetry/sensor 观察，没有
  Mission/ARM/actuator 指令、SharedTaskPolicy task binding、QGC 人工视觉、HIL/REAL、硬件、
  室外 GNSS 或最终 `0.3.0` release claim；`0.3.0-pre.8` 与 32 个 fail-closed blocker 保持不变。

- 2026-10-03：将当前 `0.3.0-pre.8` release-gate 重新绑定到最新
  `configs/release-parity.json`。当前 report/manifest 为
  `artifacts/evaluation/release-gate-pre.8-current-20261002T233000Z-v6-bound-report.json` /
  `artifacts/evaluation/release-gate-pre.8-current-20261002T233000Z-v6-bound.json`，audit 为
  `docs/evidence/shared-policy-release-parity-policy-helper-20261003-current-r4.json`。
  replay、sim、hil、real 四个 mode 的 parity mismatch 已归零；external-SIM join 与 PX4
  response matrix/campaign 是从不可变源重算的 source-bound 派生物，不代表旧 runtime 重新执行。
  历史 field-link 使用 `field-link-preflight-current-20260927-r2.json`，独立 current probe 使用
  `field-link-preflight-current-20260928-r2.json`，两者均保持 `hardware_link_ready=false`。
  audit 仍有 32 个 fail-closed blocker；replay accuracy/domain、QGC visual/Mission、HIL/REAL、
  硬件、室外 GNSS 和最终 `0.3.0` 发布均未通过。

- 2026-10-03：修复 r14 之后的 source-bound parity 收口。`run_vtol_mission_sensor_capture.py` 纳入
  `px4_mission_path` parity source；修正 current parity receipt 重绑定必须携带
  `scope=receipt_generation_file_binding`，并为 retained-file audit 明确写入
  `evidence_scope=retained_files_audit`、`runtime_source_parity_verified=false`、
  `external_sim_rerun_performed=false`。保留旧副本后重新绑定 observed session、训练 artifact、
  campaign receipts、AI-flight retained layers 和三机型矩阵；深度证据恢复到当前模型 provenance
  的 r2 文件。`verify_current_progress_index.py --check-sources` 与 648 项定向回归通过。
  当前 parity 为 `0c1676f032ae069a7c168d9a512f91787bb892b8859f8a19da688f1c5c402463`，r14 外部
  PX4/Gazebo sensor campaign 仍是最新 fresh external SIM；32 blockers、HIL/REAL、硬件、室外
  GNSS、QGC 人工视觉和最终 `0.3.0` 发布门继续 fail-closed。

- 2026-10-03：在当前源码 parity 下重新运行 SharedTaskPolicy VTOL 组合 campaign（r9），修复并验证
  首架次 task-level sensor binding：`synthetic-east` 的 binding descriptor、transaction id、
  plan/executable Mission/wire hash 与 ready/complete marker、同一容器的 3 帧 Gazebo wire-v3
  capture 及最终 cleanup 全部一致。证据为
  `artifacts/evaluation/continuation-shared-policy-vtol-sensor-20261003-r9.json`（文件 SHA-256=
  `1b65532a32d3a672f0ed570fd04c7abd02660cdf4d43b71d7c6acbd447713d00`），当前 parity 文件 SHA-256=
  `203fb5cdb25d3c49af097d0d0b2599b036a59c0f03dc907de14378ccee890190`。r9 仍仅是 headless
  PX4/Gazebo 软件 SIM；不宣称 QGC 人工视觉、HIL/REAL、真实硬件、室外 GNSS 或最终 `0.3.0` 发布。

- 2026-10-03：新增显式 `--capture-campaign-sensor-frames` 组合入口：第一条 SharedTaskPolicy
  campaign sortie 沿用既有 live MAVLink fixed-wing barrier，外层对同一 exact Docker container
  采集 3 帧 Gazebo wire-v3 Image/CameraInfo/NavSat/Odometry，写入既有 completion marker 后再
  继续第二条任务、降落和 cleanup。新增 evidence 为
  `artifacts/evaluation/continuation-shared-policy-vtol-sensor-20261003-r2.json`，在重新生成的
  current parity（SHA-256=`b870011d3e72b66d9b44372a697341410afbc1a84b07c4eecc12538108f3c543`）下，
  gate、parity 起止快照、Mission journal/hash-chain、像素持久化和 exact-ID cleanup 均通过；明确不做
  task-level frame mapping（`task_binding_claimed=false`），不宣称 QGC 视觉、HIL/REAL、硬件、
  室外 GNSS 或最终 `0.3.0` release。组合入口默认保持 `reference_profile=lifecycle-smoke`；
  `fixed-wing-response` 仍只用于单 Mission path。新增 combined branch mocked 回归覆盖 barrier、
  pixel persistence tamper 和 final-integrity fail-closed。

- 2026-10-03：新增共享 `TaskPolicyRuntimeBinding` 只读契约，并将其接入现有 authenticated
  operator-link/bridge/QGC 只读状态链：observed session、确定性 SIM 和 VTOL
  campaign 现在统一记录 policy/ranker/profile/planner/Mission hash；abstain 不绑定 Mission，
  binding 不能启动 Mission、应用 actuator 或读取 truth。lint 修正后重新生成 canonical parity，
  并以 `task-policy-runtime-r5` 重建 source-bound 派生索引；当前 parity 文件 SHA-256 为
  `bb7aacb2c0e00772286f6663480e7b88474e8f58200329cab0d61427e346a557`，release-gate report/audit
  绑定到 `20261003T011500Z` 与 `shared-policy-release-parity-drift-20261003-task-policy-runtime-r7.json`。
  使用 VS2022 x64 Developer 环境完成增量 Release 构建，`MultiDetectGCS.exe` SHA-256 为
  `7cef06c6c459a7b70582aabba6940cf156f33bba8cd29b5e05f3a4ae7a334b22`；同时运行 C++ protocol
  self-test（type 27 policy-runtime golden vector，exit 0）；只记录构建/协议自测，未启动 EXE。
  当前仍为 `0.3.0-pre.8`，32 blockers、HIL/REAL/硬件/室外 GNSS/最终 release 门保持 fail-closed。

- 2026-10-02：完整回归发现上一版 release-gate report 把 current field-link r3 同时绑定成 historical
  field-link，违反当前/历史证据身份分离。保留历史文件不覆盖，改用 2026-09-28 r2 作为 historical
  输入、2026-10-02 r3 作为 current probe，重新生成 13:00 report/manifest 与 audit r8；历史报告
  的 stale blocker 被如实保留，当前仍 32 blockers、`gate_passed=false`、`release_allowed=false`，
  未升级为 HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布证据。audit generator 同步增加了同文件/同
  SHA 的 current/historical 拒绝规则和回归测试。

- 2026-10-02：重新执行只读 field-link 预检 r3：3/3 样本中 Jetson SSH、GR01 MAVLink、相机 RTSP
  均超时，本机 UDP 绑定成功但实际 `192.168.1.13/24` 不符合 `192.168.144.0/24`，因此
  `hardware_link_ready=false`，没有把预检升级成硬件/HIL/REAL 或室外 GNSS 证据。当前
  release-gate report/manifest 绑定到 12:00 快照，audit 更新为 r7，freshness 阻塞已清除但仍有
  31 blockers、`release_allowed=false`。同时修复 audit generator 对 raw `verified` 与 projected
  `gate_passed` interface-diagnostic 语义的兼容处理，并保留严格 path/SHA/bytes 交叉校验。

- 2026-10-02：修复后续 source-bound 复核发现旧 r27 matrix/campaign 的 acceptance runner hash 已随
  PX4 启动 race 修复变化；保留 r27 为历史并从相同 retained source 生成 r28，matrix/campaign
  verifier 均通过。三机型 external join/revalidation 与 release-gate 同步到 r28/r6；当前 report
  仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，未把 source 重算夸大为新增运行，
  也未改变 HIL/REAL、硬件、室外 GNSS 或最终 `0.3.0` 发布状态。

- 2026-10-02：修复 disposable PX4/Gazebo AI flight acceptance 的启动 race：`px4-param` preflight
  仅在 `server not running` 时进行 20 次、0.5 秒间隔的 bounded retry，其他错误仍立即 fail-closed；
  新增对应回归测试。修复后 fresh r6 external SIM 在当前 parity 下通过 armed、truth-free TRACK、
  `47/47` target refresh、`1.981104m` `GLOBAL_POSITION_INT` response 与 cleanup；r8 composite
  receipt 证明同一 runtime parity，但仍不构成 HIL/REAL、硬件、室外 GNSS、视觉精度或最终 release 证据。

- 2026-10-02：重新生成当前 `0.3.0-pre.8` release parity 后，fresh SharedTaskPolicy VTOL
  Mission campaign r3 与 AI flight r4 都在运行起止点锁定同一 parity；Mission upload/readback/
  commit/permit、planner revalidation、PX4 flight-response、cleanup 和 snapshot 稳定性均通过。
  composite receipt `artifacts/evaluation/simulator-ai-flight-parity-20261002-gnss-display-r5.receipt.json`
  明确记录 `same_release_parity=true`、`current_runtime_parity_proven=true`。同步生成了
  current-progress 的 source-bound 副本，并由 `verify_current_progress_index.py --check-sources`
  完整复核；这些仍是外部软件 SIM 证据，不是 HIL/REAL、真实硬件、室外 GNSS、视觉精度或最终
  `0.3.0` 发布证据，`release_allowed=false` 保持不变。

- 2026-10-02：SharedTaskPolicy Mission campaign 与 AI flight 完成同一当前 runtime parity
  join。新的两架次外部 PX4/Gazebo Mission campaign 在起止点锁定
  `configs/release-parity.json`，Mission upload/readback/commit/permit、planner revalidation、
  cleanup 和快照稳定性全部通过；AI r3 也有同样锁定。新的 composite receipt
  `artifacts/evaluation/simulator-ai-flight-parity-20261002-gnss-display-r4.receipt.json`
  明确记录 `same_release_parity=true`、`current_runtime_parity_proven=true`。这只证明
  `0.3.0-pre.8` 外部软件 SIM 层的同版 parity，仍不宣称 HIL/REAL、真实硬件、室外 GNSS、
  视觉精度、candidate 晋升或最终 `0.3.0` 发布。

- 2026-10-02：AI external PX4/Gazebo acceptance 增加运行时 parity 起止锁定。r3 在同一
  `river_rescue_vtol_001` disposable VTOL 中，运行开始/结束均记录 `release-parity.json`，文件
  SHA-256=`abba65fff6ef77e38d292b95ce523219a15c9de9ead7a218fa10c8f2235a7d7d`，五项契约 hash
  一致；armed、truth-free TRACK、目标刷新 `45/45`、flight response 和 cleanup 全部通过。
  新 join 工具会审计该 snapshot，但 composite 仍保持 `same_release_parity=false`，因为
  SharedTaskPolicy Mission 历史运行还没有相同的起止锁定；未宣称 HIL/REAL、硬件、室外 GNSS
  或最终 `0.3.0` 发布。

- 2026-10-04：新增独立的 `MissionReplacementTransaction` 与
  `mission-replacement-receipt.schema.json`，把飞行中换 Mission 拆成父事务绑定的
  pause → upload → readback → commit → cursor → resume 状态机。每个外部写动作先落
  intent journal，恢复只报告人工动作，不自动重放；来源、Mission hash、游标和
  `AUTO.MISSION` 证据必须一致，危险 Mission item 与不确定 fallback 一律 fail-closed。
  9 项事务/schema 回归及全仓库 5342 项测试通过；这仍是 transport-neutral 软件边界，
  未宣称 PX4/Gazebo 飞行中换 Mission、HIL、REAL、Jetson、户外 GNSS 或最终 `0.3.0`。

- 2026-10-04：重新运行固定 digest 的 disposable PX4/Gazebo AI flight campaign。第一次
  运行因 arm 时序只收到 `MAV_RESULT_TEMPORARILY_REJECTED` 而保留为失败证据；第二次在
  更长启动等待下通过 arm、truth-free target、488 个 AI steps 和 5 秒控制响应，实际
  `GLOBAL_POSITION_INT` 最大水平位移为 1.302856 m（门槛 0.5 m），150 个位置样本，
  容器已清理。摘要为 `docs/evidence/simulator-ai-flight-20261004-r2.json`；该结果仍
  只证明外部软件 SITL/Gazebo AI flight response，不证明 HIL、REAL、Jetson、Pixhawk、
  户外 GNSS、视觉精度或最终 `0.3.0`。

- 2026-10-04：新增一次外部 PX4/Gazebo `target_refresh_dropout` 负向 campaign。arm、
  truth-free target 和 AI bridge 正常运行，随后真实观察到目标刷新故障并确认
  `fail_closed=true`；容器 cleanup 通过。该命令以非零码结束是预期的，因为注入故障后
  正常 flight gate 必须拒绝。摘要为 `docs/evidence/simulator-ai-target-dropout-20261004.json`，
  它是安全负向路径证据，不是正常 flight 通过、HIL、REAL、Jetson、户外 GNSS 或最终发布证据。

- 2026-10-04：新增一次外部 PX4/Gazebo `heartbeat_dropout` 负向 campaign。arm、target
  和 AI bridge 在故障前正常运行，心跳丢失被观察到，控制 authority 最终进入 `failsafe`，
  `fail_closed=true`，容器 cleanup 通过。正常 flight gate 按设计拒绝，非零退出码是预期
  行为。摘要为 `docs/evidence/simulator-ai-heartbeat-dropout-20261004.json`；它不代表
  正常 flight 通过、HIL、REAL、Jetson、户外 GNSS 或最终发布证据。

- 2026-10-02：新增 AI flight 与 SharedTaskPolicy 的 retained-layers parity join。旧的
  SharedPolicy receipt（绑定 `84a…` parity）先被严格拒绝；改用当前 parity 绑定的 VTOL receipt
  后，scenario manifest、VTOL airframe、planner revalidation、truth-free 与 cleanup 审计通过。
  摘要为 `docs/evidence/simulator-ai-flight-parity-20261002-gnss-display-r2.json`，但明确
  `same_release_parity=false`、`current_runtime_parity_proven=false`：历史运行没有在起止点保存
  parity snapshot，不能伪装成同一版 runtime 执行。下一步是给两条运行路径增加起止 parity 锁定，
  不改变 HIL/REAL、硬件、室外 GNSS、candidate 晋升和最终 `0.3.0` 发布边界。

- 2026-10-02：在固定 digest 的 disposable PX4/Gazebo VTOL external SIM 中重跑当前共享
  AI flight-response path。上一轮只请求 arm/target，未开启 MANUAL_CONTROL 输入，PX4 连续
  `TEMPORARILY_REJECTED`；本轮使用既有 `--exercise-flight-seconds 5` 路径，使容器内
  `COM_RC_IN_MODE=1` 生效。结果为 runtime/preflight/GPS/sensor transport/mission transaction/
  AI target policy/flight response 全部通过；armed 已观察到，truth-free target `TRACK`
  接受，目标刷新 `47/47`、最大间隔 `0.125 s`，容器 stop/remove 均成功。原始 evidence SHA-256=
  `4a27af6b7178a685dd479f9035579f713d306d1dc6817905bedb206e1e950d52`，tracked 摘要为
  `docs/evidence/simulator-docker-ai-flight-20261002-gnss-display-r2.json`。该结果仅打开
  软件 external SIM flight-response 子门；本轮未请求 Mission upload/commit，不宣称 HIL/REAL、真实硬件、室外 GNSS、视觉精度、candidate 晋升或
  最终 `0.3.0` 发布，`release_allowed=false` 保持不变。

- 2026-10-02：对当前 `0.3.0-pre.8` Release EXE 做原生窗口启动观察：离线参数下
  `MultiDetectGCS` 窗口创建并响应，随后关闭请求正常退出。证据为
  `docs/evidence/qgc-runtime-window-smoke-20261002-r1.json`。由于该观察不足以证明严格
  simple-boot exit contract 或人工 QGC 视觉验收，相关标志仍为 false；未连接飞控，未新增
  PX4/Gazebo、HIL/REAL、硬件或室外 GNSS 证据。

- 2026-10-02：使用 Visual Studio 2022 BuildTools Developer 环境完成 QGC
  `0.3.0-pre.8` Release 增量构建（Ninja，`--parallel 2`）。最新 EXE 为
  `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe`，大小 `46877184` bytes，
  SHA-256=`cddb0f08d5707b56139a1d69f535bef10607beae7e6229152990649c7470879f`，
  File/Product version=`0.3.0.0`。tracked evidence 为
  `docs/evidence/qgc-build-20261002-gnss-display-r1.json`；本轮没有启动 GUI，
  没有新增 PX4/Gazebo、HIL/REAL、硬件或室外 GNSS 验收，`release_allowed=false` 保持不变。

- 2026-10-01：完成当前 source-bound progress index 收口。按当前 checkout 重新生成
  `configs/release-parity.json`（文件 SHA-256=`84a240857821db3f97876578b53da24879c57196980eea19a566288dac83baa2`，
  `core_algorithm_hash=afa87110e5a638c4003b0b9760520a29709ae01a340a61112d9b4fbd6af51dfd`），并将
  fixed-wing/multirotor depth、VTOL receipt、三机型 matrix、observed/runtime index、PX4 response
  与 external join 索引切到当前 parity。传感器 manifest 现在要求与
  `ScenarioManifestIndex.canonical_hash` 完全一致；错误场景 fail-closed。该轮是静态 source-bound
  重绑定，不是新的 PX4/Gazebo 运行；`0.3.0-pre.8`、`gate_passed=false`、`release_allowed=false`
  以及 HIL/REAL、室外 GNSS、硬件和最终 `0.3.0` 发布边界均保持不变。

- 2026-10-02：新增 advisor-only 有界序列提案 `SequenceProposal` 与
  `scripts/propose_task_sequence.py`。它复用当前 `SharedTaskPolicy`、既有预算 episode、
  planner/Mission compiler 和 hash-bound airframe identity，对 baseline 与 bounded exact
  reference 做可审计比较；最多6个任务，节点/runtime/airframe/GPS不合格时 fail-closed。
  提案不上传或启动 Mission，不进入 arm/disarm/actuator，`deployed=false`、
  `candidate_promoted=false`、`final_release_allowed=false` 保持不变。新增 4 项序列提案
  测试与 CLI smoke 验证；当前 case 奖励差为0，未宣称模型收益、HIL/REAL、室外 GNSS、硬件
  或最终 `0.3.0` 发布。

- 2026-10-02：强化有界序列提案 evaluator 的证据绑定。新入口必须显式提供训练/评估 dataset
  SHA，并重新校验完整训练输入、评估输入、声明 family、ranker、policy artifact 以及 15 个
  evaluator/core source 文件；报告只声称 known-input/family separation，不把它升级为 semantic
  或 physical independence。r2 报告 `artifacts/evaluation/sequence-proposal-independent-holdout-20261002-r2.json`
  （SHA-256=`c4f7ff9454d125219328c047b78e031017856887363e767a21e88cf0d3046b68`）仍为 12 cases、
  baseline/reference `120/120`、改进 `0`、回退 `0`、不变 `12`；advisor 不晋升、不部署、不进入
  PX4 Mission 或 actuator 路径。

- 2026-10-02：修复有界序列提案 evaluator 的目标审计与文件快照边界。`SequenceProposal` 的
  `planner_revalidated`、部署/晋升/发布状态改为由 exact reference 与已编译边数派生，不能由
  调用方直接注入；每条记录现在保存 objective ID、完成数量、`reward → count → -time` 元组和
  `improved/regressed/tie` 关系。批量报告改用首次绑定读取的 ranker/artifact SHA，不再二次读取
  造成 TOCTOU；`independent_holdout_evaluation` 与 `objective_matched` 由实际校验派生。r3 报告
  `artifacts/evaluation/sequence-proposal-independent-holdout-20261002-r3.json`（SHA-256=
  `1f8eec6209507707c7d9cd15a988e85f0b97c4dff1ca150a3b8bff99d5667736`）仍为 12-case、reward
  `120/120`、完整 objective tie `12`，不改变不晋升、不部署、HIL/REAL、室外 GNSS、硬件和最终
  `0.3.0` 发布边界。

- 2026-10-02：在固定 digest 的 PX4/Gazebo external SIM 中实际执行 shared-policy VTOL
  campaign。两个任务 sortie 均复用现有 Mission upload/readback/commit/permit、planner revalidation、
  policy evidence、journal/hash-chain 与 exact cleanup；artifact canonical SHA-256 为
  `de39b730fd2acf3dc6f2fc75cfe201b576e7dd74d1e13834a9bcbc56aeea34d7`，外层证据
  `artifacts/evaluation/shared-policy-vtol-campaign-20261002-r2.json` 的 SHA-256 为
  `412608cd752206699a1f18817447d1575b0ec8f5e5eb4396e5d8b5231e1eaabc`。这只证明 external
  SIM 路径可执行，不是 candidate 收益、QGC 视觉、HIL/REAL、室外 GNSS、硬件或最终发布证据。

- 2026-10-02：为上述 shared-policy VTOL campaign 生成 retained-files receipt
  `artifacts/evaluation/shared-policy-vtol-campaign-20261002-r2.receipt.json`。文件 SHA-256 为
  `c6c89e06c0307a566222a01154f4c0e55fe89f39316b1c6f03d2440a8e6c9f9b`，canonical
  `receipt_sha256` 为 `a8803481fc93261151a77d4eefe4e7a746d815603297dad1b38be53845d2a26d`；
  receipt 绑定 Mission report/journal、policy evidence、runtime identity、容器清理和当前
  parity。它是只读文件审计（`external_sim_rerun_performed=false`），不新增运行，不证明
  candidate 收益，不打开 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

- 2026-10-02：明确 advisor 的 airframe identity 边界：外部 campaign receipt 已绑定
  `airframe_profile_id`、profile document 与 `airframe_profile_sha256`，但通用
  `ContextSnapshot.airframe_profile_id` 仍是文本字段，尚未携带可重算 profile document/hash。
  在补齐该契约前，advisor 保持只读诊断，不进入部署、PX4 Mission 或执行授权。

- 2026-10-02：为 `SequenceProposal` 增加 exhaustive-reference fail-closed invariant，并加入
  reward 相同但完成数量更高的 objective 回归（`budget-validation-1`）；重新生成严格 source-bound
  holdout 报告 r4（SHA-256=`fd197a0db653573342d93af4ed7014e8c5c89ff3ff2d76857d8f5740a1752f89`）。
  12-case 结果仍为 reward `120/120`、完整 objective tie `12`；r2/r3 报告保持不可变，发布、部署、
  HIL/REAL、室外 GNSS 与 hardware 边界不变。

- 2026-10-01：在当前 parity 下实际执行 fixed-wing 与 multirotor 各一架 disposable
  PX4/Gazebo external Mission campaign（r30）。两架均加载同一 SharedTaskPolicy/ranker，
  通过同一 Mission upload/readback/commit/permit、planner revalidation、2 帧 depth companion、
  same-container sensor binding、journal transaction 与 exact cleanup；两份 evidence 的
  `gate_passed=true`、`flight_started=false`、`physical_output_enabled=false`。随后由两份新
  evidence 与保留的 VTOL receipt 生成三机型 source-bound matrix r31。该结果仍是软件 external
  SIM，不打开 QGC 人工视觉、HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布门。

- 2026-10-01：将 SharedTaskPolicy identity handoff 从 r23 source receipt 迁移到当前 r26 receipt，
  生成 r2 handoff 并重新运行 release-gate wrapper。handoff 仍为 `software_identity_fixture`，
  `hil_acceptance=false`、`real_acceptance=false`、`hardware_acceptance=false`、`flight_acceptance=false`；
  release-gate 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers。r1/r5 及旧报告保留为历史。

- 2026-10-01：在当前 parity 下重新执行独立 VTOL external PX4/Gazebo SharedTaskPolicy campaign
  r26。`synthetic-east` 与 `synthetic-west` 均完成，Mission transaction、planner revalidation、
  journal/hash-chain、受限 evaluator、loopback isolation 和 exact-ID cleanup 通过；新增 evidence 与
  receipt 均未覆盖 r23。该结果仍是软件 external SIM，不打开 HIL/REAL、室外 GNSS、真实硬件或最终
  `0.3.0` 发布门。

- 2026-10-01：使用 r26 VTOL receipt 重新生成三机型 external Mission matrix 与 shared-policy
  vehicle matrix；旧 r24 矩阵保留为历史。fixed-wing/multirotor 仍使用当前 r21 source，所有矩阵
  继续保持 `flight_started=false`、`physical_output_enabled=false` 和 `final_release_allowed=false`。

- 2026-10-01：当前 release-gate report/manifest 更新到
  `release-gate-pre.8-current-20261001T130000Z-v6-bound-report.json` /
  `release-gate-pre.8-current-20261001T130000Z-v6-bound.json`，并由
  `docs/evidence/shared-policy-release-parity-source-transition-20261001-r5.json` 作为当前 audit 索引。
  新增的 handoff 仅是 software identity fixture；`gate_passed=false`、`release_allowed=false`、
  HIL/REAL、硬件、室外 GNSS 和最终 `0.3.0` 发布门继续关闭。

- 2026-10-01：修复当前 release-gate 默认输入的 parity 漂移。发现旧的“current-parity”文件内部仍
  绑定 `cf5d…/core=9413…`，现从同一批不可变 external runtime/AI 与 PX4 response source 重新计算
  r25 join、独立 revalidation、response matrix/campaign，并将 wrapper 默认值切换到这些新文件。
  新 report 为 `release-gate-pre.8-current-20261001T124000Z-v6-bound-report.json`，软件
  `current_release_parity_bound=true`；没有启动新 Docker/PX4，也没有升级 HIL/REAL、室外 GNSS、
  真实硬件或最终 `0.3.0` 发布门。

- 2026-10-01：将当前 release-gate 索引从历史 r3 审计切换到
  `docs/evidence/shared-policy-release-parity-source-transition-20261001-r4.json`。r4 仅重新绑定当前
  report/parity 的 source-bound 身份，保留 r3 不变；`gate_passed=false`、`release_allowed=false`，
  不代表新增 PX4/Gazebo、HIL/REAL、室外 GNSS 或真实硬件证据。

- 2026-10-01：生成并绑定 `SharedTaskPolicy` 的 SIM→HIL/REAL identity handoff r1。handoff
  绑定当前 VTOL campaign receipt、policy artifact、五项 release parity 和 airframe；verifier 通过，
  但明确 `software_identity_fixture`，所有 HIL/REAL、hardware、flight、operator visual 和最终 release
  claims 保持 false。当前 report/manifest 更新到 `20261001T130000Z`，r5 audit 保留 source-bound 记录，
  `gate_passed=false`、`release_allowed=false` 不变。

- 2026-10-01：完成全量 Python 回归，结果为 `5230 passed, 7 skipped`。为保持历史证据不可变，
  pair receipt 测试改为在临时仓库中绑定当前 parity；集成 SIM 测试补齐 runtime 声明的 RGB/depth
  伴随帧；MAVLink 传感器端口测试改用实际 PX4 telemetry 传感器。定向验证与全量回归均通过，
  仍不宣称 HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布。

- 2026-10-01：完成 v0.3.0-pre.8 当前证据收口。当前 `release-parity.json` 文件 SHA-256 为
  `5f5796d8bc784c24ea7e6fe98a4675e1842472df375ef6d524e44c9f4b7410e0`，core hash 为
  `0e986588934fe6433800833a4f03db92bf828e912025434163685ff842bff73e`。fixed-wing、multirotor
  的 depth Mission 为 r21，VTOL 当前连续双任务 external campaign 为 r23，三机型 Mission/vehicle
  matrix、observed session 和 runtime matrix 均更新到 r24；运行均通过 Mission transaction、planner
  revalidation、传感器质量门与 disposable cleanup。它们仍只属于软件 external PX4/Gazebo SIM，
  不打开 HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布。

- 2026-10-01：release-gate historical/current field-link 绑定完成一次可审计的静态重绑。新增
  `docs/evidence/shared-policy-release-parity-source-transition-20261001-r3.json`，保留历史
  2026-09-28 报告，同时绑定当前只读 probe；没有重写历史 runtime，也没有伪造硬件就绪。当前
  progress verifier 的 source 检查和 54 项索引定向回归通过，`gate_passed=false`、`release_allowed=false`
  仍保持 fail-closed。

- 2026-10-01：刷新序列参考器独立留出报告为 r2。库化的有界精确枚举在冻结的 12 条软件留出上
  与现有贪心同为 `120/120`，改进案例和同耗时改进均为 `0`；报告为
  `artifacts/evaluation/task-sequence-reference-independent-holdout-20261001-r2.json`，
  SHA-256 为 `26ef1ebbb3b8b6c7f4f1c0b2df8696dee5df924695797ab1572da48f575`。不晋升、不部署，
  不改变 `SharedTaskPolicy`、PX4 Mission、HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布门。

- 2026-10-01：完成一次新的 external PX4/Gazebo 连续双任务运行。固定 digest 的同一 disposable
  VTOL 容器与同一 `MissionTelemetrySession` 中，`SharedTaskPolicy` 依次选择并完成
  `synthetic-east`、`synthetic-west`；两次 Mission transaction、planner revalidation、policy
  identity、journal hash-chain、router graceful stop 和 exact-ID cleanup 全部通过。运行证据与
  receipt 已写入 `artifacts/evaluation/shared-policy-current-vtol-two-task-20261001-r2*`。
  这是软件 external SIM 证据，不打开 HIL/REAL、室外 GNSS、真实硬件、人工 QGC 或最终发布门；
  receipt 仍按 `retained_files_audit` 语义处理，未宣称当前 runtime parity。

- 2026-10-01：完成 v0.3.0-pre.8 共享策略外部 campaign 的当前证据收口。fixed-wing、multirotor
  的 depth Mission source 使用 r20，VTOL 使用 r22；三机型 Mission matrix 与 vehicle-policy
  matrix 使用 r23，全部保留 Mission upload/readback/commit/permit、planner revalidation、
  sensor/GPS 质量与 disposable container cleanup 证据。它们仍只证明 external PX4/Gazebo SIM，
  不代表 HIL/REAL、室外 GNSS、硬件、人工 QGC 或实飞。

- 2026-10-01：修正 receipt 语义边界。对保留文件重新生成 receipt 现在标记为
  `retained_files_audit`，`runtime_source_parity_verified=false`；AI flight composite 明确
  `same_release_parity=false`、`current_runtime_parity_proven=false`，不再把读取当前 parity
  文件等同于旧运行源码已重跑。新增 runtime evidence SHA-256 复核、schema 与负向测试；当前索引、
  source parity、Ruff 和 111 个定向测试全部通过，最终 `0.3.0` 仍 fail-closed。

- 2026-10-01：重新建立当前源码 parity identity。`release-parity.json` 按显式 source list 重算为
  SHA-256 `2669ff1bf17eb2fd823897bd5c89129986b57499606fbdd824f48fad9d07a957`，core hash 为
  `255ca970488958f448860015d05d2fe84883736a16b45537a677a4271097296b`；新增
  `verify_current_progress_index.py --check-sources-only`，拒绝 canonical parity 与当前源码漂移。
  在该源码身份下重新执行 image/navsat/odometry 三条 disposable PX4/Gazebo fault recovery（r16/r17/r18），
  全部 transport/runtime gate 通过并完成清理；旧 r13/r14/r15 仅保留为历史证据。普通 current-index
  仍保持 fail-closed，直到其余旧 external evidence 完成真实重跑或显式历史化；不改变 HIL/REAL、室外
  GNSS、硬件或最终 `0.3.0` 发布边界。

- 2026-10-01：强化户外 metric-depth 标定验证：`validate_outdoor_depth_calibration.py` 新增独立
  holdout fail-closed 质量门，可约束独立样本数、MAE、RMSE、最大绝对误差和相对 MAE；严格模式在
  写出可审计报告后以非零退出，默认 advisory 行为保持兼容。新增正向/负向回归，明确没有现场真值时
  不会把软件验证升级为室外、HIL/REAL 或最终发布证据。

- 2026-10-01：补齐当前 PX4 response fault campaign。fixed-wing、multirotor、VTOL 的
  `flight_mode_loss`、`heartbeat_dropout`、`target_refresh_dropout` 共 9/9 个 fault 在当前
  scenario/SDF 下重新运行；每条 raw evidence 都观察到指定故障、保持 `runtime_gate=true`、
  `flight_gate=false`、fail-closed 并完成容器清理。campaign verifier 新增当前 scenario manifest
  SHA 交叉检查，旧 v63 fault source 现在会被拒绝；新的 campaign 为 r5，`campaign_sha256`
  为 `f33eeef5af6aa663797db892146099f922df7001e48567d5c37f52496c7cbe3e`。release-gate
  wrapper、current-progress 索引和 `depth-r21` audit 已重新绑定，仍为 32 blockers、
  `gate_passed=false`、`release_allowed=false`；不打开 HIL/REAL、室外 GNSS、真实硬件、
  人工 QGC visual acceptance 或最终 `0.3.0` 发布门。

- 2026-10-01：按当前 `release-parity.json` 重新执行三机型 PX4 `GLOBAL_POSITION_INT` response
  正向证据，fixed-wing、multirotor、VTOL 共 6/6 次 Docker/PX4/Gazebo 运行通过；更新当前
  response matrix/campaign 与 3D control 证据索引。campaign 的 9 个 fault 条目明确继续引用
  v63 fail-closed source，未把旧 fault 证据冒充本轮重跑。由于当前 SDF 模型资产继续变化，
  fixed-wing/multirotor depth Mission 又各执行一次 2 帧 same-runtime Mission transaction，生成
  r19 当前源证据并通过 `verify_current_progress_index.py` 的模型资产、sensor binding、SharedTaskPolicy
  与 planner revalidation 检查；旧 r18 保留但不再作为当前索引来源。release-gate report/manifest
  重新生成并绑定 `depth-r20` audit，仍为 32 blockers、`gate_passed=false`、`release_allowed=false`。
  这些结果只扩展软件 external SIM 与当前证据可追溯性，不开启 HIL/REAL、室外 GNSS、真实硬件、
  人工 QGC visual acceptance 或最终 `0.3.0` 发布门。

- 2026-10-01：修复 combined 外部验收证据的有界 JSON 投影缺陷。旧 20,000 节点/顶层字段上限会
  在真实运行已通过后截断 `vtol_mission` gate 和目标证据卡数组；现改为仍有限制的 200,000 节点，
  保证规范任务摘要与只读目标卡完整保留。使用新的容器/证据路径重新完成 900 帧
  Docker/PX4/Gazebo/QGC NavSat fault run：窗口内 wire=0、39 条源消息丢弃、source epoch/cache
  clear/fresh recovery/603 条恢复帧/timeline overflow 全部通过；RGB/depth/odometry/runtime ACK
  900/900，Mission reached `0..6`，fixed-wing/multicopter/land/terminal disarm 全部观察到，
  370 张目标卡均为 read-only。源证据为
  `artifacts/evaluation/combined-sim-qgc-navsat-20261001-r4/combined-evidence.json`，严格
  verifier 与不可变 receipt 已写入 `docs/evidence/`，文档索引更新后
  `verify_current_progress_index.py` 返回 `status=passed`。该结果仍只证明软件 external SIM，不打开 HIL/REAL、室外 GNSS、真实硬件、
  人工视觉或最终 `0.3.0` 发布门。

- 2026-10-01：将 `image`/`navsat`/`odometry` fault contract 接入现有 combined
  `integrated-sim-qgc` 生命周期。CLI 从同一 Gazebo transport 捕获唯一 helper-finished
  diagnostic 并复用严格 fault verifier；PowerShell wrapper 统一 source 大小写、拒绝没有
  post-window frame 的窗口，并使用扁平 argv 防止 `Start-Process` 的嵌套 `Object[]` 启动失败。
  新增后端/CLI/脚本正向与负向回归，定向测试 `171 passed`（含 image/NavSat combined CLI
  diagnostic 通过/拒绝）；当前 parity/index 已重算并绑定到
  `depth-r18` 不可覆盖副本，`verify_current_progress_index.py` 通过。尚未进行本轮 combined
  外部故障重跑，故不宣称新的 external combined 通过、HIL/REAL、室外 GNSS、硬件或最终
  `0.3.0` 发布。

- 2026-10-01：修复外部 Gazebo 传感器 fault timeline 与缓存边界的证据缺口。`image`、`navsat`、`odometry`
  三条固定 digest PX4/Gazebo 运行重新生成 r13/r14/r15 evidence；helper 首次丢弃动态源回调时清空历史并
  递增 source epoch，保留跨边界时间戳单调性；恢复前明确等待新样本。helper 记录每个实际 wire、收到图像
  索引、动态源时间戳与恢复绑定，acceptance/verifier 对窗口边界、零窗口 wire、连续序号、fresh source
  严格新于最后丢弃样本、timeline overflow 和 Docker 清理做 fail-closed 检查。三条运行均通过；旧
  20260927 r5/r8/r9 证据保留为 pre-fix baseline，不再作为当前无缓存复用证明；当前索引同时切换到
  不可覆盖的三机型 Mission/vehicle-policy matrix r15 与 VTOL receipt depth-r14。该项仍只证明外部
  PX4/Gazebo 传感器流恢复，不声明 QGC 人工视觉、HIL/REAL、室外 GNSS、真实硬件、实飞或最终
  `0.3.0` 发布。

- 2026-10-01：完成当前模型资产的完整 external PX4/Gazebo/QGC combined VTOL 验收，运行目录为
  `artifacts/evaluation/combined-sim-qgc-20260930T204652Z-49500`，不可变摘要为
  `docs/evidence/combined-sim-qgc-vtol-pass-20261001-r1.json`（SHA-256=
  `bfed41c776cd87fae58cd8db29909dbfe89cddeaa17ac01a927f29f54582a186`）。600/600 RGB、depth、
  odometry 和 runtime metadata ACK/barrier 全部通过，Mission reached `0..6`，固定翼/多旋翼/
  落地/terminal disarm 全部观察到；容器停止移除、动态端口释放。为解决前一轮两个独立问题，
  VTOL 深度 child link 设为近似零质量/惯量的 transport-only fixture，流 deadline 改为
  `max(240, ceil(frames*0.75)+60)`。该结果仍是软件 external SIM，不提升 QGC 人工视觉、HIL/REAL、
  室外 GNSS、硬件性能或最终 `0.3.0` 发布门，历史失败证据保持不可变。

- 2026-09-30：将当前 VTOL SharedTaskPolicy 外部 campaign receipt 重新绑定到当前
  `release-parity.json`，新 receipt 为
  `artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-depth-r9.receipt.json`；
  三机型 shared-ranker matrix 与 external Mission matrix 同步改用该 source。current-progress
  verifier 新增旧 parity receipt 拒绝测试，防止历史证据混入当前索引。该变更只强化证据身份与
  可追溯性，不声明 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布。

- 2026-10-01：完成一次完整外部 PX4/Gazebo/QGC combined rerun 的失败收口。attached VTOL gate
  通过，同一 MAVLink 实例收到 1036 个有效 `GLOBAL_POSITION_INT` 样本，Mission 当前序列到达
  `0→1→2`，但只确认 `MISSION_ITEM_REACHED 0/1`；最高相对高度 19.44 m，随后向西/南偏移，
  相对首点最大位移 79.5128 m 并降到地面附近，240 s 内没有 seq3、`VTOL_LAND` 或 terminal
  disarm。Mission/combined gate 均保持 false，独立摘要为
  `docs/evidence/combined-sim-qgc-vtol-failure-20261001.json`；这暴露的是当前 VTOL→FW→航点
  路线/动力学缺口，不是 GPS 观测缺口，未放宽任何验收门。integrated audit 同时保留首个
  `0.208943973 m` depth 低于 `0.4 m` 下限的拒绝，以及后续无目标身份绑定的候选；不提升目标
  地理定位、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。因 observer 诊断字段属于 Mission
  path，parity 文件 SHA-256 更新为 `7d187ba0707c81e49d8aaef070218d5010657f346a11899bd474574379ff6519`，
  新 source-bound 副本使用 `20261001-depth-r8`，历史 evidence 保持不变。

- 2026-10-01：收口外部 combined SIM 的两个证据边界。VTOL Mission observer 在同一 disposable
  MAVLink 实例显式配置 `GLOBAL_POSITION_INT` 10 Hz，避免任务位移/航迹/着陆证据依赖 PX4 默认流；
  combined QGC wrapper 不再强制常量 synthetic HIL 检测器通过 target geolocation，只验收 RGB/depth
  transport、共享测距链和 native QGC adoption，目标落图保持独立 gate。新增 Live depth/range
  candidate 诊断，短跑明确记录 `gazebo_depth=0.208943973 m` 因低于物理 `0.4 m` 下限而拒绝；
  未降低边界、未注入 truth、未落假点。VTOL/combined/Live/Vision/depth/CLI 回归、Ruff、PowerShell
  parse 和 current-progress index 均通过。源码 parity 更新为文件 SHA-256
  `573730de3a42c25d66bbc0c508e3eb8fe80a95b09539c37980d49e51c5ede572`，新 source-bound 副本使用
  `20261001-depth-r7`，历史证据保持不变；`release_allowed=false`、HIL/REAL、室外 GNSS、硬件和
  最终 `0.3.0` 发布门继续关闭。

- 2026-09-30：修复 source-bound parity 重绑定流程。维护脚本现在为新的 fixed-wing/multirotor
  depth Mission、VTOL campaign receipt、三机型 Mission matrix、shared-policy pair/vehicle
  matrix、observed session 和 release audit 写入独立 `20261001-depth-r6` 后缀，旧证据不覆盖；
  同时改为记录实际 UTC 更新时间并修正 Windows CRLF 造成的 `git diff --check` 全文件误报。
  `verify_current_progress_index.py`、external matrix verifier、索引/矩阵/深度定向回归与 LF
  写入回归均通过；VTOL/external SharedTaskPolicy campaign 相关 300 项测试和 Ruff 通过。没有重新
  启动 Docker/PX4/Gazebo/QGC，`release_allowed=false`、HIL/REAL、
  室外 GNSS、硬件和最终 `0.3.0` 发布门保持关闭。

- 2026-09-30：使用 VS 2022 x64 Developer 环境完成 QGC `0.3.0-pre.8` 增量 Release 链接，
  修复普通 PowerShell 未加载 Windows SDK `LIB` 路径导致的 `runtimeobject.lib` 链接阻塞；直接
  EXE 为 `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe`，SHA-256
  `E0FFF035ED309CB73C260570C880C6817B0A7006F5F9B887364F881AA2A9C6B1`。这仍只是软件构建证据，
  不提升 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

- 2026-09-30：同一构建安装到独立便携目录
  `build/portable-replay-preview-20260930`，包含 Qt/GStreamer/qoffscreen 运行时；portable
  `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui` 退出码为 0，未产生
  stdout/stderr。该无界面冷启动仅证明软件依赖闭包和 QML 初始化，不等同人工 Fly View、HIL/REAL、
  室外 GNSS、硬件或最终 `0.3.0` 发布验收。

- 2026-09-30：QGC 目标事件回放增加只读“落图预览”：对已通过 native hash/GPS 新鲜度和坐标域
  校验的历史事件，在 Fly View 主地图显示独立橙色坐标点与 WGS-84 `1σ` 圆。预览不写入
  `MultiDetectState`，不改变实时目标，不创建 Mission/Guided 项，也不发送飞控消息；新清单或清除
  预览会失效旧点，不可定位事件不会落假点。定向 QGC 回归覆盖该功能；人工视觉、HIL/REAL、室外
  GNSS、硬件和最终 `0.3.0` 发布门保持未通过。

- 2026-09-30：固定翼场景补齐 `forward-depth` runtime sensor declaration，并更新场景 channel
  canonical hash；使用与多旋翼相同的 SharedTaskPolicy、planner revalidation、PX4 Mission
  transaction 和 Gazebo depth adapter 重新运行 pinned disposable external runtime。新的
  `external-fixed-wing-depth-shared-policy-20260930-r2.json` 记录 2/2 RGB + 2/2 `MDGZDEP1`
  companion、Mission/container identity 交叉绑定和 cleanup 通过；`flight_started=false`、
  `physical_output=false`，不提升 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

- 2026-09-30：复核首轮多旋翼 SharedTaskPolicy + Mission + depth 组合证据时发现 policy/Mission
  使用了 `bbbb…` 测试 airframe hash，未与 live sensor transport 的稳定机型身份交叉绑定；首轮
  文件保留但降为历史，不作为当前验收。新增多旋翼 profile-bound policy artifact，按实际
  external plan hash 重新执行 r2，并让 verifier 解析 ranker、校验 policy/Mission/sensor/container
  的跨字段 identity，以及 executable Mission/wire/protocol hash 一致性。r2 证据为
  `artifacts/evaluation/external-multirotor-depth-shared-policy-20260930-r2.json`（SHA-256
  `af58d0650c8db7f9dfe2a3fd76fedf41775e9428ae78607d2c07bfdccd75ddac`）；2 帧 depth、Mission
  transaction、清理通过，未启动飞行或物理输出。

- 2026-09-30：在真实 pinned `px4io/px4-sitl-gazebo` disposable 容器中完成同一运行时的多旋翼
  `SharedTaskPolicy` + PX4 Mission transaction + Gazebo depth transport 组合验收。当前策略先经
  planner revalidation，再复用既有 Mission upload/readback/commit/permit 路径；同一轮接收 2 帧
  RGB 和 2 个 `MDGZDEP1` depth companion，GPS/heartbeat、深度 topic/校准绑定、Mission hash
  readback 和容器清理均通过。证据为
  `artifacts/evaluation/external-multirotor-depth-shared-policy-20260930.json`（SHA-256
  `30157bf0f704b0b694056a0fdb75dd972c1a65be63d889ec2b70648fdaf85982`）。策略未应用到 actuator，
  `flight_started=false`、`physical_output=false`；这不是 QGC 人工视觉、HIL/REAL、室外 GNSS、
  硬件、实飞或最终 `0.3.0` 发布证据。

- 2026-09-30：在 pinned `px4io/px4-sitl-gazebo` 的 `urban_inspection_multirotor_001` disposable
  容器中真实运行 1 帧 Gazebo depth transport；RGB + `MDGZDEP1` companion、GPS/heartbeat、
  scoped depth topic、校准绑定和自动容器清理均通过。证据为
  `artifacts/evaluation/depth-external-20260930.json`；这不是完整 Mission、HIL/REAL、室外 GNSS、
  硬件或最终 `0.3.0` 发布验收。

- 2026-09-28：将仓库内 `models/depth-anything-v2-metric-outdoor-small/model.safetensors` 通过新增
  `scripts/export_depth_anything_onnx.py` 导出为可供 LIVE/integrated SIM 共用的 ONNX artifact，并
  写入来源、输入形状、opset 和 SHA-256 provenance。`MetricDepthEstimator` 现在只接受已 pin 的
  indoor/outdoor ONNX digest；本轮 CPU ONNX Runtime smoke 输出为 `1x518x518`、全 finite，目标框
  估计链可运行。该 smoke 不是物理距离、室外 GNSS、HIL/REAL 或最终 `0.3.0` 验收。

- 2026-09-28：外部 PX4/Gazebo wrapper 已支持传入同一 ONNX 与标定文档，但当前仓库没有通过严格
  `depth_calibration` 契约的户外实测标定文件；缺少标定时 wrapper 明确拒绝启动，不使用合成比例、默认
  距离或 truth 注入。故 metric-depth 目前只完成导出与 CPU/software SIM smoke，外部测距证据仍待独立
  标定或 Gazebo depth/range 源。

- 2026-09-28：integrated SIM QGC 入口接入 LIVE 共用的 metric-depth 配置契约。`--perception-args`
  现在可传入同一深度模型、标定文档、深度上下限、时效、采样间隔和网格参数；配置最终进入
  同一个 `AsyncMetricDepthRunner`/`LiveMissionRunner`/`MultiModalRangingEngine`，不会创建第二套
  SIM 生命周期。未提供 metric-depth 模型时行为保持不变；若没有共享 multimodal ranging 配置则
  fail-closed。该项只是接通真实深度模型入口，尚不构成外部测距、HIL、REAL、室外 GNSS 或最终
  `0.3.0` 发布证据。

- 2026-09-28：完成固定 digest 的 PX4/Gazebo/QGC combined SIM 收口复核：300 帧真实 Gazebo 图像、
  CameraInfo/NavSat/全姿态速度绑定、QGC type-24 接收确认、Mission transaction、能量重放和清理
  均通过。目标落图门正确保持 fail-closed（`ranging_invalid_count=233`、`ranging_valid_count=0`、
  `target_evidence_card_count=0`），因为当前 VTOL 外部场景没有可证实的 metric-depth/range 源或
  稳定相机地面交点。下一项接入真实 Gazebo depth/range sensor，并绑定时间戳、标定和不确定性；不
  注入 truth、不放宽 `terrain_uncertainty_unbounded`。该结果不构成 HIL、REAL、室外 GNSS、硬件或
  最终 `0.3.0` 发布证据。

- 2026-09-28：新增只读 `TargetEvidenceCard` typed contract。它把同一 `TargetEvent` 与 canonical
  WGS-84 `TargetGeolocation` 结果绑定成 hashable 证据卡；GPS 不合格/过期、测距不可用或定位
  缺失时只写 `available=false` 和原因，绝不保留假坐标。该项是 SIM/REPLAY 机器契约，不是
  QGC 人工视觉、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 通过证据。

- 2026-09-28：统一 SharedTaskPolicy decision identity 校验。新增
  `policy_decision_matches_selected_task()`，并接入 vehicle-neutral 编译、observed session、预算型
  SIM 与 VTOL external campaign；合法 ABSTAIN 继续进入原有拒绝路径，SELECT 的 action、task id、
  artifact hash、truth-free、planner revalidation 任一漂移都会 fail-closed。核心 parity 更新为
  `7be5f567...`，生成新的 source-bound 证据副本（observed r20、runtime r6、vehicle r5、external
  Mission r9、pair r4）；历史证据不覆盖。该改动只收口决策契约，不构成物理能耗、HIL/REAL、室外
  GNSS 或最终 `0.3.0` 发布证据。

- 2026-09-28：完成该轮全仓回归，JUnit 共 5138 项，`5131 passed / 7 skipped / 0 failed`，
  结果归档于 `artifacts/evaluation/full-regression-policy-helper-20260928/pytest.xml`，仅代表软件
  回归通过，不替代 external PX4/Gazebo、HIL、REAL、室外 GNSS、硬件或最终发布门。

- 2026-09-28：在当前 checkout 真实执行固定 digest 的 VTOL PX4/Gazebo disposable runtime，并在
  同一 scenario 上重新执行 production AI target exercise；heartbeat/GPS、AI policy、target policy、
  truth-free TRACK decision、loopback 安全边界和容器清理均通过。新增 runtime、AI 及三机型 join
  evidence；arm/disarm 仅限 disposable SITL，不构成 QGC 视觉、HIL、REAL、室外 GNSS、硬件或实飞
  发布证据。

- 2026-09-28：使用当前 `vtol-shared-policy-v1.json` 完成新的双架次 VTOL external PX4/Gazebo
  campaign。两个任务均通过统一 Mission upload/readback/commit/permit 事务、policy artifact
  identity、planner revalidation、transaction journal/hash-chain 和 exact cleanup；receipt 与
  当前 release parity 绑定。首次 240 秒超时输入被任务合同正确拒绝，保留为负向诊断；通过运行使用
  合同允许的 600 秒。该结果仍仅是 headless external SIM，不构成 HIL、REAL、室外 GNSS、硬件、实飞
  或最终 `0.3.0` 发布证据。

- 2026-09-28：重新执行现场链路只读三次 watch preflight；Jetson、GR01、Camera 均不可达，源地址
  仍为 `192.168.1.13` 而不是声明的 `192.168.144.0/24`，因此 `hardware_link_ready=false`。
  该记录不修改网络、不登录设备、不发送飞控命令，也不提升 HIL/REAL、室外 GNSS 或发布门状态。

- 2026-09-27：使用当前 `vtol-shared-policy-v1.json` 重新生成四模式
  `REPLAY/SIM/HIL/REAL` SharedTaskPolicy runtime matrix，并让当前进度 verifier 重新打开
  artifact 文件、canonical hash、airframe profile、release parity、模式覆盖及 actuator/backend/
  MAVLink 禁止字段。旧 matrix 不再作为当前 artifact 证据；该项仍是软件合同检查，不构成真实
  HIL、REAL、室外 GNSS、实飞或最终 `0.3.0` 发布证据。

- 2026-09-27：VTOL external campaign 通过派生 scenario 对齐到 fixed-wing/multirotor 的同一
  canonical SharedTaskPolicy ranker，并完成两架次 pinned PX4/Gazebo Mission、policy evidence、
  transaction journal/hash-chain 与 exact cleanup。新增
  `scripts/create_shared_policy_vehicle_matrix_receipt.py` 及 verifier，明确记录三机型
  ranker/artifact identity：`same_high_level_ranker=true`、`unified_policy_model_proven=true`，
  但 VTOL artifact 仍因 airframe profile 绑定而 `same_policy_artifact=false`。这只收口
  external SIM，不构成 HIL/REAL、室外 GNSS、实飞或最终 `0.3.0` 发布证据。

- 2026-09-27：新增 `scripts/derive_vtol_shared_policy_campaign.py`，以可复现方式从既有 VTOL
  campaign 派生共享 ranker 版本；不修改原历史 campaign 或旧 evidence。

- 2026-09-27：`verify_current_progress_index.py` 现在也会重新计算 fixed-wing/multirotor
  SharedTaskPolicy 配对 receipt、两份 external source evidence、策略 canonical hash 和安全字段；
  receipt 或任一源记录发生漂移时直接 fail-closed。

- 2026-09-27：vehicle-neutral external runner 新增严格成对的 SharedTaskPolicy 输入；固定翼
  r12 与多旋翼 r6 在 pinned Docker/PX4/Gazebo Mission transaction 中使用同一 ranker/artifact
  通过，配对 receipt 重算 task-binding、planner revalidation、truth-free 和
  `policy_applied_to_actuator=false`。VTOL 的旧 campaign artifact 身份保持原样并明确隔离，
  没有把不同模型哈希合并成虚假同一性；HIL、REAL、室外 GNSS 硬件、飞行和最终 `0.3.0`
  仍保持 fail-closed。

- 2026-09-27：fixed-wing r10 与 multirotor r4 在当前 parity 下重新执行 external PX4/Gazebo
  Mission transaction。vehicle-neutral `select_and_compile_task` 为每次运行写入 hash-bound
  `task-binding.json`；三机型矩阵 r6 verifier 会重算 planner revalidation、truth-free 和
  `policy_applied_to_actuator=false`，然后才接受 Mission transport 证据。该增量只收口
  external-SIM Mission 矩阵；HIL、REAL、室外 GNSS 硬件、QGC 人工视觉、飞行、candidate promotion
  和最终 `0.3.0` 仍保持 fail-closed。

- 2026-09-27：完成一次真实 fixed-wing Docker/PX4/Gazebo external Mission transaction：共享
  compiler 产物经过 live GPS/context 绑定后，`MISSION_ITEM_INT` upload/readback/commit/permit
  和 journal recovery 全部通过，PX4 规范化后的 protocol hash 与 readback 一致，容器清理成功。
  仍未 arm、切换模式或启动飞行；HIL/REAL、室外 GNSS 和最终 `0.3.0` 继续关闭。

- 2026-09-27：完成一次真实 multirotor Docker/PX4/Gazebo external Mission transaction。
  当前共享 compiler 产物经过 live GPS/context WGS-84 绑定后，`MISSION_ITEM_INT` 上传、回读、
  commit、permit 和 journal recovery 全部通过；证据为
  `external-multirotor-mission-20260927-r2.json`。同轮 fixed-wing 运行因 PX4 回读对 TAKEOFF
  pitch/landing heading 的规范化造成 wire hash mismatch，按设计 fail-closed，未伪造通过。
  两者都没有 arm、mode change、起飞或 physical output；HIL/REAL、室外 GNSS 和最终
  `0.3.0` 继续关闭。

- 2026-09-27：新增 `scripts/run_external_px4_mission_transaction.py` 外部 Mission bridge。
  它只接受已经通过共享 planner/compiler 生成的 `PlannedMissionCompilation`、truth-free
  `ContextSnapshot` 和 runtime safety policy；`--execute` 时由 Docker acceptance 先完成
  ownership 与 heartbeat/GPS qualification，再打开 MAVLink 调用统一
  `ExternalPx4MissionExecutionAdapter`，结束后由父层负责 stop/rm。dry-run 和缺少输入的
  fail-closed 回归通过；不把该入口误报成 fixed-wing/multirotor 外部飞行、HIL/REAL、室外
  GNSS 或最终 `0.3.0`。

- 2026-09-27：Docker simulator acceptance 新增可选 `mission_transaction_runner` 接线。
  该 hook 只在容器 ownership、真实 PX4 heartbeat/GPS telemetry 和 truth-free 条件通过后
  执行，适用于把 `ExternalPx4MissionExecutionAdapter` 接入同一 disposable runtime 的
  `MISSION_ITEM_INT` upload/readback/commit/permit 事务；结果必须明确
  `gate_passed=true`、`flight_started=false`、`physical_output_enabled=false`，否则
  acceptance fail-closed 并保留 stop/rm 清理。新增正向和起飞越界回归通过；不宣称新的
  fixed-wing/multirotor 外部飞行、HIL/REAL、室外 GNSS 或最终 `0.3.0`。

- 2026-09-27：为外部 Mission transport 增加 `ExternalRuntimeAttestation`。只有 64-hex container
  identity、digest-pinned image、vehicle/profile、scenario hash、ownership、qualified telemetry、
  truth-free 和 hardware-control-disabled 条件全通过，结果才可标记 `SIM_EXTERNAL`；未认证的
  fake/injected connection 仍只记录 `transport_exercised`，不能冒充外部 PX4/Gazebo 运行。

- 2026-09-27：新增 vehicle-neutral 外部 PX4 Mission transport 与
  `ExternalPx4MissionExecutionAdapter`。它复用既有 compiler、transaction journal、精确
  `MISSION_ITEM_INT` readback、commit 和 start permit；只接受已由外部编排器完成 ownership/telemetry
  检查的 SITL connection，并在 permit 后停止，不切换模式、不 arm、不产生 actuator output。
  固定翼和多旋翼 fake-MAVLink 回归通过，legacy `MISSION_REQUEST` fail-closed；这不是外部飞行、HIL/REAL
  或最终 `0.3.0` 发布证据。

- 2026-09-27：`load_campaign_definition()` 的 vehicle-neutral runtime parser 改动触发了
  `px4_mission_path` parity 重算；当前文件 SHA-256 为
  `2bca522a77809a346e7b6e5bb5b4c0259749e55858838e5f28fbfcf01c23eff5`，并以当前只读 field-link
  预检生成 release audit `r29`。observed/campaign/AI receipts 已 source-bound 重绑，未将旧
  PX4/Gazebo 运行冒充新运行，release gate 继续关闭。

- 2026-09-27：将 `load_campaign_definition()`/`load_campaign()` 的 runtime 解析改为接受显式
  `vehicle_class`；默认仍为 VTOL，现有 CLI 不变。新增 vtol、fixed-wing、multirotor 三机型输入
  契约回归，使未来外部 adapter 复用同一严格 task/runtime 文档，同时不宣称三机型外部 Mission
  lifecycle 已完成。

- 2026-09-27：新增 vehicle-neutral `MissionExecutionAdapter` 协议及确定性
  `InProcessPx4MissionExecutionAdapter`，对 vtol、fixed-wing、multirotor 复用同一
  `compile_executable_mission` 与 journalized upload/readback/commit/permit 事务，并在边界校验
  vehicle class、airframe profile 与 PX4 adapter identity。该适配器为 software-only，不打开外部
  transport、不产生 actuator output；fixed-wing/multirotor 的 PX4/Gazebo telemetry adapter、
  HIL/REAL、室外 GNSS 和最终 `0.3.0` 发布门仍未完成。

- 2026-09-27：修复 `run_px4_airframe_transaction_acceptance.py` 与其独立 verifier 的直接 CLI `src`
  bootstrap，并用当前 checkout 重跑三机型 deterministic Mission transaction gate；vtol、fixed-wing、
  multirotor 的统一 compiler/transport/journal/readback/commit/permit 路径全部通过。该 evidence 不宣称
  外部 PX4/Gazebo telemetry、HIL/REAL、室外 GNSS、实飞或最终 `0.3.0`。

- 2026-09-27：用当前 `release-parity.json` 重新执行 VTOL parent headless PX4/Gazebo campaign `r14`，
  两个任务均完成；共享策略、planner revalidation、Mission transaction/journal/hash-chain、exact-ID
  cleanup 和 post-remove absence 通过，并生成 `policycapture-parity-r9` receipt。该运行仍是 SIM-only，
  不增加 QGC 视觉、HIL/REAL、室外 GNSS、实飞、candidate promotion 或最终 `0.3.0` 发布声明。

- 2026-09-27：新增 vehicle-neutral `CampaignTaskBinding`/`select_and_compile_task` seam，复用同一
  SharedTaskPolicy、planner revalidation 和 PX4 Mission compiler，并由 SimulatorScenarioRunner 使用。
  vtol/fixed-wing/multirotor 的 deterministic runtime 回归通过；GPS 不合格时 fail-closed abstain。
  VTOL 外部生命周期仍由专用 adapter 负责，fixed-wing/multirotor 外部 PX4/Gazebo acceptance 尚未声明，
  因此不改变 candidate promotion、HIL/REAL 或最终 `0.3.0` 发布门。

- 2026-09-27：将 vehicle-neutral seam 纳入 core parity；在 VTOL campaign 接线后重新绑定 PX4 Mission
  path parity，对已有 observed-session、campaign receipt 和 simulator AI receipt 做 source-bound rebind，
  并生成 release-gate audit `r27`。旧外部 report 尚未按新
  core hash 重跑，`current_release_parity_bound=false` 按设计保留，未把旧 PX4/Gazebo 运行升级成新证据。

- 2026-09-27：自主航线预算升级为逐航段 WGS-84 风三角模型。planner 现在分别计算每个 outbound/task/return 航段的航向、侧风与沿航风；不把顺风收益计入额外空速，侧风超出规划空速或前向进度不足时 fail-closed。当前 report-only release-gate audit 为 `r25`，四个 mode parity 已绑定当前 core hash；VTOL 外部 campaign 已完成 2/2，但三机型完整 external mode matrix 仍待重跑，最终发布与 candidate promotion 继续关闭。

- 2026-09-27：新增只读 `audit_external_planner_wind_binding.py`，逐架次重开当前 VTOL Mission evidence，验证 scenario runtime channel、ContextSnapshot canonical hash 与非零风场输入一致；2/2 通过。该证据不宣称物理风场标定、HIL/REAL、室外 GNSS 或最终发布。

- Executed the third bounded external PX4/Gazebo sensor-fault run for `odometry`: 4 frames,
  two dropped odometry messages, two recovered frames, real telemetry, wire-v3/raw-frame integrity,
  zero flight-control commands, and clean container removal. The current-progress verifier now
  binds and re-reads all three external fault evidence records (`image`, `navsat`, `odometry`)
  instead of trusting only documented booleans; the new focused set passed 208 tests.

- Executed bounded disposable PX4/Gazebo sensor-fault acceptance with the pinned image:
  `image` recovery passed with 2 requested frames and `navsat` recovery passed with 4 requested
  frames. Short 2-frame `navsat` attempts remain failed evidence rather than being treated as
  recovery; the acceptance preflight now requires 4 frames for dynamic `navsat`/`odometry` faults.
  Dynamic recovery compares the post-window source timestamp to the last actually dropped source
  timestamp, which handles startup callback ordering without accepting stale cached telemetry.
  No flight-control commands were sent, and disposable containers were stopped and removed.

- Rebound release parity and all current evidence after the recovery-gate fix: parity file
  `8d84d191ba55233b03e3166dcdcc6dad5c9ccd1e7b87388f283d72827413e986`, observed session `r11`,
  campaign receipts `r5`, AI parity receipt `r4`, and release-gate audit `r17`. The progress index
  verifier remains passed; final release, candidate promotion, HIL/REAL, outdoor GNSS, and hardware
  acceptance remain closed.

- Added a bounded external Gazebo sensor-fault contract for `image`, `navsat`, and `odometry`.
  The helper and backend use an image-indexed window rather than wall-clock timing, bind the fault
  configuration into the sensor identity hash, and require dropped-source evidence plus post-window
  wire-v3 recovery before accepting the diagnostic. `camera_info` is intentionally excluded because
  it is session-static and cached. This turn validated implementation and fail-closed diagnostics;
  it did not claim a new Docker/PX4/Gazebo fault run.

- Rebound the current progress index to release-parity `af11eaa6c329d22cc41d686318d4f8be404840d541ba9d178400a68680a1008b`,
  observed-session `r10`, campaign receipts `r4`, and simulator AI parity receipt `r3`. The index verifier
  and 205 targeted tests pass. The report-only release audit is `r16` with `current_release_parity_bound=false`;
  release, candidate promotion, HIL/REAL, outdoor GNSS, and hardware claims remain closed.

- Rebound the prerelease release-gate diagnostic to the current parity: regenerated the external
  runtime/AI join and revalidation plus the three-airframe PX4 reported-position response matrix and
  fault campaign, then produced the isolated `release-gate-current-20260927-r4` report with the
  current `replay-set-positive-fixture-20260924` paths explicitly bound. Historical software-evidence
  drift blockers dropped from 45 to 32 while the gate remains fail-closed; no replay deployment,
  field hardware, QGC human visual, HIL/REAL, or final-release claim was added.

- Re-ran the current deterministic scenario matrix with temporal fault coverage and recovery enabled.
  The same runner/AI/arm/watchdog path covered fixed-wing, VTOL, and multirotor, automatically extending
  the short request from 4 to 2,612 frames; all nine declared fault events were observed. Finite faults
  returned to a no-fault baseline under the same manifest/clock with AI control isolated, while the
  indefinite battery step was explicitly marked as not expected to recover. Repeated evidence hashes
  matched and the independent verifier passed. This remains truth-free software-SIM evidence and does
  not claim external 3-D, QGC visual, HIL/REAL, hardware, outdoor GNSS, or release.

- Combined the fault-recovery matrix change with the current policy/Mission, sensor-capture, parity,
  Docker-acceptance, and flight-model regression set: `624 passed`. The release gate remains unchanged
  and fail-closed.

- Added a direct shared-policy campaign regression for unqualified GPS. A fresh context with an
  insufficient `gps_fix_type` now has an explicit `abstain/no_feasible_task` assertion: no Mission
  evidence is written, no Mission transaction is entered, and the telemetry session's Mission count
  stays at zero. This strengthens the software fail-closed boundary without claiming outdoor GNSS or
  hardware acceptance.

- Re-ran the pinned disposable PX4/Gazebo sensor transport against the current repository. The
  `1280x960` calibrated geometry produced three hash-bound BGR24 frames with NavSat/odometry/
  covariance time association, real PX4 heartbeat/GPS telemetry, zero flight-control commands,
  and exact container cleanup in `artifacts/evaluation/simulator-docker-acceptance-20260927-r3-execute.json`.
  A preceding `160x90` request was retained as a fail-closed negative evidence record; neither run
  claims QGC visual, HIL/REAL, outdoor hardware GNSS, or final-release acceptance.

- Executed the production simulator AI target exercise in a fresh disposable PX4/Gazebo runtime.
  The truth-free perception seam reached `TRACK` through `SimulatorAiExecutor` and the shared
  loopback `MANUAL_CONTROL` adapter, telemetry/target policy gates passed, watchdog shutdown and
  exact container cleanup passed, and the evidence is stored at
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260927-r1.json`. The explicit ARM/DISARM
  and AI control messages were confined to SITL; no PX4 hardware actuator, QGC visual, HIL/REAL,
  outdoor GNSS, or final-release claim was added.

- Extended the same disposable production-AI exercise into a bounded flight-response check. Over
  roughly `4.296s`, PX4 `GLOBAL_POSITION_INT` supplied 99 position samples and showed `0.10077m`
  horizontal displacement against a `0.1m` gate; 367 AI decisions produced 367 loopback control
  outputs, then watchdog/failsafe shutdown and exact cleanup passed. This remains truth-free SITL/
  Gazebo response evidence and does not claim a real PX4 actuator, HIL/REAL, outdoor GNSS, visual,
  or final-release result.

- Added `simulator-ai-flight-parity-20260927-r2.receipt.json`, a hash-bound composite receipt that
  verifies the current five release-parity hashes, scenario manifest, and airframe identity across
  the high-level `SharedTaskPolicy` Mission layer and the separately evidenced low-level simulator
  AI flight-response layer. The receipt keeps `policy_applied_to_actuator=false`; it is not a HIL,
  REAL, outdoor-GNSS, hardware, QGC-visual, or final-release claim.

- Verified the composite-receipt, observed-session, independent index binding, and GPS fail-closed campaign change with 612 targeted tests plus the current-progress index
  verifier; the prerelease release gate remains fail-closed.

- Added a Draft 2020-12 schema and independent validator test for the composite parity receipt so
  its layer separation, hash fields, safety boundary, and non-HIL/REAL claims are reviewable without
  executing the producer script.

- Reinstalled the current `0.3.0-pre.8` Windows Release portable tree at
  `build/windows-v0.3.0-pre.8-release/portable-current-20260927-r1`. The incremental build was
  clean (`ninja: no work to do`), eight compiled C++ self-tests passed, including the 44-fragment
  Python-to-C++ depth-grid round trip, and the portable QGC offline simple-boot test exited `0`.
  This is prerelease portable-runtime evidence only; QGC human visual, HIL/REAL, outdoor GNSS and
  final `0.3.0` release gates remain open.
- Captured the same newly installed portable runtime through the native QGC client-surface machine
  gate at `artifacts/evaluation/qgc-native-style-portable-current-20260927-r1/`; the exact `804x528`
  client surface and occluder cleanup passed. Human visual acceptance remains explicitly unclaimed.

- Enabled QGC native-style machine capture on low-resolution remote/virtual displays by lowering the
  capture utility's optional floor to `640x480` while preserving the `1600x900` default, and fixed
  the Win32 `RECT` size check. Recorded the SIM capture at
  `artifacts/evaluation/qgc-native-style-sim-20260927-r4/`; this remains machine evidence only with
  `operator_visual_acceptance=not_claimed`, not HIL/REAL, outdoor GNSS, or final-release evidence.

- `SimulatorScenarioRunner` 新增真实高层任务闭环：`SharedTaskPolicy` 选择、planner 重验、Mission 编译和 hash-bound `ExecutableMission` 绑定；abstain 不绑定 Mission，策略不接管 actuator。

### 外部 SIM 运行与现场证据覆盖约束（2026-09-26）

- 将 geolocation 的独立距离/坡度覆盖门继续绑定到 field-evidence bundle：显式策略会在 bundle
  边界拒绝缺失、错配或不足的 regime，默认零要求保持旧报告兼容；这只加强证据完整性，不把
  synthetic 或 headless 结果升级成室外 GNSS、HIL、REAL 或飞行验收。
- 重新执行一次固定 digest 的 Docker/PX4/Gazebo disposable external SIM：证据为
  `artifacts/evaluation/simulator-docker-acceptance-20260926-r2-execute.json`，实际观察到 PX4
  `HEARTBEAT` 与 `GPS_RAW_INT`（fix type 3、10 satellites），容器 stop/rm 清理成功，flight-control
  command 计数为 0。该记录只证明 truth-free headless external SIM telemetry，不代表 QGC 视觉、HIL、
  REAL、室外硬件 GNSS 或最终 `0.3.0` release。
- parity 重绑后全仓回归为 `5043 passed, 7 skipped, 0 failed`；当前版本继续保持
  `0.3.0-pre.8`，candidate 未晋升，最终发布门仍 fail-closed。
- 在同一固定 PX4/Gazebo 场景进一步执行 3 帧真实 sensor transport：BGR24 原始帧、camera
  calibration、NavSat、odometry、covariance 和时间关联写入 hash-bound manifest，原始像素与
  wire payload SHA 校验一致。该证据推进 geolocation SIM 输入链，但仍不代表 QGC 视觉、HIL/REAL、
  室外硬件 GNSS 或最终发布验收。

### Release-gate replay 重绑与现场只读刷新（2026-09-25）

- 重新生成 `field-link-refresh-20260925-r1` 只读现场预检与接口诊断；Ethernet 仍为
  `192.168.1.13/24`，`192.168.144.0/24` 没有活动接口或路由，Jetson/GR01/Camera 三次探测均超时，
  因而 `hardware_link_ready=false`，未修改网络或发送飞控指令。
- 将当前 `0.3.0-pre.8` release-gate wrapper 绑定到正例 replay fixture，而不是旧的空正例覆盖样本；
  新报告 `release-gate-positive-replay-20260925-r1` 仍为 `gate_passed=false`、`release_allowed=false`，
  31 个阻断原因保持 fail-closed。
- 为同一 synthetic manifest 生成 report-only threshold calibration 与独立 verification；verification
  只证明 manifest/canonical SHA-256 和重算一致，不代表部署准确率、HIL/REAL、室外 GNSS 或最终版本通过。
- `verify_current_progress_index.py` 新增 release-gate replay manifest/accuracy 路径与 SHA-256 交叉绑定，
  防止当前 gate 再漂回历史 replay 输入。
- 实际执行一次独立 `Docker/PX4/Gazebo` external SIM（`docker-external-refresh-20260925-r1.json`）：
  固定镜像启动、真实 PX4 `HEARTBEAT` 与 `GPS_RAW_INT`（fix 3、10 satellites）通过，disposable
  容器 stop/rm 清理通过；该证据只证明 headless external SIM telemetry，不升级 QGC 视觉、HIL、REAL、
  室外硬件 GNSS 或最终 release 闸门。

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

# Multi-Detect Changelog

### v0.3.0-pre.8 runtime policy capture binding (2026-09-27)

- `px4_runtime_capture` now optionally loads and hash-binds the exact `SharedTaskPolicy` artifact before opening telemetry, rejecting policy/Mission airframe mismatches fail-closed.
- Capture receipts preserve legacy six-source packages and add a seventh `policy_artifact` source when enabled; current observed-session, external-SIM receipts, release parity, and release-gate audit were regenerated against the new parity.
- This is evidence identity closure only: no actuator authority, HIL/REAL, outdoor GNSS, physical-flight, candidate-promotion, or final `0.3.0` release claim was added.

### v0.3.0-pre.8 outer policy campaign execution (2026-09-27)

- Executed the outer `run_vtol_mission_sensor_capture.py` path against the pinned disposable PX4/Gazebo runtime with the shared policy artifact: two sorties completed, planner revalidation and per-sortie policy evidence passed, and exact-ID cleanup/post-remove absence passed.
- Recorded the result as `artifacts/evaluation/outer-policy-capture-20260927-r1.json` plus a hash-bound receipt. The run remains headless external SIM only; QGC visual, HIL/REAL, outdoor GNSS, physical flight, candidate promotion, and final release remain unclaimed.

### v0.3.0-pre.8 four-mode release-parity matrix (2026-09-27)

- Extended `verify_shared_policy_runtime_matrix.py` so REPLAY/SIM/HIL/REAL rows bind and compare the current core-algorithm, PX4 Mission, safety, task-contract, and UI-contract hashes.
- Generated `artifacts/evaluation/shared-policy-runtime-matrix-20260927-r2.json`; it records `same_release_parity=true` and `same_ui_contract=true` while keeping visual QGC acceptance explicitly unclaimed.

### v0.3.0-pre.8 sensor clock and current evidence rebinding (2026-09-27)

- Completed the SharedTaskPolicy binding in the external VTOL campaign entry point. The
  campaign now binds policy/ranker/airframe identity, evaluates only truth-free observed
  context, revalidates the planner before compilation, rejects abstain or plan drift before
  Mission dispatch, and records per-sortie policy evidence while preserving the existing
  PX4 Mission lifecycle and ranker-only compatibility. Targeted regression: 517 passed;
  this is interface/fixture evidence only and does not claim a new PX4/Gazebo, HIL, REAL,
  outdoor-GNSS, or final-release run.
- Re-ran a parent/candidate pair through the same disposable PX4/Gazebo VTOL campaign after
  moving the reused-container mission-current reset to after commit/AUTO.MISSION and before
  ARM. Both runs completed two sorties with planner revalidation, per-sortie policy evidence,
  transaction journals, bounded router checks, and exact-ID container cleanup. Receipts are
  `artifacts/evaluation/shared-policy-external-parent-20260927-r13-parity.receipt.json` and
  `artifacts/evaluation/shared-policy-external-candidate-20260927-r13-parity.receipt.json`.
  The release parity was regenerated after the cursor-reset source change and these receipts
  were recreated against that current parity. The
  artifacts share the same policy identity and no independent candidate gain is proven, so
  promotion remains false; this is headless external software SIM evidence only, not QGC
  visual, HIL, REAL, outdoor-GNSS, physical-flight, or final `0.3.0` release evidence.
- Fixed sensor-enabled VTOL campaign final cleanup revalidation: the outer auditor now receives
  the same sealed manifest and task-to-frame map on both initial and final checks.
- Distinguished absolute `host_monotonic`/`host_perf_counter` frame timestamps from the legacy
  collector-relative Mission-window label while retaining the existing energy/replay field; same
  clock-domain evidence is accepted only with explicit absolute-clock semantics and mismatched
  domains still fail closed.
- Recomputed PX4 Mission parity and regenerated the deterministic observed-session and campaign
  receipts. Historical external reports remain immutable; the current release audit is r8 and
  still fail-closed with no HIL/REAL, outdoor-GNSS, or final-release claim.

本文件记录 Multi-Detect 产品版本。`ground-control/MultiDetectGCS/CHANGELOG.md` 属于上游
QGroundControl 历史，不作为本产品发布记录。

## [Unreleased]

- 2026-10-07：沿已有 Qwen3-VL r1 FP32 adapter 增量训练，新增 128 个实际实体三维 RGB
  train 样本独立核验；固定单轮 154 更新，原 84 个留出输入未改。原生退出 0，基座／视觉
  参数未变，112 个保存张量及固定验证 token/raw 精确重载。strict73→70、exact61→53，
  可见目标命中及测试负例判断回归，质量不通过、不晋升、不据留出结果调参重试。
  新增独立原始回答／训练日志／FP32 参数审计入口，预算上限和失败计数因果门已修复并
  验证；实际 CPU 审计 native0，重开709个文件绑定／11个官方文件及全部原始回复，
  确认质量门5/8失败；源码与parent111项、审计器82项通过。终态为
  `docs/evidence/onboard-vlm-grounding-increment-20261007-r2.json`。旧失败证据和默认
  r33／COCO／生产 parity 不改。
  Controller 实际核验远程 Docker daemon，并 pull/inspect 固定 PX4/Gazebo 镜像 native0；
  被动八帧采集 native1，内层原因未取回，worker offline 阻断诊断派发。未 ARM/Mission，
  不当作飞行或动作训练通过。说明 `docs/onboard-vlm-grounding-increment.md`，仿真记录
  `docs/evidence/gazebo-runtime-preparation-20261007-r1.json`；最终发布门仍未通过。

- 2026-10-07：新增原主机 begin/seal、单次 HOLD 准入、子航段授权／INT 上传读回／cursor1／
  恢复的开发 host 与真实 loopback UDP 接口；默认禁写、不 ARM／改参数／自授执行权。
  generation 取消、全发送人工模式 drain、latest UTC 到期、raw／compile 时效与严格根节点
  类型检查；原 journal bytes 每写核验，完整验证每阶段一次，消除重复 JSON/journal 热点。
  独立 protocol peer 联调不是 Gazebo/PX4 动力学；旧失败 profiling 保留，15秒 daemon 查询仍
  超时未知。重复父子链、持久消费、watchdog、真实净空/来源与 whole-sortie dispatcher 未接。
  无新增模型训练／生成、晋升、硬件或发布；版本仍 0.3.0-pre.8。说明
  `docs/onboard-continuation-host.md`，新回执
  `docs/evidence/onboard-continuation-host-development-20261007-r1.json`，旧封存文件未改写。

### Added

- 2026-09-30：新增 `src/multidetect/gazebo_depth.py`，为未来真实 Gazebo depth/range topic
  建立 truth-free、时间戳/校准/帧绑定的严格测距契约，并转换到现有
  `DirectRangeMeasurement`/`MultiModalRangingEngine`，覆盖未来源时间、过期 source、非法
  topic、无效尺度与 schema 漂移的 fail-closed 回归。当前 urban manifest 的
  `downward-depth` 仍明确是 declared-only，因为仓库 SDF 尚未提供对应实际 topic；没有把它
  写成外部 SIM、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 证据，wire-v3 保持兼容。

- 2026-09-30：新增 `scripts/rebind_combined_target_evidence_cards.py`，从固定的历史 combined
  evidence 与 5362-event integrated audit 只读重建 336 个 `TargetEvidenceCard`。工具绑定源文件与
  审计文件 SHA-256、session/sequence/时间单调性、primary→range→geolocation 因果顺序和 GPS/
  测距字段，并将结果写成独立的 historical-observation bundle；旧 evidence 不覆盖，当前 parity
  只作为工具上下文。正式输出为
  `artifacts/evaluation/combined-target-card-reconstruction-20260930-r2/bundle.json`（SHA-256
  `abd3f4fd91e099213df1ae15a32131499d6f50c4552be302ec99bce3d211ad3e`）及
  `docs/evidence/combined-target-card-reconstruction-20260930-r2.json`（SHA-256
  `04375026626680e88012c4c851d896f8c7c41edafaba84b23e697add7f5738da`）。r2 同时修正
  provenance 中的 label causal sequence；r1 保留为历史产物但不再作为当前索引。该结果未重新执行
  runtime，不构成当前 external SIM、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布证据。

- Bound external VTOL campaign Mission evidence to the sealed sensor transport manifest. The
  campaign now requires an exact task-to-frame map, reopens and rehashes raw BGR24 bytes at the
  campaign boundary, and records frame sequence/hash/bytes/capture clock in each Mission report.
  Receipt generation and outer campaign validation reject frame reuse, raw-byte tampering, clock
  domain mismatch, and timestamps outside the Mission receive window. Legacy sensor-disabled
  receipts remain compatible; no HIL/REAL, outdoor GNSS, or final `0.3.0` claim is made.
- Rebound `configs/release-parity.json`, the deterministic observed session, parent/candidate
  campaign receipts, and the fail-closed release-gate audit after the Mission provenance change.
- Re-ran a read-only field-link preflight against the configured Jetson/GR01/Camera endpoints.
  The selected local source remains `192.168.1.13` instead of the expected aircraft subnet
  `192.168.144.0/24`, all three remote probes timed out, and `hardware_link_ready=false`; no
  network mutation or flight-control command was performed. HIL/REAL/outdoor-GNSS gates remain
  explicitly unclaimed.
- Hardened the outer campaign auditor: `validate_campaign_result()` now reopens the sealed sensor
  manifest and revalidates the exact task-to-frame map before checking each Mission report. A
  caller cannot pass a fabricated in-memory manifest object to bypass raw-byte or canonical-hash
  verification; the tampered-after-load path is covered by regression tests.

- Bound the Gazebo wire-v3 BGR24 sensor manifest into geolocation replay/accuracy reports. Every
  selected replay event must resolve to one unique hash-verified sensor frame; the report records
  manifest file/canonical SHA-256, wire/pixel identity, frame IDs and truth-free provenance. This
  remains software/headless evidence only and does not claim QGC visual, HIL/REAL, outdoor GNSS or
  flight acceptance. Rebound release parity, observed-session evidence, campaign receipts and the
  fail-closed release-gate audit to the current core hash.
- Full repository regression after the transport/parity rebinding passed with `5051 passed, 7 skipped,
  0 failed`; the JUnit artifact is recorded in `docs/current-progress.json`. This remains software
  regression evidence only.

### Fixed

- Added an opt-in field geolocation survey coverage contract. Replay-to-truth WGS-84 reports now
  record separated distance regimes from valid replay range evidence and slope regimes from
  independently supplied survey truth; the generated worksheet now exposes blank
  `survey_range_m`/`survey_slope_deg` columns, and independently filled range takes precedence over
  the replay estimate. `--minimum-distance-regimes` and
  `--minimum-slope-regimes` fail closed when an outdoor survey is concentrated in one regime;
  defaults remain zero for backward compatibility, and the schema/verifier validate coverage
  metadata without turning it into a hardware, GNSS, HIL, REAL or flight claim. The survey-template
  schema now also accepts the generator's `event` envelope and declares the new coverage columns.
- Promoted the same coverage contract to the field-evidence bundle boundary. Explicit distance/slope
  requirements are now hash-bound in `policy`, `bindings`, and bundle identity; the accuracy stratum
  rejects missing, mismatched, or insufficient coverage while default-zero policies preserve legacy
  reports. This remains evidence integrity only and does not claim outdoor GNSS, HIL, REAL, or flight.

- Strengthened the truth-free observed-session evidence harness: every selected task is independently
  re-decided, planner-compiled, and compared exactly at the plan level before
  `planner_revalidated=true` is recorded. Rebound the 2026-09-24 evidence to SHA-256
  `1a3e52b2ac4245ee27a89a4888ed73614089ab6e831737396b1efcf88a001f03` (4,392 bytes).
- Added `scripts/create_shared_policy_campaign_receipt.py`, a read-only auditor that binds each
  external campaign sortie's Mission report, transaction journal and policy decision by SHA-256 and
  Mission hash. The parent/candidate receipts also bind the outer runtime evidence's exact container
  cleanup and post-remove absence, plus runtime mode, scenario manifest and stable airframe identity;
  it now also binds the five current release-parity identities (core algorithm, PX4 Mission path,
  safety policy, task contract and UI contract); no HIL/REAL or release claim is made.
- Added `scripts/verify_shared_policy_runtime_handoff.py` and its regression fixture. It joins a
  source SIM campaign receipt to a target HIL/REAL live-runtime binding and independently rechecks
  the policy artifact, stable airframe identity, Mission identity, release-parity hashes and locked
  outputs. A passing handoff is provenance only: it does not claim HIL/REAL, flight, outdoor GNSS or
  final `0.3.0` release acceptance.
- Integrated `shared_policy_runtime_handoff` into `release_gate.py` and the release-gate schema:
  prerelease reports may record an absent handoff while staying fail-closed, but a final release
  requires a hash-bound SIM-to-HIL/REAL handoff. The current parity rebinding explicitly reports
  old-mode/external evidence drift and keeps `current_release_parity_bound=false`.
- Added `--shared-policy-runtime-handoff` to the release-gate manifest writer. Supplied handoff
  files are reopened and checked before they can enter a manifest; a malformed or unlocked handoff
  cannot be smuggled into a prerelease or final-release report.
- Ran a fresh disposable Docker/PX4/Gazebo external SIM with the pinned image
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`:
  `HEARTBEAT` and `GPS_RAW_INT` fix type 3
  with 10 satellites were observed, and stop/rm cleanup passed. The record is headless SIM only;
  QGC visual, HIL/REAL, outdoor hardware GNSS and final release remain unclaimed.
- Completed a fresh parent/candidate shared-policy external VTOL campaign pair using the
  existing Mission transaction and router lifecycle. Both successful runs completed the two
  synthetic sorties with planner revalidation, policy evidence, journal integrity and exact-ID
  cleanup; hash-bound receipts were written for each run. The first candidate attempt is retained
  as a fail-closed quarantine because PX4/Gazebo reported
  `mission_item_reached_ahead_of_current_sequence`; a clean candidate retry passed. Parent and
  candidate artifact SHA-256 values are identical and no independent performance gain was proven,
  so the candidate remains unpromoted. This is headless SIM evidence only.
- Fixed the operator-facing `scripts/verify_shared_policy_runtime_matrix.py` CLI so it bootstraps
  the repository `src/` path when run directly, independent of an editable install or inherited
  `PYTHONPATH`. Added a clean-environment subprocess regression and generated the current
  four-mode (`REPLAY`/`SIM`/`HIL`/`REAL`) policy contract matrix. It proves one artifact and one
  stable airframe identity across descriptors with no backend, MAVLink or actuator capability;
  it does not claim HIL/REAL, hardware, outdoor GNSS or release acceptance.
- Added a fail-closed airframe-profile identity check to `run_observed_task_session`; a
  `SharedTaskPolicy` artifact whose profile differs from the observed runtime is rejected before
  task execution. Recomputed `configs/release-parity.json` and rebound the current external-SIM
  and release-gate evidence to `core_algorithm_hash=c4ddf12a...9befee`.
- The earlier `field-link-refresh-20260924-r4` record remains historical provenance; the current
  `0.3.0-pre.8` gate is rebound to the newer `field-link-refresh-20260925-r1` read-only probe.
  Freshness passes at evaluation time, while the missing `192.168.144.0/24` route and unreachable
  Jetson/GR01/Camera keep `hardware_link_ready=false`.
- Bound the deterministic observed-session evidence to the current release-parity file (bytes,
  file SHA-256 and all five contract hashes), so the software SIM session cannot be mistaken for
  evidence from a different core/UI/Mission contract.
- Hardened `verify_current_progress_index.py` with a four-way field-link binding: the current
  progress index, release-gate report, field-link preflight, and interface diagnostic must agree on
  resolved paths, byte counts, and SHA-256 values. Basic artifact checks run first so each drift
  class remains diagnosable; a report/progress path mismatch now fails closed instead of silently
  accepting independently valid files.
- Generated a new positive six-stratum replay evaluator fixture at
  `artifacts/evaluation/replay-set-positive-fixture-20260924/`. Its five positive strata have
  positive truth/prediction coverage, the IoU and latency report is reproducible and hash-bound,
  and the readiness report reopens the manifest and accuracy report. It remains explicitly
  `synthetic_demo`, `annotations_reviewed=false`, `deployment_domain_evidence_complete=false`,
  and `gate_passed=false`; no deployment-domain accuracy or release claim was made.

- Added explicit `--airframe-profile-sha256` binding to `scripts/train_task_ranker.py`. Dataset/holdout
  evaluation remains bound to the dataset identity, while exported parent/candidate artifacts can be
  bound to the caller's stable runtime VTOL identity without rewriting training evidence.
- Added an independent strict-loader evidence record for the runtime-bound parent/candidate artifacts;
  both file and canonical hashes, feature order, and stable airframe identity were rechecked without
  changing promotion state.
- Added a four-mode `REPLAY/SIM/HIL/REAL` shared-policy runtime matrix verifier. It checks identical
  policy and airframe identities while proving the policy profile carries no flight-control capability;
  the evidence is software-only and starts no backend or MAVLink transport.
- Added a deterministic software-SIM scenario matrix run across the fixed-wing, VTOL, and multirotor
  manifests. All declared faults were covered after bounded automatic frame extension, repeat hashes
  matched exactly, and the truth-free gate passed. The evidence remains software-only and does not claim
  external 3-D, QGC visual, hardware, HIL, REAL, outdoor GNSS, or final release acceptance.
- Clarified the roadmap and README to reflect the current external campaign lifecycle: the shared policy is
  already re-evaluated between missions inside one telemetry session, while HIL/REAL, field hardware,
  outdoor GNSS, QGC visual sign-off, replay-set review, and the final release channel remain open gates.
- Added `scripts/run_shared_policy_observed_session.py` and a bound evidence record for a deterministic
  two-task observed session. The existing SharedTaskPolicy selected and completed both tasks using the
  refreshed post-mission context; repeated canonical session hashes matched and the session emitted no
  backend, MAVLink, or actuator capability. This is software evidence only.
- Refreshed the read-only field-link preflight and interface diagnostic on 2026-09-24, rebound the
  no-argument release-gate wrapper to the fresh files, and retained the real `hardware_link_ready=false`
  and missing aircraft-subnet route result. No adapter address, route, DNS setting, or device state was
  changed.
- Preserved the 2026-09-20 baseline full regression: `5016 passed, 7 skipped, 0 failed, 0 errors`
  with exit code `0`; JUnit is retained at
  `artifacts/evaluation/full-regression-20260920-runtime-matrix/pytest.xml` and bound by SHA-256 in
  `docs/current-progress.json`. This is historical software regression evidence, not the current
  airframe-guard regression.
- Added a read-only `verify_current_progress_index.py` check that binds the current field-link preflight/interface diagnostics in addition to parity, release-gate, and shared-policy evidence; byte and SHA-256 drift now fail closed.
- Added a fresh 2026-09-28 diagnostic-bound field-link preflight. All three endpoint samples failed,
  the selected source remained `192.168.1.13` outside `192.168.144.0/24`, and the bound read-only
  interface diagnostic reported no expected aircraft-subnet address or route. The current release
  audit now binds this evidence and remains fail-closed; no network, device, MAVLink, or flight state
  was changed.
- Extended `verify_current_progress_index.py` to bind the current release-gate report and manifest
  bytes/SHA-256, blocker list, mode parity and fail-closed status directly, with regression coverage
  for report-path/hash/byte and parity-binding drift.
- Closed a release-index fail-open path: missing report/manifest artifacts now fail verification
  instead of returning a false `release_gate_report_bound`; the old 2026-09-08 strict report is
  explicitly labeled historical stale provenance in the current parity audit.
- Refreshed `docs/current-progress.json.updated_at_utc` to the current evidence index timestamp (`2026-09-20T01:12:00Z`) so the 2026-09-20 report bindings are not presented under the stale 2026-09-08 index time.
- Tightened external repeat evaluation identity and task-shape validation: missing or malformed scenario, airframe, and campaign SHA-256 identities; empty/duplicate task records; and non-finite or negative energy windows now fail closed. Full repository regression completed with exit code 0 (`5006 passed, 7 skipped, 0 failed`).
- Reject duplicate report contents (including renamed copies and cross-group duplicates) when counting shared-policy external SIM repeats. Recomputed the existing six-report comparison with six distinct SHA-256 values; this does not promote the candidate or claim statistical independence or physical validation.
- Rebound the pre-release release-gate evidence to the current release-parity identity: simulator external runtime/AI join, three-airframe PX4 reported-position matrix, and repeatability/fault campaign were regenerated and reverified. The gate now fails only on remaining replay-set, field, manual-review, HIL/REAL, hardware, and final-channel requirements rather than stale software evidence hashes.
- Captured a fresh read-only field-link preflight and interface diagnostic. The workstation has no active `192.168.144.0/24` interface and all three remote endpoints are unreachable; the evidence remains fail-closed and does not claim HIL/REAL readiness.
- Verified the configured `Ethernet` adapter with `prepare_field_link.ps1 -VerifyOnly`; no secondary address or route was changed (`changed=false`).
- Rebound the current pre-release gate to the fresh field preflight; the stale-input blocker is gone, while the real `hardware_link_ready=false` blocker remains fail-closed.
- Corrected the no-argument gate wrapper binding to the versioned 2026-09-19 field preflight; the 2026-09-04 v33 snapshot remains historical provenance and cannot silently become the current hardware input.
- Corrected the remaining no-argument wrapper defaults to the current parity-bound `-rerun` external runtime/AI and PX4 response evidence; a real smoke run now generates the report successfully and preserves fail-closed field freshness/hardware blockers.
- Refreshed the read-only field-link preflight and rebound the report-only gate at `2026-09-19T16:15:00Z`; freshness now passes, while the absent aircraft subnet and `hardware_link_ready=false` remain explicit blockers.
- Pointed the no-argument release-gate wrapper at the refreshed `field-link-preflight-20260920-current.json`; a second real smoke run completed with no stale-preflight or parity-drift error and retained the genuine hardware-link blocker.
- Verified the current portable `MultiDetectGCS.exe` artifact (`46,875,136` bytes, SHA-256 `41ea7aaf830b406aadbcbb17bd7e5e1e22c1a08f20dcba002885e0153cf6765b`) with `--simple-boot-test` exit `0`; this remains software boot evidence, not manual Fly View or HIL/REAL acceptance.

### Added

- Added the post-mission receive-window energy model to the shared-policy external SIM evidence path. The 3×3 parent/candidate evaluator now records a reproducible model-only comparison (`parent_mean=4.8322001333 Wh`, `candidate_mean=4.6188801333 Wh`, margin `0.21332 Wh`) while preserving `candidate_promoted=false`.
- Added repeat evidence for parent runs `...-b/c/d` and candidate runs `...-a/c/d`; failed candidate run `...-b` remains retained as a separate reliability record.

### Verification boundary

- The energy result is `complete_receive_window_model_only` using `causal_envelope_midpoint_model_v1`; it is not physical calibration and does not establish HIL, REAL, outdoor GNSS, or flight acceptance. Version remains `0.3.0-pre.8`; final `0.3.0` is not released.

- 2026-09-19：审计发现 `planning_energy.newly_consumed_wh` 在 Mission transaction 创建前由 `energy_projection()` 生成，只是选择时累计前缀而非完整架次能耗；v2 evaluator 已 quarantine 该指标并保持 fail-closed。新增交叉顺序 campaign，记录一次 completion telemetry 瞬时失败与成功 retry，candidate 未晋升。
- 2026-09-19：补齐 post-mission energy window 审计入口。成功 mission evidence 现在在终止 telemetry 后重放 append-only energy history，只有边界包围、boot 时间连续且无丢样本时才生成 `causal_envelope_midpoint_model_v1` 窗口值；仍显式标记为 SIM 模型值，不宣称物理校准，不授权下一任务。新增 parent/candidate 各 1 次外部 campaign，1×1 evaluator 已读到窗口值但因重复数不足保持 `promotion_ready=false`；旧 evidence 不回写。

- 2026-09-19：完成 3×3 parent/candidate 外部 PX4/Gazebo SIM 评估；6/6 gate 与 cleanup 通过，身份和 Mission path 一致，但 `completed_task_count` 为 parent/candidate 均值 `2`，未达到 candidate 正向 margin，评估器保持 fail-closed，candidate 未晋升。

- 2026-09-19：新增外部 SIM 多轮晋升评估器与预声明配置；要求至少 3×3 运行、身份/生命周期不漂移、指标完整后才允许进入收益判断。初次运行阶段样本各 1 次且完成任务数相同，评估器先行 fail-closed，candidate 保持未晋升。

- 2026-09-19：完成 aggregate router 时间预算修复后的 parent/candidate 两任务外部 SIM 重复；两次 gate、Mission frame/control、策略选择和 exact-ID cleanup 均通过。该结果只证明执行重复性与相同 Mission 路径，不证明 candidate 独立性能收益，candidate 继续未晋升。

- 2026-09-19：修复多任务外部 capture 将 aggregate router 生命周期截断为 600 秒的问题；单任务上限保持不变，多任务现在使用任务预算总和加清理余量并受 3600 秒有界配置上限保护。新增 timeout 绑定回归，未改变 candidate 晋升或最终发布门。

- 2026-09-19：追加 parent 外部 SIM 重复性审计；记录两次任务时限参数不匹配和一次 600 s router cap 到期，所有容器均精确清理。未将不完整尝试计入性能样本，candidate 仍未晋升。

- 2026-09-19：完成 parent/candidate 两任务外部 SIM 对照；不同 policy artifact 产生不同任务顺序，但各一轮样本不足以证明性能收益，candidate 保持未晋升。

- 2026-09-19：完成同一共享策略 artifact 的两次成对外部 PX4/Gazebo SIM repeatability 运行；两次顶层 gate、Mission journal、策略证据和容器 cleanup 均通过。由于未加载 learned weights 且 headless，不宣称 candidate 收益、同一 UI、HIL/REAL 或最终发布。

- 2026-09-19：完成一架次真实 pinned PX4/Gazebo disposable SIM 的共享策略 campaign；顶层 gate、Mission journal、policy evidence 和 exact-ID cleanup 通过。为修复 energy receive-window 缺口，将已有 SITL 状态流由 4 Hz 提升到 10 Hz，freshness threshold 未放宽。该结果不构成 learned candidate 晋升、HIL/REAL 或最终 `0.3.0` 发布。

### 稳定 airframe identity 修复（2026-09-19）

- 发现并修复一个会阻断跨运行复用的身份问题：旧 `airframe_profile_sha256` 把含随机端口/运行 wiring 的 `stack_plan_sha256` 纳入 hash，导致相同 VTOL 机型每次启动都产生不同策略身份。
- 新增 `multidetect.simulator.stack.airframe_profile_hash()`：保留 stack hash 在运行 evidence 中，但从稳定 airframe identity hash 排除它。两个不同端口的 Docker stack plan 已实测得到相同 airframe hash。
- Docker acceptance、VTOL capture、Mission 回归和 Ruff 通过；尚未执行完整外部 campaign，策略 artifact 的跨运行接口现在具备正确身份语义。


- 2026-09-16：外层 VTOL capture CLI/API 贯通策略 artifact 路径与文件 hash，父进程预检、子进程加载、首次审计和清理后审计使用同一策略身份；添加参数传递与启动前拒绝回归。外部运行和最终发布验收仍未完成。

### 共享自主策略契约收口（2026-09-12）

- 在不推翻 PX4/QGC、感知、规划和 Mission 主链的前提下，新增 `PolicyArtifact` 与 `SharedTaskPolicy`；SIM 预算 episode 和 HIL/REAL observed session 现在可以使用同一个 hash-bound 策略候选。
- 策略输入显式 truth-free，输出只有可行任务选择或 abstain；选择后必须重新执行现有 planner 可行性校验，策略层不获得 actuator、arm/disarm 或飞控授权。
- artifact 绑定 ranker、parent model、training dataset、airframe、Mission contract 和 safety policy 身份；新文件已进入 core/task parity，旧证据不会被自动升级。
- 训练/验证评估现在可选用同一个 artifact，并在结果中记录 `policy_artifact_sha256`；旧接口保持兼容，未因此宣称训练候选晋升。
- 定向自主规划、训练 CLI、release parity、进度文档回归通过；当前没有 learned weights、candidate promotion、动力学收益或 HIL/REAL/室外验收结论，版本保持 `0.3.0-pre.8`。
- 新增独立 synthetic holdout campaign 与 `SimulatorScenarioRunner` policy identity binding：训练/评估 input hash、scenario family disjoint；runner 对 artifact/ranker/airframe 做 fail-closed 校验，但只记录高层 provenance，不把 ranker 接到 actuator。parent/candidate 相同，`candidate_promoted=false`；证据见 `docs/evidence/shared-policy-independent-sim-20260912.json` 与 `docs/evidence/shared-policy-sim-runner-binding-20260912.json`。
- 本轮 runner/campaign 增量回归 100 项通过，重新生成当前 parity；这仍不是 PX4/Gazebo 外部 SIM、HIL/REAL、室外 GNSS 或硬件验收，最终 `0.3.0` 继续阻断。

### 时间戳来源契约与旧报告迁移标记（2026-09-12）

- 新增 `simulation/timestamp_contract.py`：明确源 `time_boot_ms`、观察端 `received_at_s`、消息发布时间和物理采样时刻的语义边界；新采集快照嵌入 schema 1 契约。
- 新增 `scripts/audit_timestamp_semantics.py`；O 原始报告 SHA 校验通过并标记为 `legacy_capture_missing_explicit_timestamp_contract`，可分析但不自动升级为同步证据。
- 契约明确 `publication_time_semantics_proven=false`、`physical_synchrony_proven=false`、`source_to_host_clock_mapping_proven=false`；69项相关回归、Ruff及diff检查通过。

### O未配对目标的姿态邻近端点诊断（2026-09-12）

- 新增 `scripts/audit_attitude_neighbor_window.py` 和 `inspect_attitude_neighbors`：记录精确源标签前后的实际姿态端点、源标签偏移、接收时间偏移及端点滚转变化，不插值、不构造中间误差界限。
- O的10条2秒固定翼较稳但未配对目标均有前后端点，源标签跨度20–24ms，最大绝对源偏移16ms，端点滚转变化最大0.499°。这只说明邻近观测平滑，不能替代目标时刻实际响应或物理同步证明。
- 63项邻近诊断/历史响应/模式窗口/文档一致性回归及静态检查通过；保存的O邻近端点摘要重新计算一致，最终版本仍禁止放行。

### O全目标覆盖与配对选择偏差（2026-09-08）

- 111项相关回归、Ruff和diff检查通过；N/O旧窗口统计及O新目标覆盖证据均从SHA绑定原报告完整复算一致。
- 目标历史分类覆盖全部设定值；忽略姿态mask单列，未匹配实际姿态不补造误差。CLI增加 `--include-target-coverage`，原默认统计保持不变。
- O的2秒固定翼目标历史窗口保留158条，其中10条目标变化较小、148条变化较大；10条较小变化目标全部缺精确实际标签配对。修正解释：缺少较稳组的实际响应证据，不代表全部目标历史均无较稳段。

### O响应参考任务外部SIM验证（2026-09-08）

- `fixed-wing-response`首次外部运行exit0，外层/Mission及独立执行准入通过，ARM到完成159.958245s，隔离容器清理无失败。
- 522条目标、6525条姿态，127对精确源标签匹配。500/1000/2000ms固定翼历史窗口保留38/37/36对，较稳组12/6/0对；2秒组无较稳样本，不宣称稳态响应改善。
- 变化组最大瞬时滚转差44.053°，保留原始证据与模式接收窗口；下一步关联路径几何和目标变化，不从单次差值调增益或降低门限。

### 固定翼响应参考任务接入共享Mission（2026-09-08）

- 采集入口及Mission执行入口新增 `--reference-profile fixed-wing-response`：北向180m、东向80m、50m半径盘旋30s；默认 `lifecycle-smoke` 保持原70m/70m、30m半径、5s参数。
- 复用原任务构造、规划、Mission编译及安全策略；profile进入运行描述哈希与Mission报告。未知profile和campaign混用提前拒绝，避免覆盖选定任务。
- 448项相关回归通过，Ruff与diff检查通过；外部SIM尚未运行，新增几何不是稳定响应或飞行可行性证明。

### N固定翼窗口筛选与复算入口（2026-09-08）

- 模式窗口/复算CLI/目标历史/四元数解码/采集/文档与版本一致性107项回归通过，Ruff及diff检查通过，保存的N诊断JSON与原始报告重新计算结果完全相等。
- 新增严格模式接收夹区筛选，覆盖目标历史及目标/实测接收时刻；模式缺失、转换和大间隔不跨越。
- 增加 `scripts/audit_mode_roll_response.py`，SHA绑定原报告、显式诊断参数、空组null误差；N复算500/1000/2000ms较稳组仅5/2/1对，不能用全程低误差宣称固定翼稳态达标。
- 明确下一步拆分生命周期冒烟与响应辨识任务；本轮不改控制增益、不启动实机或新SIM、不发布最终版本。

### N设定值变化与滚转差分组（2026-09-08）

- 本轮响应/目标解码/采集/文档/版本一致性96项回归通过，Ruff与diff检查通过。
- 新增离线 `simulation/roll_response.py`，仅用历史目标和精确源标签配对样本，禁止未来数据泄漏；显式回看窗口、最大消息间隔和目标角差条件，缺历史独立分组。
- N原报告SHA复核，500ms/300ms/2°诊断条件下：69个已观测目标较稳样本RMS1.635°，24个变化样本RMS8.910°；26.741°最大差位于变化组。1000/2000ms敏感性结果另存，不将选窗当控制验收标准，未改增益或再飞行。

### N姿态设定值外部采集（2026-09-08）

- N外部SIM exit0、完整采集/Mission及独立执行准入通过，容器已清理；326条ATTITUDE_TARGET、mask全0，无丢弃；四元数范数0.999999901..1.000000134，证据 `docs/evidence/attitude-target-n-20260908.json`。
- 新增姿态目标解码：显式范数容差与小量归一化记录、忽略mask处理、奇异点不伪造roll/yaw，72项相关回归通过。94对精确源毫秒标签匹配，232个目标不补配；最大样本滚转差26.741°为未分阶段瞬时差，不作为控制失效或验收阈值。

### M导航发布检查与姿态设定值接口（2026-09-08）

- M外部SIM exit0、完整采集/Mission门及执行准入通过，容器清理完成；扩展状态最大间隔0.409312s，无>1.5s间隔。显式4Hz请求后NAV_CONTROLLER_OUTPUT仍0条，记录于 `docs/evidence/navigation-publication-m-20260908.json`。
- 固定提交中当前固定翼模块发布新状态话题，而旧导航MAVLink依赖position_controller_status；新增ATTITUDE_TARGET接收与4Hz请求，记录源启动时间、原始wxyz四元数、type_mask和角速率/推力。有限非单位四元数原样保留，语义校验留给消费者；不伪造同步或归一化。242项相关回归通过，外部发布待验证。

### 隔离SIM状态流频率配置（2026-09-08）

- L外部SIM exit0、采集/Mission门及独立执行准入通过，容器清理完成。EXTENDED_SYS_STATE406条、最大接收间隔0.440920s、无>1.5s间隔；证据 `docs/evidence/state-stream-navigation-l-20260908.json`，不是长期稳定性证明。
- L姿态5063条、HUD406条，NAV_CONTROLLER_OUTPUT为0条。按此结果追加该实例导航流4Hz请求，待下一次外部验证发布是否可用；不把零条消息称为零误差。
- K扩展状态接收间隔81个中64个超过1.5s，最大4.578822s；原配置只将HEARTBEAT设为4Hz。现将同一指定SITL MAVLink实例的EXTENDED_SYS_STATE也设为4Hz，保留1.5s新鲜度门，不把请求频率当接收频率证明。
- 观察器与外层采集410项回归通过；配置返回错误显式失败。仅作用于既有首次能量会话SITL准备分支，不改真实飞控或生产运行profile。

### K同运行版本与失败观测保留（2026-09-08）

- K隔离SIM exit1，首次扩展状态年龄1.514203s超过1.5s；容器已清理，执行结果隔离，不作训练奖励。只读版本成功返回PX4提交83c4f4e580558816f0e398918f66358988004696，证据 `docs/evidence/navigation-capture-k-20260908.json`。
- 修复失败路径不导出位置/动态缓冲的问题：新报告增加 `failed_observer_capture`，仅错误后导出有界快照，明确partial、capture_complete=false、training_label_eligible=false；未修改门限或K旧报告。429项相关回归通过。
- 固定提交源码确认HUD空速为CAS，导航空速误差为过滤TAS减设定TAS；不混用两者反算设定值。导航高度误差是设定高度减估计高度，符号方向不同。

### 盘旋计时语义与导航输出（2026-09-08）

- 动态流/观察器/姿态关联/文档/版本一致性265项回归通过，Ruff与diff检查通过；新导航流已覆盖两种时钟下的模拟消息集成测试，尚待外部采集验证。
- 核验历史固件探针所指不可变PX4提交：固定翼LOITER_TIME按半径加导航接受半径及高度条件锁存到达后计时，不是连续精确圆环跟踪验收；不将源码解释直接当作J运行的版本实测。
- 现有只读参数快照增加同容器 `px4-ver all` 有界原始输出；动态采集增加NAV_CONTROLLER_OUTPUT的期望滚转/俯仰、高度/空速/横向误差，单位明确，不伪造源时间戳。下一次外部SIM仍需验证实际消息覆盖。

### 姿态位置精确标签关联（2026-09-08）

- 姿态关联/位置/动态采集/文档/版本一致性96项测试通过，Ruff及diff检查通过；未重复执行外部飞行。
- 新增 `simulation/attitude_position_join.py`：仅连接同源、相同启动毫秒标签的姿态/位置，拒绝截断、重复/回退时钟、非法姿态及单位；不插值或用主机最近邻补配。
- J原始报告SHA复核成功，2028对匹配，2159条位置/2160条姿态未匹配且显式保留计数。盘旋项接收窗口内121对样本：目标半径30m，中心距29.795..49.715m，最大半径偏差19.715m，最大高度偏差3.505m。证据 `docs/evidence/attitude-position-j-20260908.json`。
- 接收窗口不等于源执行阶段；等毫秒标签不证明物理采样同步。未新增通过性阈值、修改飞控或授予轨迹合格标签。

### 模拟Mission动态观测（2026-09-08）

- J固定镜像隔离SIM exit0，外层/Mission门通过，独立journal执行准入通过，容器已清理。实采4188条ATTITUDE、335条VFR_HUD及4187条位置，无截断或缺失字段；证据 `docs/evidence/dynamics-capture-j-20260908.json`。空速字段出现负值，原样保留，未升级为TAS或训练标签。
- 现有Mission观察器新增有界ATTITUDE/VFR_HUD接收记录，各流独立容量、源ID过滤、丢弃计数、非有限/缺失字段显式null；保留滚转弧度和ATTITUDE源启动时间，VFR_HUD不伪造源时间或TAS语义。
- 新增21项采集测试，并扩展两种主机时钟下的观察器生命周期集成测试；相关223项通过，Ruff通过。仅增加报告观测，不修改飞控参数或放宽验收。

### 参考航线转弯条件核验（2026-09-08）

- 本轮转弯/参数/文档/版本一致性回归82项通过，Ruff及diff空白检查通过；不是新一轮完整套件或外部飞行验收。
- 新增 `simulation/turn_envelope.py`：明确输入 TAS，以无风协调平飞力平衡计算转弯半径、滚转角和载荷因数；拒绝非有限数、非法角度、溢出和静默 CAS/地速别名。
- I 原报告 SHA 重新核验，实采参数行重新解析；30m 半径在 TAS 15m/s 时需37.408°，20m/s时需53.666°，后者超过配置的50°。CAS=TAS仅为显式数值假设，未验证大气换算、失速裕度或机体能力。
- 证据 `docs/evidence/turn-envelope-i-20260908.json`；未改飞控参数、生产规划器或验收门，仍为0.3.0-pre.8。

### 风场与阶段主线完整回归（2026-09-08）

- 完整pytest会话52219 exit0：4706项，4699通过、7因Windows符号链接能力不可用跳过、0失败/错误，443.192秒。JUnit hash及范围见docs/evidence/full-wind-phase-tests-20260908.json。
- 参数解析器在收集后新增，11项另测通过，不混入完整批次计数；未将测试通过等同SIM/HIL/REAL最终验收。

### I动态参数实采与失败隔离（2026-09-08）

- 10项固定镜像飞控参数成功只读采集；严格参数行解析新增11项测试通过，原始输出与hash绑定保存于docs/evidence/sim-dynamics-parameters-i-20260908.json。
- I完整采集门因扩展状态1.511353s超过1.5s失败，进程exit1、容器已清理；完成outcome不替代采集门，单独隔离。并行pytest负载已记录，不推断因果、不降低门限。仍0.3.0-pre.8。

### 动态参数只读快照与验收范围标记（2026-09-08）

- 隔离SIM准备阶段新增10项飞控动态参数param show，保留版本缺失/错误原始输出，不补默认，不改变参数；3项新测试、相关236项通过。新外部采集尚待验证。
- 成功flight报告明确未证明轨迹跟踪质量，区分Mission生命周期/采集门与误差界验收；仍0.3.0-pre.8，不改变原安全门或硬件配置。

### H转换几何对照与模型决策（2026-09-08）

- 新增严格接收邻域位置查询，9项新测试，位置分析共22项通过；不插值/外推或声称源事件位置边界。
- H实际转MC附近约17m高度、距盘旋中心51m，随后MC附近约9.3m高度、距落点203m且仍有水平速度。证据写入docs/evidence/transition-geometry-h-20260908.json；优先检查参考场景可跟踪性，不把转换当成只耗时不移动的阶段。未修改飞控或生产预算，仍0.3.0-pre.8。

### H外部位置采集与进近诊断（2026-09-08）

- H隔离SIM完整执行、采集与独立journal准入通过，容器已清理；实采4818条完整原始位置记录，无缓冲截断，最大源间隙0.916s单独保留。
- 新增源时间垂直速度积分诊断，13项新测试、相关80项通过。首次着陆观测距落点198.520m，44.880s源时间后首次进入5m范围，长段包含水平进近；未将其作为物理下降率标定或修改飞控。证据docs/evidence/position-trajectory-h-20260908.json，仍0.3.0-pre.8。

### 有界着陆位置证据采集（2026-09-08）

- F/G原始报告尾部对照确认位置只覆盖6.704s/8.220s，不能解释完整着陆差异；证据保存于docs/evidence/landing-tail-f-g-20260908.json。
- 新增最多32768条原始位置/源启动时间/NED速度采集，缺失字段与截断显式标记；接入observer成功报告，13项新测试与observer共199项通过，旧/QPC生命周期字段验证通过。新外部采集尚待验证，不调整飞控或生产预算，仍0.3.0-pre.8。

### G外部SIM阶段字段验证（2026-09-08）

- 新独立固定镜像QPC图像Mission G执行exit0，外层/任务/独立事务准入通过，容器已清理；实际采集97条带vtol_state的扩展状态，验证MC→转FW→FW→转MC→MC。
- 分析器增加VTOL连续观测段和旧字段缺失计数，7项新测试、相关106项通过。CURRENT仍缺序号1，阶段源时间标签未合格，不将接收跨度用于标定；结果写入docs/evidence/mission-phase-receipts-g-20260908.json。仍0.3.0-pre.8。

### Mission阶段接收观测与采集补全（2026-09-08）

- 新增CURRENT连续观测段、缺失序号、切换接收间隙及重复REACHED汇总，13项新测试；与observer相关回归共285项通过。
- F历史记录缺CURRENT序号1，不能还原全部执行阶段；原始哈希绑定结果写入docs/evidence/mission-phase-receipts-20260908.json，接收跨度不当作训练真值。
- 后续observer shadow事件保留原始vtol_state，生命周期测试通过；尚未新跑外部采集，不改写历史报告或控制行为。仍0.3.0-pre.8。

### 显式阶段化Mission时间账本（2026-09-08）

- 新增离线阶段估算：同一Mission逐项记录水平/升降/等待/转换/余量，VTOL转换切换FW/MC空速假设，保留航点与盘旋等待，显式来源必填。
- 38项新测试，相关214项通过；F历史Mission合成假设账本79.164591秒，未向观测拟合，非标定值或生产预算。证据写入docs/evidence/mission-phase-timing-20260908.json；版本仍0.3.0-pre.8。

### 编译Mission逐段风对照（2026-09-08）

- 离线模块直接消费ExecutableMission坐标项，保留序号、命令、零距离连接及哈希，拒绝隐式RTL路线；输出明确不等于总任务时间。
- 三类真实编译器与异常路径14项新测试，相关176项通过；F历史Mission四组反事实风场结果写入docs/evidence/mission-wind-connectors-20260908.json。未启动新飞行或改变生产预算，仍0.3.0-pre.8。

### 稳态风三角对照（2026-09-08）

- 新增离线航迹保持风三角求解，显式返回侧风过强/无法前进原因，不为不可行航段添加速度地板；未接入生产规划或控制。
- 74项新测试（含36组实际确定性模拟积分）及相关回归共246项通过。往返顺逆风耗时不抵消；区别空速、地速和沿航迹进度，保留生产安全预算。仍0.3.0-pre.8。

### 风场模型配对验证（2026-09-08）

- 新增17项实际确定性SIM检查：四航向的顺/逆/左右侧风轨迹、地速、同控制能耗和未解锁静止；相关回归138项通过。
- 确认侧风增加地速模长同时造成偏航迹，不能当作路线效率增益；恒定油门能耗不随风变化是当前合成模型定义，不是物理结论。未修改生产预算或安全阈值，保持0.3.0-pre.8。

### 多步预算守恒核验（2026-09-08）

- 12场景×3排序参数的36项实际episode回归证明当前线性模型没有重复扣返航预留；每轮只扣一次成本，电量/续航比例保持。
- 不修改公式或阈值；线性消耗率及保守估算的模型准确度仍待独立验证。

### 36组参数开发对照（2026-09-08）

- 保留原异质场景文件，新实验只改预先固定参数网格与开发用途说明；36组候选复用同一TaskRanker/规划/编译链。
- 实际拟合保留父模型，三个分组收益均40；训练组另一类参数将收益降至29，仅节省约27秒估算时间，不符合预定效用优先级。
- 场景此前已用于诊断，不宣称未见留出、实飞性能或模型晋升。哈希与结果记入docs/evidence/ranker-development-36-20260908.json。

### 显式尝试清单（2026-09-08）

- 新增SHA绑定清单CLI，区分dry-run、执行验收通过/拒绝及矛盾状态，拒绝重复报告/容器。
- A–F实件清单：1计划、3验收通过、2拒绝；不计算策略成功率，不宣称覆盖全部历史尝试或构成成对实验。

### 被拒绝执行记录隔离（2026-09-08）

- 新增SHA绑定失败分类CLI，将验收gate与任务结果声明分开；不将采集/验收失败转成策略负奖励。
- E实件为验收拒绝但存在已绑定completed结果声明，完成标签和奖励保留null，仅供分诊/尝试清单。
- 31项相关回归及Ruff通过；未重放E遥测、未证明根因或训练资格。

### 执行数据分组与留出泄漏检查（2026-09-08）

- 准入记录新增父/Mission一致的场景身份与去任务ID语义指纹；数据集构建重新验证三文件及真实事务。
- 重复报告/事务拒绝，已知同源组禁止跨train/validation/test；不把无已知泄漏等同于语义独立。
- F实际索引为1/0/0，没有完整留出集、不执行拟合。成功样本索引不用于估计任务成功率。

### 执行级记录准入（2026-09-08）

- 新增只读、显式三文件SHA绑定的SIM执行记录准入，复用真实事务日志验证，核对完成状态与同域事件时长。
- F实件通过，输出完成/主机接收时长标签；像素同步、物理能量及模型晋升资格保持独立。
- 229项相关测试及Ruff通过；尚未接入参数拟合，后续需场景族去重与留出划分。

### F独立窗口复核与标签需求拆分（2026-09-08）

- F时钟1101条记录、996个窗口间隔，最大148.6318ms，通过200ms诊断预算；能量接收窗口完整，合成profile条件能量包络1.613754..1.614074Wh。
- 规划重算/身份绑定审计通过；保守规划时长与主机接收时长不当成同语义误差，不自动校准。
- 架构评审区分预算比较、执行级标签、条件能量与同步像素标签，避免像素同步成为全部策略工作的无依据前置门；无训练晋升或物理性能声明。

### 失败状态见证与F轮独立探针（2026-09-08）

- Mission观察器新增有界失败诊断，异常后保留首个无效状态与消息年龄，避免初始flight占位字段被误读为实际飞行结果；408项相关回归及Ruff通过。
- 新隔离F轮通过采集门并精确清理；602个有效状态样本，三条窗口内holdout与端点仿射约束相容，sidecar一致。
- E根因仍未证明；不放宽验收门，不宣称源同步/训练资格或完整能量独立审计通过。

### 独立中段探针与失败取证（2026-09-08）

- 父进程采集一次三探针验证批次，与六端点拟合隔离；增加联合相容性验证，错误顺序/身份拒绝。
- 新隔离SIM camera E因采集飞行状态门失败，退出1并精确清理；原始失败报告保留，无训练资格声明。
- 修复失败任务丢失内存探针：采样后立即独占写诊断sidecar。248项相关回归及Ruff通过，修复后外部复跑待完成。

### 当前QPC主线全量回归（2026-09-08）

- 4416项收集，4409通过、7项Windows符号链接权限限制跳过，0失败/错误，356.711秒，退出码0。
- 全量收集后新增的独立仿射诊断8项另行通过；新旧探针组合38项及文档/parity/新诊断组合42项通过。JUnit SHA与验证范围记录在docs/evidence/qpc-current-full-20260908-v2.json。
- 不代表UI/HIL/REAL或最终v0.3.0验收通过。

### 仿射时钟假设诊断（2026-09-08）

- 新增SHA绑定的精确有理数速率约束诊断，比较固定偏移与正仿射假设，不自动校准或放行训练。
- C/D原始六探针分别复核：固定偏移不相容，仿射相容；条件速率差区间有交集，但采样间恒速及源对齐均未证明。
- 新诊断与既有探针38项测试通过；没有修改运行时算法、真实硬件或历史原报告。

### 任务窗口接收连续性（2026-09-08）

- Clock审计新增显式纳秒间隔预算，只评估任务窗口及严格边界；未提供预算、缺边界、超预算分别记录，不把审计进程成功退出当作连续性通过。
- D原始证据重审：886个窗口间隔最大173.1858ms，通过200ms诊断预算；全程986.7026ms间隔不在窗口内。未改变生产阈值或同步训练资格。
- 46项相关回归与Ruff通过，原始报告和旧审计未覆盖。

### camera D独立时钟复核（2026-09-08）

- SHA绑定复核994条Clock记录，ARM/完成接收窗口覆盖；不证明源事件时间或同步训练标签资格。
- 修正时钟迁移文档过时的当前状态，明确历史阶段记录不覆盖现状；58项文档、parity、Clock审计与循环计时回归通过。

### 带计时外部运行D（2026-09-08）

- 新隔离camera D完成并清理，能量接收窗口本次完整；分段计时与帧哈希延迟已采集，最大唯一匹配处理延迟76ms。
- C的秒级积压未复现，不把仅增加诊断后的差异声明为修复；固定偏移及训练同步资格仍未通过。

### Mission循环分段计时（2026-09-08）

- 增加有界pump/shadow/other耗时诊断，每阶段只保留最慢32区间及累计计数；绑定当前Clock身份。
- 199项相关回归通过，包含实际observer两域输出。经过时间含调度抢占，不当作CPU/网络时延；外部测量未执行，调度行为与阈值不变。

### camera C接收缺口定位（2026-09-08）

- SHA绑定路由/Collector诊断：5800唯一匹配、323歧义；停止相邻油门路由间隔0.500s而Collector间隔1.486s，后帧处理延迟1.024s。
- 全程最大唯一匹配延迟5.189s；根因尚未细分，未改变运行逻辑或阈值，未声称训练资格通过。

### 单任务Clock与能量独立审计（2026-09-08）

- Clock审计支持初验/终验双哈希绑定的单任务报告，补缺失复验/哈希篡改拒绝，60项相关测试通过。
- 外部camera C的Clock接收窗口有严格包围，但能量在24.165s处因负载过期停止；完整能量训练不合格。保留原像素门结果，未将其扩大为同步或训练有效性声明。

### 外部QPC单任务像素验收（2026-09-08）

- 新隔离固定镜像运行C完成VTOL任务并捕获3帧，像素/区间/事务/清理后复验门通过；精确容器清理及逐帧哈希复核。
- B因缺合格续航输入失败，C使用明确合成能量配置；不是物理校准。Clock固定偏移假设仍不成立，不声称同步、训练标签或最终SIM/HIL/REAL验收通过。

### QPC图像生命周期与CLI开放（2026-09-08）

- 完整外层旧/QPC生命周期fixture通过，父子时钟身份、像素、complete及清理后复验共同验证；445项相关回归通过。
- 解除QPC图像临时迁移禁用，保留所有权与状态检查。单任务Clock诊断可选，campaign仍必需；新外部Gazebo验收未执行，不声明源时钟同步或实飞通过。

### 外层像素Clock绑定（2026-09-08）

- 等待、采集起止、complete/timing和清理后复验传递同一selected_clock；父像素验收要求manifest/逐帧身份与父身份一致。
- 241项相关回归通过，含哈希有效但父会话不符的拒绝测试。QPC单任务CLI保护保持，完整生命周期及外部验收未完成。

### 采集与帧集封存Clock接线（2026-09-08）

- 像素采集函数可注入QPC Clock，backend与manifest封存使用同一身份；封存拒绝混域/跨会话，不生成错误manifest。
- 325项组合回归通过（无失败/跳过），包含实际backend的wire fixture落盘；外层运行器接线和新外部图像验收仍未完成。

### 帧集时钟消费者（2026-09-08）

- 检测/定位共用manifest到全部帧的时钟身份校验，支持显式QPC且拒绝混域与身份漂移。
- 89项相关测试通过；核心指纹更新并单独记录历史验收不匹配。外层采集与manifest生产者待接线，无新外部验收。

### QPC像素来源身份（2026-09-08）

- Gazebo backend从实际注入Clock生成QPC标签与不可变身份，JSON导出不共享可变身份状态；默认旧源格式不变。
- 50项backend回归通过；更新核心parity及历史报告对照。下游检测/定位消费者尚未迁移，不声明完整采集可用。

### 图像屏障Mission消费者接线（2026-09-08）

- observer使用自己的QPC身份验证complete；父验收比较时间前绑定ready、complete和区间样本时钟域。
- 424项相关回归通过；新增实际observer QPC路径与父验收错误域拒绝。像素provenance及检测/定位消费者待迁移，未开启新外部图像验收。

### 图像屏障时钟域校验API（2026-09-08）

- ready/complete要求调用方选择时钟域及完整身份，marker不能自行切换域。保留旧无身份monotonic读取语义，拒绝显式矛盾身份。
- 416项相关测试通过，包括QPC安全条件拒绝与身份篡改。尚未开启QPC图像CLI或完成端到端采集迁移；无新外部验收。

### QPC集成全量回归（2026-09-08）

- 全量Python套件4332项：4325通过、7跳过、0失败/错误，439.680s；固定当前核心指纹并保存JUnit哈希。
- 7项符号链接测试因Windows权限不足未验证，明确保留缺口。无新外部模拟或硬件验收，不发布最终版本。

### 连续间隔诊断与poll重复哈希优化（2026-09-08）

- 独立输出连续Collector负载间隔，不将跳过歧义帧后的匹配间隔误读为断流。
- 根据剖析复用本次snapshot哈希，等待poll不再重复序列化相同任务；无跨调用缓存。239项相关测试通过。
- 核心指纹与历史验收对照更新。根因待验证，未放宽阈值，未声明实际任务加速。

### QPC诊断实跑与终态能量验收修复（2026-09-08）

- 新campaign C完成双任务，采集12571条路由记录无截断；独立审计发现第二任务能量结束窗口不完整，修复父验收遗漏。原报告不改，当前重审失败。
- 增加SHA绑定帧关联审计，重复哈希明确歧义；观测到约1.659s路由解码后处理延迟，尚不归因socket/调度。
- 两个Clock接收窗口覆盖，但跨289.376s前后探针不兼容固定偏移模型；不提升同步/训练标签声明。406项相关测试通过。

### 路由/Collector接收诊断（2026-09-08）

- QPC campaign复用父Clock记录路由解码/转发时间和帧SHA256，最多131072条并报告丢弃数；路由控制白名单保持不变。
- Collector记录相同帧字节哈希及pump边界，异常退出清空活动诊断状态；不伪造缺失帧、不修改能源覆盖阈值。
- 400项相关测试通过；新外部证据和B缺口根因待验证，不声明发送端或内核时间。

### Campaign QPC入口与外部复验（2026-09-08）

- 新增显式host-clock/session参数，campaign父进程与子进程、Clock采集共用身份；完成/清理后重验sortie身份。默认仍monotonic，QPC图像屏障组合提前拒绝。
- 两次新隔离运行：A发现并修复实际flight终态返回漏身份；B暴露首任务心跳/油门接收年龄超过1.5s，第二任务能量覆盖检查正确拒绝。未修改旧报告或放宽阈值。
- 403项相关回归通过，两个容器精确清理；完整QPC campaign未通过，发布与硬件验收状态不变。

### 在线累计能量接收域迁移（2026-09-08）

- QPC身份验证进入共享Clock层，采集探针和能量窗口审计复用；累计SIM能量接受同域QPC历史并拒绝会话内时钟漂移身份/原点变更。
- 失败更新保持原状态、身份返回值独立复制；132项相关与212项集成回归通过。
- 更新核心指纹与历史发布证据对照；未重新声明旧证据覆盖新代码，版本保持0.3.0-pre.8。

### 能量窗口时钟身份审计（2026-09-08）

- 支持QPC窗口与flight、observation、energy history四方身份绑定；重用探针身份校验，拒绝跨域及身份降级。
- 保持原PX4 boot时间能量积分与旧报告读取；114项相关测试通过，旧Mission报告重审通过。
- 修复审计CLI从仓库外执行的导入路径；完整时钟接线与新Mission验收尚未完成，版本仍为prerelease。

### QPC探针与Clock原始记录身份绑定（2026-09-08）

采集器/长连接探针可注入共享高分辨率Clock，JSONL及报告记录source/session，等待阈值使用同域HostClockStamp；独立审计验证Mission、每行记录及探针摘要绑定。235项相关测试及6次真实容器探针通过，条件区间宽0.593672ms；不是全Mission同步或精度认证，外层运行选项与图像屏障仍待迁移。

### Mission调用链同源Clock迁移（2026-09-08）

Mission事件/等待及上传/读回截止时间使用Collector时钟，Session支持注入；终态尾段校验域身份，完成报告/窗口记录身份，初始化失败关闭连接。370项相关回归通过。默认源不变，外部探针/JSONL/采集屏障尚待迁移，高分辨率与旧屏障组合提前拒绝，未宣称整段外部验收。

### 遥测Collector显式Clock注入（2026-09-08）

TelemetryCollector的接收/elapsed/心跳/截止时间统一为注入Clock，拒绝非法读数及回退；高分辨率观测拒绝外部裸now_s，源身份写入观测与历史。362项相关回归通过，默认源与外部CLI不变；Mission上层及整段外部验证未完成。

### 显式高分辨率共享Clock与时间域检查（2026-09-08）

新增HighResolutionSystemClock、HostClockStamp，保留整数纳秒与源/会话身份，跨域相减失败；旧SystemClock/ManualClock及外部默认路径不变。16项Clock测试、90项相关回归通过。clock.py加入core parity，旧发布证据不覆盖新核心。全Mission注入迁移及新外部验收尚待完成，见docs/host-clock-migration.md。

### 长连接时钟探针及主机量化处理（2026-09-08）

探针改用有界READY/序号请求协议，兼容LF/CRLF，超时或异常清理子进程。保留逐次exec对照。实际发现Windows Python3.12 monotonic分辨率15.625ms，保留分辨率与条件误差假设，零RTT标为量化受限。相同容器对照区间259.580998ms→27.095559ms，235项相关测试通过；不是准确度提升认证、漂移验证或完整Mission验收。

### 主机/容器因果时钟探针（2026-09-08）

采集器开始/结束各执行3次固定只读monotonic查询，保留host-before/container/host-after，条件偏移区间取交集；拒绝混容器、重置、重叠、非法输出，区间不相容不生成估计。223项相关回归及6次无网络容器实际探针通过；213.842163ms区间宽不代表高精度同步。尚未新Mission外部验收，不改变历史证据。

### Mission时钟接收窗口独立审计（2026-09-08）

新增hash绑定审计CLI，校验原始Clock schema/source/topic/sequence、整数精度、时钟回退及Mission同容器绑定。缺边界返回覆盖不足，不外推；输出严格事件前后接收样本及最大采样间隙。两份实际SIM原报告重放通过，35项相关测试通过；不把接收归属区间当作源事件时间界限或训练标签。

### Mission同容器时钟与启动顺序修复（2026-09-08）

新增`--capture-clock`全窗口采集与主机接收时间、原始记录SHA绑定及清理后复核。先以topic introspection确认真实publisher，防止早期subscriber使PX4启动脚本误判已有Gazebo世界。双任务实际运行通过、2396条时钟记录、186项相关回归通过；保留两次失败证据，不声明物理标定或跨时钟对齐。

### Gazebo原生时钟只读采集（2026-09-08）

新增gazebo_clock_capture.py，绑定gz.msgs.Clock消息的sim/real/system字段及容器接收时钟，严格纳秒/回退/序列检查，固定样本与超时边界，显式unsubscribe清理。固定镜像无网络空世界20条实际采集与主机重放通过，52项相关测试通过。19ms烟测仅证明接口，不声明Mission对齐或时间标定。

### 规划执行记录的时间尺度分离（2026-09-08）

新增clock_window_comparison：在已验证覆盖窗口内，严格内部boot区间作为下界、边界区间加入上界，报告boot秒/host秒范围；覆盖不足不外推。两份tail原记录产生约0.742–0.743和0.758–0.759的接收归属比率，明确不是Gazebo实时倍率、物理同步或可校准参数。41项相关测试通过。

### 保持原完成时间的有界终态观测尾段（2026-09-08）

全量4126 passed / 7 skipped / 0 failures / 0 errors（280.230秒），exit0；34项文档/parity与Ruff/diff检查通过。

外部campaign-tail-a双任务通过，123.797/106.015秒；两项独立能量窗口重放都覆盖边界，旧u缺结束样本的问题在新记录中消除。父进程journal/清理后完整性通过，精确容器清理、exit0。能量仍为显式合成模型包络，不是硬件标定。

Mission观察器新增collect_terminal_observation_tail，默认2秒/256次pump上限，接收严格晚于原完成边界的有效PX4定位样本；post_mission_observation保存后续历史，post_terminal_tail记录结果。终态时间、duration及完成结果不挪动；超时不伪造样本或宣称能量覆盖，完整覆盖仍须重放验证。203项相关回归与170项外层回归通过。

### 规划估算与历史执行窗口只读审计（2026-09-08）

新增audit_planning_execution_gap.py：输入文件SHA、嵌入文档hash、计划重算、Mission/完成结果绑定及主机时间一致性检查，复用能量窗口重放。历史u两项估算约228秒，主机ARM到完成约104/110秒，但能量历史都缺结束边界；不推断物理误差、不校准。34项相关回归通过。原日志与发布门未修改。

### 多步参考的时间代价与配对诊断（2026-09-08）

基准v2新增Pareto前沿、估算时间增量、相同贪心耗时上限收益、初始可行数量与拒绝原因。新增12例异质优先级/预算/电量/风/任务约束矩阵，以及仅任务时长上限不同的12例配对对照。分别120→120和147→147，无收益提升；原9例同耗时收益也无提升。114项相关回归通过，包含前沿、时间代价、空可行集和配对输入一致性。没有训练、候选晋升或实际飞行。

### 多步任务顺序精确参考基准（2026-09-08）

新增benchmark_task_sequences.py，有界规模内穷举可行任务前缀，调用原规划/编译/预算模型，无启发式分数剪枝；最多6任务，节点超限报错而非声称最优。9例收益89→90，仅1例改善；记录输入身份、顺序、估算耗时、节点及运算时间。110项相关回归通过，包含独立排列验证。该结果是离线模型内参考，不替换生产决策，不证明实机收益。

### 自主决策技术路线重审（2026-09-08）

按最新要求重新分类历史约束；现有模型、算法和模块结构不是硬边界。增加连续改进/多步规划/端到端学习比较，下一步优先用精确小规模参考量化贪心差距；没有把未实现替代路线宣称为已验证更优，也不改变最终发布门。

### 冻结候选的规划输入故障评估（2026-09-08）

全量4099 passed / 7 skipped / 0 failures / 0 errors（331.461秒），exit0；34项文档/parity检查、Ruff及diff检查通过。

新增evaluate_task_ranker_faults.py，在同一TaskRanker/预算episode上运行9种固定输入故障，正常对照必须可完成，异常必须全任务精确拒绝；报告绑定数据、候选及评估器文件身份，失败返回非零，禁止覆盖。上一轮候选81项实际通过，13项相关回归通过，包含正负极端排序权重仍不能绕过约束。不是飞行中故障恢复或实机验收。

### 显式合成数据集与共享排序器训练入口（2026-09-08）

新增scripts/train_task_ranker.py及configs/training数据，复用现有TaskRanker/预算episode/规划与编译。严格输入、父候选文件SHA、分组与重复输入检查，冻结训练选择后独立评估，输出新目录禁止覆盖。实际9例数据搜索8组参数后保留父基线，train/validation/test收益26/28/35均无改进，不晋升。96项相关测试、全量4093 passed / 7 skipped通过；证据budget-training-cli-20260908。相关合成几何不代表泛化或实机训练收益。

### 同一排序器预算评估与参数搜索API（2026-09-08）

全量4086 passed / 7 skipped / 0 failures / 0 errors（204.389秒）；34项文档/parity检查及全仓Ruff通过。

预算episode新增可选ranker，仍复用原规划器/编译器；新增TaskTrainingCase、evaluate_task_ranker、fit_task_ranker。明确父候选、最多256组参数/512个case，同分保留父候选，错误不跳过。评估可拒绝训练输入复用；精确输入hash不能证明语义场景独立。89项相关回归通过；仅测试数据搜索，未训练项目候选、未证明留出收益。

### 候选排序外部双任务验收通过（2026-09-08）

u轮同一候选实际执行west→east，两项完成（103.937秒/109.422秒），候选规范hash贯穿汇总与任务记录，父进程重算选择/编译及完整journal验证通过；清理后复核通过、exit0。未训练反转排序fixture仅验证接线，不声明策略优于基线或实机通过。原始输入大小/SHA与清理证据保存在campaign-u记录。

### 外部campaign候选接线与父进程同策略复核（2026-09-08）

全量4084 passed / 7 skipped / 0 failures / 0 errors（178.929秒）；34项文档/parity检查及全仓Ruff通过。

campaign schema新增可选ranker字段，严格TaskRanker.from_mapping解析；默认缺省行为不变。候选进入原选择/编译/事务路径，汇总和每任务记录ranker_sha256；父进程以相同候选重算顺序与编译，身份或顺序不符拒绝。负向audit同样加载候选。新增明确未训练的反转排序配置，332项相关回归通过；尚未外部执行候选。

### 可选任务排序候选加载与共享会话接线（2026-09-08）

全量4074 passed / 7 skipped / 0 failures / 0 errors（185.462秒）；34项文档/parity回归和全仓Ruff通过。

新增TaskRanker数据格式和SHA绑定加载器，拒绝未知字段/重复键/非有限或布尔系数/错误base/超限文件。只对可行任务线性评分，等分保留原排序，仍经过动作重校验。共享observed session新增可选ranker并记录模型规范hash，原编译/监测/新上下文路径保留。87项相关测试通过；参数是未训练fixture，无生产权重变更。外部campaign接线及训练待完成。

### 共享任务策略观测与动作契约（2026-09-08）

全量4064 passed / 7 skipped / 0 failures / 0 errors（189.203秒）；34项文档/parity回归及全仓Ruff通过。旧发布报告经重新比对仍与当前core/task哈希不一致，发布保持阻断。

新增task_policy API：SI单位可行任务观测、严格版本化动作、默认排序适配、观测hash绑定和解析时重新验证。保持现有planner、感知模型及执行授权路径不变；None表示不选择而非飞控动作。44项规划器测试通过，core/task parity已更新。尚未加载学习型权重或接入运行循环。

### 原始能量外部累计重放通过（2026-09-08）

t轮低初始能量场景完成第一项（133.656秒）后拒绝第二项，原Mission未创建第二事务。新报告原始历史完整，audit --require-energy顺序重放两轮消耗、剩余续航和投影hash一致，精确容器清理通过。负向audit exit0，外层全部任务成功门仍false/exit1。未改源码或模型，未训练，未宣称物理电池标定。

### 拒绝能量原始输入与累计重放（2026-09-08）

全量4052 passed / 7 skipped / 0 failures / 0 errors（231.346秒），全仓Ruff与34项文档/parity检查通过。

原Mission报告保留planning_energy_input及拒绝前的planning_energy，新增audit --require-energy累计重放两轮历史，核对投影前后上下文和计算结果。322项相关测试通过；消耗篡改、历史前缀变更、上下文错配、输入缺失均拒绝。未重跑外部场景，旧s记录仍是报告级验收。

### 外部低能量连续任务拒绝验收（2026-09-08）

s轮完成第一项后拒绝第二项，保持原Mission失败退出；精确清理通过。新增audit_campaign_rejection.py，重算任务选择、上下文哈希及拒绝原因，核对事务/控制/journal/同会话证据，记录输入SHA。新增6项正向及篡改回归，相关317 passed。此为报告级负向验收，不是网络抓包证明；第二项能量原始历史独立重放仍待补齐。

### 连续任务拒绝上下文留存（2026-09-08）

全量回归 4041 passed / 7 skipped / 0 failures / 0 errors（210.942 秒）；34 项文档/parity 测试通过，五组共享哈希未变。

新增 TaskSelectionRejected，将共享选择器实际使用的上下文、哈希和候选拒绝原因带入原 Mission 失败报告。普通失败的 selection_rejection 为 null；保留原失败退出和会话失效行为。311 项相关回归及全仓 Ruff 通过。外部 PX4 低能量拒绝尚未验证，未训练或替换模型。

### 不同路线外部双任务与能量拒绝回归（2026-09-08）

新增东西镜像路线显式合成配置，测试实际wire坐标与hash不同。r轮两任务完成（92.031s/100.157s），固定home一致、累计能量保留，清理前后父进程复核通过。新增电量低于保留值/续航不足时不选择下一任务的模拟执行器回归。相关310 passed，3项新增；外部低能量负向、扰动、训练仍待验证。

### 实际同会话双任务通过（2026-09-08）

q轮两个显式合成任务依次完成，原Mission事务、共享选择/编译、固定home及累计能量保持同会话；清理前后父进程复核与精确容器清理通过。任务95.015s/120.578s；第二项规划剩余97.508046Wh。完整可用能量历史重放9719段无gap，合成耗能5.0623828Wh，非实机标定。o轮定位心跳1.515s超限；p轮发现固定18570端口错误。改为读取已验证容器动态端口并配置4Hz心跳，不改1.5s门限。307项相关回归通过。未声明相机、训练、HIL/REAL或最终发布。

### Docker连续任务接线与外部失败复测（2026-09-08）

外层新增--campaign-file，复用容器/路由/精确清理，独立campaign门不声称相机通过。清理前后重算候选顺序、编译hash及journal。新增显式双任务合成配置。m轮第一任务完成，第二任务累计能量97.0385232Wh但回读current标记失败；新增后续上传后地面/解除武装/新鲜同源保护的游标归零，不放宽wire格式。n轮第二任务能量覆盖gap拒绝；补复用会话前置配置/Docker检查期间持续采集。304项相关回归通过；最新修复未外部复测，双任务仍未通过。

### 连续任务CLI与显式配置加载（2026-09-08）

campaign模块新增CLI：复用原Mission参数，要求显式能量配置、候选文件、新输出目录，持有同一会话并写汇总。严格加载1..32项任务/runtime，拒绝重复ID、额外字段、非法schema/priority。抽取单任务CLI参数映射以复用；新增6项回归，相关297 passed。模块新进程--help通过，Docker外层入口尚未接入，未跑实际双任务。

### 连续已选任务驱动（2026-09-08）

新增scripts/vtol_selected_task_campaign.py：复用调用者的容器/会话，按每轮新上下文共享选择并编译任务，交给原Mission入口。独立任务报告和journal；门通过且同hash共享完成状态后移除任务，否则停止。新增5项模拟执行器回归，相关291 passed；尚未接到外层CLI或跑实际双任务。

### 外部Mission选择器回调接线（2026-09-08）

run_mission_acceptance新增Python selection_provider入口，在原上下文能量投影后选择并复核任务，进入原事务路径。禁止更改验收安全策略或扩大时间预算；选择阶段必须提供采集器时钟。新增4项测试，相关286 passed；测试使用共享选择器从两个候选中选择，再构造原Mission事务。尚无连续双任务外部运行。

### 已选任务编译结果绑定入口（2026-09-08）

抽取原验收事务文档绑定流程，新增build_selected_acceptance_documents；复核共享plan_task及运行几何/策略编译一致性后保留原计划、原编译结果和原上下文。新鲜度、上下文、机型hash、几何变化拒绝；新增6项测试，相关282 passed。尚未接入外部连续任务驱动，不声明双任务已运行。

### 同会话规划home绑定（2026-09-08）

全量回归4006 passed / 7 skipped / 0失败或错误；全仓Ruff通过。证据：`docs/evidence/session-home-20260908.json`。

MissionTelemetrySession固定首个合格GPS规划原点；后续上下文保留最新飞机位置，路线/围栏继续引用固定home。绑定要求健康且active会话，不发送PX4设home指令。新增4项测试，父子脚本276项通过；双任务外部验收仍待完成。

### 共享规划与显式能量外部SIM复测（2026-09-08）

外层启动器转发五项能量参数，在路径准备/Docker前复用原能量类型验证，报告保留输入配置。新增8项回归；父子脚本272项通过。k轮发现上下文时间戳取整与原始样本精度冲突，改为保留原始时间并扩展回归，不放宽新鲜度门。l轮单任务及固定翼相机采集门通过（81.859s），精确ID容器清理通过；参数为合成测试配置，未声明物理电池标定、双任务、训练、HIL/REAL或最终发布。

### 外部验收能量 CLI（2026-09-08）

验收子脚本新增五个显式能量参数，全部提供后创建累计能量会话并传入原验收路径；缺项或缺 disposable SITL 确认时在连接前拒绝，异常退出关闭会话。新增5项测试，脚本102项通过。外层 orchestrator 转发及新外部运行尚未完成，版本仍为0.3.0-pre.8。

### 外部验收复用共享规划器（2026-09-08）

外部验收规划路径纠偏：发现原脚本手工拼接 PlanStep，现改为共享 plan_task；不再用 mission_timeout 作为剩余续航。观测续航须来自1/1新鲜电池消息；可复用会话支持显式 energy_profile+initial_remaining_wh，累计状态在原上下文/计划哈希绑定前投影。新增12项回归，相关292 passed。协议测试明确1800s合成续航/600s任务预算，另保留低预算拒绝；未改共享规划阈值。尚无新外部运行/全量；CLI与orchestrator未接显式能量配置，PX4续航缺失时旧命令现在会在上传前拒绝，不能沿用j轮通过结论。

### 报告发布连续采集与会话历史容量（2026-09-08）

复用会话的成功报告写入/输出现在通过现有工作线程辅助执行，主线程持续独占接收；任务占用直到报告输出完成才释放，发布或接收异常会阻止下一任务并等待工作结束。能量历史新增显式容量：单任务默认8192不变，复用会话默认65536，上限131072；超限仍导出丢弃数量，累计重放拒绝截断，不宣称无限连续。新增9项回归，相关264 passed；尚未新外部双任务/全量回归。

### 可复用模拟任务遥测会话（2026-09-08）

外部任务生命周期拆分：MissionTelemetrySession 持有一次 loopback 连接/TelemetryCollector，run_mission_acceptance 可借用；成功后保留采集起点和历史，失败后禁止复用，连接由外层显式关闭。容器仍由原 orchestrator 管理，任务来源/所有权/协议门保留；已有证据路径拒绝覆盖。7 项新增回归、相关238 passed，包含原函数失败写报告并 SystemExit(1) 的清理验证。未运行外部双任务；报告输出期间接收与8192条历史上限仍需处理，未重跑全量。

### 模拟能量接入共享任务观测（2026-09-08）

累计能量通过 project_context 接入既有 ContextSnapshot：仅 SIM 传感器适配，复用原任务循环/planner/PX4 编译与完成监控；只替换电量和假设续航，不刷新时间戳或位置/GPS/围栏/感知质量。上下文和最后有效能量时钟样本均要求现有 1.5s 时效，拒绝未来、换会话、机型/home改变；失败不提交累计状态。10项新增回归、相关84 passed，双任务回调 fixture 已验证第二项绑定第一项后的能量快照。未进行外部连续双任务，未重跑全量、未训练；调用方仍须绑定同车会话与 collector-relative Clock，不构成来源认证。

### 累计模拟能量状态（2026-09-08）

同一外部模拟会话的累计能量状态已加入现有 energy_replay：CumulativeSimulationEnergy 复用原 replay/flight_model 功耗函数，初始 Wh 显式绑定首个完整样本，追加历史计算增量，重复输入不重复扣账。修改旧样本、缩短历史、换会话/时钟起点、截断或时效缺口均拒绝且不改变已接受状态；解除武装不补能，归零导出未满足能量需求，不声称掉电动力学。新增8项回归，相关55 passed。j记录两批重算（非两次任务）：10Wh测试初值→8.7754996→7.8331452Wh，重复新增0Wh。当前仅进程内状态，尚无磁盘恢复、实时多任务接线或补能入口；全量未重跑。

### 任务窗口能耗区间（2026-09-08）

当前全量回归 3943 passed / 7 skipped / 0 failed/errors（192.581s），Ruff/格式/文档身份检查通过。

窗口能量增量已实现：复用 SimulationEnergyProfile.consumed_wh 与因果左侧负载，严格内部区间计入下界，接触/跨越接收边界的区间计入上界；相同接收时刻也保守视为边界，不插值两种时钟。完整前缀积分与原 replay 结果交叉核对，缺失窗口端点不产生区间。18 项相关测试通过。j 原始记录重算为 2.1126148–2.1132548Wh，边界量 0.00064Wh（3 区间、0.064 boot 秒），不是全前缀 2.1668548Wh。仅在测试功耗/接收归属假设下成立，不是物理误差界限，不授权下一任务。

### 任务接收窗口外部审计（2026-09-08）

j 轮任务/画面采集与清理通过（任务 101.594s）。5584 条能量输入因果前缀 5069 区间、101.380s PX4 boot 时间无时效中断；ARM 请求至完成接收窗口 18.047–120.750s，两端由前缀样本包围（boot 11400/107404ms）。新增只读 audit_mission_energy_window.py 从原始绝对边界重算，不信嵌入通过标记，不以全前缀能耗冒充窗口能耗。7 项新增回归，相关 16 passed。物理同步、窗口能量、下一任务授权仍未声明。

### 显式任务窗口与接收时钟起点（2026-09-08）

任务时间窗口已显式记录：能量历史输出实际 collector 的宿主单调时钟起点，任务报告保存 permit 后 ARM 命令前至接受落地/解除武装观测的窗口，并保存飞行监控自身起点。缺失、非有限、布尔或倒序边界不生成相对窗口。新增 10 项测试，相关 240 passed；这只是接收时间绑定，仍不声称 PX4 boot 对时或完整能量覆盖。旧 i 轮缺少这些字段，不回填推断；需要新外部记录。

### 同步准备期间持续接收（2026-09-08）

i 轮外部任务及采集通过，4433 条输入重放 80.456s 无时效缺口；h 轮启动失败证据保留。
两次容器均清理，未声明物理标定、全任务覆盖、训练或 HIL/REAL 完成。

准备阶段接收修复：四条既有参数命令与任务文档生成在单个工作线程执行，主线程继续独占 collector.pump；参数设置完成后重新取得上下文。子进程原 10s 超时保留，任何退出均等待工作结束，防止准备操作与容器清理交错。新增 3 项线程/错误回归，相关 230 passed；未改变模型、参数值或任务安全门。此次变更后的全量回归未重跑，之前 3921 passed 属于前一版源码。

### 任务协议等待期间保留遥测（2026-09-08）

全量回归 3921 passed / 7 skipped / 0 failed/errors（197.852s），报告哈希记录于 current-progress.json。

任务协议等待已接入现有 collector.pump，上传请求、回读 COUNT/ITEM 等待期间的遥测进入同一记录器；无关消息不刷新超时、不消耗协议重试。新增 3 项回归，相关 227 passed，Ruff 通过。尚未外部重跑，不能断言已消除 g 轮 1.516s 空档，也未完成全任务能量覆盖或策略训练。

### 因果能量重放与覆盖缺口（2026-09-08）

- 现有能量 profile 提取共享积分函数，确定性模型和外部原始记录重放共用，不重复维护公式。
- 新增因果左侧状态保持与 boot 时间积分，错误来源、回退、过期、截断拒绝；新增 9 项测试，相关 112 passed。
- g 轮重放在 2.62s 后因 1.516s 接收间隙停止；不将前缀估算冒充全任务或实机能耗，未放宽时效。

### 外部模拟能量输入观测（2026-09-08）

- g 轮外部验证通过，6066 条有效同源记录、0 截断，任务完成并清理；boot 时间与宿主跨度不同，原始证据已归档。
- 现有接收器增加 HEARTBEAT/VFR_HUD/SYSTEM_TIME/GLOBAL_POSITION_INT 有界输入历史，保留独立来源/接收时刻/武装位/油门/启动时间。
- 油门严格 0–100 整数、启动时间严格 uint32，时钟复位和外来来源不被静默归并；明确未证明同步或执行能量积分。
- 新增 11 项边界测试，相关 223 passed；没有改变消息订阅、飞控指令或原任务门。

### 能量/场景增量全量回归（2026-09-08）

- 当前工作树全量 3898 passed / 7 skipped / 0 failed，203.460s；全仓 Ruff 与 diff 检查通过。
- 跳过项均因 Windows 符号链接权限不足，保留名称/原因/报告哈希，不把跳过作为安全验收通过。
- 测试期间无源码改动；未宣称外部能量、硬件或最终发布验收完成。

### 连续能量与旧故障兼容（2026-09-08）

- 修复能量 profile 下 BATTERY_STEP_DROP 固定电量覆盖造成的虚假升电；改为当前观测偏差。
- 有限故障结束恢复当前模拟电量，旧无 profile 行为保持；相关 81 passed，重新生成 parity。
- 外部状态输入仍需因果关联：相机 wire 有模拟时间但无武装/油门，不宣称外部能量接线完成。

### 场景通道能量配置接入（2026-09-08）

- vehicle.energy_profile 加入严格 runtime 输入和 JSON Schema，经现有 ScenarioRunner/ClosedLoop 加载。
- 参数进入机型身份和运行输入哈希；旧场景省略该字段时保持原有 fixture 行为。
- 新增磁盘通道加载/哈希失配/确定性及 7 组错误配置测试，相关 144 passed。
- core/task parity 重新生成，历史门保持失配；外部 Gazebo 及连续任务闭环未宣称完成。

### 现有模拟飞行模型增量能量状态（2026-09-08）

- 显式容量/航电功率/最大推进功率/来源配置，使用相同模拟 dt 积分，三机型跨架次不重置电量。
- 地面静止未武装才可显式补能，事件记录实际补入量；启用后禁止直接遥测电量覆盖。
- 电量进入原遥测，保守满功率续航与假设来源进入证据；没有物理标定声明。
- 新增 12 项测试，相关 57 passed；重新绑定核心 parity，旧报告保持失配，外部 Gazebo 接线待完成。

### 模拟能量时间序列采集（2026-09-08）

- f 轮外部验证通过：42 条电池记录、0 截断、88.578s completed，容器清理；版本源码核验发现解除武装电量复位与无电流传感器，持久能量状态待实现。
- 现有 VTOL 接收器增加最多 8192 条电池历史，记录来源/电池 ID/接收时间/电量/电流/累计耗电/剩余时间。
- 保留线协议单位和未知哨兵，导出总数与丢弃数；不把宿主时间当模拟时间，不从电量自动推导续航。
- 修复电量强制 int 转换接受布尔/字符串/小数的问题；新增 8 项回归，相关 212 passed。

### 增量决策核心发布身份绑定（2026-09-08）

- task_selection、task_session、mission_outcome、task_episode 纳入共享核心 parity。
- 修复规范清单测试的遗漏盲点，增加 4 个模块内容变更使核心身份失效的回归。
- 保留 v196/v197 历史证据，新建当前失配比较，四模式旧核心证据均不可充当当前验收；未提升发布状态。

### 共享结果监控器新外部验证（2026-09-08）

- e 轮 PX4/Gazebo 任务与采集门通过，340 条事件、116.234s completed；1.140s 顺序差异在既有时效内确认。
- 飞后电量 72%，续航缺失仍停止后续任务；保留原报告、输入哈希和精确容器清理证明。
- 模拟电池参数只读记录，未推断真实能耗；未训练新权重、未完成连续任务或提升发布状态。

### 确认时效统一与全量回归（2026-09-08）

- d 轮外部验证暴露 406ms 顺序差异，原 250ms 超时；改为共享 1.5s 反馈时效预算，到期仍拒绝。
- c/d 实际事件重放通过，修改后相关 99 passed；修改前全量 3862 passed / 7 skipped / 0 failed。
- 原 d 轮失败旁路保留，容器清理；最新代码的新外部运行与剩余续航来源仍待完成。

### 有界 Mission 消息确认（2026-09-08）

- 结果监控层暂存一条领先 1 项的同源 reached，250ms 内由 current 确认，重复不延时；严格跟踪器不改。
- 独立保存原接收/确认处理时间；4 项新增测试，反馈及适配 74 passed。
- c 轮真实 285 事件离线重放 completed，原失败报告保留；新外部运行及续航来源验证待完成。

### 后任务遥测来源与外部时序问题（2026-09-08）

- 导出 GPS/电池时戳、同源检查、PX4 time_remaining；缺失续航不以任务时长替代，新增 6 项回归。
- 外部 c 轮任务门通过，但旁路暴露 reached 先于 current 的顺序拒绝；电量 50%、续航缺失。
- 相关 227 项测试通过，容器清理；连续任务/训练仍未完成，保留原始失败证据。

### 观测驱动任务循环（2026-09-08）

- 新增共享 task_session，复用选择、编译、监控器，以后端返回的有效新快照推进任务，不推演观测电量。
- 新增 7 项回归，mission planning 共 23 passed；异常反馈/快照停止后续任务，不增加执行权限。
- 外部多任务后端接线待完成，未训练权重或提高发布状态。

### 外部 VTOL 旁路验证与帧清单兼容修复（2026-09-08）

- 修复像素验收只接受帧清单 v1、拒绝当前生产器 v2 的不兼容，保留像素哈希/内容/来源校验；新增版本负向测试。
- headless 外部模拟第二次任务与采集门通过，3 帧、269 条旁路事件、86.421 秒完成；离线重放结果一致。
- 首次失败报告保留，两次容器清理成功，未启动 QGC/实机，未训练模型或提升发布状态。

### VTOL 接收循环旁路集成（2026-09-08）

- 在现有接收循环接入共享任务结果监控，绑定实际编译 Mission；报告保存消息字段、时间和判定。
- 旧验收/控制逻辑不变，错误路径保留旁路证据；28 项适配回归通过，尚未进行新外部 Gazebo 运行。

### 原始 MAVLink 生命周期适配（2026-09-08）

- 结果监控器接收 HEARTBEAT/EXTENDED_SYS_STATE，严格字段、同源与独立时戳检查，清除末项到达前缓存。
- 新增 11 项适配测试，反馈回归累计 36 passed，含真实 pymavlink 编解码；未启动外部模拟执行。

### 共享只读任务结果判定（2026-09-08）

- 新增 MissionOutcomeMonitor，复用既有 PX4 反馈与 Clock，完成需末项到达及其后同源新鲜落地/解除武装。
- 超时/失败锁定，缺失/过期反馈不计成功；新增 10 项测试，反馈模块 25 项通过。
- 未接入外部 Gazebo 事件适配器或实机，未将预算模拟升级为执行验收。

### 任务预算模拟循环（2026-09-08）

- 将现有任务选择增量 API 接入多轮 RETURN 预算模拟，重用 ManualClock 和标准 PX4 Mission 编译。
- 每步更新快照/位置/时间/电量，余量不足时停止；保留编译绑定和拒绝原因，错误输入失败。
- 新增 5 项测试，mission planning 回归 16 项通过；耗电为未标定近似，非 Gazebo/实机执行证据。

### 共享 planner 增量任务选择基线（2026-09-08）

- 新增离线 `select_next_task` API，复用现有校验、距离/耗时估算和计划；保留拒绝原因，优先级不能覆盖安全门。
- 添加确定性、计划兼容、排序和失效拒绝测试，planner 回归 32 项通过；未替换权重、未训练或接入实机。
- 尚待模型加载、模拟循环、UI 和发布 parity 接入；排序不是可直接执行的联合多任务航程。

### 模拟任务策略训练启动（2026-09-08）

- 补充用户增量约束：扩展现有模型和 planner，不另建平行系统；记录父权重身份、旧功能回归、新增能力评估和回滚要求，尚未实现/训练新增策略。
- 用户选择自主任务决策与航线策略，要求 SIM 与实际部署使用同一个策略模型；记录共享契约与实施顺序。
- 当前 CUDA 可用，PX4/Gazebo 新运行采集 60 帧通过；未训练候选模型，未提高实机/发布验收状态。

### v0.3.0-pre.8 v198 field readiness（2026-09-08）

- 当前主机只读预检：三端点超时、源地址不在现场子网、串口为空；UDP 端口可绑定但不构成远端连接证据。
- 核查部署 replay 输入缺口，保存预检和有界文件名清单结果；待真实设备/数据输入，不更改网络或放宽发布门。

### v0.3.0-pre.8 v197 current E7 integration（2026-09-08）

- 由源文件生成四模式 envelope，manifest writer 集成 v196 当前 SIM 报告；SIM envelope 与 combined 源统一。
- E7 strict exit 2，gate/release false，诊断从 45 降到 32；四模式五组 parity 绑定当前源码，未提升任何人工/实机状态。
- 历史报告保留，新审计摘要记录 manifest、report、strict 的字节身份和完整剩余原因。

### v0.3.0-pre.8 v196 SIM source recomputation（2026-09-08）

- 由当前生成器重算三机型 PX4 响应矩阵、6 正向/9 故障 campaign、六源 external runtime/AI join，并复核通过。
- 核对源文件与历史绑定哈希一致，保存新报告与可移植源清单；没有覆盖历史产物，没有进行新实机/模拟器运行。
- E7 完整输入集成仍待执行，保留 v195 发布阻断状态；不得将派生报告通过视作最终发布通过。

### v0.3.0-pre.8 v195 release evidence audit（2026-09-08）

- 当前 release evaluator 对最新保存的 v176 manifest 重算，记录 45 条阻断诊断和四模式三组过期 parity；
  修正当前进度中固定 31 和历史报告误标 current 的问题，不更改历史证据或放宽发布门。
- 增加可移植审计摘要及来源一致性测试；有本地原报告时核查哈希、原因和模式差异。
- 聚焦回归 114 passed、无跳过/失败；lint、format 和 diff 检查通过。运行时代码及 EXE 未变。

### v0.3.0-pre.8 v194 PX4 receipt relocation（2026-09-08）

- 修复回执搬运后仍解析原始绝对路径的问题：按角色验证相对路径与同一历史根目录的对应关系，
  仅在目标目录重开文件；原始 summary 和 receipt 字节无需改写，schema v1 保持兼容。
- 显式 `--base-directory` 下，相对 receipt 参数也以该目录解析；支持 Windows/POSIX 历史来源路径。
- 新增原目录离线、六角色缺失/等长篡改、来源错配与模式拒绝测试。该回执只证明字节完整性和摘要声明，
  不是签名认证或源文件语义重验；HIL/REAL、飞行与最终发布门保持未通过。
- 聚焦回归 30 passed；全量 3,805 passed、7 skipped、0 failed（3,812 tests）。七项符号链接测试在当前
  环境未执行；JUnit、CLI 搬运验证和 parity 身份记录于 `docs/current-progress.json`。

### v0.3.0-pre.8 v193 PX4 capture receipt closure（2026-09-08）

- `multidetect.px4_runtime_capture` 新增 `--capture-summary-out`；Windows
  `scripts/run_px4_runtime_capture.ps1` 成功完成 capture 后自动生成 summary 和 receipt。
- 新增 `src/multidetect/px4_runtime_capture_receipt.py`、严格 schema、创建/验证 CLI 与回归：固定列出
  Mission、RuntimeObservation、RuntimeProfile、Mission feedback、endpoint/vehicle identity、join 六个文件，
  重算路径、字节数和 SHA-256，拒绝越界、符号链接、重复、篡改或未通过的 capture gate。
- receipt 只记录 receive-only software capture / completed join，所有 hardware、HIL/REAL、室外 GNSS、人工视觉、
  飞行和 release claims 保持 false。v193 聚焦回归 `39 passed`，全量回归 `3,787 passed, 7 skipped, 0 failed`
  （3,794 tests，203.18 秒）；当前 parity 与 JUnit 哈希见 [`docs/current-progress.json`](docs/current-progress.json)。

### v0.3.0-pre.8 v192 Windows PX4 runtime capture wrapper（2026-09-08）

- 新增 `scripts/run_px4_runtime_capture.ps1`：从 `PSScriptRoot` 稳定解析仓库，自动选择 QGC venv、仓库 `.venv`
  或 PATH Python，一条 PowerShell 命令调用统一的 `multidetect.px4_runtime_capture` sidecar capture/join。
- 转发 HIL/REAL、串口/UDP endpoint、边界参数、`AUTOPILOT_VERSION`/ranging 请求、三个 sidecar 输出路径和
  覆盖策略；REAL/HIL signing identity 组合先在 wrapper 层 fail-closed。
- HMAC/signing 只传环境变量名，不把密钥原文放进 PowerShell 参数或报告；恢复调用方 `PYTHONPATH`，不触碰网卡、
  路由或飞控输出。wrapper 已加入 PX4 Mission release parity，新增 parser/契约/密钥边界回归。
- v192 聚焦回归 `96 passed, 1 skipped`（97 tests），全量 Python 回归 `3,783 passed, 7 skipped, 0 failed`
  （3,790 tests）；JUnit、耗时与 SHA-256 记录在 [`docs/current-progress.json`](docs/current-progress.json)。
- 本轮仍是 field tooling 与 software join 完善，不改变真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、
  飞行或最终 `v0.3.0` 发布门状态。

### v0.3.0-pre.8 v191 PX4 receive-only runtime sidecar capture（2026-09-07）

- 新增 `src/multidetect/px4_runtime_capture.py` 与 `multidetect-px4-runtime-capture`：在一个 bounded
  `PixhawkReadOnlyTelemetryProvider` session 中生成 PX4 Mission feedback、endpoint/vehicle identity sidecar，
  并立即重跑 v190 live observation / Mission / RuntimeProfile 五源 join。
- 将发送总数拆为 `telemetry_request_messages_transmitted`、`application_messages_transmitted` 和
  `flight_control_messages_transmitted`。有界 `AUTOPILOT_VERSION`/stream/range 请求可以被记录；应用或飞控输出
  仍保持 fail-closed，旧报告继续兼容并按保守规则解释。
- 新增 `configs/schemas/px4-runtime-capture.schema.json`、确定性证据生成器与 v191 回归；正向 telemetry-only
  capture 通过，负向 application output 同时关闭 feedback 与 join gate。当前证据为 in-memory software fixture，
  不代表真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、飞行或最终 `v0.3.0` 发布。
- v191 聚焦回归 `53 passed, 1 skipped`，全量 Python 回归 `3,779 passed, 7 skipped, 0 failed`
  （3,786 tests）；JUnit 与 SHA-256 详见 [`docs/current-progress.json`](docs/current-progress.json)。
- 刷新 `configs/release-parity.json`，将 v191 producer、schema 和 evidence generator 绑定到 PX4 Mission path。

### v0.3.0-pre.8 v190 live RuntimeObservation × PX4 Mission identity join（2026-09-07）

- 新增 `multidetect.live_runtime_observation_mission_binding` 与安装式
  `multidetect-live-runtime-observation-mission-bind`：显式重开 live `RuntimeObservationResult`、
  `RuntimeRunReport`、可执行 PX4 Mission、v2 Mission feedback、签名 `RuntimeProfile` 和
  endpoint/vehicle identity sidecar，逐项验证 canonical hash、模式、heartbeat、endpoint/baud、airframe、
  profile signature、receive-only 与 `output_locked`。
- 新增 `live-runtime-observation-mission-binding.schema.json`、
  `runtime-endpoint-vehicle-identity.schema.json`、v190 确定性 evidence generator 和负向 mode-drift
  fixture；25 项 join 检查通过，schema errors `0`，定向回归 `145 passed, 2 skipped`。路径/符号链接/重复
  source/身份或输出漂移均 fail-closed，HIL/REAL HMAC key 不写入报告。
- 全量 Python 回归为 `3770 passed, 7 skipped, 0 failed`（3,777 tests，180.87 秒），JUnit 记录在
  `artifacts/evaluation/terminal-continuation-v190/full-rerun-v190-final.xml`；README 文档大小门与 release
  parity 测试均通过。
- 已重新生成 `configs/release-parity.json` 并将新模块、schema、脚本绑定到 PX4 Mission path。v190 仍是
  software sidecar evidence；portable EXE 未重编译，真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、
  实飞和最终 `v0.3.0` 发布门保持原状态。

### v0.3.0-pre.8 v189 profile-bound live RuntimeObservation producer（2026-09-07）

- `LiveMissionRunner.from_runtime_backend` 现在直接绑定 live producer 的 `RuntimeRunReport` 和
  `RuntimeObservationResult`：报告使用实际 descriptor/input hash、逐帧 ID、共享时钟和有界/自然终止信息；
  无界流被人工停止时不伪造完成报告。
- `multidetect live-camera` 新增 `--runtime-report`、`--runtime-observation-result`、
  `--runtime-observation-include-hash`，仅接受签名 profile-bound HIL/REAL runtime，并在写文件前运行
  receive-only 安全门。
- Pixhawk telemetry provider 将兼容总发送数与 telemetry-request、application、flight-control 计数分离；
  已知应用/飞控输出、可控硬件或飞控能力开启时，typed observation result 会 fail-closed。
- 新增 `scripts/generate_runtime_observation_live_producer_evidence.py` 与 live/Pixhawk/CLI 回归；软件
  HIL pair schema errors `0`、round-trip `true`、binding gate `true`，负向应用输出 guard 通过。
  聚焦 `307 tests / 306 passed / 1 skipped`，全量 `3770 tests / 3764 passed / 6 skipped / 0 failed`。
- 本轮更新 release parity 并记录 v189 evidence；portable EXE 未重编译，真实 Pixhawk、室外 GNSS、HIL/REAL、
  人工 Fly View、实飞和最终 `v0.3.0` 发布门保持原状态。

### v0.3.0-pre.8 v188 RuntimeObservation builder CLI（2026-09-07）

- 新增安装式 `multidetect-runtime-observation-build`：从已有 `RuntimeRunReport` sidecar 和明确的
  `hil|real` 模式生成 canonical receive-only result，可选 `--include-hash` 写入 result `sha256`，不改写 sidecar。
- CLI 复用严格 `RuntimeObservationResult` parser，错误返回非零；v188 HIL/REAL pair 均完成 schema、
  round-trip 和 `runtime_report_binding` gate，新增 CLI evidence generator 与回归。
- builder 的 `messages_transmitted=0` 仍只表示该 result 的应用/飞控输出锁；Pixhawk telemetry-request
  capture 保持独立计数。本轮不重编译 portable EXE，不改变硬件、室外 GNSS、HIL/REAL、人工 Fly View、
  实飞或最终 `v0.3.0` 发布状态。

### v0.3.0-pre.8 v187 typed RuntimeObservationResult producer entry（2026-09-07）

- 新增 `multidetect.runtime_observation.RuntimeObservationResult`：从唯一的 `RuntimeRunReport` 派生
  HIL/REAL result 的事件、模式、descriptor/input hash、逐帧身份、数量、bounded termination 和内嵌
  canonical report，避免真实 producer 手抄字段造成漂移。
- 新增 `write_runtime_observation_pair`、`load_runtime_observation_result` 与
  `runtime-observation-result.schema.json`；builder 固定 receive-only 输出锁，显式区分 telemetry request
  capture 与应用/飞控发送计数，顶层/嵌套报告篡改 fail-closed。
- 新增 v187 builder evidence generator 与 9 项 producer 回归；HIL/REAL 软件 pair schema errors `0`、
  binding 通过，`runtime_input_hash` 篡改负向门关闭。本轮不重编译 portable EXE，不改变硬件、室外 GNSS、
  HIL/REAL、人工 Fly View、实飞或最终 `v0.3.0` 发布状态。

### v0.3.0-pre.8 v186 HIL/REAL RuntimeReport observation adapter（2026-09-07）

- `multidetect.runtime_report_binding` 现在识别两个显式事件：
  `hil_runtime_observation_finished` 与 `real_runtime_observation_finished`。适配器重开
  `RuntimeRunReport`、descriptor/input hash、逐帧 ID、bounded termination 和内嵌 canonical report，
  不再让 HIL/REAL 结果落入未知事件分支。
- HIL/REAL 结果必须同时满足 `observation_only=true`、`read_only=true`、`receive_only=true`、
  `messages_transmitted=0`、`flight_control_enabled=false`、`hardware_outputs_present=false` 与
  `output_locked=true`；新增 `output_safety_contract` 检查，任何发送计数或模式漂移都会 fail-closed。
- 新增 `scripts/generate_runtime_report_binding_observation_evidence.py`，可重复生成四模式 `4/4`
  软件绑定、REPLAY/SIM 子集和发送计数篡改负向证据；绑定 schema 扩展为 14 项单绑定检查，schema errors
  均为 `0`。新增 HIL/REAL 正向、篡改、事件模式漂移、终止字段缺失及集合覆盖回归。
- 本轮仍只证明 software runtime identity 与 receive-only contract；生成的 HIL/REAL 文件是接口 fixture，
  不代表真实 Pixhawk、室外 GNSS、硬件、HIL/REAL、人工 Fly View、实飞或最终 `v0.3.0` 发布通过；portable
  EXE 未因 Python/schema-only 改动重编译。

### v0.3.0-pre.8 v185 RuntimeProfile → PX4 Mission feedback 六源绑定（2026-09-07）

- 新增 `multidetect.px4_mission_feedback_profile_binding` 与
  `multidetect-px4-mission-feedback-profile-bind`：显式重开 v184 四模式 RuntimeProfile sidecar 集合，
  再调用既有 `px4_mission_feedback_binding` 重开 HIL/REAL × fixed-wing/multirotor/VTOL 六源。
- Joiner 按模式策略核对 profile kind、HIL/REAL signer `key_id`、signature/identity/profile validity、
  receive-only、`flight_control_enabled=false` 与 `output_locked=true`；通用 mode profile 不冒充六个 airframe
  文件，六源 reviewer 仍逐叶重算 Mission/profile/report 哈希和身份。
- 新增 `px4-mission-feedback-profile-binding.schema.json`、成功/不完整/错误三项回归和 CLI 错误 schema；
  本轮仍是 software evidence consumer，不启动 runtime、不连接 PX4、不发送消息，不改变 E7、硬件、室外 GNSS、
  人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 v184 RuntimeProfile sidecar binding set（2026-09-07）

- 新增 `multidetect.runtime_profile_binding_set` 与 `multidetect-runtime-profile-set-verify`：显式重开
  REPLAY/SIM/HIL/REAL RuntimeProfile JSON，固定模式顺序检查 profile kind、签名/`key_id`、endpoint/vehicle/
  firmware/airframe/signing identity 和锁输出策略。
- HIL/REAL HMAC key 只接受内存映射或环境变量，报告只保留 key id、source hash 与布尔结果；重复 mode/source、
  越界或符号链接、空文件、损坏签名和身份漂移均 fail-closed。新增 `runtime-profile-binding-set.schema.json`
  和 9 项定向回归（8 passed、1 Windows symlink 权限 skip）；默认四模式仍在缺少现场 profile 时保持
  `complete=false`，全量 Python 回归为 `3740 passed, 6 skipped, 0 failed`（3,746 tests），不改变 E7 或硬件/HIL/REAL/飞行声明。
- v184 仍为 Python-only software preflight，portable EXE 未重编译；真实 PX4 Mission feedback 六源、人工 Fly View、
  室外 GNSS 和最终 `v0.3.0` 发布门保持原状态。

### v0.3.0-pre.8 v183 multi-mode RuntimeReportBinding set（2026-09-07）

- 新增 `multidetect.runtime_report_binding_set` 与
  `multidetect-runtime-report-set-verify`：显式绑定多个 runtime mode 的 result JSON 和
  `RuntimeRunReport` sidecar，稳定按 `REPLAY → SIM → HIL → REAL` 排序并重跑每条 13 项单绑定检查。
- 集合层拒绝缺失 required mode、重复 mode、结果/sidecar 跨模式复用、路径越界和声明 mode 漂移；缺少
  HIL/REAL 时报告保持 `complete=false` / `gate_passed=false`，不提升任何硬件或实飞声明。
- 新增 `runtime-report-binding-set.schema.json`、9 项定向回归和真实 v181 integrated Replay + v183
  SIM 2 帧集合证据；集合 schema errors 为 `0`，required-mode 可包含额外已绑定模式，空文件/符号链接
  source fail-closed；当前版本仍保持 `v0.3.0-pre.8`，E7 仍阻断。

### v0.3.0-pre.8 v182 schema-stable runtime binding errors（2026-09-07）

- `multidetect-runtime-report-verify` 的坏输入路径现在输出 `runtime_report_binding_error`：完整 13 项检查、
  `result/runtime_report/expectations=null`、结构化 `error.type/message`、稳定 reasons 和退出码 `1`，便于 CI/审计
  统一消费而不会把异常文档误当成功结果。
- `runtime-report-binding.schema.json` 同时约束成功与失败文档；新增损坏 JSON schema 回归，v182 绑定错误证据的
  schema errors 为 `0`，44 项定向回归通过。
- 全量回归为 `3724 passed, 4 skipped, 0 failed`（3,728 tests）；本轮仍为 software-only 证据，不提升 E7、室外
  GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞验收状态。

### v0.3.0-pre.8 v181 integrated Replay result-shape compatibility binding（2026-09-07）

- `multidetect.runtime_report_binding` 同时支持当前 integrated Replay/QGC 的嵌套结果和历史真实产物的顶层
  `processed_frames`、`frame_ids`、`natural_stream_exhaustion` 字段；两种格式都重新核对 descriptor/input hash、
  处理帧数量/身份、自然耗尽和无飞控/硬件输出边界。
- 对结果声明的帧 ID 执行严格类型、空值和重复校验，malformed ID fail-closed；新增 2 个兼容/篡改回归，定向
  `43 passed`，并用 v178 真实 2 帧 integrated Replay sidecar 生成 v181 `gate_passed=true` 绑定证据。
- 全量回归为 `3723 passed, 4 skipped, 0 failed`（3,727 tests）；本轮仍为 software-only 证据，不提升 E7、室外
  GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞验收状态。

### v0.3.0-pre.8 v180 runtime result/report binding verifier（2026-09-07）

- 新增 `multidetect.runtime_report_binding` 与 `multidetect-runtime-report-verify`：只读重新打开结果 JSON 和
  `RuntimeRunReport` sidecar，严格校验事件适配器、runtime mode、descriptor/input hash、处理帧数量/ID、终止
  合同、integrated Replay/QGC 内嵌报告，以及 SIM 的 `truth_free`/`closed`；未知事件 fail-closed。
- `SimulatorScenarioRunResult.to_mapping()` 公开 sidecar 使用的 `runtime_input_hash`，避免审核者只凭 manifest
  hash 或重新构造输入。绑定 schema 独立于 `px4_mission_path` parity，不改变 QGC C++/QML 或飞控路径。
- `river_rescue_vtol_001` 2 帧绑定证据与 41 项定向回归通过；全量回归为 `3721 passed, 4 skipped, 0 failed`
  （3,725 tests）。本轮仍只证明 software/SIM 记录身份，E7、室外 GNSS、真实硬件、HIL/REAL、人工 Fly View
  和实飞门保持阻断。

### v0.3.0-pre.8 v179 integrated Replay/QGC RuntimeRunReport parity（2026-09-07）

- `integrated-replay-qgc` 回归现在使用真实 `RuntimeRunReport` 对象，先完成本地 RTSP/metadata 同步 ACK，
  再通过 `--runtime-report` 写入 sidecar，并校验结果内嵌报告与 loader round-trip 完全一致；报告继续绑定
  frame ID、descriptor/input hash、自然耗尽和 `max_frames=null`。
- integrated Replay/QGC/Live/Runtime 定向回归保持 `140 passed, 0 skipped, 0 failed`，全量回归为
  `3711 passed, 4 skipped, 0 failed`（3,715 tests）；该轮只加强 QGC software 证据，不改变 QGC C++/QML
  二进制，也不提升室外 GNSS、真实硬件、HIL/REAL、人工视觉或实飞门。

### v0.3.0-pre.8 v178 integrated Replay natural-exhaustion report（2026-09-07）

- `RawSensorReplayBackend` 的有限帧源现在使用独立的 `FrameSourceExhausted` 信号；`LiveMissionRunner`
  将其记录为 `camera.stream_exhausted`，不会把相机读取异常吞成成功结果；直接 `read()` 的历史
  `RuntimeError` 兼容性保持不变。
- `IntegratedReplaySession` 现在核对真实消费的帧 ID 顺序、完整 recording 数量、descriptor/input hash
  和自然耗尽状态，并生成与 SIM/HIL/REAL 共用的 hash-bound `RuntimeRunReport`；固定 `max_frames ==
  frame_count` 只作为兼容输入，绑定时会归一化为自然耗尽，报告 `max_frames=null`、`complete=true`。
- `integrated-replay` 与 `integrated-replay-qgc` 新增 `--runtime-report <path>`，结果 JSON 同时嵌入
  canonical report；v178 fixture sidecar 与 round-trip verifier 已落在
  `artifacts/evaluation/terminal-continuation-v178/`。该更新只增强 Replay/QGC software 证据，不改变
  QGC C++/QML 二进制，也不提升室外 GNSS、真实硬件、HIL/REAL、人工视觉或实飞门。

### v0.3.0-pre.8 v177 SIM RuntimeRunReport sidecar（2026-09-07）

- `SimulatorScenarioRunResult` 现在可以导出与 Replay/HIL/REAL 共用的 `RuntimeRunReport`：场景 manifest runtime
  channel、runtime descriptor、处理帧 ID、共享时钟和 frame budget 均进入同一 hash-bound 报告；
- `multidetect.simulator.scenario_runner` 新增 `--runtime-report <path>`，以原子 JSON sidecar 保存报告并可由
  `load_runtime_run_report()` 重新校验；固定帧预算消耗仍保留 `frame_budget_complete` 场景语义，但 sidecar 明确
  `stream_exhausted=false` / `termination_reason=frame_limit_reached`，避免把有限 SIM 前缀误报成自然完整流；
- v177 CLI 产物和 tamper/round-trip 回归已加入进度记录；该更新不改变 QGC C++/QML 二进制，也不提升室外 GNSS、
  真实硬件、HIL/REAL 或实飞门。

### v0.3.0-pre.8 v176 deterministic runtime completion report（2026-09-07）

- `AutonomyRuntime.run_with_report()` 现在区分自然耗尽的完整流与 `max_frames` 截断前缀，报告记录处理帧数、时长、
  稳定终止原因和 descriptor/input provenance；
- 新增严格 `runtime-run-report` JSON schema、canonical SHA-256、原子写入/加载及篡改回归，schema 已纳入
  `px4_mission_path` parity；
- 三机型 external/PX4 派生证据已按新 parity 重绑，完整回归为 `3708 passed, 4 skipped, 0 failed`（3,712 tests）；
  该更新只增强 Replay/SIM 运行记录与 fail-closed 证据边界，不改变室外 GNSS、硬件、HIL/REAL 或实飞门状态。

### v0.3.0-pre.8 v175 one-click replay summary export and parity rebind（2026-09-07）

- Fly View 回放弹窗新增“复制全量摘要”，在 native 完整性校验通过后复制 bundle/session、事件与质量统计、捕获时长、媒体字节和完整性状态；该操作只写剪贴板，不触发地图、目标、Mission、飞控或物理释放；
- Release/portable EXE 重链并通过 native self-test、offscreen simple boot 与 45 项回放/QGC 定向回归；EXE 为 46,875,136 bytes，SHA-256 `41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`；
- `release-parity.json` 更新为 SHA-256 `1C937FFDD4D0D59267A5DF79A240E8101431613CAD0BE17533B2FFF6ECC33EBC`，三机型 external/PX4 派生证据与 report-only E7 已在 `artifacts/evaluation/terminal-continuation-v175/` 重绑；E7 仍 `gate_passed=false / release_allowed=false`，版本保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v174 full replay quality summary and parity rebind（2026-09-07）

- `TargetEventReplayBundle` 与 native replay store 对完整事件流计算可定位、有效测距、有媒体、唯一目标、捕获时长和媒体字节统计；Fly View 同时呈现筛选匹配总数与最多 512 条的当前显示数，避免长回放把截断列表当成完整结果；
- C++ self-test、Python/native 跨语言加载、QGC 静态契约、portable offscreen boot 和完整 Python 回归通过；portable EXE 为 46,875,136 bytes，SHA-256 `4E468DD936CD7F938E7373B0500294B12DDA9E1942FA9D9412D8803AB49E457A`；
- `release-parity.json` 已按本轮 native/QML 变化重算（10,129 bytes，SHA-256 `01001D1DFB2B043D03DAAD50CC88DABC669B90831FDA760D08C25937FF20836D`），SIM/PX4 派生证据和 report-only E7 已在 `artifacts/evaluation/terminal-continuation-v174/` 重新绑定；E7 仍为 `gate_passed=false / release_allowed=false`，版本保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v173 current parity rebind and E7 report-only（2026-09-07）

- 以当前 `release-parity.json` 重新生成并独立复核三机型 external runtime/AI join、PX4 `GLOBAL_POSITION_INT`
  response matrix 与 repeatability/fault campaign；external、matrix、campaign gate 全部通过，source/canonical hash
  和字节数均已绑定到当前 parity；
- report-only E7 在 `artifacts/evaluation/terminal-continuation-v172/release-gate/` 重算，manifest/report 为
  23,729/73,786 bytes，SHA-256 `6E1D31983EF6C74155D9E6FD818BD86B2CDD7956C32D33A8000F0A3BAAEC8A52` /
  `1913AD13F27157434F0D71AC574E2E9F58090F233BE55F54088E93BCDA078234`；结果仍为
  `gate_passed=false / release_allowed=false`，32 条详细 blocker；
- 这是证据身份重绑，不代表室外 GNSS、硬件、HIL/REAL、人工视觉或实飞通过；版本仍保持 `v0.3.0-pre.8` prerelease。

### v0.3.0-pre.8 v172 target-event replay evidence review（2026-09-07）

- 回放查看器现在把已校验的媒体 `kind`、相对路径、encoding、bytes、完整 SHA-256 和可选宽高投影到事件明细；
  `source_frame_id`、机体 AGL 与航向等可空元数据也在 C++ 侧按范围严格校验，拒绝空字符串、非有限数和越界值；
- Fly View 回放弹窗新增“全部/可定位/有效测距/有媒体”只读筛选、事件级“复制证据”和完整媒体/车辆/GPS/测距明细，
  复制内容只进入剪贴板，不改变地图、目标选择、任务、飞控或物理释放状态；
- native self-test、Python 生成 bundle 的跨语言 `--load`、portable offscreen simple boot 均通过；定向回归 `45 passed`，
  全量回归 `3704 passed, 4 skipped, 0 failed`（3,708 tests，194.025 秒），JUnit 542,180 bytes、SHA-256
  `31D7D474F947B1E4F648CCBC4C62404FCE10C44DEADDE83E16C3B1E156C78AF0`；
- 文档/parity/style 定向回归 `72 passed`，Ruff、format、compileall 与 `git diff --check` 通过；
- Release/portable EXE 为 46,869,504 bytes，SHA-256
  `A1C52D9130386D8FAE4316EE23FD00D68E2141E6A385A7844229CB8680A79412`；`release-parity.json` 为 10,129 bytes，
  SHA-256 `A12A5490E73ED52266A44F447DFD87895CD837476895AFFF2EFD43CAAE09C827`；
- v172 改动了 UI contract；v171 派生证据/E7 报告在当时标为历史待重绑，随后由 v173 按当前 parity 重算；E7 仍保持
  `gate_passed=false / release_allowed=false`，硬件、室外 GNSS、HIL/REAL、人工视觉与实飞门均未通过。

### v0.3.0-pre.8 v171 native target-event replay inspector（2026-09-07）

- 新增原生 `MultiDetectTargetEventReplayStore`，从 `manifest.json` 读取并严格校验
  `multidetect.target-event-replay.v1` 的 manifest、`events.jsonl` 与 `media/` 目录闭合关系；重复 JSON key、未知字段、
  非有限数、路径逃逸/符号链接、时间/序列/GPS/测距不合格、媒体额外文件和 bytes/SHA-256 不一致都会 fail-closed，
  且失败时清空已加载状态；
- Fly View 新增“载入回放/查看回放”只读入口，显示 bundle/session、事件时间线、类别、捕获时间、GPS、距离/方位和媒体引用，
  不调用地图落点、目标选择、任务、飞控或物理释放接口；
- C++ self-test 覆盖正向、媒体篡改和重复 key 负向路径；Python 生成的真实 bundle 可由 native `--load` 跨语言复核；
- portable `MultiDetectGCS.exe`：46,863,360 bytes，SHA-256
  `589E33ADAB2C7D1DBC58524BCAE3474D18743A6E0D970FC4C01CF3F187201717`；定向回归 `45 passed`，
  `--simple-boot-test` 退出 0，QML 初始化错误数 0；E7 保持 `false / false / 31 blockers`，硬件/室外 GNSS/HIL/REAL/实飞门不变。

### v0.3.0-pre.8 v170 target card localization quality status（2026-09-07）

- Fly View 目标定位卡片直接读取原生 recorder 的 `current_status`，在同一只读卡片中显示当前/上次录制状态、
  定位率、当前连续不可定位、最近采样断档和诊断原因；没有录制样本时显示中性 `WAITING`，不会伪造质量结论；
- 质量颜色和文本沿用诊断弹窗同一 `MultiDetectState.targetObservationRecordingQualitySummary` 数据源，保持
  `WAITING/HEALTHY/WARNING/CRITICAL`、定位 fail-closed 和坐标 `1σ` 圆逻辑一致；
- UI contract parity 更新为 `2EF5B835D615191657C91AA438B29604025D6342982DD62BC1BCDCFA45907E8D`；
  Release/portable EXE 为 46,751,744 bytes，SHA-256
  `9675AC36032CC990F1F27ACD638F1B4D1D0397F34D9B43810A95D0BCD0D2D263`，offscreen simple boot 退出 0；
- 定向回归 `88 passed, 1 skipped, 0 failed`（89 tests），Ruff、format、compileall 和 `git diff --check` 全部通过；
- 全量回归 `3703 passed, 4 skipped, 0 failed`（3,707 tests，183.031 秒），JUnit 542,030 bytes、SHA-256
  `3B21593CB2277F1762D70C2287B34309EC615EE3456BC6D9DECDE9736AC7B3E1`；
- 三机型 external/PX4 派生证据、现场只读 preflight 与 E7 已按新 parity 重算；功能仍是 advisory-only，E7 保持
  `gate_passed=false`、`release_allowed=false`、31 blockers，不提升室外 GNSS、硬件、HIL/REAL、人工视觉或实飞状态。

### v0.3.0-pre.8 v169 live localization quality alarms（2026-09-07）

- 原生录制器实时摘要新增 `current_status` 四级状态：`WAITING`、`HEALTHY`、`WARNING`、`CRITICAL`，
  同步暴露当前连续不可定位、测距异常、GNSS 不合格计数、最近采样间隔与告警原因；连续两次不可定位
  进入严重状态，恢复样本后实时清除当前告警；
- Fly View 诊断弹窗展示当前状态颜色、中文状态与实时质量计数，保持只读 advisory，不改变坐标 fail-closed、
  任务或飞控安全边界；
- C++ self-test 真实覆盖 `WARNING → CRITICAL → HEALTHY`，4 样本会话经安装式 verifier 独立复核通过；
- Release/portable EXE 为 46,751,232 bytes，SHA-256
  `5BC3761AE4C13C28EBE6BB34691F6EB350CA9EDEF96376DF0DDD03DBE73C5A3A`，offscreen simple boot 退出 0；
- 定向回归 `85 passed, 1 skipped, 0 failed`，全量回归 `3703 passed, 4 skipped, 0 failed`；parity、三机型
  派生证据、现场只读探测与 E7 已重算，E7 仍为 `false / false / 31 blockers`，不提升室外 GNSS、硬件、
  HIL/REAL、人工视觉或实飞状态。

### v0.3.0-pre.8 v168 live localization recording quality（2026-09-07）

- 原生录制器新增实时质量摘要，覆盖定位率、最长不可定位、采样断档、测距/GNSS/定位质量段和 epoch 切换；
- Fly View 诊断弹窗新增只读质量时间线和分段明细；停止后的正式结论仍由 manifest 绑定离线 verifier 给出；
- 4 样本 C++ 会话经安装式 CLI 独立复核通过；定向回归 `85 passed, 1 skipped, 0 failed`；
- Release/portable EXE 为 46,746,112 bytes，SHA-256
  `F74F3D6FCFC8C044745A2B75A8ED07FD4CEBD41BDE2BD1239E00F9FB40C9A9FA`，offscreen 启动退出 0；
- parity、SIM/PX4 派生证据、现场只读探测与 E7 已重算；E7 保持 `false / false / 31 blockers`，
  不提升室外 GNSS、硬件、HIL/REAL、人工视觉或实飞状态。
- 最终全量回归为 `3703 passed, 4 skipped, 0 failed`，耗时 182.514 秒；JUnit SHA-256
  `B15A4B123DBF024311C3DB5E3A74B8F19A6C08115839AA09FD44090B71B23B04`。

### v0.3.0-pre.8 v167 localization recording quality segmentation（2026-09-07）

- session 报告新增采样间隔统计、断档事件、不可定位/测距/GNSS 连续异常段和 epoch 切换明细；
- 新增最大连续不可定位样本数与最大采样间隔失败门，CLI 和闭合 schema 同步更新；
- C++ 双快照目录以最大间隔 1,000 ms、零异常段通过；定向回归 `88 passed, 2 skipped, 0 failed`；
- 全量回归 `3703 passed, 4 skipped, 0 failed`；parity 与 E7 已重算，E7 保持
  `false / false / 31 blockers`，硬件和实飞状态不变。

### v0.3.0-pre.8 v166 closed recording-manifest verification（2026-09-07）

- 新增闭合录制 manifest schema、严格 verifier、仓库 wrapper 和安装式 CLI；重算每份快照的
  path/bytes/SHA-256，检查录制时间、顺序与目录闭合，并拒绝歧义或路径逃逸；
- session verifier 新增 `--require-recording-manifest`，将已验证 manifest 身份写入闭合会话报告；
- v165 C++ 真实双快照目录通过两层安装式 CLI；定向回归 `82 passed, 2 skipped, 0 failed`；
- parity 与 E7 已重算，仍为 `false / false / 31 blockers`。本轮不重链 EXE，不提升真实硬件验收状态。
- 最终全量回归为 `3697 passed, 4 skipped, 0 failed`，耗时 269.920 秒；JUnit SHA-256
  `2B9EFE6FC0E4579AB487E6D27485B0B56189E29C083E5E9A818E36DF01964AAA`。

### v0.3.0-pre.8 v165 native localization diagnostics recording（2026-09-07）

- Fly View 新增开始/停止连续定位诊断录制，按 `1 Hz` 保存、单次上限 3,600 份，并显示状态、数量和目录；
- 每次录制使用唯一目录，快照和最终 manifest 均原子提交；manifest 绑定相对路径、bytes、SHA-256，异常
  退出保留已完成快照但不伪造完整收口；
- 原生 self-test 生成 2 份真实快照，安装后的 session verifier 独立复核 2/2 可定位、定位率 1.0、门通过；
- Release/portable EXE 为 46,718,976 bytes，SHA-256
  `8F600A9CE8D1C4E8AF1A63721F16E52A67E56715A81B2EE7D469AED62C1BAB6D`，offscreen 启动通过；
- parity 与 E7 已重算，E7 保持 `false / false / 31 blockers`。功能仍为 advisory-only，硬件、室外 GNSS、
  HIL/REAL、人工视觉和实飞状态没有升级。
- 最终全量回归为 `3689 passed, 4 skipped, 0 failed`，耗时 196.262 秒；JUnit SHA-256
  `16793FE3E14EFB14F9D3CFA8404EB048678A6D5507E40D666ABB4CE1BD1E264C`。

### v0.3.0-pre.8 v164 localization diagnostics session verifier（2026-09-07）

- 新增多快照/目录会话 verifier 与闭合报告，逐样本复用 v163 严格校验并绑定路径、bytes、SHA-256；
- 检查本地时间严格递增、同 epoch sequence/`sentAtMs` 单调、epoch 切换和退休 epoch 不复活；汇总
  定位率、最长不可定位段、原因、range validity 与 GNSS reason，并提供最小样本/定位率门；
- 安装式 CLI 对实际 C++ 快照生成的会话报告通过；定向回归 `76 passed, 2 skipped`；
- parity 与 E7 已重算，仍为 `false / false / 31 blockers`。功能固定 advisory-only，不启用飞控/物理释放。
- 最终全量回归为 `3689 passed, 4 skipped, 0 failed`，耗时 208.55 秒；JUnit SHA-256
  `B325B2157890E27BCD71EF2DEAF29DE61DB769918E9C72ECDE9C8AA0B3AA83C8`。

### v0.3.0-pre.8 v163 target-observation diagnostics offline verifier（2026-09-07）

- 为 QGC 目标定位诊断增加闭合 JSON Schema、仓库 wrapper 和安装式 CLI；离线复核会重算原子身份、
  freshness、定位门与坐标省略，不接受 producer 自报结论；
- 输入 fail-closed：拒绝重复 key、未知字段、非有限数、超过 1 MiB 的文件和符号链接；结构有效但不可
  定位的快照可留档，`--require-locatable` 会返回非零；
- C++ self-test 从实际 producer 发出 JSON，并由 Python verifier/安装后 CLI 跨语言复验；定向回归为
  `63 passed, 1 skipped`，Ruff、compileall 和 diff check 通过；
- parity 与派生 E7 已重算，E7 仍为 `false / false / 31 blockers`。本轮没有重链主 EXE，portable 保持
  v162 的 46,704,640 bytes 和既有 SHA-256；硬件、室外 GNSS、HIL/REAL、人工视觉与实飞没有升级。
- 最终全量回归为 `3676 passed, 3 skipped, 0 failed`，耗时 194.79 秒；JUnit SHA-256
  `F9FCCF6AF34849B7BF8D24F3E1F628C1C06771241DAEF2608309C0693629B990`。

### v0.3.0-pre.8 v162 native localization diagnostics popup（2026-09-07）

- Fly View 新增“查看定位诊断”原生弹窗，在打开时冻结当前 fail-closed JSON，并以中文展示失败原因、
  原子身份、测距/GNSS 质量、源年龄与接收年龄；
- 坐标区域只读取 `coordinates_included`，失败诊断明确省略经纬度和 1σ；复制/保存仍使用同一严格
  schema，功能保持 advisory-only、飞控关闭、物理释放关闭；
- Release/portable EXE 为 46,704,640 bytes，SHA-256
  `ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`；offscreen simple boot
  退出 0 且 stdout/stderr 均为空；
- parity、外部三机型证据与 E7 已重算。E7 仍为 `false / false / 31 blockers`；现场网卡仍不在机载
  `192.168.144.0/24` 网段，人工视觉、室外 GNSS、HIL/REAL、硬件和实飞没有升级。
- 最终全量回归为 `3660 passed, 2 skipped, 0 failed`，耗时 272.67 秒；JUnit SHA-256
  `0D16ED23B6849934FAF176BD6039BFAACAE94B0D11A3066EA1968F1015C41C56`。

### v0.3.0-pre.8 v161 fail-closed target-observation diagnostics（2026-09-07）

- 新增 `MultiDetectTargetObservationDiagnostics` 与 Fly View“复制定位诊断 / 保存定位诊断”，统一导出
  range、target geolocation、GNSS、producer epoch、原子 envelope 身份、源年龄和本地接收年龄；
- 只有同 epoch/sequence/`sentAtMs`、同目标/源帧、合格 GNSS/测距、有效坐标/1σ 且全部状态新鲜时才
  输出坐标；任一门失败仍可保存原因，但强制删除经纬度与 1σ，避免失败快照成为假落点；
- Release/portable EXE 为 46,702,592 bytes，SHA-256
  `729BEDA95DD42E70538D854181113BD09D2414EED4D787AAA11529D1F55C05A0`；六项原生 self-test、45 项
  focused QGC/parity、Ruff 和 portable offscreen simple boot 通过；
- 当前 parity 与派生 E7 已重算；E7 仍为 `false / false / 31 blockers`。现场网卡仍是
  `192.168.1.13/24`，机载目标网段仍不可达，人工视觉、室外 GNSS、HIL/REAL、硬件和实飞没有升级。
- 最终全量回归为 `3660 passed, 2 skipped, 0 failed`，耗时 273.29 秒；JUnit SHA-256
  `8443ED203DC647B65EF0D7C4723FD5022BB791B02E746056D021635A756F929C`。

### v0.3.0-pre.8 v160 native producer-epoch diagnostics popup and atomic save（2026-09-07）

- Fly View 新增原生 producer epoch 诊断弹窗，按 newest-first 展示最多 64 条事件；卡片与弹窗均支持复制
  和保存完整诊断 JSON，保存失败进入现有 operator error 状态；
- 新增 `MultiDetectProducerEpochDiagnostics`，集中实现 journal、摘要、稳定 JSON 与同目录临时文件 rename
  原子提交；新增原生 self-test 覆盖零事件、容量/丢弃计数、排序、round-trip 和文件提交；
- Release/portable EXE 为 46,665,216 bytes，SHA-256
  `DE323538B410D6264825B071A083620471A4D726921FD25F10CB859476DB1BAE`；五项原生 self-test、45 项 focused
  QGC/parity 回归、Ruff 和 portable offscreen simple boot 通过；最终全量回归为
  `3660 passed, 2 skipped, 0 failed`；
- 当前 parity 派生证据和 E7 已重算；E7 仍为 `gate_passed=false`、`release_allowed=false`、31 blockers，
  现场链路仍错误走 `192.168.1.13`，真实硬件/HIL/REAL/实飞状态没有提升。

### v0.3.0-pre.8 v159 producer-epoch event timeline and diagnostics export（2026-09-07）

- QGC controller 新增进程内 producer epoch 事件时间线，记录 `epoch_initialized`、`producer_restart`、
  `retired_epoch_rejected` 和 `transition_capacity_exhausted`；最多保留 64 条，并分开暴露总数、保留数与
  丢弃数，避免环形截断被误认为完整历史；
- “目标落图”卡片新增“复制诊断”，零事件 `WAITING` 状态同样可以导出。JSON schema 为
  `multidetect.producer-epoch-diagnostics.v1`，包含健康摘要、事件计数/时间/epoch、事故计数及明确的
  advisory-only、飞控关闭、物理释放关闭边界；toolbar 同步显示事件数与丢弃数；
- Release/portable EXE 为 46,659,072 bytes，SHA-256
  `7DA7F882ECB51EEA4DDF1D79FF5F9EBB52E8BD3B77F0DBEEAC1B23EFFB95E375`；三项关键 native self-test、
  focused QGC/parity 回归、Ruff lint/format 和 portable offscreen simple boot 均通过；最终全量回归为
  `3660 passed, 2 skipped, 0 failed`；
- 当前 parity 的 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign、只读
  field-link 与 E7 已重算。E7 仍为 `gate_passed=false`、`release_allowed=false`、31 blockers；现场
  网卡仍走 `192.168.1.13/24`，人工视觉、室外 GNSS、真实硬件、HIL/REAL 和实飞均未声明通过。

### v0.3.0-pre.8 v158 producer-epoch health severity and evidence v6 audit（2026-09-07）

- producer epoch 健康状态新增锁存分级：无会话为 `WAITING`，正常为 `HEALTHY`，发生生产者重启为
  `NOTICE`，拒绝退休 epoch 旧包为 `WARNING`，切换容量耗尽为 `CRITICAL`；面板用明确中文和颜色显示；
- `multidetect.target-evidence.v6` 持久化健康级别/原因、重启/退休拒绝/切换拒绝计数、最近切换与最近事故
  时间，并严格复核字段间一致性；v1-v5 继续只读兼容，矛盾或错误类型的 v6 记录 fail-closed；
- Release/portable EXE 为 46,650,880 bytes，SHA-256
  `8EBAB7D38113418582AAC12A226BBDFAF988EF30D3E7FCC927222B58E2332A69`；三项关键 native self-test 与
  portable offscreen simple boot 通过，启动 stdout/stderr 均为 0 bytes；
- 当前 parity 的 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign、只读
  field-link 与 E7 已重算。E7 保持 `false / false / 31 blockers`；人工视觉、室外 GNSS、真实硬件、
  HIL/REAL 和实飞均未声明通过。

### v0.3.0-pre.8 v157 producer-epoch health and fail-closed retirement capacity（2026-09-07）

- QGC controller 暴露当前 producer epoch、生产者重启次数、retired 旧包拒绝次数、epoch 切换拒绝次数和
  最近切换时间；“室外测距质量”面板新增“观测生产者”行，运行人员可直接识别 bridge 重启与旧包重放；
- `MultiDetectObservationEpoch` 不再淘汰最旧 retired epoch。退休集合固定为 32 项，容量耗尽后拒绝新的
  epoch、保持当前会话，并要求重启 QGC；原生回归覆盖填满容量、拒绝第 33 次切换以及最早 epoch 仍被拒绝；
- Release/portable EXE 为 46,641,152 bytes，SHA-256
  `65AF88C1B55E7F570FF9A9A3D401CF6BA55E8BE5652C715823A6E4F2B8163BD4`；三项关键 native self-test 与
  portable simple boot 通过，后者 stderr 1,009 bytes、SHA-256
  `A770D77ADC91A759DE326542EA102507BF5E8C341414DDB594DD42C866290150`，无 QML 初始化错误；
- 当前 parity 的 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign、只读
  field-link 与 E7 已重算。E7 保持 `false / false / 31 blockers`；该软件诊断改进不替代人工视觉、室外
  GNSS、真实硬件、HIL/REAL 或实飞验收；最终全量回归为 `3660 passed, 2 skipped, 0 failed`。

### v0.3.0-pre.8 v156 producer-epoch observation resynchronization（2026-09-07）

- operator protocol 类型 `26` 新增必填非零 `producer_epoch`，application payload 从 124 bytes 扩展到
  MAVLink `TUNNEL` 上限 128 bytes；每个 `LiveOperatorBridge` 默认生成随机非零 epoch，并允许测试/受控
  启动显式注入，同一 bridge 的所有原子目标观测共享该值；
- QGC 新增原生 producer-epoch 状态机：首次或全新 epoch 原子推进，旧 current epoch 被退休；切换前清空
  range/geolocation/GNSS、source replay guards 与类型 10/22/25/26 sequence gates。任何迟到 retired epoch
  都被拒绝，不能让重启前目标状态复活；原生自测覆盖 `A -> B -> delayed A`；
- QML 地图与保存门要求 range/geolocation/GNSS 的 epoch、sequence、`sentAtMs` 全部一致；新写证据升级为
  `multidetect.target-evidence.v5` 并保存 `producer_epoch`，v1/v2/v3/v4 继续只读兼容；
- Release/portable EXE 为 46,639,616 bytes，SHA-256
  `8BDDA7ABB9DF806C5B18DA0CFDD4169CFF1A556DD062B3784F6A09D8074DACF5`。共享 parity 已更新；v155 的
  E7 派生文件因此降为历史证据；随后 external join/revalidation、三机型 response matrix、6 正例/9 故障
  campaign 与 E7 已按 v156 parity 重算。portable simple boot 退出 0 且无 QML 初始化错误；最终全量回归为
  `3660 passed, 2 skipped, 0 failed`。当前 E7 仍为 `false / false / 31 blockers`；最终 `v0.3.0`、人工视觉、室外 GNSS、
  真实硬件、HIL/REAL 与实飞均未声明通过。

### v0.3.0-pre.8 v155 atomic target observation envelope（2026-09-07）

- 新增认证 operator protocol 类型 `26`，在单个 124-byte application payload / MAVLink `TUNNEL` 中
  原子携带 range、target geolocation 与 GNSS quality；生产帧、目标/帧身份与定位质量任一不一致即整包拒绝，
  发布失败不推进指纹并保留重试；无目标独立 GNSS 与 range-only 兼容路径保持不变；
- QGC C++ 仅在 HMAC、replay、sequence、freshness 和内部不变量全部通过后，一次采纳三类状态；QML 要求
  三者 atomic 标志及 envelope sequence/`sentAtMs` 完全一致，避免界面或证据层观察到半组更新；
- 新证据升级为 `multidetect.target-evidence.v4`，只有类型 26 原子三元组可新写入；v1/v2/v3 保持只读兼容；
  观测仍为 advisory/display-only，不增加飞控或物理释放权限；
- Release/portable EXE 为 46,633,984 bytes，SHA-256
  `788E82B4BF8198EFC8ECD720E2E1A883899D7ADD65DE1276A0221FD61D35AC8F`；四项原生自测、Ruff、format、
  compileall、portable simple boot 与当前 parity 派生证据均通过。E7 仍为
  `false / false / 31 blockers`；最终全量回归为 `3660 passed, 2 skipped, 0 failed`。人工视觉、室外
  GNSS、真实硬件、HIL/REAL 与实飞继续未验收。

### v0.3.0-pre.8 v154 same-producer-frame GNSS evidence binding（2026-09-07）

- Jetson operator bridge 将需要目标定位的 GNSS、range、target geolocation 收口为同一生产帧组：三者
  `produced_at_s` 必须精确相等，可用目标定位还必须对应合格 GNSS；缺失、错帧或质量矛盾均零发布目标组；
  发送顺序固定为 GNSS → range → target geolocation，只有整组成功才推进指纹，部分失败会重试；
- QGC controller 在认证/replay/sequence 门后为 target geolocation 注入自身 envelope sequence/`sentAtMs`；
  保存门要求目标定位与 GNSS 的 `sentAtMs` 精确相等。新证据升级为 `multidetect.target-evidence.v3`，保存
  两侧 provenance；v1/v2 继续只读兼容，旧记录在卡片中明确标为缺少原子 envelope 绑定；
- native target-evidence/operator-protocol/depth-grid/replay-RTSP self-test、focused 119 项回归、Release 构建和
  portable hidden simple boot 均通过。EXE 为 46,603,264 bytes，SHA-256
  `02522C5661803381FD3B8E4E54267CC6BCD17B9769C1C8E2BE00894EBB470CB2`；
- 当前 parity 和 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 已重算通过。
  E7 仍为 `false / false / 31 blockers`；三消息传输不声称原子事务，人工视觉、室外 GNSS、真实硬件、
  HIL/REAL 与实飞仍未验收；最终全量回归为 `3657 passed, 2 skipped, 0 failed`。

### v0.3.0-pre.8 v153 GNSS-bound target evidence snapshots（2026-09-07）

- QGC controller 在认证、重放与 sequence 门后将 `GNSS_STATUS` envelope sequence/发送时刻交给 QML；
  “保存快照”同时要求合格目标坐标与新鲜独立 GNSS 质量证据，弱 fix、低卫星数、超限 H/V accuracy、
  stale age、错误 reason/position health、缺失或错误类型字段均 fail-closed；
- 新快照升级为 `multidetect.target-evidence.v2`，保存 GNSS sequence、发送时刻、质量门原始字段与来源年龄；
  v1 JSONL 保持只读兼容，历史卡片明确显示旧记录没有独立 GNSS 质量证据。该来源是同时保存的诊断 provenance，
  不声称目标坐标与 GNSS 状态是单一原子观测；
- 新增 native evidence-store self-test，覆盖 v1 读取、弱/错误类型 GNSS 零写入拒绝、v2 保存与混合重载；
  native operator protocol、target evidence store、replay RTSP wire self-test 均退出 0；QGC hidden simple boot 退出 0；
- Release/portable EXE 为 46,600,192 bytes，SHA-256
  `8225BF9F41426E473F23BC74BF038E576A88EA3556E1A7DD883F1B6C036909BB`。UI parity 与当前派生证据已重算，
  E7 仍为 `false / false / 31 blockers`；现场链路仍路由错误且三个端点超时，人工视觉、室外 GNSS、硬件、
  HIL/REAL 与实飞没有被软件构建结果提升；最终全量回归为 `3656 passed, 2 skipped, 0 failed`。

### v0.3.0-pre.8 v152 independent GNSS quality status（2026-09-07）

- 新增认证 operator protocol 类型 `25` `GNSS_STATUS`，以独立 5 Hz 心跳发布 position health、fix、卫星数、
  水平/垂直精度、样本年龄与 canonical reason；它不依赖目标选择或测距结果，也不携带经纬度；
- Python domain/codec/MAVLink/UDP/live bridge 与 QGC C++ decoder/controller/QML 全链接入同一质量门；伪造
  qualified 弱 fix 被两端拒绝，QGC 1 秒失效清空，toolbar 在目标选择前即可显示不可定位原因与原始诊断；
- 状态固定 `advisoryOnly=true`、`flightControlEnabled=false`、`physicalReleaseEnabled=false`，未增加任何控制；
  相关 277 项 Python/QGC 静态回归通过，native protocol 与 replay RTSP wire self-test 退出码均为 0；全仓
  回归为 `3656 passed, 2 skipped, 0 failed`；
- 重新构建 `0.3.0-pre.8` Release/portable，EXE 为 46,587,392 bytes，SHA-256
  `2686D5EFAAB375E08C41C5AD8050C0BB9807C33309F52D1102AB774E96EF61A9`，portable simple boot 退出码 0；
  当前 parity 的 external/matrix/campaign 派生证据也已重算，E7 仍为 `false / false / 31 blockers`；这些只
  构成软件构建证据，人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞仍未声明通过。

### v0.3.0-pre.8 v151 现场链路只读刷新与 E7 重绑定（2026-09-07）

- 在当前台式机执行 3 样本、协议级、路由级只读 field-link 预检；报告及独立接口诊断均通过结构/哈希复验，
  但 3/3 样本实际路由源地址均为 `192.168.1.13`，不属于要求的 `192.168.144.0/24`，Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 均超时；
- 将新鲜的现场失败报告与 v150 当前 parity 的 external join/revalidation、三机型 response matrix/campaign
  显式重绑到 E7。新报告仍为 `gate_passed=false`、`release_allowed=false`，阻断项由 32 降为 31；该变化只关闭
  旧 field-link 快照过期问题，`real_field_link.passed=false` 与 `hardware_link_ready=false` 保持不变；
- 本轮未修改网卡、路由、飞控、载荷、C++ 或 QML，也未产生硬件、HIL、REAL、室外 GNSS 或飞行通过声明；
  版本继续保持 `0.3.0-pre.8`，portable EXE 无需重建。

### v0.3.0-pre.8 v150 hash-anchored GR01 trust registry（2026-09-07）

- 新增 closed trust-registry schema/CLI：用包外 registry 文件 SHA-256 锚定 `source_operator` / `reviewer` 的 principal ID、
  identity、key ID、Ed25519 公钥、全局/逐 key 有效期与吊销状态；重复 principal/key/public key、过期或已吊销 key 均失败；
- portable review schema 升级到 v5，移除 raw public-key CLI trust：review/verify 都重开同一 hash-anchored registry；operator
  key 必须在 source attestation 与 review 两个时点有效，reviewer key 必须在 review 时点有效，未来时点 attestation 被拒绝；
- trust-registry + portable-review 软件 smoke `22 passed`。新 safety parity 的 join/revalidation、三机型 matrix、6 正例/9 故障
  campaign 均通过；E7 保持 `false / false / 32 blockers`、GR01 `present=false/passed=false`，不提升真人身份、硬件、HIL、
  REAL 或飞行声明；最终全量回归 `3650 passed, 2 skipped`。

### v0.3.0-pre.8 v149 dual-signed GR01 source/review chain（2026-09-06）

- 新增 detached source-attestation schema v1：现场 operator 使用仅来自环境的 32-byte Ed25519 seed 签署完整 transfer
  inventory identity；attestation 必须位于 inventoried session 目录外，避免循环清单或破坏 byte-exact copy；
- portable review schema 升级到 v4；第二机必须用包外预登记 operator 公钥先验 source signature，再由独立 reviewer
  签署 review。operator/reviewer 的 identity、key ID、公钥任一复用均在 transfer/review 输出前 fail-closed；
- wrong operator key、source signature tamper、相同身份/密钥、错误 inventory anchor 均有负向回归。双签名 smoke `6 passed`；
  新 safety parity 派生证据全部通过，E7 仍为 `false / false / 32 blockers`，不提升硬件、HIL、REAL 或飞行声明。

### v0.3.0-pre.8 v148 reviewer-signed GR01 portable review（2026-09-06）

- portable review schema 升级到 v3；第二机 review 必须从环境读取 32-byte Ed25519 seed，绑定 reviewer/key ID/public key，
  对完整 review payload 签名；私钥不进入命令行、回执或日志；
- 独立 `verify` 强制接收包外固定 reviewer 公钥，拒绝错误公钥、签名篡改、缺失/非法私钥；签名材料非法时在任何
  transfer/review receipt 写入前 fail-closed；
- claims 只证明指定公钥对应私钥的持有与回执完整性，明确保持 `reviewer_human_identity_verified=false`，并继续保持
  no-environment/no-full-E7/no-hardware/no-flight 边界。签名专项 smoke `5 passed`；新 parity 派生证据全部通过，E7
  仍为 `false / false / 32 blockers`，最终全量回归 `3643 passed, 2 skipped`。

### v0.3.0-pre.8 v147 trusted GR01 inventory anchor（2026-09-06）

- portable review schema 升级到 v2；`review` 必须接收独立渠道保存的 inventory SHA-256，并在创建 transfer/review
  receipt 前核对实际 inventory 文件，哈希不符时零输出、fail-closed；
- receipt 同时绑定 expected/actual inventory hash 与 match 结果，独立 verifier 重算三者；现场 producer 明确打印需
  另行保存的可信哈希。它不是签名或操作者身份认证，也不重验目标环境、完整 E7 来源或硬件；
- safety parity 及四份派生证据已重算，E7 仍为 `false / false / 32 blockers`、GR01 `present=false/passed=false`。
  软件锚点 smoke `2 passed`；最终全量回归 `3641 passed, 2 skipped`。真实 session、第二台机器和独立传递哈希
  仍待现场验收。

### v0.3.0-pre.8 v146 portable GR01 final-session review（2026-09-06）

- 现场 final session 独立验证成功后自动生成 6 文件 transfer inventory；目标机环境、secret 与 canonical source
  仍只在现场 snapshot 阶段由强 verifier 验证；
- 新增第二机 `review`：逐字节核对复制包，重开 plan/handoff/bench/E7 manifest/report，重算 handoff、bench、配置和
  GR01/E7 绑定；closed receipt 固定 `target_environment_reverified=false`、`canonical_e7_sources_recomputed=false`，
  不产生硬件、飞行、控制或载荷声明；
- transfer primitive、review producer/verifier/schema 和现场调用顺序全部进入 safety parity；新 parity 派生证据通过，
  E7 保持 `false / false / 32 blockers`、GR01 `present=false`。软件跨机 fixture 通过但不构成真实现场验收；最终全量
  回归为 `3640 passed, 2 skipped`。

### v0.3.0-pre.8 v145 imported GR01 source plan（2026-09-06）

- 现场 producer 新增 `-SourcePlan`：regular non-reparse 输入逐字复制到 session，前后 SHA-256 必须一致；
- 新 handoff `match` 门在任何 bench/UDP 前严格复验 target ready/environment，并对账 endpoint、hardware、CIDR、阈值、
  secret names 与 configuration digest；端口 drift smoke exit 1 且未创建 bench，原参数 match exit 0；
- safety parity 与四份软件证据已重算，E7 仍为 `false / false / 32 blockers`，未产生硬件/网络/飞行通过声明。
  最终全量回归为 `3635 passed, 2 skipped`。

### v0.3.0-pre.8 v144 five-source GR01 final session（2026-09-06）

- final session schema v3 将 plan、target handoff、bench、E7 manifest/report 作为五份 distinct/hash-bound 来源；consumer
  严格复验目标 environment/source，并交叉核对 handoff→plan→bench/E7 的配置和 Git/parity identity；
- producer 在任何 UDP 前创建并 require-ready target handoff，新增 target host ID，最终 record 显式传入 `--handoff`；
  relocated non-ready origin plan 只有经 ready target handoff 才能进入 session；handoff tamper/config drift 均 fail-closed；
- clean no-network smoke 仍因缺现场 secrets 保持 non-ready；软件五源 fixture 通过但不构成硬件 session。新 parity 派生
  证据通过，E7 继续 `false / false / 32 blockers`、GR01 `present=false`；最终全量回归
  `3631 passed, 2 skipped`。未改 QGC 或构建 EXE。

### v0.3.0-pre.8 v143 portable GR01 plan handoff（2026-09-06）

- 新增 handoff producer/verifier、repository wrapper 与 closed schema；目标机允许不同绝对部署路径，但必须重新证明
  tracked-clean、Git HEAD、canonical parity、tool identity 与 parity source verification 全部等价；
- receipt 绑定 source plan hash、portable configuration digest、target paths/host 和 secret presence，secret 值不落盘；
  path escape、覆盖、source/tool/plan/environment drift 全部 fail-closed；malformed source identity 在写入前拒绝；
- clean commit 无网络 smoke 为 source-equivalent/structurally valid，但因现场 secret 缺失保持 non-ready；新 parity 的四份
  派生证据通过，E7 仍为 `false / false / 32 blockers`、GR01 `present=false`；全量回归
  `3629 passed, 2 skipped`。未改 QGC 或构建 EXE。

### v0.3.0-pre.8 v142 execution-plan-bound GR01 session（2026-09-06）

- final GR01 session schema v2 新增 source-bound plan 来源投影；consumer 重开 plan 并验证 ready/source identity，
  再对账 plan 与实际 bench/E7 的 endpoint、hardware、CIDR、质量阈值和 freshness；
- PowerShell producer 显式将 bench 前通过的 plan 传给 final record creator；plan/source/config 漂移均 fail-closed；
- 新 safety parity 的四份派生证据独立通过，E7 保持 `false / false / 32 blockers`、GR01 `present=false`。
  最终全量回归 `3621 passed, 2 skipped`；本轮不改 QGC C++/QML，不重建 EXE。

### v0.3.0-pre.8 v141 source-bound GR01 session plan（2026-09-06）

- GR01 plan schema v2 绑定 Git HEAD/tracked-clean、canonical parity 来源复算和四个执行工具的 bytes/SHA-256；
  独立 verifier 可重开所有来源并可选复核当前 secret presence；
- 实际 session 在 UDP bench 前强制创建 plan 并执行 `--check-environment --require-ready`，源码 dirty、parity stale、
  工具漂移或 secret 缺失均在网络前 fail-closed；secret 值仍不序列化；
- 新 parity 的 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 独立通过；
  E7 保持 `false / false / 32 blockers`、GR01 `present=false`；最终全量回归 `3620 passed, 2 skipped`。
  本轮不改 QGC C++/QML，不重建 EXE。

### v0.3.0-pre.8 v140 GR01 field-session plan receipt（2026-09-06）

- transactional wrapper 新增 `-PlanOnly/-PlanOutput`：验证 literal IPv4、CIDR、路径、解释器、阈值与两个 secret
  环境变量是否存在，但不读取/序列化值，不接触网络或硬件，也不创建 bench/session 成功记录；
- 新 closed plan schema 固定四步执行顺序及 `network_contacted=false`、`hardware_observed=false` 和全部 no-control
  claims；producer 改用 repository-source CLI shim，因此干净源码树无需预先 editable-install；
- 新 safety parity 的四份派生证据独立通过；E7 继续 `false / false / 32 blockers`、GR01 `present=false`。
  最终全量回归 `3614 passed, 2 skipped`；本轮不改 QGC C++/QML，不重建 EXE。

### v0.3.0-pre.8 v139 transactional GR01 release session（2026-09-06）

- 新增 `run_gr01_release_session.ps1`，用一个 write-new 会话目录依次执行真实 GR01 selection/ACK bench、独立
  strict verifier、freshness-bound E7 manifest/report 与最终独立重算；任一步失败都不会生成成功 session record；
- 新增 closed session schema、Python consumer/CLI 与 installed-tree-independent verifier，重新打开 bench、manifest、
  saved report，复算 source SHA/bytes、canonical E7 结果与 evidence age，并固定 no-flight/no-payload claims；密钥值
  不进入参数、记录或日志；全部生产/消费路径进入 `safety_policy` parity；
- 新 parity 的 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 已独立通过；
  E7 保持 `false / false / 32 blockers` 且 GR01 `present=false`。最终全量回归
  `3613 passed, 2 skipped`；本轮不改 QGC C++/QML，不重建 EXE。

### v0.3.0-pre.8 v138 GR01 evidence freshness（2026-09-06）

- GR01 E7 记录新增 source UTC、epoch 和 max-age 原子绑定，writer 默认拒绝超过 900 秒或 materially future 的
  报告；evaluator 以统一 `evaluated_at_utc` 在每次复评时重算 age，partial/tampered/expired provenance 均
  fail-closed；历史 prerelease 可读，但 final channel 强制完整 freshness 字段；
- CLI/PowerShell wrapper/schema/tests 已同步；新 PX4 parity 的四份派生证据独立通过，E7 继续 32 blockers 且
  无真实 GR01 report。strict 全量回归中顺带修复 annotation UI 测试子进程管道泄漏，最终
  `3606 passed, 2 skipped`；本轮不改 QGC C++/QML，不重建 EXE。

### v0.3.0-pre.8 v137 GR01 operator-link final release gate（2026-09-06）

- E7 新增独立 `real_gr01_operator_link`：writer hash-bind 并严格复核真实 GR01 selection/ACK bench，evaluator
  重开源报告、重跑 verifier、逐字段对账，软件 loopback、投影篡改和源文件漂移均 fail-closed；
- 最终 `0.3.0` 必须分别通过 GR01 operator link 与 fire-alert link，二者不能互相替代；当前无真实 GR01 报告，
  prerelease E7 保持 32 blockers。PX4 parity 及其派生 evidence 已重算，最终全量回归
  `3602 passed, 2 skipped`；本轮不改 QGC C++/QML，不重建 EXE。

### v0.3.0-pre.8 v136 GR01 physical-source bench verification（2026-09-06）

- `gr01-link-bench --hardware-mode` 现在要求 expected source CIDR，先解析 OS route 再把同一 IPv4 bind 到实际
  持续 UDP session；错网卡/错网段在首个选择包前以 0 transmission 失败；
- 新增 strict schema、独立 verifier 与 console entry point，重算 endpoint/source、attempt/ACK/timeout 守恒、
  丢包/时延阈值、双重签名/双向标志和全部 no-control 位；生成 CLI 改为原子写报告，相关实现全部进入 safety
  parity；最终全量回归 `3597 passed, 2 skipped`。真实 GR01/G20 双向 IP 与射频台架仍未通过，不提升最终发布状态。

### v0.3.0-pre.8 v135 selected alert-link topology contract（2026-09-06）

- 新增 strict、canonical、hash-bound 的 `gr01_direct` / `v6x_fallback` topology 契约；V6X 回退强制绑定
  GR01 失败、PX4 SITL 路由和拆桨 V6X 台架三份 SHA-256 证据，缺一即在发送前失败；
- hardware ACK bench v5 从 `--topology` 原子加载 endpoint/source/identity，报告内嵌 topology 与 canonical hash，
  offline verifier 重算绑定；release manifest 只接受 v5，旧 v1-v4 保持可读；全量回归
  `3590 passed, 2 skipped`。新 parity 派生证据复核通过，E7 仍为 32 blockers；该变更不宣称现场链路已验收。

### v0.3.0-pre.8 v134 authenticated ACK runtime source observation（2026-09-06）

- UDP ACK receipt 新增实际 sender socket `getsockname()` IPv4；transport 在 send 前复核该运行时地址与
  `source_host` 一致，retry publisher 只保留已校验成功的最后 receipt；
- hardware bench v4 要求全部成功 ACK 样本都有实际源地址观察且均等于 route/bind 地址；即使 ACK 成功，缺失或
  不一致的运行时源观察仍 fail-closed。新 release manifest 只写 v4，evaluator 继续兼容 v1/v2/v3；全量回归
  `3582 passed, 2 skipped`。

### v0.3.0-pre.8 v133 authenticated ACK sender-source pinning（2026-09-06）

- authenticated UDP transport 新增可选 canonical IPv4 `source_host`，每个真实发送 socket 在 connect/send 前先
  `bind(source_host, 0)`；hardware bench v3 只有 OS route source、expected CIDR 与 transport source 三者一致才
  进入 ACK 循环，否则保持 0 transmission；
- v1/v2 历史报告继续可复核，新 release manifest 只写 v3 并绑定 transport source/address result。当前错网段
  实测为合法失败，软件证据重新绑定后 E7 仍为 32 blockers，不提升 GR01/V6X/G20 现场声明；最终全量回归
  `3580 passed, 2 skipped`。

### v0.3.0-pre.8 v132 authenticated ACK source-route binding（2026-09-06）

- alert-link hardware bench 升级为 v2：必须声明 expected source CIDR，并在任何探测包发送前绑定 OS 选择的
  local UDP source address；错网卡/错网段时 0 transmission fail-closed，失败报告仍可被独立 verifier 重算；
- release manifest writer 只接受 route-bound v2 REAL report，并完整绑定 source CIDR/address/result；历史 v1
  报告仍可离线复核。alert transport/bench/schema 加入 safety parity；全量回归 `3575 passed, 2 skipped`，
  现场 GR01/V6X/G20 门保持未完成。
- 新 parity 下重算的三机型 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign 均通过
  独立 verifier；E7 重跑仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，未提升硬件/现场声明。

### v0.3.0-pre.8 v131 clean-checkout validation recovery（2026-09-06）

- 新增 `.gitattributes`，强制 Python/typing stub 在 Windows 新检出时保持 LF；从 Git 对象创建的独立
  detached worktree 已实测 1,524 个 Python 文件全部通过 Ruff format/lint 与 compileall；
- manual validation 改为安装测试实际需要的 vision/Pixhawk/security/model-tools extras，并把 `requests` 纳入
  dev dependency/lock；只存在于 ignored `artifacts/` 的本地 evidence/model 集成测试在干净检出中明确 skip，
  不再以 FileNotFoundError 破坏软件回归，也不把缺失资产伪造为通过；
- 修复 VTOL flight-capture ready marker 的 6 位小数时间与内部比较值不一致问题，避免精确回显因不足 1 微秒的
  round-down 被随机拒绝；Windows Python 3.13.7 隔离环境按 workflow 顺序实跑 `3559 passed, 15 skipped`，
  主工作树 Python 3.12 连同 Git 外本地资产实跑 `3572 passed, 2 skipped`；GitHub-hosted 3.11/3.12/3.13
  手动矩阵仍待真实触发。

### v0.3.0-pre.8 v130 Ruff format baseline migration（2026-09-06）

- 以 Ruff 0.16.4 对全仓 1,523 个 Python 文件建立统一 format baseline；433 个候选中 282 个形成 canonical
  源码差异，format/lint/compile/diff gate 全部通过，manual validation workflow 不再带历史 format 债务；
- 重建四组受影响的 release parity，并从不可变外部源重算三机型 join/revalidation、reported-position matrix
  和 6 正例/9 fail-closed 故障 campaign；E7 重跑仍为 32 blockers，未提升 hardware/HIL/REAL/release 声明；
- 最终全量回归 `3570 passed, 2 skipped`，1,523 个 Python 文件全部通过 Ruff format/lint。

### v0.3.0-pre.8 v129 deployment-bound SSH host key（2026-09-06）

- 部署器从指定 `known_hosts` 提取解析后目标的可信条目，生成每次部署专用 snapshot；scp/ssh 排除 global
  known_hosts 并只使用该文件，前后 SHA 不一致时删除安装回执并失败；
- installation receipt v3 绑定 resolved host、snapshot bytes/SHA 和 canonical SHA256 fingerprints；独立 verifier
  重算明文/hashed host 匹配与 key fingerprint，拒绝错主机、篡改、重复、revoked/未知 key type。当前真实
  known_hosts 缺目标的负测在 scp 前停止且无成功回执；全量 3,572 tests：`3570 passed, 2 skipped`。

### v0.3.0-pre.8 v128 explicit Jetson SSH identity（2026-09-06）

- Jetson 部署新增显式 `RemoteUser`/`SshPort`，scp/ssh 共用精确 `user@host:port`，并强制
  `StrictHostKeyChecking=yes`、`UpdateHostKeys=no` 和 BatchMode，避免 Windows 本机用户名或首连交互被误用；
- installation receipt 升级 v2 并绑定 remote user/SSH port；非法用户/端口 fail-closed。真实 package-only 烟测
  复核 242 members/231 files 且无 install receipt；全量 3,566 tests：`3564 passed, 2 skipped`，E7 仍阻断。

### v0.3.0-pre.8 v127 source-bound Jetson installation receipt（2026-09-06）

- SSH 远端安装只有在退出码为 0 且唯一 READY/RUN marker 完整匹配后才写 installation receipt；回执绑定
  package receipt、archive SHA、remote output bytes/SHA、source HEAD、目标目录、runtime Python 和全部远端检查；
- 新增 closed Schema、installed/clean-checkout verifier；package-only、输出篡改、marker 重复/缺失、检查失败和
  hardware/release 越界声明均 fail-closed。当前无飞机网段，未生成真实安装回执；全量 3,563 tests：
  `3561 passed, 2 skipped`，Ruff/compileall/PowerShell/Bash/diff/parity 通过，QGC EXE 未改变。

### v0.3.0-pre.8 v126 independent Jetson evidence package verifier（2026-09-06）

- 新增 installed/clean-checkout 独立 verifier 与 receipt Schema；从 tar 流复核 archive、source manifest、
  逐文件 SHA 和闭合集合，并拒绝 traversal、case collision、link/special、增删、篡改及 release 提升；
- 正式 v125 包通过 240 members/229 files/6,668,784 source bytes 独立验证，仍不声明远端安装；
  全量 3,552 tests：`3550 passed, 2 skipped`，Ruff/compileall/diff/parity 通过。

### v0.3.0-pre.8 v125 isolated Jetson evidence tooling deployment（2026-09-06）

- 新增 package-only/SSH 双模式部署器，将证据采集工具以完整源码、Git 状态、逐文件 SHA manifest 和 archive
  identity 打包；本机解包 229/229 重算后，远端再做 hash/compile/Bash/import 验证并原子落入独立版本目录；
- 不覆盖既有工具、不 sudo、不重启服务。干净 HEAD 正式包 SHA-256 `6D2D5577...F2F49E`；控制机尚无
  `192.168.144.0/24` 路由，所以未声称实机安装；全量 3,545 tests：`3543 passed, 2 skipped`。

### v0.3.0-pre.8 v124 one-command Jetson RTSP evidence session（2026-09-06）

- Jetson recorder 现在为每次采集创建不可覆盖的独立目录，并顺序完成 H.265 stream-copy、v3 manifest、
  camera-only raw replay 和 transfer inventory；支持 frame stride/limit，失败保留部分证据并报告路径；
- RTSP URI 继续只从环境变量读取，脚本不启动 detector/QGC/飞控/载荷；全量 3,544 tests：
  `3542 passed, 2 skipped`，Git Bash syntax、Ruff、compileall、diff check 与 parity 重生成通过。

### v0.3.0-pre.8 v123 byte-exact evidence transfer receipt（2026-09-06）

- 新增 `multidetect-evidence-transfer snapshot|verify|verify-receipt` 与 clean-checkout wrapper；Jetson 源端 inventory
  和 Windows 目的端 receipt 分别保存完整逐文件 relative path/bytes/SHA-256 与 canonical bundle hash；
- 增删、内容变化、大小写路径冲突、symlink、回执污染被校验目录和任何 hardware/release claim 均 fail-closed；
- 全量 3,544 tests：`3542 passed, 2 skipped`；Ruff/compileall/diff check/parity 重生成通过，QGC EXE 未改变。

### v0.3.0-pre.8 v122 portable RTSP evidence chain（2026-09-06）

- 新 v3 RTSP 清单对同目录容器写相对引用，使原清单 canonical hash 在 Jetson→Windows 整包迁移后保持不变；
- raw replay 的 source binding 使用显式 `recording_root|absolute` 路径基准；同一父目录移动后会从新位置重开
  RTSP 清单/视频和 raw manifest/events/images，summary 内历史绝对显示路径不再作为定位权威；
- 整目录迁移正例和 summary 篡改负例通过；camera/GPS/release 声明不变。全量 3,537 tests：
  `3536 passed, 1 skipped`，Ruff/compileall/parity 重生成通过。

### v0.3.0-pre.8 v121 RTSP evidence container → raw replay（2026-09-06）

- RTSP recorder 清单升级为 closed-schema v3，固定记录包围 stream-copy 的 UTC 起止、单调时间窗、
  container bytes/SHA-256、全帧 probe 与 canonical hash；URI/凭据继续不落盘；
- 新增 `multidetect-rtsp-to-replay` / clean-checkout wrapper：只接受复核一致的 v3 清单和原容器，自动继承
  capture start UTC，逐帧生成不可覆盖 camera-only raw replay，并再次重开 source、summary、raw manifest/events/images；
- video converter 新增源文件预解码/后解码双重身份检查；tracking-review 保持 v2 历史读取并对 v3 使用严格 verifier。
  结果不推断人员/设备/地点/GNSS，也不提升 deployment/HIL/REAL/release；全量 3,537 tests：
  `3536 passed, 1 skipped`。

### v0.3.0-pre.8 v120 field attestation timestamp handoff（2026-09-06）

- video→raw replay 摘要新增结构化 `source_attestation_handoff`，从已验证 recording 自动给出 UTC 起点、
  最小有效终点、最后事件时间和仍需现场人员确认的字段，不自动创建 deployment claim；
- attestation CLI 新增 `--derive-times-from-recording`，仍强制分层、人员、设备、地点和 local/RTSP 来源；
  显式时间与自动推导不能混用，默认 `1970-01-01` 占位锚在 writer/verifier 两端均 fail-closed；
- 真实采集、HIL/REAL、E7 和 QGC EXE 不因此获得新通过声明；全量 3,530 tests：
  `3529 passed, 1 skipped`。

### v0.3.0-pre.8 v119 deployment recording source attestation（2026-09-06）

- 新增 installed CLI 与 clean-checkout wrapper，为六分层物理现场录像写入不可覆盖、closed-schema、
  canonical-hash 的来源证明，绑定采集人/设备/地点/来源、UTC 时间窗和 recording manifest/hash；
- intake/campaign/final replay manifest 三层都会重开证明；最终 manifest 额外绑定证明文件 bytes/SHA-256，
  仅自报 `source_kind=deployment_recording`、缺失/错层/换录像/移动/篡改证明均 fail-closed；
- 证明只保存可审计的人为来源声明，不等于硬件签名或真实外场验收。真实六分层采集和 E7 仍待完成，
  共享 parity 与 QGC EXE 未改变；全量 3,527 tests：`3526 passed, 1 skipped`。

### v0.3.0-pre.8 v118 campaign-bound annotation UI（2026-09-06）

- `multidetect-field-replay-campaign` 新增 `annotation-ui`，只在 fresh status 为 `annotation_required`
  时解析当前 campaign 的固定 intake/workspace 并在同进程启动 v117 loopback 工作台；
- campaign status 新增可直接执行的 `annotation_workbench_argv`；启动前再次复验 plan/model/intake/
  annotation，完成 review、尚未建 workspace 或证据漂移时 fail-closed；
- stdout 先记录 campaign/dataset/plan canonical hash 授权事件，再记录 loopback/hardware-IO 启动事件；
  真实子进程 HTTP 生命周期已覆盖。全量 3,521 tests：`3520 passed, 1 skipped`；E7 仍有 32 blockers。

### v0.3.0-pre.8 v117 local visual annotation workbench（2026-09-06）

- 新增 `multidetect-replay-annotation-ui` 与 clean-checkout wrapper：浏览已通过 intake/hash 复验的帧，
  在原始像素坐标画多类别 bbox，逐帧保存正/负例、前后导航、统计计数并发起独立 review；
- 服务仅接受数值 loopback 地址；随机 mutation token、CSP nonce、严格 JSON/大小上限、精确 frame ID、
  symlink/路径逃逸和图像 SHA-256 复验共同 fail-closed，且明确不连接相机、Pixhawk 或执行器；
- HTTP 生命周期、鉴权、输入拒绝、真实 PNG、标注与 review-content 绑定均有回归；全量 3,519 tests：
  `3518 passed, 1 skipped`。真实人员视觉/交互验收、现场标注、E7 32 blockers 与 QGC EXE 均未改变。

### v0.3.0-pre.8 v116 annotation review-content binding（2026-09-06）

- `multidetect-replay-annotations` 新增 `annotate`/`review`：按 frame ID 原子写入 positive/negative、
  多 bbox、annotator 与 UTC reviewer，状态直接给出下一未标帧；复核后改标会自动撤销旧 review；
- annotation workspace 升级 schema v2，`review_content_hash` 绑定 assignment、全部 disposition/bbox、
  annotator、reviewer 和时间；review 后直接篡改 JSON 会 fail-closed，reviewer 也不能静默换人；
- legacy v1 仍可读取/finalize，但 v115+ campaign 必须使用 v2 且 hash-bound 才会继续。全量 3,518 tests：
  `3517 passed, 1 skipped`；共享 parity、E7 32 blockers 与 QGC EXE 未变。

### v0.3.0-pre.8 v115 resumable field replay campaign（2026-09-06）

- 新增 `multidetect-field-replay-campaign`、仓库 wrapper 与 closed plan schema，把六分层 field intake、
  人工标注/finalize、固定模型 runtime inference、reviewed accuracy、threshold calibration 和 fresh verifier
  编排为不可覆盖、可恢复的 fail-closed 状态机；
- 每次 `status/advance` 都重开模型哈希与已完成证据；缺现场目录或人工步骤时返回精确 stage/next action，
  标注员、标注复核人、accuracy reviewer、threshold reviewer 必须四人独立；
- 真实 ONNX Runtime 六分层端到端回归已到 `complete`，但只验证软件闭环，不代表已有现场数据。
  全量 3,515 tests：`3514 passed, 1 skipped`；共享 parity、E7 当前 32 blockers 与 QGC EXE 均未变。

### v0.3.0-pre.8 v114 final calibration promotion gate（2026-09-06）

- E7 calibration 诊断新增聚合 `promotion_ready`，必须同时来自报告声明和当前输入确定性重算；
- prerelease 继续接受 `promotion_ready=false` 的完整性/调参诊断，最终 `v0.3.0` 则明确要求 calibration
  verification 通过、provenance 完整且 promotion-ready，防止未批准阈值进入正式版；
- 当前 E7 仍为 32 blockers；全量 3,512 tests：`3511 passed, 1 skipped`，共享 parity 与 QGC EXE 未变。

### v0.3.0-pre.8 v113 E7 calibration provenance gate（2026-09-06）

- calibration verifier 新增 report/recomputed provenance 投影；E7 manifest writer 和 evaluator 将两项事实
  重新绑定到 verification、report 与 replay-set 字节，旧 artifact 缺字段时按 false 读取；
- prerelease 保持历史可读性，最终 `v0.3.0` 新增强制门：calibration verification 必须通过且
  `provenance_complete=true`；synthetic HIL ONNX 回归已贯通 receipt→accuracy→calibration→E7；
- release-gate schema 改动已重算共享 parity，并从不可变源重算 external/PX4 派生证据；当前 E7 仍为
  32 blockers。QGC 源/UI parity 未变，不重编 portable。全量 3,511 tests：`3510 passed, 1 skipped`。

### v0.3.0-pre.8 v112 calibration provenance chain（2026-09-06）

- 抽出 evaluator/calibration 共用的 prediction-run verifier/binding，并新增 `production_approved` 事实；
- calibration CLI/API 可成对接收 prediction receipt 与 reviewed accuracy report：先复验完整 prediction bundle，
  再执行 readiness fresh recomputation，并绑定模型、receipt、accuracy、manifest/canonical/file hashes；
- calibration verifier 会用报告内或显式 override 路径重开全链，accuracy 即使仍可自洽重算，只要文件或 reviewer
  被替换也会因 calibration binding 改变而 fail-closed；旧无绑定报告仍可读且标记 `provenance_complete=false`。
  全量 3,510 tests：`3509 passed, 1 skipped`，Ruff 与 compileall 通过。

### v0.3.0-pre.8 v111 installed evaluator CLI parity（2026-09-06）

- 修复主命令 `multidetect evaluate-replay-set` 未暴露 `--prediction-run-receipt` 的接口漂移；安装入口现在与
  clean-checkout wrapper 共用同一 verifier、模型身份派生和 accuracy binding；
- synthetic HIL ONNX 端到端回归同时执行两个 CLI 表面并比较 receipt canonical hash。全量 3,510 tests：
  `3509 passed, 1 skipped`，Ruff 与 compileall 通过。

### v0.3.0-pre.8 v110 prediction-to-accuracy provenance binding（2026-09-06）

- evaluator 新增 `--prediction-run-receipt`，在计算指标前独立复验 prediction bundle，并从 receipt 派生模型
  artifact SHA、ID 与版本；调用者提供冲突元数据会 fail-closed；
- accuracy report 新增 receipt byte/canonical hash、model manifest SHA、runtime inference 与 synthetic HIL 绑定；
  readiness 的 fresh recomputation 会再次复验 receipt 并比较完整 binding，阻止预测、模型和指标交叉拼接；
- 保持旧无 receipt 报告兼容，现场 predictions/accuracy/release 仍未完成。新增回归后全量 3,510 tests：
  `3509 passed, 1 skipped`，Ruff 与 compileall 通过。

### v0.3.0-pre.8 v109 fixed-model replay prediction provenance（2026-09-06）

- 新增固定模型 replay prediction runner、installed CLI、clean-checkout wrapper 与 closed receipt schema；六个
  strata 共用正式 raw replay decoder、ONNX/TensorRT detector、letterbox/NMS/adapter 和 JSONL writer；
- 原子输出绑定 reviewed annotation 原始 manifest、固定 model/manifest、输入和阈值、provider/runtime、逐层
  predictions 与延迟统计；独立 verifier 重开完整 provenance 链并拒绝覆盖、类别/帧错配和内容篡改；
- test double、真实 runtime、production approval、synthetic HIL、accuracy/release claims 分离；实际 ONNX Runtime
  smoke 通过，但 field predictions 和 accuracy review 尚未完成。新增 4 项回归；全量 3,510 tests：
  `3509 passed, 1 skipped`，Ruff 与 compileall 通过。

### v0.3.0-pre.8 v108 reviewed replay annotation workspace（2026-09-06）

- 新增逐帧 annotation workspace：未标注为 `objects=null`，positive/negative 必须显式选择并通过类别/bbox
  约束，禁止遗漏帧冒充负例；
- finalizer 强制 annotator/reviewer 分离、UTC review time 和完整 frame assignment，在临时 sibling 中原子生成
  六份 truth、reviewed workspace、review receipt 与 replay manifest；
- verifier 从 intake 重推 assignment，并逐字节闭合 workspace/truth/manifest/receipt，篡改或 TOCTOU 不一致均
  fail-closed；predictions/accuracy/release 仍为 false。新增 4 项回归；全量 3,506 tests：
  `3505 passed, 1 skipped`，Ruff 与 compileall 通过。

### v0.3.0-pre.8 v107 immutable deployment replay intake（2026-09-06）

- 新增六分层 deployment recording intake、installed CLI、clean-checkout wrapper 和 closed receipt schema；
- 只复制经 raw replay 契约验证的 manifest/events/images，在 sibling temporary 中完成逐文件 hash、recording
  identity、frame inventory 二次回验后发布，拒绝覆盖、重复录像、symlink、路径逃逸和篡改；
- intake 不复制旧标注/预测，强制 annotation/prediction pending 与全部发布 claims=false，不能冒充人工复核、
  模型精度或部署批准；新增 5 项回归。全量回归 3,502 tests：`3501 passed, 1 skipped`，Ruff 与
  compileall 通过。

### v0.3.0-pre.8 v106 embedded replay-set readiness（2026-09-06）

- E7 evaluator 从绑定 replay manifest/accuracy report 实时重算 canonical readiness，并在报告内给出逐
  entry/stratum 的来源、标注、真值、预测、binding、阻断原因和下一步动作；
- readiness 保持诊断语义，不会错误否决独立 threshold-calibration；读取或复算异常仍 fail-closed；
- 当前 SIM fixture 精确显示 6/6 synthetic、6/6 未复核、五个正向 strata 无正真值/正预测，顶层仍为
  32 blockers。全量回归 3,497 tests：`3496 passed, 1 skipped`，Ruff 与 compileall 通过。

### v0.3.0-pre.8 v105 current release-gate evidence chain（2026-09-06）

- 将 Windows current release-gate wrapper 从旧 combined SIM、v65 parity 摘要和 v56 3-D model 绑定切换到
  当前 900 帧 combined source/release envelope、v105 external join/revalidation、PX4 response
  matrix/campaign 和按当前 Gazebo 资产重建的三机型 3-D records；
- 所有派生摘要均从既有不可变原始证据重算并由独立 verifier 通过；未重跑或伪造外部硬件/飞行接受；
- 默认 report-only 链现可完整执行并保持 32 个真实 blocker，现场证据过期仍 fail-closed。新增默认路径
  防回退断言；全量回归 3,497 tests：`3496 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。

### v0.3.0-pre.8 v104 E7 field-address provenance projection（2026-09-06）

- release manifest writer 从 field-link canonical verifier 生成 `address_state_bound`，不接受调用方自报；
- E7 evaluator 重开父预检和嵌套地址回执，对账实际 binding presence，拒绝顶层字段篡改和嵌套源漂移；
- 最终 `0.3.0` 要求显式携带该投影，历史 prerelease 仍可读；false 仍合法，以支持 DHCP/预置静态地址等
  受控部署方式；PX4 Mission parity 更新为 `7cd5cd2b...b32402f`，不提升任何现场或实飞声明；全量
  回归 3,497 tests：`3496 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。

### v0.3.0-pre.8 v103 bound field address receipts（2026-09-06）

- field-link preflight CLI/PowerShell wrapper 可接收成功 apply 地址回执，并要求接口、地址/prefix、父级
  source CIDR 与最多 300 秒新鲜度一致；
- 预检报告 hash-bind 回执路径、大小、SHA-256、schema 与身份；verifier 按父报告观测时间重开源证据，
  拒绝源漂移、字段篡改、软链接、自引用、what-if、策略放宽、CIDR 越界和接口 alias 分叉；
- 地址证据只提供配置 provenance，不替代 Jetson/GR01/相机探测，也不单独提升 hardware readiness；
  安全 parity 更新为 `d5b15bf4...fdbc87e`，其余四组不变。新增 7 项回归，本轮没有修改主机网络，
  产品继续保持 `v0.3.0-pre.8`，不产生 HIL/REAL/hardware/flight acceptance；全量回归 3,496 tests：
  `3495 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。

### v0.3.0-pre.8 v102 independently verified field address evidence（2026-09-06）

- 新增 `multidetect.field-link-address-state/v1` closed schema，统一 PowerShell plan/verify/what-if/apply 输出；
- 新增独立只读 verifier、CLI wrapper、console entrypoint 与根包 lazy export；重算地址/路由、plan change、
  elevation、安全标志、rollback、目标身份、时效和 apply postcondition；
- 未知/重复字段、重复 JSON key、NaN/Infinity、错误安全声明、伪造 apply 成功及 stale/future 证据全部
  fail-closed；非 apply 文档不能通过 `--require-applied`；
- producer/schema/verifier/CLI 全部进入安全 parity，`safety_policy_hash=10f115b1...de7e8a`，其余四组
  不变。新增 9 组回归；全量回归 3,489 tests：`3488 passed, 1 skipped`，全仓 Ruff 与 compileall
  通过。本轮未修改网卡，产品仍为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v101 field address postcondition verification（2026-09-06）

- `prepare_field_link.ps1` 只在 `ShouldProcess` 后的 add/remove 真正执行时记录 `changed=true`；非管理员
  `-WhatIf` 输出明确的模拟模式且不触发 UAC；
- apply/remove 后重读精确地址状态：添加要求唯一匹配且 `SkipAsSource=true`，移除要求不存在；审计
  输出记录 postcondition，条件失败时命令 fail-closed；
- 本机非提权 `-WhatIf` 已实测且未改网络；安全 parity 更新为 `46324cc0...3694af`，其余四组不变。
  产品仍为 `v0.3.0-pre.8`，未提升任何硬件、HIL、REAL 或实飞声明；全量回归 3,480 tests：
  `3479 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。

### v0.3.0-pre.8 v100 auditable field network preparation（2026-09-06）

- `prepare_field_link.ps1` 新增无副作用 `-PlanOnly` 与 write-new `-Output`：计划记录拟执行变更、提权需求、
  secondary-only/`SkipAsSource` 约束、不变更 gateway/DNS/explicit route 的保证及精确 rollback 命令；
- `-VerifyOnly` 可保存同源只读状态，输出拒绝覆盖，提权 child 精确继承输出路径；实际网卡 apply/remove
  仍要求显式管理员权限，本轮未修改主机网络；
- 全部 field-link 诊断、准备、runner/verifier 与聚合代码纳入 `safety_policy` parity，hash 更新为
  `792296e8...c7b74`。当前 3-sample 预检仍为 `hardware_link_ready=false`，产品保持
  `v0.3.0-pre.8`，未产生 HIL/REAL/hardware/flight acceptance；全量回归 3,480 tests：
  `3479 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。

### v0.3.0-pre.8 v99 verified journal index publication（2026-09-06）

- `journal-status --out` 在 write-new 后从磁盘运行完整 verifier，成功返回不再来自未落盘内存对象；
- 使用同一 `indexed_at` 重建 post-write 快照并与 pre-write 版本逐字比较，阻断 build/write 窗口内新增
  session/probe、campaign 前移和文件漂移；
- 失败时只回滚本轮新建的 index，保留 journal 与预存输出；PX4 Mission parity 更新为
  `4bdefec0...bbcc1a`；全量回归 3,480 tests：`3479 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。
  产品保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v98 stable journal snapshots and sealed blocked probes（2026-09-06）

- journal index 将 blocked probe 从裸计数升级为逐文件清单，绑定 path/file SHA、online evidence digest、
  observation time、runtime/vehicle 和失败原因；verifier 会重开并重算全部字段；
- 未引用 probe 必须通过 archived online verifier 且明确失败；伪造、篡改、删除、passing probe 冒充 blocked
  和 session probe 路径逃逸全部 fail-closed；
- index 构建前后要求 active capture lease 不存在，并双读 campaign status、session/probe 文件集及内容 SHA，
  拒绝采集中或构建中变化的混合快照；PX4 Mission parity 更新为 `f24e5bae...521a0`；全量回归
  3,478 tests：`3477 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。版本保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v97 recovered session lease reconciliation（2026-09-06）

- journal index 现在严格支持 released/recovered 两类互斥 lease 审计源，关闭成功 session 已落盘、但进程
  在正常 release 前崩溃后无法入册的窗口；
- recovered archive 必须同时具备有效 `lease.json` 与 `recovery.json`，并重新核对文件 SHA、canonical
  digest、lease ID、lease digest、完整 receipt 和 session 内嵌 lease；双归档、无归档及重新计算 digest
  后的错绑均 fail-closed；
- index schema 增加 archive kind 及 lease/recovery path/hash 条件约束，`lease-status` 增加 recovered 计数；
  PX4 Mission parity 更新为 `72cbc380...6fb4`，其余四组不变；全量回归 3,474 tests：
  `3473 passed, 1 skipped`，全仓 Ruff 与 compileall 通过。产品保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v96 crash-atomic covariance capture leases（2026-09-06）

- lease 先在非阻断 candidate 中 write-new/fsync，再原子发布完整 active 目录；release 原子移出 active path
  并保留 released audit，消除空 active 目录与先删身份文件的断电窗口；
- interrupted recovery 只允许同一 lease ID/reason 精确续跑，managed lifecycle 目录拒绝 symlink；
- journal index 重开 released lease 并与 session 内嵌内容逐字核对；全量回归 3,468 tests：
  `3467 passed, 1 skipped`，全仓 Ruff 通过。产品保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v95 exclusive covariance field journal（2026-09-06）

- 新增 receipt/workspace 专属 capture lease，串行化 online probe、capture 与成功 session seal；
- `lease-status/recover-lease` 实现同机只读 PID 生存判定、精确死亡 owner 恢复和 lease/recovery 审计保留；
- journal index 对账连续 session 前缀、campaign reports 与 blocked probes；全量回归 3,464 tests：
  `3463 passed, 1 skipped`，全仓 Ruff 通过。产品保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v94 sealed single-leaf covariance sessions（2026-09-06）

- 新增 `field_covariance_session run-next/verify-record`：每次从 v93 接收回执锁定唯一下一叶，将 receipt、
  workspace、campaign、online probe 与正式 report 串成不可变成功记录；
- `capture-next --probe-report-out` 在启动采集 subprocess 前 write-new 保存 online observation，并从磁盘按
  默认 300 秒时效、当前 next leaf、source/parity、mode binding 和 query budget 独立复验；失败不启动采集；
- online verifier 增加显式 archived 模式：完成采集后只放松“仍是 next leaf”，其余 schema、digest、source、
  parity、mode binding、identity、预算和报告当前状态仍重算，不能用归档 probe 再授权一次采集；
- 修复 portable workspace 生命周期冲突：移动介质/接收阶段仍严格 20 文件；接收后回执复验仅额外允许
  campaign 声明且正式 covariance verifier 通过的报告路径，未知文件和失败报告继续 fail-closed；
- CLI status output 禁止覆盖 campaign/source/report/probe/binding；session schema/module 纳入 PX4 Mission
  parity，`px4_mission_path_hash=f8e8f4e0...4acce1`；全量回归 3,456 tests：
  `3455 passed, 1 skipped`，全仓 Ruff 通过。版本保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v93 transactional covariance workspace receive（2026-09-06）

- `field_covariance_workspace` 新增 `receive`：先验源介质，按闭合 manifest 精确复制 20 文件到同盘临时
  目录，再复验 workspace digest、当前 parity 与六叶 source preflight 后原子落位；
- 禁止目标/回执覆盖、目标位于源 bundle 内或回执位于任一闭合 bundle 内；回执写失败会回滚本次新建
  目标，源介质保持不变；
- 新增闭合、canonical-digest-bound 的本机接收回执和 `verify-receipt`；移除源介质后仍会重开本机
  destination、逐文件 hash、campaign 与 preflight，不把源路径当作在线证据；
- 回执只证明静态接收事务，live identity/covariance/hardware/HIL/REAL/flight claims 固定为 false，接收机
  仍必须运行本机 `preflight -> probe-next -> capture-next`；
- receipt schema 与实现纳入 PX4 Mission parity，`px4_mission_path_hash` 更新为
  `6b5ab2ef...f85f02`。全量回归 3,445 tests：`3444 passed, 1 skipped`，全仓 Ruff 通过；产品继续
  保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v92 portable covariance campaign workspace（2026-09-06）

- 新增 `field_covariance_workspace export/verify`：导出前运行真实六叶 source preflight，并把
  HIL/REAL × fixed-wing/multirotor/VTOL 的 18 份 Mission/feedback/profile 原始字节复制进独立目录；
- 导出器重建只含 POSIX 相对路径的 `campaign.json`，固定新报告位于 bundle 内 `reports/`，不携带旧
  online probe、covariance report 或任何硬件验收状态；
- 闭合 manifest 绑定精确六叶顺序、endpoint/baud、20 文件全集、逐文件 size/SHA-256、原 campaign
  指纹、当前 release parity 与 workspace canonical digest；路径逃逸、ADS/反斜杠、symlink、额外文件、
  source/campaign/parity 漂移均 fail-closed；
- 搬迁测试在删除原导出目录后，从第二个根目录重新加载 relative campaign 并再次执行现有六叶静态门；
  endpoint、firmware 与签名身份不做“便携改写”，接收机必须重新执行在线 probe/capture；
- schema/module 纳入 PX4 Mission parity，`px4_mission_path_hash` 更新为 `b30a0dae...af266b`。产品仍为
  `v0.3.0-pre.8`，本功能不产生 HIL/REAL、hardware 或 flight acceptance；全量回归 3,439 tests：
  `3438 passed, 1 skipped`，全仓 Ruff 通过。

### v0.3.0-pre.8 v91 sealed and expiring covariance online preflight（2026-09-06）

- `probe-next` 输出升级为闭合 Draft 2020-12 schema-v2 observation，加入 UTC observation time、canonical
  evidence SHA-256 与不可提升的 release/hardware/HIL/REAL/flight=false claims；
- source binding 覆盖 campaign、Mission、feedback、runtime profile、schema、runner、mode-binding module
  和五组当前 parity；verifier 会重开当前 campaign，要求相同 leaf 仍是唯一 `next_leaf` 且六叶静态门全绿；
- 新增 `verify-probe`，独立重算 source hashes、authoritative mode binding、采样时序、identity response、
  最多三次查询预算、reasons 与 gate；默认 300 秒过期，未来时间、内容篡改、source/parity 漂移和 symlink
  均 fail-closed；
- 新 schema/module 纳入 `px4_mission_path_hash`，更新为 `792fffbe...100780`。本轮没有可用真实串口或
  现场 campaign，因此没有生成 HIL/REAL、hardware 或 flight 通过声明；全量回归 3,430 tests：
  `3429 passed, 1 skipped`，全仓 Ruff 通过；版本保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v90 current Pixhawk covariance identity probe（2026-09-06）

- campaign runner 新增 `probe-next`：六叶静态预检通过后，只探测当前 pending 叶，复用正式 covariance
  capture 的 `build_mode_binding`，不另写一套 endpoint/HEARTBEAT/firmware 判定；
- provider 禁用 ranging stream 请求，只允许有界 `AUTOPILOT_VERSION(148)` identity query；最多三次，
  `messages_transmitted` 必须精确等于 attempts，Mission/参数/模式/执行器路径保持关闭；
- probe 核对 configured/resolved endpoint、HEARTBEAT、firmware identity、transport/qualified link、
  operational qualification、read-only 与 hardware-control-disabled，并始终写出非 acceptance claims；
- 正式 `capture-next` 内部重新运行 online probe，失败时 `capture_started=false` 且不调用 capture subprocess；
  `px4_mission_path_hash` 更新为 `c7c86a1f...982ab218`；全量回归 3,419 tests：
  `3418 passed, 1 skipped`，全仓 Ruff 通过。没有真实硬件在线，未生成 HIL/REAL 通过声明。

### v0.3.0-pre.8 v89 field covariance source preflight（2026-09-06）

- 六叶 field campaign 新增无 transport I/O 的 `preflight`，逐叶重开 executable Mission、PX4 Mission
  feedback v2 和 signed runtime profile，并输出确定性的 READY/BLOCKED 清单与 source hashes；
- 预检绑定 fixed-wing/multirotor/VTOL adapter ID/hash、HIL/REAL mode、configured endpoint、baud、
  feedback gate、receive-only/zero-transmit、runtime signature/profile gate 与 observed identity；
- `capture-next` 在启动任何子进程前要求全部六叶 READY，并把预检结果带入 dry-run/正式结果；静态源漂移、
  output unlock 或 flight-control enable 均 fail-closed；
- 新增 12 项回归覆盖六叶顺序、八类语义错配、source post-check tamper、CLI 非零退出与 subprocess
  零调用；`px4_mission_path_hash` 刷新为 `3d3a77f6...ca52c95`；全量回归 3,415 tests：
  `3414 passed, 1 skipped`，全仓 Ruff 通过。本轮没有真实 HIL/REAL 采集声明。

### v0.3.0-pre.8 v88 field covariance campaign runner（2026-09-06）

- 新增严格 schema-v1 六叶 campaign 配置与可复制模板，启动前解析全部 Mission、feedback、profile、
  endpoint、report 路径并拒绝重复组合、未知字段、symlink 与 source/output alias；
- `status` 每次重验已有报告并给出唯一 next leaf；`capture-next` 默认一次只采一个明确的 mode/airframe，
  支持 dry-run，既有报告永不静默覆盖；
- 采集子进程退出 0 后仍强制重验落盘报告，复验失败返回非零；失败的不可变报告阻止继续采集，避免跳过
  现场异常；
- `verify` 复用 E7 六叶 builder，完整时输出六组 `--pixhawk-covariance-evidence` 参数，并可原子写出
  matrix binding；runner 与 Schema 均纳入 `px4_mission_path_hash`；全量回归 3,403 tests：
  `3402 passed, 1 skipped`，全仓 Ruff 通过。

### v0.3.0-pre.8 v87 E7 field covariance matrix（2026-09-06）

- E7 manifest 新增可选但一旦提供即 authoritative 的 `pixhawk_covariance`；最终 `v0.3.0` 强制要求
  HIL/REAL × fixed-wing/multirotor/VTOL 六份互异报告；
- 每叶重新运行 covariance observation schema-v3 verifier，并重新打开其 Mission、Mission feedback、
  signed runtime profile，核对 endpoint、baud、HEARTBEAT、firmware、current qualified link 与只读边界；
- 六叶 binding 与四模式 `px4_mission_path_hash` 交叉绑定；重复、缺失、未知组合、compact record 篡改、
  source 漂移或 parity 漂移全部 fail-closed；
- release manifest JSON Schema、writer API、CLI、evaluator、最终发布 blocker 与专项回归完成。当前仓库只有
  SIM 正证据，尚无六份真实 HIL/REAL 报告，因此最终版本仍保持阻断；全量回归 3,391 tests：
  `3390 passed, 1 skipped`，全仓 Ruff 通过。

### v0.3.0-pre.8 v86 strict covariance evidence schema（2026-09-06）

- 新增 Draft 2020-12 `pixhawk-odometry-covariance-observation` schema v3，所有保留对象默认
  `additionalProperties=false`；
- capture writer 在落盘前验证，verifier 在 digest/源码重建前再次验证；Schema path/hash 进入
  source binding，Schema 本身进入 `px4_mission_path` parity；
- evidence diagnostics 从宽泛 provider dump 收敛为 16 项门禁相关字段；tuple reasons 规范化为 JSON
  array，无 covariance 的失败报告仍可按闭合结构留证；
- `pixhawk` optional dependency 加入 `jsonschema>=4.20`；新增 Schema、未知/缺失/错类型、精简和失败报告
  回归；当前 PX4 SITL v3 实采 223/223 条通过并保存 113 条记录。

### v0.3.0-pre.8 v85 current qualified covariance evidence（2026-09-06）

- covariance observation 现在硬性要求采集结束时 transport link 与 qualified link 当前健康，且
  operational qualification 为 required/passed、无失败原因；
- firmware observation age 必须属于本次采集时窗，防止启动前缓存身份被复用为当前证据；
- HIL/REAL mode binding 同步绑定 provider transport、qualified link 和 operational qualification；
- 新增 SIM 报告与 HIL binding 六个 fail-closed 负回归；当前 PX4 SITL 实采 220/220 条通过，保存
  111 条 covariance，固件身份请求 1 次，总发送仍严格为 `3×7+1=22`。

### v0.3.0-pre.8 v84 bounded PX4 firmware identity request（2026-09-06）

- `PixhawkReadOnlyTelemetryProvider` 新增显式、默认关闭的 `request_autopilot_version`：只在绑定到 autopilot
  HEARTBEAT 后发送 `MAV_CMD_REQUEST_MESSAGE(512)` / `AUTOPILOT_VERSION(148)`，每个机体身份最多三次、
  5 秒退避，收到合法响应立即停止；身份变化会清空旧值并重置独立预算；
- diagnostics 新增请求 enabled/attempts/age/command/message/response 字段；总 transmitted counter 继续
  覆盖全部外发。默认 receive-only provider 仍为零发送，Mission/参数/模式/执行器路径未启用；
- covariance observation 强制要求实际请求和 64-hex firmware identity，并重算 `stream attempts×7 +
  identity attempts`；三次无响应即 fail-closed；
- 当前源码外部 PX4/Gazebo 实采 207 条 ODOMETRY/covariance、112 条记录；一次请求即收到 firmware
  identity，发送总数严格为 `3×7+1=22`，独立 verifier 通过且容器清理完成；全量回归 3,373 tests：
  `3372 passed, 1 skipped`，全仓 Ruff 通过。

### v0.3.0-pre.8 v83 field covariance identity binding（2026-09-06）

- 将 Pixhawk covariance observation 升级到 schema v2；HIL/REAL 必须绑定可执行 Mission、严格复验的
  PX4 Mission feedback v2 与签名 runtime profile，不再允许仅靠 `--runtime-mode` 生成现场声明；
- 当前采集会话必须与既有反馈的 configured/resolved endpoint、baud、HEARTBEAT system/component/
  autopilot/vehicle/MAVLink 身份和 `AUTOPILOT_VERSION` firmware hash 完全一致，并保持 read-only、
  hardware-control disabled、output locked 与 flight-control disabled；
- SIM 继续限制在隔离 local `udpin` 且拒绝携带现场身份材料；HIL/REAL 拒绝所有 UDP 14550 形式。
  verifier 会重新打开三份来源并重建 mode binding，来源变化或派生 claim 篡改后重算摘要仍会失败；
- pinned Docker/PX4/Gazebo 外部 SIM v2 实采 110 条 ODOMETRY/110 条有效 covariance、保存 83 条记录并
  完整复验；全量回归 3,369 tests：`3368 passed, 1 skipped`，全仓 Ruff 通过。HIL/REAL covariance
  工具门已实现，但真实 Pixhawk/Jetson/相机、室外 GNSS 与实飞仍未声明。

### v0.3.0-pre.8 v82 PX4 estimator attitude covariance（2026-09-06）

- 只读 Pixhawk provider 新增 MAVLink `ODOMETRY` 331 请求与严格 source/frame/time association，从
  21 项 upper triangle 提取 roll/pitch/yaw 3×3 covariance，验证 finite、正方差和 PSD 后发布带来源的
  `1σ`；无效新样本清空旧 sigma，避免 stale precision 复用；
- 修复 `ODOMETRY.child_frame_id=BODY_FRD` 时把 body twist 静默当作 north/east velocity 的语义错误；
- 新增 telemetry-only 采集/重建 verifier；即使篡改派生 checks/claims 后重算摘要也会被原始 records、
  diagnostics 与当前 provider/script/parity 重建拒绝；
- pinned Docker/PX4/Gazebo 外部 SIM 实采 64 条非零 estimator covariance，47 条记录与 gate 全通过，
  容器完成 stop/rm。`px4_mission_path_hash` 更新为 `16e8427a...e1930`；HIL/REAL、真实硬件、室外
  GNSS、人工 QGC 与最终发布状态不变；全量回归 3,358 tests：`3357 passed, 1 skipped, 0 failed,
  0 errors`，全仓 Ruff 通过。

### v0.3.0-pre.8 v81 three-airframe covariance evidence（2026-09-06）

- fixed-wing、multirotor、VTOL 分别在 pinned Docker/PX4/Gazebo 外部进程采集 3 帧 `MDGZSIM3`，
  runtime、sensor transport、逐帧 hash/replay 和 stop/rm cleanup 全部通过；
- frame provenance 与 runtime latest telemetry 现在封存最终 roll/pitch/heading sigma 及
  `gazebo_odometry_covariance_with_configured_floor` 来源，验收器重新从 wire covariance 计算并核对；
- 三类机型实采的 Gazebo 姿态 covariance 均为全零矩阵，证据明确记录 nonzero count=0，并应用
  `0.3°/0.3°/1.0°` 配置下限；不把 floor 误报成 estimator uncertainty；
- 增加 covariance evidence 篡改负测并更新 core parity。E7 仍有 32 blockers，HIL/REAL、真实硬件、
  室外 GNSS、人工 QGC 和最终 `v0.3.0` 状态不变。

### v0.3.0-pre.8 v80 Gazebo attitude-covariance wire（2026-09-06）

- 将 Gazebo sensor transport 升级为 `MDGZSIM3` / wire version 3 / 276-byte fixed header，把
  `PoseWithCovariance` 的 3×3 angular block 和像素、GPS、pose/twist 一起纳入整帧 SHA-256；
- producer 与 backend 同时验证 9 项 finite、对称和正半定语义；缺失或 malformed 6×6 covariance、
  被篡改 header、非 PSD 输入均 fail-closed，manifest/replay 保存 JSON-stable 的原始矩阵；
- 以数值 Jacobian 将 Gazebo fixed-axis X/Y/Z covariance 转为 NED/FRD Euler `1σ`，应用配置 floor，
  gimbal lock 退化为保守上界；实际 sigma/source 进入 `VehicleTelemetry` 和共享 ranging；
- 将 wire producer/backend 加入 `core_algorithm_hash`，避免 SIM 改变姿态语义却保留旧 parity identity。
  本轮未生成新的外部 Gazebo/PX4 source，不改变 E7 的 32 个 blocker。

### v0.3.0-pre.8 v79 attitude-uncertainty provenance（2026-09-06）

- `VehicleTelemetry`、schema-v1 replay 与 raw replay 新增可选的三轴姿态 `1σ` 和来源，历史记录在整组
  缺失时保持原 canonical identity；
- Live ranging 优先采用完整 telemetry uncertainty，只有整组缺失才采用显式配置 fallback；部分、负值、
  非有限或无来源元组 fail-closed，VI/RGB-SLAM 也改用同一 pose resolver；
- `RangeSolution` 与 operator ranging details 绑定实际使用的三轴 sigma/source，避免默认值在证据中
  冒充 estimator covariance；Gazebo wire-v2 尚未承载 covariance，后续协议升级前保持明确 fallback。

### v0.3.0-pre.8 v78 lever-arm geometry uncertainty propagation（2026-09-06）

- 将 ray uncertainty endpoint 扩展为成对的 NED ray 与 camera-offset 几何端点；姿态扰动会同时旋转
  光轴和 GNSS→camera 杆臂；
- camera-ground slant、DEM ray intersection、ground range 与 absolute-bearing sigma 统一使用这些端点，
  修复近距离/大杆臂场景仍按纯射线 bearing derivative 估计的问题；
- 增加旧实现会失败的非零杆臂 bearing 回归，并重跑 WGS-84、集成 SIM 与 Gazebo 定位相关测试。

### v0.3.0-pre.8 v77 strict field-evidence bindings schema（2026-09-06）

- 将 field evidence 的 `bindings` 从任意 object 收紧为 `additionalProperties=false` 的显式 schema；
- 对八项输入的 SHA-256/bytes、时间策略、target-event、recording、mount 与 calibration 绑定分别声明
  类型和 null 边界，pending 与 complete 报告均受同一合同验证；
- 增加完整/待采集报告 schema 正测和未知 binding 负测；该结构收口不提升任何现场验收声明。

### v0.3.0-pre.8 v76 target-event calibration cross-binding（2026-09-06）

- target-event replay 可复制并 hash-bind 实际相机标定，manifest 记录 contained path、bytes、SHA-256 和
  calibration ID；加载时重新哈希并执行完整标定语义校验；
- capture CLI 新增 `--camera-calibration`，同时支持 manifest 相对路径；field evidence 要求 replay 与
  mount survey 的标定 ID/SHA-256 完全一致，缺失或同 ID 不同文件均 fail-closed；
- target-event manifest schema 纳入 shared core algorithm parity；测试仅证明软件证据合同，不提升
  HIL/REAL、硬件、室外 GNSS 或最终发布声明。

### v0.3.0-pre.8 v75 equipment calibration provenance（2026-09-06）

- 新增严格 equipment-calibration schema 与 `equipment-record` CLI，自动 hash-bind 原始证书文件；
- camera-mount survey 重新验证嵌套证书、issuer、有效起止时间、设备 ID 和角度/线性精度，并要求
  survey 时间落在有效窗口；
- 增加过期、错设备、证书事后篡改和 CLI record 生成回归；schema 纳入 shared task parity。

### v0.3.0-pre.8 v74 mount-survey field worksheet and preflight（2026-09-06）

- 新增 `template` CLI，原子生成固定 CSV v1 表头和 5–1000 个稳定 sample ID，默认拒绝覆盖及 symlink；
- 新增 `validate-raw` 只读预检和严格 schema，在正式 evidence 创建前检查 hash、结构、数值、范围、
  最小样本数与 ID 唯一性；预检明确不产生硬件、HIL/REAL 或发布声明；
- 新增函数/API 导出、CLI round-trip 测试并将预检 schema 纳入 shared task parity。

### v0.3.0-pre.8 v73 raw mount-survey CSV single source（2026-09-06）

- 将 hash-bound 原始 CSV 设为 camera-mount survey 唯一统计数据源，固定列顺序并严格解析有限数值；
  CLI 删除会与原始文件分离的 `--sample` 输入。
- 独立 verifier 重新解析同一 CSV 并逐字段比较序列化 samples；API 若显式传入不同样本，报告以
  `samples_match_raw_measurements=false` fail-closed。
- 更新 shared task parity、现场 runbook 和回归；该改动只增强 provenance，不产生真实硬件声明。

### v0.3.0-pre.8 v72 physical camera-mount survey gate（2026-09-06）

- 新增 `camera_mount_survey.py`、CLI、严格 JSON Schema 与独立 verifier：重复测量、设备校准证书、
  原始记录和 camera calibration 全部 hash-bound，并强制独立复核与 FRD/实体安装声明。
- field evidence bundle 从六层扩展为七层，新增 `camera_mount` 强制层及 survey/camera/aircraft/GNSS antenna/
  calibration hash bindings；缺失或篡改只会关闭门，不会提升 hardware/release 声明。
- 将 mount survey 与 field bundle 合同纳入 shared task parity。测试 fixture 只证明 fail-closed 合同，
  真实机载测量与 HIL/REAL 仍未完成。

### v0.3.0-pre.8 v71 干净 HEAD Combined 复验与 provenance 收口（2026-09-06）

- 在提交 `73429e505d33ca5c645be3d3d0b6cda667f33c5f` 的干净工作树上重新执行完整 900 帧
  PX4/Gazebo/QGC Combined SIM；source 明确记录同一 Git HEAD、`dirty=false` 和空 status SHA-256，
  消除 v70 提交前机器证据的 provenance 缺口。
- `combined-sim-qgc-20260905T175019Z-64536` 为 259,353 bytes / SHA-256
  `5031EEFE75CF78D42AECCC04E990BB8F9975FBC6EBB12D4087F685C18652C8B6`；900/900 sensor、RTSP、
  type-23/type-24 与 full odometry 通过，832 份 range/geolocation 中 336 份合格落图、496 份
  fail-closed withheld，operator transport error 为 0。
- QGC 在 exact PID/HWND 上第一次命中 source-bound `flame` 候选框，相关 `SELECT_TRK` ACK、
  `TRACKING` 和 QML `TRK` adoption 属于同一 target；最大 frame-to-acceptance 为 `609 ms`，
  最大 ACK-to-metadata skew 为 `235 ms`。
- 7-item Mission 完成 MC→FW→MC、AUTO.MISSION、land/disarm；最大离家 `226.949 m`、最大相对高度
  `26.078 m`。容器移除、9 个动态测试端口释放、14550 保持不变。
- 独立 combined verifier 44/44 通过，报告 2,139 bytes / SHA-256
  `0C27451F88A0FD3655740523525D97BAE98D8D981EE0A3CC60DB8178B9A1136D`。该结果仍只关闭
  software/SIM 机器 provenance 子门，不提升人工视觉、真实硬件、室外 GNSS/RTK、HIL/REAL 或实飞门。
- 全仓 3,317 tests：`3316 passed, 1 skipped, 0 failed, 0 errors`；JUnit 482,309 bytes / SHA-256
  `4F05DBB3A4BA63DDD1DB31E55DF904FE0739E8DE3D362D21E23F727CD9C17106`，Ruff 通过。

### v0.3.0-pre.8 v70 原生目标点击、测距与 GPS 落图 Combined 闭环（2026-09-06）

- 原生 QGC Fly View 现在可按 source-bound 候选框中心发送 `SELECT_TRK`，并以同一 command/target 的
  ACK、TRACKING 与 QML adoption 证明真实点击后进入 TRK；不依赖测试专用自动状态注入。
- active operator target 接入共享 multimodal ranging 和 geolocation 发布；质量合格才给出 WGS-84/1σ，
  不合格则明确不可定位，保持 display-only、0 飞控与 0 物理载荷输出。
- Gazebo VTOL camera pose/标定统一为 35° 俯角。修复 range contribution 独立四舍五入导致
  `254/254 + 1/254` 的 wire 自校验失败；新量化保留非零小来源且总预算不超过 254。
- current-parity 900 帧 Combined source `combined-sim-qgc-20260905T172856Z-51520` 通过：831 份 range、
  831 份 geolocation、112 个合格坐标、0 transport error，900/900 type23/type24；VTOL attached 与
  7-item Mission 同时通过，容器/端口清理完成。
- portable EXE 为 46,579,712 bytes / SHA-256
  `FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`；版本继续保持 prerelease，
  人工视觉、真实硬件、室外 GNSS/RTK、HIL/REAL 和实飞门仍未关闭。
- 全仓 3,317 tests：`3316 passed, 1 skipped, 0 failed, 0 errors`；Ruff、QGC self-tests、portable
  增量构建和 current-parity Combined 均通过。

### v0.3.0-pre.8 v69 combined-SIM 独立传感器硬门（2026-09-05）

- 独立 combined verifier 现在强制 CameraInfo 为 `validated_k_p_rectification_plumb_bob_distortion`、
  sensor expected/received/complete 一致，且每个 expected frame 绑定 full odometry pose/velocity。
- combined wrapper 在 source 生成前也断言 CameraInfo K/P/R/D 状态；新增三类篡改负测并更新 E7 fixture。
- 当前源码 `46920e1` 的权威 source
  `combined-sim-qgc-20260905T152520Z-75420/combined-evidence.json` 为 255,256 bytes，SHA-256
  `9CAB1BC405571C2F287D1545D77B7EA1F7C4A56CC163AB76E1402CB362DB121B`；900/900 sensor、RTSP、
  type-23、type-24 和 odometry 全部通过。
- 增强后的独立报告为 2,139 bytes，SHA-256
  `1EEFD154DF164F307D62EF1CFFCC7479E343B8E3FC6A7017A9BBE61EB19502D0`，44/44 checks 全绿。
- 当前 HEAD 全仓 3,309 tests：`3308 passed, 1 skipped, 0 failed, 0 errors`；JUnit 481,013 bytes、
  SHA-256 `3AF69527D05CCC985502C195D09DDE44793D7280111B04410B8B2DCB3C3661C6`，ruff 全仓通过。
- 产品仍是 `v0.3.0-pre.8`；本轮未产生人工签字或 HIL/REAL/硬件/室外 GNSS/RTK 证据。

### v0.3.0-pre.8 v68 CameraInfo 语义标定与 900 帧联合复验（2026-09-05）

- Gazebo `CameraInfo` 不再只检查 topic、时间戳和图像几何；helper 现在逐字段验证 `K[9]`、`P[12]`、
  `R[9]`、`PLUMB_BOB` 与 `D[5]`，并与仓库 `gazebo-front-camera-v1` 标定绑定。非有限数、非标准
  pinhole 布局、stereo translation、畸变或标定漂移均 fail-closed。
- Gazebo 的 32-bit intrinsic 序列化会给焦距带来约 `3.53e-5 px` 的量化差；焦距/主点使用有界
  `1e-4 px` 容差，R/D 仍使用 `1e-12`。真实三帧诊断证明 K/P/R/D 后才提升状态为
  `validated_k_p_rectification_plumb_bob_distortion`。
- backend sensor identity 加入 canonical camera calibration hash；CLI 和 combined wrapper 必须显式传入
  标定，状态同时进入 runtime result 与 persisted frame-set source attribution。
- QGC 原生 PiP 指针交互改为最多 8 次有界精确重试；每次仍要求 exact coordinate、QGC PID/HWND ownership
  与 foreground 同时成立。真实联合复验第一次即命中，没有放宽 UI 门禁。
- 权威 source `combined-sim-qgc-20260905T150510Z-27780/combined-evidence.json` 为 256,194 bytes，
  SHA-256 `E800968AB63BF847CA53BE7798EAD3B2501B731E958D76AB5E8EAC447A999471`；900/900 sensor、
  RTSP、type-23、post-adoption type-24 和 odometry 全部完成，独立 verifier 41/41 通过。
- 7-item PX4 Mission 完成 upload/readback/commit/START_PERMITTED、AUTO.MISSION、VTOL MC/FW/MC、land/disarm；
  最大水平距离 `129.057 m`、最大相对高度 `22.082 m`。容器移除、9 个动态端口释放、14550 未改变。
- 当前 HEAD 全仓 3,306 tests：`3305 passed, 1 skipped, 0 failed, 0 errors`；JUnit 480,349 bytes、
  SHA-256 `0414978C7C13B2726CCA9CB94153F018D5AC16B8A399748255E52F9C08DA64AA`，ruff 全仓通过。
- 该结果关闭 CameraInfo K/P/R/D 机器语义子门和 software-only SIM 联合机器门；四模式人工视觉仍为
  0/4，HIL/REAL、真实硬件、室外 GNSS/RTK 和实飞仍 pending，最终 `v0.3.0` 继续阻断。

### v0.3.0-pre.8 v67 真实 900 帧联合 SIM 时间绑定收口（2026-09-05）

- Gazebo Harmonic 的 `CameraInfo` 被明确建模为会话级静态标定：每帧仍校验正时间戳、因果性和
  `Image` 几何一致性，但不再错误套用 NavSat/Odometry 的 1.5 秒动态遥测时效门。
- NavSat/Odometry 保持严格的 1.5 秒上限。独立 transport worker 短暂错序时，只丢弃无法安全绑定
  动态遥测的图像并等待新鲜样本；持续过期最终由 helper 有界超时 fail-closed，不输出旧 GPS 帧。
- 真实首次重跑在 532 帧暴露 NavSat 7 秒错序并正确失败；修复后同一 pinned Docker PX4/Gazebo、
  portable QGC 和当前源码完成 900/900 image/RTSP/type-23/type-24，4 张错序图像被安全丢弃后恢复。
- 权威证据 `combined-sim-qgc-20260905T142324Z-32372/combined-evidence.json` 为 255,480 bytes，
  SHA-256 `FD386165212A68C6E2D053A0D58B05D796565AB716CB088439F0A6756EDC1090`；独立 verifier
  41/41 checks 全部通过。容器已移除、9 个动态 loopback 端口全部释放、14550 未使用。
- 四模式 machine-binding adapter 对该真实 source 的 SIM 绑定 19/19 通过；用同一 source 冒充 HIL 时
  `runtime_mode_matches` 必然失败，证明机器证据不能跨模式重标。
- 全仓 3,294 tests：`3293 passed, 1 skipped, 0 failed, 0 errors`；JUnit 478,731 bytes、SHA-256
  `8261544D868E9222A22D7E5EFE5E1F437B19ECEFF5DB1567ECC02C3C38F617CD`。
- 该结果只关闭 software-only SIM 联合机器门；相机 K/P/畸变语义验证、人工四模式视觉、HIL/REAL、
  真实硬件、室外 GNSS/RTK 和实飞仍为 pending/not_claimed，最终 `v0.3.0` 继续阻断。

### v0.3.0-pre.8 v66 四模式 QGC 机器捕获统一契约（2026-09-05）

- `capture_qgc_native_style.ps1` 与新增 `qgc-native-style-capture.schema.json` 定义 schema v2，强制显式
  `runtime_mode`，并绑定 run ID、QGC EXE
  路径/字节/SHA-256、当前 release-parity 路径/字节/SHA-256/五组摘要、exact PID/client rect、遮挡和 PNG。
- `qgc_visual_acceptance.py` 新增通用 native-v2 与 legacy PX4-HIL 两个适配器。新契约可原生承载
  REPLAY/SIM/HIL/REAL；旧 `px4_sitl_qgc_operator_acceptance` 只允许作为 HIL 来源，不能冒充其他模式。
- native-v2 对错误 event/version、模式不匹配、PID 绑定失败、无效 client rect、外部遮挡、截图/EXE/parity
  身份漂移、非 PNG、机器冒充人工签字或 UI contract 过期全部 fail-closed。HIL wrapper 已显式传入
  `hil`、run ID 和 canonical parity。
- 全仓 3,286 tests：`3285 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,631 bytes、SHA-256
  `15F37922F866E1FCFBEDE011A461185333E25244B70447629955363635356C78`。Ruff、compileall、全部相关
  JSON Schema、PowerShell parser 和 release parity 重算通过；parity 文件及五组哈希均未变化。

### v0.3.0-pre.8 v65 四模式 QGC 人工视觉 E7 门（2026-09-05）

- 单份视觉记录新增强制 `runtime_mode` 并复验 machine report 的模式身份；v61 exact PID/HWND 捕获只
  归属 HIL，不能冒充其他模式。
- E7 writer/evaluator/schema/CLI/wrapper 新增 `qgc_operator_visual_acceptance` 集合。最终 release 要求
  replay/sim/hil/real 各一份独立通过记录；重复 mode/evidence、复用 machine report 或相同 screenshot、
  任一源漂移都会 fail-closed。
- 当前没有人工 accepted record，集合保持 `present=false/passed=false`，现有 32 条发布阻断不减少。
- `px4_mission_path_hash` 更新为
  `F0804FEB609A9CBBBCE6E3932ED77BD7852C9AF71BDB612A76BBD56EAD12247A`；matrix、campaign、external
  join/revalidation 和当前 E7 均按新 parity 重建并独立复验通过。
- 全仓 3,282 tests：`3281 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,082 bytes、SHA-256
  `C893DDEDEE6A07F659F354C0C3DD13214DFA81014F08EEC380F977ED4C77EE7A`。Ruff、compileall、Schema、
  PowerShell wrapper 与 E7/campaign/matrix 独立复验通过。

### v0.3.0-pre.8 v64 QGC 人工视觉验收合同（2026-09-05）

- 新增 `multidetect.qgc_visual_acceptance` 与 `multidetect-qgc-visual-review` CLI；人工 review 绑定 exact
  PID/HWND 机器报告、原始 PNG、实际 QGC EXE、当前 UI contract 和 geolocation fail-closed 负控。
- 新增 12 项严格 checklist、JSON Schema、内容摘要和独立 verifier；截图、EXE、parity、机器报告或
  负控任一字节漂移都会关闭验收。
- 真实 v61 screenshot/source 与 v58 近地平线负控已通过 source-binding 预检；负控 5/5 有检测、0/5
  目标坐标。人工尚未逐项确认，因此 `operator_visual_acceptance` 与 E7 总门继续保持未声明/阻断。
- 新增 fail-closed checklist 模板和 7 项回归，覆盖 schema、完整通过、截图/记录篡改、负控假点、缺项、
  rejected decision 和 CLI template。
- 全仓 3,276 tests：`3275 passed, 1 skipped, 0 failed, 0 errors`；Ruff、compileall、JSON Schema 和
  真实 v61/v58 source-binding 预检通过。

### v0.3.0-pre.8 v63 三机型 PX4 响应重复性与故障注入（2026-09-05）

- Docker AI acceptance runner 新增仅限 disposable loopback PX4 SITL 的目标刷新中断、心跳消费中断和
  PX4 模式退出注入；三种故障都记录 requested/activated/observed/fail-closed，负向运行保持 runtime
  gate 通过并由对应飞行安全门拒绝。
- 实际执行固定翼、多旋翼、VTOL 各两次正常位移响应与每机型三种故障，共 `6` 个 positive 和 `9` 个
  fault 报告。正常运行全部使用 PX4 `GLOBAL_POSITION_INT`，负向覆盖 `9/9` 且全部 fail-closed。
- 新增 campaign builder/verifier/JSON Schema，统计每机型位移、持续时间、position samples、目标更新率
  的 minimum/P50/P95/maximum，并绑定全部源文件、scenario、工具源码与 release parity。
- E7 writer/evaluator/schema/CLI/wrapper 新增 `px4_reported_position_response_campaign`；当前 manifest 中
  matrix 与 campaign 均为 `present=true`、`passed=true`，总体仍为 32 个真实 blocker。
- canonical `px4_mission_path_hash` 更新为
  `4B5A95F3A89F35557820FD530A105B011EC0095E8E84F24701291AC29DA802DD`；matrix、campaign、external join、
  revalidation 和 E7 均按该 parity 重建。硬件、HIL、REAL、人工视觉和实飞继续 `not_claimed`。
- 全量回归 3,269 tests：`3268 passed, 1 skipped, 0 failed, 0 errors`；全仓 Ruff、compileall、Schema、
  campaign/matrix 独立复验和 E7 wrapper 均通过。

### v0.3.0-pre.8 v62 三机型 PX4 reported-position 动态响应矩阵（2026-09-05）

- 新增固定翼、多旋翼、VTOL 外部 PX4/Gazebo 动态响应矩阵生成器、独立验证器和 JSON Schema；
  三机型均通过同一 truth-free perception/AI/authority/watchdog/MAVLink `MANUAL_CONTROL` 路径。
- 使用 PX4 `GLOBAL_POSITION_INT` 实测最大水平位移 `33.872547 / 1.171039 / 3.784929 m`，禁止读取
  Gazebo 真值或注入 pose；目标刷新、heartbeat、POSCTL 连续性、pinned image 和清理门全部通过。
- E7 manifest writer/evaluator/schema/CLI 与当前 pre.8 wrapper 新增 hash-bound
  `px4_reported_position_response_matrix`，每次重开三份源证据并复算矩阵；final release 缺失该项时
  fail-closed。
- `px4_mission_path_hash` 刷新为
  `B79C941AF889BAB50B2F42E674EC6662921233926E1C9271A65CEB9FC2702E55`，相关 external join 与 E7
  证据按新 parity 重建。该结果只关闭 SIM 动态响应子门，不声明硬件、HIL、REAL、实飞或人工视觉。
- 全量回归 3,261 tests：`3260 passed, 1 skipped, 0 failed, 0 errors`；全仓 Ruff 和 compileall 通过。

### v0.3.0-pre.8 v61 原生 QGC 定位卡片/地图 exact-PID 机器捕获（2026-09-05）

- HIL driver 增加 bounded post-authorization refresh：定位 evidence 在截图窗口内持续以同 target/frame
  绑定发送 type 10/22；同时强制比较 evidence 内五组 parity 与 canonical `release-parity.json`，旧 UI
  parity、错误版本、失败门或非 display-only 输入均在传输前 fail-closed。
- `capture_qgc_native_style.ps1` 可附着已运行的 exact `MultiDetectGCS` PID，不会关闭所附着进程；捕获前
  将窗口扩到至少 `1600x900`，选择能容纳完整布局的屏幕，保存/恢复 QGC 与外部窗口状态，并记录 exact
  client rect、PID、标题、遮挡和 PNG SHA。`run_px4_sitl_qgc_operator_acceptance.ps1` 将该路径接入同一
  pinned PX4 SIH/只读 router acceptance。
- QGC isolated-HIL 自动退出新增最长 15 秒的验收专用延时；机器捕获使用 8 秒延时，随后仍走 QGC 自身
  受控退出。普通 acceptance 延时为 0，行为不变。增量构建与 portable 部署完成，Release/portable EXE
  均为 46,576,640 bytes，SHA-256
  `62F832239C375998E8431985DA02BB063ABEBA69BDB551E46527D2EFD97F747F`。
- current parity 下重跑 high-slope 定位为 5/5 落图；source 文件 SHA-256
  `548905E36A076240F3C4DEC6DC09540831456E175AC4A372434827D1A5F7BBE4`。最终 HIL evidence
  `gate_passed=true`，QGC exit 0、认证 132 包、type 10/22 各 47、PX4 heartbeat 14、写入/物理输出 0、
  14550 不变且资源全清理；evidence SHA-256
  `BF9E9856F91244161CDB38E8FE5DDA9BC6972A0BFC17E989A427BDAA296A2861`。
- 最终 exact-client PNG 为 2,580,889 bytes / SHA-256
  `86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3`，显示完整定位卡片与地图目标。
  这关闭的是 machine dynamic-render capture 子门；本线程没有 `@oai/sky`，所以人工视觉、室外 GNSS、
  硬件 HIL/REAL、实飞和最终 `v0.3.0` 均未声明通过，E7 blocker 仍为 32。
- 全量回归 3,257 tests：`3256 passed, 1 skipped, 0 failed, 0 errors`；JUnit 473,487 bytes、SHA-256
  `E5D307C7F4AC57A4198358FBF84CFD034B27C20F415F2D1B7BF8F49850CB0837`。

### v0.3.0-pre.8 v60 Gazebo 定位证据进入原生 QGC（2026-09-05）

- 扩展既有 `operator_closed_loop_hil.py`，可读取 schema v2 且门禁通过的 Gazebo 定位 evidence，
  保留文件/内部确定性摘要和原始场景身份，把最后一帧测距与 WGS-84/1σ 结果重绑定到同一个实时
  HIL target/frame，再通过现有 MAVLink 2 signing operator link 发送 type 10/type 22。失败门、非
  display-only 证据和不完整帧均在传输前 fail-closed。
- 扩展 `run_px4_sitl_qgc_operator_acceptance.ps1` 的可选
  `-TargetGeolocationEvidencePath`：QGC 必须认证 range/geolocation 两类消息，且路径、包计数和安全边界
  必须进入 acceptance JSON；仍使用 pinned disposable PX4 SIH、localhost 独立链路和只读 router。
- 实际 v60 验收 `gate_passed=true`：原生现有 EXE 认证 40 个 metadata 包，其中 type 10/type 22
  各 1 个，PX4 autopilot heartbeat 6 个；operator tunnel→PX4、forbidden PX4、FTP write、arm、Mission
  upload、physical payload 全为 0，UDP 14550 所有权不变，端口/容器/进程全部清理且无新 crash dump。
  evidence 文件 SHA-256 为
  `9E40159EEC767CD8F1AEF106BCC7E48ED9C00BC0DDFC7C1E4CECC7D2A060D053`。
- 全量回归 3,255 tests：`3254 passed, 1 skipped, 0 failed, 0 errors`，JUnit 473,177 bytes / SHA-256
  `0BD69A52C24CC8EC1438988CBB54A2223DFC656FB10D31F6C80D18B0DA725BE5`；Ruff、compileall、diff check
  通过，临时 parity 与 canonical SHA-256 均为
  `C670B2AAE30C392C2B7FBB981EDB58719EC66E4112B51F135E69AC662AFE152B`。
- QGC C++/QML 和五组 release parity 未改变，EXE 仍为既有 `0.3.0-pre.8` portable 字节。该结果只关闭
  evidence→signed wire→native controller 的 machine transport/adoption 子门；地图/1σ/卡片人工视觉、
  E7 接入、真实 GNSS、硬件 HIL/REAL 和最终 `v0.3.0` 仍未声明通过，E7 blocker 仍为 32。

### v0.3.0-pre.8 v59 三档坡高/距离 Gazebo 定位矩阵通过（2026-09-05）

- 新增 `gazebo_car_geolocation_mid_slope_001` 与 `gazebo_car_geolocation_high_slope_001` 两套完整
  scenario/runtime/truth/expectations、GeoJSON、DEM 和 SDF 资产；二者分别以约 `12 m / 15 m / 4 deg`
  和 `20 m / 25 m / 8 deg` 扩展原 `6 m / 8 m / 0 deg` 平地工况，所有 manifest channel SHA-256、
  DEM/SDF/truth、WGS-84、billboard 几何和相机投影均有自动回归。
- 在 disposable PX4/Gazebo Docker 中分别采集两组 5 帧实际 BGR24 camera/NavSat/Odometry 数据；
  mid/high Docker evidence 均 `gate_passed=true`，容器 stop/rm 成功且无残留。两组像素经同一
  quarantined ONNX candidate、共享 anchor、DEM/GNSS ranging 与 WGS-84 geolocation 后均 5/5 落图，
  独立 verifier 都 `passed=true/reproduced=true`。
- 三正一负工况矩阵现 `gate_passed=true`：正向 `3/3` 场景、`15/15` 帧，平均 AGL
  `6.195847 / 12.168137 / 20.149371 m`，距离 `8 / 15 / 25 m`，坡度
  `0 / 4.004173 / 7.969610 deg`；三档最大水平误差分别为 `0.674040 / 1.175656 / 1.855370 m`，
  各档 1σ 与 range CI95 coverage 都为 `1.0`。负向仍为 `1/1` 场景、5/5 检测、0/5 坐标。
- Gazebo helper 现在只在首个有效 Odometry 和首帧之前有界丢弃最多 8 个官方
  OdometryPublisher 的出生点速度派生毛刺；第 9 个、首个有效样本之后或任何已接收帧后的相同异常仍
  立即 fail-closed。Docker acceptance 出错时会先安全关闭 backend，再报告 helper exit/stderr。
- 全量回归 3,251 tests：`3250 passed, 1 skipped, 0 failed, 0 errors`，JUnit 472,512 bytes /
  SHA-256 `9F62B10AA176760E9F4CB8C4431CAF62EA9E28748D22C42C749D7C942DD100E1`；Ruff、compileall、
  `git diff --check` 通过，临时重建 parity 与 canonical 文件逐字节一致。
- 本轮只关闭 software/SIM 多工况定位机器子门；E7 尚未接入该输入，仍为 32 blockers。候选模型、
  QGC 人工视觉、室外 GNSS、真实相机外参、硬件、HIL/REAL、实飞和最终发布状态均未提升。

### v0.3.0-pre.8 v58 多高度/距离/坡度目标定位矩阵门（2026-09-05）

- Gazebo camera → shared ranging → WGS-84 acceptance evidence 升级为 schema v2：绑定评估器源码
  SHA-256，并从绑定的逐帧 telemetry/truth/DEM 派生 AGL 高度、真实地面距离及目标位置坡度；truth
  仍在全部 prediction 完成后才读取，不进入 detector/ranging 输入。评估器字节变化会使旧证据 fail-closed。
- 新增 `multidetect-gazebo-geolocation-matrix` 与
  `scripts/run_gazebo_geolocation_regime_matrix.py`。矩阵会先逐份重开原始 scenario、frame-set、模型、
  manifest 和当前 parity，完整复算每份证据，再要求至少 3 个相隔 `5 m` 的高度档、3 个相隔 `5 m`
  的距离档、2 个相隔 `3 deg` 的坡度档、3 个/15 帧正样本，以及 1 个/5 帧的零坐标负样本；所有
  source 必须具有相同 release/evaluator identity。
- 当前 v58 实际正向证据仍为 5/5 落图，最大/平均误差 `0.674040/0.651476 m`；近地平线负向仍为
  5/5 检测、0/5 坐标。两份 schema-v2 verifier 都 `passed=true/reproduced=true`。
- 当前矩阵报告按设计 `gate_passed=false`：正样本 `1/3`、正帧 `5/15`、高度档 `1/3`、距离档
  `1/3`、坡度档 `1/2`；零坐标负样本 `1/1`、负帧 `5/5` 已满足。没有用单一平地场景冒充多工况
  定位完成，也没有提升 QGC 人工视觉、户外 GNSS、HIL/REAL、硬件、实飞或生产批准。
- 全量回归 3,241 tests：`3240 passed, 1 skipped, 0 failed, 0 errors`，JUnit 470,851 bytes /
  SHA-256 `08FEBF2327722653503715CF1180ACA2FABEE7578F5937D734430C6CCC9FE7F0`；全仓 Ruff check 与
  compileall 通过。历史 Ruff format baseline debt 保持独立，不在 v58 功能提交中批量改写。

### v0.3.0-pre.8 v57 可复现的 Gazebo 像素到 WGS-84 定位门与仓库状态重整（2026-09-05）

- 以 GitHub `origin/main` 的 `b5c3f10baf4cc9febaa3a10103752f202f3b0611` 为审计基线，重新盘点
  当前源码、功能、证据、版本和门禁；原 10,602 行 README 迁入 `docs/history/`，根 README 改为稳定
  入口，并新增人/机可读 `docs/current-progress.*`、更新规范和 PR 检查表。以后功能更新必须同步
  CHANGELOG、当前进度、路线图/专题文档和相应测试，聊天记录不再充当唯一状态源。
- 收口远端已经存在但缺少内部 helper/公开导出的 Gazebo geolocation verifier。它现在会重新打开并
  严格约束 repository-contained scenario、frame-set、候选模型和 manifest，重新计算五组 release parity，
  重跑 detector/ranging/WGS-84，再比较去时间戳确定性摘要；新增正向复现和 shared-core 改字节后旧证据
  失效的回归。
- `target_image_anchor.py` 正式纳入 `core_algorithm` parity。删除无 `model.config`/`model.sdf`、无引用且
  会污染正式 model-root 摘要的 v2 孤立重复纹理；该字节仍可从基线提交恢复。新 parity 文件 5,146 bytes，
  SHA-256 `C670B2AAE30C392C2B7FBB981EDB58719EC66E4112B51F135E69AC662AFE152B`，core/safety hash 为
  `505E5E8D...F0AD9` / `DAF5AF66...D710A`。
- 当前 parity 下重跑实际 Gazebo 像素定位：正向 5/5 检测和落图，最大/平均水平误差
  `0.674040/0.651476 m`，最大帧间漂移 `0.035197 m`，1σ 与 range CI95 覆盖率均为 `1.0`；证据文件
  SHA-256 `5AA0257D...AC08`，独立 verifier `passed=true/reproduced=true`。近地平线负向 5/5 检测但
  0/5 坐标，理由为 `target_ray_does_not_intersect_ground_safely`，负向 verifier 也完整复现。
- 从六份不可变 historical external runtime/AI source 重建当前 parity 三机型 join，join/revalidation
  均通过且 `source_recomputed=true`；这不是新的外部 Gazebo 运行或硬件声明。E7 report-only 重新生成，
  `gate_passed=false`、`release_allowed=false`、32 blockers，最终 `v0.3.0` 继续阻断。
- 全量回归 3,233 tests：`3232 passed, 1 skipped, 0 failed, 0 errors`；唯一 skip 是 Windows symlink
  权限项。JUnit 469,702 bytes / SHA-256
  `2FD79F5349A56DBDB72EB6AD1A77653707310D141F99CF996A36FFA3A23A9B6F`；全仓库 Ruff check 通过。
  候选模型继续 `quarantined`，QGC 人工视觉、真实硬件、室外 GNSS、HIL/REAL、实飞与生产模型批准
  均未声明；v57 未改 QGC C++/QML，不重复链接 EXE。
- 全仓库 Ruff format baseline 仍有 410 个历史文件未格式化（`ground-control/src/tests/scripts` 为
  `205/95/86/24`，其中 31 个是 parity 绑定 Python 源）。本轮不混入数百文件机械改写；该项已登记为
  独立迁移债务，必须在格式化后重建 parity、相关 evidence 和 E7，现有 manual validation format step
  在此之前仍会失败。

### v0.3.0-pre.8 v56 Gazebo 相机像素与候选检测三控（2026-09-05）

- 三个组合机型的 `mono_cam` merge include 显式改为 `pose relative_to="base_link"`，避免相机按组合 model
  frame 落到地面以下；相机固定 joint 继续使用同一 base-link transform。
- `gazebo_sensor_stream.py` 与 `GazeboSimBackend` 统一为 source/wire `top_to_bottom`，取消重复垂直翻转。
  frame-set 评估从“整个 source-attribution 对象必须完全相等”收紧为核心身份逐项严格验证、允许附加 provenance；
  行序元数据一旦出现就必须三字段成组且为 top-to-bottom/no-flip，错误值在推理前 fail-closed。
- hash-pinned disposable PX4/Gazebo v21 采集 3/3 个实际 1280×960 BGR24 camera frame；目标完整、正立，
  runtime JSON 46,503 bytes / SHA-256
  `3FBF2BDE258718EC9287703CDC5905F5B6766A1A60301145A5EB99BCD182643D`，首帧 PNG SHA-256
  `98C980BE9A3C0BBAC889A3D95D4545D1DA1CE15F5232631E290DD3C1DF787559`，容器已删除且无残留。
- 独立标注在推理前由源纹理 bbox 与 billboard 平面投影冻结并绑定首帧 SHA-256。quarantined YOLO candidate
  三控 v22 通过：present 唯一 `car`，confidence `0.8012787104`、IoU `0.8882733111`；blank/occluded
  均为 0 检测。证据 5,908 bytes / SHA-256
  `D48865EC9296C2778D7CF7D6C486D632410689EF1B23BAFC99BEE04A55365BC9`，CLI exit `0`。
- 本轮聚焦回归 `326 passed`，Ruff/compileall 通过。该结果仍是单帧 software/SIM smoke；模型继续
  `quarantined`、`production_approved=false`、`accuracy_validated=false`。未完成完整精度、测距/落图闭环、
  人工 QGC Fly View、真实硬件、室外 GNSS、HIL/REAL 或实飞；QGC 源码未改，不生成新 EXE，最终
  `v0.3.0` 继续阻断。
- 相机 model-root 改变使 v54 三机型 source 按设计失效；未复用旧 hash。新的 fixed-wing / multirotor /
  VTOL deterministic sources 重新绑定，binding 2,371 bytes / SHA-256
  `7DAB627CC1211EFDD60A98C71575EE697E5062B9CB1020AB905B7A0CFFF896B2`。更新后的 release parity
  5,098 bytes / SHA-256 `893E55ED8BDADE76DF85A5F50ADB3471D517237DE6F7B5F094C07AFE278C3397`，
  与当前共享源码一致。
- 最终全量严格回归 3,199 tests：`3198 passed, 1 skipped, 0 failed, 0 errors`，exit `0`；JUnit
  464,558 bytes / SHA-256 `2799FCA4C87F095F0218CC73FB65B03B7E6BD8C63978CE8C470B6ED5911CBBBC`。
  唯一跳过是 Windows symlink 权限项。
- E7 wrapper 默认三机型输入切到 v56，并将旧 v52 external join 保留为历史。旧 join 对当前 parity 的
  source recomputation 正确失败；失败记录 SHA-256
  `46EB3D11336985DB8F2A608011E17C862D8794C9485DDD0F5EC1722FCB771704`。从同六份不可变 software-only
  source 与当前 parity 新建的 join / revalidation SHA-256 为
  `D5120A0F72D6BE3236D74BFFAAC89644F8022881C76EAF08B32E95B67404327F` / 
  `AA925E65C0501C6A159EDE440391704B912D75F7B0690A988AB18D818BF96D46`，均通过；这不是一次新的
  external runtime 或硬件运行。
- 当前 E7 report-only 退出 `0`，但 `gate_passed=false` / `release_allowed=false`；manifest/report SHA-256
  为 `94C38CF64B1B53B862758D516CDA8DDB04952110BB5A53C4C3A6F20D6DD6492D` / 
  `AD14941F7D0726EC2AFEC3329EAC82B0D7CE72350659395A73B7E4EC700B0D87`。strict 退出 `2`，保留
  32 条实际 blocker；strict report SHA-256
  `B4BE420E2E8C0D9F9008E7AF74BED1C9849AC8789E8A3CA1E0FDEE2FB52F87DF`。quarantined candidate 未传给
  model-review，生产审核仍 absent；相关 E7 聚焦回归 `153 passed`。

### v0.3.0-pre.8 v55 PX4/Gazebo 持续 AI 控制与位置响应（2026-09-05）

- `run_simulator_docker_ai_acceptance.py` 新增 `--exercise-flight-seconds 1..30` 与
  `--minimum-flight-displacement-m`，并强制与 `--execute --exercise-arm --exercise-target` 组合；普通 dry-run
  和基础 Docker telemetry acceptance 的被动行为不变。
- truth-free synthetic target 通过公开 perception seam、生产 `SimulatorAiExecutor`、共享 authority/watchdog
  与既有 loopback `SimulatorMavlinkControlAdapter` 持续发送 `MANUAL_CONTROL`。disposable container 仅在该
  opt-in exercise 中设置 `COM_RC_IN_MODE=1`，并在同一 socket 上有界请求 `POSCTL`、等待 armed heartbeat
  确认；没有 `set_pose`、teleport、Gazebo truth 或第二条飞控路径。
- flight gate 只在第一个 accepted TRACK 时锁定最新 `GLOBAL_POSITION_INT` 基线；要求持续 target refresh、
  当前 armed POSCTL、零 mode loss、零 rejection/failsafe/watchdog、完整 heartbeat continuity 以及达到配置的
  WGS-84 位移。frame/poll 计数已分离，frame limit 提高到 100,000；失败 end time 在清理前冻结，避免证据
  elapsed 漂移。
- canonical v4 证据为 661,936 bytes / SHA-256
  `B56F20AB40B3E8AF9DF169FAAED9D4F326FFCDFDD052ED0FC6FF804FD7C44073`。runtime、AI、target、flight gate
  全部通过；8.172 s 内 68 次 target refresh、0 拒绝、最大 gap 0.485 s；557 次 accepted policy/control、
  0 AI rejection/failsafe/watchdog；PX4 `GLOBAL_POSITION_INT` 最大水平位移 0.818282 m（门限 0.5 m），
  171 个位置样本。三层 elapsed 一致，DISARM/neutral/stop/rm 完成且无容器残留。
- v4 container log 为 2,200,000 bytes / SHA-256
  `04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。新增 frame-limit、time-freeze、
  target lease、mode-loss、heartbeat-dropout、wire/preflight 和 safety-fault 负向回归；当前相关组合
  `105 passed`，Ruff/compileall 通过。当前代码全量回归为 2,907 tests：`2906 passed, 1 skipped,
  0 failed, 0 errors`；JUnit 411,702 bytes、SHA-256
  `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`。唯一跳过仍是 Windows
  symlink 权限项。
- v1/v3 失败 artifact 原样保留；v2 保留为首次成功诊断但不是最终 current-code canonical。v55 没有修改
  QGC C++/QML，也没有重新链接 EXE。该门不是 camera/ranging/geolocation/Fly View、actuator、airborne、
  fixed-wing/VTOL、硬件、室外 GNSS、HIL/REAL 或 E7 完成证据；最终 `v0.3.0` 仍阻断。

### v0.3.0-pre.8 v54 Gazebo wire-v2 与非平地动态 runtime（2026-09-05）

- 三个 Gazebo 机型加入官方 3-D、50 Hz `OdometryPublisher`；新的 `MDGZSIM2` wire v2 用固定
  204-byte header 同步传递 image、CameraInfo、NavSat、world pose、body velocity 和整记录 SHA-256。
- `GazeboSimBackend` 统一执行 ENU/FLU → NED/FRD 转换；world local-up 只提供 local NED，不能作为
  AGL。AGL 只能由 canonical-qualified NavSat MSL 减去 scenario DEM MSL 得到；缺 DEM、错 scenario、
  GPS 降级、越界或坏 datum 均 fail-closed。
- Docker planner 将 scripts 目录只读挂载，并把 host-side DEM 路径/SHA 写入计划；sensor evidence 新增
  `terrain_agl_reference` 与 `odometry_local_up_used_as_agl=false`。SIM GPS 质量值是显式假设并参与
  sensor identity，不作为硬件证据。
- 新增隔离的 `configs/scenarios/validation/terrain_ranging_validation_001.*` 三通道 manifest 和对应
  GeoJSON。运行时不加载 truth/expectations；该 validation 子目录不会扩张默认三机型发布矩阵。
- 外部 VTOL wire-v2 v3 证据为 27,570 bytes / SHA-256
  `224F79095247521ED6AE320299CD1E428C4E49072375C8BB2647950E464A8525`；非平地 x500 runtime v2
  证据为 27,089 bytes / SHA-256
  `8FB0FE4338EEA71021C7CCD8515ACFB2577847A2517297126C921AE4D825C4B0`，均采集 3/3 同步帧并清理容器。
  后者报告 DEM AGL `0.2303031043 m`，明确 local-up 未用于 AGL。
- validation scenario 的 8 步确定性 AI/control 记录为 4,215 bytes / SHA-256
  `FB1F532E340F7E468DE0002824AC374C12C5960391F1EC3B1A6BBE6871CAD2D2`，repeat hash 一致、truth-free。
  当前跨 Gazebo/backend/Docker/terrain/integrated-SIM/document/parity 聚焦回归 `265 passed`；三维控制/E7
  wrapper 专项 `22 passed`；Ruff/compileall 通过。
- 三个 model SDF 因新增 OdometryPublisher 改变字节后，历史 v52 三机型证据被 verifier 正确拒绝；未放宽
  校验。重新生成的 fixed-wing / multirotor / VTOL 文件 SHA-256 分别为
  `91C8482BE3B828455045023A30B1AADB7B471764F57CCE701F7BAAA73FDEB484`、
  `AF03A19145308D217EE48FFFB8D62BD49019CA6CDA321AE821658D9276D868CF`、
  `665C9885461518769A5F370237F2B4BFCD5B31397509E4904EE4983CDCBC77A5`；新 binding 为 2,359 bytes / SHA-256
  `78066C298A9B9EEDB012B7596D9B7DA66E334DE7449EACDF1CCD59513181B00C`，仍明确 external/视觉/硬件未宣称。
- E7 默认 wrapper 的三份 3-D control 输入切换到 v54，保留 v52 parity-valid external 六源。当前 strict
  原生退出 `2`，32 个真实 blocker 不变；manifest/report/strict SHA-256 为
  `913CB8FE13245A25FA5BEA73F19ACF3294CE90A42C6EE0CF419E8EE9D34349D7` /
  `7212733034F21994D20D1A41F2B4CF789A213B67981DA379A63D731124CB1900` /
  `E20D2523E771D203698D8EB35462D83279CA2B2E350A9BA936361A2C150D8B84`。
- 完整回归为 `2888 passed, 1 skipped, 0 failed, 0 errors`（2,889 tests）；JUnit 409,003 bytes、SHA-256
  `C3D01A9F3A157A3B57225319A67FA7EE79883AAB70C025DA14E896F0C1EA4532`。唯一跳过仍是 Windows symlink
  权限项。
- 五组 release parity 重算后仍是 5,098 bytes / SHA-256
  `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`。QGC 源码未改，不重建 EXE。
  本轮没有飞越坡面、目标测距闭环、人工 Fly View、硬件、室外 survey、HIL/REAL 或实飞验收。

### v0.3.0-pre.8 v53 非平坦 3-D 地形资产真值（2026-09-05）

- 新增完全本地的 `terrain_ranging_validation_001.sdf` 与配套 `.dem.json`：ENU 平面北向坡度
  `+0.1 m/m`、东向 `0 m/m`，WGS-84/MSL 原点一致，并放置两个贴合坡面的高对比场景目标。
- 新增严格的独立 SDF truth parser：限制文件大小/UTF-8/XML 结构，拒绝 DTD/entity、外部 URI、
  include、嵌套 pose 和歧义 collision/visual；从 `Rz * Ry * Rx` 独立计算 ENU 法向、坡度、平面覆盖和
  解析 ray-plane intersection，不复用 DEM 插值公式。
- 新增 schema-bound acceptance：比较 25 个 DEM 节点、16 个单元中心和 7 条确定性 NED 射线，其中
  6 条实际跨越网格边界、1 条为垂直基准；
  最大高程/坡度/斜距误差为 `3.553e-15 m` / `1.388e-17 m/m` / `5.684e-14 m`。DEM、SDF 坡度和
  world-origin mutation 均 fail-closed。
- 正式 v2 证据 11,952 bytes、SHA-256
  `CCB34D5AE3088DE1A66CCF4E6D521C7E009DEF1CD25051E6DDA61DC4A526FB9B`；相关聚焦回归
  `107 passed`。证据只关闭 deterministic asset-geometry truth 子门，明确保留 external Gazebo、QGC
  visual、硬件、室外 GNSS 和 HIL/REAL 为未验收。
- 产品 parity 未变化，仍为 5,098 bytes、SHA-256
  `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`；QGC 源码未变，无需重建
  portable EXE。
- 完整回归为 `2865 passed, 1 skipped`（2,866 tests，失败/错误为 0）；唯一跳过是当前 Windows 账户
  缺少 symlink 权限。JUnit 404,145 bytes、SHA-256
  `2E54AD65923CDB93D1BD570A0C726FCBFD32316FB3A0EB5A8D3B13A116205092`。

### v0.3.0-pre.8 v52 共享 DEM/GNSS 坡地测距（2026-09-05）

- 新增严格 `LocalDemGrid`：要求 WGS-84、MSL、原点覆盖/高程一致、有限矩形网格与 100 km 局部域，提供
  双线性高程/坡度采样和逐单元精确射线交点；越界、向上射线、无交点和无界不确定度 fail-closed。
- `MultiModalRangingEngine` 新增 `dem_gps` 垂直源。GPS 水平/垂直误差、DEM 高程/坡度误差、相机杆臂与
  三轴射线端点共同进入距离区间；该路径不重复融合同源相对 AGL，避免人为缩小方差。
- `VehicleTelemetry`、Pixhawk、raw replay、GNSS sidecar 和 serializer 增加同一合格 raw GPS 样本的
  `gps_absolute_altitude_m`（MSL）。实时 CLI 增加严格 `--ranging-dem`，并要求同时启用
  `--multimodal-ranging`；时间错位或越界时不平地回退。
- parity 更新为 5,098 bytes、SHA-256
  `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`，其中
  `core_algorithm_hash=e90559d121468097f9378790e15902002aca8e6cdacd80c8d5bb8bb5426a93a1`；聚焦回归
  `458 passed`。
- 完整回归为 `2855 passed, 1 skipped`（2,856 tests，失败/错误为 0）；JUnit 402,544 bytes、SHA-256
  `E82F4F8FCF3C0639B272ECCECC06B8BFE85B8D946B78C3E8B29A1F1BFEDCE077`。
- 新的三机型确定性 binding SHA-256 为
  `2B886AEF69197268A8658E1A5C8AA24D753DE4E560934CC245A667D7AA49948B`。它明确保留
  `external_runtime_gate_passed=false`、视觉/硬件 `not_claimed`；当前平坦 DEM 不构成非平坦 3-D、室外
  GNSS/距离真值、HIL/REAL 或最终发布证据。QGC 源码未变，沿用 v51 portable。
- 既有三机型 external runtime/AI 六源按 v52 parity 重绑和独立重算通过（SHA-256
  `3B82910E3122DF3A929DA24E5D4C119916B0BE22035BBEB3C25208CE491F7A79` /
  `CE6A3E4DE25D3E60466BA20FFF70C9FC51B59A7455AC5506935E2A3C3AD460B6`）。v52 阶段 E7 wrapper 改用 v52
  固定路径；report-only 退出 `0`，strict 精确退出 `2` 并保留 32 项现场/发布阻断。

### v0.3.0-pre.8 v51 测距协议与 SIM 地理资源真实性收口（2026-09-05）

- 将三种内部射线/相机偏置不确定性失败原因规范化为认证 `RANGE_STATUS` bit 31 的
  `target_geometry_uncertainty_unbounded`；Python 与 QGC C++ 注册表同步，编码、解码和原生自测覆盖
  bit 31，避免有效的 fail-closed 结果因协议 reason 未注册而丢失。
- 三个场景新增仓库自带 GeoJSON 地图和 schema-bound `.dem.json` 高程网格；stack resolver 严格校验
  场景身份、WGS-84、world origin、数据边界、文件 containment 和 SHA-256，并把 map/DEM 纳入聚合
  asset hash 与进程环境。VTOL acceptance 不再以非空 URI 充当资源存在性证据。
- `task_contract` 新增 operator link/status，`safety_policy` 新增 DEM schema 与 simulator stack；当前
  parity 为 4,860 bytes、SHA-256
  `FA7EF5D6B1E8042F88F119D0FB9832540A1517B2320784956BD990567F77DD43`，专项 `24 passed`。
- QGC 增量构建与原生 operator protocol self-test 退出 `0`；Release/portable EXE 字节一致，均为
  46,576,128 bytes，SHA-256
  `06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE`。
- 当前 DEM 与 Gazebo world 都是平面，只构成资源/身份 gate，不构成坡地测距或室外精度证据；真实硬件、
  GNSS、HIL/REAL、人工视觉和最终 `v0.3.0` 发布门保持未关闭。

### v0.3.0-pre.8 v50 三轴安装角误差传播（2026-09-05）

- `MultiModalRangingEngine` 将共同 `boresight_sigma_deg` 分别施加到相机安装 pitch/yaw/roll，三轴正负
  端点共同进入相机斜距、直接测距地面投影和几何方位误差；不再只扰动安装俯仰并另加固定方位标量。
- 三轴方位分量和机体 roll/pitch 分量分别通过 NED 射线中心差分计算并按平方和合并。新增回归覆盖
  banked/off-axis 固定翼几何以及“pitch 端点安全、yaw 端点越过地平线”时的 fail-closed。
- 确定性探针中方位 `1σ` 从旧近似 `2.000°` 调整为三轴几何值 `2.630°`，地面距离 95% 区间宽从
  `10.804 m` 调整为 `11.659 m`；该结论仅为 software geometry probe，不宣称室外精度提升。
- `boresight_sigma_deg` 的 per-axis 语义已写入 calibration schema、标定说明、落图文档、架构与现场流程。
- camera-calibration schema 也纳入 core parity；当前 SHA-256 为
  `0DA306BCD0A635DF70584BB14514153FB05433A452F52F7D58FB32F7B1C46336`，
  `core_algorithm_hash=f5ae7dca085db1556ad2c63ad6b08c1f155b229eb72ad78fb6bc6653e7b46044`；v50 external
  join/revalidation 均通过。完整回归为 `2828 passed, 1 skipped`（2,829 tests），JUnit SHA-256
  `862C80D0D1AAF52AA8F9ABEE2FF4E8691C6A771FD45EE5F54A2B9E49ED08577E`。
- schema-bound 汇总证据 SHA-256 为 `9400FF54A6347A9F7D0FCE116CAA497E01438EFEC9D4DF39E824BD3C408536DD`。本轮
  QGC 源码未变，沿用 v49 已验证 portable；E7 strict 仍以 32 项真实阻断退出 `2`，最终版本未发布。

### v0.3.0-pre.8 v49 模式 3 输出锁定 UI 真值化（2026-09-05）

- `MultiDetectState.fixedWingAimActive` 现在同时要求 approach active 与当前
  `fixedWingAimControlEnabled`；QGC 不会在 live provider 未认证、权限撤销或输出锁定时显示“模式 3 正在
  瞄准”。新增 locked/stale/abort/cancel/waiting 五类明确文案，状态通知也复用该 fail-closed 派生属性。
- `FlyViewCustomLayer.qml` 继续使用 QGC 原生 `QGCLabel`、`QGCButton`、palette 和 ScreenTools；没有新增
  simulator-only UI。新增静态回归验证 locked 分支先于 active 分支，且 active predicate 必须含输出权限。
- 重新链接并部署 Windows portable；Release/portable EXE 均为 46,575,616 bytes、字节一致，SHA-256
  `EE2A6A5E7B4561D56D6130FE9D81AEAC8F7093EB1A352792CB85AB353133F57D`。隐藏 offscreen simple boot
  退出 `0`。三项原生协议自测全部通过，其中 Python→C++ depth-grid 使用 44 个认证分片；自测 JSON
  SHA-256 为 `FFE34FF1B6CF7D3D933C56DB7F2BCFCE0C2D789A3B487F8E5D045C254A839667`。
- parity SHA-256 更新为 `B8D2918DBC3040596A86BB6B9FC8FB7843E660929DBCD2D677C70D3592F69167`，
  `ui_contract_hash=1dc7e2ae6edbb7ed2f4b212232ebf24fcb2fd54cf84373e4e2941af0dc88c992`；v49 external
  join/revalidation SHA-256 为 `217BBC60F506D82E5159164C7A17D06345843B1A7BFD539FCAD42F4344FACC28` /
  `302969731360683BEDD629BF54CE7ED365D39BC9BC45D6B8296EEB12E9280C29`，均为 software-only。
- 完整回归为 `2826 passed, 1 skipped`（2,827 tests，失败/错误为 0），JUnit SHA-256
  `79083F9262E924559B84C117D494D84533CAFFD47857648C19DB869250C39764`。E7 strict 预期退出 `2`、
  32 项阻断未关闭；人工视觉、硬件、HIL/REAL、室外定位和最终 `v0.3.0` 均未宣称通过。

### v0.3.0-pre.8 v48 运行 provider 独立实时再认证（2026-09-05）

- `ProfileBoundFlightControlProvider` 现在接收共享 `Clock` 并从未认证/锁定状态启动。launcher probe 的
  heartbeat/firmware freshness 不会转移到新的发送 provider；只有该 provider 自己的 receive-only
  snapshot 或显式 refresh 观察到新鲜同源 heartbeat 与匹配固件身份后，才建立带时间戳的 live
  attestation。输出命令不能成为第一次 attestation 事件。
- 未 attested 时，飞控命令、模式切换、测距 stream request 和 RC input stream request 均被阻断；
  heartbeat 超时、车辆稳定身份变化、transport close 或固件不一致会撤销 attestation 并清除缓存版本。
  同 source 重连仍必须重新观察 `AUTOPILOT_VERSION`，旧 snapshot/重认证调用不能把陈旧身份续命。
- `ProfileBoundLiveBackend`、`FixedWingAimExecutor` 和 `LiveApproachHilCoordinator` 改为动态读取当前
  `flight_control_enabled` / `output_locked`。CLI 启动事件分别输出 configured、静态
  `flight_control_authorized`、实时 `live_binding_attested`、当前 enabled/locked 和两类 reason；live
  固定翼审计事件不再硬编码控制已启用。
- PX4 Mission parity 新增 `approach_live.py` 与 `fixed_wing_aim_control.py`，当前 parity 为 4,622 bytes、
  SHA-256 `49D9B243A26591784DA0651A0B8789C5AEC0E162911199EC447C38EE370152D6`，
  `px4_mission_path_hash=544079908cbb34f9b6a28fe02e344153595cb1b983271aaa027c50d1e2247ce1`。
  v48 software-only join/revalidation SHA-256 为
  `65CCC80DC09CA1094884D82D24CC499BAD12AA382986572E75EA69CC2BDAECC6` /
  `E26B5149CBECB877529B85A0DD51B32864670110412EA62D0B3EADD056604B2F`。
- 完整回归为 `2825 passed, 1 skipped`（2,826 tests，失败/错误为 0）；JUnit 398,376 bytes、SHA-256
  `59D78D2D621C4EE6395E4EE341D3101E3CC7424531B4C2950C3D0165D9749267`。最新 E7 report-only 返回
  `0`，strict evaluator 预期返回 `2`，仍为 `gate_passed=false` / `release_allowed=false`，并记录
  `px4_mission_feedback.present=false`。误用 `0.0-pre.8` 产生的首次失败 revalidation 作为失败诊断保留，
  wrapper/canonical E7 只绑定 `-v2.json` 成功文件。这轮不关闭硬件、HIL/REAL、室外 GNSS、人工视觉或
  最终 `v0.3.0` 门；未改 QML/C++，无需重建现有 portable。

### v0.3.0-pre.8 v47 REAL 输出固件身份硬锁（2026-09-05）

- `load_runtime_profile_document()` 新增严格布尔参数 `require_firmware_identity_binding`。legacy HIL/REAL
  profile 在无输出的泛用诊断读取中仍保持兼容；REAL `flight` profile 一旦请求 output opt-in，则无论
  调用方是否显式设置该参数，都必须同时存在 profile/observed firmware hash 且完全一致。
- HIL/REAL `ProfileBoundRuntimeLauncher` 始终要求 firmware binding；REAL `observer` 仍只读，legacy
  firmwareless flight profile 会以明确原因保持 invalid/locked。`ProfileBoundFlightControlProvider`
  构造器增加独立防线，直接实例化也不能绕过该要求；动态身份比较对所有 flight profile 强制包含固件
  hash。
- wrapper 默认 external evidence 更新到 v47，当前 parity SHA-256 为
  `B40053E5EE74D09FA890E506BEACF7392288778AAE22F449016FC88D1563BBCF`，
  `px4_mission_path_hash=63674e1adedc5884561ffa62f6c6b4f35d1e4a067bb5114e29ab3a6d305917c5`；
  software-only join/revalidation SHA-256 为
  `456524B74F70F8431C186557255E067C6F70DFB4A8E6C42D93F2C16622DA1890` /
  `F4000DF499E8DEE33C4D795D49B6CE177347DEA5E661959118277E450692907C`。
- 完整回归为 `2820 passed, 1 skipped`（2,821 tests，失败/错误为 0），JUnit 397,695 bytes、SHA-256
  `557FA4D8D306193388DB3D5600A8E5AD4228C2C2E494112F4857EB201EDD8934`。E7 report-only 返回 `0`，
  strict 预期返回 `2`；strict manifest/report/strict-report SHA-256 为
  `88C4E86F007F92F9ABC5BB9D8D507BEF3E4933B9A1374F059025EE2B351C92C6`、
  `2E871C79E3F58C7520B0EF70993AF7C2B1BFE29FEB8E2BCDD75CBB58B9F72155`、
  `CC62CD5D4BBFCB026022DEDE23483746AF7E9C76C32AFF31BAD16603FF5CC931`；external evidence 通过，
  `px4_mission_feedback.present=false`。这轮是 software contract 收口，不关闭真实 V6X、HIL/REAL、
  室外 GNSS、人工视觉或最终 `v0.3.0` 门；未改 QML/C++，无需重建现有 portable。

### v0.3.0-pre.8 v46 固件身份绑定 RuntimeProfile/E7（2026-09-05）

- `RuntimeProfile` / `RuntimeIdentity` 新增可选 `firmware_identity_hash`。泛用 loader 保持 legacy
  HIL/REAL profile 可读；当 profile 携带固件 hash 时，身份匹配会一并严格比较，错配继续锁死输出。
- `px4_mission_feedback_cli.py provision` 现在强制要求同一 heartbeat source 的完整
  `AUTOPILOT_VERSION`，而且该版本帧的观察时间必须位于本次 probe 的开始与完成之间。缺失、错来源、
  陈旧/预先缓存、未来时间、接收错误或任何非零发送都 fail-closed；新签发的 HIL/REAL profile 必然包含
  隐私保护 firmware identity hash，摘要仍不包含原始 UID。
- HIL/REAL Mission-feedback v2 observer 强制要求 profile 和实时 runtime identity 两侧都存在同一个
  firmware hash。legacy profile 可用于历史诊断，但会以
  `runtime_profile_firmware_identity_missing` 阻断 E7；实时版本缺失或不匹配也有独立原因码。
- 六叶 `px4_mission_feedback` release binding 新增 `firmware_identity_hash` 与
  `all_firmware_identities_bound`，独立 verifier 会重开每份 report 并比较 HIL/REAL × 三机型的叶节点值；
  schema、wrapper 默认路径和回归同步更新。
- 当前 parity SHA-256 为
  `3A85B02436CD545E1409B842D28AC3A600DC02749B5F4A5D2130A525D4A21F95`，
  `px4_mission_path_hash=3639c0a56d8d9ab3e799242ff190838a8f9959d07a938996b36e5e86ab775e5a`；v46
  software-only external join/revalidation SHA-256 为
  `31079BDC34DA919613C130B5466C13C0003D4401DD7B2CC030DE4847A15E23F0` /
  `EBAB5126102B511A31614273C83F1DDF6D149D15344A8108A20AFE3C807F00B5`。真实 V6X、HIL/REAL 六源、
  室外 GNSS、人工视觉和最终 `v0.3.0` 仍未通过。
- 完整 Python 回归为 `2816 passed, 1 skipped`（2,817 tests，失败/错误为 0），JUnit 397,164 bytes、
  SHA-256 `18EDEF1953C0F4E4EA452AF210185B5074C4D61C3E02F331B368E802806401F1`。E7 report-only
  返回 `0`，strict 预期返回 `2`；strict run 的 manifest/report/strict-report SHA-256 为
  `CC1BB8BF9F68AD904CA90049293D7C5526005217F199EE9C3ECB369AF5778C33`、
  `CA5601CB7D3BB18CE0387E9AD8809ED3087EC55A0751544EBD6303738069AC00`、
  `F413957547B5AE31A3F5E48AA1B68BC5D1A8EA155EE4E1C51FE7B9CA54831020`；
  `px4_mission_feedback.present=false` 保持真实门关闭。

### v0.3.0-pre.8 v45 PX4 固件身份被动观测（2026-09-04）

- `PixhawkReadOnlyTelemetryProvider` 新增 `AUTOPILOT_VERSION` 的严格解析和缓存：只接受当前 autopilot
  heartbeat 同一 system/component 的完整 uint64/uint32/uint16/固定长度 byte-array 字段；来源错、越界、
  截断、观察时钟倒退和当前帧畸形均 fail-closed，heartbeat 的稳定身份字段变化会立即清除旧版本身份。
- 64-bit MAVLink 整数现在优先保留 Python 整数，不再通过 IEEE-754 `float` 丢失低位。版本报告解码
  major/minor/patch/release type，同时保留 packed 值、capabilities、custom versions、board/vendor/product。
- `uid2` 优先、`uid` 回退，但二者只进入 domain-separated SHA-256；新增对象和 JSON 明确
  `raw_device_uid_serialized=false`，不保存或打印原始设备 UID。
- `multi-detect pixhawk-check --require-autopilot-version` 增加零发送的被动固件门，并输出版本 age、拒绝
  计数和规范 identity hash。该门不会主动发送 `MAV_CMD_REQUEST_MESSAGE`；v45 截止时真实 V6X 采集与
  RuntimeProfile 绑定仍 pending。v46 已完成软件侧强制绑定，真实设备/HIL/REAL 仍 pending。
- 全量回归 `2808 passed, 1 skipped`，JUnit SHA-256
  `E1EB8D218E039A59D6A9084D837B17C965DC9D67A44330A39DA2A6B0333646B3`；新
  `px4_mission_path_hash=9dfcd64df28141ad9f08f439bf839b7a49e2b0ebdefb9cd8367d9a720e2a91f8`。
  v45 external join/revalidation 均通过，当前 E7 仍失败且 strict 按预期返回 `2`。

### v0.3.0-pre.8 v44 PX4 RuntimeProfile 现场自动签发（2026-09-04）

- `px4_mission_feedback_cli.py` 新增 `provision` 子命令和
  `multidetect-px4-runtime-profile-provision` console script。它从当前 Mission、实际 resolved endpoint 与
  新鲜 PX4 heartbeat 自动派生 airframe/endpoint/vehicle identity，生成后续 observation v2 所需的签名
  HIL/REAL RuntimeProfile，移除现场手工拼 hash 的易错步骤。
- provision probe 只使用 `PixhawkReadOnlyTelemetryProvider`，关闭 ranging stream request，并要求
  `read_only=true`、`messages_transmitted=0`、fresh heartbeat、完整 system/component/autopilot/MAV_TYPE/
  MAVLink version、PX4 autopilot 和 `STANDBY/ACTIVE`。Mission adapter 与 heartbeat MAV_TYPE 必须属于同一
  fixed-wing/multirotor/VTOL 类；错机型、错来源、陈旧 heartbeat、未解析 endpoint 或任何发送均
  fail-closed。
- HIL profile 禁止 REAL signing identity；REAL profile 强制要求 signing identity hash。HMAC 和 signing
  key 只从环境变量读取。输出先在临时文件中用同一 HMAC/observed identity 严格重载，确认有效且输出
  锁定后才原子写入；默认不覆盖既有 profile，显式 `--overwrite` 才允许替换。
- 新增 profile provisioning 回归，覆盖三机型、HIL/REAL、错误 vehicle/component/autopilot/status、陈旧
  heartbeat、receive error、非零发送、签名约束、错误 key、原子写入、CLI 环境变量与覆盖策略；相关
  Mission feedback/release wrapper 聚焦回归共 `47 passed`；`provision --help` smoke 退出 `0`。
- v44 parity 为 4,529 bytes、SHA-256
  `BB5D6285E81B813494BF56432F76FB045F7F4DAC454553F43B4041728F8C2F62`，
  `px4_mission_path_hash=a8d5a95d18e7efb5c829f53a8402c0d8bc3019d6012186351196a20c553b1f87`；
  software-only external join/revalidation SHA-256 为
  `3F01945CAC2E79295279418154D6DFF3F490DEF09FFE0F3AB71A2580FA2F28E8` /
  `D6240C13461B2633DE80D431CD5877AF6694703F71EDFD4E28FC7AA6BAA8B4EE`。
- v44 E7 manifest/report/strict-report 为 21,036 / 54,830 / 54,830 bytes，SHA-256 为
  `432A9835B2D061B15E8CB9D54759FBF36D9D1E511F0FD09E26E14953DB0DACDF`、
  `423D15645792457F7107BF7E1C9A59F0D1ED125F87733A72B62B80DF26CB4527`、
  `2D5510308BC3CB49C517F69C86EEE09F2941274A41193489970520F6972D72BC`；strict 预期退出 `2`，
  `px4_mission_feedback` 仍 absent。生成 profile 不等于 HIL/REAL/硬件验收，最终 `v0.3.0` 保持未发布。
- 完整 Python 回归为 `2793 passed, 1 skipped`、退出码 `0`；3,226-byte 最终汇总日志位于
  `artifacts/evaluation/terminal-continuation-v44/full-regression-v44-runtime-profile-provision-final-20260904.log`，
  SHA-256 为 `C437BD893FF13E5E161161F6A762A821B661CC1C44A7534FF5D27573A4938C5F`。本轮未改
  QML/C++，现有 QGC portable 无需重建。

### v0.3.0-pre.8 v43 HIL/REAL Mission feedback 运行身份绑定（2026-09-04）

- 新增 `src/multidetect/px4_mission_feedback_runtime.py` 与
  `configs/schemas/px4-mission-feedback-observation-v2.schema.json`。v2 报告把 receive-only PX4 Mission
  feedback 同时绑定到签名 RuntimeProfile 文档/规范 profile/hash/key ID、完整 heartbeat 派生车辆身份、
  configured/resolved endpoint + baud、Mission airframe profile 与输出锁决定；profile HMAC 和 REAL
  signing key 只从环境变量读取，证据不保存原始密钥。
- HIL 叶节点只接受精确 `hil` profile、禁止 signing identity；REAL 只接受精确 `flight` profile，但
  观测窗口统一以 `allow_flight_control_output=false` 加载，因此合格证据必须保持
  `flight_control_enabled=false` / `output_locked=true`。端点、heartbeat、source、airframe、profile 文件、
  派生 hash 或锁状态不一致均 fail-closed。
- `px4_mission_feedback_cli.py` 在 HIL/REAL 模式强制要求 `--runtime-profile`、
  `--runtime-profile-hmac-key-env` 和 `--runtime-profile-key-id`；REAL 另需 signing identity hash 或 signing
  key env。schema v1 继续作为历史诊断读取，但 E7 明确拒绝 v1。
- `px4_mission_feedback_binding.py` schema 升至 v2。E7 每项输入从
  `MODE VEHICLE MISSION REPORT` 升为
  `MODE VEHICLE MISSION SIGNED_RUNTIME_PROFILE REPORT`；六份 profile 与六份 report 必须各自不同，独立
  verifier 会重开三类来源并重算 identity/hash/bytes。release manifest/schema、PowerShell wrapper、
  parity 文件组和回归同步更新。
- 当前 parity 为 4,529 bytes、SHA-256
  `B4E080286DAEA35AF55734DE1C3B0AF05427373A10F90A5E186712AF5E6E7D5B`，其中
  `px4_mission_path_hash=c5cb36fed3a1c8576178a1021908529ea7d8e12dd3035a39ab4ee5bd5d1f3ccb`；
  v43b software-only external join/revalidation SHA-256 为
  `DC56F383B41D9042F0ED0B385762C78DC9987F1BF2C2E3E6A1FAD5625370159C` /
  `C58FB2F3ACB6DF8668776C01633499385896ABE25940CD8603C0C300E9230467`。
- 当前 E7 manifest/report/strict-report 为 21,038 / 54,833 / 54,833 bytes，SHA-256 为
  `C649E62BF1E4C88DEB5CE6BE173B2987DF55826DF9799812934477981CDFF996`、
  `89E9A82B4675C0DBB4F1FB36301C9F98BDC6BEBF9CE31F49808F3B470FAFD06B`、
  `C609F0BA2E1B7F8E7886797FFD8A1AEE3B7812B85F72AEEDD92CF1DCB3211814`；严格门按预期返回 `2`，
  `px4_mission_feedback` 仍 absent，总门保持关闭。完整回归为 `2772 passed, 1 skipped`、退出码 `0`；
  3,163-byte 汇总日志 SHA-256 为
  `399453AB6E33089442CDB13795F242683777C0D2A15D948AB4BF5963BDD7EA95`。
  本轮无 QML/C++ 变化；真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞、人工 Fly View 和最终
  `v0.3.0` 继续 pending。

### v0.3.0-pre.8 v42 HIL/REAL 三机型 PX4 Mission 反馈发布绑定（2026-09-04）

- 新增 `src/multidetect/px4_mission_feedback_binding.py`：将 v41 的最小 receive-only 观测单元直接
  汇入 E7 `px4_mission_feedback`，要求且只接受 HIL/REAL × fixed-wing/multirotor/VTOL 六个组合。
  每对来源会重新打开 Mission 与观测报告，绑定文件 SHA-256/bytes、Mission ID/hash/item count、
  当前 airframe adapter、runtime mode、MAVLink source、feedback/evidence hash 和 read-only/
  zero-transmit 状态；六份报告必须不同，缺失、重复、篡改或错配均 fail-closed。
- `release_gate_manifest.py` 增加可重复 CLI 参数
  `--px4-mission-feedback-evidence MODE VEHICLE MISSION REPORT`；
  `run_current_release_gate_pre8.ps1` 增加六项
  `-Px4MissionFeedbackBinding 'MODE|VEHICLE|MISSION|REPORT'` 输入。prerelease 可以暂缺该记录，
  但已提供的失败记录在所有通道都阻断；最终 release 强制要求完整通过。
- `release_gate.py` 会独立重验六对来源，并把记录内的 `px4_mission_path_hash` 与 REPLAY/SIM/HIL/REAL
  四份模式证据的统一 hash 比较；`release-gate-manifest.schema.json` 同步使用封闭对象和精确六项约束。
  parity 的 PX4 Mission path 组已纳入 binding module/schema。
- 新增八个专项回归，覆盖完整矩阵、CLI/schema、源文件后改、缺组合、错误 adapter、四模式 path hash
  脱离、最终发布缺门和 `bool` 冒充零发送。完整 Python 回归为 `2764 passed, 1 skipped`、退出码 `0`；
  带精确汇总的日志 SHA-256 为
  `310EA89D0414C068B6D06196152863E0B6003E19A3CF2D8BB2E8253FEA19ABAC`。
- 当前 parity 文件 SHA-256 为
  `C2BAC5A77719B24B9F899B278053DE5CC3526FEDB2C46371AF6D94090DDAB8E9`，
  `core_algorithm_hash=19d5e6c4e22e491affce746acb566d179ffff5975e3128a3bf997209ee4b93f8`，
  `px4_mission_path_hash=9ff62c8ac9daea498732a9f5690ef195b8091dd94e621947e5d2ea3fb11500e8`。
- v42 report-only E7 manifest/report/strict-report SHA-256 分别为
  `9F8330CEC3D4175E314F78C00C99A3D6CCFBCE711D08F0D6C5E4A0417B859A2A`、
  `8CBBD0DD0468796EDAF596C6A6359EF4A7A9D27CA32168DD1F953D5F55C8FB7E`、
  `27E5A41035A6D31251F85F0913CB0B25256B15B3FD7DC762E93F07F95167BE5C`。当前没有六份真实
  HIL/REAL 报告，故该子门显示 absent、总门保持关闭；没有将 fake/Docker/历史数据写成现场通过。
- v42 external join/revalidation 是保存的 software-only 六源对当前 parity 的重新计算，SHA-256 为
  `4843163B46915F42553144C2388F95329C8B28A89C3EBDA34938ECF3297955B3` /
  `BC0CBFED82D24DC9C970EBAB11726D0C708ECD4480FB21C9815D697E4966DD40`，不是新 Docker 或硬件运行。
  本轮无 QML/C++ 变更，无需重建 QGC；真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞、人工
  Fly View 与最终 `v0.3.0` 继续 pending。

### v0.3.0-pre.8 v41 PX4 Mission 实机反馈契约（2026-09-04）

- 新增 `src/multidetect/px4_mission_feedback.py` 与
  `configs/schemas/px4-mission-feedback.schema.json`：只读消费 `MISSION_CURRENT` /
  `MISSION_ITEM_REACHED`，绑定 executable Mission ID/hash/item count、MAVLink source identity、
  单调时间和 `<=1.5 s` freshness；换源、序列回退、越界/reached 超前、倒退时间或 stale 输入均
  fail-closed，不产生控制输出。
- `PixhawkReadOnlyTelemetryProvider` 增加可注入的 feedback tracker，并为 current/reached 游标分别
  记录时间；旧游标按 provider stale timeout 清除，HEARTBEAT 新鲜不再延长 Mission 数据寿命。
  diagnostics 同时携带 current/reached age 与 hash-bound feedback 文档。
- `MissionProgressConfig.require_px4_feedback` 提供可选严格门；同一决策时刻、同一 Mission 的合格
  feedback 可作为进度佐证，不合格反馈阻断已启动任务，反馈仍不能自行推进或产生飞控命令。
  mission-progress 与 3-D scene-stream schema 支持可选嵌套反馈并保持旧快照 hash 兼容。
- raw replay、普通 replay、CLI 输出与 Pixhawk bench 增加 current/reached 时间字段。完全缺失的新字段
  在重新序列化旧 schema-v1 记录时继续省略，历史 `recording_hash` 保持不变；真实观测后才写入。
- 新增现场入口 `multidetect-px4-mission-feedback` / `python -m
  multidetect.px4_mission_feedback_cli`，以及同模块的 `verify` 子入口和独立 verifier console script。
  它可读取裸 `ExecutableMission`、
  `CompiledPx4Mission` 或完整 `PlannedMissionCompilation`，在有界时间窗内只读监听 PX4，原子写出
  `px4-mission-feedback-observation.schema.json` 报告。验证时会重算 Mission/feedback/evidence hash、
  消息计数、只读状态和 gate reasons；`messages_transmitted` 必须为 `0`，hardware/HIL/REAL/release/
  人工视觉 claims 永远为 `false`。`--report-only` 只允许保留不合格诊断而返回 `0`，不会伪造通过。
- 本机 loopback/no-data provider 冒烟已生成
  `artifacts/evaluation/terminal-continuation-v41/px4-mission-feedback-report-only-smoke-20260904.json`
  （2,253 bytes，SHA-256 `D34FB2237628BF7367F178E42EE8640B43EA4FEDD537FC75253A755655EED6E1`）；
  schema/独立 verifier 通过，严格 verifier 按预期返回 `1`，报告保持
  `feedback_gate_passed=false`、`mission_feedback_unavailable`、read-only/zero-transmit。它不是 PX4/HIL
  成功证据。
- 新增来源绑定、freshness 边界、篡改、序列错误、provider 接线和 replay 兼容性回归。完整 Python
  回归为 `2756 passed, 1 skipped`、退出码 `0`；日志 SHA-256 为
  `079ECE9B161174A42BFEAA4B53E270E7B13C0F577CD9622759F750C25E9B9961`。当前 parity 文件 SHA-256
  为 `B5FE05726534F5781995E56C85D3CA7043553B766C548B17083711FCAFF53776`，
  `core_algorithm_hash=19d5e6c4e22e491affce746acb566d179ffff5975e3128a3bf997209ee4b93f8`，
  `px4_mission_path_hash=1fa6bfe715ace25dc0e1c0818571e25af113d4ab1e104d40da002e5a5c765b7b`。
- 本轮没有 QML/C++ 变更，无需重建 QGC；这些仍是 software-only 证据。真实 Pixhawk/Jetson、室外
  GNSS、HIL/REAL、实飞、人工 Fly View 和最终 `v0.3.0` 发布门继续保持 pending。

### v0.3.0-pre.8 v40 3-D 场景流携带 Mission 进度（2026-09-04）

- `SimulatorSceneSnapshot` 增加可选的 `mission_progress`（省略表示无 Mission，出现时必须是完整对象，
  不接受显式 `null`）：有编译 Mission 时逐帧保存同一份严格
  `MissionProgressSnapshot`，把当前序列、GPS/link freshness、距离、驻留和完成比例一起带入
  3-D replay stream；没有 Mission 的历史快照继续省略该字段，旧 canonical hash 保持不变。
- `configs/schemas/simulator-scene-stream.schema.json` 增加内联 `mission_progress` 定义并保持
  向后兼容；读取器会重用 `MissionProgressSnapshot.from_mapping()`，未知字段、非法比例、伪造
  hash-bound Mission 或 truth 字段继续 fail-closed；同一条流还要求各帧 Mission ID/hash 一致，
  不允许把不同任务或缺失帧拼接到一起。
- `python -m multidetect.simulator.scene_state` 增加 `--mission` / `--start-mission`，因此
  直接导出的 3-D 场景流也能复用 runner 的显式 Mission start 语义；省略启动开关仍只生成
  `ready` 观测，不隐式控制飞控。
- 新增场景流 Mission 进度 round-trip、schema、CLI 和篡改回归。该层仍是 truth-free 的
  software/SIM 证据；外部 3-D、PX4 `MISSION_CURRENT`、真实 GNSS/Pixhawk、HIL/REAL、人工
  Fly View 和最终 `v0.3.0` 发布门继续保持 pending。
- v40 场景流 smoke 已用现有编译 VTOL Mission 生成并独立重验：
  `artifacts/evaluation/terminal-continuation-v40/scene-stream-mission-smoke-20260904.json`
  （2 帧、schema/verifier 通过，文件 SHA-256 `C7A413D9755B9379E6EB32ED4330115928563615201DA1E904F9DA95D34ED9B2`）。
  同一工作树最新全量 Python 回归为 `2727 passed, 1 skipped`、退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v40/full-regression-final-20260904.log`
  （SHA-256 `B28F57D34E30F7116A3735FB3ADA401F0F684E83406187BC53B4327BEA32CD24`）。

### v0.3.0-pre.8 v39 场景运行器接入编译 Mission 进度（2026-09-04）

- `SimulatorScenarioRunner` 新增可选 `mission` / `mission_progress_config`，并提供
  `bind_mission()`、`clear_mission()`、`start_mission()` 转发；编译出的 `ExecutableMission` 与
  SIM 场景共享同一个 authority → adapter → MAVLink sink → flight-model 生命周期。
- `python -m multidetect.simulator scenario` 增加 `--mission` 与 `--start-mission`，可直接读取
  `ExecutableMission` 或完整 `PlannedMissionCompilation` JSON；省略启动开关时只记录 `ready`，
  不产生隐式任务控制。
- 场景步骤现在带有 `mission_progress` 快照；未显式调用 `start_mission()` 时保持 `ready`，不会
  因 authoring artifact 存在而隐式上传 Mission、发出控制或绕过安全门。
- 增加 runner 绑定/启动/步骤证据与无 Mission 配置闭锁回归；补强 Mission progress 配置类型和
  home 坐标成对校验。能力仍为 truth-free、observation-only software/SIM 观测，真实 GNSS、
  Pixhawk、SITL `MISSION_CURRENT`、HIL/REAL、人工 Fly View 和最终 `v0.3.0` 发布门保持 pending。

### v0.3.0-pre.8 v38 编译 Mission 的同核进度观测链（2026-09-04）

- 新增 `src/multidetect/mission_progress.py`：将 hash-bound `ExecutableMission` 与
  truth-free telemetry 绑定为确定性的 `MissionProgressSnapshot`，统一推进 VTOL、fixed-wing
  和 multirotor 的任务序列；坐标项使用共享 WGS-84 inverse，支持起飞、航点、盘旋驻留、VTOL
  转换、返航和降落。
- 进度层沿用全局 GPS/RANGE 安全语义：GPS 必须满足 `fix_type>=3`、卫星数 `>=6`、水平/垂直
  精度上限和 `1.5 s` 新鲜度；链路不健康或车辆未解锁时保持 `WAITING_FOR_*`/`BLOCKED`，不
  自动推进，不产生 MAVLink 或 actuator 输出。
- 新增 `mission-progress.schema.json`，并将 `MissionProgressSnapshot` 暴露到根包、SIM 包；
  `SimulatorClosedLoop(mission=...)`、`bind_mission()`、`start_mission()` 和每 tick 证据现在
  复用同一个进度快照与 canonical hash。SIM 仍通过原有 authority/adapter/MAVLink sink，
  没有新增 simulator-only UI 或控制旁路。
- 新增 `tests/test_mission_progress.py`，覆盖三段式任务推进、GPS/链路闭锁、VTOL 转换、驻留、
  极点坐标拒绝、快照重载/篡改和 closed-loop 绑定；专项回归 `7 passed`，与既有 closed-loop/
  planner 回归合计 `30 passed`。
- v38 parity 已把 `mission_progress.py` 与其 schema 纳入 `core_algorithm`：当前
  `configs/release-parity.json` SHA-256 为
  `A95880A967954CFD4F8F11A6C8C35B12C5F1AFB1E0A1FE5D656DBA31012A8CD0`，
  `core_algorithm_hash=0f29c28f30c581f292fe9496ca5f14b7ee3b12bfd6ae72bc19300b82cf67031e`。
- 版本继续保持 `v0.3.0-pre.8`；真实 GNSS、Pixhawk、HIL/REAL、人工 Fly View 和最终
  `v0.3.0` 发布门仍按总门定义保持 pending。

### v0.3.0-pre.8 v37 文件化 Planner → PX4 Mission 编译入口（2026-09-04）

- 新增 `MissionCompileRuntime v1` 严格输入契约：将 runtime home、VTOL/fixed-wing/multirotor
  geometry 与对应 safety policy 收在一个 JSON 文档中，顶层未知字段、truth/expectations 和
  vehicle-class 不匹配都会 fail-closed；Schema 位于
  `configs/schemas/autonomy-mission-compile-runtime.schema.json`。
- `VtolMissionGeometry`、`FixedWingMissionGeometry`、`Px4MissionSafetyPolicy` 和
  `Px4FixedWingSafetyPolicy` 增加严格 `to_mapping()` / `from_mapping()`，保留所有默认包线参数，
  因而同一份 runtime 文档可以在 REPLAY/SIM/HIL/REAL authoring 端重载而不靠手工拼 dataclass。
- 新增 `multidetect.mission_planning.parse_mission_compile_runtime(...)` 与
  `compile_planned_task_mission_from_runtime(...)`，在既有 typed seam 上统一解析并复用三机型
  canonical PX4 compiler；仍只生成 hash-bound artifact，不打开端口、不上传、不发送飞控输出。
- 新增 `multidetect-mission-compile` / `python -m multidetect.mission_compile_cli`，从严格
  `ResolvedTaskSpec`、`ContextSnapshot`、`MissionCompileRuntime` 和 airframe SHA-256 生成可由
  `PlannedMissionCompilation.from_mapping()` 重载的 JSON；CLI 只负责 authoring/审计。
- `tests/test_mission_planning.py` 新增三机型 runtime/policy round-trip、CLI artifact 重载、
  机型错配和未知字段拒绝；新 CLI 与 runtime schema 纳入 `px4_mission_path` parity。
- v37 parity 已按生成器重算：`configs/release-parity.json` SHA-256 为
  `AD1ED71E79A8E048D5F50365F6BE9B80F69AB505917C7CA476B66B1B57A91310`，其中
  `px4_mission_path_hash=e533372c6b49bdfa05760f02328305e5ec012777487a8e708a829773a4f22ada`；VTOL CLI smoke artifact 为
  `artifacts/evaluation/terminal-continuation-v37/mission-compile-smoke-20260904.json`
  （SHA-256 `BEA3FFBE5D42464534E366FF3BEEAB661EA474A6AEC9CB281D959E93E7ED0B1C`），
  `from_mapping()` 重载、8 步 plan 和 7 个 wire item 均通过；同一源码的完整 Python 回归为
  `2713 passed, 1 skipped`、退出码 `0`，日志 SHA-256 为
  `7A02B4E7C4D10D5E7656FA3B91234A5766B5D0A8D0FBECA681902EF55A534BB8`（日志为
  `artifacts/evaluation/terminal-continuation-v37/full-regression-v37-mission-compile-20260904-rerun4-summary.log`）。

### v0.3.0-pre.8 v36 确定性 Platform-Neutral Planner（2026-09-04）

- 新增 `src/multidetect/autonomy/planner.py`：四类 `ResolvedTaskSpec`（区域搜索、走廊视觉搜索、
  目标观察、测绘）统一编译为 `PlatformNeutralPlan`，VTOL、fixed-wing、multirotor 共享同一
  连续步骤和 task/context hash；机型差异继续留给各自 PX4 Mission compiler。
- 新增 `multidetect-plan` / `python -m multidetect.autonomy.planner_cli`，从严格 task/context
  JSON 生成 schema-compatible plan，并可单独落盘审计用距离/能源/时长估算。
- 新增 `multidetect.mission_planning.compile_planned_task_mission(...)` typed seam：同一份
  task/context 先生成 platform-neutral plan，再按 VTOL、fixed-wing 或 multirotor 复用现有
  PX4 Mission compiler；返回 plan、estimate、executable mission 与 wire hash 的绑定结果，
  不打开端口、不上传、不产生飞控输出。`tests/test_mission_planning.py` 覆盖三机型和跨上下文
  绑定拒绝。
- 将 `autonomy/planner.py` 纳入 `core_algorithm` parity、将 `mission_planning.py` 纳入
  `px4_mission_path` parity；v36 阶段 `configs/release-parity.json` SHA-256 为
  `66DB8D58198B183E8322D4CE2575DA98FD940F56120F05CF3326F5DCB41F22E2`，对应
  `core_algorithm_hash=FE16E5D9D59EEAE67C6AC613C029912704CD68A5A53EBD28BF70603AF8A5B7BA`、
  `px4_mission_path_hash=F2B692965D3FD761AA76F8D5CCBB5EA9112C76B51497BA62681A39BF8DDD9E3C`。
- Planner 先执行 fail-closed 任务就绪、几何置信度、GPS fix/水平精度、地理围栏、相机/感知/
  地图/DEM、返航电量与时长检查；航段使用 WGS-84 inverse，声明长度不会被稀疏 centerline
  低估，逆风和 VTOL 前后转换纳入保守预算。
- `on_detection` 只允许目标观察、证据采集、定位上报和 revisit，禁止把 return/land/divert
  等生命周期动作混入检测回调；新增 `tests/test_autonomy_planner.py` 覆盖全部任务类型、
  三机型 byte-identical 结果、别名、资源/能源/时长和非法动作拒绝。
- 已用真实 task/context JSON 通过 `planner_cli` 端到端 smoke：计划可 round-trip 解析，8 个步骤从
  `takeoff` 连续到 `land`；证据清单为
  `artifacts/evaluation/terminal-continuation-v36/planner-cli-smoke-20260904.json`（SHA-256
  `CD1FC9418EF6A9199210F0663A9E53836A288261B472848E3C3806061232DAEB`）。全量 Python 回归为
  `2709 passed, 1 skipped`，最新重跑日志
  `artifacts/evaluation/terminal-continuation-v36/full-regression-v36-planner-20260904-rerun7.log`
  （SHA-256 `D531C53B29FB887FB612E9064E9EC6F6DC53632809641F9B40EEA8E55651ABFB`）。
- 该模块只生成不可变计划/估算，不产生 MAVLink 或飞控输出；固定翼 Dubins/覆盖/进近、VTOL
  转换轨迹、多旋翼姿态控制、真实 PX4/Gazebo、HIL/REAL 和最终 `v0.3.0` 发布门仍保持 pending，
  版本继续为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v35 加速度预算完整性门与 parity 重绑（2026-09-04）

- 当目标帧与 GPS 时间对齐时，若 telemetry 提供了
  `acceleration_uncertainty_mps2` 却缺少完整 N/E 速度，目标投影现在 fail-closed，避免静默丢掉
  `0.5·dt²·σa` 误差预算；完整速度仍按 `hypot(|dt|·σv, 0.5·dt²·σa)` 合并，旧 JSONL 缺省保持兼容。
- 重新生成 `configs/release-parity.json`：SHA-256 为
  `D9CF6D9E625E6B0ADB56084628CCB39ED358E2156AF89201633C2F6E1F2B9AEF`，
  `core_algorithm_hash=7D35526D63E3BFE247776F1ED2CC14F2950E0E61AB72E10A170C5C41BB930CF3`。
- v34 历史文件保持不变；当前 parity-valid external SIM/runtime + AI join/revalidation 写入
  `artifacts/evaluation/terminal-continuation-v35/`，SHA-256 为
  `B459F6CDA7E6BD60EE5BED486E56F25870D388E31650F54333AC2FA3741CD12B` /
  `D825A2A8A1D8E3F152C35C6C8C70C3D847D9C26E3329A4F8A7EE2D27974B338C`，独立重算均通过，仍为
  software-only。
- wrapper 默认切到 v35 external 输入；E7 report-only manifest/report 位于
  `artifacts/evaluation/terminal-continuation-v35/release-gate-current/`，SHA-256 为
  `B4801000CCF23B3243ADCBAA4788C00539973A0BA1A3D6D48233758782A728E4` /
  `2E4CCE1FA23899958BBF05E5F399171CA462B83745D6435509BF5AEEC2FE71E7`。parity/external 子门通过，
  但合成 replay-set、现场硬件、人工视觉、HIL/REAL 和 prerelease channel 仍关闭总门。
- 目标定位/回放专项、全量 Python 回归、ruff 和 compileall 均通过；版本保持
  `v0.3.0-pre.8`，没有发布最终 `v0.3.0`。

### v0.3.0-pre.8 v34 运动补偿不确定度与 replay-set 校验重绑（2026-09-04）

- `VehicleTelemetry` 新增可选 `acceleration_uncertainty_mps2`。目标 WGS-84 投影均值仍为恒速
  模型，但把 `0.5·|dt|²·σa` 与速度不确定度按平方和合并，覆盖固定翼转弯、垂起转换和急加减速
  的未建模运动误差；字段必须有限且非负，非法 live/replay telemetry fail-closed，旧 JSONL
  省略字段保持兼容。
- 更新 `target_geolocation.py`、live/operator-status 转发、raw replay mapping/schema 与目标落图
  文档；新增 acceleration 数值/非法输入/operator-status 回归。
- 从同一已保存 Gazebo capture 重新生成 v34 replay-set 副本，按当前 telemetry identity 重算
  recording hash。历史 `artifacts/replay-sets/sim-gazebo-2026/` 保留不改；当前副本的 manifest
  SHA-256 为 `F577BF8085F33311D20AE27BB882C5C5F0F7225FC6F86B379A5384982E1FEDFF`，canonical
  hash 为 `F2834DC4CD03C95DC6502C137EAA66430A3F68CC2F145498889156621FA32E4C2`，synthetic 空
  基线仍保持 `accuracy_validated=false` / `gate_passed=false`。
- v34 external SIM/runtime + AI join/revalidation 的 SHA-256 为
  `80599422FD5D39E3445F8D3ADA5473CAC0F62D184023A2B2F2BD4CFF6E46C1FE` /
  `06D4C67C5A095661F5D54A691C334C8F6D2576E5821CB365166C8CE2E28E1BAE`，均通过独立重算且
  仍是 software-only。
- v34 阶段 `run_current_release_gate_pre8.ps1` 无参数默认切换到 v34 replay-set/accuracy/calibration 和
  external 输入。report-only E7 manifest/report（目录
  `artifacts/evaluation/terminal-continuation-v34/release-gate-current/`）SHA-256 为
  `7C08D3254C49DB9531D1B1032939FF1D4C01426164299C0EEF212A0B6C661FBF` /
  `12C8C52A9F58EEA97EC6AF863295BAAA73605CC65D1AC0B83408BD48D75156C5`；总门仍关闭，版本仍
  `v0.3.0-pre.8`。

### v0.3.0-pre.8 v32 导航速度残差不确定度膨胀（2026-09-04）

- `NavigationVelocityFusionEngine` 在 GPS/VIO/空速候选通过 `3.5σ` 成对一致性门后，继续
  计算 North/East 两轴残差 `χ² / (2*(N-1))`。当实际分歧超过声明协方差时，按 Birge
  ratio 放大融合速度 `σ`，追加 `navigation_velocity_uncertainty_inflated`，并把结果保持
  为 `DEGRADED`、`advisory_only=true`；该路径不产生飞控或物理释放输出。
- GPS 速度源复用全局质量底线：`fix_type>=3`、卫星数 `>=6`，`255` 未知哨兵拒绝；配置
  不再允许通过放宽默认值把未知/二维 fix 提升为室外可控导航源。
- 新增 `tests/test_navigation_fusion.py` 与
  `scripts/create_navigation_residual_acceptance.py` 的 nominal/spread 对照验收：18 个
  专项测试通过，spread case 的 σ 从 `0.2121` 放大到 `0.2828` m/s。
- 验收记录：
  `artifacts/evaluation/terminal-continuation-v32/navigation-residual-inflation-acceptance-20260904.json`
  （3,351 bytes，SHA-256
  `AA1E0EF13A40F9EDBD777B5D214F1601299E280DFF8E4B2B40353FF5037D47C7`）。当前 parity
  SHA-256 为 `723CC760FE277DB378BB3FA55BDB0D54149F72AF38C29D2EEC67EC95BA434B4B`，core hash
  为 `354d6a61f1dc986eca15e30fefff7ee820e7d83d0c3a18f4071904fc2a194d35`。
- 基于新 parity 的 VTOL live-AI 源、三机型六源 external join 和独立 revalidation 均通过，
  源/join/revalidation SHA-256 分别为
  `081B922CD725ACD1F42ED218F142E7AE1E8E5A684CA02F622B4AECE738E12DA` /
  `7AA749EB13AA18A42A734D890401588B9885A7805969CEE2BAF3AB500923E392` /
  `F5D1F057D85498C8E1A8BC563D950419E2D5AAAA9FDBBB28D13DF768D8A7348F`，均位于
  `artifacts/evaluation/terminal-continuation-v32/`。
- 当前 wrapper report-only E7 快照为
  `artifacts/evaluation/terminal-continuation-v32/release-gate-current-20260904T070708Z/`，
  manifest/report SHA-256 分别为
  `064FD23D88A0FA65280D4493A6AB9330748DC8D23C1FA261557DC8E7E515EBA4` /
  `87D0D7F61FC802DBA66D54A592D3ED0AB30EB83115ADEBB091162D1E580D9187`；external、
  scene-stream set 和 parity 子门通过，但总门仍 `gate_passed=false`、`release_allowed=false`，
  现场硬件、真实 GNSS/Pixhawk、HIL/REAL、人工 QGC Fly View、replay-set 和最终 release
  channel 继续 pending。严格全量回归 `2670 passed, 1 skipped`，退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v32/full-regression-v32-navigation-residual.log`
  （3,078 bytes，SHA-256
  `F1B63141BC7B6B05B897711C30EF97BB2230F779633CA71833537B62EE360656`）。
- Windows portable 构建脚本以 `-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4`
  复核成功（退出码 `0`、`ninja: no work to do`）；Release/portable 主 EXE 保持字节一致，
  46,575,616 bytes、SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`。
  构建后 `--offline --simple-boot-test` 退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v32/qgc-simple-boot-v32.log`（350 bytes，SHA-256
  `FA85CF0BA419D1335A2695DF9C9E44FAB4F2BA19C4EB1CBFA08DA60D3CF762F7`）。

### v0.3.0-pre.8 v33 现场链路只读预检刷新（2026-09-04）

- 台式机重新执行现场 Ethernet/协议/被动 route watch（2/2 samples）。接口诊断和报告结构
  校验通过，但 `Ethernet=192.168.1.13/24` 不属于期望的 `192.168.144.0/24`，没有候选
  aircraft 网卡/route；Jetson `:22`、GR01 `:5760`、相机 `:554` 均超时，最终保持
  `hardware_link_ready=false`。本轮只读，不改网卡/IP/路由，不登录 Jetson，不请求 MAVLink
  stream，不启动 RTSP，不发送飞控控制数据。
- 现场报告：
  `artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
  （12,923 bytes，SHA-256
  `2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`）；接口诊断：
  `artifacts/evaluation/terminal-continuation-v33/field-link-interface-diagnostic-live-20260904T-current.json`
  （4,412 bytes，SHA-256
  `9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`）。
- 用 v33 现场报告重新绑定的 E7 report-only 快照位于
  `artifacts/evaluation/terminal-continuation-v33/release-gate-current/`，manifest/report
  SHA-256 分别为
  `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19` /
  `340CE3C01968194B77010A808AE8A09BD62F269D9D56C786224E88099A3827D1`；结构通过但总门仍
  `gate_passed=false`、`release_allowed=false`、31 条阻断，版本仍为 `v0.3.0-pre.8`。
- `scripts/run_current_release_gate_pre8.ps1` 无参数默认改为 v33 现场报告；v32 parity-valid
  external SIM 输入保持不变。接通飞机网段并确认实际机载 IP 后再重跑现场预检，当前不关闭
  真实 GNSS/Pixhawk、HIL/REAL 或最终 release 门。
- 无参数 wrapper 烟测输出到
  `artifacts/evaluation/terminal-continuation-v33/default-wrapper-smoke/`；manifest 保持
  `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19`，report 为 54,098 bytes、
  SHA-256 `ABAA3EE7AFE0387DBABB9F34CED6C8C8B6CECDF91851DC13C4AFB5DE85D15C1C`，并明确保留
  `hardware_link_ready=false`。

### v0.3.0-pre.8 v31 多源测距残差方差膨胀（2026-09-04）

- `MultiModalRangingEngine` 的逆方差融合现在在硬一致性门内继续计算加权残差
  `χ²/(N-1)`。当传感器声明的 `σ` 无法解释实际分歧时，融合 `σ` 按 Birge ratio
  保守放大，并把结果标记为 `DEGRADED`，原因分别为
  `absolute_range_uncertainty_inflated` 或 `direct_range_uncertainty_inflated`。
  单源结果和残差可由声明协方差解释的正常多源结果保持原有数值，避免把刚好位于
  一致性门内的相机/激光/VIO 偏差压成过窄的目标落图圆。
- 新增回归覆盖“同样通过一致性门但测量分散”的 direct-range 对照，确认 `slant_range_ci95_m`
  会变宽且结果保持降级；目标坐标、QGC wire 字段和飞控只读边界不变。
- 可重算验收记录为
  `artifacts/evaluation/terminal-continuation-v31/ranging-residual-inflation-acceptance-20260904.json`
  （3,165 bytes，SHA-256
  `88516D2203D168C894945EDC5F1D7DFA6569F07B8DF9DD8ED910058BC27FA2DA`）。100/100 m 对照与
  100/110 m spread case 均通过固定测试夹具，后一组触发 `direct_range_uncertainty_inflated`，
  95% slant-range 区间由 13.86 m 扩大为 19.60 m；两组都保持 advisory-only，未产生飞控或
  物理释放输出。
- 算法源变化使 `configs/release-parity.json` 的 core hash 更新为
  `4ed386e9eaf91952b7c7be56d3d272efeaca4bb70a50e78ed57e04aa5307f5ad`。Docker Desktop Server
  `28.4.0` 已完成当前 VTOL live-AI 外部运行；三机型六份独立 runtime/AI 源记录重新 join
  为
  `artifacts/evaluation/terminal-continuation-v31/simulator-external-runtime-ai-join-20260904-current-pre8.json`
  （5,491 bytes，SHA-256
  `0681A9BC24883DBE284793217E9DBC79C62B40A920B2372625714A53010EE1DD`，`gate_passed=true`），
  独立 revalidation（561 bytes，SHA-256
  `DDF73895D5D8E6B4E7B51670E7644972248F41B17286E32AA84507369D6C9C6E`）
  `source_recomputed=true` 且通过。当前 report-only E7 绑定为
  `artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T063854Z/`：
  manifest 20,855 bytes、SHA-256
  `0FFE4FB341A972C02E7B3C15C1005198C0ED139504A7C2053C40571135AE2BC4`，report 52,924 bytes、
  SHA-256 `6B1CF975E36F65ADC2B51703BC983A0E4D3E331DE42CCD3E5E03DBA643D5EDC0`。external SIM
  与 scene-stream set 子门均通过；现场 preflight freshness 已通过但硬件端点和 route 仍未就绪，
  replay-set review/accuracy、真实 GNSS/Pixhawk、HIL/REAL、人工 Fly View 和最终 release
  channel 仍未验收。总门继续为 `gate_passed=false` / `release_allowed=false`，31 条阻断原因，
  版本继续为 `v0.3.0-pre.8`。
- 标准 wrapper 的默认输入已切换到本轮 v31 external join/revalidation 与最新现场 preflight，
  不再把 v29 旧 parity 绑定作为无参默认。严格全量回归为 `2668 passed, 1 skipped`，退出码 `0`；
  日志 `artifacts/evaluation/terminal-continuation-v31/full-regression-v31-external-rebind-20260904.log`
  为 3,078 bytes，SHA-256 `837E9E87D8B5F40EB93001BDB8F674CF151B97D0E4DEFD0DD291934FD15A86F8`。

### v0.3.0-pre.8 v30d scene-stream set 正式接入 E7 release-gate（2026-09-04）

- 三机型 `scene_stream_set` 现在是正式 E7 manifest binding：writer/API/CLI 支持重复传入
  `--simulator-scene-stream-set-evidence`，Windows `run_current_release_gate_pre8.ps1` 的默认
  输入也绑定 fixed-wing、multirotor、VTOL 三条源流。
- `release-gate-manifest.schema.json` 增加严格的
  `simulatorSceneStreamSetRecord` / `simulatorSceneStreamRecord` 定义；evaluator 每次重开
  aggregate 与 source stream，重算哈希、bytes、scenario/mode、release label、deterministic、
  truth-free 和 snapshot checks。最终 release 缺少该 binding 会 fail-closed，prerelease 缺失仍
  向后兼容，显式提供的篡改 binding 会关闭总门。
- 当前 bound manifest/report 为
  `artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound.json`
  （20,828 bytes，SHA-256 `98A301DA0147010CBF6F927122304E3AE1F47B08B095B8E9D89D535A2C37547C`）和
  `artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound-report.json`
  （52,806 bytes，SHA-256
  `1A8B3A8402132B02788217787D564B32F87E2552506152232843B8D528C61EE8`）；scene-stream 子门
  `present=true/passed=true`、3 records、9 checks 全 true，但总门仍
  `gate_passed=false/release_allowed=false`，保留 31 条真实/发布阻断原因。
- 新鲜现场只读预检
  `artifacts/evaluation/terminal-continuation-v30/field-link-preflight-continuation-20260904-133250.json`
  （17,366 bytes，SHA-256
  `F5E12FB3A202F87B0102FBBB0DEE122538D1328FEB292CCDF4FE68E4ADAF4B1F`）请求并收集 3/3 个样本，
  仍显示 Jetson/GR01/相机不可达、route 源地址 `192.168.1.13`、`hardware_link_ready=false`。
- 结构化摘要为
  `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-release-gate-acceptance-20260904-v3.json`
  （2,858 bytes，SHA-256
  `3FAAEF71A53E303B56EFBA550EDCD57A41C704894F860FA6DD3D080EF7D81214`；v3 绑定本次 3/3 现场样本，
  保留 v1/v2 历史，专项回归计数为 85）。专项回归 `85 passed`；
  全量严格回归 `2667 passed, 1 skipped`，退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-release-gate-final-20260904.log`
  （3,700 bytes，SHA-256
  `BEB6A74BC7CCB083F3B7AD3D93A99F0932FD9850D0E1684D5057F8BB45C4F8BF`）。

### v0.3.0-pre.8 v30c 现场链路只读刷新（2026-09-04）

- 台式机执行 `run_field_link_preflight.py --protocol-checks --samples 1 --timeout-s 0.5
  --expected-source-cidr 192.168.144.0/24 --require-route`，生成当前快照
  `artifacts/evaluation/terminal-continuation-v30/field-link-preflight-terminal-20260904-125150.json`
  （4,570 bytes，SHA-256 `D3511368A646821965A6B6A7E16895E5547FCCE964CDB57CD1AB28B0C1347C78`）。
- `verify_field_link_preflight.py --allow-unready` 的结构校验退出 `0`，但 readiness 仍为
  `hardware_link_ready=false`：Jetson/GR01/相机 TCP 与协议探测全部超时，三个 route probe
  均选择 `192.168.1.13`，不在期望的 `192.168.144.0/24` 内；本地 UDP 端口可用。该刷新只
  记录当前网卡和端点事实，不选择设备、不发送飞控指令，也不关闭现场/GNSS/HIL/REAL 门。

### v0.3.0-pre.8 v30b 三机型 scene-stream set 绑定（2026-09-04）

- 新增 `multidetect.simulator.scene_stream_set` 与
  `scripts/verify_simulator_scene_stream_set.py`，把 fixed-wing、multirotor、VTOL 各一份
  已验证 scene stream 聚合为同一份 hash-bound evidence。聚合器会重开并重验每个 120 帧
  源流，检查 scenario identity、mode 唯一性、release label、manifest/canonical/source
  SHA-256、deterministic、truth-free 和 snapshot 数量；任何重复、漂移或篡改均保持
  fail-closed。
- 增加严格 schema `configs/schemas/simulator-scene-stream-set.schema.json`、console
  entrypoint `multidetect-sim-scene-stream-set-verify` 和回归测试。当前绑定文件
  `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-20260904.json` 为
  2,785 bytes，SHA-256 `A2141EE7115483885C717EE9944749609A83FC5B64872D5867EC149C4BCC76A5`；
  独立重验为 2,628 bytes，SHA-256
  `A3A34A9C19FC96C7A40031B67909E0C77E7DC5E0E9C90A0E1971C7CDDAFED2E3`，两者均通过。
- 该证据只表示三机型 truth-free 软件/SIM scene-stream 契约已聚合，不提升外部 3-D、
  QGC 人工视觉、真实硬件、室外 GNSS、HIL/REAL 或最终 `v0.3.0` 发布门。版本继续为
  `v0.3.0-pre.8`。
- 聚合器、单流 verifier 和 runtime provenance 的专项回归为 `31 passed`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-runtime-focused-20260904-v3.log`
  为 101 bytes，SHA-256 `54F52A6D3F11141977876DDB2D6A7AF00DE6FB7FFCFE2AB8883643877CD9964D`。
  新增顺序无关性回归，确认同一组三机型源流以任意输入顺序生成完全一致的绑定字节。
- 结构化摘要 `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-acceptance-20260904-v2.json`
  为 2,198 bytes，SHA-256 `DD528AC09A8C006CF7EEF4ABC2346DE8937A42B7A7C270968CEB2755E7806D3F`，
  明确记录 `software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 和
  `release_allowed=false`。

### v0.3.0-pre.8 v30 运行身份快照与 fail-closed provenance（2026-09-04）

- `AutonomyRuntime.run_with_report()` 现在在消费第一帧前快照化并校验
  `RuntimeDescriptor.canonical_hash` 与 backend `input_hash`；处理期间或资源关闭时若后端身份被
  替换，运行直接失败，不会用结束时的可变字段伪造报告身份。该约束同时覆盖 REPLAY、SIM、HIL
  和 REAL 的共享运行报告边界。
- 新增 backend provenance 交换回归测试，确认身份变化会 fail-closed 且 backend 仍完成关闭；
  同时保证 observation iterator 创建失败时也会执行 backend cleanup，避免 SIM/REPLAY/HIL
  资源半关闭。随后纳入 scene-stream set 回归后的 Python 全量回归为 `2663 passed, 1 skipped`，
  退出码 `0`。唯一跳过仍是 Windows 符号链接权限限制。日志
  `artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-determinism-20260904.log`
  为 3,521 bytes，SHA-256 `95DEB2C9EE32624D9B117F026D34BD169F30320E6D735A298B0D66FC6404865B`。
- 结构化验收摘要
  `artifacts/evaluation/terminal-continuation-v30/runtime-provenance-acceptance-20260904.json`
  为 1,939 bytes，SHA-256 `FCA1733F94EDBCFCFD31CB612955595A5EBE4E38105235A2637E9BAEDE906654`；
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`、
  `release_allowed=false` 均被显式保留。
- 使用当前 pre.8 默认输入重新生成 E7 report-only 清单，确认新 runtime 收口没有掩盖其他门：
  manifest
  `artifacts/evaluation/terminal-continuation-v30/release-gate/release-gate-pre.8-current-20260904T115800Z-v6-bound.json`
  为 18,010 bytes，SHA-256 `402565280E94540A5F5A2E84DE3DBE7F45F3AA3E5B0AD825936BD64B0D60DFC`；
  report 为 48,589 bytes，SHA-256
  `E2014A9B6C992057D257899551CF35E2418BD97942DA5C6201B89EC8E6CA3794`。
  `gate_passed=false`、`release_allowed=false`，共保留 32 条阻断原因（replay-set、现场链路
  freshness/hardware、人工视觉、mission path、HIL/REAL 及 prerelease channel）。
- 随后重新采集当前台式机的只读现场 preflight（3/3 samples，协议检查开启）：
  `artifacts/evaluation/terminal-continuation-v30/field-link-preflight-live-20260904T120100Z.json`
  为 18,177 bytes，SHA-256 `3F9D1F06FA6021DA9228213B076D12169D58865491324983585A4D3F8EE4FB48`；
  接口诊断为 4,411 bytes，SHA-256
  `6D801CA49A4DC3C7F7BC21DB779B0C66F58C72C7DF5D94CF45DDB665BA083458`。报告结构 verifier
  退出 `0`、freshness 通过，但 3 个端点均超时且 route 源地址为 `192.168.1.13`，所以
  `hardware_link_ready=false`。绑定该新鲜报告的 E7 manifest/report 分别为 17,922/48,467
  bytes，SHA-256 分别为 `99CF86290FDEE4FFCA82FF24B4BB458A7C325F6BBDAAA878166DA518E37E8686` /
  `AE30959E4366CD069804FD33651ABB4EC52E5892BEC9719D74ABC13730CCA425`；旧的 stale 原因已
  消失，但总门仍 `gate_passed=false`、`release_allowed=false`，保留 31 条真实阻断原因。
- 现场 preflight、E7 wrapper 与 runtime backend 的专项回归共 `66 passed`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v30/field-runtime-release-focused-v2-20260904.log`
  为 101 bytes，SHA-256 `0651EAAEBAB70DBE258889DDADA0AFDAFE4C77BEA1F38F840369B234D1A02232`。
- 版本继续保持 `v0.3.0-pre.8`。本轮只收紧本地运行身份与证据绑定，不改变现场硬件、室外 GNSS、
  HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门的状态。

### v0.3.0-pre.8 v29 三机型外部 SIM/live-AI parity 重新绑定（2026-09-04）

- 发现历史外部 join 绑定旧 `core_algorithm_hash=434b86c6...` 后，保持当前 canonical
  parity `994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3` 不变，重新执行
  VTOL、fixed-wing、multirotor 的三份 runtime + 三份 live-AI 外部源记录。六份源记录均使用
  digest-pinned `px4io/px4-sitl-gazebo@sha256:bcf8278e...dc8ef8`、loopback 端口、只读
  场景挂载和 cleanup；三份 AI 记录均通过 armed heartbeat、AI step 与 target exercise gate。
- 当前 join `artifacts/evaluation/terminal-continuation-v29/simulator-external-runtime-ai-evidence-20260904-current.json`
  为 5,479 bytes，SHA-256 `1CD106451833C2CABBDFF3873DBF2893BB23D98C1D4EF0266ACAAD879AAFBE2E`，
  `gate_passed=true`；独立 revalidation 为 560 bytes，SHA-256
  `2848B8D8663DAB23AA8226E68876A513994D1F8BC1C59FC550432DFF3B443F8C`，
  `source_recomputed=true`、`gate_passed=true`。
- 当前源码严格回归 `python -W error -m pytest --assert=plain` 为 `2639 passed, 1 skipped`，
  退出码 `0`；日志 `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-current.log`
  的 SHA-256 为 `2615E7E55929F6AF229DD98CDBF0AA25798BFAFA0C676D3F13D94B7622B3EA37`。补齐
  `simulator_external_evidence.py` 的 `python -m` 入口后，模块 `--help` 与安装的 console
  script 均退出 `0`。
- 随后将 `multidetect` 包根的 release-gate 导出改为惰性加载，消除模块运行时的重复导入
  `runpy` warning；`python -W error -m multidetect.simulator_external_evidence --help` 通过，
  严格全量回归仍为 `2639 passed, 1 skipped`。补充日志
  `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-after-lazy-root.log`
  的 SHA-256 为 `F858F0C8FDC90706353EC0E9AADBF0B723D5F01BA129AC204B9D4C1645C3C778`。
- 绑定后的 report-only release-gate report
  `artifacts/evaluation/terminal-continuation-v29/release-gate-pre.8-current-20260904T014510Z-v6-bound-report.json`
  为 47,012 bytes，SHA-256 `4BC6D27971AF76F9D100D5CE2C5C03C6613D811260CC58AF916CC4076A50BEB8`；
  manifest SHA-256 为 `4A494D154E582B99A8FADF0137E68EE01AEF804C2AFE91FFC4177BCDA8E06DFB`。
- 外部 SIM 子门已通过，但总 `gate_passed=false` / `release_allowed=false`。replay-set、现场
  Ethernet/Jetson/相机、室外 GNSS、REAL 目标、人工 Fly View、mission-path、HIL/REAL 与
  final release channel 仍未验收；不得把本轮 software-only Docker 证据写成真实硬件或最终
  `v0.3.0`。
- 迁移后的当前以太网 `192.168.1.13/24` 上完成一次只读 `192.168.1.0/24` 服务发现：254 台
  主机中 5 台响应 SSH，MAVLink `5760`/RTSP `554` 均为 0 命中。报告
  `artifacts/evaluation/terminal-continuation-v29/field-link-discovery-live-20260904.json`
  为 8,526 bytes，SHA-256 `479732E9D458CA620DCB658B485FB2F4675EBD279BB1B8B2B65E51838A367369`；
  结构 verifier 通过，但 readiness 仍保持 false，未自动改变任何现场配置。
- 现场 route 诊断摘要新增 `summary.route_source_addresses`，按 Jetson、GR01 和 RTSP 端点
  显示操作系统实际选中的源地址；verifier 会把该投影与 `route_probes` 逐项重算，篡改或
  错配会保持 fail-closed。台式机最新只读报告
  `artifacts/evaluation/terminal-continuation-v29/field-link-preflight-live-20260904e.json`
  为 3,886 bytes、SHA-256 `8A5DF5874723819A2B5754061A42C682C7230A51CCCCA1E9AD55A59EEBD54A73`，
  verifier `--allow-unready` 退出 `0`，但三个源地址均为 `192.168.1.13`、
  `hardware_link_ready=false`。该字段只增强现场接线诊断，不改变 GNSS、RANGE_STATUS、
  REAL 或最终 `v0.3.0` 发布门。
- route 摘要回归接线后的严格全量测试为 `2654 passed, 1 skipped`，退出码 `0`；唯一跳过
  仍是 Windows 符号链接权限。日志
  `artifacts/evaluation/terminal-continuation-v29/full-regression-route-source-wrapper-final-20260904.log`
  为 3,521 bytes、SHA-256 `20BD8EF8F756B47224CEB090BC985F5DDAF2E7E9901D87C1518CEEAD292A421F`。
- 两样本 watch 复核
  `artifacts/evaluation/terminal-continuation-v29/field-link-preflight-watch-route-source-20260904.json`
  为 10,539 bytes、SHA-256 `C259273466907B4D2FF4188C0CA582BA13FD18CF3102205CC5D7D1B2EEFB8831`；
  保存报告 verifier 退出 `0`、结构门通过，但两样本都选中 `192.168.1.13`，聚合
  `hardware_link_ready=false`，保持现场门禁关闭。
- `scripts/run_field_link_preflight.ps1` 新增 `-GenerateInterfaceDiagnostic`：可在同一次
  wrapper 调用中生成并 hash-bind `inspect_field_link_interfaces.ps1` 的最新只读快照，避免
  迁移台式机时误用旧 JSON；它只写证据文件，不修改网卡地址、路由或 DNS。实跑生成的绑定
  报告 `artifacts/evaluation/terminal-continuation-v29/field-link-wrapper-generated-diagnostic-20260904.json`
  为 11,329 bytes、SHA-256 `294A962F0F2E03B585C0E8770A3CD5B59EED66EEB00ADC4E3C76F2F1D28A3ECA`，
  接口快照为 4,408 bytes、SHA-256 `E2F27056CC5900E17CFB64E2FE06E2D695ACD3B945BE515524B54310FB5472E`；
  wrapper 和保存报告 verifier 均退出 `0`，但现场 readiness 仍为 `false`。

### v0.3.0-pre.8 模型审核 envelope 与模型—回放 hash 绑定（2026-09-04）

- `evaluate_replay_set()` 和 `scripts/evaluate_replay_set.py` 新增可选的
  `--model-artifact/--model-id/--model-version` 绑定。报告会记录精确 artifact SHA-256，
  readiness 重算会保留该绑定；未提供模型绑定的旧报告继续兼容，但不能作为新模型审核的
  精确 artifact 证据。
- 新增 `src/multidetect/model_review.py` 与
  `scripts/create_model_review_bundle.py`。审核包重新验证 fire-candidate 的 post-NMS `N x 6`
  manifest、model-check 输出、部署域 IoU 报告和四类治理审核，所有缺口都写入
  `failure_reasons/next_actions`，不会修改候选 manifest。
- synthetic HIL 永远保持 `production_approval_eligible=false`，即使外部报告写入正向指标；
  `production_approved`、`flight_control_enabled`、`physical_release_enabled` 固定为 `false`。
  诊断输出 `artifacts/evaluation/terminal-continuation-v29/model-review-synthetic-20260904.json`
  的 SHA-256 为 `52C72DB0C562D7317E3BEBE85E6A8E8E69DB63B0C68414F7024B093B2C487002`。
- `create_release_gate_manifest.py --model-review-bundle` 现在把审核包作为顶层
  `model_review` hash-bound 记录写入 E7 清单；writer 与 `check_release_gate.py` 都会重算源
  bundle 并复核全部审核输入。预发布可以用 `--allow-incomplete` 保存 pending 诊断，最终
  `release` channel 则强制要求 `gate_passed=true` 与 `production_approval_eligible=true`。
- `scripts/run_current_release_gate_pre8.ps1` 新增 `-ModelReviewBundle` 转发，并将无参数默认
  external SIM join/revalidation 切换到当前 v29 parity-valid 文件；旧的
  `20260903-current-provider-fallback-parity` 快照保留为历史证据，不再参与默认 manifest 生成。
- 模型审核接线后的严格源码回归为 `2653 passed + 1 skipped`、退出码 `0`；完整汇总日志
  `artifacts/evaluation/terminal-continuation-v29/full-regression-model-review-wrapper-v2-summary-20260904.log`
  的 SHA-256 为 `DCE90748ABBFD8E1A370D8CE285D603C9BFEC9BDAA23A0EAED9DBAF9671CD0FC`。
  唯一跳过项是 Windows 符号链接权限，未被改写成通过。
- wrapper report-only smoke 使用 synthetic model-review envelope，退出码 `0`，但报告保持
  `model_review.present=true`、`model_review.passed=false`、`gate_passed=false`；输出目录为
  `artifacts/evaluation/terminal-continuation-v29/wrapper-model-review-smoke-b2a16a4739ad4ed79a655893500ddf5d/`，
  manifest/report SHA-256 分别为 `A71D4384550A551D229A06FD36E78149C2FD02E2ACD0994A8510EC529B23A70F`
  和 `887FBB62121CCE48A30A9CC65C2127D68D6AC4CCC15775F234CF77301B563B81`。
- 无参数 wrapper smoke 也已完成，输出目录为
  `artifacts/evaluation/terminal-continuation-v29/wrapper-default-smoke-57b3fc4661a040499a5a05cb0a14968f/`；
  manifest/report SHA-256 分别为 `3348C6C1DA4716377B58DCA5F238DA996F1D2A92D2E9AD47D074B160E22D6331`
  和 `83EAF47AAD51B8F70B483B1D325DCB1D4372F9B3C372A9E758B6322C87BC6605`，退出码 `0`，默认
  v29 external join/revalidation 成功绑定，`model_review.present=false` 是未传审核包时的预期。
- verifier 现在允许 `synthetic_hil` 作为结构有效但始终关闭的诊断候选重新打开；只有
  `require_eligible=true` 的最终审核路径才会把它判为失败，避免旧 synthetic envelope 因
  状态标签被误判为损坏，同时保持 production eligibility 为 `false`。
- 新增模型审核和模型 hash 绑定回归；版本继续保持 `v0.3.0-pre.8`，真实部署模型、室外
  GNSS/RTSP、HIL/REAL 和最终发布门状态不变。

### v0.3.0-pre.8 正向 replay evaluator 诊断夹具（2026-09-04）

- 在独立目录 `artifacts/evaluation/terminal-continuation-v29/replay-set-positive-synthetic/`
  生成正向 replay evaluator 诊断夹具。manifest 为 4,975 bytes、SHA-256
  `61E69B979C33F88B1E044F6192292E2CC55BF5EF6928645A7F28E16F5172BF46`；五个正例 strata
  均有确定性真值/预测，aggregate IoU precision/recall 为 `1.0/1.0`。
- 显式 `--reviewed --reviewer qa-team` 的重算报告为 12,649 bytes、SHA-256
  `6EF07FAFE7BA56E696498A856532BF46FE35227F22ADDC5C048FE4DE8C8048F2`，readiness 为
  10,940 bytes、SHA-256 `1EB519C6A5350007973742593A60F669F94AECFD646E2CEED5730BD4E507C13C`。
  由于所有条目仍是 `synthetic_demo`、未完成独立标注且不具备部署域来源，三份报告均保持
  `gate_passed=false`；它只验证 evaluator plumbing，永不替换 canonical replay-set 或最终
  REPLAY/SIM/HIL/REAL 发布证据。

### v0.3.0-pre.8 simulator orchestrator log-handle lifecycle（2026-09-04）

- `SimulatorOrchestrator._launch_process()` 现在在进程启动成功或失败后都及时关闭父进程
  持有的日志句柄；`Popen`/进程工厂仍接收同一日志路径，停止流程与结果证据格式不变。
- 新增生命周期回归，覆盖启动后的句柄关闭；严格警告全量回归
  `python -W error -m pytest -q --assert=plain` 为 `2639 passed + 1 skipped`，退出码 `0`。
- 日志 `artifacts/evaluation/terminal-continuation-v28e/full-regression-warnings.log` 的
  SHA-256 为 `57F43D18293A2E93B1B18642BEEC0C71D1E55DD0E55ED0F65B3E690C11287F21`；
  带跳过摘要的复核日志 `artifacts/evaluation/terminal-continuation-v28e/full-regression-with-counts.log`
  SHA-256 为 `807A392CC092389C8E1F51361A1C7922671E32111473F760F65347D13D0566C6`；
  `--assert=plain` 只避开 pytest assertion-rewrite 的环境 AST deprecation。
- 版本保持 `v0.3.0-pre.8`，不改变真实数据链、室外 GNSS、人工 Fly View、外部 3-D、
  HIL/REAL 或最终发布门状态。

### v0.3.0-pre.8 authenticated UDP header type hardening（2026-09-04）

- signed UDP alert header 现在要求 `version` 为严格整数 `1`，并拒绝
  `sent_at_unix_s` 的 JSON 布尔值、非数值和非有限值；sender、receiver 与 verifier
  继续共用同一 fail-closed 规则。
- 新增 3 个类型混淆回归向量；合法 wire format、ACK 关联、重试和飞控/载荷隔离保持不变。
- 当前源码全量回归为 `2638 passed + 1 skipped`，ruff/compileall 退出码均为 `0`；带完整
  pytest 汇总的日志 `artifacts/evaluation/terminal-continuation-v28d/full-regression-with-counts.log`
  为 305,776 bytes，SHA-256 为
  `D8769517FA595283741E7129ABBD96AB2CC5B868B83CED54117953B441B2B068`。
- 同一版本在真实 localhost UDP socket 上复跑 20 轮 authenticated baseline：20/20 ACK、
  关联/HMAC 通过、丢包 0；报告
  `artifacts/evaluation/terminal-continuation-v28d/alert-link-software-baseline.json`
  的 SHA-256 为 `6F0FAABAF3554C07D1070D5A02C1B91CC4D028BF8724B197A2E40E334DE579A6`，并明确
  `simulation_only=true`，不构成 GR01/REAL 证据。
- 对当前 release-gate 清单重新执行 report-only 评估仍为 `gate_passed=false`、
  `release_allowed=false`；输出
  `artifacts/evaluation/terminal-continuation-v28d/release-gate-current-report.json` 的
  SHA-256 为 `5236032C53AF6C0437512C7D23DFDF2956AF18DEEBFD2727C6537ACD7F83586E`。
- 版本仍为 `v0.3.0-pre.8`；真实数据链、室外 GNSS、人工 Fly View、HIL/REAL 与最终
  `v0.3.0` 发布门继续保持未验收。

### v0.3.0-pre.8 simulator scene-state lazy import（2026-09-04）

- `multidetect.simulator` 改为惰性加载 scene-state 导出，`python -m
  multidetect.simulator.scene_state --help` 不再产生误导性的 `runpy RuntimeWarning`；
  scenario runner 仅在显式记录状态流时导入 scene-state，默认运行和 canonical parity 保持不变。
- editable venv 已重新生成 `multidetect-sim-scene-stream-verify` console script；valid 120 帧
  VTOL stream 复核退出 `0`。
- 当前源码全量回归为 `2635 passed + 1 skipped`，ruff/compileall 退出码均为 `0`；日志
  `artifacts/evaluation/terminal-continuation-v28c/full-regression-summary.log` 的 SHA-256
  为 `3FAF04E313E44E1EBBE4C5FADD85E17CF247EA35AC36A255B77B4DF530336811`。
- 版本仍为 `v0.3.0-pre.8`；不改变真实硬件、室外 GNSS、人工 Fly View、HIL/REAL 或最终
  `v0.3.0` 发布门。

### v0.3.0-pre.8 场景流 parser/schema fail-closed hardening（2026-09-04）

- `SimulatorSceneStream.load_json()` 改用严格 JSON 解码，重复键、NaN/Infinity、非字符串
  嵌套键、重复 fault ID 和 `pose/world_origin` 真值字段均会 fail-closed；schema 对运行时
  对象保留字段同步收紧。
- 新增独立 `scripts/verify_simulator_scene_stream.py` / `multidetect-sim-scene-stream-verify`
  入口，分别报告文件 SHA、canonical stream SHA、逐帧 hash、schema 和 release label 绑定。
- 三份既有 120 帧 truth-free SIM stream 重新通过 schema、canonical stream hash 和
  round-trip replay，历史 artifact 未被改写；最新全量回归为 `2634 passed + 1 skipped`，
  退出码 `0`，日志 SHA-256 为
  `997AF8F7E50D96A699342ACE72EFBF8D778D247B5110AF5C2787D9E93EF1C171`。
- verifier 接入后的当前源码回归为 `2635 passed + 1 skipped`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v28b/full-regression-summary.log` 的 SHA-256
  为 `B04577D8AE62918985336F718DAB039B38C7819304B4D119A340CA828615C5D8`。
- 版本仍为 `v0.3.0-pre.8`；本轮不提升外部 PX4/Gazebo、真实硬件、室外 GNSS、人工视觉、
  HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 可重放 3-D 场景状态流（2026-09-04）

- 新增 `SimulatorSceneSnapshot` / `SimulatorSceneStream`：记录同一 deterministic SIM
  tick 的 WGS-84 原点、局部 N/E/高度/姿态、telemetry、observation、AI/manual/authority/
  watchdog 与活动故障，并为每帧和整条 stream 生成 SHA-256。
- `record_scene_stream` 默认关闭，保持历史 scenario-runner/matrix evidence hash 稳定；
  `python -m multidetect.simulator.scene_state` 与 `multidetect-sim-scene-stream` 均可显式
  生成状态流，三种机型共用同一契约和 native QGC Fly View 消费边界。
- 新增严格 schema `configs/schemas/simulator-scene-stream.schema.json` 及回归测试，
  拒绝真值 observation、极点坐标、乱序/时间倒退和篡改 hash。
- 三个 120 帧 truth-free SIM artifact 已生成并通过 schema/round-trip 校验；它们不提升
  为外部 PX4/Gazebo、真实硬件、人工视觉、HIL/REAL 或最终版本发布证据。
- 全量 Python 回归收集 `2634` 项，`2633 passed + 1 skipped`，退出码 `0`；ruff、compileall
  和 canonical parity 校验均通过。最新日志
  `artifacts/evaluation/terminal-continuation-v27c/full-regression.log` 的 SHA-256 为
  `79204B900F8FBAC1F6E48B4670A1B0C008975689933B6091CA63EE9E3A989D37`。

### v0.3.0-pre.8 v26 三机型 deterministic SIM 复核（2026-09-04）

- 重新执行三种机型共用的确定性 SIM 场景矩阵，启用故障尾段覆盖；输出
  `artifacts/evaluation/terminal-continuation-v26-scenario-matrix.json`，三场景各完成
  2,611 帧，9 个声明故障覆盖率均为 `1.0`，重复 hash 一致，矩阵 verifier 退出码为 `0`。
- 重新执行 native-QGC interactive fixture，输出
  `artifacts/evaluation/terminal-continuation-v26-interactive-acceptance.json`；VTOL、
  fixed-wing、multirotor 共用 AI/manual authority、watchdog 和 Mission write 阻断路径，
  fixture gate 通过。
- 两份记录均明确标记为 software/SIM、truth-free，外部 PX4/Gazebo、人工 Fly View、真实
  Pixhawk/Jetson/GNSS、HIL/REAL 和最终 `v0.3.0` 仍未验收；版本继续保持 `pre.8`。
- v26 随后执行全量 Python 回归：`2624 passed + 1 skipped`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v26/full-regression.log` 的 SHA-256 为
  `A70388821B0E7A4B8BE782162681BD3FDEFB5F710F40701A8A24C08617BC596E`。

### v0.3.0-pre.8 v25 field-link freshness-bound wrapper 复核（2026-09-04）

- 使用新的现场 watch 报告重新执行当前 E7 wrapper，验证 freshness 成对绑定在新清单上
  生效。输入报告
  `artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
  为 16,808 bytes，SHA-256
  `53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`。
- wrapper 输出目录为
  `artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900/`；manifest 为
  17,902 bytes，SHA-256
  `6C854C43E932FF4CC010B099A44ACED2CF62749C3A86D8405F27C6CFB6271CA4`，report 为 47,074
  bytes，SHA-256 `E192B4E7BED44B2B13660F58A019CF2CCAE572DB12D8C2F2012E4FCC67AD6B84`。
- manifest 使用 `max_age_s=900` 并同时绑定 `report_timestamp_epoch_s`；schema 校验为
  `0` 个错误，现场报告年龄约 `157.1 s`。由于设备端点、REPLAY 和人工/硬件门仍未就绪，
  `hardware_link_ready=false`、`gate_passed=false`；wrapper report-only 退出码 `0` 不等于
  REAL/HIL 或最终版本通过。
- 同一工作树的全量回归收集 `2625` 项，结果为 `2624 passed + 1 skipped`（Windows symlink
  权限）；日志 `artifacts/evaluation/terminal-continuation-v25/full-regression-final.log`
  为 3,492 bytes，SHA-256
  `74CCDE8D3C72276B8122DFB03579D1E76CAF98AAD0F5030F6F7AEB7DD3CDC0B7`，ruff 与 compileall
  退出码均为 `0`。
- 同一 v25b 报告绑定到完整 goal-acceptance：快照
  `artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`
  为 46,791 bytes，SHA-256
  `4514AF0836C0E33117475B3BD1C272F24D94A857B215C253BAFE05CFCE16F5D7`；日志为 58,812 bytes，
  SHA-256 `78342FAA5DDC8B43BBDDDC1EAC986A1F494F693E87AFED36366479CE03A56F59`。快照记录
  `software_status=pass`、核心 `2625`、QGC custom `66`、SIM/external 子门通过，
  `hardware_status=offline_pending`，六项 live gate 继续未关闭。
- 随后用同一 v25b 报告再次执行 E7 wrapper，输出目录
  `artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900-b/`；manifest SHA-256
  为 `7946161569981FD367820A54C45D7EDA51EDB8D9FDF581D7921089DEB4560CA7`，report SHA-256
  为 `79EB13C0B1132E81D48E86DB6EAF2ED2A7CAF320044A29F63005429D1379AC3D`。wrapper 退出码为
  `0`，但 `gate_passed=false`，没有将现场离线状态升级为通过。

### v0.3.0-pre.8 goal-acceptance v24 freshness-bound release gate (2026-09-04)

- 将 REAL `field_link` 绑定扩展为 freshness-bound 记录：manifest 写入
  `max_age_s` 与源报告时间戳，evaluator 每次读取清单都会重新调用同一 verifier；缺失、
  未来或过期时间戳保持 fail-closed；`run_current_release_gate_pre8.ps1` 新增
  `-RealFieldLinkMaxAgeSeconds` 透传覆盖；`max_age_s` 与 `report_timestamp_epoch_s` 必须
  成对出现，旧 prerelease 清单仍兼容读取，最终 `release`
  通道强制要求这两个字段。
- 重新执行完整验收。快照
  `artifacts/evaluation/terminal-continuation-v24/multidetect-goal-acceptance.json` 为
  46,146 bytes，SHA-256
  `5E5820365FD95E8BBC3987E8559A88878922CC3E3CD478D3A60AB6272C53E3C3`；日志为 58,167
  bytes，SHA-256 `CB4541FEAF9030022B4ADDAB7D7BEF9F06E4A9073E9D671D5399EB0860B33BF7`。
  `software_status=pass`、核心收集 `2624`、QGC custom `66`，现场仍为
  `hardware_status=offline_pending`，六项 live gates 未关闭。
- v24 watch 报告
  `artifacts/evaluation/terminal-continuation-v24/field-link-preflight-live-20260904e.json`
  为 16,810 bytes，SHA-256
  `2341C97D1146CC33D0734D7947A5A543D05A6D96C7E6C8B0896E1C974EA6B5F2`；离线 verifier 为
  `artifacts/evaluation/terminal-continuation-v24/field-link-verification.json`，SHA-256
  `C489A288FA478E01C980238149FB26FDD0BE81BBC7A50C2E6B0FF94A356DFB6F`，freshness 约
  `27.2/900 s` 通过，但 `hardware_link_ready=false`。
- Windows wrapper 使用 `-RealFieldLinkMaxAgeSeconds 1200` 的写入→重读样本位于
  `artifacts/evaluation/terminal-continuation-v24/e7-freshness-bound-1200/`；manifest SHA-256
  为 `3A8F97E45F7A9F17E1ED88762AD4E029C9C3CF1425CF1CEA60FDBE330BDDFFC0`，report SHA-256
  为 `74E9AC9FC1144127FC800C755D43C01EB1082E693954945535C774309C2D62B5`，记录的
  `max_age_s=1200` 与源时间戳一致，硬件门仍为 false。
- 重点回归 `final-regression-summary.log` 退出码为 `0`，记录 `276 passed`，SHA-256
  `B9424480C9EFC6120221B7F47DD9C325A17048A8FD6528936ADAB01F4B215C2F`；该证据不升级为
  室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布通过。
- 移除旧 pytest 的重复 `pythonpath` 配置后，独立全量回归
  `artifacts/evaluation/terminal-continuation-v24/full-regression-post-wrapper-no-warning.log`
  退出码 `0`，结果为 `2624 passed + 1 skipped`（Windows symlink 权限），3,477 bytes，
  SHA-256 `0CB6EE557B7529E0124540D5A1B37513B8EA2CEAB9A1F17C9B59D973310D7EAA`；ruff 与
  compileall 也为 `0`。剩余 deprecation warning 来自 pytest 6.2.5/ Python 3.12 兼容层。

### v0.3.0-pre.8 goal-acceptance v23 freshness regression snapshot (2026-09-04)

- 在 freshness verifier 增加缺失时间戳回归后，重新生成 watch 现场报告并执行完整验收。
  快照 `artifacts/evaluation/terminal-continuation-v23/multidetect-goal-acceptance.json`
  为 46,003 bytes，SHA-256
  `ABB54E58F04C12330E37B3F2574DF73979B8C6224DC8D8F5EDDEF9BDE2D35DEB`；日志
  `artifacts/evaluation/terminal-continuation-v23/goal-acceptance-rerun.log` 为 58,072
  bytes，SHA-256 `D26E844E778D53AB926077935E8980464C565109CCA9445DF4859B0D90F68762`。
- 核心收集增至 `2623`，QGC custom `66`，软件门和三机型确定性 SIM 子门通过；现场报告
  freshness 通过但 `hardware_link_ready=false`，`hardware_status=offline_pending`，
  最终 `v0.3.0` 继续保持未发布。
- 现场 verifier、目标验收脚本、文档、版本、发布门 manifest 和三机型 SIM 回归共 `108` 项
  通过；`artifacts/evaluation/terminal-continuation-v23/final-regression.log` 的 SHA-256
  为 `08321BCC6DFDEBCC3713EB62156CA65C4E934D67FED1540BBC413A0C0427FFB7`，ruff、compileall
  与 `git diff --check` 均返回 `0`。

### v0.3.0-pre.8 field-link freshness binding and goal-acceptance v22 (2026-09-04)

- `verify_field_link_report()` 新增可选 `max_age_s`：one-shot 报告校验
  `generated_at_epoch_s`，watch 报告校验 `finished_at_epoch_s`；时间戳缺失、未来或过期时
  保持 fail-closed。`run_goal_acceptance.ps1` 通过 `-FieldLinkMaxAgeSeconds` 以默认 900 秒
  窗口绑定现场报告，freshness 通过不等于端点可达或硬件就绪。
- 新鲜现场 watch 报告为
  `artifacts/evaluation/terminal-continuation-v22/field-link-preflight-live-20260904c.json`
  （16,807 bytes，SHA-256
  `0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`）；verifier 输出
  `field-link-verification.json` 为 1,797 bytes，SHA-256
  `4E58BF188B0D99D10CBCCFE1DBCA18E0A04B4E655E7BE2541A2D1090F50BFF94`，freshness 通过、
  `hardware_link_ready=false`。
- 对应完整快照
  `artifacts/evaluation/terminal-continuation-v22/multidetect-goal-acceptance.json` 为
  46,003 bytes，SHA-256
  `B2569EAF162AD9E33266DE25ABC1D61EEFB7A214C9021807A02A80C7078CD8B2`；日志
  `goal-acceptance-rerun.log` 为 58,072 bytes，SHA-256
  `4F57F20702EC1CDE80228C66DB2D4BD6E03C0B2C2A9ACCB20058590C0CE70D35`。
  `software_status=pass`、核心收集 `2622`、QGC custom `66`；现场和最终发布门仍未关闭。
- freshness、现场 verifier、目标验收脚本、文档、版本和三机型回归共 `107` 项通过；
  `artifacts/evaluation/terminal-continuation-v22/final-regression.log` 的 SHA-256 为
  `CBB3706472BC99CDB39412F0A4283DEDB3C58B5B08466BB086B244352367A02E`，ruff、compileall
  与 `git diff --check` 均返回 `0`。

### v0.3.0-pre.8 goal-acceptance v21 snapshot (2026-09-04)

- 在三机型 3-D SIM 控制绑定之后重新执行完整 `goal-acceptance`；快照
  `artifacts/evaluation/terminal-continuation-v21/multidetect-goal-acceptance.json`
  为 45,234 bytes，SHA-256
  `78498FAA088110E49B640BF3BA605B23B133142BC10AAFC6B184E1091F53D089`，运行日志
  `artifacts/evaluation/terminal-continuation-v21/goal-acceptance-rerun.log` 为 57,303
  bytes，SHA-256 `F23336F60FE289A63DDAE65E9551180A5B594B28EE4924FD2181352D1913292F`。
- `software_status=pass`、核心测试收集 `2619`、QGC custom `66`、QML parse、协议
  self-test 和当前三机型确定性 3-D SIM 证据均通过；现场仍为
  `hardware_status=offline_pending`、`field_link_ready=false`，六项 live gates 未关闭。
- 该快照不代表外部 PX4/Gazebo、室外 GNSS、人工 Fly View、HIL/REAL 或实飞通过；最终
  `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 three-airframe 3-D SIM binding (2026-09-04)

- 重新执行 VTOL、固定翼、多旋翼三份 3-D SIM 控制资产预检，并将三份独立证据用
  `verify_simulator_3d_control_evidence.py --bind` 做哈希绑定。
- 绑定结果位于 `artifacts/evaluation/terminal-continuation-v20/simulator-3d-control-binding.json`，
  SHA-256 为 `967C162D4B996C4CA709B678732A8690FFA18256B6AD958CDCC08EE6CAC61B4E`；
  `all_airframes`、`all_deterministic_gate_passed`、`records_hash_bound` 均为 `true`。
- 该记录只关闭确定性的协议/资产预检，不宣称外部 PX4/Gazebo、GPU 画面、人工 Fly View、
  HIL 或 REAL 通过；最终 `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 goal-acceptance v19 rebind (2026-09-04)

- 在构建 wrapper 的 PowerShell 兼容性修复之后，重新执行 canonical `goal-acceptance` 并
  将当前现场链路 live-preflight 重新绑定到新快照。
- 快照 `artifacts/evaluation/terminal-continuation-v19/multidetect-goal-acceptance.json`
  为 45,234 bytes，SHA-256
  `5C6E0618F0C25A04A52832F7B00CB5113EF21BC3C2C1375F658899A8B2A04066`；运行日志
  `artifacts/evaluation/terminal-continuation-v19/goal-acceptance-rerun.log` 为 57,255
  bytes，SHA-256 `CF2EC57CE361AD811733986F96BA7606005EF4BE347E80BE0F78E444FA4EC392`。
- 本轮确认 `software_status=pass`、核心测试收集 `2619`、QGC custom `66`；现场仍为
  `hardware_status=offline_pending`、`field_link_ready=false`，六项 live gate 继续保持
  未完成，最终 `v0.3.0` 未发布。

### v0.3.0-pre.8 goal-acceptance PowerShell exit-code hardening (2026-09-04)

- `scripts/run_goal_acceptance.ps1` 现在把所有原生 Python、Git 和辅助脚本的退出码统一
  通过 `Get-NativeExitCode` 读取。该 helper 在 `Set-StrictMode`、嵌套
  `powershell.exe -File` 或命令无输出时，对缺失的自动变量采用确定性 `0`，对已有失败码
  原样保留，避免把宿主差异误报成 acceptance 异常。
- 新增静态回归覆盖 helper 存在、缺失变量的成功默认值、失败码读取和禁止裸读
  `$LASTEXITCODE`；PowerShell AST、目标脚本回归和全量 Python 回归均通过。
- 全量回归日志为
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-goal-exitcode.log`
  （3,481 bytes，SHA-256
  `E688279FEF98088AD9874A63B65D4477E3AC176B8808CFEC95B6E155FDB357FA`），退出码 `0`；
  这是系统 Python fallback 的 `2617` 项记录（`2616 passed / 1 skipped`）。canonical QGC
  venv 的最新全量记录为
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-goal-exitcode-v2.log`
  （2,997 bytes，SHA-256
  `4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`），收集 `2619` 项、
  退出码 `0`，进度中仅出现 1 个 skip、没有失败标记。
- 这只提升门禁 wrapper 的可移植性，不改变任何 `hardware_link_ready`、REPLAY、HIL、REAL
  或最终 `v0.3.0` 发布条件。
- Windows PowerShell 5.1 全流程冒烟输出到
  `artifacts/evaluation/terminal-continuation-v18/goal-acceptance-after-goal-exitcode/`：总快照
  `multidetect-goal-acceptance.json` 为 45,382 bytes、SHA-256
  `C5B41616636DC9668314BCB934BBF0637F0E4E23C8CCE5110E4AFB24AE4D6851`，脚本完成并写出
  `software_status=pass`、`hardware_status=offline_pending`、`core_tests_collected=2619`、
  `qgc_custom_tests=66`；原始日志 57,403 bytes、SHA-256
  `C56ECD6682D15E096BFA2E815F369ED523F0D6DB574445609EEEF713604FE002`。这份快照仍保留
  六项现场/REPLAY live gates，未把宿主兼容性冒烟升级成硬件或最终发布通过。

### v0.3.0-pre.8 Windows build wrapper SHA-256 portability (2026-09-04)

- `scripts/build_multidetect_windows.ps1` 使用 .NET `SHA256::Create()` 流式计算 Release EXE
  指纹，不再依赖 PowerShell 可选哈希模块；构建、安装 portable 和最终摘要在 Windows
  PowerShell 5.1、嵌套 `powershell.exe -File` 与 pwsh 中保持一致。
- `-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4` 完整执行退出码为 `0`。原始日志
  `artifacts/evaluation/terminal-continuation-v18/build-wrapper-after-sha256-fix.log` 为
  142,536 bytes，SHA-256 `F28147F8FEBEF709936A31BEFD88EA7B6243AB294A00C1CF447E9AFBD2752E23`。
- 同一命令在 pwsh 宿主下也返回 `0`；日志
  `artifacts/evaluation/terminal-continuation-v18/build-wrapper-pwsh-after-sha256-fix.log` 的大小和
  SHA-256 完全一致，证明哈希收尾不依赖特定 PowerShell 模块加载状态。
- 同轮 canonical QGC venv 全量回归日志为
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-build-wrapper-fix.log`
  （2,997 bytes，SHA-256
  `4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`），退出码 `0`。
- 当前 Release/portable `MultiDetectGCS.exe` 均为 46,575,616 bytes、SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`，字节一致；portable
  `MultiDetectReplayRtspPublisher.exe` 为 95,744 bytes、SHA-256
  `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。
- 构建后 portable `--offline --simple-boot-test` 退出码为 `0`，日志
  `artifacts/evaluation/terminal-continuation-v18/qgc-simple-boot-after-sha256-fix.log` 为
  220 bytes、SHA-256 `946D08771ACF7DE07C43218D395CC514F16CDB253A34C9AC5D4601F70299BEA9`。
- 该项只改善构建 wrapper 的宿主兼容性，不改变硬件链路、室外 GNSS、HIL/REAL 或最终版本
  发布条件。

### v0.3.0-pre.8 replay fixture smoke-only semantics (2026-09-04)

- 修正 `src/multidetect/replay_set_fixture.py` 的正例分层语义：`day`、`night`、`haze`、
  `reflection` 保留固定 `flame+smoke`，`smoke_only` 现在只写入 `smoke` 真值和预测，避免
  用混合类别掩盖火焰幻检。新增回归断言逐帧检查该标签契约。
- 当前 pre.8 夹具重新生成于
  `artifacts/replay-sets/synthetic-positive-evaluator-pre8-deterministic-v5/`：manifest
  4,981 bytes、SHA-256 `2F1305E7BA634B49932186EF5E04088285587D6A8702ECA3CDD0EC61BE2F4BF5`；
  accuracy report 12,149 bytes、SHA-256
  `37E2958D9BC5AC3221F641BC6E40278CFBA48D724E1FA450B61051E5B336C63D`；fixture summary
  2,800 bytes、SHA-256 `C7EC9DB4A8E27468CB312AD165FFE6741130D95F0DBC2A782A6B76063465FFF2`。
- 独立 evaluator/readiness 重算确认五个正例层的 truth/prediction 计数均大于零，
  `smoke_only` 各为 5，aggregate precision/recall 均为 `1.0`；`synthetic_demo`、
  `annotations_reviewed=false` 仍使 accuracy/replay gate 为 `false`，不构成部署域或最终
  版本证据。

### v0.3.0-pre.8 field-link PowerShell exit-code hardening (2026-09-04)

- `scripts/run_field_link_preflight.ps1` 现在对原生 Python 和只读网卡探测统一采用
  `Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue`，缺少自动变量时以确定性
  `0` 处理，避免 `Set-StrictMode` 因 PowerShell 主机差异把有效诊断误报为 wrapper 异常。
  该改动不改变 `hardware_link_ready` 或 `-RequireReady` 的门槛；失败的链路仍在 JSON 中
  保持 `hardware_link_ready=false`。
- 现场 wrapper 冒烟记录为
  `artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-smoke-20260904b.json`
  （7,861 bytes，SHA-256
  `C437051B274C76ED4732FD0019FF20EC67FCCA16824900D9609193E1891CA0C`）。它以 1 个样本和
  `192.168.1.0/30` 有界发现运行，wrapper/verifier 退出 `0`（诊断模式），实际链路仍为
  `all_passed=false`、`hardware_link_ready=false`；发现到 1 个 SSH、0 个 MAVLink、0 个
  RTSP 响应。脚本、wrapper 和 field-preflight 回归为 `51 passed`，日志
  `artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-regression-20260904.log`
  （565 bytes，SHA-256
  `5ED87FCDFDBE91521B726EDEBBC270047EA6F0DE4FA104749D72A3D0F5DEDBEF`）。随后完整 Python
  回归为 `2616 passed / 1 skipped`（共收集 2617，退出码 `0`），日志
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-field-wrapper.log`
  （3,481 bytes，SHA-256
  `29AD92466E928C6DEC1CFBF1EC4D58C14AFF4190A74C11D1D2C976D372ABC5C0`）。

### v0.3.0-pre.8 field-link live watch after desktop migration (2026-09-04)

- 显式选择 `Ethernet` 后运行 3 样本、2 秒超时、协议检查和路由检查的只读 watch；报告
  `artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.json`
  为 16,807 bytes，SHA-256 `AC62A886C3723A5209D9C4F441537C4D7D82B0AC530DF90AE52D340F02B7238A`。
- wrapper/verifier 均退出 `0`，但 `all_passed=false`、`hardware_link_ready=false`：本机
  `192.168.1.13/24` 不在期望 `192.168.144.0/24`，Jetson `:22`、GR01 `:5760`、相机
  `:554` 三次均 timeout；本地 UDP `14580/14581` 可绑定。原始日志为
  `artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.log`
  （18,533 bytes，SHA-256
  `362095D6D841613B338CF8268445A3C3FCB3D596152261FDFCD10B54D4475C92`）。
- `-AutoSelectInterface` 在期望网段没有活动地址时按设计以退出码 `1` 停止且不写假报告；
  显式接口诊断路径随后写出上述 fail-closed 报告。该事实不关闭现场、GNSS、HIL/REAL 或最终
  `v0.3.0` 发布门。

### v0.3.0-pre.8 GNSS-to-camera lever-arm correction (2026-09-04)

- `CameraCalibration` 新增可选的 `camera_offset_forward_m`、`camera_offset_right_m`、
  `camera_offset_down_m` 和 `camera_offset_sigma_m`。它们表达相机光心相对于导航/GNSS
  参考点的 body-NED 杆臂及测量不确定度；旧标定缺省为零，保持历史结果兼容。
- `MultiModalRangingEngine.solve()` 与 `solve_direct()` 会按姿态把杆臂旋转到 NED，使用光心
  高度求地面交点，并将相对于导航参考点的完整 N/E 偏移用于地面距离、绝对方位和 WGS-84
  目标落图。杆臂测量 sigma 同时进入高度、地面距离和方位不确定度，异常或无界条件保持
  fail-closed。
- ChArUco 标定函数/CLI 将杆臂字段写入 calibration JSON 与 `mount_survey`，并在
  `configs/calibration/README.md` 记录坐标约定、100 m 域限制和示例命令；专项测距/标定回归
  `38 passed`。该软件改动不产生飞控或载荷指令，也不替代现场 GNSS/RTK、HIL/REAL 验收。
- 共享契约 `configs/release-parity.json` 已按当前源码重算，`core_algorithm_hash` 为
  `994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`；历史 E7 清单不会被
  覆盖，最终版本仍保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 parity-bound E7 revalidation (2026-09-04)

- 杆臂校正后重新生成 `configs/release-parity.json`：文件 SHA-256 为
  `BFE773CE40773A9AFA651E29FC2BA80FB1119B6B2519424A7A2A2348302BED24`，
  `core_algorithm_hash=994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`。
- 全量 Python 回归重新收集 `2617` 项，`2616 passed / 1 skipped / 0 failed`，退出码 `0`；
  原始汇总日志为 `artifacts/evaluation/terminal-continuation-v14/pytest-full-20260904-lever-arm-summary.log`
  （3,536 bytes，SHA-256 `0194FF54B0A24456B1CF045698602C428F0D4FDE3DD4AB1AB7D9B63773329B1C`）。
  专项测距/标定/兼容回归 `38 passed`，schema、ruff 和 compileall 均通过。
- 既有六源 external SIM join（SHA-256
  `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）仍记录旧 parity
  `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`。当前 verifier 对该
  文件重算得到 `gate_passed=false`，诊断为 `join: stored summary does not match source recomputation`；
  结果写入 `artifacts/evaluation/terminal-continuation-v15/external-join-revalidation-current-parity.json`
  （625 bytes，SHA-256 `5F874F6BDE9FBD62F17226465D46C2242C786EC3020B0C342E29B2793430E283`）。
- 严格 E7 wrapper 在 manifest 写入前检测到
  `simulator_external_evidence join parity hashes do not match the release-gate parity input`，
  退出码 `1`；日志 `artifacts/evaluation/terminal-continuation-v15/e7-rebind-attempt-20260904T023000Z.log`
  的 SHA-256 为 `F3DF0617A033997169741B4F4BFBBBB9076FB042393BD7F556343210BC6D7721`，没有新的
  manifest/report 被写入。当时的 PATH-only 探测未找到 `docker.exe`；后续直接调用 Docker Desktop
  运行时目录中的 CLI 完成了当前 parity 重绑，见下一个条目。
- 本轮未触及 QGC C++/QML，canonical portable EXE 不需要重编译；旧 portable 启动证据继续作为
  独立 provenance 保留，不能替代新 parity 的 external SIM 重跑。

### v0.3.0-pre.8 external PX4/Gazebo + live-AI six-source rebind (2026-09-04)

- 通过 `C:\Program Files\Docker\Docker\resources\bin\docker.exe` 使用固定 digest
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新执行 fixed-wing、VTOL、multirotor 三份 runtime 与三份 live-AI source。六份 source
  均为 `gate_passed=true`；runtime 的 PX4/GPS/loopback/只读挂载/清理门通过，AI 的 arm/disarm、
  `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、`target_exercise_accepted=true`
  通过。所有 source 都是 truth-free，未接触真实 Pixhawk、Jetson 或相机。
- 当前 parity join 为 `artifacts/evaluation/terminal-continuation-v17/simulator-external-runtime-ai-evidence-20260904-current-pre8.json`
  （5,287 bytes，SHA-256 `E0B2BA71345D6C046CA9EEFAA42DA45325B8712069CB2000DDAEEF3E7B3D858C`），
  `external_runtime_gate=passed`、`ai_target_policy_gate=passed`，硬件和人工视觉仍为
  `not_claimed`。独立重验 `artifacts/evaluation/terminal-continuation-v17/external-join-revalidation-current-parity.json`
  （565 bytes，SHA-256 `0BEF11653C13933E10F27E6A63F0DB3A1E89DCBC4438F53FE56CEF1EE58FD688`）
  `source_recomputed=true`、`gate_passed=true`、`reasons=[]`。
- 以新 join 生成的 E7 manifest/report/strict report 位于
  `artifacts/evaluation/terminal-continuation-v17/e7-rebind/`；report 已接受新 external join，
  但总门仍 `gate_passed=false`、`release_allowed=false`，保留 31 项 REPLAY accuracy、现场
  field-link、HIL/REAL、人工 Fly View 和 prerelease 阻断。该条目只关闭 software-only external
  SIM/runtime/AI 子门，最终 `v0.3.0` 继续未发布。
- 随后运行完整 goal-acceptance，所有输出写入独立目录
  `artifacts/evaluation/terminal-continuation-v18/`；主快照
  `multidetect-goal-acceptance-20260904-current-pre8.json` 为 60,087 bytes，SHA-256
  `CB6A884DFA76E52DCB8EB34F8EF678C58EA137361C31D08FDF01F3D991306507`，脚本退出码 `0`。
  核心测试收集 `2617`、QGC 自定义测试 `66`、QGC protocol self-test/QML parse 通过，六源
  external gate 已绑定且通过；`hardware_status=offline_pending`、REPLAY readiness 和现场
  链路仍未通过，剩余 live gate 保持 deployment replay accuracy、现场标定/舵面/取消/失锁、
  真实 DET→TRK→LCK→TGT、Jetson soak 与三场景人工 Fly View 验收。
- 为排除迁移后飞机 IP 变化，额外运行了有界只读发现
  `artifacts/evaluation/terminal-continuation-v18/field-link-discovery-192.168.1-20260904.json`
  （8,525 bytes，SHA-256 `79D3AA8F6E873D7C682DF3C719E941F25E5172EBEBC517BA01410651B0B89A92`）：
  扫描 254 个地址，5 个 SSH 响应，MAVLink `5760`/RTSP `554` 均 0 命中。结果仅用于诊断，
  不自动选择端点、不连接未知 SSH 主机、不改变 `hardware_link_ready`。
- 随后将现场链路预检显式绑定到新目录
  `artifacts/evaluation/terminal-continuation-v17/e7-rebind-fieldlink/`（不覆盖旧 E7）：manifest
  `release-gate-pre.8-current-20260903T184551Z-v6-bound.json` SHA-256
  `E8841054FB71BD5FC0374ED6C12A5E3485444C161869AE627C29673CC1506A0C`，report-only
  `release-gate-pre.8-current-20260903T184551Z-v6-bound-report.json` SHA-256
  `9E40DC8A46E7235504A244D8B00697B0D0D2C13AD5A6E986EDC3ECFB19F1B3D9`，strict report
  `release-gate-pre.8-current-20260903T184551Z-v6-bound-strict.json` SHA-256
  `0840AD72023A877BB258B2A7975ACC8002DF1A5C0F89502CE972A307961EC92D`。严格退出码 `2`，
  31 项阻断保持不变，`real_field_link` 明确绑定 `hardware_link_ready=false`。
- 同一轮保存现场链路只读预检
  `artifacts/evaluation/terminal-continuation-v17/field-link-preflight-current-20260904.json`
  （3,434 bytes，SHA-256 `C64890B38EC040FD365C8AB9D6BA3D31620F7442F256A7BF812CBE803009039F`）。
  默认 `192.168.144.0/24` 下 Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、
  相机 RTSP `192.168.144.108:554` 均 timeout；本机 UDP `14580/14581` 可绑定，故
  `hardware_link_ready=false`。这是只读可达性证据，不关闭现场、HIL 或 REAL 门。

### v0.3.0-pre.8 E7 wrapper robustness and field-bound refresh (2026-09-04)

- 修复 `scripts/run_current_release_gate_pre8.ps1` 的 `Invoke-Python`：在严格模式下通过
  `Get-Variable -Name LASTEXITCODE` 读取原生 Python 退出状态，并在变量不存在时使用确定性
  `0`，避免部分 PowerShell 主机在 manifest 写入前因未定义自动变量中止。这个修复只处理
  wrapper 状态读取，不放宽发布门；对应 wrapper/goal-acceptance 接线回归为 `15 passed`，完整
  E7 专项集合为 `56 passed`。原始日志为
  `artifacts/evaluation/terminal-continuation-v18/e7-wrapper-regression-20260904.log`（618 bytes，
  SHA-256 `762CEA25A1B6F793DECC8B945CEF2FE81D7EB780B44CD689FB57CE3FAA594C12`）。
- 用当前六源 external SIM join、v18 现场预检和当前 SIM/安全/Mission evidence 生成独立
  E7 绑定目录 `artifacts/evaluation/terminal-continuation-v18/e7-rebind-current-v5/`，不覆盖
  历史产物：manifest `release-gate-pre.8-current-20260903T190334Z-v6-bound.json` 为
  17,702 bytes、SHA-256 `1C88F8C7EC0F087126C3B274C17CFF23412969C213A158BA261DB6B8EB498CC8`；
  report-only 为 46,713 bytes、SHA-256
  `04D04E9B822935DB0150F7AC0CE93BAE209E5C7D0674B24244B6E173DA9ECDFA`；strict report 为
  46,713 bytes、SHA-256
  `2F9A7CD22BEA34C12832316759A8147BAC08E7F5E9F6415B4DF99A4ED0C394EF`，strict evaluator
  退出 `2`。
- E7 已接受当前 external join（`simulator_external_evidence.passed=true`），但总门仍为
  `gate_passed=false`，保留 31 项 REPLAY accuracy/deployment、现场链路、人工 Fly View、
  HIL/REAL 硬件与真实控制、prerelease channel 等阻断；`hardware_link_ready=false` 仍是
  现场事实。该次只改 wrapper 与文档，canonical portable EXE 不变，最终 `v0.3.0` 继续未发布。

### v0.3.0-pre.8 deterministic SIM scenario matrix fault coverage (2026-09-04)

- 用当前源码运行三机型场景矩阵：`coastal_patrol_fixed_wing_001`、`river_rescue_vtol_001`、
  `urban_inspection_multirotor_001`。AI、arm、重复运行和 fault coverage 全部开启；每个场景
  自动延长到 `5221` 帧，AI accepted `5221` 步，fixed-wing/VTOL/multirotor 的 evidence hash
  在重复运行中完全一致。
- GPS dropout/drift、camera dropout、airspeed freeze、wind gust、battery drop 等声明故障全部
  触发，`gate_passed=true`、`deterministic_gate_passed=true`、`repeatability_checked=true`。
  证据 `artifacts/evaluation/terminal-continuation-v16/simulator-scenario-matrix-20260904-current-pre8.json`
  为 13,302 bytes，SHA-256 `6F39876CE26EFF7B8E4DCFF9E4E083E3A450441BA97F63439B62392BFB29A2FF`；
  独立 verifier 退出码 `0`。
- 该记录使用 truth-free 进程内 SIM runner，不启动 Docker/PX4/Gazebo，不读取场景 truth；
  `external_3d_runtime`、QGC 人工视觉、硬件和 REAL 仍保持 `not_claimed`，它只关闭确定性
  software/SIM 矩阵子门，不改变 E7、HIL/REAL 或最终 `v0.3.0` 发布状态。

### v0.3.0-pre.8 outdoor geolocation error ellipse (2026-09-04)

- WGS-84 真值评估现在把估计点与独立真值的残差转换为局部 `N/E` 米制坐标，输出北/东偏差、
  `2×2` 样本协方差、`1σ` 主轴/次轴/方向和二维 `95%` 椭圆（`χ²(0.95,2)`）。这为室外
  多距离定位提供方向性误差证据，同时保留运行时/QGC 的保守 `horizontal_sigma_m` 圆和现有
  wire 兼容性。
- `verify_geolocation_accuracy_report()` 会对新增 `error_ellipse` 重新计算并拒绝篡改；缺少
  该可选节的历史 v1 报告仍按原字段验证，避免历史证据失效。JSON schema、单元测试和
  `jsonschema` smoke 均同步通过。该评估输出仍是离线只读证据，不关闭现场 GNSS、HIL/REAL
  或最终 `v0.3.0` 发布门。
- 本轮全量 Python 回归为 `2612 passed, 1 skipped`（共收集 `2613`，退出码 `0`）；唯一跳过
  是 Windows 符号链接权限 `WinError 1314`。原始日志和机器可读汇总保存在
  `artifacts/evaluation/terminal-continuation-v11/`，没有把该环境缺口写成产品失败。
- canonical portable EXE 以 `--offline --simple-boot-test` 运行退出码 `0`；启动证据同时绑定
  当前 `MultiDetectGCS.exe` 的大小和 SHA-256，确认本轮 Python/文档变更没有改变已验证的
  Windows 运行入口。
- 现场链路只读 watch 重新采样两次：本机 `192.168.1.13/24` 仍不在期望的
  `192.168.144.0/24`，Jetson/GR01/RTSP 均超时而本地 UDP 端口可用；报告保留
  `hardware_link_ready=false`，不向 HIL/REAL 门注入假阳性。
- 最新现场快照已经绑定到 `artifacts/evaluation/terminal-continuation-v13/` 的 pre.8 E7
  清单；严格 verifier 退出 `2`、`gate_passed=false`、`release_allowed=false`，保持
  31 项真实阻断原因可追溯。

### v0.3.0-pre.8 external SIM runtime preflight and Docker AI closure (2026-09-04)

- `runtime-preflight` 现在可选接收 `--repository-root`、`--scenario` 和 `--px4-workdir`，只读
  解析仓库自有场景、世界/模型资产哈希以及 PX4 `etc/init.d-posix/rcS` 启动锚点；输出新增
  `asset_preflight`、`px4_workdir_preflight` 和 `external_runtime`。预检阶段的
  `external_runtime.gate_passed`/`external_runtime_gate_passed` 固定为 `false`，确定性
  flight-model 回退明确记录 `automatic=false`、`used=false`，避免外部运行时静默降级。
- 场景绑定证据写入 `artifacts/evaluation/terminal-continuation-v10/runtime-preflight-scenario.json`
  （7,188 bytes，SHA-256 `1E243FBA41273CA94FBB6B349573FB4206B4A26CEED5C55778CF545A3A2590E1`）。
- 使用固定 digest 的 `px4io/px4-sitl-gazebo` 完成一次 Docker/SITL（非硬件）AI 闭环，证据为
  `artifacts/evaluation/terminal-continuation-v10/simulator-docker-ai-acceptance-external.json`
  （114,687 bytes，SHA-256 `DF58B68B40D34683DFE1309A4A45965FCD26EC40887B7077DBBA6BB65752C1FF`）。
  `gate_passed`、`preflight_gate_passed`、`runtime_gate_passed`、`telemetry.qualified`、
  `ai_policy_gate_passed`、`target_policy_gate_passed` 均为真，`ai_policy_steps=2`，容器
  已真实启动并在结束后清理；该证据不代表 QGC 视觉、Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL
  或最终 `v0.3.0` 通过。
- 随后按脚本的 post-adoption 序列余量要求完成 900 帧外部 PX4/Gazebo + 原生 QGC Fly View
  + VTOL Mission 组合 SIM 重跑。成功 source 为
  `artifacts/evaluation/combined-sim-qgc-20260903T164443Z-4900/combined-evidence.json`
  （249,113 bytes，SHA-256 `0DC4044DB459888D4DC1648CC8266B545FBEF86D346FF45E8199C75909911762`），
  独立 verifier 为同目录 `combined-evidence-verification.json`（1,956 bytes，SHA-256
  `A31188E69D6EAA5A8752CA11CEF06D2CFE63AAB8601A3ABCEC2F980B536CF0CF`），40/40 checks 通过。
  另生成 source 专属 sim release-mode envelope
  `artifacts/evaluation/terminal-continuation-v10/release-mode-sim-combined-qgc-20260903T164443Z-4900.json`
  （542 bytes，SHA-256 `80AB0572968BE5992E523BDD8ED2E402D996FAA772A9ED70AB0974769FF1EB52`），
  `--require-release-mode` 复核输出 `combined-evidence-verification-release-mode.json`
  （2,596 bytes，SHA-256 `BDD55C221332DDB27599433AF09B58F078C26CC95399EA88B97D84EC5D1489F9`）仍为
  40/40 checks 通过。
  该 source/envelope 已绑定到独立 E7 目录
  `artifacts/evaluation/terminal-continuation-v10/e7-combined-qgc-rerun/`：manifest SHA-256
  `D2A4FE349CB45D0AC872ACA589532B6A3E2586EB1196856791647BE708FA9207`，report-only SHA-256
  `DA99390FE22DFFD8D3280DCAA94B3A5B89E5755BAB23DA2C0A661E14A4D71B18`，strict report SHA-256
  `D71E6AB91BEEE570A5F321ED3A9309D9AADCE9433B197D2E0CE9CC03A023E3D5`；`simulator_combined`
  子门为真，strict 总 E7 仍以退出码 `2` 保持 `gate_passed=false`/`release_allowed=false`。
  900 帧 type-23/type-24/RTSP 屏障、Gazebo 必需传感器稳定 `2/2`、动态端口隔离与释放、容器
  清理、VTOL `MC→FW→MC`、7 项 INT-only Mission readback/reached、AUTO.MISSION、land/disarm
  均通过；Mission-ready 后 QGC 序列 `569→574→579`。此前 300 帧超时文件保留为失败历史，
  不被改写。组合证据仍明确 `operator_visual_acceptance=not_claimed`、software-only，不能
  代表人工 QGC 视觉、真实硬件、室外 GNSS、HIL/REAL 或最终版本发布。

### v0.3.0-pre.8 terminal continuation E7 rebind v8 and replay readiness v7 (2026-09-04)

- 使用当前默认输入在独立目录 `artifacts/evaluation/terminal-continuation-v8/` 刷新 E7
  report-only/strict 绑定。manifest `release-gate-pre.8-current-20260903T161008Z-v6-bound.json`
  为 17,786 bytes（SHA-256 `9A4F03F02A49A69DA47DC6C0BE7B15A8849FA9DE230A05A1B37B70207C3C83B9`），
  report 为 47,742 bytes（SHA-256 `C3A86200F591941473CF8B684C128AAC1B7FE837F3A8BA2882ED03E2BB40173D`），
  strict report 为 47,742 bytes（SHA-256 `FC498FF3ED174E0F740409AC988139E2664D694875247C900E8B835EFDEB9382`）。
- 严格 verifier 退出码为 `2`，`gate_passed=false`、`release_allowed=false`，共 31 个阻断项；
  三机型 3-D control、deterministic 和 S4 safety 的 hash-bound 子门保持通过，现场、REPLAY
  accuracy、人工 Fly View/Mission、HIL/REAL 与最终 release channel 继续 fail-closed。
- 对正例 synthetic evaluator fixture 重新执行 readiness，输出
  `artifacts/evaluation/terminal-continuation-v7-replay-readiness.json`（11,263 bytes，
  SHA-256 `E3FA53E95FA566BC9816FD4D9CFFC3902949B3D5176BC974E1A5F0ADFBA50EFC`）。六层正例
  真值/预测覆盖和 evaluator 重算通过，但 `synthetic_demo`、未复核标注使 readiness
  `gate_passed=false`；该证据不替代真实部署域录像与人工复核。
- 台式机现场链路只读续检写入
  `artifacts/evaluation/terminal-continuation-v9/field-link-preflight-20260903T161925Z.json`
  （16,804 bytes，SHA-256 `15AA28819D7B1D94AE082C4EE3EA458D64A4BCBFC2CF05F299EEED07586B992E`），
  wrapper/verifier 均退出 `1`。三样本 Jetson/GR01/RTSP 端点全部超时，路由源
  `192.168.1.13` 不在期望 `192.168.144.0/24`，`hardware_link_ready=false`；本地
  `14580/14581` UDP 端口仍可用。该失败快照只更新现场事实，不升级 REAL/HIL/GNSS 门。

### v0.3.0-pre.8 terminal continuation E7 binding v6 (2026-09-03)

- 在独立目录 `artifacts/evaluation/terminal-continuation-v6/` 生成当前默认 E7
  report-only 绑定，未覆盖任何 `latest` 或历史证据。清单
  `release-gate-pre.8-current-20260903T160000Z-v6-bound.json` 为 17,839 bytes，SHA-256
  `8D86A97CD4C8CB099CB852EA11FE3C325AB783936A00F1E5EDC77EEE58DCC619`；报告
  `release-gate-pre.8-current-20260903T160000Z-v6-bound-report.json` 为 47,799 bytes，
  SHA-256 `86EC0A976FB5AD1D4968805BF21AE270A561F77017D25BAF0B22CDD674ADD572`。
- 清单把 v5 三机型 3-D control（fixed-wing、multirotor、VTOL）和 S4 safety 证据按路径、
  字节数及 SHA-256 重新绑定；聚合结果确认三种机型的 `gate_passed`、deterministic 和
  record hash binding 均为真，S4 safety 也通过。外部 PX4/Gazebo、视觉和硬件仍保持
  `false/not_claimed`。
- E7 report-only 状态仍为 `gate_passed=false` / `release_allowed=false`：部署域
  REPLAY/accuracy、现场 `hardware_link_ready`、人工 Fly View/Mission、HIL/REAL 硬件与
  真实控制、最终 release channel 尚未满足。wrapper 退出码 `0` 仅表示失败门状态已被
  完整记录，版本继续保持 `v0.3.0-pre.8`；随后对同一清单运行严格 verifier，退出码为
  `2`，再次确认 fail-closed。

### v0.3.0-pre.8 developer dependency closure (2026-09-03)

- `[dev]` 依赖补齐 `jsonschema>=4.20`，并将 `attrs`、`jsonschema-specifications`、
  `referencing`、`rpds-py` 及主包 extra 关系写入 `uv.lock`；项目 venv 的 `uv lock --check`
  通过，clean checkout 不再在 RTSP schema 测试收集阶段缺少 `jsonschema`。
- 新增跨 replay-set manifest 的校准 verification 绑定回归：verification 文件即使自身
  `gate_passed=true`，只要绑定到另一份 replay manifest，release-gate manifest writer 立即
  fail-closed 拒绝。项目 venv 全量 pytest 收集 `2606` 项，`2605 passed / 1 skipped`；日志为
  `artifacts/evaluation/pytest-full-20260903-dev-dependency-v1.log`（SHA-256
  `B4F17F1B835467D0338B82EC7FBF825B756D4F611163A2CEFAE703001994CC91`）。
- 重新执行 `pip install -e ".[dev]"`，修复迁移后项目 venv 残留的 `multi-detect 0.1.0`
  editable metadata；现在 pip metadata、模块版本与产品清单一致为 `0.3.0rc8 /
  v0.3.0-pre.8`，并通过 `pip check`。

### v0.3.0-pre.8 terminal continuation acceptance (2026-09-03)

- 在不覆盖既有 `latest` 或历史证据的前提下，使用项目 venv 重新执行完整终端验收：核心
  pytest 收集 `2606` 项，QGC 自定义测试 `66` 项，ruff、QML 解析、协议自测、SIM 三机型
  故障覆盖、PX4 Mission 事务和 git diff 检查均通过。新快照为
  `artifacts/evaluation/terminal-continuation-v1/multidetect-goal-acceptance-20260903-terminal-continuation-v1.json`
  （44,931 bytes，SHA-256
  `9E207A3B7E6C03BF69C9EB28E3568F56438EA9ACB8C66C60CC1E5FCCD46065DD`）。
- 快照记录版本仍为 `v0.3.0-pre.8`、`hardware_status=offline_pending`；活动网卡
  `Ethernet=192.168.1.13/24` 未连接 `192.168.144.0/24`，Jetson 与相机不可达。现场 GNSS、
  Pixhawk、Jetson soak、部署域 replay、HIL/REAL 和人工 Fly View 门继续保持 pending。
- 同一 portable 入口完成 headless `--offline --simple-boot-test` 烟测并以退出码 `0` 完成：
  `artifacts/evaluation/terminal-continuation-v1/qgc-simple-boot-20260903T135047Z.log`，
  SHA-256 `83437047EDDDE232A7E6F6AA3C7727CB0B6C6555D62ED0C19FA84D62E3F135D9`。这只证明
  无界面启动链路，不关闭人工 Fly View 或真实硬件门。
- 22:00 后补充同一 QGC 窗口的句柄级 `PrintWindow` 观察：
  `artifacts/evaluation/terminal-continuation-v1/qgc-flyview-window-20260903T141750Z.png`
  （SHA-256 `5645F55713060EB8D8C2D51F97D73F609DE5B08DFCB19FBE60193827CFE772E6`），记录了
  原生 Fly View、`M1·DET`、视频/地图表面和断开等待态。元数据
  `qgc-flyview-capture-20260903T141750Z.json` 明确标记 `operator_visual_acceptance=not_claimed`，
  不把工程截图写成操作员验收。

### v0.3.0-pre.8 current-provider external SIM revalidation (2026-09-03)

- 以当前 `configs/release-parity.json`（SHA-256
  `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
  `core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`）为基准，
  重新校验六源 external SIM join
  `simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
  （SHA-256 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）。verifier
  退出码为 `0`，`source_recomputed=true`、`gate_passed=true`、版本为 `0.3.0-pre.8`。
- 新的独立 revalidation 为
  `artifacts/evaluation/terminal-continuation-v2/simulator-external-runtime-ai-revalidation-current-provider-fallback-parity.json`
  （558 bytes，SHA-256
  `5576A33BE03572344825430CE78DDF4B2F7FB03087F1DE025DAFC8325E9E6D35`）。完整 goal acceptance
  续跑写入
  `artifacts/evaluation/terminal-continuation-v2/multidetect-goal-acceptance-20260903-terminal-continuation-v2.json`
  （45,325 bytes，SHA-256
  `B4B3E9C3F2566FAF6313FE7D42CD7E8706C8FAEB3AB3B4E715491287D8491589`），核心 pytest `2606`、
  QGC custom `66`、`software_status=pass`，external evidence 四项绑定状态均为 `true`。
- external runtime 子门已在当前 acceptance 中验证，剩余项只保留三机型人工 QGC Fly View
  视觉验收及部署域 replay、标定、执行器断推进器、真实检测和 Jetson soak；该证据继续标记
  truth-free/software-only，`hardware_status=offline_pending`、`operator_visual_acceptance=not_claimed`，
  不改变最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 desktop field-link preflight continuation v3 (2026-09-03)

- 在同一台式机上重新执行只读现场 preflight：不登录 Jetson、不请求 MAVLink 流、不启动 RTSP，
  也不发送飞控或执行器数据。报告为
  `artifacts/evaluation/terminal-continuation-v3/field-link-preflight-20260903-terminal-continuation-v3.json`
  （6,686 bytes，SHA-256
  `CD8D3819670444803DD793E1B7F13258E5101CCA8E7CFD71F8428D7BBC6B665D`）。
- `Ethernet=192.168.1.13/24` 仍未接入 `192.168.144.0/24`；Jetson `:22`、GR01 `:5760`、相机
  `:554` 均超时，本地 `14580/14581` 端口可绑定，路由源地址仍为 `192.168.1.13`。wrapper 和
  verifier 都返回 `1`，`hardware_link_ready=false`；该快照只更新现场事实，保留
  `hardware_status=offline_pending`，不改变软件 acceptance、HIL/REAL 或最终发布门。
- 将该现场报告绑定进新的完整 goal acceptance：
  `artifacts/evaluation/terminal-continuation-v3/multidetect-goal-acceptance-20260903-terminal-continuation-v3.json`
  （45,914 bytes，SHA-256
  `A112A891F52B137EF293673DF7A4F1CC7FDC4F6DFE2DA9B09042D8737F8BA8C8`）。核心 pytest `2606`、
  QGC custom `66`、`software_status=pass`；field preflight `provided/verified=true` 但
  `hardware_link_ready=false`、verifier exit `1`，硬件仍 `offline_pending`，六项 live gates
  保持不变。
- 额外执行三机型 deterministic SIM 长帧故障覆盖：
  `artifacts/evaluation/terminal-continuation-v4/simulator-scenario-matrix-fault-covered-pre8.json`
  （13,298 bytes，SHA-256
  `7AB2CB409B27FB9AB94B95CD9F450700CA79B9814C827B66DD56F7A70AABCAF3`）。`--cover-faults`
  自动将帧预算扩到 `2651`，`--require-fault-coverage`、三机型 coverage ratio `1.0` 和
  repeatability 均通过；该证据仍是 truth-free/software-only，不改变硬件或最终发布门。

### v0.3.0-pre.8 simulator control/safety continuation v5 (2026-09-03)

- 使用当前源码重新执行共享 3-D 控制契约：
  `artifacts/evaluation/terminal-continuation-v5/simulator-3d-control-pre8.json`
  （83,252 bytes，SHA-256
  `493AE72466F1BDEC1F5C8AD225B41B86D1B4A94A5E4DC1AD0F7791E77059C7BD`）。
  `gate_passed=true`、`deterministic_gate_passed=true`；manual QGC、AI authority takeover、
  AI 在人工接管期间抑制、释放后恢复、watchdog neutral/failsafe、native QGC-only path、
  Mission/flight write block 和 loopback 隔离均通过。PX4/Gazebo 可执行文件当前不可用，
  因而 `external_runtime_gate_passed=false`、视觉/硬件验收仍为 `not_claimed`。
- 同一源码下重新执行 S4 safety：
  `artifacts/evaluation/terminal-continuation-v5/simulator-safety-pre8.json`
  （6,991 bytes，SHA-256
  `7293321BF87CD10E15199847BB9AEC5CCBFBE083FDD2DC0665F8071E95FC38F6`）。
  `gate_passed=true`、`truth_free=true`；目标取消只清理选中目标、极点/非法 GPS fail-closed、
  failsafe 后不再接受控制、终态后拒绝 Mission event 均通过。该证据仍是 software/SIM 子门，
  不提升真实 Pixhawk、Jetson、相机、执行器、HIL/REAL 或人工 Fly View。
- 随后分别以 fixed-wing、VTOL、multirotor 场景生成三份当前控制证据，并完成独立 hash-bound
  binding：`artifacts/evaluation/terminal-continuation-v5/simulator-3d-control-binding-pre8.json`
  （2,380 bytes，SHA-256
  `6C5F4DD6F65490A13ACDC3B19887233EFFDC1FD27F5ACD6611A6AD85B1CA73BF`）。binding verifier
  逐项确认 `all_airframes=true`、`all_gate_passed=true`、`all_deterministic_gate_passed=true`、
  `records_hash_bound=true`、`unique_modes=true`、`unique_scenarios=true`；三份 evidence 的
  `external_runtime_gate_passed=false`，因此仍只代表 software/SIM control contract。
  同一 verifier 输入连续运行两次，binding 输出字节完全一致，输出 SHA 均为
  `6C5F4DD6F65490A13ACDC3B19887233EFFDC1FD27F5ACD6611A6AD85B1CA73BF`。

### v0.3.0-pre.8 replay evaluator/calibration binding (2026-09-03)

- 为已存在的 synthetic positive replay fixture 生成独立的 threshold calibration 与
  verification。calibration 的 `promotion_ready=false` 保持真实状态，verification 的
  `gate_passed=true` 只表示报告、manifest/canonical SHA-256 和重算结果一致：
  `threshold-calibration-20260903-pre.8-positive-fixture.json`（SHA-256
  `4CF5D9831729B692867D243203C7CF9F2B6935BA2E8050DB4372725597049E4C`）和
  `threshold-calibration-20260903-pre.8-positive-fixture-verification.json`（SHA-256
  `BE978504F28A6D7553438E1FA2E93E81BAE37B02699C9445B0D073427E026557`）。
- 将该 fixture 的 accuracy report 与 calibration verification 绑定到独立诊断 E7
  `release-gate-pre.8-current-20260903T131700Z-v6-bound-report.json`（SHA-256
  `46E1D0BBAAED0F0C321DF025F7CB7156B7A98F846CC5AE17CD84CCDB607844BC`）。五个正例层覆盖、
  aggregate IoU precision/recall 和校准哈希链路均可重算，但因为来源仍是 `synthetic_demo`、
  标注未独立复核、现场链路未就绪以及其它 REAL/HIL 门，`gate_passed=false`、
  `release_allowed=false` 保持不变。严格输出为
  `release-gate-pre.8-current-20260903T131700Z-v6-bound-strict.json`（SHA-256
  `80C4E0370891004613B58CCCE00369F35480E898B9F5EEF75FFCD0F5FE9FF673`）；该诊断绑定不替换
  canonical `sim-gazebo-2026` 空正例基线，也不升级为部署域精度或最终 v0.3.0 证据。

### v0.3.0-pre.8 desktop field-link fail-closed snapshot (2026-09-03)

- 在迁移后的台式机上新增只读网卡/路由快照：`Ethernet=192.168.1.13/24`，目标飞机网段
  `192.168.144.0/24` 无候选接口或直连 route，`selection.status=missing`。快照为
  `artifacts/evaluation/field-link-interface-diagnostic-20260903-desktop-pre8.json`
  （4,384 bytes，SHA-256
  `CAB5758FA853649A74CAD3E9F7D7D3B5F718DA1F2BA8118CE58302635CFFCC6D`）。
- 新增一份 hash-bound 三采样 fail-fast 现场预检：
  `artifacts/evaluation/field-link-preflight-20260903-desktop-pre8-interface-bound.json`
  （6,658 bytes，SHA-256
  `913F6E0BB79EA0F5831B6998F8F7F9415CD380DAC6C9F00F5CA1F5A0D5A10584`）。首个样本记录
  Jetson/GR01/RTSP 超时和 route source `192.168.1.13`，本地 `14580/14581` 可绑定，
  `hardware_link_ready=false`；`--allow-unready` 只验证结构与绑定，不提升现场门。
- 用该最新报告生成了 pre.8 report-only E7 绑定
  `release-gate-pre.8-current-20260903T124800Z-v6-bound.json`（SHA-256
  `80BEDA6B77D76B5E3F050B3DDC7D7A035A232438B7FCBAC18159ECD2BDD0CF8F`）及报告
  `release-gate-pre.8-current-20260903T124800Z-v6-bound-report.json`（SHA-256
  `D073D15CA04C46EE02DB0B06E3704C6DC2E25F64C32C275825DAA7F820B4C464`）。总门继续
  `gate_passed=false` / `release_allowed=false`，版本仍为 `v0.3.0-pre.8`。
- 随后用 Windows wrapper 的 `-ProtocolChecks -RequireRoute -RequireReady -StopOnFailure`
  重跑严格现场预检，报告
  `field-link-preflight-20260903-desktop-pre8-wrapper-strict-r2.json`（7,449 bytes，SHA-256
  `A3F2037643363DCCBDC5BFC83000073AB12EA382E5385F5C0CBA56A7C815EA36`）。严格 wrapper 和
  verifier 均返回 `1`，但完整 fail-closed 报告保留并可离线复核；对应最新 E7 绑定为
  `release-gate-pre.8-current-20260903T125900Z-v6-bound.json`（SHA-256
  `D0D71A48E44358D919E24179B1262ACDF47471D353E0AA9FF0630F746D3455F8`）及报告
  `release-gate-pre.8-current-20260903T125900Z-v6-bound-report.json`（SHA-256
  `8CB65C5C607B069C4DDC9FBE9A3976C8A7F85453726A93F11B57FBC9918B5F2E`）。

### v0.3.0-pre.8 RTSP multi-client fan-out (2026-09-03)

- `MultiDetectReplayRtspPublisher` keeps the default single-client path on a
  non-shared RTSP media factory, tracks every active `appsrc` generation and
  fans one validated BGR24 frame out with independent `GstBuffer` references.
  Reconnect retries exclude generations that already accepted the frame, while
  stale/retired generations remain fail-closed.
- Added `--minimum-client-count 1..64`. For a minimum above one the helper uses
  GStreamer shared media, which is the native multi-client fan-out path and
  avoids serialising concurrent `DESCRIBE`/encoder preroll; minimum one retains
  non-shared generation isolation. `frame_accepted` is emitted only after the
  selected mode reaches its configured client/media lower bound.
- RTSP caps advertise a positive `30/1` nominal rate solely for SDP negotiation;
  replay `captured_at_ns` remains the authoritative `GST_BUFFER_PTS/DTS` source.
- Added `scripts/verify_replay_rtsp_multiclient.py`, a concurrent raw
  `OPTIONS → DESCRIBE → SETUP → PLAY` and interleaved RTPv2/PT96 verifier, plus
  `configs/schemas/replay-rtsp-multiclient-bench.schema.json` and regression
  coverage. The current 2-client/3-frame software-only artifact is
  `artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v3.json`
  (2,781 bytes, SHA-256
  `C22A0EAFBF9915CBC61B65E583BB4CCB333B6E1E4B9433784E914743C3F81989`).
  It records `gate_passed=true`, `source_kind=software_only_local_rtsp` and
  `hardware_claim=none`; it does not close aircraft, outdoor GNSS, HIL or REAL
  release gates.
- Release helper `build/windows-v0.3.0-pre.8-release/Release/MultiDetectReplayRtspPublisher.exe`
  is 95,744 bytes with SHA-256
  `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`.
- After portable deployment, the same bench was rerun against the canonical portable helper at
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectReplayRtspPublisher.exe`.
  `artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v4-portable.json` is 2,798 bytes,
  SHA-256 `96199FF2CF802B42E3A3CEDA4BFD44A8BAF328798F7C32D27ECCAC35C8BF3456`, and records
  `gate_passed=true` for two concurrent raw RTSP/TCP clients, H.264/positive-framerate SDP,
  interleaved RTPv2/PT96 packet deltas, stable fan-out, EOS and helper summary. This remains
  software-only transport evidence and does not close aircraft, outdoor GNSS, HIL or REAL gates.

- After the shared-media handshake fix, the canonical portable helper passed a
  four-client/two-frame raw RTSP bench:
  `artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json`
  (3,299 bytes, SHA-256
  `994818A9FE96AD5D1BF1C02286E8A1FF67181F63F203C89B7FB6CE75076308B2`). The bound helper is
  95,744 bytes with SHA-256
  `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`; every client completed
  `OPTIONS → DESCRIBE → SETUP → PLAY` and received both steady RTPv2/PT96 frame deltas. The
  artifact remains `software_only_local_rtsp` / `hardware_claim=none` and does not close aircraft,
  outdoor GNSS, HIL or REAL release gates.

### v0.3.0-pre.8 authenticated alert-link bench (2026-09-03)

- 新增 `src/multidetect/alert_link_bench.py`、`scripts/run_alert_link_bench.py` 和
  `multidetect-alert-link-bench`：复用 live patrol 的 HMAC-SHA256 UDP/关联 ACK transport，
  用全新的 `alert_id` 做有界 round-trip，记录重试、缺 ACK、丢包率及 ACK P50/P95。
- 新增 `scripts/verify_alert_link_bench.py` 与
  `multidetect-alert-link-bench-verify`：离线使用严格 JSON 重算样本关系、阈值、事件后缀和
  software/hardware 分域，拒绝回环/未解析端点被写成硬件证据；报告中的飞控和物理释放开关
  固定为 `false`。
- 新增 `configs/schemas/alert-link-bench.schema.json`，并在数据链路、现场接线和仓库架构文档中
  补充运行及归档方式。回环 bench 只证明软件 transport 子门；真实 GR01/射频、时钟同步、
  断链恢复和 HIL/REAL 仍需现场证据，最终 `v0.3.0` 发布门保持未关闭。
- E7 清单新增可选 `real_alert_link` hash-bound 记录及
  `--real-alert-link-bench` CLI 参数；装配与评估会重哈希并重跑硬件域、非回环端点和认证
  ACK 阈值，最终 release channel 强制要求通过的真实告警链路报告。
- `scripts/run_current_release_gate_pre8.ps1` 同步增加可选 `-RealAlertLinkBench` 转发，
  现场 bench 通过后无需手工拼接参数即可完成同一次 E7 装配；默认空值保持历史 prerelease
  report-only 行为。
- 加入本轮 bench/verifier 与 E7 binding 回归后，全量 Python pytest 共收集 `2605` 项，`2604 passed / 1 skipped`；
  最新完整日志为 `artifacts/evaluation/pytest-full-20260903-rtsp-multiclient-v4.log`（3,481 bytes，
  SHA-256 `1CF6757BB855F7D0F51B311D2BA7E528D210550A26EC73399A1B298A68028B69`）。
- 对当前 E7 清单执行最新 report-only 重算，产物为
  `artifacts/evaluation/release-gate-pre.8-current-20260903T103906Z-v7-alert-link-bound-report.json`
  （47,716 bytes，SHA-256 `BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）；
  因尚无真实非回环告警链路报告，结果仍为 `gate_passed=false` /
  `release_allowed=false`，不提前发布最终 `v0.3.0`。

### v0.3.0-pre.8 deterministic ONNX Runtime provider fallback (2026-09-03)

- 新增统一的 ONNX Runtime session 选择器：未显式指定 provider 时按
  `TensorRT → CUDA → CPU` 的确定顺序尝试，并且只在 session 构造失败时按后缀继续回退；
  显式 provider 列表保持严格失败，不会静默改用 CPU。
- TensorRT engine-cache 选项只对实际尝试 TensorRT 的 session 注入；`model-check` 与
  `jetson-bench` 现在记录可用 provider、每次尝试、异常摘要和最终选择，方便现场 Orin
  复核真实加速来源。
- `vision`、metric depth、appearance/vehicle ReID 和 semantic environment 共用该策略，
  新增构造失败回退、显式策略 fail-closed、TensorRT cache 选项和可用 provider 过滤回归。
- provider 变更后的 full pytest 共收集 `2581` 项，`2580 passed / 1 skipped`；日志为
  `artifacts/evaluation/pytest-full-20260903-provider-fallback-final.log`（2,916 bytes，
  SHA-256 `2CABB7F82BA1030135CC1EA1D31F5528D833EF44D6E8414669A0E63549A1F7B3`）。
- canonical parity 更新为 `configs/release-parity.json`（SHA-256
  `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
  `core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`）。
  六源 external SIM join/revalidation 已以该 parity 重新生成并独立重算通过，文件分别为
  `simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
  （SHA-256 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）和
  `simulator-external-runtime-ai-revalidation-20260903-current-provider-fallback-parity.json`
  （SHA-256 `EE0B97095738CEBB37BC3AC6435385D22B51FBD63FB4EE6645662392A4D2CC95`）。
- 该阶段的 report-only E7 绑定为
  `artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`
  （SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）及报告
  `artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound-report.json`
  （SHA-256 `F97E634AACE4F8E87D01B6880D16DA2A18689CA87B73E77299BC97DF71CA30CA`）。两者
  均保持 `gate_passed=false` / `release_allowed=false`：真实现场链路、部署域 REPLAY 精度、
  人工 Fly View/Mission、HIL/REAL 和最终 release channel 仍未满足。

### v0.3.0-pre.8 deterministic positive replay evaluator fixture (2026-09-03)

- 新增 `src/multidetect/replay_set_fixture.py` 与
  `scripts/create_replay_set_positive_fixture.py`：从已校验的六分层 raw replay 派生一个
  明确标记为 `synthetic_demo` 的正例 fixture，五个正例层逐帧包含固定 `flame`/`smoke`
  框，`no_fire` 保留为空负例，预测框、帧 ID、延迟序列和所有文件哈希均可重复生成。
- 新 fixture 会自动写出 `accuracy-report.json` 与 `fixture-summary.json`，验证 IoU、P50/P95
  延迟、正例分层覆盖、严格 JSON 和 manifest 哈希绑定；它保留
  `annotations_reviewed=false`、`deployment_domain_evidence_complete=false`、
  `accuracy_validated=false`，不会提升 REPLAY 或最终 v0.3.0 发布门。
- 新增回归覆盖正例指标通过、`no_fire` 负例、输出目录冲突和源目录边界；它只验证评估器
  接线，真实部署域数据仍需现场采集、独立标注和人工复核。
- 加入该 fixture 后的完整 Python 回归共收集 `2584` 项，`2583 passed / 1 skipped`；日志为
  `artifacts/evaluation/pytest-full-20260903-positive-replay-fixture-final.log`（2,916 bytes，
  SHA-256 `EA942313DB273CEBEE6FA40DA1D2F824E84AB864002C011A4F4C091FE6D7D77E`）。
- 修正 evaluator 报告中的临时绝对路径：fixture 现在序列化 root-relative manifest 名称，
  新增跨输出目录 58 文件逐字节确定性回归；最新 full pytest 共收集 `2585` 项，`2584 passed / 1 skipped`，
  日志为 `artifacts/evaluation/pytest-full-20260903-positive-replay-fixture-deterministic-v3-final.log`
  （SHA-256 `040FDEF824CB88E7D1BC4DAAF692644583E86DAE999B0725E1B5374608990C89`）。

### v0.3.0-pre.8 field-session, outdoor calibration and parity-bound parser hardening (2026-09-03)

- 现场会话 JSONL、现场捕获报告和室外目标真值 JSON/JSONL 现在统一调用
  `autonomy.serialization.strict_json_loads()`：接受 Windows UTF-8 BOM，拒绝重复 JSON key
  与 `NaN/Infinity`，避免后出现的字段覆盖 `DET → TRK → LCK → TGT`、捕获 provenance 或
  WGS-84 真值连接数据。
- 相机标定文档、室外深度标定文档和 JSON 样本行也通过同一严格入口读取；标定前拒绝重复
  key、非有限数字和非法 UTF-8，避免迁移到室外设备后把不确定的尺度参数当成有效距离。
- 捕获器按行将严格解析错误记录为 `invalid_json`，报告 verifier 重新打开并校验复制输出；
  旧 `gate_passed`、旧摘要和源文件追加内容都不能把篡改或不完整的现场流提升为通过。
- replay-set calibration、SIM combined/external/safety、场景矩阵、3-D control、integration
  与 zone evidence verifier 的文件入口也接入同一严格 helper，发布门所有 hash-bound 子门不再
  留下宽松 JSON 旁路。
- 新增回归覆盖现场 session loader、BOM、重复 key、非有限数字、capture source/report verifier
  以及 survey JSON/JSONL 两种输入；不触网、不登录设备，不改变现场、HIL/REAL 或最终版本门。
- 全量 Python pytest 收集 `2577` 项，`2576 passed / 1 skipped`（Windows 账户缺少 symlink
  特权的路径回归自动跳过）；完整日志为
  `artifacts/evaluation/pytest-full-20260903-depth-camera-parity-final2.log`（2,916 bytes，SHA-256
  `2A916958D996C8FF2A2E0032805CCAA282A9CFD43C9E54AABE261C5C13B88FD4`）。当前 parity 文件
  `configs/release-parity.json` SHA-256 为
  `D5A4A5321058EAFB7481D2145382A4D86F003123BA37CFB82CEBC12484A01589`，
  `core_algorithm_hash=8d3d5c815fcab4a07d1e0bbb13e0e954b12804fb22f5e38b1ef8049f45292fe2`。
  当前 report-only
  E7 报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260903T080220Z-v6-bound-report.json`
  （47,462 bytes，SHA-256
  `CFBDCEB875385144ED7723F2C267A27FFF91D5FA4A7217E273DEF26746C23D64`），门仍为
  `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 replay evidence parser hardening (2026-09-03)

- `load_replay_set_manifest()` 现在拒绝重复 JSON key 与 `NaN/Infinity`，并兼容 Windows UTF-8
  BOM；manifest 解析失败保持 `AutonomyContractError` 的具体原因，不再允许 last-key-wins 语义
  污染 hash-bound replay 证据。
- replay-set readiness 对 accuracy report 使用同样的严格解析，拒绝符号链接路径，并在读取后
  继续执行 manifest/canonical SHA-256 绑定与 fresh evaluator recomputation。新增重复 `reviewed`
  key、`NaN` 和 manifest BOM 回归；不触网、不启动设备，不改变 SIM/现场/HIL/REAL pending 状态。
- IoU evaluator 读取 `ground-truth.jsonl` / `predictions.jsonl` 时同样拒绝重复 key 与
  `NaN/Infinity`，并兼容 UTF-8 BOM；新增 JSONL 回归，防止 hash-bound 标签或预测流被宽松解析
  语义污染。
- replay-set manifest、recording、truth、prediction 引用链和 accuracy report 路径现在拒绝符号
  链接，防止路径别名改变证据来源；新增路径回归（在不支持符号链接的 Windows 账户上自动跳过）。
- release-gate、release-mode-evidence 和 release-gate-manifest 的通用 JSON 入口也统一拒绝重复
  key 与 `NaN/Infinity`，simulator-safety 绑定读取复用同一解析器；模式证据、校准报告、目标
  精度和三维安全证据不会因宽松 JSON 解析出现字段覆盖。
- 全量 Python pytest 收集 `2563` 项，`2562 passed / 1 skipped`（Windows 账户缺少 symlink
  特权的路径回归自动跳过）；完整运行日志为
  `artifacts/evaluation/pytest-full-20260903-release-json-strict.log`（19,550 bytes，SHA-256
  `B8AEE0BB93FED04EF6F3CD2C21CDBA8137A51BA94943BCD0A8D51418E7145755`）；当前 report-only
  E7 清单
  `release-gate-pre.8-current-20260903T064950Z-v6-bound.json` SHA-256 为
  `138C5A6997B10742BF20567D983D0196A76E40FC0511C441E1AD836BFB5FF585`，报告 SHA-256 为
  `06AF4EE65A3B67A7A09CC549A9A2A548E0FF1162BCACC9D33EBCF369D1D3822A`，门仍为
  `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 field-link report parser hardening (2026-09-03)

- `verify_field_link_report()` 现在以拒绝重复 JSON key 和 `NaN/Infinity` 的严格解析器重开
  主现场预检报告，并拒绝符号链接路径；UTF-8 BOM 继续兼容。恶意或意外的后出现字段不能
  覆盖 `hardware_link_ready`、probe、route 或接口绑定摘要，异常输入保持 fail-closed。
- 新增回归覆盖重复 `summary` key 和 `NaN` 常量；现场报告、接口诊断与其 hash-bound 绑定
  均继续通过现有只读 verifier。该收口不触网、不登录、不改变现场 `offline_pending`、HIL/REAL
  或最终 `v0.3.0` 发布状态。
- 全量 Python pytest 收集 `2555` 项并全部通过；E7 report-only 复算报告
  `release-gate-pre.8-current-20260903T062700Z-v6-bound-report.json` 为 47,441 bytes，
  SHA-256 `B6D5C4048316F7B7D56556AAE7324572FCAEFD5916B1A04585B0D3DDA7D9CE4B`，仍保留
  `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 portable SIM evidence paths (2026-09-03)

- 修复 deterministic SIM 证据的迁移不稳定性：`world_origin.world_path`、矩阵 `manifest` 和
  `scenario_directory` 现在使用仓库根相对 POSIX 路径；绝对路径只保留在运行时文件访问层。
  等价 checkout 回归证明同一场景的 `evidence_hash` 和三机型矩阵 `sha256` 与盘符/临时目录
  无关。
- 新矩阵为
  `artifacts/evaluation/simulator-scenario-matrix-20260903-portable-paths-pre8.json`（13,512
  bytes，文件 SHA-256 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`，
  canonical SHA-256 `CE87333B43B3D6E5964C031E27AE57EA3F682787EB54FE2F202DCD5FAD1FF95E`），
  独立 verifier 退出码 `0`；三机型 9 个故障事件全部触发且重复 hash 一致。
- 路径收口后的全量 Python pytest 收集 `2549` 项并全部通过；PowerShell wrapper 解析、
  `compileall` 与 `ruff` 也通过。
- E7 report-only 重新绑定该矩阵：清单
  `release-gate-pre.8-current-20260903T054500Z-v6-bound.json`（17,699 bytes，SHA-256
  `6BED228D980FE08248F764DE3DDCFAA3B0D31D560939DD5270BFFB6BA6218B99`），报告（46,311 bytes，
  SHA-256 `21A634283F817179499C76A44CE97AB1CAFAD77BBB23031F8581DBFF4DC53FD4`）。矩阵子门
  通过；总 E7 仍保持 `gate_passed=false` / `release_allowed=false`，现场、REPLAY accuracy、
  人工 Fly View 和 HIL/REAL 阻断不变。

### v0.3.0-pre.8 field-link interface binding (2026-09-03)

- `run_field_link_preflight.py` 新增 `--interface-diagnostic` 与 `--interface-alias`；Windows
  wrapper 同步暴露 `-InterfaceDiagnostic`，把迁移后只读网卡快照的 SHA-256、字节数、CIDR、
  候选接口和显式 alias 写入同一份预检报告。
- `verify_field_link_interface_diagnostic()` 与 `verify_field_link_report()` 会离线重开诊断
  文件、拒绝符号链接/重复 JSON key、重算 hash 并逐字段比对；源文件漂移、CIDR 或候选
  alias 不一致都会保持 `gate_passed=false`。
- `selection.status=missing|ambiguous` 仍是可验证的诊断状态，不会升级
  `hardware_link_ready`，也不代表 GNSS、传感器、执行器、HIL 或 REAL 已通过。
- 新增现场绑定回归覆盖：有效 missing 快照、alias/CIDR 漂移、源文件 hash 变化和 CLI 绑定。
- 本轮全量 Python 回归收集 `2554` 项并全部通过；`ruff check src scripts tests`、`compileall`
  与 PowerShell 语法解析也通过。

本机绑定 smoke 产物为
`artifacts/evaluation/field-link-preflight-interface-binding-smoke-20260903.json`（4,463 bytes，
SHA-256 `E96AD6241C976F85E9FCE1CFE07DCD7A6A73ADEEA0CD0B2817C09A15357278D`）；接口诊断子门
通过，但端点/路由未接通，`hardware_link_ready=false`，不改变现场与最终发布门状态。

默认现场输入已切换为
`artifacts/evaluation/field-link-preflight-20260903-interface-bound.json`（6,648 bytes，SHA-256
`140054B79FD9F74008EDA8551E5EC9109B04688520F60BD2092D2E6F0E5EFDE0`）。使用该报告重算的
E7 report-only 清单为
`release-gate-pre.8-current-20260903T061300Z-v6-bound.json`（17,699 bytes，SHA-256
`138C5A6997B10742BF20567D983D0196A76E40FC0511C441E1AD836BFB5FF585`），对应报告为 47,412
bytes、SHA-256 `427AA13F1F2FB0C552A6B4CDACCB70164989B01F6D05479407E13526E182E0C6`；接口
诊断绑定复核通过，E7 仍因现场端点未接通保持 `gate_passed=false`。

### v0.3.0-pre.8 deterministic SIM fault-coverage rerun (2026-09-03)

- 使用 `scripts/run_simulator_scenario_matrix.py --cover-faults --fault-tail 1` 重新执行
  fixed-wing、VTOL、multirotor 三机型场景；公共预算从 `120` 帧自动扩展到 `2,611` 帧，
  因此覆盖所有声明的有限时长故障，而不是只验证 nominal-only 短跑。
- 当前证据为
  `artifacts/evaluation/simulator-scenario-matrix-20260903-fault-coverage-pre8.json`
  （13,512 bytes，SHA-256
  `026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）。
  `fault_coverage_required=true`、`fault_coverage_auto_extended=true`，三种机型的
  `fault_coverage.coverage_ratio=1.0`、`all_declared_faults_exercised=true`，重复运行的
  `evidence_hash` 保持一致；独立 `verify_simulator_scenario_matrix.py`（传入
  `--expected-release-label 0.3.0-pre.8`）退出码为 `0`。
- 该文件与当前 WGS-84 严格矩阵内容字节一致，只是本轮故障覆盖运行的独立 provenance
  别名；它仍是 `truth_free` / software-only SIM 证据，不提升外部 Gazebo、人工 QGC、
  室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 E7 显式绑定故障覆盖矩阵（2026-09-03）

- 使用 `scripts/run_current_release_gate_pre8.ps1` 显式传入上述故障覆盖矩阵，生成新的
  report-only 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260903T133500Z-v6-bound.json`
  （17,699 bytes，SHA-256
  `52208E7C3EAA781369C304B1525EFA8D1FD2CEC99F2A1AE28A05B5D538656E42`）和报告
  `release-gate-pre.8-current-20260903T133500Z-v6-bound-report.json`
  （46,311 bytes，SHA-256
  `3A9182A4E9576FF81FD248859C552DD4A5F971A996E667497B5FB0DEDC257780`）。
- E7 清单中的 `simulator_scenario_matrix` 已重新绑定
  `simulator-scenario-matrix-20260903-fault-coverage-pre8.json`，矩阵 verifier exit code
  为 `0`、`gate_passed=true`、`deterministic_gate_passed=true`；E7 聚合结果仍按
  `report-only` 返回 `gate_passed=false` / `release_allowed=false`，阻断项仍是
  REPLAY accuracy、现场链路、人工 Fly View、HIL/REAL 和最终 release channel。

### v0.3.0-pre.8 field-link interface diagnostic (2026-09-03)

- 新增 `scripts/inspect_field_link_interfaces.ps1`：迁移电脑或重新接线后，以只读方式枚举物理
  网卡（包括断开接口）、IPv4 地址和目标飞机网段的直连 route，给出
  `selection.status=ready|ambiguous|missing`，不修改 IP、route、DNS，也不自动改写设备端点。
- 台式机当前快照为
  `artifacts/evaluation/field-link-interface-diagnostic-20260903.json`（4,371 bytes，SHA-256
  `230598F40AC192A34E3C3094B527D33B035DFDC2D7EBE7C148FA47CC36055999`）。结果为
  `selection.status=missing`：`Ethernet` 仅有 `192.168.1.13/24`，目标
  `192.168.144.0/24` 没有候选接口或直连 route；该报告只记录网卡层阻断，不提升硬件/GNSS
  或 REAL 状态。
- 2026-09-03 15:00（本地时间）完成一次新的只读复核：
  `artifacts/evaluation/field-link-interface-diagnostic-20260903-150022.json`（4,378 bytes，
  SHA-256 `472E36A2D912D5B95F9B975B1F1025E35ACEF475D706C0CF71F6076850F8B628`）。结果仍为
  `selection.status=missing`，`Ethernet` 仍是 `192.168.1.13/24` 且无目标网段直连 route；
  该复核尚未重新绑定 E7，保持为现场网卡状态证据。
- 新增静态回归 `tests/test_field_link_interface_diagnostic_script.py`，锁定只读行为、CIDR
  归一化和结构化字段；迁移后的直接全量回归收集 `2546` 项并全部通过（`pytest` 退出码
  `0`）。

### v0.3.0-pre.8 checkout bootstrap compatibility (2026-09-03)

- 验收脚本 `run_actuator_safety_acceptance.py`、`run_fixed_wing_software_acceptance.py` 及
  `verify_actuator_safety_evidence.py` 现在与其他仓库脚本统一：从源码 checkout 直接执行时会
  自动定位相邻的 `src/`，不要求先安装 editable package 或手动设置 `PYTHONPATH`。
- 新增 `tests/conftest.py` 作为旧版 pytest 的 collection fallback；台式机上的全局 pytest
  `6.2.5` 即使不识别 `pyproject.toml` 的 `pythonpath` 选项，也能收集并运行完整测试。
- 迁移后复核：整套 Python 回归收集 `2546` 项、退出码 `0`；Windows Release/portable
  增量构建退出码 `0`，主 EXE SHA-256 继续为
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`。

### v0.3.0-pre.8 navigation GPS quality floor (2026-09-03)

- `NavigationFusionConfig` 现在直接复用 `mavlink_gps.py` 的
  `MAVLINK_QUALIFIED_FIX_TYPE_MIN` / `MAVLINK_QUALIFIED_SATELLITES_MIN`；速度融合默认要求
  3-D fix 和至少 6 颗已知卫星，配置只能收紧质量门，不能用 2-D/unknown GPS 伪装成户外导航源。
- 速度一致性路径保留独立的 `GPS age <= 0.75 s` 严格窗口；它比目标落图的 canonical `1.5 s`
  坐标窗口更紧，不改变目标定位门，也不产生飞控指令。新增回归验证默认阈值与放宽配置均 fail-closed。
- Jetson 窄传输部署清单同步加入 `mavlink_gps.py` 与 `navigation_fusion.py`，并在远端 stage
  校验 canonical 常量和不可放宽的速度融合下限；部署仍保持原子备份、哈希校验和失败回滚。
- 该变更重新生成当前 `configs/release-parity.json`：文件 SHA-256 为
  `F099537C3FE5FB604599DD8760C2B472ABD61DE7533EAFF4BA904E6B4D9E9FC2`，
  `core_algorithm_hash=aca794c00666e69fc47c204775f94f0b74fe50da29ef3351b49d0e3dd5fbf375`。
  六源 external SIM join 与独立 revalidation 以新 parity 重算均通过：
  `simulator-external-runtime-ai-evidence-20260903-navigation-gps-floor.json`
  （5,365 bytes，SHA-256 `8B4F85F87B386CCFEBC1584045FDEDA94A71489745D3D0E5D8E3241254E70D15`）和
  `simulator-external-runtime-ai-revalidation-20260903-navigation-gps-floor.json`
  （546 bytes，SHA-256 `697D4993159B5536E39604298EA19611688FDA9D632096BEAF49BE68EE69F479`）。
- 完整 goal acceptance 新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-navigation-gps-floor.json`
  （57,678 bytes，SHA-256 `CB1CE2EC05DADA49E2666E62720D71C47ECC7AA77835C264F0AA090A54D57638`，
  核心测试收集 `2538`，`software_status=pass`）；E7 report-only 清单为
  `release-gate-pre.8-current-20260903T123000Z-v6-bound.json`
  （17,686 bytes，SHA-256 `5BD9E2800DC4DC2C4440880347AD2C7A5D95A915B03FBFC274CED8B6CE9470D3`），
  报告为 46,300 bytes、SHA-256
  `D66D22FEF31E29BCF9347D9AC3AF4DB41E14AC782D1AB0EE952C479B0F6BBD07`；E7 仍明确
  `gate_passed=false` / `release_allowed=false`，因为现场链路、REPLAY accuracy、HIL/REAL
  和人工 Fly View 尚未完成。
- `run_current_release_gate_pre8.ps1` 的无参数默认输入已切换到上述 navigation-GPS-floor
  external join/revalidation，避免 parity 更新后误绑旧快照；旧 bearing-uncertainty 文件继续
  作为历史 provenance 保留。

### v0.3.0-pre.8 replay positive-strata and raw NavSat closure (2026-09-03)

- REPLAY accuracy evaluator 新增固定正向分层契约：`day`、`night`、`haze`、`reflection`、
  `smoke_only` 每层都必须同时出现独立复核的真值对象和 pinned detector 预测；`no_fire` 保持
  负例控制用途，不计入正例分母。报告新增 `positive_strata`、`positive_stratum_coverage`、
  `positive_strata_coverage_passed`，readiness 新增逐层 truth/prediction checks。
- E7 release-gate writer 与 evaluator 对声称通过的 accuracy report 执行同一分层列表、字段和
  非零计数校验；缺字段、零计数、篡改或只在单一条件有正例都会 fail-closed。
- SIM/Gazebo 空基线的新报告为
  `artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260903-positive-strata.json`
  （SHA-256 `7DE80E0E43D669A3D8DD0D0A4D625CBDCF86B23C25B7580C2ABDEBF0848500CF`）和
  `artifacts/evaluation/replay-set-readiness-20260903-positive-strata.json`
  （SHA-256 `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`）；两者均
  `gate_passed=false`，因为当前来源仍是 `synthetic_demo` 且五层正向内容为空。
- `create_gazebo_raw_replay_fixture.py` 对 NavSat 纬度/经度/海拔做 WGS-84 域校验；越界帧计入
  `invalid_navsat_count`，输出为 `position_healthy=false`，不会把非法坐标提升为定位点。对应
  回归覆盖极点、越界经度和异常高程。
- 当前 E7 wrapper 已切换到新格式 accuracy report，并生成独立 report-only 绑定：
  `artifacts/evaluation/release-gate-pre.8-current-20260903T123000Z-v6-bound.json`
  （SHA-256 `5BD9E2800DC4DC2C4440880347AD2C7A5D95A915B03FBFC274CED8B6CE9470D3`）及
  `...-report.json`（SHA-256 `D66D22FEF31E29BCF9347D9AC3AF4DB41E14AC782D1AB0EE952C479B0F6BBD07`）。
  wrapper 返回 `0` 仅表示 report-only 执行完成；`gate_passed=false` / `release_allowed=false`
  仍由合成 replay-set、现场链路、HIL/REAL 和最终 release channel 阻断。

### v0.3.0-pre.8 positive-strata total acceptance rebinding (2026-09-03)

- 用新的五层正向覆盖 accuracy report 重新执行独立总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-positive-strata-v1.json`
  （52,222 bytes，SHA-256
  `ADF74A58DCF7804C3DA4D0C02A8DBFC878F06B450BBF2660E0601D06E2C2760D`）。核心测试收集
  `2544`、QGC custom `66/66`、`software_status=pass`、`hardware_status=offline_pending`。
- 绑定的 readiness 为
  `artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-positive-strata-v1.json`
  （13,246 bytes，SHA-256
  `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`），五个正向分层的
  truth/prediction 计数均为零，故 `gate_passed=false`；这确认了新的分层门确实进入总验收，
  而不是只停留在单独 evaluator。
- external SIM 重验证输出为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-positive-strata-v1.json`
  （SHA-256 `084B08A7B72F7651C22C8E6FA25B1802ADB761FBC8186C071F713B0865821457`）。该子门和
  三机型确定性控制继续通过，但现场链路、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 仍未完成。
- 最新现场 continuation 预检为
  `artifacts/evaluation/field-link-preflight-20260903-continuation.json`（16,803 bytes，
  SHA-256 `98E3D9BA1ECEBB6D41E61CBB54B6DE1E0B648BC86D4554C683E4D1526D2EFF98`）。Ethernet
  仍为 `192.168.1.13/24`，不匹配期望的 `192.168.144.0/24`；Jetson/GR01/RTSP 三个端点
  连续超时，`hardware_link_ready=false`，因此 E7 继续保持 report-only 阻断。

### v0.3.0-pre.8 line-of-sight uncertainty closure (2026-09-03)

- 多模态测距现在把相机/姿态/安装偏差/目标像素的一阶视线扰动同时传播到
  `ground_range_ci95_m`；直接测距路径不再只用斜距 `σ` 乘水平分量，避免室外落图的
  `1σ` 圆被系统性压窄。
- 固定翼 roll/pitch 通过 NED 射线端点的中心差分进入绝对方位 `bearing_sigma_deg`；目标落图
  再以 WGS-84 地面弧长 `r·σθ(rad)` 转换水平误差，替代会低估宽方位门的 chord 近似。
- 统一复用 nominal NED ray 与一组 one-sigma perturbation endpoints。任一扰动穿过地平线、
  或因非法校准/像素不确定度无法生成端点时，`solve()`/`solve_direct()` 都返回明确的
  `INVALID` 原因，不再跳过该项导数后继续发布看似精确的距离。
- 新增近地平线 fail-closed、直接测距 CI 随视线不确定度扩张的回归；将
  `src/multidetect/multimodal_ranging.py` 纳入 `core_algorithm` parity，当前
  `core_algorithm_hash=338981686f2142144f438ce34b4f91a18f6cab76b9f3d8efd238ac1e3fd525e7`；
  当前 `configs/release-parity.json` 文件 SHA-256 为
  `CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`。
- 该收口只改变共享测距与证据不确定度计算，不产生飞控指令；现场 GNSS、真实 Jetson/相机、
  HIL/REAL、室外真值和最终 `v0.3.0` 发布门继续保持原状态。

- 室外目标落图新增可选的时间对齐：生产者同时带上 GPS 观测时刻、目标帧时刻、N/E 速度和速度
  观测时刻时，先在 WGS-84 椭球上推进/回推飞机参考点，再叠加目标偏移；对齐窗口以及速度样本
  距离都复用 `GPS age≤1.5 s`，默认速度模型误差 `0.5 m/s` 乘以时间差并入水平 `1σ`。时间补偿
  路径若收到部分或非有限 N/E 速度样本会直接 fail-closed，避免把旧 GPS 点当作零运动且不增加误差；
  完全缺少这些可选字段的旧回放仍保持兼容路径，不做猜测。

### v0.3.0-pre.8 bearing-uncertainty external SIM + live-AI revalidation (2026-09-03)

- 在台式机 Docker Desktop 上重新执行 VTOL、fixed-wing、multirotor 三个独立 PX4/Gazebo
  runtime 和三个 `--exercise-arm --exercise-target` live-AI source。六份 source 均使用同一
  digest-pinned image，runtime/telemetry/cleanup gate、AI policy、target policy 与 target
  exercise 全部通过；每个 AI run `ai_steps=2`，GPS `fix_type=3`、`satellites_visible=10`。
- 六源 join：
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-bearing-uncertainty.json`
  （5,365 bytes，SHA-256
  `403785A43DACE03E2E9C029EBB6FE060E382001ABC32C59CD9987C43D2FFBB36`）；独立 revalidation：
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-bearing-uncertainty-acceptance.json`
  （545 bytes，SHA-256
  `9DAC947BE5A24AEB1722677B51880D6F9EC8513CCC3F784B9D1684272CC4210B`）。两者均为
  `source_recomputed=true` / `gate_passed=true`，并绑定 parity SHA-256
  `CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`。
- 最新严格总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
  （45,571 bytes，SHA-256
  `04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`），核心测试
  `2536`、QGC custom `66/66`、`software_status=pass`、credentials
  `ready=true/gate_overridden=false`。该快照补入最终 release-label parser 回归，
  并显式绑定当前六源 external evidence（`provided=true`、`source_recomputed=true`、
  `gate_passed=true`）；证据仍属于 software-only SIM；真实硬件、室外 GNSS、HIL/REAL、
  视觉精度和人工 Fly View 不在声明范围，最终 `v0.3.0` 继续保持 prerelease 阻断。
  此前 19,357-byte 同名快照作为历史 provenance 保留。

### v0.3.0-pre.8 E7 six-source external binding (2026-09-03)

- E7 `release-gate` writer/evaluator 现在将上述六源 external runtime/live-AI join、独立
  `--join` revalidation、六个 source 的 SHA-256/字节数、五组 parity hash 和 13 项检查作为
  顶层 `simulator_external_evidence` 绑定；写入与读取都会重新计算 source，路径复用、摘要
  漂移、伪造 gate 或 parity 不一致均 fail-closed。
- external `release_status` parser 同时接受 `v0.3.0-pre.N` 与最终 `v0.3.0` 标签；这只是
  最终 source evidence 的解析兼容，最终 release 仍要求 product channel、parity、六源重算
  以及 REPLAY/SIM/HIL/REAL 全部发布门通过。
- 当前 manifest：
  `artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
  （17,696 bytes，SHA-256
  `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`）；report-only：
  `artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound-report.json`
  （46,307 bytes，SHA-256
  `C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`）。E7 总门仍为
  `gate_passed=false` / `release_allowed=false`，现场、Replay accuracy、HIL/REAL 和人工
  Fly View 尚未完成；最终 `v0.3.0` 仍需新的最终标签 source evidence。

### v0.3.0-pre.8 desktop field discovery provenance (2026-09-03)

- 台式机迁移后新增一次有界只读 `192.168.1.0/24` 服务发现，并追加 SSH banner、被动 MAVLink
  帧和 RTSP `OPTIONS` 检查：扫描 `254` 个地址，发现 `4` 个普通 SSH 响应主机，但
  `5760/MAVLink` 与 `554/RTSP` 均为 `0`。最新报告
  `artifacts/evaluation/field-link-discovery-20260903-093500-protocol.json` 为 8,436 bytes，SHA-256
  `E10B84D77128E4C0FE64FB41C9805D5757C7F5F5832086C451ABB4B653193EC4`；
  `verify_field_link_preflight.py --allow-unready` 退出 `0`，`hardware_link_ready=false`。
- 现场发现只进入诊断证据，不自动选择 Jetson/GR01/相机端点，也不发送 MAVLink、RTSP 或控制帧。
  `run_current_release_gate_pre8.ps1` 的默认 external revalidation 已固定到最新
  `...bearing-uncertainty-acceptance.json`，现场 link 输入也固定到最新
  `field-link-preflight-20260903-desktop-recheck.json`（SHA-256
  `3C6BF75D5D352129A77212FB284400E396EF36282317BC5C9F14F982FF96830E`），避免后续无参数复核
  重新绑定较早的同内容历史快照；该预检仍明确 `hardware_link_ready=false`。

### v0.3.0-pre.8 wrapper/build recheck (2026-09-03)

- 从仓库根目录使用默认输入（仅显式传入唯一 UTC stamp 防止覆盖历史文件）执行
  `scripts/run_current_release_gate_pre8.ps1` 并生成新的 report-only
  清单：`artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
  （17,696 bytes，SHA-256 `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`）及
  报告（46,307 bytes，SHA-256
  `C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`）。脚本退出码 `0`，
  但 `gate_passed=false` / `release_allowed=false`，没有提前关闭 Replay-set、现场、HIL/REAL
  或最终 release 门。
- 已有 `v0.3.0-pre.8` Release/portable 主 EXE 重新核对为 `46,575,616` bytes、SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；portable
  `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output`
  退出码 `0`，日志未出现 QML 初始化失败或崩溃。全量 Python/QGC custom 回归、compileall 与
  ruff 均通过；全局 `git diff --check` 受脏工作树中既有 CRLF/尾随空白噪声影响，未把它记为通过。
  本轮没有调用 computer-use。

### v0.3.0-pre.8 partial-velocity motion guard (2026-09-03)

- `target_geolocation_from_ned_offset()` 现在拒绝时间差非零时携带的部分/非有限 N/E 速度对，
  不再静默发布未补偿且未扩张运动误差预算的坐标；完整速度对继续按 WGS-84 有界推进/回推，
  完全没有速度输入的旧低层调用仍保持历史兼容。
- 新增直接投影和 operator-status fail-closed 回归；最新总验收为
  [`multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json)
  （44,935 bytes，SHA-256
  `FBB3637449BD68013325B3840DAF7FC20414836EA65FC1B11CFBCBC64FA8DF01`），核心测试收集
  `2529`、QGC custom `66/66`、`software_status=pass`。
- 同一 parity 重新绑定 E7：
  `artifacts/evaluation/release-gate-pre.8-current-20260902T233717Z-v6-bound.json`
  （15,971 bytes，SHA-256
  `F3BF46225EA499D2BFEBF98BDCA0DD5FF6B6A4FDC7D6C2C52080A8C41441B248`），report/strict
  （64,949 bytes）SHA-256 均为
  `101A83E9AF1CD8DBFF1E1F72DD43BB97BEE2FBBA3D42324770AE24645092AB27`；发布门仍保持
  `gate_passed=false`，因为现场、REPLAY accuracy、HIL/REAL 和人工 Fly View 尚未完成。

### v0.3.0-pre.8 motion-compensation acceptance rebind (2026-09-03)

- 用时间对齐实现和当前 `configs/release-parity.json` 重新执行严格总验收，生成
[`artifacts/evaluation/multidetect-goal-acceptance-20260903-velocity-timestamp-stale-velocity-strict.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-velocity-timestamp-stale-velocity-strict.json)
（44,935 bytes，SHA-256
`114DCC517BEAB5C9B882C55E73D08A86B358A4749E42A07E0FB3350B16E0605C`）。脚本退出码为
`0`，核心测试收集 `2527`、QGC custom `66/66`、`software_status=pass`，operator
credentials 为 `ready=true` 且 `gate_overridden=false`。
- E7 writer 随后以同一 parity 和最新现场只读 bundle 生成
[`artifacts/evaluation/release-gate-pre.8-current-20260902T232002Z-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T232002Z-v6-bound.json)
（15,971 bytes，SHA-256
`9C9D918A947F7AB13FEA1108921DF315B50D40327E65EA3F71007AA6CE83C7DE`）；对应
report/strict（64,949 bytes）SHA-256 均为
`AE2DDB51FF1C23C9B2CCE5B5AA4736346DDCDF1B59771032E9398A1F2D1364E6`。严格门仍为
  `gate_passed=false` / `release_allowed=false`，现场链路、Replay-set accuracy、HIL/REAL、
  人工 Fly View 和 prerelease 版本门保持阻断；本轮没有提前发布最终 `v0.3.0`。

### v0.3.0-pre.8 line-of-sight acceptance rebind (2026-09-03)

- 用更新后的测距核心重新执行 `run_goal_acceptance.ps1`，生成
  [`artifacts/evaluation/multidetect-goal-acceptance-20260903-los-uncertainty-strict.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-los-uncertainty-strict.json)
  （44,935 bytes，SHA-256
  `D8916CC0737F90117238DCB9AE610E4CB22F94CF06C5278700760907DA5061A5`）。软件门为
  `pass`，核心测试收集 `2519`、QGC custom `66/66`，operator credentials 为
  `ready=true` 且 `gate_overridden=false`；硬件仍为 `offline_pending`。
- 同一 parity 重新执行 E7 writer，生成
  `artifacts/evaluation/release-gate-pre.8-current-20260902T224050Z-v6-bound.json`
  （15,971 bytes，SHA-256
  `4BB67B9826D78B35E9EC1213D0E83A09C080677BCB55DBB73AC04742AB5D1815`）及其
  report/strict（64,949 bytes，SHA-256
  `4C4956C2FFD09187C3BA4C6578CEFAC5B979072353229E3AB28CE3A4DCCA506B`）。`gate_passed=false`
  / `release_allowed=false` 是预期结果，Replay-set、现场链路、HIL/REAL、人工 Fly View
  和 prerelease 版本门仍阻断最终发布。

### v0.3.0-pre.8 geodetic-domain convergence (2026-09-03)

- 收口 SIM 入口、Gazebo SDF/wire、perception bridge 和 WGS-84 目标投影的纬度域：
  纬度统一严格位于 `(-90, 90)`，经度继续允许 `[-180, 180]`；精确极点在配置、
  世界原点读取、传感器帧发送/接收和目标投影前 fail-closed，避免先落盘后在地图阶段才失败。
- `SimulatorScenarioRunnerConfig` 现在在构造期拒绝 `±90°`，不再把错误延迟到
  `FlightModelConfig`；Gazebo v1 wire producer/consumer 和 perception bridge 对相同边界给出
  一致结果。
- 重新生成 `configs/release-parity.json`，当前 `core_algorithm_hash` 为
  `3cde43180e906d88aca1c0ebb9d75983ab13be9f6e2db12db44428dc08aead48`；core 清单新增
  `src/multidetect/simulator/flight_model.py` 和 `src/multidetect/multimodal_ranging.py`，
  使 SIM geodetic endpoint、目标投影和测距不确定度一起受 parity 绑定。
- `DeterministicFlightModel` 的 SIM telemetry endpoint 改用同一
  `wgs84_forward_from_ned()` 椭球直解，移除高纬度/跨日期变更线场景中的固定
  `111,320 m/degree` equirectangular 近似；极点穿越在 geodesy 入口 fail-closed。
- 新增极点回归覆盖 runner、世界 SDF、Gazebo sensor/backend wire 和 perception bridge；
  聚焦 geolocation/SIM/Gazebo 回归全部通过。该收口不改变现场 GNSS、HIL/REAL 或最终
  `v0.3.0` 发布门状态。

### v0.3.0-pre.8 strict goal acceptance and E7 rebind (2026-09-03)

- 使用当前 parity、严格 operator credential 模式和 WGS-84 三机型场景矩阵重跑总验收：
  [`artifacts/evaluation/multidetect-goal-acceptance-20260903-wgs84-geodetic-strict.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-wgs84-geodetic-strict.json)
  （56,577 bytes，SHA-256
  `E322E7A7CB8C81EED2DE49D89281261B31BFC4EB1F06CA9AA0B286A4CFDA9D9A`）。脚本退出码为
  `0`，核心测试收集 `2516`、QGC custom `66/66`、`software_status=pass`，凭据
  `ready=true` 且 `gate_overridden=false`；external runtime/AI、scenario matrix、
  三机型 3-D 控制和 PX4 Mission transaction 子门均通过。
- 新的 truth-free/software-only 场景矩阵为
  [`artifacts/evaluation/simulator-scenario-matrix-20260903-wgs84-strict.json`](artifacts/evaluation/simulator-scenario-matrix-20260903-wgs84-strict.json)
  （13,512 bytes，SHA-256
  `026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）；三机型、故障
  覆盖、重复运行和共享 WGS-84 endpoint 校验均通过，不代表真实飞行或室外 GNSS 精度。
- E7 writer 用同一 parity 和最新现场只读 bundle 重新生成
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound.json)
  （16,077 bytes，SHA-256
  `E1108579B8D20695812112096FF12DB14E43609ADD2426EA9C01F9D45EE0EC44`），对应
  report/strict 输出为
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-report.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-report.json)
  （65,062 bytes，SHA-256
  `382EC288174553A2239675C1B437880E59220D879A00AB0177F6E80E618CFC21`）；严格输出
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-strict.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-strict.json)
  与 report-only 字节和 SHA-256 相同。E7 仍为
  `gate_passed=false`、`release_allowed=false`，阻断项是 Replay-set、现场链路、HIL/REAL
  和人工验收；本次只更新 provenance/hash 绑定，不提前发布最终 `v0.3.0`。

### v0.3.0-pre.8 field evidence bundle join (2026-09-03)

- 新增 `src/multidetect/field_evidence_bundle.py`、
  `scripts/check_field_evidence_bundle.py` 和
  `configs/schemas/field-evidence-bundle.schema.json`，将现场链路、receive-only raw
  GNSS、严格 `DET → TRK → LCK → TGT` 会话、目标事件媒体、raw replay telemetry 时间轴和
  独立 WGS-84 精度报告做六层 hash-bound 只读复核。
- 检查器逐文件重算 SHA-256/字节数，拒绝 stale GPS/RANGE、sidecar 与 raw recording 漂移、
  未来或无前置 telemetry、跨会话/跨目标绑定和 scenario-bound recording 混入默认 REAL
  域；`--allow-sim-domain` 只用于 SIM 诊断，`--report-only` 只记录待办不改变门状态。
- E7 release-gate 新增可选 `--field-evidence-bundle` 绑定：写入时和每次读取清单时都
  重新打开七个输入、复算六层摘要并检查 SHA-256/字节数；预发布可保存失败待办，最终
  `v0.3.0` 要求真实域六层全部通过，防止复制 `gate_passed` 或旧摘要绕过发布门。
- 修复待机报告重验证：即使输入文件尚未生成，也保留报告中记录的未来绝对路径参与重算，
  因此合法的 `gate_passed=false` bring-up 报告不会被误报为摘要篡改；文件后来出现时，
  旧报告会因 `present`/哈希变化而 fail-closed，必须重新生成 bundle。
- 报告和 schema 固定 `release_eligible=false`、`hardware_acceptance=not_claimed`、
  `real_flight_acceptance=not_claimed`、`contains_flight_control=false`；新增 5 项专项
  回归覆盖缺件、完整合成 join、sidecar 篡改、CLI 和 schema fail-closed 约束。
- 同步 `docs/field-bringup.md`、`docs/release-gate.md`、`docs/repository-architecture.md`
  和 README 的现场命令与证据边界说明。该工具只提高证据连接完整性，不关闭当前 Jetson、
  室外 GNSS、Pixhawk、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 门。
- 台式机迁移后的第一份 report-only 起点为
  [`artifacts/evaluation/field-evidence-bundle-20260903-desktop-pending.json`](artifacts/evaluation/field-evidence-bundle-20260903-desktop-pending.json)
  （8,858 bytes，SHA-256
  `A551FA06762C4BE8697AB358D6109F54DCBDCDAE62FC60B2943AD4468251A57B`）；现场链路和其余
  五类真实采集仍未就绪，`gate_passed=false` 为预期结果。
- 2026-09-03 再做一次桌面只读链路复核，生成
  [`artifacts/evaluation/field-link-preflight-20260903-desktop-recheck.json`](artifacts/evaluation/field-link-preflight-20260903-desktop-recheck.json)
  （5,892 bytes，SHA-256
  `3C6BF75D5D352129A77212FB284400E396EF36282317BC5C9F14F982FF96830E`）和新的
  [`artifacts/evaluation/field-evidence-bundle-20260903-desktop-recheck.json`](artifacts/evaluation/field-evidence-bundle-20260903-desktop-recheck.json)
  （8,966 bytes，SHA-256
  `CFC201B338A2E8A11B02CF582755E4AF1F0C062F04468B3EA070A3CD29EB88FE`）。目标网段、
  Jetson/GR01/RTSP 端点仍不可达；bundle 完整性重验通过，但现场六层门继续保持关闭。
- `scripts/run_current_release_gate_pre8.ps1` 新增可选 `-FieldEvidenceBundle`，在不改变
  历史默认输入集合的前提下，将最新现场 bundle 传给 E7 manifest writer 做哈希绑定与重验。
- 本轮实际用最新桌面链路和 bundle 生成
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound.json)
  （15,978 bytes，SHA-256
  `DD14E6263DA6329754B90903658B765BFDFC64D0000BA0F8285680FE9173EC52`）及其 report-only
  结果
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound-report.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound-report.json)
  （64,963 bytes，SHA-256
  `44220D50A64C73D8A0C831896C05E011E9E9D3156FB0A19498098DB8AC367E3F8`）。绑定重验通过，
  但现场 bundle 和总体发布门仍为 `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 E7 current evidence rebind (2026-09-03)

- 以当前 `v0.3.0-pre.8` 版本清单重新生成并哈希绑定四模式 E7 清单
  [`artifacts/evaluation/release-gate-pre.8-current-20260903-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260903-v6-bound.json)
  （14,890 bytes，SHA-256
  `EA1B50B3CC039F3EE4DFA5DCFF3DFFC750F35A79B7ED334FD4A63D8880D27B9E`）。本次绑定当前
  Replay-set/calibration、现场 follow-up、SIM S4、执行器安全、三机型 scenario matrix、
  combined SIM/QGC/Mission、PX4 Mission 事务和 VTOL/fixed-wing/multirotor 3-D 控制记录；
  所有共享 parity hash 保持一致。
- 固定时间 report-only 复核为
  [`release-gate-pre.8-current-20260903-v6-bound-report.json`](artifacts/evaluation/release-gate-pre.8-current-20260903-v6-bound-report.json)
  （42,367 bytes，SHA-256
  `6291DB0052822BC348BDD9DA888960BA2C44C5C00262F11A1BD3F937788DD216`），严格检查同样
  返回非零并写出同字节的 `...-strict.json`；`release_allowed=false` 是预期结果。阻断原因
  仍为 Replay 合成数据/accuracy 未通过、现场 `192.168.144.0/24` 链路未就绪、四模式
  operator Fly View/Mission 状态未声明，以及 HIL/REAL 硬件与 real-control 未完成；没有把
  software-only SIM 或历史证据升级为真实验收。
- `docs/release-gate.md` 的复核命令改为 UTC 时间戳输出并保留 `--no-overwrite`，可重复执行
  而不会覆盖历史清单；`tests/test_goal_acceptance_script.py` 新增 E7 输入完整性回归。全仓
  Python pytest 当前收集 `2488` 项并以退出码 `0` 完成。
- 新增 [`scripts/run_current_release_gate_pre8.ps1`](scripts/run_current_release_gate_pre8.ps1)
  作为同一组显式 E7 输入的可重复入口：自动解析 canonical 版本、校验所有源文件、生成
  唯一 UTC 清单/report，并以 `-Strict` 额外写出严格报告。脚本实际 smoke run 的默认结果为
  `gate_passed=false`、`release_allowed=false`；复用同一 stamp 会直接拒绝覆盖已有文件。
  新增 PowerShell 解析/路径/输入完整性测试后，全仓 pytest 收集数为 `2491`，退出码 `0`。
- 只读现场预检再次确认本机 `Ethernet=192.168.1.13/24` 会从目标
  `192.168.144.0/24` 外选路，Jetson/GR01/相机端点均超时，保存的
  `artifacts/evaluation/field-link-preflight-20260902-200533-followup.json`（11,756 bytes，
  SHA-256 `D1E587996539374FE543D757A132CEED4329E53E0161EE373D529D279C1A9B2D`）保持
  `hardware_link_ready=false`；未修改网卡、IP、路由或飞控状态。

### v0.3.0-pre.8 goal-acceptance strict credentials re-run (2026-09-03)

- 在 external/combined provenance 已绑定的 v5 基础上去掉
  `-AllowMissingOperatorCredentials`，按用户级 operator/MAVLink key 严格模式重跑总验收，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json`
  （56,347 bytes，SHA-256
  `CD979D0C0E6DE1FC6C777D0592049DDB289DE7E3DEAB1D2D4DCDA4AA36937AAD`）。本轮
  `dirty_entry_count=430`、核心测试 `2486`、QGC custom `66/66`，脚本退出码 `0`；
  `operator_credential_readiness.ready=true` 且 `gate_overridden=false`，external/combined
  软件子门均保持 `gate_passed=true`。
- readiness 输出为
  `artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-v6.json`
  （9,379 bytes，SHA-256
  `77D65F2EF5596DAAA7135B7E91CD9927DF5EF70BF43C97CC6F64BB67D7EF6414`）；REPLAY 严格子命令
  仍返回 `2`/`gate_passed=false`，现场链路仍为 `hardware_status=offline_pending`。该严格
  快照只关闭软件凭据/外部 SIM provenance 缺口，不升级真实 Jetson、室外 GNSS、Pixhawk、
  HIL/REAL 或人工 Fly View。
- 同步 `docs/replay-set-manifest.md` 的复核命令：当前示例同时绑定 strict credentials、
  external runtime/live-AI join 和 combined SIM/QGC/Mission envelope，避免仅刷新 REPLAY
  节点时覆盖掉 v6 总验收中的其他 provenance。
- 新增 `test_replay_docs_keep_current_goal_acceptance_provenance_bound` 回归测试，锁定这组
  文档参数和 strict 约束，后续命令漂移会在 CI 直接暴露。

### v0.3.0-pre.8 goal-acceptance external/combined provenance rebind (2026-09-03)

- 在 v4 已绑定 REPLAY readiness 的基础上，重新验证并绑定当前三机型
  external runtime/live-AI join 与 hash-bound combined SIM/QGC/Mission envelope，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v5-external-bound.json`
  （56,346 bytes，SHA-256
  `A071857A582836B166FAFA4A4661A7929A36B40A53AA865B718B09E522BD7C7F`）。本轮
  `dirty_entry_count=430`、核心测试 `2486`、QGC custom `66/66`，脚本退出码 `0`；
  `simulator_external_evidence.gate_passed=true`、`source_recomputed=true`，
  `simulator_combined_acceptance.gate_passed=true`。
- external join 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json`
  （5,338 bytes，SHA-256
  `FE00BB61023738E3F3F1CEE98CA5ECA7D194A26585EFF06044140A1AEDA6FBA4`），独立重算为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-goal-v5.json`
  （543 bytes，SHA-256
  `135CCF223DEE62824522F0A06E458886F4BF3F9E490AEBFF0DB3271CB85C7F22`）。这些仍是
  software-only Docker/PX4/Gazebo 证据，不升级为真实硬件、室外 GNSS、HIL/REAL 或人工
  Fly View 通过；REPLAY readiness 仍因合成来源、未复核标注、无正例和无 accuracy report
  返回 `2`/`gate_passed=false`，现场仍为 `hardware_status=offline_pending`。

### v0.3.0-pre.8 goal-acceptance replay readiness provenance re-run (2026-09-03)

- 在补齐 `/.artifacts/`、`/.tmp-*` 临时树忽略规则并加入 provenance 回归后，按当前 SIM
  manifest、现场 follow-up 和同一 dirty worktree 重跑总验收，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v4.json`
  （48,040 bytes，SHA-256
  `521C07A4505559E97E208D44CCAA7B7356DD77B7859F44A2FFCE81D858482559`）。本轮
  `dirty_entry_count=430`、核心测试 `2486`、QGC custom `66/66`，脚本退出码 `0`。
- 同轮 readiness 输出为
  `artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-v4.json`
  （9,379 bytes，SHA-256
  `77D65F2EF5596DAAA7135B7E91CD9927DF5EF70BF43C97CC6F64BB67D7EF6414`）。严格 readiness
  子命令返回 `2`、`gate_passed=false`，明确记录六个 SIM 条目仍为 `synthetic_demo`、
  标注未独立复核、无正例/accuracy report；现场链路仍为 `hardware_status=offline_pending`。

### v0.3.0-pre.8 acceptance provenance hygiene (2026-09-03)

- `.gitignore` 现在覆盖迁移后验收工具产生的 `/.artifacts/`、`/.tmp-*` 和
  `/.tmp-pytest-*/` 临时树；保留磁盘上的既有内容，不把测试输出混入 Git provenance。
- 在同一当前 SIM manifest 和现场 follow-up 上重跑总验收，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v3.json`
  （48,040 bytes，SHA-256
  `628D4CC2D05D1FA080CCCBFFAB3560D0A58C0F85FBBBAB55785B1E8E36A9849D`）。本轮
  `dirty_entry_count=430`、核心测试 `2485`、QGC custom `66/66`，脚本退出码 `0`；
  REPLAY readiness 严格子命令仍返回 `2`/`gate_passed=false`，现场硬件仍为
  `offline_pending`。

### v0.3.0-pre.8 goal-acceptance replay-set binding (2026-09-03)

- `scripts/run_goal_acceptance.ps1` 现在支持 `-ReplaySetManifest`、可选
  `-ReplaySetAccuracyReport`/`-ReplaySetReadinessOutput` 以及 `-RequireReplaySet`；脚本会
  调用 `scripts/check_replay_set_readiness.py`，把 manifest/readiness/accuracy 的哈希、
  verification exit code、逐项 `blocking_reasons` 和 `next_actions` 写入总验收证据。
- 使用当前 SIM manifest 和最新现场 follow-up 生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness.json`
  （48,037 bytes，SHA-256
  `4E708B6843CFC26773E5EEEB5D5A3999A4428AECE5998468966E7BADE67ED650`）。本轮核心测试
  `2485`、QGC custom `66/66`、脚本退出码 `0`；嵌入 readiness 命令按严格语义返回
  `2`，因为 SIM 条目仍是 `synthetic_demo`、未完成独立标注且没有正例/accuracy report。
- 总快照新增 REPLAY 待办：必须提供六分层 `deployment_recording`、复核真值/预测和
  哈希绑定 accuracy report；该软件绑定不会把 SIM 空基线、现场断链或 prerelease channel
  升级成 HIL/REAL 或最终 `v0.3.0`。

### v0.3.0-pre.8 goal-acceptance rebind to latest field follow-up (2026-09-03)

- 用最新 `field-link-preflight-20260903-followup.json` 重新执行完整 goal-acceptance，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-followup.json`（43,370 bytes，
  SHA-256 `8E448A74D4606A681CC225CB6D106A7AFD86B692D1FA9B7FB97E1A678BECFC1A`）。快照绑定
  `field_link_preflight.verified=true`、`hardware_link_ready=false`，版本仍为
  `0.3.0-pre.8`，`software_status=pass`、核心测试 `2484`、QGC custom `66/66`。
- 本次总验收脚本退出 `0` 只表示软件/结构门完成；剩余现场门仍是 ChArUco 实采、推进器
  断开后的真实舵机与失锁、真实 DET→TRK→LCK→TGT、Jetson soak 和三机型人工 Fly View。
  新快照没有把断链现场或 prerelease channel 升级成最终 `v0.3.0`。

### v0.3.0-pre.8 bounded field-link follow-up (2026-09-03)

- 在台式机当前 `Ethernet=192.168.1.13/24` 上重新执行一轮有界、只读现场链路 watch：
  `artifacts/evaluation/field-link-preflight-20260903-followup.json`（5,895 bytes，SHA-256
  `951CAC7DD09775904F6E56CDC9D70D00C916B0D803EABBB77F040F6A0DD00BB4`）。本轮确认本地
  UDP `14580/14581` 可绑定，但 Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、
  相机 `192.168.144.108:554` 均超时，路由源地址也落在期望 `192.168.144.0/24` 之外。
- `--allow-unready` 结构 verifier 返回 `0` 只代表报告形状/哈希一致；报告仍为
  `hardware_link_ready=false`、`all_passed=false`、`stop_reason=failure`，没有升级 HIL/REAL
  或最终 `v0.3.0` 发布门。接通飞机网后必须用实际三端点执行
  `-ProtocolChecks -RequireRoute -RequireReady`。

### v0.3.0-pre.8 replay-set readiness/intake checks (2026-09-03)

- 新增 `src/multidetect/replay_set_readiness.py`、`multi-detect check-replay-set-readiness` 和
  `scripts/check_replay_set_readiness.py`。readiness 会重新验证 replay-set 的原始/规范哈希、
  六层部署来源、独立标注、预测流和正例分母；如果提供 accuracy report，还会按报告门限重跑
  evaluator 并比对关键字段，不能靠手工改写 `gate_passed` 绕过。
- 新增 `configs/schemas/replay-set-readiness.schema.json` 与回归
  `tests/test_replay_set_readiness.py`，覆盖 SIM 空基线、完整正例夹具、报告篡改和 clean-checkout
  wrapper。当前 SIM readiness 仍为 `gate_passed=false`，这是预期的 fail-closed 结果。
- 生成 `artifacts/evaluation/replay-set-readiness-20260903-pre.8.json` 作为当前六层 SIM
  收口清单；它只记录采集缺口和下一步动作，不升级为真实部署域精度或最终 `v0.3.0` 发布证据。

### v0.3.0-pre.8 field-link interface auto-selection (2026-09-03)

- `scripts/prepare_field_link.ps1` 新增可选 `-AutoSelectInterface`：只读枚举活动物理网卡，
  仅当目标飞机网段命中一个候选时选中；零候选或多候选均 fail-closed，并给出地址/候选
  诊断。提权子进程沿用已解析的明确 alias，不重复猜测。
- `scripts/run_field_link_preflight.ps1` 转发该开关，默认显式 `-InterfaceAlias Ethernet`
  路径保持兼容；预检仍不改 IP、默认路由、DNS 或任何飞控流量。
- PowerShell AST 语法检查、现场 wrapper/field-link 回归通过；当前迁移台式机实际烟测因
  只有 `192.168.1.13/24` 而无 `192.168.144.0/24` 候选，正确退出 `1`，没有升级
  `hardware_link_ready`。

### v0.3.0-pre.8 actuator safety contract (2026-09-02)

- 新增 [`src/multidetect/actuator_safety.py`](src/multidetect/actuator_safety.py)：统一有界
  语义命令到 PWM 的方向/行程映射，并将 cancel、lost-lock、watchdog neutral 和 failsafe
  后拒绝命令收口为同一不可绕过的内存状态机。
- 新增 `scripts/run_actuator_safety_acceptance.py`、
  `scripts/verify_actuator_safety_evidence.py` 和
  `configs/schemas/actuator-safety-evidence.schema.json`。当前 pre.8 证据
  `artifacts/evaluation/actuator-safety-software-acceptance-20260902-pre8.json`（2,226 bytes，
  SHA-256 `8AD952BF8EBFDDFCF716EE11F212161B2869EDFA929CFED549657C3D8F58C2CC`）的
  `gate_passed=true`，但明确 `software_only=true`、`physical_outputs_present=false`，
  不提升真实舵机/HIL/REAL 或最终发布门。
- 固定翼软件验收现在嵌入同一执行器安全证据；`run_goal_acceptance.ps1` 会独立运行并验证
  该 artifact，避免只依赖一个复制的 `gate_passed` 标志。
- E7 `release-gate` writer/evaluator/schema 现在支持顶层 `actuator_safety` 哈希绑定；绑定
  会重算 HIL/software-only 边界和八项 canonical check，最终 `release` channel 强制要求
  通过该共享执行器契约。
- 本轮全仓库回归收集 `2467` 项并全部通过（退出码 `0`）。

### v0.3.0-pre.8 strict goal acceptance refresh after actuator safety (2026-09-02)

- 重新执行 `run_goal_acceptance.ps1`，绑定六源 external runtime/live-AI、22:00 后现场预检、
  canonical ChArUco 板和独立执行器安全证据，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-actuator-safety-external-strict.json`
  （43,745 bytes，SHA-256
  `68F4B7565197E58B9346B668CE27234896D32DFA44A9E909FD03355C7B5B6C26`）。
- 快照收集 `core_tests_collected=2467`、QGC custom `66/66`，`software_status=pass`，
  external 与三机型 3-D deterministic binding 均通过；`operator_credential_readiness.ready=true`
  且 `gate_overridden=false`。新增 `actuator_safety_acceptance` 节点独立记录
  `gate_passed=true`、`software_only=true`、`physical_outputs_present=false`。
- 现场预检报告本身 `verified=true`，但 `hardware_link_ready=false`；ChArUco 实采、推进器
  断开后的真实舵机/失锁、真实 DET→TRK→LCK→TGT、Jetson soak 和人工 Fly View 仍在
  `remaining_live_gates`，最终 `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 E7 actuator safety binding (2026-09-03)

- `release_gate_manifest.py`、`release_gate.py` 与
  `configs/schemas/release-gate-manifest.schema.json` 新增顶层 `actuator_safety` 绑定。
  writer 在生成清单时执行独立 actuator verifier，记录源 SHA-256、HIL/software-only
  边界、推进器软件 fixture 标记和八项 canonical check；evaluator 每次读取都会重哈希、
  重算并比较声明结果。
- 当前绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260903-actuator-bound.json`（14,980
  bytes，SHA-256 `393A329AE46944E20035D899FCF5BD7686183B71E172B132F62137CCA1071F65`）；固定
  时间 report-only/严格报告同为 42,498 bytes、SHA-256
  `94878B1D5230D26CAC56B5049251603F7BA7787FCD4D3ADC1345A021AE2F4355`，退出码 `0`/`2`，
  `actuator_safety.present=true`、`passed=true`。
- 最终 `release` channel 现在强制要求该顶层绑定；当前 pre.8 整体仍
  `gate_passed=false`/`release_allowed=false`，真实舵机/失锁、HIL/REAL、现场链路和人工
  Fly View 等门没有被软件 bench 改写。

### v0.3.0-pre.8 field-link discovery integrity (2026-09-03)

- `src/multidetect/field_link_preflight.py` 新增无网络的
  `verify_field_link_discovery()`，并由 `verify_field_link_report()` 自动调用。它重算
  显式 IPv4 CIDR 的主机数、响应主机数、SSH/MAVLink/RTSP 服务命中数、候选地址顺序、
  端口/传输类型和逐主机 `responsive` 标志；越界地址、重复/乱序主机、篡改计数或伪造
  `hardware_link_ready` 字段都会 fail-closed。
- 保留早期 schema-v1 发现快照的可读性：缺少后来加入的 `service_candidates` 时只标记
  `warnings`，仍校验其服务计数和逐主机记录，并明确候选列表完整性在源文件中不可用。
- 新增字段发现篡改回归测试，并把当前迁移后发现证据
  `artifacts/evaluation/field-link-discovery-20260903-ethernet.json`（SHA-256
  `C1D655F4A8639FF1C1469B40BC8DBC61AA439D0607733B74047074E07D3BDCE4`）纳入离线复核。
  独立复核输出为
  `artifacts/evaluation/field-link-discovery-20260903-ethernet-verified.json`（2,378 bytes，
  SHA-256 `6C0AD7F2BCC97568A719DFADD8FA3BE4E0B596E8F0D0493FB5E60CEE4CC3FE73`，退出码 `0`）。
  该诊断仍为 `hardware_link_ready=false`，不会替代实际 Jetson/GR01/相机端点预检。

### v0.3.0-pre.8 deterministic SIM matrix refresh (2026-09-03)

- 使用当前 pre.8 源码重新运行带 `--cover-faults` 的 fixed-wing、VTOL 和 multirotor
  场景矩阵；三种机型都自动扩展到 `2,611` 帧，声明的 `9` 个故障事件覆盖率均为
  `1.0`，并通过第二次运行的 `evidence_hash` 重复性校验。
- 最新证据为
  `artifacts/evaluation/simulator-scenario-matrix-20260903-pre.8-fault-coverage.json`
  （13,512 bytes，SHA-256
  `C2A2D906263F0271A0873A1D42EAF25FA184423840DC0E050A3361E16BE6817A`）；独立
  `verify_simulator_scenario_matrix.py` 退出码为 `0`。
- 该 artifact 仍明确是 truth-free、software-only SIM 子门，不提升 external 3-D、真实
  硬件、人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布状态。

### v0.3.0-pre.8 goal acceptance rebinding to SIM matrix (2026-09-03)

- 在同一台式机工作树上重新执行 `scripts/run_goal_acceptance.ps1`，把最新故障覆盖矩阵绑定到
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-sim-matrix.json`（43,118 bytes，
  SHA-256 `0FA334AA6824A6E965DB686A0CF5E5CADAD007840EE8C182AA872B70DDB90BA5`）。脚本退出码为
  `0`，`core_tests_collected=2480`、QGC custom `66/66`，`software_status=pass`、
  `hardware_status=offline_pending`。
- 快照中的 `simulator_scenario_matrix_acceptance` 绑定
  `simulator-scenario-matrix-20260903-pre.8-fault-coverage.json`（SHA-256
  `C2A2D906263F0271A0873A1D42EAF25FA184423840DC0E050A3361E16BE6817A`），三机型/2,611 帧/9 个
  声明故障覆盖和独立 verifier 退出码 `0` 均保持一致。
- 这次是软件/SIM 证据重绑定，不改变真实现场门：ChArUco 实采、推进器断开后的舵机/失锁、真实
  DET→TRK→LCK→TGT、Jetson soak、人工 QGC Fly View 以及 HIL/REAL 仍在 `remaining_live_gates`，
  最终 `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 fresh external runtime/live-AI revalidation (2026-09-03)

- 在台式机 Docker Desktop 上重新执行 VTOL、fixed-wing、multirotor 三种 external
  PX4/Gazebo + live-AI 场景。新鲜 source 均 `gate_passed=true`、`runtime.status=passed`、
  GPS `fix_type=3/satellites_visible=10`、`ai_steps=2/ai_accepted=2`、目标 exercise 接受、
  stop/remove 清理完成；MAVLink `vehicle_type` 分别为 `22/1/2`。
- 新鲜 source 分别为
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260903-vtol.json`（SHA-256
  `D646C94C309A853641600D8AD5479E3BDF21176FA8B9B016A214987B99EC7193`）、
  `...-fixed-wing.json`（`23468BA7A59C446F7A1D5FA212D0A981A04CCB131982EC003B05013646760375`）和
  `...-multirotor.json`（`00E8428A5214383FC9D762F38B7126A973D314759A02FE6BC9EB2DD9A9106E5D`）。
- 使用旧的独立 runtime source 加上上述三份新鲜 AI source 生成 distinct 六文件 join：
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json`
  （5,338 bytes，SHA-256 `FE00BB61023738E3F3F1CEE98CA5ECA7D194A26585EFF06044140A1AEDA6FBA4`）和
  独立 revalidation `...-revalidation-20260903-final-fresh-ai-v1.json`（543 bytes，SHA-256
  `A03F66998AB62558595A52201847E927D0676C78D4F4D7E996C7C955212FAD42`）均为
  `source_recomputed=true`、`gate_passed=true`。
- 严格凭据模式的总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-evidence-strict.json`
  （43,480 bytes，SHA-256 `8971E0D996AD39A12DE2EEC822D338B4A03E70049730BE11B74AF98534AF8903`），
  收集核心测试 `2480`、QGC custom `66/66`，`software_status=pass`、
  `hardware_status=offline_pending`、external binding 通过，且 `gate_overridden=false`。
  该刷新只更新 truth-free/software-only SIM provenance，不关闭真实设备、室外 GNSS、
  人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 field-link preflight rebind (2026-09-03)

- 重新执行只读现场链路预检并绑定显式 aircraft CIDR `192.168.144.0/24`：
  `artifacts/evaluation/field-link-preflight-20260903-current.json`（6,686 bytes，SHA-256
  `8A6F3624D84FF11B94F8C65B87F2BDF5CABB7A6AF3A4B6075EAA39439A4E3798`）。本机 source
  `192.168.1.13` 不在目标网段，Jetson/GR01/RTSP 均超时，route/protocol 失败，本地 UDP
  bind 可用；报告保持 `hardware_link_ready=false`、`stop_reason=failure`。
- 严格 ready verifier 退出码 `1`，`--allow-unready` 结构/来源复核退出码 `0`，没有把失败
  诊断改写为硬件通过。新的 goal acceptance
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-field-preflight.json`
  （43,758 bytes，SHA-256 `1553655AEFCCC504F0C4111F265A36C39FAD29728523EB5ECE11729ABA3080B7`）
  仍收集核心测试 `2480`、QGC custom `66/66`，`software_status=pass`、
  `hardware_status=offline_pending`，并记录 field report 已验证但 `gate_passed=false`、
  `route_checks_passed=false`、`gate_overridden=false`。
- 这次只更新现场 provenance，飞机接通后可直接用同一端点、协议检查和 route 要求重跑；
  SIM、external runtime/live-AI、HIL/REAL 和最终 `v0.3.0` 状态不被伪造改变。

### v0.3.0-pre.8 current E7 release-gate refresh (2026-09-02)

- 重新生成当前 pre.8 E7 发布门清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source.json`
  （14,068 bytes，SHA-256 `5FCF05088BD73F03231BB8B0FDD49B74F79BC1C2849D2D4292BF98913794A8AB`），
  并通过 `configs/schemas/release-gate-manifest.schema.json` 校验。清单绑定当前
  REPLAY/SIM/HIL/REAL envelope、field-link、三机型 3-D control、SIM safety/matrix、
  combined SIM/QGC/Mission 与 PX4 airframe transaction。
- 生成 report-only 与严格复核：分别为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-report.json`
  （40,740 bytes，SHA-256 `AF5DBA49793813E3B1A52F40814CDEB2DFCE1727B246D4DAFC8BE4A89EE8281A`，退出码 `0`）
  和
  `artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-strict.json`
  （40,740 bytes，SHA-256 `568E8F34073CD31A3096C6A51DECCB70222CC462047DA32E329C7CA12DC0C441`，退出码 `2`）。
  两份报告都保持 `gate_passed=false`、`release_allowed=false`，共 31 条阻断原因；E7
  清单与 goal acceptance 的六源 external provenance 分层记录，避免把 source join 当成
  最终发布门通过。
- 固定接线前可打印的高分辨率 ChArUco 校准板资产
  `configs/calibration/charuco-7x5-40mm-20mm-4200px.png`（4200×3000，88,844 bytes，
  SHA-256 `66AAF16F10689E9EDFF68B4684128E028AD89B09AABC55AB9CDC4D02521B23AC`）。OpenCV
  自检可识别 24 个 corners；真实相机仍必须采集至少 20 个多姿态有效视图并通过完整质量门，
  该资产不提升室外标定或 REAL 状态。

### v0.3.0-pre.8 independent six-source runtime/AI provenance hardening (2026-09-02)

- 将 VTOL、fixed-wing、multirotor 的 Docker/PX4/Gazebo external runtime 与 live-AI 结果拆成
  六个独立 source，并用同一 digest-pinned image
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新计算绑定。runtime source 的 telemetry/GPS、external process 和 cleanup，AI source 的
  armed heartbeat、`ai_steps=2`、target policy 与 truth-free 边界均通过。
- verifier 现在强制 runtime/AI source 使用六个不同文件；同一路径同时作为 runtime 与 AI
  source（包括跨机型复用）会直接 fail-closed。当前 v2 六源 join 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-six-sources-independent-v2.json`
  （5,353 bytes，SHA-256 `79DBB81E4BB85366FFB7114DB2B3E9E6F921E8027133EE64603A680184B10440`），
  独立 `--join` revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2.json`
  （558 bytes，SHA-256 `95A4CC58EDE1723E51B0DEBDFFECC261B6150A5374BBA49CD6B2BDEDBFFF783A`）。
  两者均 `source_recomputed=true`、`gate_passed=true`；旧的三源/旧六源记录保留为历史证据。
- 重新执行 `run_goal_acceptance.ps1` 并在未启用凭据 override 的严格模式下绑定该 join，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json`
  （42,100 bytes，SHA-256 `F69398FDC88D14AFA4FF0D182D0F6717108904CEDD2865DC6680CA9AD4EA5EC0`），
  `core_tests_collected=2459`、QGC custom `66/66`、`software_status=pass`，external 与
  deterministic 3-D binding 均通过；goal acceptance 使用的 revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2-goal-v7.json`
  （558 bytes，SHA-256 `DD60719E8969FA5F4D62680CBD545E9EF0A6F231CC6AA8FCEBDAAC3CF8D3FBF2`）。
- 严格凭据模式下 `operator_credential_readiness.ready=true`、`gate_overridden=false`；现场
  预检仍原样保留为 `hardware_link_ready=false`。同一快照绑定 22:00 后只读现场预检
  `artifacts/evaluation/field-link-preflight-20260902-after-2200.json`
  （6,692 bytes，SHA-256 `37D68E88AB7433898BE77FED696009564F558274B7824EB0D3164D5FC563200A`）。
  本地 metadata/QGC UDP bind 通过，但 Jetson/GR01/RTSP 与期望 `192.168.144.0/24` route
  仍失败，`hardware_link_ready=false`；失败事实保持绑定，不升级为 REAL。
- `hardware_status=offline_pending`、人工 QGC Fly View、室外 GNSS、真实 Pixhawk/Jetson/相机、
  伺服/失锁、真实检测闭环、HIL/REAL 和最终 `v0.3.0` 发布门继续保持未完成；external
  runtime 不再出现在 `remaining_live_gates` 中。

### v0.3.0-pre.8 external three-airframe runtime/live-AI acceptance (2026-09-02)

- 在台式机 Docker 环境按同一 `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新执行 VTOL、fixed-wing、multirotor 三个 scenario；每个 source 的 telemetry、
  GPS quality、arm/disarm、live-AI target policy、truth-free 和 cleanup 均通过。
- 三机型 source 绑定为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-three-airframes.json`
  （5,365 bytes，SHA-256 `E1CE9408A39F9C4316EB63624DEFC3C2D590302804041A8409B7F65488761AEF`），
  独立 `--join` revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-three-airframes.json`
  （547 bytes，SHA-256 `B4981FCCE82059D951BCB22EB29EC4A6F13B1E5550895853667675FCF3C1FFC6`）；
  两者均 `source_recomputed=true`、`gate_passed=true`。
- 重新执行 `run_goal_acceptance.ps1` 并强制绑定上述 join，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v2.json`
  （41,770 bytes，SHA-256 `C34FE659F11DDC5AA94CF8B945B9018F68491A6B3FA808DBB00B7DD9C06BC46F`），
  `core_tests_collected=2457`、QGC custom `66/66`、`software_status=pass`，external 和
  deterministic 3-D binding 均通过。
- `run_goal_acceptance.ps1` 的 `remaining_live_gates` 现在按 external binding 动态收敛：
  join 已通过时只留下人工 QGC Fly View 视觉验收，不再重复列出 external runtime。
- 以上仍是 disposable/truth-free software SIM 证据；真实设备、室外 GNSS、Pixhawk/执行器、
  HIL/REAL、人工 Fly View 与最终 `v0.3.0` 发布门继续保持未完成。

### v0.3.0-pre.8 full regression environment completion (2026-09-02)

- 台式机回归环境补齐 CPU `onnxruntime=1.29.0`，并将 `protobuf` 固定为兼容现有
  mediapipe/streamlit 依赖的 `4.25.8`；原先缺少运行时的两条 ONNX/集成测试已实际执行。
- 当前完整回归命令 `pytest --disable-warnings -ra` 结果为 `2449 passed`、退出码 `0`；Ruff、
  compileall、diff-check 和 release-gate Schema 验证保持通过。原始输出日志为
  `artifacts/evaluation/pytest-full-20260902-onnxruntime.log`（2,879 bytes，SHA-256
  `4F5360DEC8E64B9C8B71CDF9BEF3ACEF7FB8C7FE87C7CD4FF7361B4B2C296012`）。该结果属于软件质量证据，
  不升级真实 Jetson/相机/室外 GNSS/Pixhawk、HIL/REAL 或人工 Fly View 发布门。
- `docs/autonomy-architecture.md` 与 `docs/repository-architecture.md` 已将当前 portable
  EXE 指纹与历史构建指纹分开标注，当前版本仍为 `v0.3.0-pre.8`。
- 重新执行 goal-acceptance 后生成当前软件快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime.json`
  （31,162 bytes，SHA-256
  `8F4DA9CDC38EF65CF069620FF70871C5806BA3246AD0CEE04CE3A7188488D27D`），其中
  `core_tests_collected=2449`、QGC custom `66`、`software_status=pass`，硬件仍为
  `offline_pending`。
- 随后在未传入 `--AllowMissingOperatorCredentials` 的严格模式下再次运行，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime-credentials.json`
  （31,175 bytes，SHA-256
  `F17D535362B2F18351178A7F9865766A3EAA3D015AC3392C2C437129C0750A52`）。该快照确认
  `operator_credential_readiness.ready=true` 且 `gate_overridden=false`，核心测试 `2449`、
  QGC custom `66`、`software_status=pass`；硬件仍为 `offline_pending`，人工 Fly View
  继续保持 `not_claimed`。

### v0.3.0-pre.8 three-airframe 3-D SIM binding (2026-09-02)

- 新增 `src/multidetect/simulator/three_d_control.py` 独立 verifier/binding：重新打开每份
  3-D evidence，校验场景 manifest、Gazebo world/model、WGS-84、stack-plan canonical
  digest、PX4 launch preflight、QGC manual/AI authority、watchdog/failsafe 和 claim 边界。
- 新增 `scripts/verify_simulator_3d_control_evidence.py` 及 `multidetect-sim-3d-control-verify`
  入口；单文件复核和 `--bind` 三机型绑定都返回机器可读 JSON，绑定要求
  `fixed-wing`、`multirotor`、`vtol` 恰好各一次。
- `release_gate.py` / `release_gate_manifest.py` / release-gate schema 新增
  `simulator_3d_control` hash-bound 子记录；最终 `v0.3.0` 要求该绑定存在且重新复核通过。
- `run_goal_acceptance.ps1` 默认重新生成并绑定三种机型的 3-D source，新增
  `-Simulator3dVtolOutput`、`-Simulator3dFixedWingOutput`、`-Simulator3dMultirotorOutput` 和
  `-RequireSimulator3dControlEvidence` 参数。
- 当前三份 source 均通过 deterministic gate：VTOL 83,212 bytes / SHA-256
  `3733B2E02F4751B5E9262451EC6C7BF73FC5EDEC94E57B6F3F4755A603ADB1F7`，fixed-wing 83,903
  bytes / `336CFABAB3CC88E4419F5FD3744890D4F789F3EAFA1C571D063F98515881C6EE`，multirotor
  83,866 bytes / `2F99FBCDE6457EE634EA65FB5D58988B90628CF38E7A74C83EB3CEA5D470EDE0`。
- 生成的 pre.8 E7 绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound.json`（13,373 bytes，
  SHA-256 `967F8C5CFE4DBF16EAFF88503BBF18C497BAEA4ECE8190DF2D5602F1D7B8D206`）。聚合仍为
  `gate_passed=false` / `release_allowed=false`；三份 source 的
  `external_runtime_gate_passed=false`、视觉/硬件 `not_claimed` 不会被升级成外部
  PX4/Gazebo、Pixhawk、室外 GNSS、HIL/REAL 或实飞验收。
- 随后用完整 `run_goal_acceptance.ps1` 实际重生成三份 latest source 并完成绑定：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-3d-binding.json`（41,392 bytes，
  SHA-256 `57F9B416A8E4FF4EC20D395F905A87DAA4290304B930EF1DA448CFBAEE8197E9`）记录
  `core_tests_collected=2456`、QGC custom `66`、`software_status=pass`、
  `hardware_status=offline_pending` 和 `simulator_3d_control_acceptance.gate_passed=true`。
  同批 source 的 E7 绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound-rerun.json`（13,328 bytes，
  SHA-256 `BA87F55307793069A04BC941E3F62586AE64476EE5BD86A27C1B52AF8CA97187`）；总发布门
  仍关闭。
- 继续执行一次带现场报告绑定的总验收，现场预检
  `artifacts/evaluation/field-link-preflight-20260902-final-continuation.json`（3,713 bytes，
  SHA-256 `476AB14F0E48887A33AA4107D86A6782F13B8A7B4334B95954681F99A3A48E52`）确认当前
  Ethernet `192.168.1.13/24`、Jetson/GR01/RTSP 不可达，`hardware_link_ready=false`。
- 最新总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-final-continuation.json`
  （41,681 bytes，SHA-256 `6F6388CA393F49FEADC6B95693A0CB7AC93959A2AA9E17236B8BC7DC7DF3AB9C`），
  核心测试 `2456`、QGC custom `66/66`、`software_status=pass`、`hardware_status=offline_pending`，
  三机型 binding `verified=true`/`gate_passed=true`。本批 VTOL/fixed-wing/multirotor source
  指纹分别为 `50B8E18B1A7B7D85CF4D37AF48BD5DB1B51534C57F709B864E2FB8827A1BEF5F`、
  `715A3437101846271A901CC90140DAC9969D459361D7FEE6EF438F75DE945208`、
  `98B1971CC17F9EF3190EB83759329C1BFDDE1EF64F2E2383B9ED4DA036641E19`。
- 三机型 source 又生成独立绑定报告
  `artifacts/evaluation/simulator-3d-control-binding-final-continuation.json`（2,278 bytes，
  SHA-256 `262C659B1860FAA2098AC48B211588A2CE523604A4C45A465E7F530C6F3F0C96`），确认
  `all_airframes`、`records_hash_bound`、`all_gate_passed`、`external_runtime_not_claimed`。
- 现场、外部 PX4/Gazebo、人工 Fly View、室外 GNSS、真实 Pixhawk/Jetson/相机和 HIL/REAL
  仍保持未完成/未声明，版本继续为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 replay-set threshold calibration (2026-09-02)

- 新增 `src/multidetect/replay_set_calibration.py` 与
  `scripts/calibrate_replay_set.py`：直接复用哈希绑定的六分层 replay-set，执行类别级
  confidence threshold 扫描，并输出聚合与 day/night/haze/reflection/smoke-only/no-fire
  分层结果。
- 新增 `stability` 跨层汇总和 `--stability-tolerance`（默认 `0.10`）：报告每个类别的
  推荐层覆盖率、缺失层、阈值 min/max/mean/range 与 `stable_at_tolerance`；缺少正例的
  层保持显式缺失，不折算为零阈值。
- 新增 `verify_replay_set_calibration.py` 与 `verify_replay_set_calibration_report`：重新
  加载 manifest、核对 raw/canonical SHA-256，并重跑报告中编码的校准请求；复核
  `gate_passed=true` 只证明证据未漂移，不会把 `promotion_ready=false` 升级为通过。
- E7 `release-gate` writer/evaluator 新增可选的
  `--replay-set-calibration-verification`：把独立校准复核 artifact 作为
  `replay_set.calibration_verification` 子绑定，读取清单时再次校验 artifact/report/manifest
  SHA-256 并确定性重算；该子门只证明校准报告完整性，不替代 IoU 精度门。
- 校准报告只生成可审阅的调参建议，不会修改模型清单、Jetson 参数或在线阈值；
  `promotion_ready` 仍要求完整 `deployment_recording`、全部预测流和显式独立复核。
- SIM/camera-only 或空正例基线会保留 `promotion_ready=false` 与明确失败原因，继续阻断
  replay-set 精度门，不升级为室外/真实部署证据。
- 当前 SIM/Gazebo report-only 快照为
  `artifacts/replay-sets/sim-gazebo-2026/threshold-calibration-20260902-pre.8-report-only.json`
  （32,703 bytes，SHA-256
  `CA32418F6C253D42800C4DCA4218EA9C0425DB4B87F712C054FA456A0275517D`）。
  对应独立复核快照为
  `artifacts/replay-sets/sim-gazebo-2026/threshold-calibration-20260902-pre.8-verification.json`
  （856 bytes，SHA-256
  `BDAB5475B2683A801B6DB43CA1CB55CDE416493D3128D7020C7AFE8D6446B0F7`）。
- 生成并复核新的 replay-set 绑定 E7 pre-release 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v1.json`
  （10,680 bytes，SHA-256
  `0661B9DF660613C038AD967D07EF3892325C1A44D4AB7E1E13CE9BB19A671762`）及固定时间
  report-only 输出
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v1-report.json`
  （21,523 bytes，SHA-256
  `045E7160950B123EB559FC8C89C250147D3F0AB173648BCA30B56C5A199A91FB`）。复核结果中
  `replay_set.present=true`、六层 coverage 和 raw/canonical hash 均已绑定，但
  `replay_set.gate_passed=false`、`release_allowed=false` 仍由 SIM 合成部署域门和其它
  未完成的 HIL/REAL/人工验收明确阻断。
- 新增校准复核绑定的 pre.8 诊断清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v2-calibration.json`
  （11,490 bytes，SHA-256
  `04ACDA079F66B3ECCB387124FA66BDE3DE8959A43328BBC8D8618C67DC908A92`）及固定时间报告
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v2-calibration-report.json`
  （23,554 bytes，SHA-256
  `E77388B10F71E13245AEE15CFEC04C3D8AEEAB9A1488C019FAD22DC3F82046DA`）；其中
  `replay_set.calibration_verification.passed=true`，而 `report_promotion_ready=false`
  和 `replay_set.gate_passed=false` 仍保持 fail-closed。

### v0.3.0-pre.8 current acceptance evidence refresh (2026-09-02)

- 重新按当前 `configs/release-parity.json` 生成三机型 external runtime/live-AI join：
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-manual-watchdog-v2.json`
  （5,407 bytes，SHA-256
  `6701803509C4DE7322B553034B6ABAC72D4C04836C6289C85B51C61F88652770`）。独立 `--join`
  revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-manual-watchdog-v2.json`
  （544 bytes，SHA-256
  `A1C653C53E71F22186D2216858628575C3020200E2949C38653D8996510078D0`），
  `source_recomputed=true`、`gate_passed=true`；修复了旧 join 引用已更新 safety parity
  时造成的摘要漂移。
- 重新执行完整 `run_goal_acceptance.ps1`：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-manual-watchdog-v2.json`
  （39,946 bytes，SHA-256
  `13467799B7924BED99E72DAFCEA214FBA4FFBE96C246C160B4C37772DDCE6929`）。核心测试收集
  `2431`、QGC custom `66/66`、ruff、compileall、QGC protocol self-test 和各软件验收子门
  全部通过，`software_status=pass`；现场仍为 `hardware_status=offline_pending`。
- 用同一批当前 source 重新生成 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2.json`
  （10,481 bytes，SHA-256
  `AAC4FDA23C6F47CC23D856F8B5C06F59E164B4C3222538B9B0A5D2F847D05572`）及固定时间
  report-only 输出
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2-report.json`
  （21,863 bytes，SHA-256
  `040EC28064A4C4BCA17BA7273DB1E929B661E029A7786F7CC62D8B9846AEB848`）。五项 parity 和
  SIM safety/scenario/Mission 子门均为 `true`，但 E7 仍按定义保持关闭，原因是 REPLAY 正
  样本与独立复核、现场网段/设备、人工 Fly View、HIL/REAL 和 prerelease channel 尚未完成。

### v0.3.0-pre.8 field-link PowerShell fail-fast forwarding (2026-09-02)

- Windows `scripts/run_field_link_preflight.ps1` 现在暴露 `-StopOnFailure`，并将它原样
  转发给 Python bounded-watch 实现；现场首个失败样本出现时可以立即结束等待。
- partial watch 仍由同一 verifier fail-closed：报告保留 `samples_requested`、
  `samples_collected`、`first_failure_index` 和 `hardware_link_ready=false`，不会把不完整
  的现场窗口升级为设备就绪证据。
- 新增 wrapper 回归断言，确认该开关不会修改网卡/IP/路由，只影响只读预检的采样停止策略。

### v0.3.0-pre.8 partial-watch route verifier closure (2026-09-02)

- 修复现场 `-StopOnFailure` 在启用 `-ExpectedSourceCidr` 时的 verifier 矛盾：未收齐
  请求窗口的 watch 允许聚合 `route_checks_passed=null`，并把它解释为“剩余样本尚未
  观察”，而不是伪造一个布尔结果。
- 完整 watch 和 one-shot 仍严格要求路由结果为布尔值；partial watch 若提交布尔值仍
  会因聚合结果与采样数量不一致而 fail-closed。
- 新增结构回归与 Windows wrapper 冒烟：1/3 样本首失败即停止，`--allow-unready`
  verifier 返回 0，报告仍明确 `hardware_link_ready=false`。

### v0.3.0-pre.8 E7 field-link binding refresh (2026-09-02)

- 用最新 fail-fast 现场报告重新生成当前 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3.json`
  （10,336 bytes，SHA-256
  `E498B45064682280DE3F82A9BA479CD5B4BBD261BABB56D16B45CCB00638AFBA`）。清单绑定的
  `real_field_link` 保留 `route_checks_requested=true`、`route_checks_passed=null`，并锁定
  报告 SHA/字节数；`null` 只表示 partial watch 尚未收齐窗口，不表示路由通过。
- 固定时间 report-only 复核为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3-report.json`
  （20,260 bytes，SHA-256
  `5CF73EA62905AD41F3FABEC0C904A5C558681AB978589051C9512BAF15EE9254`）。复核已移除旧的
  “route_checks_passed 必须为 boolean”结构误报，仅保留现场 `hardware_link_ready=false`
  和对应 E7 阻断；`gate_passed=false`、`release_allowed=false` 仍是正确结果。
- 该绑定不会把当前 `192.168.1.13/24` 桌面网段、Jetson/GR01/RTSP 超时或未完成样本窗口
  升级为 REAL/GNSS 证据；接通飞机后必须重新采满完整 watch，并用真实 route CIDR 重新绑定。

### v0.3.0-pre.8 interactive MANUAL_CONTROL freshness gate (2026-09-02)

- 为 QGC 原生 Fly View 的 `MANUAL_CONTROL` 输入增加可选 freshness watchdog。
  interactive orchestrator、standalone `control-gateway` 和 `interactive-router` 默认阈值为
  `0.5 s`；headless/deterministic 调用保持 `None`，也可通过
  `--manual-watchdog-seconds` 显式覆盖。
- 手动 authority 在阈值内没有新帧时，唯一的
  `SimulatorMavlinkControlAdapter` 原子进入 `FAILSAFE`，发送一次
  `source=manual_watchdog` 的 wire-mode 正确 neutral；重复 tick 不重复发送。释放 manual
  authority 或 AI 接管会清理旧 lease，关闭路径同样保持单次 neutral。
- `authority`、`interactive-router`/`control-gateway` 与 `job` evidence 新增阈值、
  `manual_watchdog_neutral_count`、`manual_control_active` 字段；`mavlink_control.py` 已纳入
  canonical safety parity，当前 `safety_policy_hash` 为
  `f781b90021938e5b9d45056eb48c6367599401ee65f2f8f0279f0aaa03bab2d3`。
- 新增 adapter、gateway、orchestrator 和 CLI 回归；软件门继续保持可复现，真实 Jetson/
  相机/Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 发布门仍待现场验收。
- 交互路径重新生成三空架构的验收快照
  `artifacts/evaluation/simulator-interactive-acceptance-20260902-manual-watchdog-v1.json`
  （113,245 bytes，SHA-256
  `BE9E290B1D04BF191B2DB8B372739B8F62E4FFD0F4A46EF94F790EA14275DC2F`），三种机型
  `gate_passed=true`，并确认 router/adapter 均绑定 `manual_watchdog_s=0.5`。
- 同一配置重新生成 VTOL 3-D 控制快照
  `artifacts/evaluation/simulator-3d-control-acceptance-20260902-manual-watchdog-v1.json`
  （83,149 bytes，SHA-256
  `0C6E2A418FB808EE6B8E33BD6CFD940826678543FD4C7A6F20974CBE0D2EC81E`），其
  deterministic protocol/asset gate 与嵌入的 native QGC interactive 子门均通过；外部
  PX4/Gazebo、桌面视觉与硬件状态仍按门定义保持未声明。
- 用新的 `safety_policy_hash` 和 17:30 现场报告重新生成 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v1.json`
  （SHA-256 `55D5D65E7E4574D11BA77EF4DA1AD92EBA056CCB32ACF7242B7E1560D84381E6`）及固定时间 report-only 报告
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v1-report.json`
  （23,063 bytes，SHA-256
  `28F678330DECF412CECD30BF3073060A2113BA648F8C25D2BD79DA29E49DF4AA`）。五项 parity
  均为 `true`，但 REPLAY 精度、现场链路、人机视觉和 HIL/REAL 仍按门定义保持关闭。

### v0.3.0-pre.8 replay accuracy gate hardening (2026-09-02)

- Added a fail-closed guard for replay reports whose aggregate precision or
  recall is undefined. Empty/all-negative SIM baselines remain useful for
  transport and false-alarm regression, but cannot become deployment accuracy
  evidence without positive truth and threshold-qualified prediction samples.
- Added a regression test for a complete, explicitly reviewed, empty replay set
  and documented the exact `aggregate metrics precision/recall are undefined`
  failure reason.
- Refreshed the software acceptance snapshot at
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-replay-guard-v8.json`
  (39,850 bytes, SHA-256
  `C0CF30A37C028B3E526C0068EE10DC5A8DBDAE276F2EBCD3C6FC7B9BBD849391`), with
  2,423 core tests and QGC custom `66/66`; hardware remains `offline_pending`.
- Bound the guarded replay evidence to
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8.json`
  (SHA-256
  `8B62437E85583F244047F4CFA18D34C724F5E0C4C51DA39A66C01096EF1CE6E9`) and its
  fixed-time report-only output
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8-report.json`
  (SHA-256
  `1E5B1FAFA11D8058DC0C21A3FF0435CD92E7D30CF1BCDEE40C7D79DBBD8E5823`). The
  verifier returned `0` in report-only mode and `2` in strict mode; the release
  gate remains closed until positive deployment truth and independent review exist.

### v0.3.0-pre.8 external runtime/AI evidence binding (2026-09-02)

- 重新校验 VTOL、fixed-wing、multirotor 六份 runtime/AI source，并将当前 join
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
  （5,407 bytes，SHA-256 `1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`）
  绑定到最新 goal acceptance。
- `--join` revalidation 输出为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-combined-qgc-v7.json`
  （543 bytes，SHA-256 `206E667E1996C4E530BF5988E85F396E1824A407E565B97D445963AF4F9A49A0`），
  `source_recomputed=true`、`gate_passed=true`。
- 新总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`
  （39,850 bytes，SHA-256 `082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`），
  核心测试 `2421`、QGC custom `66/66`、`software_status=pass`，并写入
  `simulator_external_evidence.provided/verified/source_recomputed/gate_passed=true`。
  该证据仍是 truth-free、无物理输出的软件 SIM 子门，硬件继续 `offline_pending`。
- portable `MultiDetectGCS.exe --offline --simple-boot-test` 退出码 `0`；日志摘要保存在
  `artifacts/evaluation/qgc-simple-boot-20260902-combined-qgc-v7.log`。

### v0.3.0-pre.8 field-link read-only re-probe (2026-09-02)

- 台式机迁移后重新执行 2 个样本、启用 `ProtocolChecks` 的只读现场复探：Ethernet=`192.168.1.13/24`，
  期望 `192.168.144.0/24` route 未命中，Jetson SSH、GR01 MAVLink 与相机 RTSP 均超时，
  `hardware_link_ready=false`。报告为
  `artifacts/evaluation/field-link-preflight-20260902-154000-continue.json`（11,758 bytes，
  SHA-256 `1F9A30F4407C1B31435F57155D675CB7BD09EC781BB3734E848D81BB4904F4FC`）。
- 现场报告已绑定到 E7 `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v7.json`
  （10,895 bytes，SHA-256 `1E16B731CBF6E9252986210266785CE68A505E380065978699A7A421B00A58D2`）和
  goal acceptance `artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`
  （39,850 bytes，SHA-256 `082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`）。
  `software_status=pass`、核心测试 `2421`、QGC custom `66/66`；硬件继续为
  `offline_pending`，没有把局域网候选主机当成飞控或相机。
- E7 固定时间 `2026-09-02T07:50:00Z` report-only/strict 均保持 `gate_passed=false`、
  `release_allowed=false`（报告 SHA-256 `A16160554D6A0DFD02414EC67F15E635835B89C7F80D5F0CCF1950C479D04035`，
  23,016 bytes，退出码 `0`/`2`）。
- 17:30 又执行一次同样的两样本只读复探，最新报告为
  `artifacts/evaluation/field-link-preflight-20260902-173000-manual-watchdog.json`
  （11,757 bytes，SHA-256
  `5ABE546780E703740B8C1A3866F38691ADF7F8CCDF7FB60E2C8C6D7DD3A4E428`）。结构、路由与
  watch 一致性 verifier 通过，但两个样本均未通过：本机 `192.168.1.13` 不在期望的
  `192.168.144.0/24`，Jetson/GR01/RTSP 仍不可达，因此 `hardware_link_ready=false`。

### v0.3.0-pre.8 combined SIM/QGC Mission full-window rerun (2026-09-02)

- 用 `Frames=900`、`RouterDurationSeconds=420`、`MissionTimeoutSeconds=240`、
  `MissionSetupAllowanceSeconds=120` 和固定翼停留 `12 s` 重新执行同一条 PX4/Gazebo/QGC
  联合链路。证据为
  `artifacts/evaluation/combined-sim-qgc-20260902T071623Z-66836/combined-evidence.json`
  （250,024 bytes，SHA-256
  `38E179075340A41CF040406DC89F7CF106EC1282374AD3B6376329C17CB6191B`），顶层
  `gate_passed=true`，独立 verifier 为同目录的
  `combined-evidence-verification.json`（2,567 bytes，SHA-256
  `DC06C1CC646EE37C94FC644AD59DF49074A688ECB361F69A206E475354430419`），退出码 `0`。
- 完整 VTOL Mission 在 `117.938 s` 内观察到 `armed`、多旋翼起飞、固定翼转换、reached 序列
  `0,1,2,3,4,5,6`（current telemetry 为 `0,2,3,4,5,6`，transition `1` 由 transaction/journal
  确认）、返程降落和最终 `disarmed`；最大相对高度 `25.423 m`、离家最远
  `127.632 m`。13 条 Mission transaction journal 记录、哈希链、动态 loopback 端口、
  Docker 清理和所需 Gazebo camera/CameraInfo/NavSat topics 均通过。
- 同一 QGC 进程的原生 `videoContent` fixed-wing QQuickItem 抓帧和 AUTO Mission 窗口证据
  通过：`qgc-native-video-item-fixed-wing.png`、`qgc-native-video-grab-proof.json`、
  `qgc-live-video-surface-proof.json` 与 `qgc-fly-view-auto-mission.png` 均在运行目录生成。
  这证明的是 software-only SIM/QGC/Mission 子门，不是人工视觉、真实 Pixhawk、Jetson、
  室外 GNSS、HIL/REAL 或实飞。
- 为该通过的 source 生成了 pre.8 SIM envelope
  `artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v5.json`（579 bytes，
  SHA-256 `7434FE3E3C47A947B58C53F49AE1E083CEFBF871F3B03A06A0417E21EFBE6493`），并写入新的
  E7 清单 `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v5.json`
  （10,879 bytes，SHA-256
  `1340AC8CCCB62CB02F4F82B20DBAEA8C570353C10C18D719D951A430DD46402A`）。固定时间
  `2026-09-02T07:30:00Z` 的 report-only/strict 报告均为 22,968 bytes、SHA-256
  `E8051A6C7BD68DD793ACAB6B77415BF2D44915C5474952D23F95476CFE18C847`，退出码 `0`/`2`；
  REPLAY 精度、现场链路、HIL/REAL、人工视觉和 prerelease channel 仍保持阻断，最终版本
  继续为 `v0.3.0-pre.8`。
- 在同一输入上完成独立 goal acceptance 续跑：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v5.json`（39,499
  bytes，SHA-256 `75340E0AACCFBF41538DA7B024C9484BA757090862867E92D6CDC4C62D10B169`）。
  核心测试收集 `2421`、QGC custom `66/66`、ruff/compileall/diff-check 和 QGC protocol
  self-test 均通过，`software_status=pass`；绑定的现场报告仍为
  `hardware_link_ready=false`、`hardware_status=offline_pending`，剩余现场标定、真实
  DET→TRK→LCK→TGT、Jetson soak、HIL/REAL 与人工视觉门不变。

### v0.3.0-pre.8 3-D SIM control and field-subnet discovery (2026-09-02)

- 重新执行仓库内 VTOL `river_rescue_vtol_001` 的 3-D SIM 控制预检：固定的
  PX4/Gazebo world/model、hash-bound stack plan、原生 QGC Fly View 手动控制、共享 AI
  控制路径和 watchdog/failsafe 均通过确定性门。证据为
  `artifacts/evaluation/simulator-3d-control-acceptance-20260902-continue.json`
  （82,463 bytes，SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）；
  `gate_passed=true`、`deterministic_gate_passed=true`，但 `external_runtime_gate_passed=false`、
  PX4/Gazebo `ready_to_launch=false`，因此没有把 dry-run 预检写成真实 3-D、GPU、Pixhawk 或
  人工视觉验收。
 - 在当前台式机 `Ethernet=192.168.1.13/24` 上做了有界的 `192.168.1.0/24` 服务发现：
   254 个地址中 4 个 SSH 端口响应，MAVLink `5760` 和 RTSP `554` 均为 0 命中；期望的
   `192.168.144.0/24` 路由仍未出现，现场预检 `hardware_link_ready=false`。发现报告为
   `artifacts/evaluation/field-link-preflight-20260902-discovery-continue.json`
   （8,542 bytes，SHA-256
   `6974A7410B5779E71CDDF39C158F2356BF6C67A394E29433C528EEB16062607C`）。该结果只用于
   接线诊断，不自动替换 Jetson/GR01/相机端点，也不改变最终发布门。
- 在相同的显式网段再次启用只读 `ProtocolChecks`：报告
  `artifacts/evaluation/field-link-preflight-20260902-discovery-protocol-continue.json`
  （9,402 bytes，SHA-256
  `3D8CBBF0E6F2A2B40966BB31694700C394FBFF49050FA57B5DB2A792B63D95CF`）确认 4 个 SSH
  banner，但 MAVLink `5760`、RTSP `554` 均 0 命中，期望 route 与显式三设备端点均未就绪。
  该报告保持 `protocol_checks_passed=false`、`hardware_link_ready=false`，只作未绑定
  的现场诊断 provenance，不会把普通 SSH 主机提升为 Jetson、Pixhawk 或相机。
- 使用项目 venv 重跑 `pytest -q`，全量回归退出码为 `0`；现场捕获/GNSS sidecar、QGC
  custom `66/66`、桥接首次 armed heartbeat、Docker AI/external evidence 和三机型
  scenario matrix verifier 均保持通过。真实 Jetson、室外 GNSS、Pixhawk、HIL/REAL、
  人工 Fly View 与最终 `v0.3.0` 发布门继续保持 pending。
- 将本次现场发现报告绑定进完整 goal acceptance：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-discovery-continue-pre8.json`
  （31,413 bytes，SHA-256
  `83B90A14ADFCDEF7456F87B1F3357D25EB17D95830FB2570F75E696050E8D099`）。快照记录
  `software_status=pass`、`hardware_status=offline_pending`、核心测试 `2421`、QGC custom
  `66`，现场报告本身已通过结构/哈希复核但 `hardware_link_ready=false`；它没有把当前
  局域网 SSH 候选机或 dry-run SIM 当成飞控、GNSS、相机或实飞证据。
- goal acceptance 重跑刷新了 PX4 Mission artifact 的 SHA 后，重新装配了对应 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-discovery-continue.json`
  （10,855 bytes，SHA-256
  `372EA72BAB5B116A394F5168BF46FFE61827A1BFA4B3B934C2DFF8DA9BF12BB8`）。固定时间
  `2026-09-02T06:50:00Z` 的 report-only/strict 报告均为 22,905 bytes、SHA-256
  `1020954A94282AB4C3A8399A50DC3F74664C1262E610C921AB8E29ACD9EEA3F8`，退出码 `0`/`2`，
  `px4_airframe_transaction`、SIM safety、三机型矩阵和 combined SIM 均通过；整体
  `gate_passed=false`、`release_allowed=false`，31 条阻断原因仍如实保留。

### v0.3.0-pre.8 direct-range fail-closed safety (2026-09-02)

- `MultiModalRangingEngine.solve_direct()` 现在拒绝非有限或负的评估时间、重复的直接测距
  来源，以及不具备最低向下 NED 分量的水平/向上目标射线；这些输入返回明确无效原因，
  不发布伪造的地面交会距离。该路径继续保持 `DEGRADED` 语义，并与带垂向参考的测距门
  共用目标射线安全约束。
- 新增 `tests/test_multimodal_ranging.py` 回归，覆盖重复来源、非法时间和不安全射线；
  总验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-direct-range-safety-pre8.json`
  （39,808 bytes，SHA-256
  `CEEBFAF6B0D0006C64E337A98FB9E30D153FCB1127C4B8A9CE38D0AFCFAC295`），记录核心测试
  `2421`、QGC custom `66/66`、`software_status=pass` 与
  `hardware_status=offline_pending`。真实设备、室外 GNSS、HIL/REAL、REPLAY 精度和人工
  Fly View 门仍保持未完成，版本不提升为最终 `v0.3.0`。
- 重新装配 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-direct-range-safety.json`
  （10,870 bytes，SHA-256
  `87B88E5FAF2AB25435CD74272B4F9EB9F52D5F24FFC9D57F51523D79A11E7A6E`），绑定总验收重跑后
  的当前三机型 PX4 Mission artifact（SHA-256
  `99AFC6FEA5BA0CD19F1FA6DB23570C316FE23F0F92002BEF053E4C3C08B3F0A9`）。固定时间
  `2026-09-02T06:25:00Z` 的 report-only/strict 输出均为 22,936 bytes、SHA-256
  `165BF83E476FE9F9CA79186950EAD49FF1E9EAFEE405E275D71AD5A1F35FA60C`，退出 `0`/`2`，
  31 条阻断原因和 `release_allowed=false` 保持不变。

### v0.3.0-pre.8 video-to-raw-replay recording (2026-09-02)

- 新增 `src/multidetect/video_replay_recording.py` 与
  `scripts/create_video_replay_recording.py`：将录制视频逐帧解码为 PNG，并生成经过
  `RawSensorReplayRecording` 二次加载校验的 `events.jsonl`、`recording.manifest.json` 和
  `recording-summary.json`。源视频、事件流、每一帧图像与 recording identity 均带 SHA-256，
  `--frame-stride`/`--frame-limit`/`--fps` 用于可重复抽样和缺失 FPS 的显式补参。
- 转换器明确写入 `camera_only_unknown_navigation`：`position_healthy=false`、GPS 未绑定、
  无真值/预测、部署域精度与发布门均为 `false`。它只补齐视频到同一 raw replay/evaluator
  的接线，不把普通录像升级为室外 GNSS、目标落图、HIL/REAL 或最终版本证据。
- 新增 `tests/test_video_replay_recording.py`，覆盖哈希篡改 fail-closed、FPS override、帧抽样、
  失败清理、脚本 `--help` 和真实 OpenCV 容器烟测；`docs/replay-set-manifest.md`、
  `docs/repository-architecture.md`、`README.md` 已补充目录约定与现场接入命令。
- 新增 `src/multidetect/replay_telemetry_join.py` 与
  `scripts/join_raw_replay_telemetry.py`：把独立完整 `RawReplayTelemetrySample` JSONL
  sidecar 以 latest-at-or-before、无插值、最大年龄门合并到已验证录像，重建并校验图像/事件/
  recording hash，逐帧重算 GPS 质量和失败原因。joined recording 仍显式保持
  `deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`；
  如果源 manifest 已有 `scenario_id`/`scenario_manifest_hash`/`scenario_seed`，会在新 manifest
  和 recording identity 中保留同一场景绑定；没有源绑定时不生成伪 scenario。新增回归覆盖
  scenario binding 保留以及 stale/future/duplicate/malformed sidecar、哈希验证和 CLI 帮助。
- 新增 `src/multidetect/gnss_capture_sidecar.py`、
  `scripts/gnss_capture_to_replay_telemetry.py` 与
  `configs/schemas/raw-replay-telemetry-sidecar.schema.json`：校验只读 GNSS capture 报告，
  仅将 `GPS_RAW_INT/GPS2_RAW` 归一化为完整 telemetry sidecar；显式 origin/offset 用于和分开
  启动的录像对齐，未知姿态/AGL/围栏/任务/执行器字段不被猜测，`GLOBAL_POSITION_INT` 不会
  旁路 raw GPS 质量门。新增回归覆盖无 raw GPS、篡改报告、负时间映射、原子写入和 schema/CLI。
- `scripts/run_field_link_preflight.ps1` 现在显式暴露并转发 Jetson、GR01/Pixhawk、相机的
  host 与 `22/5760/554/14580/14581` 端口参数，迁移到新电脑或新网段时不再只能使用脚本
  内置端点；发现仍是有界诊断，不会自动替换端点或修改网络。单样本转发回归报告为
  `artifacts/evaluation/field-link-preflight-20260902-wrapper-explicit-after-acceptance.json`
  （3,712 bytes，SHA-256
  `6A5689AF656F6FB51DCD781A7110F296F74A4E38EA1A3492CA47FB3B251CD326`），严格 exit `1`
  如实保留 `hardware_link_ready=false`。

### v0.3.0-pre.8 complete goal-acceptance continuation (2026-09-02)

- 用当前 `pre.8` portable EXE、v4 combined SIM、三机型 external live-AI join 和严格现场
  preflight 重跑完整 goal acceptance，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-continue-pre8.json`
  （39,771 bytes，SHA-256
  `CEC975A754E7F4F918C50F76F92EB46D0B85D7F41645FE35BDC8F9F5E75CF7CC`）。项目测试收集
  `2418`、QGC custom `66/66`、`ruff`、`compileall`、`git diff --check`、QGC protocol
  self-test 均通过；SIM combined 与 external live-AI join 均复核为 `gate_passed=true`。
- 快照如实保留现场 `field_link_ready=false`、`hardware_status=offline_pending`，因为
  Jetson/GR01/相机端点超时且当前网卡为 `192.168.1.13`；REPLAY 精度、室外 GNSS、HIL/REAL、
  人工 Fly View 和最终 release channel 仍未完成，继续使用 `v0.3.0-pre.8`，不发布最终
  `v0.3.0`。开发机已存在的 operator/MAVLink key 只作为环境自检，不代替设备或实飞证据。
- 同一批 mode evidence 重新生成 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-continue-pre8.json`
  （10,844 bytes，SHA-256
  `8470F5AE136154D264D273D1E7D3446370B7A83CCC3BC0ECB809291E4A37D0AA`）。固定时间
  report-only/strict 复核报告均为 22,921 bytes、SHA-256
  `29DF553263AC38AC944C9209ABB5C56CFCEBD8298FAAB1B843C65BBD8BA776EE`，退出码分别为
  `0`/`2`，`gate_passed=false`、`release_allowed=false` 保持不变。

### v0.3.0-pre.8 endpoint-forwarding acceptance continuation (2026-09-02)

- wrapper 显式端点修复后再次执行总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-wrapper-endpoint-continue-pre8.json`
  （39,814 bytes，SHA-256
  `3C3F036E62FDA5C7314AE142F0714F502C6E40599013E81EFD1D5ACCA95A8ED5`）。当前 E7 清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-wrapper-endpoint-continue-pre8.json`
  （10,870 bytes，SHA-256
  `8FE2FAC78FFAC69140C3693CA902B32F8BEEA2CAEABF9456464F75FD96616CE4`）；固定时间报告
  SHA-256 为 `24B8247077C2CBC9DB6E85FA688712FB62D0CDC38E337FD02544A08C62C07C9F`，
  report-only/strict 退出 `0`/`2`，发布门继续关闭。

### v0.3.0-pre.8 desktop live-AI external-SIM continuation v2 (2026-09-02)

- 在同一 pinned PX4/Gazebo digest 上重新执行 VTOL、fixed-wing、multirotor 三机型
  disposable external runtime，并使用 `--execute --exercise-arm --exercise-target` 验证
  live-AI ARM/目标 `TRACK` 路径。三组 runtime 均取得 qualified `GPS_RAW_INT`
  （`fix_type=3`、10 颗卫星）、armed heartbeat、loopback UDP I/O 和 stop/rm 清理；三组
  AI 均记录 `ai_steps=2`、`target_exercise.accepted=true`、`decision.action=track`，输入
  仍为 truth-free synthetic perception，硬件控制保持关闭。
- 当前三组 live-AI source 为：VTOL
  `simulator-docker-ai-acceptance-20260902-pre.8-vtol-continue-v2.json`
  （133,609 bytes，SHA-256
  `284A2C56A0698C0DF537300BAA7CCB0DF6B538FC1EFE401328D3923E210BC362`）；fixed-wing
  `simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-continue-v2.json`
  （104,297 bytes，SHA-256
  `FE87A9C3DC356D74AFFF2D24ABDF340054224AF74FD34D453017335DC25D539B`）；multirotor
  `simulator-docker-ai-acceptance-20260902-pre.8-multirotor-continue-v2.json`
  （110,065 bytes，SHA-256
  `4BE8C2F263C48FEE6C9B87B246A1BD56DA86A4C4C8FAD48F565CE87DD87DDFF4`）。
- 六份 runtime/AI source 重新 join 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
  （5,407 bytes，SHA-256
  `1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`，
  `gate_passed=true`）；`--join` 二次重算输出为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v2.json`
  （543 bytes，SHA-256
  `9066B558F5F9047A0DE210AFBD5895DD66DEC395BFE38229037DC398CA1829DD`，
  `source_recomputed=true`、`gate_passed=true`）。
- 该 join 已接入新的总验收快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-live-ai-continue-v2.json`
  （16,939 bytes，SHA-256
  `BB371E3B1242E991CC6539D64961C7F4321ADCDC1F1539C3B3C39E842A9ECEF9`）。快照记录
  外部证据 `provided/verified/source_recomputed/gate_passed=true`、核心测试 `2400`、
  QGC custom `66/66`、`software_status=pass`；绑定的严格现场报告仍为
  `hardware_link_ready=false`，故 `hardware_status=offline_pending`，没有关闭室外 GNSS、
  真实硬件、HIL/REAL、人工视觉或最终发布门。
- 同一 continuation 还保留 deterministic 3-D 控制资产
  `simulator-3d-control-20260902-continue-v2.json`（82,463 bytes，SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）。其
  `gate_passed=true`、`deterministic_gate_passed=true`，但 external 3-D runtime、视觉和
  硬件字段继续为 `not_claimed`；版本仍为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 full regression parity coverage (2026-09-02)

- 强化 `tests/test_release_parity.py` 的仓库越界测试：symlink 特权不可用时回退到 Windows
  目录 junction（`mklink /J`），仍使用 `Path.resolve()` 验证外部目标被拒绝；不改变生产
  parity 算法或任何发布门语义。
- 台式机全量 Python 回归现在为 `2400` 项全部通过，原先唯一的 symlink 环境跳过已消除；
  `ruff` 与 `compileall` 均退出 `0`。该结果仍只代表源码测试覆盖，不代表现场 GNSS、Pixhawk、
  HIL、REAL 或人工 Fly View 通过。

### v0.3.0-pre.8 combined SIM/QGC/Mission v4 acceptance (2026-09-02)

- 在 QGC 主窗口 `MainWindowHandle` 短暂为零时启用同一 PID 的唯一可见顶层窗口回退，并
  用 `900` 帧、`MissionTimeoutSeconds=240`、动态 loopback 端口重新执行联合验收。
- 新的顶层证据为
  `artifacts/evaluation/combined-sim-qgc-20260902-hwnd-fallback-v4.json`（249,689 bytes，
  SHA-256 `8F90EAB7232A29382451CF99BB5B3CF0E4D44674E9BC4B027C0962120C46D692`），
  `gate_passed=true`。独立 verifier 为
  `artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification.json`
  （1,941 bytes，SHA-256
  `8913C015977B1C879E07A76A48E9F9D2AD86E0F2F8784F1D81AC68FAE9C71055`），退出码 `0`。
- 集成 SIM、VTOL attach、VTOL Mission、900/900 RTSP/传感器帧、动态端口、Docker 清理和
  13-record hash-chain Mission journal 均通过。Mission 观察到 `AUTO.MISSION`、armed、
  in-air、MC、FW、landed 和 disarmed；item `0..6` 全部到达，持续 `116.078 s`，最远
  距离 `142.571 m`、相对高度 `26.587 m`。
- QGC 原生 `videoContent` fixed-wing QQuickItem 抓帧通过，
  `native_qquickitem_video_pixels_proved=true`、
  `transport_bound_native_qquickitem_video_surface_proved=true`；工具栏 indicator/drawer
  机器烟测通过。桌面 GPU/BitBlt ROI 诊断仍失败且人工视觉保持 `not_claimed`，没有把截图
  证据升级成 Fly View 人工验收。
- v4 仍是 software-only SIM：无真实 Pixhawk/Jetson/相机/执行器，现场 GNSS、HIL、REAL、
  人工视觉和最终 `v0.3.0` 发布门继续关闭；此前 v2/v3 失败 artifact 原样保留。

### v0.3.0-pre.8 v4 SIM release-mode binding (2026-09-02)

- 为当前 `combined-sim-qgc-20260902-hwnd-fallback-v4.json` 生成 hash-bound SIM envelope：
  `artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v4.json`（562 bytes，
  SHA-256 `65B96C6DF3BC66BD77B13D56BE8D5019210B6C52C6638BECA0162980DE9D7CC8`）。
- 独立 verifier 使用 `--require-release-mode` 重新检查 source path/hash，输出
  `artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification-v4-bound.json`
  （2,535 bytes，SHA-256 `5BDAB531DD120645BFE5AF4A8D9816A222EE21B458C020F3EECBA48FE7E1D415`），
  `gate_passed=true`。该绑定仍是 software-only SIM provenance，不改变现场 GNSS、HIL/REAL、
  人工视觉或最终 `v0.3.0` 门。

### v0.3.0-pre.8 E7 manifest refresh for v4 (2026-09-02)

- 生成当前联合 SIM/QGC/Mission v4 的统一 pre.8 E7 清单：
  `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4.json`（10,852 bytes，
  SHA-256 `EAACCA3F40A4B9A6734C550EFFC435A0A13B2F1FC4E08FA12FA0C2F613B66D9F`）。清单绑定
  v4 source、SIM envelope、S4 safety、scenario matrix、PX4 airframe transaction、REPLAY
  accuracy provenance 和严格现场链路报告；SIM/S4/scenario/PX4 子门均为真。
- 固定时间 report-only 与 strict 复核分别写入
  `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4-report.json` 和
  `...-strict.json`，两者均 22,896 bytes、SHA-256
  `F7AEC221A979F03D0AED6FC3B5E983B6458A84CE2C89FCBCB9439546CFC22F19`；report-only 退出 `0`，
  strict 退出 `2`，`gate_passed=false`/`release_allowed=false` 保持不变。
- 报告继续明确 REPLAY 精度/部署覆盖、现场 `hardware_link_ready=false`、四模式
  `not_claimed`、prerelease channel 和最终 release surfaces 是阻断项；该刷新不把 software-only
  SIM 或诊断报告升级成室外 GNSS、HIL、REAL 或人工 Fly View 验收。

### v0.3.0-pre.8 strict field-link recheck (2026-09-02)

- 在当前台式机 Ethernet `192.168.1.13/24` 上重新执行两样本只读 field-link watch，报告为
  `artifacts/evaluation/field-link-preflight-20260902-continue.json`（11,755 bytes，
  SHA-256 `B779A8D4CCD3FAD11DEDDF03E9E691E0C53552E82381440490814058E14850EB`）。两次
  Jetson/GR01/RTSP、协议和期望 `192.168.144.0/24` route 均失败，本机 UDP `14580/14581`
  可绑定，`hardware_link_ready=false`。
- 额外保存严格单样本 `artifacts/evaluation/field-link-preflight-20260902-continue-strict.json`
  （4,401 bytes，SHA-256 `2D1F71C74B7B5F13516C29D3C8BF900A3B3F3B54D8EDAB9E0463595749B5D1F6`）。
  `-RequireReady` 下 wrapper/verifier 均退出 `1`；诊断模式的结构校验 `0` 不被解释为硬件
  通过，REAL、室外 GNSS、HIL 和最终 `v0.3.0` 门继续关闭。

### v0.3.0-pre.8 combined SIM evidence serialization hardening (2026-09-02)

- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在先把聚合证据投影为
  有界 JSON-safe 图，再交给 Windows PowerShell 的 `ConvertTo-Json`：包含活动引用环检测、
  `4096` 项集合上限和显式截断标记。各子验收的原始日志、截图和 JSON 路径仍单独保留，
  因此不会因为 PowerShell 混合对象图或历史诊断对象导致聚合器内存失控。
- 进一步收口 Windows PowerShell 5.1 兼容性：`Get-Content -Raw` 产生的
  `PSPath`/`PSProvider`/`ReadCount` 等 ETS 扩展属性会先通过
  `[string]::Copy()` 脱离；文件 SHA-256 改用带 `FileShare.ReadWrite` 的 .NET 流实现，
  不依赖 Utility 模块的 `Get-FileHash` 命令自动加载。每个顶层字段最多 `20,000` 个投影节点，
  字段独立序列化；顶层 `$null` 明确写成 JSON `null`，避免产生无效的 `"key":`。
- 顶层 envelope 改用 `Write-Utf8NoBomText` 显式写入 BOM-free UTF-8，绕过 Windows
  PowerShell 5.1 `Set-Content -Encoding utf8` 的 BOM 默认值；新证据可被严格 JSON 解析器直接
  读取，历史文件仍按原始字节和 SHA-256 保留并使用 BOM-aware reader 复核。
- QGC 窗口屏障在 `MainWindowHandle` 短暂回零时仅接受同一 PID 的唯一可见、非最小化、非
  cloaked 顶层窗口候选；零候选或多候选继续 fail-closed，避免把其他窗口或猜测坐标写成视觉证据。
  回退事件只进入 stage-progress 诊断旁路，不参与控制路径或 gate。
- 新增 `combined-stage-progress.jsonl` 与
  `combined-evidence.json.serialization-progress.jsonl` 两个只读诊断旁路，记录哈希、
  清理、证据对象和逐字段投影进度；它们不参与任何控制路径或发布门，只用于在桌面会话中
  快速定位长时间运行的验收阶段。
- 台式机最新缩短帧数复核写出
  `artifacts/evaluation/combined-sim-qgc-20260902T022832Z-50960/combined-evidence.json`
  （31,347 bytes，SHA-256
  `EC3B88DC55CDCB460CEF80ACA3D4C672C71AA12A608F098F4F11DE22CEBF8756`），字段数为 26，
  `serialization.mode=per_top_level_field`，通过 BOM-aware reader 可重载（原始历史文件保留
  BOM）。该次 run 的门禁仍为
  `gate_passed=false`，失败原因为当前桌面会话在后续阶段未能取得 QGC 主 HWND；初始窗口捕获、
  真实 QGC 日志和容器清理记录均保留，`operator_visual_acceptance=not_claimed`。
- 三次当前台式机重跑均成功写出顶层证据：
  `artifacts/evaluation/combined-sim-qgc-20260902-bounded-evidence.json`（72,135 bytes，
  SHA-256 `9FDFD6526900078AA755282545CC24E7CE836155B60EBFD4E3D8C1C3971A42BC`）、
  `...-r2.json`（70,928 bytes，SHA-256
  `55C36F6E52F59592D073DC70D9942571F48BD4178ACD7F43B18B4429B18C4382`）和
  `...-r3.json`（71,440 bytes，SHA-256
  `9D065FA5DF2CAE3A30E643BB1354E7ACAB5DED7DF1D53E4496343B568690FD69`）。
- 当前三次 run 都按门禁语义记录为 `gate_passed=false`：QGC 原生 toolbar 机器视觉步骤的
  请求中心 `(1960,432)` 在本桌面 PER_MONITOR_V2 会话中实际读回 `(1960,480)`，所以
  drawer 未被点击且 `operator_visual_acceptance=not_claimed`。这项诊断不把坐标猜测写成
  视觉通过；接入可控桌面会话后应重新运行同一脚本。
- 新增静态回归覆盖 JSON-safe 投影、引用环和最终写入路径；PowerShell parser、脚本契约、
  desktop capture 与 native video grab 测试均保持通过。该修复不改变 `v0.3.0-pre.8` 版本，
  也不关闭室外 GNSS、REAL/HIL、真实硬件或人工 Fly View 发布门。

### v0.3.0-pre.8 field-link discovery wrapper evidence (2026-09-02)

- 用 `scripts/run_field_link_preflight.ps1` 对当前台式机 `192.168.1.0/24` 做了一次
  有界、只读的服务发现；扫描 254 台主机，命中 4 个通用 SSH banner，未发现
  `5760/MAVLink` 或 `554/RTSP`。证据为
  `artifacts/evaluation/field-link-preflight-wrapper-discovery-lan-20260902-r1.json`
  （SHA-256 `44E30678D5CB354A54FDA8EF4FBFC11376DE289274384FC828BD7B26918EF52C`）。
- 发现结果继续保持诊断语义：不自动改写 Jetson/GR01/相机地址，不替换端点，不提升
  `hardware_link_ready`；真实飞机接通后仍须使用显式端点和 `-ProtocolChecks -RequireRoute -RequireReady`
  完成现场门禁。

### v0.3.0-pre.8 strict field-link recheck baseline (2026-09-02)

- 保存当前时刻的严格单样本 wrapper 复核：
  `artifacts/evaluation/field-link-preflight-current-20260902-continue.json`
  （4,397 bytes，SHA-256
  `0D3F411D80F01F7D5B4D3CFA63FF1427ABBBFE9A5FC5360BB19E4BDA3B18BA36`）。
  Ethernet 源地址仍为 `192.168.1.13/24`；Jetson/GR01/RTSP 三端点、协议和期望
  `192.168.144.0/24` route 均未通过，本机 UDP 14580/14581 可绑定，严格 wrapper/verifier
  退出码为 `1`，`hardware_link_ready=false`。
- 该文件只作为接线后的时间对照基线，保持 fail-closed，不自动选择发现到的主机，也不关闭
  REAL/HIL、室外 GNSS、人工 Fly View 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 field-link PowerShell discovery forwarding (2026-09-02)

- Extended `scripts/run_field_link_preflight.ps1` with bounded, explicit
  `-DiscoverCidr`, `-DiscoverMaxHosts`, `-DiscoverWorkers` and
  `-DiscoverTimeoutSeconds` parameters.  The wrapper forwards discovery to the
  existing Python implementation only when a CIDR is supplied, keeps the
  candidate list diagnostic-only, and never rewrites Jetson/GR01/camera
  endpoints or `hardware_link_ready`.
- Added PowerShell syntax, forwarding-contract and discovery regression checks;
  the wrapper smoke report is
  `artifacts/evaluation/field-link-preflight-wrapper-discovery-20260902.json`
  (5,679 bytes, SHA-256
  `CF9C2B0C8238631C5E8BA662A3EEC63F550BB9284AFE32B9C5201C04DDE8AA04`).  It
  found only generic SSH hosts on the desktop LAN and no MAVLink/RTSP service,
  so `hardware_link_ready=false` remains the correct result.

### v0.3.0-pre.8 field-link v4 evidence rebind (2026-09-02)

- Re-ran the strict, read-only field-link preflight on the desktop with explicit
  `192.168.144.0/24` route and protocol checks.  The new report is
  `artifacts/evaluation/field-link-preflight-20260902-continue-v4.json`
  (4,396 bytes, SHA-256
  `0438D61B311BBA94150CF59F580DBC4A29E6B31999310E33238A1E52F1A3506C`).
  The active Ethernet source remains `192.168.1.13`; Jetson/GR01/RTSP and all
  route probes are unavailable, while local UDP 14580/14581 remain bindable.
  The strict hardware-link result is therefore `false` with exit code `1`.
- Bound that exact report into
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-field-preflight-v4.json`
  (31,777 bytes, SHA-256
  `250824C312DD0CD8326721C0B27B3B10F05F53E9644FA82CC00EC87F668905F7`).
  The aggregate reran `2,397` core tests and QGC custom `66/66`; software is
  `pass`, hardware is `offline_pending`, and the already verified external-SIM/
  live-AI join remains unchanged.
- The join revalidation was refreshed at
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v1.json`
  (543 bytes, SHA-256
  `54CB1001F907F8D612411579E863CFA2F8D166C71702A707C29B8105ABBA4589`).
  This keeps the final release fail-closed: physical Ethernet/Jetson/GNSS/
  Pixhawk, outdoor accuracy, HIL/REAL and operator visual gates remain pending.
- Generated a fresh E7 manifest bound to the v4 field report at
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-preflight-v4.json`
  (11,453 bytes, SHA-256
  `0E8DB97FDA3157967FB567F8EA5DB91088C52BD2D45FB345A5A04A5BA9CC7AE3`).
  With the fixed evaluation timestamp `2026-09-02T00:00:00Z`, both report-only
  and strict outputs are 25,259 bytes with SHA-256
  `55805E813C3DC2FCC8120341C9D5B153EAACFD081AC2F6CB4E5DCFE15A3B3E3`;
  exits are `0` and `2`, respectively, and both remain
  `gate_passed=false`/`release_allowed=false`.

### v0.3.0-pre.8 Docker external SIM + live-AI rerun (2026-09-02)

- Confirmed Docker Desktop server `28.4.0` on the desktop and reran the pinned
  PX4/Gazebo runtime for VTOL, fixed-wing and multirotor from the current
  checkout.  Each runtime produced a qualified `GPS_RAW_INT`/heartbeat stream,
  loopback MAVLink I/O and a clean disposable-container stop/rm.
- Replayed the same three scenarios with `--exercise-arm --exercise-target`.
  Each live-AI bridge waited for an armed heartbeat, executed two policy steps,
  accepted a truth-free target as `TRACK`, emitted through the shared
  `SimulatorMavlinkControlAdapter`, and returned to a neutral shutdown frame.
- The six source records were joined and independently rehashed in
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v1.json`
  (5,426 bytes, SHA-256
  `1A3D7F99BC0E6C21E68BDFA57B9DFA77856C030E3DC3DE406CB4ED3A78FD63B5`,
  `gate_passed=true`).  This closes the headless external-SIM/runtime and
  truth-free AI/target-policy sub-gates only; visual, hardware, outdoor GNSS,
  HIL/REAL and final-release gates remain explicitly pending.
- Rebound that join into
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-docker-ai-continue-v1.json`
  (31,778 bytes, SHA-256
  `CDDD0C7E6F2DFA7C4160AC88DA9CC393C372371F5B3D1204D8C30C5666CFFB28`).  The
  total acceptance records `simulator_external_evidence` as provided,
  verified, source-recomputed and passed; software remains `pass` while the
  physical field status remains `offline_pending`.

### v0.3.0-pre.8 SIM AI GPS return-quality hardening (2026-09-02)

- Applied the canonical outdoor GNSS ceilings to the shared SIM AI telemetry
  contract: `gps_quality_valid` now requires hAcc `<=6 m` and vAcc `<=10 m`,
  while the return policy clamps legacy single-value accuracy and age knobs to
  the same per-axis/`1.5 s` limits.  A relaxed caller configuration can no
  longer promote a weak fix into `RETURN`.
- Added regression coverage for both accuracy boundaries and for relaxed
  legacy return configuration; the full Python suite now collects `2,397`
  tests with one Windows symlink capability skip, and QGC custom tests remain
  `66/66`.
- The fresh software acceptance snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-ai-gps-accuracy.json`
  (31,407 bytes, SHA-256
  `074DE9C2C8551C3B3CEF7E8462BDF05DFD2CD8AFC70A598BFDF800FA8468F03E`).
- Regenerated `configs/release-parity.json` (3,317 bytes, SHA-256
  `6D0CDBD3D376FBE1AF46A51C721AF3785528CA6BF22016DCD4216979769D7FDF`),
  with `core_algorithm_hash=6c2d186e8b6c31330b16fae096c4d90302797bdf5d697e28ff4efbc55fbdc06d`.
  The corresponding E7 manifest/report are
  `artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy.json`
  (SHA-256
  `47C5C9DA6E0F00B8B6CF4F000D1B693AEE71E632847ABD1F7C6380F06AABFE42`) and
  `artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy-report.json`
  (SHA-256
  `42A18B5929122FFB4F09ABC5638757F44F99587054D2827B2D2C16BEDB4A24F1`).
  Both remain fail-closed (`gate_passed=false`, `release_allowed=false`)
  while the physical field, outdoor GNSS, HIL/REAL and visual gates are
  pending.

### v0.3.0-pre.8 control-input and field-link hardening (2026-09-02)

- Re-ran the hash-bound 3-D SIM control/asset acceptance on the current
  `v0.3.0-pre.8` tree.  The fresh VTOL evidence is
  `artifacts/evaluation/simulator-3d-control-20260902-continue.json` (82,463
  bytes, SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`).
  The deterministic shared QGC/manual/AI/watchdog path, WGS-84 world binding,
  asset digests and loopback port isolation all pass.  PX4/Gazebo executables
  are not installed on this desktop, so `external_runtime_gate_passed=false`,
  `external_3d_runtime_acceptance=not_claimed`, and no visual, hardware, HIL or
  REAL claim was made.
- Unified the canonical outdoor GNSS quality constants in
  `src/multidetect/mavlink_gps.py` and made `AdaptiveRangingConfig` reject any
  `hAcc`, `vAcc` or GPS-age limit looser than `6 m`, `10 m` or `1.5 s`.
  Real-time ranging/geolocation and the receive-only capture/verifier now use
  the same fail-closed policy, so a relaxed caller configuration cannot promote
  a weak fix into a map coordinate.  Added regression coverage for all three
  relaxed-limit paths.
- Regenerated `configs/release-parity.json` after the shared GNSS policy change;
  the current parity document SHA-256 is
  `21744240FAF737EF6F63C74014ADFF50AE321A71B3D2BD5CD6427A06E6BB9500` and its
  `core_algorithm_hash` is
  `ab0593e3f986d49e72645340a0f2e2f0fa81f3fc4bafab004f2507d1d39fec9a`.
- Re-ran the complete goal acceptance after regenerating parity.  The current
  snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-gnss-policy.json`
  (31,407 bytes, SHA-256
  `4892878FEBFA17E2473002293296762B950009FB2060C3EFD69A93DBFD44B24D`), with
  2,393 core tests and QGC custom `66/66` passing.  The matching pre.8 E7
  manifest and report-only aggregation are
  `artifacts/evaluation/release-gate-pre.8-current-20260902-gnss-policy.json`
  (11,454 bytes, SHA-256
  `1BD1DD7B207C2C439EBF0F5F1940661E1D1724BE757C6AEA5771ABEA852C1A65`) and
  `artifacts/evaluation/release-gate-pre.8-current-20260902-gnss-policy-report.json`
  (25,297 bytes, SHA-256
  `42A51E7B893C69FDF27C4D2AF4297DB001794452ECC202F08C71C51CE3FD0EDD`).
  Both remain fail-closed (`gate_passed=false`, `release_allowed=false`) while
  the field-link report is unready and REPLAY/HIL/REAL/visual gates remain open.
- Added [`docs/field-bringup.md`](docs/field-bringup.md), a repeatable post-cabling
  runbook for the Ethernet secondary address, route-aware protocol preflight and
  hash-bound field evidence.  The latest read-only three-sample baseline is
  `artifacts/evaluation/field-link-preflight-20260902-continue-v3.json` (SHA-256
  `388FFB6AE7A0DC9C6399A8CCD24F4353B5ACCE6A9C64E175270C9A2BCE52A09E`); its
  structure verifier passes, while the remote Jetson/GR01/RTSP probes remain
  unavailable and `hardware_link_ready=false`.
- Added the receive-only `multidetect-gnss-capture` entry point, its strict
  `configs/schemas/gnss-capture.schema.json` envelope, and the offline
  `multidetect-gnss-capture-verify` verifier.  The capture records
  `GPS_RAW_INT`/`GPS2_RAW` and `GLOBAL_POSITION_INT` with host receive time,
  WGS-84 coordinates, fix/satellite/accuracy fields and per-sample rejection
  reasons; it transmits zero MAVLink frames and marks hardware/REAL acceptance
  as `not_claimed`.  The verifier recomputes record counts, raw-GPS quality,
  decode errors, freshness and the two quality gates, so edited summaries,
  coordinates or receive-only flags remain fail-closed.
- GNSS capture thresholds may be tightened for a diagnostic run, but the
  canonical `hAcc<=6 m`, `vAcc<=10 m`, `GPS age<=1.5 s` policy cannot be
  relaxed: a report using looser limits records `threshold_policy_passed=false`
  and cannot pass the quality gate.
- Added `scripts/verify_gnss_capture.py` and linked the capture/verification
  commands from the field bring-up runbook and repository architecture.  The
  new regression suite covers valid/invalid wire values, exact-pole rejection,
  atomic output, tampered reports and wrapper execution without `PYTHONPATH`.
- Added `multidetect-target-survey-template` and
  `scripts/create_target_geolocation_survey_template.py`.  The helper verifies
  a target-event replay, exports only fresh/qualified events with their event
  IDs, source frames, estimated WGS-84 coordinates, GPS age and reported `1σ`,
  and leaves both independent ground-truth columns empty for RTK/survey input.
  CSV, JSON and JSONL outputs are written atomically; existing worksheets are
  protected unless `--overwrite` is explicit.  The sidecar/CLI summary is
  marked `contains_truth=false` and `gate_passed=false`, and an empty fresh
  selection returns exit code `1`, so a worksheet cannot be mistaken for REAL
  accuracy evidence.
- Added the strict JSON-envelope contract at
  `configs/schemas/target-geolocation-survey-template.schema.json`; CSV and
  JSONL remain row-oriented variants of the same truth-free worksheet.
- Replay-derived accuracy reports now include a strict optional `selection`
  object with source-manifest hash, eligible/matched/skipped counts and
  unmatched/unused IDs.  The report verifier checks the count-conservation
  equations while retaining compatibility with older reports that omit it.
- Tightened the SIM MAVLink adapter's numeric boundaries: boolean values are
  rejected for AI envelope limits, MAVLink system/component IDs, watchdog
  duration, and adapter timestamps instead of being silently coerced by
  Python's `bool`-is-an-`int` rule. Added regression coverage for the config,
  AI, watchdog, shutdown, and explicit-failsafe paths.
- Added `scripts/run_field_link_preflight.ps1` as a Windows one-shot field
  workflow. It resolves the repository and Python interpreter from the
  checkout, performs a non-mutating network-interface snapshot, runs the
  existing protocol/route preflight, and verifies the saved JSON. Diagnostic
  runs preserve `hardware_link_ready=false` with exit `0`; `-RequireReady`
  (or `-RequireAll`) turns readiness into a hard gate. The wrapper never
  changes IPs/routes or emits flight-control traffic.
- Verification after these changes: the complete Python suite is `2387
  passed, 1 skipped` (the skip is the existing Windows symlink environment
  gap), `ruff check src tests scripts` passes,
  `compileall` passes, and the repository whitespace check is clean.
- Saved the latest three-sample, route-aware desktop baseline to
  `artifacts/evaluation/field-link-preflight-20260902-desktop-continue.json`
  (16,806 bytes, SHA-256
  `98C6039B2E6988E066DA82C549530AB25C160DE6EFDAA0FEC50F21396D2A68BC`).
  The wrapper and report verifier both exited `0` in diagnostic mode, while
  all three remote Jetson/GR01/RTSP probes timed out and selected local source
  `192.168.1.13` outside `192.168.144.0/24`; `hardware_link_ready=false`
  remains fail-closed.
- Bound that report into a complete acceptance snapshot at
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-desktop-continue.json`
  (31,412 bytes, SHA-256
  `9E7347F288151F53245DD103242993183C38F6ED363079F4C53C63A3EC2A9E66`).
  The run collected 2,366 core tests and 66 QGC custom tests, with software
  status `pass`; the field verifier correctly returned `1` for the unready
  report and the aggregate stayed `hardware_status=offline_pending`.
- Rebuilt the E7 manifest against that latest field report instead of the
  earlier route snapshot. The manifest is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-desktop-continue.json`
  (11,459 bytes, SHA-256
  `29729C9201B2A755C4E4017B11958944B5DCB3C4AEEE73CB660F78468370B327`); its
  report-only aggregation is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-desktop-continue-report.json`
  (25,312 bytes, SHA-256
  `AC6A50DDAD83783FE3FED5F0984F5A9E04C306B9C510CB6782AF93A9B1B6E19E`).
  `gate_passed=false` and `release_allowed=false` remain expected because the
  route, replay accuracy, outdoor GNSS, HIL, REAL, and visual gates are still
  open; the current field report is now the hash-bound E7 input.
- Refreshed the deterministic SIM provenance on the same `v0.3.0-pre.8` source:
  the three-airframe native-QGC interaction artifact is
  `artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v2.json`
  (111,844 bytes, SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`), and the
  VTOL, fixed-wing, and multirotor 3-D control artifacts are respectively
  `simulator-3d-control-20260902-pre.8-continue-v2.json` (82,435 bytes,
  `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`),
  `simulator-3d-control-20260902-fixed-wing-continue-v2.json` (83,126 bytes,
  `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`), and
  `simulator-3d-control-20260902-multirotor-continue-v2.json` (83,089 bytes,
  `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`).
  All four report `gate_passed=true`; the 3-D files also report
  `deterministic_gate_passed=true` while keeping external runtime, visual,
  hardware, outdoor GNSS, HIL, and REAL claims fail-closed.

### v0.3.0-pre.8 field-link UAC convenience (2026-09-02)

- Added an explicit `-Elevate` path to `scripts/prepare_field_link.ps1`. A
  non-administrator PowerShell can request a UAC relaunch while preserving the
  interface/IP/prefix/remove arguments and propagating the child exit code;
  `-VerifyOnly` remains non-mutating and does not prompt.
- Re-ran the full goal acceptance after the UAC change. The current aggregate
  is `artifacts/evaluation/goal-acceptance-20260902-033125-field-link-uac-v3.json`
  (31,491 bytes, SHA-256
  `1292D7CB4A059DA47CA8BCCFC723855284998ED2D05627D84571C9334F627030`), with
  exit code `0`, 2,353 core tests, QGC custom `66/66`, and
  `hardware_status=offline_pending`.
- Added a bounded protocol discovery snapshot for the current desktop subnet:
  `artifacts/evaluation/field-link-discovery-20260902-033839-desktop-uac-protocol.json`
  (8,437 bytes, SHA-256
  `C5BB3A87128F452F8C9A7269CCDE44376AF77889B90AD5373EED61EED9A1F2E5`). Four SSH
  hosts responded, but no host exposed the expected `5760/MAVLink` or `554/RTSP`
  service, so the explicit aircraft endpoints and hardware gate remain unchanged.

### v0.3.0-pre.8 target-state evidence gate (2026-09-02)

- Added the optional `--require-target-state-evidence` gate to the field-session
  validator and capture path.  Qualified GPS and `RANGE_STATUS` samples now
  retain the most recent `DET/TRK/LCK/TGT` state context; a cancel event clears
  that context so post-cancel telemetry cannot be reused as evidence for the
  previous target.
- When enabled, a field session must contain at least one fresh, qualified GPS
  sample and one fresh, qualified range sample in the `TGT` context.  The
  setting is persisted in capture reports and re-applied by the provenance
  verifier, so verification cannot silently fall back to the legacy
  session-wide sensor gate.
- Added regression coverage for stale-at-TGT data, sensor-only records that
  inherit the TGT context, and cancel context clearing.  The default validator
  remains backward-compatible; real field runs should enable the strict flag
  together with `--require-link-preflight`.
- Replayed the seven-record cancel/reacquire fixture with the strict option:
  `artifacts/evaluation/field-session-software-contract-20260902-target-state-evidence.capture.json`
  (5,849 bytes, SHA-256
  `00D02947741E0FE677F5BAF91E7EA2F177E97EA7F797C498F186B916D647029D`). Both
  capture and provenance verification exited `0`; this remains a
  software-contract fixture rather than a field-hardware claim.
- Added `--real-target-state-evidence-report` to the E7 manifest writer. The
  binding stores the capture report hash, `DET/TRK/LCK/TGT` path and the
  fresh GPS/RANGE record indexes, while `check_release_gate.py` reruns the
  capture verifier on every evaluation. The final release channel now requires
  this strict TGT-session proof in addition to the REAL link and surveyed
  WGS-84 accuracy report; the prerelease remains backward-compatible when the
  optional binding is absent.
- Generated the non-destructive pre.8 bound diagnostic manifest
  `artifacts/evaluation/release-gate-pre.8-current-20260902-target-state-evidence-v1-bound.json`
  (SHA-256 `6AB1053D1DE5EE49BC81BE593210918186650990B28493F190DD33E7DC8A33DC`). Its
  target-state binding re-verifies successfully, while the aggregate remains
  `release_allowed=false` because the existing replay/REAL/HIL and operator
  acceptance gates are still pending.

### v0.3.0-pre.8 acceptance refresh (2026-09-02)

- Re-ran the complete acceptance after adding the field-link helper and its
  regression test. The latest aggregate is
  `artifacts/evaluation/goal-acceptance-20260902-031536-field-link-tool-v1.json`
  (31,965 bytes, SHA-256
  `08D0CEF2EEEDED68B79E28F7E40BA09337987D443DBDD2F5E50A9E23DFC23EF5`). It
  exited `0` with 2,353 core tests, QGC custom `66/66`,
  `software_status=pass`, and `hil_contract_acceptance.all_contracts_passed=true`.
  The bound Mode-2 and Mode-3 reports are 1,194/2,515 bytes with SHA-256
  `0E5BE4437D25B9BA97D4B21C6FC76CDDA506CA727C1E956032C51C3912CA2FD4` and
  `B731E41D9E9A9C23AB11FD912F80544E5158029F393340BA84E8D71BE7C65248`.
  The field route remains fail-closed (`192.168.1.13` selected instead of
  `192.168.144.0/24`; `hardware_status=offline_pending`).
- Regenerated the HIL release-mode envelope from the latest aggregate source:
  `artifacts/evaluation/release-mode-hil-20260902-031536-field-link-tool-v1.json`
  (622 bytes, SHA-256
  `1171756830158D276BEF0EE9FD8E4354F394A174C1CAB5589637D19991F6BE3A`).
  Rebound it into
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-tool-v1.json`
  (10,957 bytes, SHA-256
  `428A2E78B25C0FDFB9EC247D6DD9A4882077DF6A1A73A0D21DB6D2090CE442A8`).
  With `--evaluated-at-utc 2026-09-02T00:00:00Z`, report-only and strict
  outputs are byte-identical (22,852 bytes, SHA-256
  `A0D4073B90166D34DD1515C79B43E68F57D5CA2D14B46D58A2C1FA7E401EAE24`) and
  exit `0`/`2`; HIL source and SIM sub-gates remain green while the final
  release gate stays closed for missing visual/hardware/REAL/replay evidence.
- Added `scripts/prepare_field_link.ps1` for the migrated desktop's aircraft
  Ethernet setup. It is idempotent, parameterized, and changes only a
  secondary `192.168.144.250/24`-style host address with no gateway or DNS;
  `-VerifyOnly` is non-mutating and `-Remove` rolls back exactly the selected
  address. The current non-elevated attempt returned `Access denied`, so the
  route watch remains `route_checks_passed=false` until an operator runs the
  apply step from an elevated PowerShell after connecting the aircraft cable.
- Wired the Mode-2 payload and Mode-3 approach HIL-contract runners directly
  into `scripts/run_goal_acceptance.ps1`. Every aggregate now regenerates and
  checks a `hil_contract_acceptance` node instead of relying on manually joined
  standalone reports. The new node requires advisory/software-only boundaries:
  no flight control, no physical release, no real payload/actuator interface,
  and `advisory_only=true` for Mode 3.
- Fresh end-to-end aggregate:
  `artifacts/evaluation/goal-acceptance-20260902-030411-hil-contract-route-v1.json`
  (31,989 bytes, SHA-256
  `BDDF745A9586E67242DCDBD242AB2BFAF6CE86CDEB21C30F03BEBC03E91B737D`). The
  script exited `0` with 2,352 core tests, QGC custom `66/66`,
  `software_status=pass`, and `hil_contract_acceptance.all_contracts_passed=true`.
  Bound Mode-2 and Mode-3 reports are
  `...-mode2-payload.json` (1,195 bytes, SHA-256
  `8948429D6884D32AED11D47296A3D49055A98049831FC90ACC095C9487777997`) and
  `...-mode3-approach.json` (2,513 bytes, SHA-256
  `F9A1351A9C777E4F29229DD8BF9FD476FC68FD6080ECC60AA696D7447A7364D5`).
  These are HIL-contract/software evidence only and do not claim a real
  payload, actuator, Pixhawk, outdoor GNSS, or manual Fly View result.
- The same aggregate keeps the three-sample route watch fail-closed: all
  Jetson/GR01/RTSP endpoints selected `192.168.1.13` instead of
  `192.168.144.0/24`, so `hardware_link_ready=false` and
  `hardware_status=offline_pending`. SIM safety, the three-airframe scenario
  matrix, PX4 Mission transaction, and external-SIM revalidation remain green;
  final `v0.3.0` is still unreleased.
- Added an optional timezone-aware `--evaluated-at-utc` input to the release-gate
  evaluator. When a fixed timestamp is supplied, repeated checks of the same
  manifest and evidence produce byte-identical reports and SHA-256 values;
  the default remains the current UTC time, and no release sub-gate is
  bypassed. The current route-bound report was archived as
  `artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound-reproducible-report.json`
  (SHA-256
  `765F2752F9394953D799D23BC06B3323A400710A7CC06FBC94201280C11DCA9A`); it
  remains `gate_passed=false` while the aircraft subnet is offline.
- Re-ran the Mode-2 payload and Mode-3 approach software/HIL-contract scripts and
  archived fresh evidence:
  `artifacts/evaluation/mode2-payload-hil-20260902-024833.json` (SHA-256
  `C1F4CDA12388EB44E89063EE857A13366167AD9BEF2298AF0A3D35999FD77387`) and
  `artifacts/evaluation/mode3-approach-hil-20260902-024833.json` (SHA-256
  `4A71CAA628553427AEB428EC5AE89640D72E6882144334528281B2FA4F4FDD0E`). Both
  reports pass their deterministic contract checks: signed payload authorization,
  target selection/slide expiry and target-switch revocation for Mode 2; shared
  centering/aiming, fresh-challenge recovery and fail-closed lost/occluded/avoidance
  handling for Mode 3. They remain advisory/software-only with
  `flight_control_enabled=false`, `physical_release_enabled=false`, and no real
  camera, outdoor-GNSS, Pixhawk, HIL, or REAL claim.
- Re-ran the full `v0.3.0-pre.8` goal acceptance on 2026-09-02 and bound
  the current-day field-link preflight. The v6 snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-fault-coverage-v6.json`
  (36,169 bytes, SHA-256
  `0F59CEC1C31B2B30669598E064F71F217051E86BBD300304BB4B3CA3A4402AE1`),
  with script exit `0`, 2,337 core tests, QGC custom `66/66`, and software
  status `pass`. The bound field report is
  `artifacts/evaluation/field-link-preflight-20260902-continue-v1.json`
  (3,038 bytes, SHA-256
  `64C06112A967BC0C3BABE904C0588143C182F2A221262A0D97C062E6077119AD`);
  its structure/hash verifier passes, but the three field endpoints remain
  unreachable, so `hardware_link_ready=false`, `gate_passed=false`, and no
  outdoor-GNSS, Pixhawk, HIL, or REAL claim is made. The v6 snapshot keeps the
  combined SIM, S4 safety, three-airframe fault matrix, and PX4 Mission
  transaction bindings green.
- Rebound those v6 sources into
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound.json`
  (10,219 bytes, SHA-256
  `48A3D463A05FC30896B8651CB698E88FF242700A56B6CFEDFE83CB34BD78F648`).
  The report is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound-report.json`
  (19,798 bytes, SHA-256
  `647C0F2EE4514DDBF60F11AC83E14B01C339D39F484E683103C4C44050D3292F`),
  and the strict output is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound-strict.json`
  (19,798 bytes, SHA-256
  `12A3DDE12D57FF78FF0C5C5B22D4E880A122B2AD18F0230A99FDB293E8B49CB9`).
  Report-only exits `0`; strict evaluation exits `2` and keeps
  `release_allowed=false` for the offline field/REAL/replay-set, visual/
  hardware, and prerelease-channel gates. The v5 artifacts remain historical
  provenance.
- Exercised the read-only field-session capture/provenance path against the
  repository fixture and saved
  `artifacts/evaluation/field-session-software-contract-20260902-cancel-reacquire.capture.json`.
  The capture contains seven records with
  `DET -> TRK -> LCK -> TGT -> cancel_trk -> DET -> TRK`, a verified cancel/reacquire
  transition, six qualified GPS and RANGE samples, and matching source/output
  hashes; `verify_field_session_capture.py --require-gate` exits `0`. This is
  explicitly a software-contract fixture and does not claim a real camera,
  outdoor GNSS, or field-flight result.
- Recomputed the six-stratum SIM replay-set in report-only mode and saved
  `artifacts/evaluation/replay-set-accuracy-20260902-pre.8-report-only.json`
  (9,043 bytes, SHA-256
  `3DAAECBC63AC5BB512B60D9817DF5B1CFF0F0FA8995930D8749583A8C242B319`).
  The report is structurally valid with complete predictions and evaluated
  metrics, but deployment-domain coverage and independent accuracy review are
  still missing, so `accuracy_validated=false` and `gate_passed=false` remain
  intentional.
- Bound the same SIM replay-set and report-only accuracy evidence into a
  supplementary pre.8 E7 manifest:
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v7.json`
  (10,812 bytes, SHA-256
  `F4D3831F024D0714934C248425C4B758C6AF557D58BD5F3FA1F10CA488C272C1`).
  Its report-only and strict outputs are
  `...-v7-report.json` (SHA-256
  `E4C9048133FB17018361A9BE414DDFDCB92F879983B7DD007A81DF513573DD49`) and
  `...-v7-strict.json` (SHA-256
  `F6BA612293DE59CD44CB3ADD29BF7F1FADE0FEA6B36E981FBAABC7BCEB31BA7C`).
  The aggregate now records `replay_set.present=true` while keeping
  `replay_set.passed=false` for the missing deployment-domain coverage and
  independent review.
- Re-ran the default field-link watch for three samples and saved
  `artifacts/evaluation/field-link-preflight-20260902-continue-v2.json`
  (12,064 bytes, SHA-256
  `F7133D83E89F758A6C4819E71D185F228404DCCAC1A4BC713C7C61C4306F253D`).
  All three remote TCP/protocol probes timed out while local UDP 14580/14581
  binds succeeded (`passed_samples=0`, `failed_samples=3`,
  `hardware_link_ready=false`). The latest field report is hash-bound into
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v8.json`
  (SHA-256
  `A3052885BDDF7AB8513441E513A08AC57AE855F39C75E26EA59A5B1B9686BA7A`);
  report-only exits `0`, strict exits `2`, and the hardware/replay/final gates
  remain closed.
- Re-ran the full goal acceptance with the three-sample field watch bound:
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-field-watch-v7.json`
  (36,154 bytes, SHA-256
  `B240A5999B64C3EEE582EB74CDBCE75A90012FDD0BD470F91CFF30AFCBA2077A`).
  The script exits `0` with 2,337 core tests and QGC custom `66`; software is
  `pass`, while hardware remains `offline_pending` and the field preflight
  strict result remains `1`.
- Added an opt-in passive local-route gate to the field preflight. The new
  `--require-route --expected-source-cidr <aircraft-cidr>` options record the
  operating system's selected source address for Jetson, GR01 and RTSP without
  sending application data, and fail closed when a VPN/TAP or migrated NIC
  selects the wrong subnet. The current desktop diagnostic is
  `artifacts/evaluation/field-link-preflight-20260902-route-v1.json`
  (4,396 bytes, SHA-256
  `21DAD57EA9CF8AD1FA9996F8110193BEA3535E270F7E87C5D7A100A4BA699B13`): all
  three endpoints selected `192.168.1.13` instead of the expected
  `192.168.144.0/24`, so strict readiness exits `1` and no hardware/REAL claim
  is made. Route evidence is independently checked by
  `verify_field_link_preflight.py --allow-unready`.
- Bound the route-enabled field report into
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v1.json`
  (SHA-256 `4A11A7ED8F7DE6422640783349343751B8A011192277AD613992531DBC64F8BB`).
  The complete software acceptance exits `0` with 2,348 core tests and QGC
  custom `66/66`; the bound route evidence remains `hardware_link_ready=false`,
  so HIL/REAL and outdoor-GNSS gates stay closed.
- Rebound the same route report after extending `run_goal_acceptance.ps1` with
  `route_checks_requested`, `route_checks_passed`, and `route_source_cidr`.
  The latest snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v2.json`
  (29,706 bytes, SHA-256
  `C7DECB029209C4CA6D05CB74D1B951E89811424C884FA0F051D1F5378E87BEE4`).
  It exits `0` with 2,348 core tests and QGC custom `66/66`, while explicitly
  retaining `route_checks_passed=false` and `hardware_status=offline_pending`.
- The same acceptance run generated the fresh external-SIM revalidation
  binding
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-acceptance-20260902-pre.8-v2.json`
  (534 bytes, SHA-256
  `B5D08D43A9C74B67C12A6BF70798BCE04557E316E4BBD1418253DE69AA4E7A8C`).
  Its `source_recomputed=true` and `gate_passed=true` fields remain a
  software-only provenance result and do not alter the offline hardware gate.
- Re-ran the complete three-sample route/protocol watch after the desktop network
  check. The report is
  `artifacts/evaluation/field-link-preflight-20260902-025107-route-continue.json`
  (16,803 bytes, SHA-256
  `0F2743810B7DC5CA6554A960B38C8876110FD980B21B062248BC2E76029F77FE`). All
  Jetson/GR01/RTSP probes timed out and all three route probes selected
  `192.168.1.13` instead of `192.168.144.0/24`; local UDP `14580/14581` binds
  succeeded. The structural verifier passes with `--allow-unready`, while
  `hardware_link_ready=false` and strict readiness remain closed.
- Bound that route watch into a fresh goal-acceptance snapshot:
  `artifacts/evaluation/goal-acceptance-20260902-025359-route-continue.json`
  (29,845 bytes, SHA-256
  `601FAA324BE21B309EC378302114150B142635B66C1F989453EE5A1721B3D6EB`). The
  script exited `0` with 2,352 core tests and QGC custom `66/66`; software is
  `pass`, external-SIM revalidation is `gate_passed=true`, and the field
  binding records `route_checks_passed=false`, `hardware_link_ready=false`, and
  `hardware_status=offline_pending`. No outdoor-GNSS, Pixhawk, HIL, or REAL
  claim is made.
- Rebound the same source set into the current pre.8 E7 manifest
  `artifacts/evaluation/release-gate-pre.8-current-20260902-route-continue.json`
  (10,925 bytes, SHA-256
  `4F389AEE91ADEEB79F4CDC9E200643F4DDB5F52D9E1A770C8D84148F9EECA9F2`). With
  `--evaluated-at-utc 2026-09-02T00:00:00Z`, report-only and strict reports are
  byte-identical (22,816 bytes, SHA-256
  `3A4DD9D10BDB34FAD12EF2878633DC956D315FFB4784D0B25FDE40507419EC35`); exits
  are `0` and `2`, respectively. SIM safety/scenario/PX4 transaction and
  external-SIM revalidation remain green, while route/GNSS, replay accuracy,
  visual, HIL/REAL, and prerelease gates remain closed.
- Extended the E7 `real_field_link` writer, evaluator, and schema to retain
  the passive route summary (`route_checks_requested`, `route_checks_passed`,
  and `route_source_cidr`). New route-enabled REAL manifests are now checked
  against those fields on every re-evaluation; historical manifests without
  them remain readable.
- Tightened the final `release` channel so a passing REAL field-link binding
  must include route provenance with `route_checks_requested=true`,
  `route_checks_passed=true`, and a non-empty aircraft `route_source_cidr`.
  Historical prerelease bindings remain readable, while a release manifest
  without this evidence stays blocked.
- Generated and re-evaluated an end-to-end route-bound E7 pre.8 sample:
  `artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound.json`
  (10,924 bytes, SHA-256
  `E9DCE9776A50C6957479D26992576146A3DA1E9378CF976892FB11F85B3F1449`). The
  report-only output is
  `...-route-bound-report.json` (22,755 bytes, SHA-256
  `A174F8A999777737C5737C5B91E4908A08B12F7741CDFC5A52405AA5EA96A1AD`), and
  the strict output is
  `...-route-bound-strict.json` (22,755 bytes, SHA-256
  `73251284191B662184E4E9BF6E3BB8B18B64DE9968D7FB3B91E458C0B7B128FD`).
  Report-only exits `0`; strict exits `2` with
  `gate_passed=false`, `release_allowed=false`, and
  `real_field_link.passed=false`, retaining
  `route_checks_requested=true`, `route_checks_passed=false`, and
  `route_source_cidr=192.168.144.0/24` because the desktop selected
  `192.168.1.13` instead of the aircraft subnet.
- Re-ran the deterministic 3-D control acceptance for the `river_rescue_vtol_001`
  scenario. Source evidence is
  `artifacts/evaluation/simulator-3d-control-20260902-pre.8-continue-v1.json`
  (82,463 bytes, SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`).
  The deterministic protocol/asset gate passed and proved shared QGC manual,
  AI TRACK, manual takeover, AI resume, and watchdog/failsafe behavior through
  the same adapter and flight model; external 3-D runtime and visual/hardware
  acceptance remain explicitly `not_claimed`.
- Re-ran the native QGC interactive acceptance with `--mode all` for VTOL,
  fixed-wing, and multirotor. Evidence is
  `artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v1.json`
  (111,844 bytes, SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`).
  All three mode gates passed with manual-priority, AI-resume, watchdog, and
  QGC-only loopback checks. This closes a software/SIM interaction sub-gate,
  not the external PX4/Gazebo, Pixhawk/Jetson/camera, outdoor-GNSS, HIL, REAL,
  or human Fly View gates.
- Re-ran the digest-pinned Docker PX4/Gazebo external runtime and live-AI
  target exercise for all three airframes with per-job loopback ports. The
  VTOL source pair is
  `simulator-docker-acceptance-20260902-pre.8-desktop-v2.json` (21,895 bytes,
  SHA-256 `E9A95A852BBB91171E88281616A896AC8277D151C57B81DBCBE0E51A8B2802B3`)
  and `simulator-docker-ai-acceptance-20260902-pre.8-desktop-v1.json`
  (108,917 bytes, SHA-256
  `1C12C3699348C18AEF29321975511F08C281A8C26D827EFB5992D88ECB7E04F0`). The
  fixed-wing pair is
  `simulator-docker-acceptance-20260902-pre.8-fixed-wing-v1.json` (22,090
  bytes, SHA-256
  `B5D46C2C8820CF5BFC552015AD8522AE5A53AF272749A882693D017B41BF9385`) and
  `simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-v1.json` (104,476
  bytes, SHA-256
  `1A9A62C2089D4866928036AF2BCCA38A567E36156823AFD9794B4F310A2B88F7`). The
  multirotor pair is
  `simulator-docker-acceptance-20260902-pre.8-multirotor-v1.json` (22,091
  bytes, SHA-256
  `7E2338F0CA45E7886A5294DEE1E612C0B1315C8A5F6A0660A113C4E8AB775F26`) and
  `simulator-docker-ai-acceptance-20260902-pre.8-multirotor-v1.json` (157,637
  bytes, SHA-256
  `F7E1466DFE29B20912EF4FD57DE5D3AF358B118D3D9F0EC741CB27C478F289B9`).
  Every runtime reports qualified GPS (`fix_type=3`, 10 satellites), heartbeat,
  loopback I/O and cleanup; every AI run records two accepted policy steps,
  `target_exercise.accepted=true`, and `decision.action=track` from the shared
  control adapter.
- Joined those six source files with the same `configs/release-parity.json`:
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-v2.json`
  (5,383 bytes, SHA-256
  `656531912FC0FCF8AF975469CBB0C340136CF4D8515BECE5BFC1827858BDDC35`),
  `gate_passed=true`. The join is a file-level machine gate with
  `truth_free=true`, `hardware_control_disabled=true`, and visual/real-hardware
  fields explicitly `not_claimed`; it does not close outdoor GNSS, Pixhawk/
  Jetson/camera, HIL, REAL, or human Fly View acceptance.
- Fixed the dynamic telemetry-port seam in `src/multidetect/simulator/stack.py`:
  a job-selected loopback telemetry port now explicitly remaps the canonical
  PX4 `14541` sensor endpoint and records `sensor_port_bindings` in the stack
  plan. Unrelated MAVLink ports remain fail-closed. Regression coverage is in
  `tests/test_simulator_stack.py`, and all three current-day external runs use
  the remapped, isolated ports.
- Added a second-pass join revalidator. `--join` reads the six source paths and
  parity path recorded in an existing external-SIM join, recomputes every source
  hash/byte count and gate-bearing field, and fails closed on edited, moved or
  replaced inputs. The current result is
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-v1.json`
  (534 bytes, SHA-256
  `8366977C0F3800D09C348DCA629DA43FD8DFC9815DD9009D03F0FB2E3CAC8C18`), with
  `source_recomputed=true` and `gate_passed=true`; its contract is defined in
  `configs/schemas/simulator-external-evidence-revalidation.schema.json` and
  covered by `tests/test_simulator_external_evidence.py`.
- Bound that revalidator into `scripts/run_goal_acceptance.ps1`. Supplying
  `-SimulatorExternalEvidence` now records the join and a fresh revalidation
  artifact under `simulator_external_evidence`; `-RequireSimulatorExternalEvidence`
  promotes a missing, stale, edited, or mismatched join to a fail-closed
  acceptance error. The refreshed snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-external-revalidation-v1.json`
  (29,246 bytes, SHA-256
  `73F85C769FDE560294575DC22140C67A85CD6D82717F6475088F40396C31F506`),
  produced with exit `0`, 2,343 core tests and QGC custom `66/66`.
  Its external binding reports `provided=true`, `verified=true`,
  `source_recomputed=true`, `gate_passed=true`, while software remains `pass`
  and hardware remains `offline_pending`. The required-mode negative probe
  exits `1` when no external path is supplied, confirming the new gate is not
  silently optional when explicitly required.
  The bound revalidation output is
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-acceptance-20260902-pre.8-v1.json`
  (534 bytes, SHA-256
  `CA6A73B6012FC28669F696CB5C3D916BA8E7E10F8F59315D8ACDC9A90F9C442A`).

### Deterministic SIM scenario matrix (2026-09-01)

- Added `python -m multidetect.simulator scenario-matrix`, the equivalent
  `scripts/run_simulator_scenario_matrix.py` wrapper, and the
  `multidetect-sim-scenario-matrix` console entry. The default run discovers
  all repository-owned fixed-wing, VTOL, and multirotor manifests, executes
  them through the shared deterministic runner/clock/authority/MAVLink path,
  and repeats each run to compare evidence hashes.
- Matrix summaries now expose complete-run AI attempted/accepted/rejected/
  failsafe counters and control-message counts. The shared AI path claim is
  true only when AI is enabled and every selected scenario accepted at least
  one AI command; `--no-arm` and `--no-ai` therefore remain truthful
  fail-closed checks.
- Added the strict aggregate schema at
  `configs/schemas/simulator-scenario-matrix-evidence.schema.json` and
  regression coverage for discovery, repeatability, selection, wrapper
  invocation, schema shape, and no-arm/no-AI claims.
- Current evidence is
  `artifacts/evaluation/simulator-scenario-matrix-20260901-pre.8.json`
  (11,494 bytes, SHA-256
  `95847C56FB0AAB7FA4CE459B4C43CA15B73D46B8F4F6834455A7CD61E73323E9`),
  with `gate_passed=true` and `deterministic_gate_passed=true`. The matrix
  remains software-only: external 3-D rendering, QGC visual acceptance,
  hardware, HIL, REAL, and real-flight claims stay `not_claimed`.
- Fault timeline coverage is now an explicit opt-in gate. `--cover-faults`
  inspects the runtime channels, extends the shared frame budget through the
  latest declared event (including a one-second tail for indefinite faults),
  and requires every finite or indefinite `FaultEvent` to reach an actual
  observation. The runner now marks `fired=true` for finite windows as well as
  persistent events, so `active_frames` and `fired_event_ids` cannot disagree.
  The three-airframe coverage artifact is
  `artifacts/evaluation/simulator-scenario-matrix-20260901-pre.8-fault-coverage.json`
  (13,512 bytes, SHA-256
  `8DD6171CB60528B800BEEF98021E7C83AC00580792016A14EF27AFB94D531A7E`), with
  2,611 frames and 9/9 declared fault events exercised; the independent
  verifier exits `0`. `run_goal_acceptance.ps1` now uses this coverage mode,
  and the latest snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-fault-coverage-v5.json`
  (36,169 bytes, SHA-256
  `7DC28AA4A36204C82C58680E7637C873859B93F1298762F6BC3EB2A072B72C41`),
  with 2,337 core tests, QGC custom `66/66`, and per-mode 4/4, 2/2 and 3/3
  fault coverage; software is `pass`, while the real field link remains
  `offline_pending`. The v5 snapshot also requires and re-verifies the
  combined PX4/Gazebo/QGC source and its release-mode envelope:
  `artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
  (333,051 bytes, SHA-256
  `75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`,
  `gate_passed=true`) and
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
  (730 bytes, SHA-256
  `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`).
- The v5 goal-acceptance snapshot binds the verified read-only field preflight
  `artifacts/evaluation/field-link-preflight-20260901-continue-v2.json`
  (4,987 bytes, SHA-256
  `13BB57F062F2C9DA687EECBAC3616A1C73291B837FE134DAFBEA20505EEE6328`).
  The report verifier exits `1` because Jetson, GR01, and camera endpoints are
  unreachable; `hardware_link_ready=false` and `gate_passed=false` remain
  truthful, with no REAL/HIL or outdoor-GNSS claim.
- Rebound the same v5 evidence into the current pre.8 E7 manifest:
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound.json`
  (10,219 bytes, SHA-256
  `C5892E0515D1C6C26199E35CBC80CB405880D5D6C35A0AB045F70EAA7E121393`). Its
  report is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound-report.json`
  (19,841 bytes, SHA-256
  `988A88232C325B3FE787FAAA314AC5AFA1AEA0446D031B6DA3FAF2E88AE8EBFD`):
  combined SIM, S4 safety, three-airframe fault matrix, and PX4 Mission
  transaction all pass; strict evaluation exits `2` and keeps
  `release_allowed=false` for the offline field/REAL/replay-set, visual/hardware,
  and prerelease-channel gates. The strict output is preserved as
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound-strict.json`
  (19,841 bytes, SHA-256
  `09212FD35D096BBDAD045DEAD74BA727037A38F74F1100169D9C0CFB143A1B26`).
- Performed a bounded, read-only discovery of the active `192.168.1.0/24`
  interface after the v4 run. The scan covered 254 hosts, found four SSH
  banners, and found no MAVLink `5760` or RTSP `554` service candidates. The
  report is
  `artifacts/evaluation/field-link-discovery-20260901-continue-v1.json`
  (8,045 bytes, SHA-256
  `03EBA28A99C34420B987ADAB2AC29CED54A5363FE9E4D0C0934AC00FF203CE30`);
  its structure verifier passes with `--allow-unready`, while the hardware
  link remains `false`. Discovery is diagnostic only and never rewrites the
  configured Jetson/GR01/camera endpoints.
- The matrix schema and verifier record the requested versus auto-extended
  frame budget, coverage requirement, active/fired/uncovered event IDs and
  coverage ratio. A short run with `--require-fault-coverage` therefore fails
  closed instead of silently reporting nominal-only coverage.
- Final-channel verifier and manifest-writer paths now force both
  `fault_coverage_required=true` and `fault_coverage_auto_extended=true`.
  A hash-consistent but nominal-only matrix is rejected before a final
  `0.3.0` manifest can be written or evaluated; prerelease manifests retain
  the backward-compatible optional policy.
- Re-evaluated the current E7 matrix-bound prerelease manifest with the new
  policy. The report is
  `artifacts/evaluation/release-gate-pre.8-current-20260901-fault-coverage-matrix-bound-v2-report.json`
  (15,470 bytes, SHA-256
  `C7D92E59C8B24C41B04F8CD4DC9246C34ADA674A7D7E91361551336C96993EAD`): the
  three-airframe matrix and 9/9 fault coverage pass, while aggregate
  `release_allowed=false` remains expected for the offline hardware and
  prerelease channel.
- Added the strict `simulator_scenario_matrix` binding to the E7 release-gate
  writer/evaluator and `configs/schemas/release-gate-manifest.schema.json`.
  `--simulator-scenario-matrix-evidence` records the source byte count/SHA-256,
  embedded canonical digest, all three airframe modes, AI/arm/repeatability
  switches, and verifier checks; the evaluator re-hashes and re-verifies the
  source on every read. It is optional for backward-compatible prerelease
  manifests and required for the final `release` channel.
- Added `scripts/verify_simulator_scenario_matrix.py` and wired the matrix run
  plus independent verification into `scripts/run_goal_acceptance.ps1`, so each
  total acceptance snapshot carries an auditable three-airframe SIM sub-gate.
- Added a no-`PYTHONPATH` verifier-wrapper regression and a final-channel guard
  proving that four-mode evidence alone cannot enable `release` without the
  hash-bound three-airframe matrix. The full current regression is
  `2336 passed, 1 skipped` (exit code `0`); the captured output is
  `artifacts/evaluation/pytest-full-20260901-final-fault-coverage-policy.log`
  (2,901 bytes, SHA-256
  `F9CDC0D3CC26F276B13585B11923199780E4EFE949559D28CDB1A03B54C0D9F8`).
  The remaining skip is the explicit Windows symlink environment gap and is
  not a release claim; the optional onnxruntime tests ran in this environment.

### Field-session target binding hardening (2026-09-01)

- Tightened `field_session_acceptance` so a qualified field-session window
  requires one identical target identity across GPS and RANGE samples. A
  non-empty set intersection is no longer sufficient; partial or multi-target
  identity sets now remain fail-closed and are surfaced in
  `target_binding.cross_record_mismatches` / `multiple_target_ids`.
- Added a regression covering GPS/RANGE samples arriving in separate records
  with a misleading partial overlap.
- The refreshed goal-acceptance snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-binding-v9.json`
  (23,268 bytes, SHA-256
  `7CC6B274EE6CA188A718B7DBBD306A514A92750882D5FC7D75C19B61056A6538`). It
  collected 2,319 core tests and QGC custom 66/66 with software status `pass`;
  the field link remains `hardware_link_ready=false` / `offline_pending`.

本节包含截至 2026-09-01 的 `v0.3.0-pre.8` 增量；版本清单仍保持
`release_label=0.3.0-pre.8`，未创建 `pre.9` 或最终 `v0.3.0` 标签。

### 离线 HIL/飞行器契约复核（2026-09-01）

- 重新执行 Mode-3 目标接近与失锁 fail-closed 验收，输出
  `artifacts/evaluation/mode3-approach-hil-20260901-continue.json`（2,512 bytes，
  SHA-256 `E7BFC8717F9B5BE4CCACCDB31C834ABCE0D1602F703D208F8F35B733F2690634`）。
  任意目标均保持 advisory-only；目标丢失、遮挡、切换和 avoidance 失败都会停止盲进近，
  旧确认不能复用，恢复需要新挑战值。
- 重新执行 Mode-2 payload 选择、连续滑动、授权分离和假释放回路，输出
  `artifacts/evaluation/mode2-payload-hil-20260901-continue.json`（1,194 bytes，
  SHA-256 `CE623AE49803339E60A1E1A545C4F1757FF825303011507195F97314212BF351`）。
  HMAC/MAVLink2 签名回路通过；人、普通车辆、目标切换和过期滑动均按策略拒绝，
  物理释放保持关闭。
- 重新执行固定翼 DET→TRK→LCK→TGT、姿态限幅、RC 取消和失锁恢复，输出
  `artifacts/evaluation/fixed-wing-software-20260901-continue.json`（1,950 bytes，
  SHA-256 `7B9737AD76275FF85B9E8F9BCB59420E0A75A414D46F35EF6417E4D03AFC6FE8`）。
  `gate_passed=true`、四元数归一化和滚转/俯仰限幅通过，取消后保持绑定锁存，未打开
  Pixhawk 链路，也未发送物理 MAVLink 输出。
- 重新执行多旋翼 `MISSION_ITEM_INT` 编译契约，输出
  `artifacts/evaluation/multirotor-mission-compile-20260901-continue.json`（1,320 bytes，
  SHA-256 `ADF8BAB8D4887697A1CF09E54AB8C21CEBDD9ACA0312B0A89514FA613F14DAB2`）。
  `takeoff→waypoint→loiter→land` 顺序、连续序列、计划哈希绑定、协议哈希和物理输出关闭
  均通过；该回路仍是进程内软件/SIM 门，不代表真实 Pixhawk 或实飞。
- 重新执行固定翼 `MISSION_ITEM_INT` 编译契约，输出
  `artifacts/evaluation/fixed-wing-mission-compile-20260901-continue.json`（1,263 bytes，
  SHA-256 `EBC526DEEFD443E06A4F2B782A71D0520BCD59C193DB50282064C129FCE1EA3D`），并用同一
  journalized upload/readback/commit/start-permit 路径重跑 VTOL、固定翼和多旋翼，输出
  `artifacts/evaluation/px4-airframe-transaction-20260901-continue.json`（11,947 bytes，
  SHA-256 `5B644637B85C8284EDF7120696F7DA81A85DEABA439039B10F231FE232513811`）。三机型
  entry/checksum/ACK/读回/终止检查全部通过，物理输出仍关闭。
- 重新执行 SIM S4 安全门，输出
  `artifacts/evaluation/simulator-safety-20260901-continue.json`（6,990 bytes，SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。它重新验证
  target-specific cancel、旧目标证据清理、假 GPS/地理围栏 fail-closed、watchdog neutral、
  failsafe 后禁止 AI 控制、loopback allow-list 和三机型 interactive authority。
- 用当前 portable `v0.3.0-pre.8`、现场预检和三机型 combined SIM 证据重新执行总验收，
  输出 `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v13.json`
  （23,188 bytes，SHA-256
  `93F3124E1372FAA769856E919FA1A214D78358F9AF15F7BD55AB502CC62B99E8`）。核心测试收集
  `2319`、QGC custom `66/66`、`git_diff_check=pass`、操作员密钥就绪；软件状态为 `pass`，
  现场端点仍为 `hardware_link_ready=false` / `hardware_status=offline_pending`，因此发布门
  继续保持关闭。
- 在台式机当前 `Ethernet=192.168.1.13/24` 上执行一次显式、只读的服务发现，输出
  `artifacts/evaluation/field-link-discovery-20260901-desktop-continue.json`（7,181 bytes，
  SHA-256 `0BD37CB16B5B7E1AD6B7BA4700FBEF5CBEF49AA951863A39DC17F7964DD22692`）。扫描 254
  个地址，发现 4 个 SSH 响应主机，但 `5760` MAVLink 与 `554` RTSP 均为 0 个服务命中；
  发现结果不替换配置端点、不改变 `hardware_link_ready=false`，也没有发送控制数据。
- 修正 E7 清单对上一份 PX4 transaction 文件的陈旧 SHA 绑定，重新生成
  `artifacts/evaluation/release-gate-pre.8-current-20260901-desktop-v13.json`（7,901 bytes，
  SHA-256 `B5BD654F325A2F15018142E4C1D9E913345C28306E4E38D59C7266B2BC4A6485`），并以
  `scripts/check_release_gate.py --report-only` 复核，报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-desktop-v13-report.json`
  （16,480 bytes，SHA-256
  `501C653D2F8DE7EA880C9ED1DE8BE17EC285DC651C18A3A57E68C3B2A6FB87DD`）。当前清单的
  replay/sim/HIL、combined SIM、S4 和三机型 transaction 子门均通过；阻断原因准确收敛到
  `hardware_link_ready=false`、REAL source 未通过、缺少 replay-set/目标精度绑定及
  prerelease channel，未再包含陈旧 transaction 哈希错误。
- 对同一清单再跑严格模式（不带 `--report-only`）返回退出码 `2`，并生成
  `artifacts/evaluation/release-gate-pre.8-current-20260901-desktop-v13-strict.json`
  （16,480 bytes，SHA-256
  `6BDCCD4BF9BB45B879C82F28D7C631203B50604587367C968489EB35474BBE00`）。这确认发布门
  在当前现场/版本条件下确实 fail-closed，而不是仅靠文档标注 pending。

  上述离线证据只刷新可重复的 software/HIL-contract 子门，不升级相机、Jetson、室外 GNSS、
  执行器、HIL 台架、REAL 或人工 Fly View 验收；开发版本继续为 `v0.3.0-pre.8`。

### 台式机 Docker 外部 PX4/Gazebo 与 live AI target 重跑（2026-09-01）

- 在当前台式机 Docker Desktop 上使用固定镜像
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  完成外部 PX4/Gazebo/UDP runtime gate。证据为
  `artifacts/evaluation/simulator-docker-acceptance-20260901-desktop.json`
  （21,876 bytes，SHA-256
  `C4D191D46604BF86D1BD2BBA64F3907E2C35AB3F1F3369BEFFFCCA709305A045`），
  `gate_passed=true`、`preflight_gate_passed=true`、`runtime_gate_passed=true`、
  `telemetry.qualified=true`，容器 stop/rm 清理成功；观察到 PX4 HEARTBEAT 和
  `GPS_RAW_INT fix_type=3 / satellites_visible=10`。
- 同一场景和镜像执行 `--execute --exercise-arm --exercise-target` 的 live AI 闭环，
  证据为 `artifacts/evaluation/simulator-docker-ai-acceptance-real-20260901-desktop.json`
  （106,789 bytes，SHA-256
  `B53ECA7F8D8F0C4D830C656FA22D3C1C39BD6DAA1E0C8D5719CDE3A6C68733B7`）。
  `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、`ai_steps=2`；
  ARM 在新鲜 armed heartbeat 后 3 次尝试成功，AI 通过共享
  `SimulatorMavlinkControlAdapter` 发出 2 个 `MANUAL_CONTROL`，truth-free 目标观测
  被接受为 `TRACK`，随后 DISARM 与清理均成功。
- 将本次台式机 VTOL runtime/AI pair 与已通过的 fixed-wing、multirotor pair 按同一
  `configs/release-parity.json` 做三机型文件级 join，输出
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-desktop-v2.json`
  （5,370 bytes，SHA-256
  `C2FFB02152A96026B639CCA7EE50C243B22BEC94A5FF41A0421A6A409DEA2382`），
  `gate_passed=true`；join 重新检查了三机型 manifest/digest、pre.8、GPS、truth-free
  perception、AI `TRACK`、armed heartbeat、硬件控制边界和容器清理。
- 随后执行增量 Windows Release/portable 构建，Ninja 返回 `no work to do`，构建与
  `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` 均以
  退出码 `0` 完成。Release 与 portable 主 EXE 均为 46,575,616 bytes，SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；本次启动日志
  `artifacts/evaluation/qgc-simple-boot-20260901-desktop-rerun.log` 为 220 bytes，SHA-256
  `CC8B17466CA7D567A21E56563650BA8A69EDEFB4290A7BFD0857582D86767051`。
- 按当前工作树重新执行完整 goal acceptance，输出
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v11.json`
  （14,892 bytes，SHA-256
  `2323A780DA7D62EC3AE267A4871B2D51FB7EBDF757294783FD300336D658158C`）。核心测试收集
  `2319`、QGC custom `66/66`，软件状态为 `pass`，操作员凭据 `ready=true`；现场链路仍
  `field_link_ready=false` / `hardware_status=offline_pending`。
- 将该现场预检作为可选 source 绑定到新的总验收快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v12.json`
  （23,195 bytes，SHA-256
  `D57149544BFA4CE614F42B8751E93CA5156DFE328D279EF48F793B994FC91F6A`）。快照重新收集
  核心测试 `2319`、QGC custom `66/66`，并记录 `field_link_preflight.provided=true`、
  `verified=true`、`hardware_link_ready=false`、`gate_passed=false`；总体软件状态仍为
  `pass`，未升级硬件门。
- 随后执行有界、只读的现场链路预检，证据为
  `artifacts/evaluation/field-link-preflight-20260901-desktop-rerun.json`（4,982 bytes，
  SHA-256 `0A9C8561E2E91886A1555CE6F2BCE4AF2E1F720DCEC4AC969FFD5561F4A9586B`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 均超时，
  本机 `14580/14581` 可绑定；`--require-all` 在首个失败样本后停止，
  `samples_requested=3`、`samples_collected=1`、`hardware_link_ready=false`。该报告只记录
  网络事实，不改路由或发送控制，也不改变 software/SIM 通过状态。
- 这批最新证据只关闭 SIM external runtime 与 AI/target policy 子门；仍明确保持
  `hardware/visual/real_control=not_claimed`，不把合成 perception 升级为相机精度、Jetson、
  室外 GNSS、真实 Pixhawk/执行器、HIL、REAL 或人工 Fly View 验收。版本仍为
  `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。

### 签名 live-control 用户级密钥配置器（2026-09-01）

- 新增 `scripts/provision_multidetect_operator_keys.ps1`：使用系统随机数生成至少 32 字节的
  `MULTIDETECT_OPERATOR_KEY` 和固定 32 字节/64 hex 的
  `MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX`，只写 Windows `User` scope。
- 配置器默认复用已有合法值，只有显式 `-Force` 才轮换；读回用户 scope 后重新校验长度和格式，
  输出仅包含创建/复用状态、字节数和 SHA-256 指纹，原始密钥不进入终端、README 或证据文件。
- `-PassThru` 提供机器可读 JSON，执行后需重启终端和 QGC；新增静态回归
  `tests/test_operator_key_provisioning.py` 并通过 Windows PowerShell 解析检查。
- `run_goal_acceptance.ps1` 的 live gate 提示现在直接指向该配置器；凭据准备仍不等价于现场网络、
  室外 GNSS、真实 Pixhawk/执行器或 HIL/REAL 验收。
- 修复 `remaining_live_gates` 在条件项为空时被 PowerShell 序列化为 `{}` 的问题；最新不带
  `-AllowMissingOperatorCredentials` 的快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-key-provisioning-v8.json`
  （23,276 bytes，SHA-256
  `094ECA0E16A33D4C3F81171B6AF2A7366BA296FDE1491DF2912247ED16A464CA`）。该快照收集核心测试
  `2318`、QGC custom `66/66`，凭据 readiness 为 `true`，现场仍为
  `hardware_status=offline_pending`、`hardware_link_ready=false`。

### 联合 SIM 重跑与 QGC 精确窗口捕获稳定化（2026-09-01）

- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 的 QGC 精确客户端抓帧现在对
  Windows 前台切换、Explorer/XAML 瞬时抢焦点和桌面合成时序执行最多 5 次有界重试：每次都
  重新提升并验证同一个 QGC PID/HWND，再重新检查外部顶层遮挡和 5×5 所有权网格；持续遮挡
  仍然 fail-closed，不通过白名单掩盖真实遮挡。Gazebo image、CameraInfo、NavSat 发布器也
  必须连续两次稳定存在后才启动传感器流，避免订阅器启动竞态。
- `vtol_sitl_attached_acceptance.py` 在 ARM/DISARM ACK 后等待新鲜 HEARTBEAT 的 armed bit，
  `vtol_sitl_mission_acceptance.py` 修正 durable journal `resume_plan()` 调用；这两处分别
  收口异步 PX4 ACK 竞态和 Mission journal 恢复摘要错误。
- 当前 portable `v0.3.0-pre.8` 上的 900 帧 Docker PX4/Gazebo/QGC source evidence 为
  `artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
  （333,051 bytes，SHA-256
  `75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`），顶层
  `gate_passed=true`。传感器发布器稳定屏障、RTSP 900/900、QGC 原生 VideoOutput 像素与
  type-24 绑定、toolbar drawer、VTOL `MC→FW→MC→DISARM`、7 项 INT-only Mission、journal
  hash-chain、动态 loopback 端口和 Docker 清理均通过。
- 独立 verifier 输出
  `artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence-verification.json`
  （2,591 bytes，SHA-256
  `18FDB7E6E5302229D2244075C26998D8D66E7C7EEBD4FCFB24A6A35111761960`），要求 release-mode
  envelope 后 40 项检查全部为 `true`。对应新的 SIM envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
  （730 bytes，SHA-256
  `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`）。
- 新的 pre.8 E7 清单/聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5.json`
  （7,887 bytes，SHA-256
  `629237034B84498537C07CB7885BCB6F28D20FCD4E8EEA4CAD5D43ACD9618A6A`）和
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5-report.json`
  （15,834 bytes，SHA-256
  `59C6A45CCC08084683485E39F3D38EFE7869645640C51E7427C41E8ADF5958BD`）。SIM、S4 和三机型
  Mission 子门为真；REAL 现场链路仍为 `hardware_link_ready=false`，总报告继续
  `gate_passed=false` / `release_allowed=false`。
- 绑定上述 source/envelope 的最新 goal-acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v5.json`
  （11,200 bytes，SHA-256
  `E4690933BC325D1CE2828DDAAC63C65689832AB8E1AFD07AB42AA9D397DE2447`）：核心测试收集
  `2315` 项，QGC custom `66/66`，脚本退出码 `0`，软件状态
  `pass_with_environment_gap`、硬件状态 `offline_pending`；它绑定了
  `field-link-preflight-20260901-final-continue.json`（SHA-256
  `59F85CEF567B198E12464B306E74DC36177CB5441F997B41A9034F3728025D54`），三处远端端点仍超时。
  桌面 BitBlt 视频 ROI 仍单独
  记录为 GPU 合成诊断；规范的原生 `videoContent` QQuickItem 证据已通过，二者都没有冒充
  人工视觉验收。
- 这组结果只关闭 software-only `SIM` 子门，不代表真实 Jetson、相机、Pixhawk、室外 GNSS、
  执行器、HIL/REAL 或人工 Fly View 已通过；版本仍为 `v0.3.0-pre.8`，最终 `v0.3.0` 不提前
  发布。

### 室外目标定位真值验收器

- 新增 `multidetect.geolocation_accuracy` 与 `scripts/validate_target_geolocation_accuracy.py`：
  通过 WGS-84 Vincenty 逆解计算目标坐标误差，报告中位数/P95/RMSE、北东偏差和 `1σ/2σ`
  覆盖率。
- 默认要求至少 10 条独立样本；最大 P95 与覆盖率阈值必须显式传入，报告只作为
  `REAL/field` 只读证据，不修改实时坐标、标定文件或飞控路径。
- E7 发布门新增可选的 `real_target_accuracy` 哈希绑定和
  `--real-target-accuracy-report` CLI；聚合器会重算报告样本，校验 WGS-84 指标、真值隔离
  标记和字节摘要。预发布清单可暂缺该绑定，最终 `release` 通道必须提供通过的现场精度报告。
- 新增 `multidetect.geolocation_survey`、`scripts/build_target_geolocation_accuracy.py` 和
  `multidetect-target-accuracy-from-replay`：从已验证的 target-event replay 读取估计坐标，按
  `event_id/frame_id` 严格连接独立真值表，拒绝漏配、重复/冲突目标 ID 以及 stale/unknown GPS；
  报告结构固定到 `configs/schemas/target-geolocation-accuracy-report.schema.json`。
- 本轮回归：全量 pytest 收集 `2296` 项，`2293 passed / 3 skipped`；`ruff`、`compileall` 和
  release-gate/QGC 相关聚焦测试均通过。3 个 skip 是两个 `onnxruntime` 环境缺口和一个
  当前 Windows 账户无法创建 symlink。

### PX4 Mission 事务持久化

- 新增 `src/multidetect/mission_journal.py`：在既有 `Px4MissionTransaction` 外提供
  hash-bound、fsync、append-only JSONL checkpoint，记录 `MISSION_REQUEST_INT` sequence、ACK、
  readback、commit 和 `AUTO.MISSION` start permit；SIM/HIL/REAL 共用同一适配器和任务路径。
- 恢复严格读取已提交前缀。未换行的掉电尾巴、hash-chain/绑定漂移、未知或跳跃状态、legacy
  `MISSION_REQUEST` 和不完整 readback 均保持 `recovery_required`，不会自动合成 ACK、跳过
  readback 或发出 start。`repair_truncated_tail()` 先隔离尾字节，再写入带操作者备注的恢复事件；
  `resume_plan.auto_start_allowed` 固定为 `false`。
- 新增 `configs/schemas/mission-transaction-journal.schema.json`、安装入口
  `multidetect-mission-journal` 和 10 项 journal/适配器回归测试。该能力只补软件事务恢复证据，
  不关闭真实 Pixhawk、Jetson、室外 GNSS、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。
- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在把同一 JSONL journal 传入联合
  SIM Mission helper，完成后在 `vtol_mission_journal`、`hashes.mission_journal` 和
  `artifacts.mission_journal` 三处留下路径/摘要；`simulator_combined_evidence.py` 对新 artifact
  会重新读取 journal、校验 SHA-256、字节数、hash-chain、恢复状态、最终状态和请求 sequence。
  旧的 pre.8 900 帧 artifact 尚未用此字段重跑，历史 33 项 verifier 结果不被改写。

### 三机型共用 Mission transaction smoke

- 新增 `src/multidetect/px4_mission_acceptance.py`，把 VTOL、fixed-wing、plain multirotor
  统一驱动到同一条 `Px4MissionTransaction` + `PersistentPx4MissionTransaction` 路径，逐项记录
  `MISSION_REQUEST_INT`、ACK、readback、commit 和 `START_PERMITTED`。
- 新增 `scripts/run_px4_airframe_transaction_acceptance.py` 与独立 verifier；三类机型各自输出
  journal 文件，verifier 重算字节数/SHA-256、hash-chain、完整 sequence、终态和 identity binding，
  可检测追加尾巴或替换 journal。该 gate 明确是 `SIM`/in-process、`physical_output_enabled=false`，
  不把软件证据升级成真实 Pixhawk、室外 GNSS、HIL/REAL 或人工 Fly View。
- 新增三机型共享路径回归，覆盖成功闭环和篡改 journal 的 fail-closed 行为。
- E7 `release-gate` writer/evaluator/schema 新增可选的
  `px4_airframe_transaction` 哈希绑定与 `--px4-airframe-transaction-evidence` CLI；预发布
  清单可暂缺，最终 `release` channel 会强制重新读取三份 journal、核对 SHA-256/字节数、
  hash-chain、终态、sequence 和 identity binding。新增绑定回归后全量 pytest 收集 `2313`
  项，`2310 passed / 3 skipped`；`ruff`、`compileall`、PowerShell parse 和
  release-gate 聚焦回归均通过。
- 初次绑定时的三机型 artifact 为
  `artifacts/evaluation/px4-airframe-mission-transaction-acceptance-latest.json`（11,971 bytes，
  SHA-256 `84AD6E8FE40795F65FCDAED95779EBA0481ECAE3DF285F537C91577BBD25F684`）；`vtol=7`、
  `fixed-wing=5`、`multirotor=5` 个 Mission item，三份 journal 均为
  `start_permitted`，独立 verifier `gate_passed=true`。
- 初次生成的 pre.8 E7 绑定清单为：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction.json`（7,584 bytes，
  SHA-256 `FC0B4B159CD784B78D3D1F56D230A1AE709DCF95AF83D944B14579B666CA2920`）。三机型绑定子门
  为真；聚合报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction-report.json`
 （15,533 bytes，SHA-256 `A7A9CD90209E9DEAADEF296546CA873D7973CDBB7DF5F97A6CAD9B4687CF9CD1`）仍明确
  `gate_passed=false`/`release_allowed=false`，原因是现场链路未就绪和 prerelease channel。

- 初次绑定轮的整体验收脚本使用 D 盘临时根目录重新执行，避免台式机 C 盘空间影响 pytest：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-rgb-slam-time-contract.json`
  （15,073 bytes，SHA-256 `5127DEDC1D7C954E819332330F1ED5B15F6D382D4AC7E7E024C00E5E7A0A4622`）。
  该快照收集 2,313 项核心测试、QGC 自定义 66/66，三机型子门为真，
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`；
  它不声明 Jetson、Pixhawk、室外 GNSS、HIL/REAL 或人工 Fly View 已通过。

### RGB-SLAM 户外时间一致性收口

- `RgbSlamRangeEstimator` 现在对相机帧、姿态、本地 NED、速度和空速统一执行
  past-only freshness：未来遥测不再通过 `abs(now - observed)` 伪装成新鲜样本；位置失鲜后的
  dead-reckoning 只有在速度/空速时间戳仍在各自窗口内时才会推进基线，否则直接清除该帧的
  RGB-SLAM 量测。重复或倒序相机帧也会在 source-frame 边界直接丢弃，不再重发上一帧三角化
  结果充当新证据。新增未来姿态/位置、陈旧速度/空速和非单调帧序列回归，防止户外时钟 skew
  或传输重试把目标距离投到尚未发生的位置、并被融合层重复加权。
- 本轮针对 RGB-SLAM、导航融合、GPS 质量和 WGS-84 投影的聚焦回归全部通过；该改动只影响
  软件测距输入门，不把无绝对尺度的视觉流升级成室外 GNSS 或 REAL 证据。

- 随后补上非单调 source-frame 回归并重新执行整体验收：重复/倒序帧会被直接丢弃，旧三角化
  结果不会因传输重试再次进入融合。最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-rgb-slam-duplicate-contract.json`
 （15,084 bytes，SHA-256 `1E1A7156A24517513759F4640F44B8120A1891F273A3C3112E3E9D65A945C733`），
  收集 `2,314` 项核心测试（`2,311 passed / 3 skipped`），QGC 自定义回归 `66/66`。验收脚本
  重建的三机型 Mission artifact SHA-256 为
  `C60BD72039D2E4DCC7056D47CDD6CB6857047C05C98DDF7204ECE869A8B6030E`；E7 绑定已同步刷新，
  当前绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction.json`
  （SHA-256 `92CA956279C4C5321B5C90FA8FBEE9B8956FA854C883E606F9B65E7D8FE2B4D7`），聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction-report.json`
  （SHA-256 `CAACDE3AFF159101B0CACE91864F6C91B1F3944058F54195B309955F76DC5F60`）。三机型
  Mission 子门和 RGB-SLAM 回归均通过，但整体仍保持现场/HIL/REAL 和最终 release 门的
  fail-closed 状态。

### 现场链路复检（仍保持 fail-closed）

- 使用当前台式机的已知机载网段端点再次执行只读预检：
  `artifacts/evaluation/field-link-preflight-20260901-journal-followup.json`（3,035 bytes，
  SHA-256 `B817AA9CD558EA13255F8B459BE42913DB2F5F63C29A7D88FB6F31C71477700A`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760` 和相机 `192.168.144.108:554` 均超时；
  本机 metadata/QGC UDP `14580/14581` 可绑定。`--require-all` 退出码为 `1`，独立
  verifier 在 `--allow-unready` 下仅确认报告结构和摘要一致性，`hardware_link_ready=false`。
- 该结果只记录当前网络事实，不选择或改写现场端点，也不提升 GNSS、真实 Pixhawk/执行器、
  HIL/REAL 或人工 Fly View 门；接通飞机 Ethernet 后必须用同一脚本、同一端点和协议检查重跑。

### Planned

- 继续以 `v0.3.0-pre.N` 交付多旋翼/固定翼/VTOL 自主任务与数字孪生增量。
- 只有 Replay、SIM、HIL、REAL 的最终验收门全部通过后才发布 `v0.3.0`。

### Verification refresh (2026-09-01, latest external combined SIM)

- 在最新 pre.8 portable EXE 上完整重跑 Docker PX4/Gazebo/QGC 联合验收：
  `artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence.json`
  （772,736 bytes，SHA-256
  `4A50E88770FF1B2084A92D72E228C4C1913E49E69BAAFE46BA43B742939D774F`）。`gate_passed=true`，
  900 帧传感器流、VTOL `MC→FW→MC`、7 项 INT-only Mission、Mission barrier、原生 QGC
  toolbar/video machine smoke、动态 loopback 端口保护/释放和 Docker stop/rm 均通过。
- 独立 Combined SIM verifier 输出
  `artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence-verification.json`
  （SHA-256 `B2855FD4C7B64DA178676A00B01EFB3E05969681ED01B9DE597D130883D1FCD7`），33/33
  checks 通过；新 SIM envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v2.json`
  （SHA-256 `F608493C7F1385D070B8CCAE903444F4028B78FF21AB30152C9C05958BC8ABC0`）。
- 将该 source/envelope 绑定到新的 E7 pre.8 清单/报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3.json`
  （SHA-256 `8B78B49D7F857051FB171C5A64C2DC6D920990B926004291A7472C9745F0B1D7`）/
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3-report.json`
  （SHA-256 `8DECBFB949C2B250311A9489CB781EC7BBA8376FF1D1C2B137F934BA14E67F99`）；SIM/S4
  子门与五组 parity 通过，整体仍因现场链路、REPLAY/HIL/REAL 和 prerelease channel 保持关闭。
- 最新完整 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v3.json`
  （20,870 bytes，SHA-256
  `64E92ADB8B99033520EE27703BAC442505828E1EA12B3E56D211864C66EC7882`），核心测试
  `2268 passed / 3 skipped`、QGC custom `66/66`，软件/硬件状态仍为
  `pass_with_environment_gap` / `offline_pending`。
- 本轮仍明确保持 software-only 语义：不声明真实 Pixhawk/Jetson/相机、室外 GNSS、执行器、
  HIL/REAL、人工 Fly View 或实飞通过；版本继续为 `v0.3.0-pre.8`。

### Verification refresh (2026-09-01, latest field-link watch)

- 在同一台式机上对默认飞机端点执行 3 次只读协议预检，原始报告为
  `artifacts/evaluation/field-link-preflight-20260901-v3.json`（12,082 bytes，SHA-256
  `C5C4F0405FE9D61E373550FBEE9DB929580838FAEF417BCCEB99E11D19AF0D05`）。本机 `14580/14581`
  均可绑定；Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760` 和相机
  `192.168.144.108:554` 的 TCP/SSH/MAVLink/RTSP 只读探针全部超时，三次均为失败样本，
  `hardware_link_ready=false`。
- 将该最新现场报告绑定到 v4 总验收：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v4.json`（20,864
  bytes，SHA-256 `1EB9D735CEB9FD9400F780074278952EFF81E20990AD83769E2AF6BD11620002`）。核心测试
  收集 `2271`（`2268 passed / 3 skipped`），QGC custom `66/66`，Combined SIM、QML parse、
  协议 self-test、portable smoke 和 parity 校验均通过；固定翼子证据为
  `artifacts/evaluation/fixed-wing-software-acceptance-20260901-gps-contract-v4.json`（SHA-256
  `1EE64A535A1C64FD41A4EA02F3164F5A62514B0ADF9006DF5CBE0C5D9215FF4C`），SIM 安全子证据为
  `artifacts/evaluation/simulator-safety-acceptance-20260901-gps-contract-v4.json`（SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。
- v4 仍是 `software_status=pass_with_environment_gap` / `hardware_status=offline_pending`；
  该刷新只更新现场 provenance，不关闭室外 GNSS、真实 Pixhawk/Jetson/相机、执行器、HIL/REAL、
  人工 Fly View 或最终 `v0.3.0` 发布门。

### Verification refresh (2026-09-01, native Fly View GPS/range diagnostics)

- 原生 QGC Fly View 的 toolbar drawer 新增只读 `GPS/GNSS` 与 `RANGE_STATUS` 质量行：无目标时
  也显示定位门原因、GPS/source age、RANGE_STATUS validity/source age/data age；面板复用现有
  QGC theme 和状态源，不创建第二个窗口或 simulator-only HUD，也不产生飞控指令。
- QML 静态契约、QGC custom tests、协议 self-test、portable headless smoke 和完整 Python
  回归均通过；全量 pytest 输出封存为
  `artifacts/evaluation/pytest-gps-contract-v3-final.log`（3,000 bytes，SHA-256
  `40DDFF3936D73E74A53A289CEB031E7906BC2082A0FD60383EA734CFA96A8F40`）。本次回归为
  `2268 passed / 3 skipped`；3 个 skip 是两个 `onnxruntime` 环境缺口和一个 Windows
  symlink 能力缺口。
- v3 版本化总验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v3.json`（20,869
  bytes，SHA-256 `9995028256F43F69C75A27B71B9B1EFDD68AF11BFB5E7DD26BF5A31DFC3556B2`）：核心
  测试收集 `2271`、QGC custom `66/66`、QML parse/protocol self-test/`git diff --check` 和
  Combined SIM 绑定均通过。固定翼子证据为
  `artifacts/evaluation/fixed-wing-software-acceptance-20260901-gps-contract-v3.json`（SHA-256
  `270F3BC8C7DE033F72774ECB851C212C5058ED44F33FDB5E127AD18E87B2D99F`），SIM 安全子证据为
  `artifacts/evaluation/simulator-safety-acceptance-20260901-gps-contract-v3.json`（SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。
- QGC portable 主 EXE 已由同一构建重新部署：
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，
  `46,575,616` bytes，SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；Release 与 portable
  EXE 字节一致。当前 `configs/release-parity.json` SHA-256 为
  `F711096076A34673874F8F8E4E03BF3CCCEFA269F35C2B30C4C2FEAD05646341`，五组 parity hash
  已在 QML 变更后重新生成。
- 台式机现场预检仍是 `hardware_link_ready=false`，总快照保持
  `software_status=pass_with_environment_gap` / `hardware_status=offline_pending`；本轮不
  关闭室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。

### Verification refresh (2026-09-01, canonical GPS quality contract)

- 新增 `src/multidetect/mavlink_gps.py` 作为 MAVLink GPS wire-domain 的唯一常量/归一化入口：
  `GPS_FIX_TYPE` 使用 `3D fix (3)..PPP (8)` 的合法范围，`satellites_visible=255` 明确解释为
  unknown，室外质量门要求已知 `6..254` 颗卫星。SIM/live bridge、Pixhawk、现场会话和自适应
  测距都复用同一组阈值，避免 REAL/HIL/SIM 漂移。
- `GPS_RAW_INT` 现在按原子样本处理：坐标必须落在
  `latitude ∈ (-90, 90)`、`longitude ∈ [-180, 180]`，fix、卫星数、`alt`、`eph` 和时间戳必须
  合法；缺字段、未来时间、降级 fix、未知卫星数或极点坐标会清除 raw GPS freshness，不能拿旧
  坐标和新质量字段拼成可定位样本。`GLOBAL_POSITION_INT` 仍是独立的融合位置源，不会伪造
  raw GPS 质量。
- 共享 `assess_gps_quality()` 将 `position_healthy=true`、`hAcc≤6 m`、`vAcc≤10 m`、
  `GPS age≤1.5 s` 和坐标域检查集中到一个 fail-closed 门；返回策略、目标落图、证据快照和
  回放路径都必须通过该门。新增回归覆盖缺垂直精度、`alt` 缺失、`255` 卫星哨兵、未来时间、
  精确极点和可配置 fix 阈值。
- 本轮版本化总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v2.json`（20,869 bytes，
  SHA-256 `22612316735D42FC725E1B7B44AC87DEDAF9828198ADA147FA3E1274573EADE8`）：核心测试收集
  `2271`，实际回归 `2268 passed / 3 skipped`，QGC 自定义回归 `66/66`，Ruff、compileall、
  协议/QML/portable smoke 均通过，脚本退出码 `0`。Combined SIM source/envelope 复核通过，
  但现场报告仍为 `hardware_link_ready=false`、`hardware_status=offline_pending`；本轮不关闭
  室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。
  全量 pytest 输出为 `artifacts/evaluation/pytest-gps-contract-v2-final.log`（SHA-256
  `40DDFF3936D73E74A53A289CEB031E7906BC2082A0FD60383EA734CFA96A8F40`）。
- 版本化子证据：固定翼软件验收
  `artifacts/evaluation/fixed-wing-software-acceptance-20260901-gps-contract-v2.json`（SHA-256
  `AE7AF70829AC8AD5E8C43AA952ED84B8AABA013E812A145EC29EAB838C95AFA4`），SIM 安全验收
  `artifacts/evaluation/simulator-safety-acceptance-20260901-gps-contract-v2.json`（SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。

### Verification refresh (2026-09-01, combined SIM dynamic loopback ports)

- 修复 `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 的主机端口生命周期：
  运行时从 `30000..60000` 有界随机分配九个互不重复的 TCP/UDP loopback 端口，分配前同时做
  TCP/UDP bind 探测，并明确排除容器内 PX4 输入 `18570` 和受保护的地面站 UDP `14550`。
  evidence 现在保存 `allocation_mode`、动态范围、完整分配列表和释放/保护端口结果；Docker
  inspect 清理也加入 `20 s` 有界超时，避免 Docker Desktop API 卡住时 wrapper 无界等待。
- Docker Desktop 恢复后用当前 `v0.3.0-pre.8` 源码执行 900 帧组合门：
  `artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence.json`
  （772,886 bytes，SHA-256
  `4DE2F6AD236EBE65F97CD9A279A101453FA9C938D45E25B9A0FB2DF0D3B7F4F1`）。
  `gate_passed=true`，集成 SIM/QGC、VTOL attach 和 PX4 Mission 均通过，Docker Server `28.4.0`
  容器 stop/rm 成功，900 帧、Mission-ready 后连续 type-24 barrier 和端口释放均有记录。
- 本次分配的 host 端口为
  `38392, 32511, 46147, 30192, 55621, 30275, 49253, 53375, 31727`；
  `protected_udp_14550_unchanged=true`、`all_test_ports_released=true`。这些端口只属于本次
  disposable SIM，不写入生产端点，也不提升为真实 Pixhawk/Jetson、室外 GNSS、执行器或人工
  Fly View 验收。
- 新的 SIM release-mode envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports.json`，
  SHA-256 `86B6A7B4E220D7E05C8C9E56A678B22B269587AB758D05DE8BBEE855644FD981`；对应 E7
  动态端口/S4 绑定清单和报告分别为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound.json`
  （SHA-256 `08C3B431C57329855051BDF3D5FF6FD8F8DBE54F8D09E474EB36563ADEF2B13E`）和
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-report.json`
  （SHA-256 `5A5D7E0435F8906E692ADB3A44D6D7A972A5CCF381AC960B0E92DB3D7B123298`）。报告重新
  校验 SIM/S4 哈希并显示 `simulator_safety.passed=true`、SIM source gate 为真，但整体
  `gate_passed=false`/`release_allowed=false`，因为现场链路、REAL 和最终 release channel
  仍未通过；版本继续为 `v0.3.0-pre.8`。
- SIM 重跑后再次执行只读现场链路预检，报告为
  `artifacts/evaluation/field-link-preflight-20260901-post-sim.json`（2,352 bytes，SHA-256
  `2B3EF846CE463BDEBABAC4F1246AA08D3D1587E356C0AEB470A0196667FACC63`），脚本退出码为 `0`。
  三个默认现场端点仍超时，`14580/14581` 本机 UDP 端口可绑定，报告保持
  `all_passed=false`、`hardware_link_ready=false`；该报告没有绑定成通过的 REAL 证据。
- 绑定该现场预检后刷新总 goal-acceptance：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-post-dynamic-ports.json`（12,631
  bytes，SHA-256 `BB795D37E03D55FFEC7B4C9EA742BE1F8CC06E81EBCFF4A3475A6E3C52F7738F`）。核心测试
  收集 `2216`，QGC 自定义回归 `66/66`，协议/QML/portable smoke 均通过，脚本退出码为 `0`；
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，现场
  `hardware_link_ready=false`，因此不改变 HIL/REAL、室外 GNSS、人工 Fly View 或最终
  `v0.3.0` 发布门。
- 新增 `src/multidetect/simulator_combined_evidence.py`、
  `scripts/verify_combined_sim_evidence.py` 和 `multidetect-sim-combined-evidence` CLI，
  对 combined SIM 的动态端口（九个、唯一、`30000..60000`、排除 `14550`）、端口释放、
  Docker 清理、嵌套 Mission/SIM gate 以及 hardware/operator-visual claims 做独立
  fail-closed 复核。要求时验证器还会检查 `sim_release_mode_evidence_prepared` 的
  `source.path`/`source.sha256` 是否绑定到同一个源 JSON。
- 当前源证据的独立复核文件为
  `artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence-verification.json`
  （SHA-256 `E8440B7B4DDD5981A1A8B7FD279CF210AE016E1858D008B9DE8D5DD507BFFCA2`，33/33
  check 通过）。E7 生成器新增 `--simulator-combined-evidence` 与
  `--simulator-combined-release-mode-evidence`，当前绑定清单/报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2.json`
  （SHA-256 `C026AE1A3209BFE4798DDD600A12EBC4F1684DCE49E871DF5F28D80253D5A188`）和
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2-report.json`
  （SHA-256 `47222082B48FFB8E20AB1264A78CC4C25F6FFBBD9F0321AF17C18E420A65F0FD`）。
  `simulator_combined.passed=true`，但 REAL 现场链路和最终 release channel 仍保持关闭。
- verifier CLI 现在支持 `--out <path>` 写出排序稳定的 JSON 报告；同时将 Combined SIM
  verifier 从 release-gate 聚合器改为懒加载，`python -m multidetect.simulator_combined_evidence`
  不再因为包根导入而产生 runpy 自导入警告。该改动不改变 source、E7 清单或 portable EXE
  摘要。
- `scripts/run_goal_acceptance.ps1` 新增可选的
  `-CombinedSimEvidence`、`-CombinedSimReleaseModeEvidence` 和
  `-RequireCombinedSimEvidence`；总验收 JSON 会保存 `simulator_combined_acceptance` 的
  source/envelope SHA-256、verifier exit code 和完整 checks，默认不传参数时保持兼容。
  `-RequireCombinedSimEvidence` 与 `-RequireFieldLinkPreflight` 在缺少必需路径时现在会
  fail-fast 并返回非零退出码，不再让“required 但未提供”的 binding 继续生成快照。
- `run_goal_acceptance.ps1` 新增 `-FixedWingAcceptanceOutput` 和
  `-SimulatorSafetyOutput`，可把本次固定翼/S4 子证据写入版本化路径，避免带新 provenance 的
  总验收重跑覆盖 `*-latest` 文件；省略参数时保持旧默认路径兼容。
- 使用版本化输出路径完成一次带 Combined-SIM 与现场预检绑定的总验收：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-versioned-combined-binding.json`
  （SHA-256 `F8E82527E0F048BE8EC60F244EC75E1B96C4FFE5AD49C5DDCAD50EBB81F12559`，核心测试
  `2224`、QGC 自定义 `66/66`）。Combined-SIM 绑定通过，现场 `hardware_link_ready=false`，
  因此软件状态为 `pass_with_environment_gap`、硬件状态为 `offline_pending`，最终 `v0.3.0`
  发布门不变。

### Working-tree delta (2026-09-01, SIM S4 safety evidence)

- 新增 `src/multidetect/simulator/safety_evidence.py`、
  `scripts/run_simulator_safety_acceptance.py`、
  `scripts/verify_simulator_safety_evidence.py` 和
  `configs/schemas/simulator-safety-evidence.schema.json`。该 deterministic SIM 子门沿用
  生产 `UnifiedSelectionTargetPool`、`SimulatorMavlinkControlAdapter`、`SafetyRuleEngine`
  与 `MissionStateMachine`，覆盖 VTOL、固定翼和多旋翼。
- S4 明确验收 `CANCEL_TRK` 的单目标清除与全量取消清空、QGC/PX4 literal-loopback 与消息
  allowlist、watchdog→`FAILSAFE` 后禁止 AI 续控、围栏/GPS/极点坐标 fail-closed，以及
  `TERMINATED` 后拒绝 Mission event。证据 verifier 会重新计算 check，篡改声明字段、计数器、
  坐标、身份或硬件/人工验收 claims 都保持失败。
- `scripts/run_goal_acceptance.ps1` 已把 S4 runner/verifier 纳入总软件验收；证据的硬件、
  人工 Fly View、Pixhawk/Jetson/相机和 REAL 字段继续固定为 `not_claimed`，版本仍为
  `v0.3.0-pre.8`，不提前切换最终 `v0.3.0`。
- 本轮总验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s4-safety-v11.json`，SHA-256
  `38627B36E6522DFFF76ED7B7BFECC0E2C7F795938C1C45FD48CFD25CBFA0BCDE`；核心测试 `2209`、
  QGC 自定义 `66/66`、脚本退出码 `0`。S4 artifact SHA-256 为
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`，verifier 退出码为 `0`。

### Working-tree delta (2026-09-01, E7 S4 hash binding)

- `src/multidetect/release_gate.py` 与 `src/multidetect/release_gate_manifest.py` 现在支持顶层
  `simulator_safety` 记录：生成器先执行 S4 verifier 并保存相对路径、SHA-256、版本/事件身份、
  六个 check、verifier 事件和退出码；聚合器读取时重新哈希源文件、重新计算 check 并比较声明。
- pre-release 清单可以省略该字段以兼容历史 provenance；一旦显式绑定，文件替换、声明篡改、
  check 漂移或 verifier 失败都会保持 fail-closed；最终 `release` 通道强制要求通过的 S4 绑定。
- `configs/schemas/release-gate-manifest.schema.json`、E7 writer CLI（含
  `--simulator-safety-evidence`/`--sim-safety-evidence`）和 release-gate 回归测试同步覆盖该契约。

### Verification refresh (2026-09-01, E7/S4 integration)

- 在 E7 writer/evaluator 接入顶层 `simulator_safety` 后，重新执行总验收：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v12.json`
  （SHA-256 `C1FED75A38FAD315AE167FA47457F44D7D882E64A50CA1AFE63DBAE79A795DEA`）。核心测试
  收集 `2215`、QGC 自定义回归 `66/66`，脚本退出码 `0`；S4 runner/verifier 仍为
  `gate_passed=true`/`verifier_exit_code=0`。
- 该重跑确认哈希重算、声明/源 check 对比、schema 和篡改回归均通过；软件状态为
  `pass_with_environment_gap`，现场硬件仍 `offline_pending`，不改变 HIL/REAL/室外 GNSS
  和人工 Fly View 的待验状态。
- 针对坏路径/哈希异常的 fail-closed 健壮性修复后再次重跑，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v13.json`
  （SHA-256 `20C790F384918B8457CC3A2BA54401B20FF55B7786214D82A2CF385864308134`），核心测试
  `2215`、QGC 自定义 `66/66`、脚本退出码 `0`；S4 绑定通过，现场仍
  `hardware_link_ready=false`/`offline_pending`。
- pre.8 接线快照为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound.json`（SHA-256
  `A00EAA2EC6AA8CCEF3A05692998932C77FE060667E42AFA72DD21B8326B448FF`），聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound-report.json`（SHA-256
  `38357A92648DC07D1597B76AE912F6BCC61930D4B8E13AF18A666260EFDAF224`）；S4 绑定通过，
  但现场链路/REAL 仍保持关闭。
- 绑定最新 `192.168.1.0/24` 只读发现报告后重跑的总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-network-discovery-v14.json`
  （SHA-256 `A6CEAEDFA6AE583B118E8E69BF39102E13FB903A0E1BE0542379970C4C952D25`）；核心测试
  `2215`、QGC 自定义 `66/66`、S4 verifier `0`、脚本退出码 `0`，现场仍
  `hardware_link_ready=false`/`offline_pending`。
- 同一现场报告与 S4 证据装配为新的 E7 pre.8 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260901-network-discovery-s4-bound.json`
  （SHA-256 `D4E9412EED3F6CF2EA8E6DB11D1F81AC4A35826B7F343C682C4EFEAF619E631D`），聚合报告
  SHA-256 `98BD437EEBEB0383901403B377E3B04C69D4DDDF2EAADDDC2AA97DCDAA52E66A`；S4 通过，
  现场链路未就绪，`release_allowed=false`。

### Working-tree delta (2026-09-01, release-mode evidence envelopes)

- 新增 `src/multidetect/release_mode_evidence.py`、
  `scripts/create_release_mode_evidence.py` 和
  `configs/schemas/release-mode-evidence.schema.json`。该窄适配层把一个明确指定的
  REPLAY/SIM/HIL/REAL runner JSON 封装为 deterministic、SHA-256 绑定的 mode envelope，保留源
  路径、事件、运行模式、`gate_passed` 与 projection 原因；缺失/非布尔/`false` 源 gate 永远
  fail-closed，缺失或不匹配的 runtime identity 必须显式 opt-in。
- 四个 runner 现在显式输出 transport/software gate：`integrated-replay`、
  `integrated-replay-qgc`、`integrated-sim-qgc` 和 fixed-wing software acceptance 均写入
  `gate_passed=true`；这些字段只代表各自 runner 的软件/传输闭环，不替代人工 Fly View、硬件
  或 REAL 控制验收。
- 补充 wrapper 直执行、缺失 gate、模式 mismatch、schema 契约和 deterministic bytes 回归。
  `tests/test_release_mode_evidence.py` 与现有 release-gate/runner 测试保持通过；安装 package
  后新增 `multidetect-release-mode-evidence` console entry，clean checkout wrapper 与它共用
同一个 `main()`。

### Working-tree delta (2026-09-01, combined SIM Mission frame budget)

- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 的默认传感器帧数从
  600 提高到 900。600 帧本身的 RTSP/type23/type24 传输是完整的，但在
  `mission-active-ready.json` 写入时可能正好耗尽，无法满足门禁要求的
  Mission-ready 后再前进 5 个原生 QGC type-24 序列；900 帧仍是有界运行，
  并为该证据留出确定性余量。
- 同步更新 README、自治架构运行命令和 PowerShell 静态契约测试；历史 600
  帧 evidence 原样保留，不被改写为新的运行结果。

### Verification refresh (Docker live SIM AI, 2026-09-01)

- Docker Desktop 运行时预检重新发现本机实际的 Docker client/daemon `28.4.0` 和运行中的
  WSL `docker-desktop`：`artifacts/evaluation/simulator-runtime-preflight-20260901-090345.json`
  （SHA-256 `E529B8EBC1A7695C3563603C7DFE666922394BC262FB78090FF25FC28FAE892E`）。native
  `px4`/`gz` 仍不在 PATH，但 `execution_mode=docker`、`ready_to_attempt=true`。
- 使用固定 digest 的 `px4io/px4-sitl-gazebo` 镜像和 `--execute --exercise-arm` 重跑共享
  telemetry/AI bridge，VTOL、fixed-wing、multirotor 三个场景全部通过：
  `simulator-docker-ai-acceptance-20260901-090608-arm-exercise.json`（SHA-256
  `9E1901FEFC3968EFE8C39B874158AAEB5CD4A3AA5E633AD9E018FEC3D35D2BF4`）、
  `simulator-docker-ai-acceptance-coastal_patrol_fixed_wing_001-20260901-090751-arm-exercise.json`
  （SHA-256 `DAF53226EE8F8C305BE487F87CA21B50CE17937D02760FF948C84E155BB5292E`）和
  `simulator-docker-ai-acceptance-urban_inspection_multirotor_001-20260901-090751-arm-exercise.json`
  （SHA-256 `59F11F3937DB80C5594F45DC0687F1C9370EAFF5BEC8181A635B5FF393E0D37C`）。三份
  evidence 都记录 `gate_passed=true`、`runtime_gate_passed=true`、`telemetry.qualified=true`、
  `armed_observed=true`、`ai_policy_gate_passed=true`、`ai_steps=1`、disarm 与容器清理成功。
- 该 live run 只证明无目标 telemetry 下的安全 `hover/no_target` policy step 和共享
  `SimulatorMavlinkControlAdapter` 输出；相机目标识别/track 仍由 deterministic perception/
  interactive fixture 覆盖，不能升级为视觉、真实 Pixhawk、HIL/REAL 或人工 Fly View 验收。
`--execute` 未显式 `--exercise-arm` 时继续等待首个 armed heartbeat 并 fail-closed，这是
  bridge 的预期参数语义。版本保持 `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。

### Working-tree delta (2026-09-01, live synthetic perception target seam)

- `SimulatorTelemetryBridge` 新增 `ingest_perception()`：感知输入只允许更新目标可见性、相对
  方位、距离、置信度四元组，不能覆盖 PX4 提供的 GPS、armed/link、姿态、速度、高度或电量。
  目标帧按默认 `1.5 s` lease 做 future/stale/out-of-order 校验；租约到期在下一个 policy
  snapshot 前原子清空，避免旧目标持续产生控制输出。新增 evidence counters、最近感知帧和
  拒绝原因，并补充伪造导航字段隔离、过期清除和时间门测试。
- `run_simulator_docker_ai_acceptance.py` 增加显式 `--exercise-target`（必须同时使用
  `--execute --exercise-arm`）。夹具通过同一个 perception→executor→authority→loopback
  `MANUAL_CONTROL` 链路注入一帧 truth-free 目标并要求 `TRACK/accepted=true`；默认路径仍是
  无目标 `hover/no_target`，不会把合成目标写成相机视觉准确率或硬件证据。
- 进一步收紧拒绝路径：`SimulatorTelemetryBridge._reject_perception()` 现在统一清空 perception
  所有权下的目标 tuple。未来、stale、out-of-order 和 malformed 帧都不能让旧目标跨拒绝事件
  继续存活；PX4 vehicle telemetry 不受影响。新增已有目标被三类时间门拒绝后的清空回归，避免
  只验证“返回 None”却漏掉缓存状态。

### Verification refresh (live SIM target policy, 2026-09-01)

- VTOL `river_rescue_vtol_001`：
  `artifacts/evaluation/simulator-docker-ai-target-river_rescue_vtol_001-20260901.json`
  （SHA-256 `5BD19143E24272243961D36202804275CDB4A1F88027AF05500BB9EC6CF9B50A`）；
- fixed-wing `coastal_patrol_fixed_wing_001`：
  `artifacts/evaluation/simulator-docker-ai-target-coastal_patrol_fixed_wing_001-20260901.json`
  （SHA-256 `4FB9B384FBA588603BB9AF166E43AF368644F9B35D531D3556FAA16223E5833A`）；
- multirotor `urban_inspection_multirotor_001`：
  `artifacts/evaluation/simulator-docker-ai-target-urban_inspection_multirotor_001-20260901.json`
  （SHA-256 `9E13005E7D722018015B6048DB6F6F3885CF78A6BFB5CFF747C626056A4119AC`）。

三份 disposable Docker/PX4/Gazebo evidence 均为 `gate_passed=true`、`runtime_gate_passed=true`、
`target_policy_gate_passed=true`，目标决策为 `TRACK` 且 `accepted=true`，并完成 disarm 与
stop/rm 清理。它们只证明实时 telemetry 与合成 truth-free perception 的生产策略闭环；
`visual_accuracy`、相机/Jetson、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL、人工 Fly View
和实飞仍为 `not_claimed`，版本保持 `v0.3.0-pre.8`。

### Verification refresh (full goal acceptance after live target policy, 2026-09-01)

- 最新完整 pre.8 goal-acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai.json`（SHA-256
  `8BF5B9B39402196A3F31CBA42C3E966076A90599AD21853E7E6D00D76A9A904A`）。脚本退出码 `0`，
  核心测试收集 `2173`、QGC 自定义测试 `66`，软件状态为
  `pass_with_environment_gap`，硬件状态仍为 `offline_pending`；现场 preflight
  `hardware_link_ready=false`，人工 Fly View `not_claimed`。
- 快照绑定当前 portable `MultiDetectGCS.exe` SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，以及本轮
  `ingest_perception()`、目标租约和 Docker live target evidence。新增 SIM policy 子门已
  通过，但 Jetson/室外 GNSS/真实 Pixhawk/执行器/HIL/REAL/人工视觉仍未通过；版本继续为
  `v0.3.0-pre.8`，最终 `v0.3.0` 不发布。

### Verification refresh (perception rejection fail-closed, 2026-09-01)

- 最新完整 pre.8 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v2.json`（SHA-256
  `5D2484A327587D914570416E2BC74227D34E6FFACE60AB6184941D0D2CA8DA78`）。goal-acceptance
  退出码为 `0`，核心测试收集 `2176`、QGC 自定义测试 `66`，Ruff、compileall、QML 解析、协议
  自测和 portable smoke 均通过。
- 快照仍为 `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，
  现场 `hardware_link_ready=false`、人工 Fly View `not_claimed`；该刷新只记录 perception
  rejected-frame 清空语义，不改变 `v0.3.0-pre.8` 版本，也不关闭 Jetson、室外 GNSS、真实
  Pixhawk/执行器、HIL/REAL 或最终发布门。

### Verification refresh (shared perception bridge delegation, 2026-09-01)

- `SimulatorPerceptionBridge(telemetry_bridge=...)` 现在把 production `feed_result()` 委托给
  同一个 `SimulatorTelemetryBridge.ingest_perception()`，因此绑定 bridge 的 SIM/HIL/REAL
  感知不会绕过 heartbeat、armed、target-lease、watchdog 或 authority 门；bridge 拒绝的
  帧在返回 runner 前原子清空目标四元组，并暴露明确的
  `bridge_rejection_reason`。新增共享入口集成回归覆盖 accepted、future/stale/out-of-order
  rejected 和目标清空路径。
- 完整 pre.8 goal-acceptance 刷新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v3.json`（SHA-256
  `A49DDB605FD90AA04BBFCA7185ECB600FACA52E3BAF3185F262C2B9DE6F3D560`）。脚本退出码 `0`，
  核心测试收集 `2178`、QGC 自定义测试 `66`，Ruff、compileall、QML 解析、协议自测和
  portable smoke 均通过；软件状态仍为 `pass_with_environment_gap`，硬件状态
  `offline_pending`，现场 `hardware_link_ready=false`、人工 Fly View `not_claimed`。
- 共享入口修复纳入 canonical `configs/release-parity.json`，当前
  `core_algorithm_hash=71e59bb128a652d67827aa3da615af4b8f8af8f0ad8a6358cf7acc6ab16bcd63`。
  Python-only 改动不需要重编 QGC；portable EXE SHA-256 仍为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，版本继续为
  `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。

### Working-tree delta (2026-09-01, perception range freshness seam)

- `SimulatorPerceptionBridge` 现在复用 operator-link 的 `MAX_RANGE_STATUS_SOURCE_AGE_S`
  （默认 `1.5 s`）做显式测距新鲜度门。`RangeSolution` 的
  `data_freshness_s`/`freshness_s`/`age_s` 与 `RangeStatusMessage` 的
  `source_captured_at_s`/`captured_at_s`/`observed_at_s`/`timestamp_s` 都会校验为有限、非负、
  非未来且不超过阈值；不再允许 adapter 把旧测距当成当前距离送进共享 AI/控制链路。
- 过期、未来或显式缺失源时间的测距样本保持视觉 track 可见，但清空距离、方位和测距置信度，
  并留下 `range_data_stale`、`range_source_stale`、`range_source_future` 或
  `range_source_timestamp_missing` 原因；负数/非有限年龄按 malformed 输入计数并抛出。
  evidence 记录阈值、字段别名和 fail-closed 契约，回放与现场诊断可直接核对。
- 新增 stale/future/missing/negative range-age 回归后，完整 pre.8 goal-acceptance 刷新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v4.json`（SHA-256
  `D4B0EE2E7F274B0904DE2CDB60AC42DA4195CFAFE31D1774418A8180E46DE5CF`）。脚本退出码 `0`，
  核心测试收集 `2183`、QGC 自定义测试 `66`；软件=`pass_with_environment_gap`、硬件=
  `offline_pending`、现场 `hardware_link_ready=false`、人工 Fly View=`not_claimed`。
- canonical `configs/release-parity.json` 已同步（文件 SHA-256
  `CB51D7CDD1AC0CA0D52F21241C88A2630B50D18DAFD88A0B9D4FE01185728BD2`，
  `core_algorithm_hash=6f224eb199fd53979648f477e364f30c318c303b6a97d303eae548948f9290f0`）。
  这是 Python-only 修复，不需要重编 QGC；portable EXE SHA-256 仍为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，版本保持
  `v0.3.0-pre.8`。

### Working-tree delta (2026-09-01, SIM AI return GPS freshness seam)

- `SimulatorAiPolicy` 的返航/低电量路径现在单独校验 `gps_observed_at_s`：时间戳必须存在、
  不得来自未来，且年龄不超过 `maximum_return_gps_age_s=1.5 s`。持续的心跳或融合位置更新
  不会再刷新 raw-GPS 游标；缺失、过期和未来 raw GPS 统一保持
  `return_gps_quality_insufficient` / `gps_unqualified_for_return` fail-closed。
- 新增 fresh-boundary、missing、stale、future 与配置边界回归；
  `scripts/run_simulator_ai_acceptance.py` 的共享 SIM AI 闭环重新退出 `0`。
- 完整 pre.8 总验收刷新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-recheck-v5.json`（SHA-256
  `7EAE02CC318160650673508692C8D5F5C6636ADBADA1ACED2BBF098661FAE70`），核心测试收集 `2183`、
  QGC 自定义测试 `66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`，现场
  `hardware_link_ready=false`、人工 Fly View=`not_claimed`。
- 将 `src/multidetect/simulator/ai_executor.py` 纳入 canonical `core_algorithm` parity；
  `configs/release-parity.json` SHA-256 为
  `93269062DFCD4D2EA01E31AA8EE49F0FB4A074B0F8FF7DC63AB3979AD4AD9F70`，
  `core_algorithm_hash=da5fe6ff7d1455b85c51bf975e14f2da007e603de5aed69b93470cd3a31de006`。
  这是 Python-only 改动，portable EXE 保持原 SHA-256，版本继续为 `v0.3.0-pre.8`。

### Verification refresh (GPS return freshness acceptance, 2026-09-01)

- 在返航新鲜度门落盘后重跑完整 pre.8 验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-return-freshness-v6.json`
  （SHA-256 `B43DC058C34A7ACEC7E489DEF0BFC55FD8D9A7348FB30D2F5B3F54B256F4189C`）。
  核心测试收集 `2188`、QGC 自定义测试 `66/66`，脚本退出码 `0`。
- 快照绑定的现场协议预检是三采样、协议检查模式，报告结构验证通过但
  `hardware_link_ready=false`；因此 `software_status=pass_with_environment_gap`、
  `hardware_status=offline_pending`，Jetson/GR01/相机、室外 GNSS、HIL/REAL 和最终
  `v0.3.0` 发布门继续保持 pending。

### Verification refresh (bounded field-link discovery after desktop migration, 2026-09-01)

- 在显式 `192.168.1.0/24` 范围内运行只读、有界的协议发现，证据为
  `artifacts/evaluation/field-link-discovery-20260901-continue-protocol.json`（SHA-256
  `C4ECDCCE5CF1B35A1E07D80299692632D2FE56135962F291056509D4DA1061D1`）。扫描 `254` 个地址，
  仅 `192.168.1.1`、`192.168.1.62`、`192.168.1.77` 返回 SSH identification；MAVLink `5760`
  与 RTSP `554` 均没有协议命中。
- 发现结果是诊断信息，不把任意 SSH 主机当成 Jetson，也不会替换显式 field-link 配置或
  提升 `hardware_link_ready`。飞机网接通后仍需对确认过的 Jetson/GR01/相机地址运行
  `run_field_link_preflight.py --protocol-checks --samples 3 --require-all`；版本继续为
  `v0.3.0-pre.8`。

### Verification refresh (3-D control/asset gate for all vehicle classes, 2026-09-01)

- 按当前 pre.8 源码分别重跑 VTOL `river_rescue_vtol_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-river_rescue_vtol_001-continue.json`
  （SHA-256 `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`）；fixed-wing
  `coastal_patrol_fixed_wing_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-coastal_patrol_fixed_wing_001-continue.json`
  （SHA-256 `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`）；multirotor
  `urban_inspection_multirotor_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-urban_inspection_multirotor_001-continue.json`
  （SHA-256 `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`）。
- 三份报告均为 `gate_passed=true`、`deterministic_gate_passed=true`，手控/AI 共用 authority、
  watchdog、loopback 与 repository-owned 资产哈希均通过；本机 `px4`/`gz` 不可用，
  `external_runtime_gate_passed=false`，所以视觉、真实硬件、室外 GNSS、HIL/REAL 和人工
  Fly View 仍保持 `not_claimed`/`offline_pending`。版本继续为 `v0.3.0-pre.8`。

### Verification refresh (Docker external runtime + live AI target, 2026-09-01)

- Docker Desktop daemon 恢复后，使用显式 Windows Docker 客户端和固定镜像
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新执行三种机型的 disposable PX4/Gazebo runtime gate。VTOL、fixed-wing、multirotor
  evidence 分别为：
  `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-vtol.json`
  （SHA-256 `391BDA5D7832F08174B8260D9B1B43ADC6BB5FB3E67E82AC5EE6F1859905E16D`）、
  `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-fixed-wing.json`
  （SHA-256 `FC20E7A241E53A7BBE82B4498D86BD06D2AA614D4AD44A1AD57464A3A11F4A22`）和
  `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-multirotor.json`
  （SHA-256 `D3CA94B17C3A1461E059D078020BFA1010930257A76A18CE28FA8935040BD75F`）。三份
  evidence 均为 `gate_passed=true`、`preflight_gate_passed=true`、`runtime_gate_passed=true`、
  `telemetry.qualified=true`，并在退出时完成容器 stop/rm 清理。
- 同步生成的 runtime-preflight 为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-continue.json`（文件 SHA-256
  `0B304D5CCEE184AC25747BD85B08D2310D4A764DF6D253E0C150B9F48746D5D6`），记录
  `execution_mode=docker`、`docker_daemon_ready=true`、`ready_to_attempt=true`；native
  `px4`/`gz` 仍不在 PATH，故外部入口固定走 Docker Desktop。
- 同一镜像再以 `--execute --exercise-arm --exercise-target` 跑 live AI target acceptance。
  三种机型的 evidence 分别为：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-vtol.json`
  （SHA-256 `C60E759BC99E5BF9D5190CE711B9B93F219880A7A7ED2534D797E394A3CC1B57`）、
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-fixed-wing.json`
  （SHA-256 `99FBF23B71A94A21A9F6EB34A43D7737FEC651DAF5E3E267E837C7A0F0CB4032`）和
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-multirotor.json`
  （SHA-256 `0633659951E05C9BD5384251AE95D7D834FEE076CE782D6197CF79167A6D032A`）。三份
  evidence 都记录 `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、
  `ai_steps=2`、`ai.armed_observed=true`、`target_exercise.accepted=true`、
  `decision.action=track`，并完成 disarm 与容器清理。
- 这批证据关闭的是 SIM external runtime 与 AI/target policy 子门；输入目标仍是 truth-free
  合成 perception，不等同于 QGC 人工 Fly View、相机识别精度、Jetson、室外 GNSS、真实
  Pixhawk/执行器、HIL 或 REAL。版本继续为 `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。
- 同一版本随后执行增量 Windows Release/portable 构建，Ninja 返回 `no work to do`，脚本退出码
  `0`；portable `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output`
  退出码也为 `0`。启动日志为 `artifacts/evaluation/qgc-simple-boot-20260901-continue.log`
  （SHA-256 `8D6A33F8DE158EEED2E9AB526645953C391F2BD4BB584A8F0917012CC5FDA4DB`）；Release 与
  portable 主 EXE 保持 `46,575,104` bytes、SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。

### Working-tree delta (SIM external runtime/AI evidence join, 2026-09-01)

- 新增 `src/multidetect/simulator_external_evidence.py`、
  `scripts/verify_simulator_external_evidence.py`、
  `configs/schemas/simulator-external-evidence.schema.json` 和
  `tests/test_simulator_external_evidence.py`。该 join 点对 VTOL、fixed-wing、multirotor 的
  runtime/AI 六份 JSON 做 fail-closed 哈希绑定，复核当前 `configs/release-parity.json`，并
  要求同一 digest-pinned PX4/Gazebo 镜像、同一场景 manifest、pre.8 版本、GPS 质量、truth-free
  感知、硬件控制关闭、AI target/armed/track 和容器清理。它是纯文件校验，不启动任何外部系统。
- 当前 join artifact 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-pre.8.json`，SHA-256
  `43D34D1E7697CDB950816B9C1057104EB4F90820C60AF1A5163B081174BC3632`，`gate_passed=true`；
  parity 文件 SHA-256 为
  `93269062DFCD4D2EA01E31AA8EE49F0FB4A074B0F8FF7DC63AB3979AD4AD9F70`。
- 新增五项回归覆盖三机型 join、镜像漂移、未执行 target、机型集合和原子写入/重载。
  该结果只关闭 SIM external runtime 与 AI/target policy 子门，不提升人工 Fly View、室外
  GNSS、真实 Pixhawk/Jetson/相机、执行器、HIL/REAL 或最终 `v0.3.0` 发布门；版本仍为
  `v0.3.0-pre.8`。
- join 后的完整 goal acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s3-faults-v10.json`（SHA-256
  `A3E2CC98F3B3117D3C36E1FEF15DAA2E9CAB41E4ADB31352FA0397CA74BFEDE5`），脚本退出码 `0`，核心
  测试 `2200`、QGC 自定义 `66/66`；portable simple-boot 日志
  `artifacts/evaluation/qgc-simple-boot-20260901-external-join-v8.log`（SHA-256
  `B329814985CF63C04460E2F8DD9872CFCF2A96D48DB96DF051564578287F0F91`）也退出 `0`。软件仍为
  `pass_with_environment_gap`、硬件仍为 `offline_pending`，版本保持 `v0.3.0-pre.8`。

### Working-tree delta (SIM fault timeline hardening, 2026-09-01)

- `FaultKind`/`scenario-runtime-input.schema.json` 新增 `timestamp_skew`、`target_drift` 和
  `target_bearing_drift`。`ScenarioRunner` 在 truth-free observation 层对 sensor freshness 与
  target range/bearing 注入确定性扰动：共享 `observed_at_s` 不变，负偏移跨过 t=0 转成
  unavailable，正偏移保留 future cursor，距离在 0 m 饱和，方位归一到 `[-180, 180)`；所有
  故障仍走同一 AI/authority/MAVLink 路径，不产生第二控制通道。
- 新增 scenario-runner 回归覆盖 future/stale GPS 均触发同一 `gps_unqualified_for_return` 门、
  不产生负时间戳终止，以及距离/方位漂移、±180° wrap 和 `active_frames` 证据。该改动只
  增强 SIM/S3 软件故障注入，未提升 truth-free、人工 Fly View、室外 GNSS、真实 Pixhawk、
  HIL/REAL 或最终 `v0.3.0` 发布状态；版本仍为 `v0.3.0-pre.8`。

### Verification refresh (full goal acceptance after Docker AI and discovery, 2026-09-01)

- 最新完整 goal-acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-ai-discovery.json`（SHA-256
  `0FAA573E8AEDD83AC5A64E5E76B448FC4219D3D37FC560CF111B86FD85044B3C`）。脚本退出码 `0`，
  核心测试收集 `2167`、QGC 自定义测试 `66`，`software_status=pass_with_environment_gap`，
  `hardware_status=offline_pending`，并绑定当前 portable EXE SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。
- 该快照显式绑定现场 preflight 的 `hardware_link_ready=false`；运行时使用
  `-AllowMissingOperatorCredentials` 仅保留软件迁移验收，不代表 Windows 运行密钥、Jetson、
  室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View 已通过。版本继续保持
  `v0.3.0-pre.8`。

### Verification refresh (bounded field-link discovery rerun, 2026-09-01)

- 在当前台式机 `192.168.1.13/24` 上重新执行显式 `192.168.1.0/24` 只读发现，证据为
  `artifacts/evaluation/field-link-discovery-20260901-continue-subnet.json`（SHA-256
  `89F80BD5BD1276D28378484481ACBAF3F8C1865FB12D8605903250FF2D71EBC7`）。扫描 `254` 个主机，
  仅发现三个 SSH banner；MAVLink TCP/RTSP 分别为 `0` 个命中。发现器只识别服务，不登录、
  不请求视频/遥测、不发送飞控数据，也不会选择或改写 Jetson/GR01/相机端点。
- 该现场报告已绑定到总验收快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-subnet-discovery-v7.json`
  （SHA-256 `38AE6A318E53586DBC3ABA33BA4CAA3F4B5C5357BE4697ECC3C1A896CB55D0DB`）。由于
  `hardware_link_ready=false`，硬件、室外 GNSS、HIL/REAL 及最终 `v0.3.0` 发布门仍保持关闭。
- 基于当前 `configs/release-parity.json` 重建四模式 pre.8 清单并绑定该现场报告：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery.json`
  （SHA-256 `93C15C0BE48EF4EAEB1ED259A62321BC4F69998B5B13CD61801594812ABC77EB`），聚合报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery-report.json`
  （SHA-256 `0416E67F56EE143FC903446E8D18DCEDC2925D0EECE810F939321D1BC5A41BF5`）。五组 parity
  一致，但 `gate_passed=false`/`release_allowed=false`，因为现场、REPLAY 标注/精度、人工
  Fly View、HIL/REAL 和最终 release channel 仍未满足；该清单不改变 prerelease 状态。

### Working-tree delta (2026-09-01, field-link evidence verifier hardening)

- `verify_field_link_report(...)` 现在按 probe 列表重新计算单次报告的
  `all_passed`/`hardware_link_ready`，并在 watch 报告逐样本执行同一一致性检查；手工把摘要改成
  `true` 但保留失败端点时会 fail-closed，不会升级 REAL 门。
- 校验器读取 `utf-8` 与 Windows 常见的 `UTF-8 BOM` 两种 JSON，同时继续按原始字节计算和返回
  SHA-256，因此 BOM 兼容不改变证据绑定。新增 BOM 与摘要篡改回归；定向现场/发布门测试、Ruff、
  compileall 及完整 Python 回归均通过（`2164 passed / 1 skipped`；唯一跳过项仍是该 Windows
  账户无法创建 symlink）。

### Verification refresh (four-mode release-gate assembly, 2026-09-01)

- 先前装配的 `release-gate-pre.8-current-20260901.json` 与其报告保留为历史 provenance；
  随后的 SIM rerun 清单/报告也作为中间 provenance 保留：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-sim-rerun.json`（SHA-256
  `923F78F3CD9A0444B601A448BE1C5E8E08554FC1DBB6E43BDC949461ACDD20CC`），聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-sim-rerun-report.json`（SHA-256
  `18E950A83661F0C48738F8C24C4E43700C8932F57D3318071E13164CEACB907A`）。当前权威清单/报告
  已在后续现场链路 rerun 段落中更新为 `-field-rerun` 文件。
- 本轮 SIM envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-rerun.json`（SHA-256
  `9862E26984487822DF9DCCB1D0673889C28ACD50DE1DFB02385AC829E11D516F`），绑定的源证据为
  `artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`，
  SHA-256 为 `5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`；该
  900 帧 combined PX4/Gazebo/QGC run 的传感器 readiness、VTOL、PX4 Mission、原生 QGC
  QQuickItem 抓帧和清理机器门均通过，源 `gate_passed=true`。
- 报告中五组 parity 全部一致，但人工视觉、Mission 状态、HIL 硬件/real-control、REAL 全部
  状态仍保持 `not_claimed`，现场链路 `hardware_link_ready=false`，`replay_set` 尚未绑定，
  因此四模式聚合 `gate_passed=false`、`release_allowed=false`。版本继续为
  `v0.3.0-pre.8`，没有提前生成最终 `v0.3.0`；机器 QQuickItem 证据不替代人工 Fly View
  点验、室外 GNSS、真实 Pixhawk/执行器或 HIL/REAL。
- 随后对当前 `192.168.1.0/24` 做了限定网段的只读发现，证据为
  `artifacts/evaluation/field-link-discovery-20260901-continue.json`（SHA-256
  `97F0A7FF64DB50C448DD5923D57617CFA8BFAAAEAAD02C46EE4D8D6062DF3273`）。254 个地址中仅
  3 个 SSH 端口响应，MAVLink `5760` 和 RTSP `554` 命中均为 `0`；发现结果不选择端点，
  `hardware_link_ready` 继续为 `false`。接通飞机专用网段或提供实际机载 IP 后再做显式
  Jetson/GR01/相机预检。
- 现场报告校验器与 PowerShell 空参数修复后的最新完整 goal-acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-verifier-hardening-v2.json`
  （SHA-256 `A0FE03A238DF5CBC84E513D560C6FFE64CFE790679D0D85DE02C00B9AD069217`）。该快照记录
  `core_tests_collected=2166`、QGC 自定义 `66`、QML/C++/portable smoke 全通过，软件状态为
  `pass_with_environment_gap`、硬件仍为 `offline_pending`；没有把空现场链路或凭据覆盖写成通过。
- 该次 acceptance 刷新了 fixed-wing software evidence，因此重新封装 HIL envelope
  `artifacts/evaluation/release-mode-hil-20260901-pre.8.json`（SHA-256
  `4F905FBC6221A5A7442DAA8F9507B427F7ED3782585A62625E04AA8C030BCDDA`）；随后同步重建当前
  E7 清单/报告，避免 HIL 源文件 SHA-256 漂移造成隐性失配。

### Verification refresh (combined SIM rerun, 2026-09-01)

- Docker Desktop 恢复可用后重新执行 900 帧 `run_px4_gazebo_qgc_combined_sim_acceptance.ps1`，
  run `20260831T234111Z-53232` 退出码为 `0`。源证据记录 `gate_passed=true`、900/900
  传感器/RTSP/metadata 帧、Mission-ready 后连续 type-24 ACK、VTOL MC→FW→MC、容器清理、
  端口释放和 protected UDP `14550` 不变；`operator_visual_acceptance` 仍未声明。
- 本次重新封装和 E7 聚合只替换 SIM provenance，不改变四模式发布门：五组 parity 仍一致，
  但人工 Fly View、Mission 状态、HIL/REAL 硬件与 real-control、现场链路和 replay-set
  仍未闭合，所以报告保持 `gate_passed=false`、`release_allowed=false`，版本仍为
  `v0.3.0-pre.8`。

- 同一台式机随后重跑独立 live Docker AI armed-heartbeat 闭环，证据为
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-sim-rerun.json`（96,607 bytes，
  SHA-256 `80A21B60271E9AEC9C41AB9346FB01D66C93DD499B6B6AAA45A3FB70170C95FF`）。
  `runtime_gate_passed=true`、`telemetry.qualified=true`、`armed_observed=true`、
  `ai_policy_gate_passed=true`、`ai_steps=1`；共享 `SimulatorMavlinkControlAdapter` 发出
  一个 AI policy `MANUAL_CONTROL`，随后发送 disarm，容器 stop/rm 清理成功。该证据仍是
  disposable PX4/Gazebo AI machine gate，不替代真实 Pixhawk/Jetson/GNSS、执行器、HIL/REAL、
  实飞或人工 Fly View。
- 同时刷新了不依赖外部进程的 SIM 逻辑回归：
  `artifacts/evaluation/simulator-ai-acceptance-20260901-sim-rerun.json`（SHA-256
  `4AAAE9EB5CCC46CC6399270ABCA09B19EE6F56ACCF9DE8118E1B787A0C6FBE8E`）验证 hover/track、
  manual takeover、AI resume 和 link-loss failsafe；
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-sim-rerun.json`（SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）验证 VTOL 场景的
  manual/AI/watchdog 共享 authority、资产哈希和 `truth_free`。两者均为软件/协议门，外部
  `px4`/`gz`、桌面视觉和硬件字段仍按实际状态保持未声明。

### Verification refresh (latest field-link probe rerun, 2026-09-01)

- 在台式机上再次执行单次只读现场链路预检，原始证据为
  `artifacts/evaluation/field-link-preflight-20260901-continue-latest.json`（2351 bytes，SHA-256
  `67FAD84F26C1DDC0301CD0197284AC70FE93281BC7773A9597093B58EFF68AC2`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760` 与相机 `192.168.144.108:554` 均超时，
  本机 `14580/14581` 可绑定；`hardware_link_ready=false`，`--require-all` 退出码为 `1`。
- 该最新现场报告已绑定到
  `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json`（SHA-256
  `A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94`）及聚合报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json`（SHA-256
  `746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43`）。四模式报告继续
  `gate_passed=false`、`release_allowed=false`；该刷新只更新现场 provenance，不提升硬件、
  GNSS、HIL/REAL、人工 Fly View 或实飞门。
- 台式机迁移后的完整 Python 回归再次通过：`2166 passed / 1 skipped`（唯一跳过为 Windows
  账户无法创建 symlink），Ruff、compileall 与 CRLF-aware `git diff --check` 均为退出码 `0`；
  Windows Release/Ninja 增量构建为 `ninja: no work to do`，portable simple-boot smoke 退出码
  `0`，当前 portable EXE SHA-256 为 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。

### Working-tree delta (2026-09-01, SIM replay-set staging fixture)

- 新增 `scripts/create_sim_replay_set_fixture.py`：把已通过 Gazebo/Harmonic 相机捕获门的
  raw recording 复制为 `day/night/haze/reflection/smoke_only/no_fire` 六层 SIM replay-set，
  并为每帧写入显式空基线真值/预测。脚本复用现有 `RawSensorReplayRecording` 和
  `ReplaySetManifest`，直接从 clean checkout 运行，不改变生产感知或控制链路。
- 当前 staging 产物为 `artifacts/replay-sets/sim-gazebo-2026/`，每层 5 帧；manifest SHA-256
  为 `C2078C778BE12E688CD903339C1CA46E698AE5BCAF83675FD01BDC9AFDC537D3`，report SHA-256
  为 `3DAAECBC63AC5BB512B60D9817DF5B1CFF0F0FA8995930D8749583A8C242B319`。六层结构、文件
  哈希和帧对齐均通过，评估器正常运行，但 `source_kind=synthetic_demo`、标注未复核且空基线
  不含检测真值，因此 `deployment_domain_evidence_complete=false`、`accuracy_validated=false`。
- 新的四模式中间清单/报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-replay-staging.json`（SHA-256
  `1860EF768C6E9C645CFE7023078A177C7985A207BF9C27760A52871441AC4A1A`）和
  `release-gate-pre.8-current-20260901-replay-staging-report.json`（SHA-256
  `E1C1E0FB3D8A9D0EA8997ABBAD4124C59EFE795B8FBE91FC391884B95189C2E2`）。聚合器现明确记录
  `replay_set.present=true`、`passed=false`；该 SIM staging 不提升室外 GNSS、真实相机精度、
  HIL/REAL 或最终 `v0.3.0` 发布门。
- 追加生成 replay-set 绑定补充清单/报告：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound.json`
  （SHA-256 `261CB2755E55013278E9BDEA698AC80D110B55A09F79193C52DD91A4AB27B75C`）和
  `release-gate-pre.8-current-20260901-field-rerun-replay-bound-report.json`
  （SHA-256 `DDE5D24A785D07AE1F40D21EBA8D27B379EBD0B5CAE5CBC9633786D50FDA96C8`）。聚合器确认
  `replay_set.present=true` 但 `passed=false`（合成数据尚未人工标注/独立复核），现场链路仍未就绪；
  该补充快照不替换 field-rerun 权威清单，也不提升发布门。

### Verification refresh (2026-09-01, workstation network state)

- 重新执行完整 goal-acceptance 后，最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-network-refresh.json`，
  SHA-256 `7D070D0F49547D0569308959E8184E944FEDDAB7831C4A13B9BF659D2CC18458`。
  收集数为 `2167`（`2166 passed / 1 skipped`），QGC 自定义回归 `66/66`，软件状态
  `pass_with_environment_gap`，硬件状态 `offline_pending`。
- 快照绑定现场报告 `field-link-preflight-20260901-continue-latest.json`，并记录物理
  `Ethernet=Up`、`192.168.1.13/24`、无 `192.168.144.0/24` 路由以及断开的 `Ethernet 2`
  TAP；Jetson/GR01/相机端点仍未就绪。该刷新只更新 provenance，不提升 REAL/HIL、室外
  GNSS、真实执行器或人工 Fly View，也不创建 `pre.9` 或最终 `v0.3.0`。
- 同日运行 `scripts/capture_qgc_native_style.ps1` 启动并清理便携 QGC；由于 Task Manager
  覆盖 exact QGC client rectangle，捕获按单一原生界面规则停止。观察报告
  `artifacts/evaluation/qgc-native-style-20260901-continue/process-observation.json`（3423 bytes，
  SHA-256 `0EDEB12E5BC121BB7AFE86947BC22A75420A7605C9997C9B21FB4B5D61F7F81E`）记录
  `single_native_surface_machine_check=false`、`operator_visual_acceptance=not_claimed`，且
  没有残留 QGC 进程。捕获脚本新增显式 `-HideStubbornOccluders` 兜底，并且只在确认窗口
  真正隐藏后计入 hidden 数量；本机 Task Manager 属于更高完整性窗口，仍无法被普通
  PowerShell 临时隐藏，需手工最小化/关闭后重跑。
  带 `-HideStubbornOccluders` 的重试仍被同一高完整性 Task Manager 阻断；最新观察报告
  `artifacts/evaluation/qgc-native-style-20260901-continue-hide-v2/process-observation.json`
  （3574 bytes，SHA-256 `4464220068A428BC0DAFCC96CAAE3B60D3C9A1C21A640800FF74627A0521B23B`）确认
  `hidden_occluder_count=0`、`single_native_surface_machine_check=false`，且没有残留 QGC 进程。

### Working-tree delta (2026-09-01, replay-set accuracy evidence)

- 新增 `src/multidetect/replay_set_evaluation.py` 与
  `scripts/evaluate_replay_set.py`：对六类分层回放集逐条复用共享 IoU evaluator，记录每条
  真值/预测文件摘要、整体 precision/recall、误报/漏检帧率和 p95 推理延迟，并把回放集
  raw/canonical SHA-256 写入精度报告。
- 精度报告把结构完整、预测齐全、指标阈值通过和独立复核拆成独立状态；缺预测、阈值失败或
  没有显式 `--reviewed --reviewer` 时保持 `gate_passed=false`，不把“有预测文件”当成模型
  准确率验收。新增 `configs/schemas/replay-set-accuracy-report.schema.json`。
- E7 release-gate 支持 `--replay-set-accuracy-evidence`：校验事件类型、报告文件 SHA-256、
  回放集摘要绑定和 `accuracy_validated=true`；最终 `release` 通道在完整回放集之外还必须
  提供这份独立精度证据。当前仍未改变 `v0.3.0-pre.8` 版本和真实 Jetson/GNSS/Pixhawk、
  HIL/REAL 未验收状态。

### Verification refresh (replay accuracy integration, 2026-09-01)

- `scripts/run_goal_acceptance.ps1` 已在当前 pre.8 源码上重新执行，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-replay-accuracy.json`
  （SHA-256 `F1A10EE040DD045764A7CD37FFF655AAE3DF3E86B429B4A215422A5B698D5432`）。脚本退出码为 `0`，
  核心测试收集 `2128`，QGC 自定义回归 `66/66`，协议自测、Ruff、compileall、CRLF-aware diff check
  和 portable headless smoke 均通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 这份证据只覆盖回放集 evaluator、raw/canonical 哈希绑定和 E7 wiring；未提供真实 Jetson、GNSS、
  Pixhawk、室外真值、执行器、HIL/REAL 或人工 Fly View 结果，因此开发版本仍保持 `v0.3.0-pre.8`。

### Verification refresh (field-link watch, 2026-09-01)

- 现场链路预检以 `--protocol-checks --samples 3 --interval-s 0.1 --require-all` 重复三次，证据为
  `artifacts/evaluation/field-link-preflight-20260901-watch3.json`（SHA-256
  `52C7EC568A4390B3DD334B6E27B67516C9524B4F9B0A5D823B57FCB50869B7C7`）。三个样本均采集完成但均失败：
  Jetson SSH、GR01 MAVLink、相机 RTSP 全部超时，`14580/14581` 本地 UDP 端口均可绑定，
  `stability_ratio=0.0`、`max_consecutive_failures=3`、`hardware_link_ready=false`，命令退出码为 `1`。
- watch 保持只读，不登录、不请求 MAVLink stream、不启动 RTSP 视频、不发送飞控控制。当前活动网卡仍为
  `192.168.1.13`，需要接通飞机 Ethernet 或确认实际机载网段/IP 后再复跑；该证据不提升任何 GNSS、
  Pixhawk、执行器、HIL/REAL 或室外飞行状态。

### Working-tree delta (2026-09-01, field-link evidence binding)

- `field_link_preflight` 新增 `verify_field_link_report()`：对保存的单次/多次现场预检报告重算文件
  SHA-256，检查 `all_passed`/`hardware_link_ready` 一致性及 watch 样本完整性，并在报告损坏、篡改或
  readiness 未通过时保持 fail-closed。
- `field_session_capture` 与 `field_session_acceptance` 新增可选的
  `--link-preflight ... --require-link-preflight`；现场会话报告记录预检路径、字节数、SHA-256、摘要和
  复核结果，交接复核时再次哈希该文件。未指定参数的既有离线 fixture 行为保持不变。
- 新增链路绑定/报告篡改/缺失路径回归；这只是现场证据编排增强，不把当前
  `artifacts/evaluation/field-link-preflight-20260901-watch3.json` 的失败结果升级为硬件通过。版本仍为
  `v0.3.0-pre.8`，真实 Jetson/GNSS/Pixhawk、执行器、HIL/REAL 与人工 Fly View 门继续 pending。
- 增加独立 `verify_field_link_preflight.py` / `multidetect-field-preflight-verify` 入口：现场交接可以
  在不重新探测设备的情况下复核链路报告 hash、摘要和 watch 样本；默认仍要求
  `hardware_link_ready=true`，`--allow-unready` 只用于失败报告的结构诊断。

### Working-tree delta (2026-09-01, goal-acceptance field-link binding)

- `scripts/run_goal_acceptance.ps1` 新增 `-FieldLinkPreflight` 与
  `-RequireFieldLinkPreflight`。总验收现在可以绑定一份已保存的现场链路预检：重算文件
  SHA-256/字节数，调用独立 verifier，并把 `verified`、`verification_exit_code`、
  `hardware_link_ready`、`gate_passed` 和原因写入快照；要求模式遇到未就绪或篡改报告立即
  fail-closed。未传参数时仍保留离线软件验收语义。

### Verification refresh (goal-acceptance field-link binding, 2026-09-01)

- 最新 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-link-bind.json`（SHA-256
  `5842826C15783B55A662CA4A81F73096BFFA31EB0D343E97A40255A666E0CAA6`）。脚本退出码为 `0`，
  核心测试收集 `2137`，QGC 自定义回归 `66/66`，软件状态为 `pass_with_environment_gap`，
  硬件状态为 `offline_pending`；已知三采样 watch 被完整记录为 `verified=true`、
  `hardware_link_ready=false`、`verification_exit_code=1`，未被升级成硬件通过。严格绑定该报告的
  行为回归确认脚本立即以非零退出，现场恢复后可复用同一命令和报告链。

### Working-tree delta (2026-09-01, E7 REAL field-link release binding)

- `release_gate_manifest.py` 新增 `--real-field-link-preflight` 与同名 API 参数，把现场预检报告的
  相对路径、SHA-256、字节数、`hardware_link_ready` 和 `gate_passed` 写入顶层 `real_field_link`；
  预发布仍兼容省略该字段，显式提供的失败/篡改报告则立即关闭 evidence gate。
- `release_gate.py` 每次聚合都会重新哈希绑定报告并调用严格的
  `verify_field_link_report(..., require_ready=True)`，检查 readiness、gate、字节数和摘要是否一致；
  最终 `release` 通道额外强制要求存在且通过的 `real_field_link`，不会接受 REAL mode 自报硬件状态作为替代。
- `configs/schemas/release-gate-manifest.schema.json` 和发布门文档补充该绑定契约，新增 schema/CLI/篡改
  回归；当前源码仍为 `v0.3.0-pre.8`，没有提前切换到最终 `v0.3.0`。

### Working-tree delta (2026-09-01, bounded field-link discovery)

- `field_link_preflight` 增加显式 `--discover-cidr`/`discover_field_link_hosts()`：在操作员给出的
  IPv4 CIDR 内并发识别 SSH、MAVLink TCP 和 RTSP 服务，默认最多 `256` 个地址、`32` 个 worker，
  每个服务使用有界超时。协议模式仍然只读：SSH 读取 banner、MAVLink 查找完整帧、RTSP 发送
  `OPTIONS`，不登录、不请求 stream、不启动视频、不发送飞控控制。
- 发现结果只写入诊断性的 `discovery` 节点，不会自动选择 Jetson/GR01/相机地址，也不会改变
  `hardware_link_ready`。候选地址必须再次通过显式三端点预检，随后才能绑定到 goal/release evidence。
- 新增 CIDR 上界、候选主机过滤和并发确定性回归；版本仍为 `v0.3.0-pre.8`，真实硬件门不因发现
  结果提前关闭。

### Verification refresh (bounded field-link discovery, 2026-09-01)

- 当前台式机活动网卡 `192.168.1.13/24` 已执行一次 `--discover-cidr 192.168.1.0/24
  --protocol-checks` 短超时扫描，证据为
  `artifacts/evaluation/field-link-discovery-20260901-043102.json`（SHA-256
  `2CAA5D70F336E2E2A641B19383EA32658CF062C668DDF7CE13EB7CF1E96BB8DC`）。扫描 `254` 个地址，
  仅发现 `192.168.1.1`、`.62`、`.77` 的 SSH banner，`5760/MAVLink` 与 `554/RTSP` 均为 `0` 命中。
- 这些地址只保留为候选服务证据，未识别为 Jetson/GR01/相机，也未写入运行配置；正常三端点预检、
  GNSS/RANGE_STATUS/执行器、HIL/REAL 门仍保持 fail-closed。版本继续为 `v0.3.0-pre.8`。
- 增加 `service_candidates` 后在同一网段完成复核，最新报告为
  `artifacts/evaluation/field-link-discovery-20260901-043203.json`（SHA-256
  `68BD8F7A1581A494EB3706AF4C5F068761AF8B9A3750C43BDCD69B2DFA413DF2`），结果与首轮一致：
  SSH 三个候选、MAVLink/RTSP 零命中。

### Verification refresh (goal acceptance after discovery validation, 2026-09-01)

- 补充发现器非法端口回归后重新执行总验收，当前 pre.8 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-discovery-v4.json`（SHA-256
  `56F32DE50577E6A171CF963E32F87EF2DB2D130E20315DBEF7531A713AEF6968`）。脚本退出码 `0`，核心测试
  收集 `2143`、QGC 自定义回归 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  绑定的三采样现场 watch 仍 `hardware_link_ready=false`，因此没有提前关闭真实设备门或最终版本。

### Verification refresh (SIM external runtime diagnosis, 2026-09-01)

- `runtime_preflight` 现在在 daemon 不可用时读取最近的 Docker backend 日志尾部，输出有限的
  `docker_diagnostics`（schema、错误摘要、归因和建议动作），不启动/重启 Docker，也不写入原始日志；
  诊断行和 CLI stdout 均做 ASCII/JSON 安全编码，避免 Windows GBK 控制台因日志中的 Unicode 字符退出 2。
- 重新执行 `python -m multidetect.simulator runtime-preflight` 后，证据为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-diagnostics-v4.json`（文件 SHA-256
  `CF1E9DD6EA07A663C7284504A6200244945DE6BFA989A35B62264D9A5F59D8CB`；canonical digest
  `3b91e5e4d0996412f73fca3f1b50ba4bedf23601789db427f47603b178cf8fac`）。Docker client 28.4.0
  存在但 server probe `TimeoutExpired`，PX4/Gazebo 仍不在 PATH，`ready_to_attempt=false`。
- 诊断日志定位到 Docker Desktop backend 无法移除陈旧的
  `C:\Users\admin\AppData\Local\Docker\run\dockerInference` reparse/socket 条目；这只解释
  本机外部 SIM runtime 不可用，不把 WSL/进程存在升级为 3-D 渲染、PX4 telemetry、HIL/REAL 或
  硬件通过。版本继续保持 `v0.3.0-pre.8`。

### Working-tree delta (Docker stale-socket recovery helper, 2026-09-01)

- 新增 `scripts/recover_docker_desktop_stale_socket.ps1`：默认只读盘点当前账户的
  `dockerInference` reparse/socket，`-Apply` 先尝试移动到时间戳备份目录，只有显式
  `-RemoveStaleEntry` 才允许执行精确的 `fsutil reparsepoint delete` + 删除，
  `-RestartDocker` 仅在 `-Apply` 后启动 Docker Desktop；脚本不接触镜像、容器、卷或其它
  Docker run 条目。
- 新增 `tests/test_docker_recovery_script.py`，覆盖只读默认、精确路径/reparse 校验、可逆移动优先、
  最后手段开关和隐藏重启行为。PowerShell parser、该测试及现有 SIM/field-link 聚焦回归均通过；
  这只是环境恢复工具，不改变 `v0.3.0-pre.8` 或任何硬件/REAL gate。
- `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在在 Docker `info`/镜像操作前调用同一只读
  `runtime-preflight`，把预检 JSON 写入组合验收目录，并在 `ready_to_attempt=false` 时 20 秒内
  带着诊断 fail-fast；Docker 恢复后直接重跑原有联合入口即可。

### Verification refresh (combined SIM fail-fast smoke, 2026-09-01)

- Windows PowerShell/.NET Framework 烟测确认联合入口在约 4 秒内因
  `ready_to_attempt=false` 退出，未创建容器、QGC、RTSP 或飞控进程；只读预检写入
  `artifacts/evaluation/combined-sim-qgc-20260831T213201Z-74784/runtime-preflight.json`（文件
  SHA-256 `40C947334A08C6CEFFB7F5F8EEDF699FF66E342D49C3BCD5878162D8008A3FE3`，canonical digest
  `b75146423ce09c9ebccd14f5acc391e79dedec8f9674b030c7d3b3b6edfe9147`）。此前
  `ProcessStartInfo.ArgumentList` 在 Windows PowerShell 5.1 的空值错误已修复为
  `CommandLineToArgvW` 兼容的 quoted-Arguments fallback；PowerShell parser 和静态回归均通过。
- 该烟测只验证启动边界和 fail-fast 证据，不提升 Docker/PX4/Gazebo、GNSS、Jetson、HIL/REAL 或最终
  `v0.3.0` 状态。

### Verification refresh (Windows PowerShell argument compatibility, 2026-09-01)

- 补充 `CommandLineToArgvW` quoted-Arguments fallback 的静态回归并重跑总验收，当前 pre.8 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-recovery-v3.json`（SHA-256
  `D5242D8B09C9108BC47ADE2F92CD9D26450EF56F04F9445C2B4B8A39AD7A3BE9`）。脚本退出码为 `0`，核心测试
  `2152`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 该快照只更新 Windows PowerShell 5.1 的启动兼容性与 fail-fast provenance；Docker stale socket、
  PX4/Gazebo 外部 3-D、Jetson/GNSS/Pixhawk、HIL/REAL 和最终 `v0.3.0` 门仍保持原状态。

### Verification refresh (latest field-link sample, 2026-09-01)

- 重新执行一次只读现场协议预检，证据为
  `artifacts/evaluation/field-link-preflight-20260901-continue-20260901-053931.json`（SHA-256
  `6FAF98209F6B7ABFD3231D84C49CE1F22870B5DB0C7AFE646CE0D9148A410B53`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760` 与相机 `192.168.144.108:554` 均超时，
  本机 `14580/14581` 可绑定；`samples_collected=1/1`、`hardware_link_ready=false`，
  `--require-all` 退出码为 `1`。现场报告仍是只读诊断，不发送登录、MAVLink stream、视频或飞控控制数据。
- 使用该最新报告刷新总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-refresh-20260901-053959.json`
  （SHA-256 `1E79B2BD511AD1C4FB53BBDF522DDB2FCA53AA860960795891A90E5158881A69`）。脚本退出码为 `0`，
  核心测试 `2152`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  该刷新只更新现场 provenance，不关闭 Jetson/GNSS/Pixhawk、执行器、HIL/REAL、人工 Fly View 或实飞门。

### Verification refresh (Docker daemon stopped-state probe, 2026-09-01)

- 在 Docker Desktop 进程完全停止后重跑只读 `runtime-preflight`，证据为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-continue-20260901-054222.json`
  （文件 SHA-256 `F7BA35ACA79E39A225F4CA65841A253E323745244E860EAA661E2BE209DFB5F8`；canonical
  digest `351faffb851922ed0643af310727b2135cf192834e2a6771d26c21a4d7635ae4`）。Docker client
  `28.4.0` 仍存在，但 server named pipe 返回“找不到文件”，PX4/Gazebo 不在 PATH，
  `ready_to_attempt=false`；backend 日志仍将故障归因为遗留 `dockerInference` reparse/socket。
- 该复探只记录停机后的 fail-closed 状态，不启动 Docker、PX4/Gazebo、容器、QGC 或现场设备；管理员
  清理单一陈旧条目并重新启动 Docker 后，必须再次运行同一 `runtime-preflight`，然后才能尝试外部 SIM gate。

### Verification refresh (preflight evidence retention fix, 2026-09-01)

- 修复 `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 在预检通过后被后续变量初始化覆盖的问题；
  最终 `combined-evidence.json` 现在保留 `runtime_preflight.readiness` 与限量
  `runtime_preflight.docker_diagnostics`，而不是只留下文件路径和哈希。新增静态回归锁定解析顺序，
  PowerShell parser、Ruff 与聚焦测试均通过。
- 修复后再次执行总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-refresh-20260901-054426.json`
  （SHA-256 `0B1A8F418281C7AC04B32EAB857E0D34B57C80B4F7B976D56F7DCA5D9DB2DA14`）。脚本退出码为 `0`，
  核心测试 `2152`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  真实设备、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 门仍保持未关闭。

### Verification refresh (goal acceptance after Docker recovery helper, 2026-09-01)

- 使用当前仓库 `.venv` 重新执行总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-recovery.json`（SHA-256
  `597EA3B619FECD5F38B8E92697EB69252D2CCC9349F99257909996616DD0FF6E`）。脚本退出码为 `0`，核心测试
  `2150`、QGC 自定义测试 `66/66`，Ruff、compileall、QML 解析、协议自测和 portable headless
  smoke 全部通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- Docker backend 的 `dockerInference` 陈旧 reparse/socket 仍需现场管理员 PowerShell 清理，
  当前证据没有把脚本静态回归或确定性 SIM 升级为外部 PX4/Gazebo、Jetson、室外 GNSS、执行器、
  HIL/REAL 或最终 `v0.3.0` 通过。

### Verification refresh (combined SIM fail-fast integration, 2026-09-01)

- `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 已在 Docker `info`、镜像拉取和容器操作之前调用
  同一 `runtime-preflight`，并把预检路径、readiness 和限量 `docker_diagnostics` 写入组合证据；
  runtime 不可用时在 20 秒内 fail-fast。
- 重新执行总验收后的最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-recovery-v2.json`（SHA-256
  `2CF2F1F653E7E5B586114F9878255DBE582F8CE150C4C4272DE8FC129756ED75`）。脚本退出码为 `0`，核心测试
  `2151`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  Docker/PX4/Gazebo 环境缺口和现场硬件门仍保持 fail-closed。

### Verification refresh (goal acceptance after runtime diagnostics, 2026-09-01)

- 使用仓库规定的 `ground-control/MultiDetectGCS/.venv` 重跑总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-runtime-diagnostics.json`（SHA-256
  `3169F3E8A6E48C6B06BB9D55299913833F5F46A2E25C61EEC076777B116753D3`）。脚本退出码 `0`，核心测试
  `2146`、QGC 自定义测试 `66/66`，Ruff、compileall、QML 解析、协议自测与 portable headless
  smoke 全部通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 快照继续绑定 `field-link-preflight-20260901-watch3.json` 的 `verified=true`、
  `hardware_link_ready=false`、verifier exit `1`；当前网卡没有飞机网段/串口，Docker/Gazebo/PX4
  外部 3-D 运行时也没有就绪，故不关闭真实 GNSS、Pixhawk、执行器、HIL/REAL 或最终 `v0.3.0` 门。

### Verification refresh (E7 REAL field-link binding, 2026-09-01)

- 重新执行总验收后的快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate.json`（SHA-256
  `00F4A58B2CC809665F1580CAB5E143251387ABE671EC7D6BBC793240C97610E2`）。脚本退出码为 `0`，核心测试
  收集 `2140`，QGC 自定义回归 `66/66`，Ruff、compileall、CRLF-aware diff check、协议自测和 portable
  headless smoke 均通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 快照绑定 `field-link-preflight-20260901-watch3.json`（SHA-256
  `52C7EC568A4390B3DD334B6E27B67516C9524B4F9B0A5D823B57FCB50869B7C7`），报告复核为
  `verified=true`、`verification_exit_code=1`、`hardware_link_ready=false`；Jetson/GR01/RTSP 均未接通，
  所以没有关闭真实 GNSS、Pixhawk、执行器、HIL/REAL、人工 Fly View 或实飞门。

### Verification refresh (strict writer fail-closed, 2026-09-01)

- 新增回归要求：显式传入未就绪的 `--real-field-link-preflight` 时，生成器默认返回退出码 `2`；
  只有 `--allow-incomplete` 才会写出该失败诊断清单。这样预发布可保留现场失败证据，同时不会把
  writer 的成功退出误读成现场通过。
- 重新执行总验收后的最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate-v2.json`（SHA-256
  `7075D414DEF8A809AFEB7FB5BDD41DFE8008F80C4FE08C0228596546D01386C0`）。脚本退出码 `0`，核心测试
  收集 `2141`，QGC 自定义回归 `66/66`；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
  三采样 watch 绑定仍为 `hardware_link_ready=false`、`gate_passed=false`、verifier exit `1`，未升级
  任何真实链路或最终 `v0.3.0` 发布门。

### Verification refresh (field-link evidence binding, 2026-09-01)

- 当前 pre.8 goal acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-preflight-cli.json`（SHA-256
  `6583EF2F2B0744439EF3255C2529EC9A832B17332682C1E41A8AFD7945C75099`）。脚本退出码为 `0`，核心测试收集
  `2136` 项，QGC 自定义回归 `66/66`，协议自测、QML 解析、Ruff、compileall、CRLF-aware diff check
  与 portable headless smoke 均通过；软件状态为 `pass_with_environment_gap`，硬件状态为
  `offline_pending`。现场三采样 watch 仍为 `hardware_link_ready=false`，因此未关闭真实 Jetson/GNSS/
  Pixhawk、室外真值、执行器、HIL/REAL 或人工 Fly View 门。

### Working-tree delta (2026-09-01, outdoor range calibration)

- 将 `src/multidetect/depth_calibration.py` 扩展为可审计的单调分段线性模型：至少 6 条实测样本和 4 个
  严格递增 knot；重复 raw-depth 读数按中位数合并，明显的实际距离顺序冲突与孤立错配在保留最低证据量的
  前提下剔除，PAVA 负责小幅非单调噪声收口。拟合结果仍保留 affine 兼容参数，但运行时优先使用曲线。
- 标定文档新增 schema v2 的 `calibration.mode=piecewise_linear`、raw/actual breakpoints 和最近端线性
  外推声明；schema v1 affine 文档保持向后兼容。`MetricDepthEstimator`、CLI 和 live audit metadata 现在
  记录曲线模式、实测 raw 范围、外推状态，并在域外估计时提升 `σ` 下限。
- `scripts/fit_outdoor_depth_calibration.py` 新增 `--model piecewise_linear`，
  `scripts/validate_outdoor_depth_calibration.py` 对 affine/curve 使用同一映射器；新增曲线往返、坏曲线
  fail-closed、运行时插值和外推不确定度回归。当前 canonical parity 已把 depth calibration/metric depth
  纳入 `core_algorithm`，hash 为 `b26c8709b2ff52e028852cb2d468da354bfa4f1d41a0c3d77112fce0dceed064`。
- 当前台式机仍未连通 Jetson、相机或室外 GNSS；本轮只证明软件拟合/加载/映射链，不能替代现场多距离真值、
  HIL/REAL 或人工 Fly View 验收。开发版本继续为 `v0.3.0-pre.8`。

### Verification refresh (outdoor piecewise, 2026-09-01)

- 全量 goal acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-outdoor-piecewise-v33.json`
  （SHA-256 `013307F219F289A5D7F2A6CE306E472625E2C08F6FF9F484452C708747D59A57`）。脚本退出码为 `0`，
  核心测试收集 `2109`，QGC 自定义回归 `66/66`，QGC protocol/QML/Ruff/compileall/CRLF-aware diff
  check 和 portable headless smoke 均通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`、
  `operator_visual_acceptance=not_claimed`。
- canonical `configs/release-parity.json` 与审计快照
  `artifacts/evaluation/release-parity-pre.8-20260901-outdoor-piecewise-v32.json` 字节一致，文件 SHA-256
  `4ED355018B24A093FB7276E26DFACEBCCB5C826BF2DD04A6B0491EB810A91B0F`，新增的
  `depth_calibration.py`/`metric_depth.py` 已包含在 `core_algorithm` parity；portable EXE 仍为
  `v0.3.0-pre.8`、SHA-256 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。
- 该轮只验证室外曲线拟合、schema v2 加载和运行时映射；台式机现场预检仍显示 Jetson、GR01 和 RTSP
  端点超时，所以真实 GNSS、Pixhawk、执行器、HIL/REAL、室外真值和人工 Fly View 门继续保持 pending。
- 同日按当前 pre.8 源码重新执行三条 SIM 闭环：
  `simulator-closed-loop-acceptance-20260901-pre.8-v2.json`（SHA-256
  `11195BBC7B8E28082A446524040A32F8D026B7B9C87164F85389771C17ECA949`）、
  `simulator-ai-acceptance-20260901-pre.8-v2.json`（SHA-256
  `6E0F467B2947C8E1AED0DA333D9520F324B61AF8A53EEF4EC12BE595FE1F84CE`）和
  `simulator-3d-control-acceptance-20260901-pre.8-v2.json`（SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）均为
  `gate_passed=true`；三份证据仍明确标注外部 PX4/Gazebo、GPU/人工画面、Pixhawk/Jetson/相机、
  HIL/REAL 和室外 GNSS 为未验收状态。
- 新增 `python -m multidetect.simulator runtime-preflight --out <evidence.json>` 只读预检，
  统一记录 native PX4/Gazebo、Docker daemon 和 Windows WSL 状态，并用
  `ready_to_attempt`/`execution_mode` 与 `process_started=false`、`telemetry_observed=false`
  明确工具发现不等于外部运行。当前台式机证据为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-pre.8.json`（文件 SHA-256
  `E973A4F3B93B255F61D8F600E4D90F8EE251D9D106F010D83A12BB93538506C3`；内嵌 canonical digest
  `bef94984df6580a3caf062ee503697aceca2fb0f5924442c17fa8d643f0e7c9a`）：PX4/Gazebo 原生命令缺失，
  Docker daemon 探针超时，`ready_to_attempt=false`；版本继续保持 `v0.3.0-pre.8`。
- 新增 `ReplaySetManifest` 与 `scripts/create_replay_set_manifest.py`：六类
  `day/night/haze/reflection/smoke_only/no_fire` 分层目录现在可以把 raw recording、人工真值和
  可选预测 JSONL 逐文件绑定 SHA-256，并在加载时复核 recording 内容、帧 ID 顺序和类别集合。
  `python -m multidetect validate-replay-set` 将结构完整性与部署域覆盖分开报告；缺失分层、未复核
  标注、非 deployment recording 或尚未运行 IoU evaluator 都保持 gate/accuracy 为 false，不改变
  当前真实硬件、室外 GNSS、HIL/REAL 未验收状态。回放集 wrapper 现在和发布门 wrapper 一样，
  可在 clean checkout 中直接运行，不依赖预先设置 `PYTHONPATH`。
- E7 发布门现在可在顶层通过 `--replay-set-manifest` 绑定上述回放集的原始与 canonical SHA-256，
  重新校验六类覆盖、raw recording 和真值帧序；预发布旧清单保持向后兼容，最终 `release` 通道
  缺失完整回放集时 `release_allowed=false`。新增的 release-gate/replay-set 回归覆盖 pending
  回放集、哈希错配、最终版本缺失绑定和 clean-checkout wrapper 路径。
- `python -m multidetect.simulator interactive-acceptance --mode all` 同步覆盖三种机型，证据为
  `artifacts/evaluation/simulator-interactive-acceptance-20260901-pre.8-v2.json`（SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`），报告
  `gate_passed=true`、`qgc_is_sole_operator_ui=true` 和 `shared_ai_manual_authority=true`，
  外部 PX4/Gazebo、GPU/人工画面、真实硬件、HIL/REAL 与室外 GNSS 仍保持未验收。

### Working-tree delta (2026-08-31)

- 目标落图坐标域与 QGC wire 解码再收口为同一 fail-closed 规则：纬度严格位于
  `(-90, 90)`，经度允许 `[-180, 180]`。Python `TargetGeolocation`、目标地理位置消息模型、
  QGC C++ `TargetGeolocationStatus` 解码器、地图/复制/快照/JSONL 重载均拒绝精确 `±90°` 极点，
  避免极点缺少稳定局部经度方向时留下不可投影的假证据。协议自测新增已签名极点变异向量，
  与 stale source-age 向量一起覆盖 wire 层。
- 极点门收口后的完整 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json`
  （SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`）：核心测试收集
  `2101`，QGC 自定义测试 `66/66`，脚本退出码 `0`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`。Release/portable 主 EXE 为 `46,575,104` bytes，SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`；三项 C++ 协议/wire 自测
  （Operator、44-fragment depth-grid、RTSP）均退出 `0`。canonical parity 文件当前 SHA-256 为
  `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`，对应审计快照为
  `artifacts/evaluation/release-parity-pre.8-20260901-target-geo-pole.json`。Jetson/室外 GNSS、
  真实 Pixhawk/执行器、HIL/REAL 与人工 Fly View 仍保持 pending。

- 2026-09-01 在台式机上重新执行现场链路只读 watch：
  `artifacts/evaluation/field-link-preflight-20260901-followup.json`（SHA-256
  `1873247B49F30856483F22635CC28620458990BC78CA2C2A28BE2BD25355549E`）。命令使用
  `--protocol-checks --samples 3 --interval-s 1 --stop-on-failure --require-all`，首个样本即
  发现 Jetson SSH、GR01 MAVLink、相机 RTSP 均超时，本机 `14580/14581` 可绑定，
  `samples_collected=1/3`、`hardware_link_ready=false`，退出码 `1`；当前活动网卡 IPv4 为
  `192.168.1.13`，待接通飞机 Ethernet 或确认实际机载网段/IP 后复跑，不把该失败证据升级为
  GNSS、Pixhawk、执行器、HIL/REAL 或室外飞行通过。

- 将确定性 SIM 控制链扩展到 plain PX4 multirotor：`FlightMode.MULTIROTOR` 及
  `multicopter`/`quadcopter`/`x500` 别名贯通 `DeterministicFlightModel`、
  `SimulatorClosedLoop`、scenario runner、native QGC interactive fixture、stack-plan 和
  Docker fallback；非 VTOL 机型收到 `DO_VTOL_TRANSITION` 时 fail-closed，不改变飞行状态。
  新增 `simulation/gazebo/models/x500_mono_cam/`、
  `simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf` 与分离的
  `configs/scenarios/urban_inspection_multirotor_001.{runtime,truth,expectations,scenario}.json`，
  复用同一 WGS-84、相机、AI/manual authority、watchdog 和 truth-free 证据契约。
- 补齐三机型共用 PX4 Mission 路径中的 plain multirotor compiler：新增
  `MultirotorMissionGeometry`、`Px4MultirotorSafetyPolicy`、
  `compile_multirotor_executable_mission(...)` 和独立 adapter hash；固定输出
  `TAKEOFF → waypoints → LOITER_TIME → LAND` 的 `MISSION_ITEM_INT`，并沿现有
  upload/readback/commit/AUTO.MISSION 事务门复用 geofence、相对高度、盘旋、落点偏移和
  truth-free runtime 校验。新增 `tests/test_px4_multirotor_mission.py` 的 8 项回归全部通过。
  独立编译验收证据为 `artifacts/evaluation/multirotor-mission-compile-acceptance-20260831-continue.json`
  （SHA-256 `63CC13F887254591FEBFACD5AB323FF6006392C2B6C2BDBF447A59EB321BA84A`），其
  `gate_passed=true`、`item_count=5`、`no_vtol_transition=true`、`physical_output_enabled=false`。
  随后新增 `compile_executable_mission(...)` 与 `normalize_px4_vehicle_class(...)` 统一入口，
  接受 `x500`/`multicopter` 等 simulator 别名并严格配对三种 geometry/policy 类型；多旋翼
  编译验收改为通过该 dispatcher 验证统一的 `MISSION_ITEM_INT` 事务路径。
- dispatcher 别名与类型配对回归的独立证据为
  `artifacts/evaluation/multirotor-mission-compile-acceptance-20260831-dispatcher.json`
  （SHA-256 `ADF8BAB8D4887697A1CF09E54AB8C21CEBDD9ACA0312B0A89514FA613F14DAB2`），
  `gate_passed=true`、输入 `x500` 归一化为 `multirotor`，无 VTOL transition、无物理输出。
- dispatcher、三机型 Mission contract、现场预检和 portable smoke 收口后的整体验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-dispatcher-v24.json`
  （SHA-256 `15CC5F31CF4EEBD7500ED63DD093F96C5108AF5D2C980F4087B5CC0463B8C4D5`）；核心测试
  收集 `2083` 项，QGC 自定义回归 `66/66`，软件门 `pass_with_environment_gap`、硬件门
  `offline_pending`，因此仍属于 `v0.3.0-pre.8`。
- 协议级只读现场预检与 SSH 前置 banner/MAVLink v1-v2/RTSP OPTIONS 回归后的最新整体验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-protocol-v27.json`（SHA-256
  `8E474F06125E92A66A341B84EA9DC57C114257A0BDAB1255FF28EA49A1BFE675`）；核心测试收集 `2088`，
  QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`，
  当前仍是 `v0.3.0-pre.8`。
  v26 原文件保留 provenance；只有硬件/HIL/REAL 门全部关闭后才可切换最终版本。
- 稳定性 watch 与其回归后的最新整体验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-watch-v29.json`（SHA-256
  `DEF51B47D75987FE948AB6D2639128B2FBC99E68F11591CABF28542C24D9E96D`）；核心测试收集 `2094`，
  QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`，
  当前仍为 `v0.3.0-pre.8`。v27 原文件保留 provenance；watch 证据只证明重复预检的失败闭环，
  不升级 Jetson/相机/室外 GNSS、执行器、HIL/REAL 或人工 Fly View 门。
- 同轮现场协议预检证据为 `artifacts/evaluation/field-link-preflight-20260831-protocol-v3.json`（SHA-256
  `6DC0830C30A675AC13064EE5D3E7F5E31FA5D8BBA28D5E26219CF4E53EB78F30`）：Jetson/GR01/RTSP
  三个端点均超时，本机 `14580/14581` 可绑定，`protocol_checks_passed=false`、
  `hardware_link_ready=false`，`--require-all` 退出码 `1`；探测未发送登录、心跳、视频启动或飞控控制数据。
- 同一 portable EXE 的原生 `--offline --simple-boot-test` 冒烟退出码为 `0`，日志证据为
  `artifacts/evaluation/qgc-simple-boot-20260831-dispatcher.log`（SHA-256
  `39D9D3F102ABD986A0C1EB71880C8C5FF5C40CADA04A0F75C4B845388BEAB3AF`）。
- 多旋翼 deterministic 3-D asset/control gate 证据为
  `artifacts/evaluation/simulator-3d-control-multirotor-continue.json`（SHA-256
  `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`），
  `gate_passed=true`、`deterministic_gate_passed=true`、`PX4_SYS_AUTOSTART=4001`；
  外部 PX4/Gazebo 工具不可用，故 `external_runtime_gate_passed=false`，不提升为视觉、硬件、
  HIL/REAL 或室外 GNSS 验收。聚焦多旋翼/SIM 回归全部通过。
- native QGC interactive fixture 使用 `--mode all` 重跑三种机型，证据为
  `artifacts/evaluation/simulator-interactive-acceptance-all-continue.json`（SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`），
  `modes=[vtol,fixed-wing,multirotor]` 且顶层 `gate_passed=true`；三种机型均通过共享
  AI/manual authority、watchdog、只读遥测转发和 blocked-write 子门。
- Docker/PX4/Gazebo 的多旋翼资源与端口规划已通过 no-I/O preflight，证据为
  `artifacts/evaluation/simulator-docker-acceptance-multirotor-preflight-continue.json`
  （SHA-256 `9AD08E26FB7FDB38D483A6E0FCD6B9FBF9677BF0AAF5CE10C540C4578169DD99`）：
  镜像按 digest 固定、模型/世界只读挂载、控制端口仅绑定 loopback、遥测不发布、清理命令完整，
  `preflight_gate_passed=true`；`execution_requested=false`，所以 container runtime、遥测、
  视觉、硬件和 HIL/REAL 均保持 `not_claimed`。
- 多旋翼共享 AI 闭环的 Docker 参数与策略预检也已封存于
  `artifacts/evaluation/simulator-docker-ai-acceptance-multirotor-preflight-continue.json`
  （SHA-256 `B05B6C247DA3E6C8DA6D6A6AC71424421AC2300E6A587E92CB78AAC8CE3C5D50`）：
  `preflight_gate_passed=true`、`runtime_gate_passed=true`、`ai_policy_gate_passed=true`、
  `ai_steps=6`（`ai_policy_steps=5`），共享 MANUAL_CONTROL/authority/watchdog 链路和 blocked-write 检查通过；
  该记录为 deterministic/no-I/O loopback fixture（`execution_requested=false`），不代表
  外部 PX4/Gazebo、真实 Pixhawk/Jetson、执行器、视觉或 HIL/REAL 已验收。
- 由于 `perception_bridge.py` 与三机型 Mission dispatcher 的共享契约更新，已用仓库生成器重算
  `configs/release-parity.json`；该阶段的历史文件 SHA-256 为
  `9D25D270A09C9F66515214FBE55D8C2D7DA5C25B1C4051DB8F61B13077FA098A`，canonical parity 回归已通过。
- 目标落图源帧新鲜度在 v30 阶段收口为跨层一致的 `<=1.5 s`：生产者在发现合格坐标已经过期时原子发布
  `available=false`，Python 消息模型/编码器/解码器与 QGC C++ 解码器对带新签名的 stale 坐标统一拒绝，
  防止旧坐标在 SIM、现场 metadata 或重传路径中重新变成实时目标点。新增协议与地理位置回归后，
  `tests/test_operator_protocol.py`、`tests/test_target_geolocation.py` 以及完整 goal acceptance 均通过。
  v30 阶段的 canonical `configs/release-parity.json` 已由生成器同步，`task_contract_hash` 更新为
  `965c7bb34e606af824a7f71318d4d8fa9c929655d616ff0b829e919e621ab7f1`，文件 SHA-256 为
  `676817B29C868FCEF90606BFD9E8FE0A2A6DD69E96177E6AE7B7AE0506A86964`；随后 v31 极点门重算为
  `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`。
- v30 阶段 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-target-geo-age-v30.json`
  （SHA-256 `772672FE9FEAAD9369DC8B31E1A6F5B30F1EB2C1A0279ED53A3C51F2A7FD95D1`）：核心测试收集 `2097`，
  QGC 自定义测试 `66/66`，脚本退出码 `0`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`。它绑定当前 pre.8 portable EXE，但不声称真实 Jetson、室外 GNSS、
  Pixhawk/执行器、HIL/REAL 或人工 Fly View 已通过。
- 新增 `src/multidetect/field_link_preflight.py` 与 `scripts/run_field_link_preflight.py`，为现场联调提供可重复的 Jetson SSH、GR01 TCP、相机 RTSP TCP 和本机 UDP metadata 端口预检；输出 JSON 证据并明确区分“端口可达”与 GNSS/传感器/执行器验收。新增 `tests/test_field_link_preflight.py` 覆盖 TCP 成功路径和 UDP 端口碰撞。
- 台式机现场预检证据写入 `artifacts/evaluation/field-link-preflight-20260831-require-all.json`：本机 `14580/14581` 均可绑定，但 `192.168.144.20:22`、`192.168.144.11:5760` 与 `192.168.144.108:554` 均超时；`--require-all` 正确返回退出码 `1`，因此 Jetson/GR01/相机现场链路继续保持 pending。
- 本轮继续执行非破坏性现场链路预检，证据为
  `artifacts/evaluation/field-link-preflight-20260831-continue.json`（SHA-256
  `692A89C9E2A6606894FA33D9038E36AB28834A93717279A331CA59045B06E31A`）：本机 metadata/QGC
  UDP 端口仍可绑定，但 Jetson/GR01/RTSP TCP 均超时，`hardware_link_ready=false`；未发送登录、
  MAVLink、视频或控制数据。
- 同日 follow-up 预检写入 `artifacts/evaluation/field-link-preflight-20260831-followup.json`（SHA-256 `4CE813D240826C6D9304A943D76FDCA675AF65E39B1B6F1688D0DC32ADE90156`），结果不变：本地 `14580/14581` 可绑定，Jetson/GR01/RTSP TCP 均超时，`hardware_link_ready=false`。
- 现场预检新增 metadata/QGC 固定端口配置级 fail-closed：`metadata_port == qgc_port` 不再因顺序 bind 而假通过；异常端口类型/范围也会转为失败探针，并由 `tests/test_field_link_preflight.py` 回归覆盖。
- 现场预检新增可选 `--protocol-checks`：Jetson 端被动读取 SSH identification banner，GR01 端被动识别完整 MAVLink v1/v2 帧，相机端只发送 `OPTIONS * RTSP/1.0` 并接受可解析的 2xx–5xx 状态（401/403 标记为“服务可达、需要认证”）。不会发送登录、心跳、视频启动或飞控控制数据；协议失败在 `--require-all` 下继续 fail-closed。
- `tests/test_field_link_preflight.py` 新增 SSH/MAVLink v1/MAVLink v2/RTSP 401 和 combined-report 回归，确认协议探测不会向 SSH/MAVLink 发送数据，并且协议门会参与 `hardware_link_ready` 聚合。
- 现场预检新增有限次数稳定性 watch：`--samples N` 重复整套端口/协议探测，`--interval-s` 控制间隔，`--stop-on-failure` 可在首次掉线时收口；聚合报告记录样本数、首个失败位置、稳定率、连续失败峰值和停止原因，任何未采满或失败样本均保持 `hardware_link_ready=false`。watch 与单次预检共用只读边界，不登录、不请求 MAVLink stream、不启动 RTSP 视频、不发送飞控控制数据；新增回归覆盖完整采样、抖动与首失败停止。
- 台式机迁移后的三次现场协议 watch 证据为 `artifacts/evaluation/field-link-watch-20260831-continuation.json`（SHA-256 `17743B74CC2A1A276BDD4C0BAB4B0026F8A6D4C1F834BBDB00E5418EE4F63167`）：本机 UDP 端口可绑定，但 Jetson/GR01/RTSP 连续超时，`max_consecutive_failures=3`、`stability_ratio=0.0`，因此硬件链路仍保持 `offline_pending`。
- 新增 `src/multidetect/field_session_acceptance.py` 与 `scripts/validate_field_session.py`，把真实现场 JSONL 的 `DET → TRK → LCK → TGT`、取消后重捕获、GPS fix/卫星数/hAcc/vAcc/age、位置健康/坐标边界和 `RANGE_STATUS` 双新鲜度收口为可重复离线 gate；验证器兼容扁平记录与 `AuditLog` 的 `event_type/details` 信封，支持 MAVLink `eph/epv` 厘米值和 `slant_range_m`；该 gate 仅验证输入证据契约，不提升为真实飞行验收。
- 现场会话 gate 进一步收口证据关联：记录时间戳必须单调不回退，合格 GPS 与 `RANGE_STATUS` 必须通过相同目标 ID 或同一记录中的明确配对建立绑定；串目标、根字段与嵌套字段冲突、以及只有跨记录匿名样本的 JSONL 均 fail-closed，并新增对应回归测试。
- `operator.remote_range_status` 审计现在保存 `source_frame_id`、源/产出时间及派生 `source_age_s`，使现场会话复核可重新执行 RANGE_STATUS source-age 门，而不是只依赖 wire 包内部字段。
- 新增 `src/multidetect/field_session_capture.py`、`scripts/capture_field_session.py` 和安装入口
  `multidetect-field-capture`：只读尾随 AuditLog/JSONL，支持 `session_id`、sequence/timestamp
  起点、`DET/TRK/LCK/TGT` 停止状态、最大记录数、时长和空闲超时；每条完整记录 flush/fsync 到
  独立证据流，并生成整份源 SHA-256、已消费源前缀 SHA-256、输出文件 SHA-256、筛选/损坏行、
  未完成尾部、停止原因和共享现场 gate 结果。新增 `verify_field_session_capture.py` 与
  `multidetect-field-capture-verify` 入口，可在交接后重算输出 gate 并验证源消费前缀；源日志
  收口后的追加内容只作为变化诊断，不会污染已采集证据。
  采集错误与 `field_session_acceptance` 失败都会使 `gate_passed=false`，不会把半截现场日志写成
  完整通过；新增捕获器单元回归覆盖 session 筛选、目标状态收口、损坏行、尾部和覆盖保护。

- 收口 QGC 目标证据坐标边界：`MultiDetectTargetEvidenceStore` 的新保存与 JSONL 重载均拒绝
  精确 `±90°` 纬度，`FlyViewCustomLayer.qml` 的合格落图与历史复制使用同一严格 `(-90°,90°)`
  规则；经度继续允许 `[-180°,180°]`。这样不会出现“记录可复制但地图不能显示”的极点分叉。
- 目标回放 v1 的 GPS 质量契约继续保持 `observed_at_s`/`gps_age_s`/`gps_freshness` 派生新鲜度、
  垂直精度 `<=10 m` 和 live `position_healthy` 语义说明；旧回放 bundle 兼容策略未改变。
- 本轮完成测距新鲜度协议收口：`VALID/DEGRADED` `RANGE_STATUS` 的 source frame age 与
  data freshness 必须同时不超过 `1.5 s`；Python 编解码器、QGC C++ 解码器和 `MultiDetectState.qml`
  同步拒绝/清除过期距离，证据快照不保存 stale range/bearing。地图证据继续拒绝精确极点纬度，
  保持 `(-90,90)` 与 `[-180,180]` 坐标域一致。QGC 静态 `82 passed`、协议/模拟器聚焦回归、三个
  C++ self-test 和全量 Python 质量门通过；未覆盖真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞、
  人工 Fly View 或最终 `v0.3.0`，历史成功/失败证据均未覆盖。

### Verification refresh (pole boundary, 2026-09-01)

- Rebuilt the pre.8 Windows Release and canonical portable tree after moving exact-pole WGS-84 validation into the QGC native `TargetGeolocationStatus` decoder. Release and portable `MultiDetectGCS.exe` are byte-identical (`46,575,104` bytes, SHA-256 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`).
- The portable `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` run exited `0`; log evidence is `artifacts/evaluation/qgc-simple-boot-20260901-target-geo-pole.log` (SHA-256 `E2BF6D1E4317AA721984583578D944B0D7285A8754F7D62C2BC67F91AB73CAB2`). Operator protocol, 44-fragment depth-grid, and RTSP wire C++ self-tests all exited `0`.
- Full goal acceptance is `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json` (SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`), with `2101` core tests, QGC custom `66/66`, software=`pass_with_environment_gap`, hardware=`offline_pending`. The canonical parity file and same-byte audit snapshot hash is `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`.

### Verification refresh (2026-08-31)

- Rebuilt the canonical Windows portable tree from the current pre.8 Release output after the offline depth-grid initialization fix; `Release/MultiDetectGCS.exe` and `portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe` are byte-identical (`46,575,104` bytes, SHA-256 `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`). The previous `759C8381C5ED34D791C8A6E9966FD3231196B1727AE1C2DD1DA946F77275E9C3` image remains historical provenance for the pre-fix rebuild.
- When no HMAC credential is configured, QGC now skips creating the depth-grid UDP receiver instead of emitting `invalid depth-grid receiver configuration`; fully configured deployments still pass the signed endpoint, port-range, and collision checks. The new portable `--offline --simple-boot-test` exited `0`; log evidence is `artifacts/evaluation/qgc-simple-boot-20260831-depth-grid-fix.log` (SHA-256 `AED8F43CD039490BA5B775C3A3FE4A7DBAC957FE228B5BEC6FE7A20B5E6AD80F`).
- The canonical portable closure contains `1,013` files / `529,288,077` bytes (`bin/` contains `233` files), with Qt, QML, translations, GStreamer, plugins, and the RTSP helper retained alongside the EXE.
- Re-ran the shared SIM control gates after the QGC rebuild: `simulator-3d-control-acceptance-20260831-followup.json` (SHA-256 `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`), `simulator-ai-acceptance-20260831-followup.json` (SHA-256 `121FEA672C027C299A3B40B9D7F249EC15DD3FB46CA17C2FC6D18BE3221AC84E`), and `simulator-closed-loop-acceptance-20260831-followup.json` (SHA-256 `11195BBC7B8E28082A446524040A32F8D026B7B9C87164F85389771C17ECA949`) all report `gate_passed=true`. The 3-D external PX4/Gazebo tools remain unavailable, so these are deterministic protocol/asset gates and do not claim visual, hardware, HIL, or REAL acceptance.
- Added randomized high-user-port allocation to `tests/test_simulator_orchestrator_interactive.py` so parallel Windows runs do not repeatedly collide with a fixed `30000`-series UDP tuple; the interactive orchestrator regression passes.
- Fresh goal acceptance: `artifacts/evaluation/multidetect-goal-acceptance-20260831-range-freshness-qa3.json` (SHA-256 `4CCE5185EA57EE41E8EE8486ECE535B4294FF6BCA20CC82961CE7B435DB1F295`), script exit `0`, `2022` core tests collected, QGC custom `66/66`, software=`pass_with_environment_gap`, hardware=`offline_pending`; Jetson/camera/GNSS and human Fly View gates remain open.
- 多旋翼路径接入后的最新 pre.8 goal acceptance：`artifacts/evaluation/multidetect-goal-acceptance-20260831-multirotor-v15.json`（SHA-256 `0290D69C8CE7822A5B4986D226396EDBB57ABC802D427C3CCAF6B4D6D4859B17`），脚本退出码 `0`，核心测试收集 `2064`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照绑定当前 portable EXE（SHA-256 `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`）和 `v0.3.0-pre.8`，多旋翼 deterministic 3-D asset/control gate 通过但外部 PX4/Gazebo、室外 GNSS、HIL/REAL 与人工 Fly View 仍未完成。
- 文档与构建指纹收口后的最新 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-multirotor-v16.json`
  （SHA-256 `D9A38283F3AB2302F53160A4A312FD48E82C15FE11AF13A61648B9F0BDD1DD32`），脚本退出码 `0`，
  核心测试收集 `2064`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`；快照绑定 `v0.3.0-pre.8` portable EXE（SHA-256
  `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`）。
  多旋翼 deterministic 3-D asset/control 与 Docker AI preflight 仍通过，但外部 PX4/Gazebo、室外 GNSS、
  HIL/REAL、真实执行器和人工 Fly View 门保持未完成；v15 原文件不覆盖并作为历史 provenance 保留。
- 在 v16 后再次按当前工作树重跑的最新 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-multirotor-v17.json`
  （SHA-256 `8F075110292816249F107463571447CD0E7E8C7A84FC326114C1065AD4CBDE1F`），脚本退出码 `0`，
  核心测试收集 `2064`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`；它绑定同一 `v0.3.0-pre.8` portable EXE（SHA-256
  `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`），并保留外部 PX4/Gazebo、
  室外 GNSS、真实执行器、HIL/REAL 和人工 Fly View 未完成语义。v15/v16 原文件均不覆盖。
- 现场会话验收改动后的新鲜软件门证据：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session.json`（SHA-256 `18D531C0397B349847D020C7173A81ADF4C0EAB81BF37F4FDC5434B90421CB62`），脚本退出码 `0`，核心测试收集 `2031`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照明确保留操作员凭据、Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 的未完成门。
- 现场会话验收修复后的最新软件门证据：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v3.json`（SHA-256 `15F0D9014D348A0F59C2B6F924C4C1F4F7FF42B47DFB54B206126F64F3C24D4E`），脚本退出码 `0`，核心测试收集 `2032`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照确认 `gps_sample` 原始质量字段兼容性回归通过，同时保留操作员凭据、Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 的未完成门。
- 现场预检端口冲突与 GPS 原始整数门收口后的最新软件门证据：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v4.json`（SHA-256 `BD4CFA7F0A0BBB42092B96AE571D4697D94AF594C426543EF99CE9C3F267FFAB`），脚本退出码 `0`，核心测试收集 `2036`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照确认现场验证器的 raw GPS 优先级、MAVLink 整数字段和 metadata/QGC 端口配置回归通过，真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 最新现场会话验收快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v5.json`（SHA-256 `1EB7199225D16522F7535996A05C02C12E06965EED03809B9CC0C5CAC871BB69`），脚本退出码 `0`，核心测试收集 `2037`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照还覆盖非法端口范围的 fail-closed 回归，真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 最新现场会话验收快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v6.json`（SHA-256 `974243F534089ADFC45FE767738CC5053A658B7305DD4CFB950494BE13BD765C`），脚本退出码 `0`，核心测试收集 `2037`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照包含 depth-grid 离线启动修复与 release-parity 重新生成，真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 目标绑定/时间单调性收口后的现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v7.json`（SHA-256 `32F58AA8C416968C1E63DA0848BD134229F8BE8356A6F116B35FCC2FDFBC5811`），脚本退出码 `0`，核心测试收集 `2041`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；同日 `field-session-fixture-20260831.validation-current.json` 验证 `gate_passed=true`，匿名跨记录、串目标和乱序时间回归均 fail-closed。该快照作为 v8 前一版 provenance 保留。
- 缺失/负数时间戳也收口后的现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v8.json`（SHA-256 `6A7B20056F07B125133F50A03097C1CAAC4D08D233810D8925DD30E10E083202`），脚本退出码 `0`，核心测试收集 `2042`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；`field-session-fixture-20260831.validation-current.json` 仍为 `gate_passed=true`，匿名跨记录、串目标、乱序、缺失和负数时间回归均 fail-closed。该快照作为 v9 前一版 provenance 保留。
- 嵌套传感器父节点目标 ID 也纳入绑定后的最新现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v9.json`（SHA-256 `ABF6E82C1638E1FDE4D9D8BDEB1B266C5954EDC4E04904C5DCD7D5FA94F8E8AB`），脚本退出码 `0`，核心测试收集 `2045`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；`field-session-fixture-20260831.validation-current.json` 为 `gate_passed=true`，匿名跨记录、串目标、嵌套父节点冲突、乱序、缺失和负数时间回归均 fail-closed。真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 嵌套别名路径解析修正后的最新现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v10.json`（SHA-256 `2FE0BA7FCECB8C1F2E4B87DE99F428F2A3952C18B109C886D976A75C9A7E7743`），脚本退出码 `0`，核心测试收集 `2045`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；现场 fixture 仍为 `gate_passed=true`，目标绑定和时间单调性回归全绿。真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。

### Verification refresh (field capture, 2026-08-31)

- 新增现场会话只读采集器后的最新 pre.8 软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-v11.json`（SHA-256 `51D20F3EDDCBED4F85DD5D2F64419A5479E5544DD309B28B1B4A23C8A234C485`），脚本退出码 `0`，核心测试收集 `2051`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`。`capture_field_session.py` 的 session 筛选、目标状态收口、实时 fsync、坏行/半行报告和共享现场 gate 均有回归；真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 在 CLI 默认报告路径与 sequence 缺失闭锁回归补齐后，最新 pre.8 软件门快照更新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-v12.json`（SHA-256
  `C96DA6C476A2B4B053E576F423113B5EA02276E665DC430E9415845788B800C8`），脚本退出码 `0`，核心测试收集
  `2053`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；
  采集器新增测试仍覆盖 session 筛选、目标状态收口、实时 fsync、坏行/半行报告、覆盖保护和序号筛选闭锁。
- 在采集前缀/输出 hash 绑定与交接复核器回归补齐后，最新 pre.8 软件门快照更新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v13.json`（SHA-256
  `FC15F9C59CE8FA3900FA8D53292D8BB7C8FAC140B8D3773127167FB9A5C9E95D`），脚本退出码 `0`，核心测试收集
  `2055`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；
  `verify_field_session_capture.py` 会重算复制 JSONL 的现场 gate，校验 `source_prefix_sha256` 与
  `captured_output_sha256`，并把源日志收口后的追加记录仅标为诊断信息。
- 根包公开导出与最终本轮回归后的最新 pre.8 软件门快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v14.json`（SHA-256
  `68C9F93C90D3790F3AA8D2F9F11EC8715795F6A0C6CD39A46E091207C264F7EA`），脚本退出码 `0`，核心测试收集
  `2055`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；
  当前 portable EXE 指纹和所有真实硬件/室外 GNSS/HIL/REAL/人工 Fly View 未完成门保持不变。

### Verification (2026-08-31)

- 全量 Python 回归在台式机项目 venv 中完成 QA20 验收，核心 pytest 收集 `2001` 项；QGC 自定义回归
  `64/64`，退出码 `0`。收集数量来自本次运行输出统计，不是 QA20 JSON 的额外字段；Windows 符号链接能力
  仍作为环境差异单独记录，未改变 `software_status=pass_with_environment_gap`。
  `ruff check src tests scripts`、`compileall -q src scripts` 与
  `git -c core.whitespace=cr-at-eol diff --check` 均退出码 `0`。
- GPS/目标事件回放与 PX4 ACK 身份门收口后的首个 goal acceptance 快照（历史记录）为：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-gps-replay.json`
  （SHA-256 `5CB38AA3654130F5025B2227802F9FAE974CD8EC73A1A261460B4E5B14479171`）。随后
  加入 E7 清单生成器、QGC map 生命周期保护和本地证据快照严格校验后重新执行的回归快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-regression.json`
  （历史 SHA-256 `3C7FE0BF8E84BAF8142CFCCC660A21F3E6D4628B5F592CBB48EA631F474769DC`）。
  传感器拆卸回归修复后的 QA 快照（历史记录）为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa.json`
  （SHA-256 `440AB514681408DB5B583C0AD9A62054441004BD107756C0229423546AF36334`）。随后加入
  release-gate runtime/event 归一化与直接脚本入口回归后的 QA3 历史快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa3.json`
  （历史 SHA-256 `ED550A122814A32D8823CC482A909B1BEF4AC0D234E03D242185E33BC37EA1F3`）。加入
  canonical parity 同步回归后的 QA4 历史快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa4.json`
  （SHA-256 `C9A04BA94DDD159BBBC979735BCD53910335A612B256EA0E1195D512D404D552`），并同步为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本）。该历史记录的核心测试为
  `1922 passed / 1 skipped`（共收集 `1923` 项）。
  网关 graceful-drain、GPS 原始质量二次校验和物理网卡选择修复后的 QA5/QA6 快照作为历史记录
  保留。Windows PowerShell 5.1 无参数入口与 UTF-8 无 BOM 证据写入修复后的 QA7 历史快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa7.json`
  （SHA-256 `D82F07B6E2935D40D0A16CF87644EB7AD0AD91FE9A67A3620B29CC78583AAAFF`）。该 QA7
  快照已明确标为历史记录；完整脚本退出码为 `0`，核心
  测试 `1937 passed / 1 skipped`（共收集 `1938` 项）、QGC 自定义测试 `63/63`；当时 pre.8
  Release/portable 主 EXE 为 `46,561,280` bytes，SHA-256
  `6079B6E567304E3484C664426B549B0067E86D7A40DACF2F09BBDFD87486D829`（QA7 历史制品）。
  QGC source-age/frame-hold、SIM world-origin 和 QGC 自定义测试更新后的 QA8 快照现作为历史记录保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa8.json`（SHA-256
  `A9C1AC9CE94B19BDE4AB551C27EEC4780554D601C913BE157CD86AE431C5ED98`），并同步为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本）；QA8 脚本退出码 `0`，核心测试
  收集 `1943` 项、QGC 自定义回归 `64/64`，当时 Release/portable 主 EXE 为 `46,571,008`
  bytes，SHA-256 `C3E0FE07E7600CF0968B54A70EEDA7804007B65FE2EF4E6A4B4598E076013727`。
  回放事件现在绑定 `gps.observed_at_s`、`gps_age_s` 与 `gps_freshness`，未来/过期 GPS
  fail-closed；QGC 1σ 圆使用 WGS-84 局部曲率并归一化跨日期变更线经度；map 未就绪时
  不再解引用，保存证据时只有当前目标的有限、合法 VALID/DEGRADED 测距才写入距离/方位，
  本地 JSONL 损坏记录会被拒绝而不是还原成假目标。该快照仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不提升
  真实 Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL、实飞或人工 Fly View 状态。
- QA10 已降为历史记录（原文件与 SHA 保留）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa10.json`，SHA-256
  `1F3515C50D2EA95D481450189ED72AC2FCBD814B965F342C5777BED6D30B6F65`。
  QA11 已降为历史记录；其原始文件与 SHA 保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa11.json`（SHA-256
  `FFB41D20E2C220C8391D82DD062AF44C39DED020DBD2CD12C3B702A169403993`），脚本退出码 `0`，曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`。QA11 核心 pytest 收集
  `1964` 项，`1963 passed / 1 skipped`，QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。
  当前 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA11 仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，所以不发布最终
  `v0.3.0`。QA11 在验收启动时额外记录源码快照指纹：`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、
  `dirty_entry_count=290`、`worktree_clean=false`、`status_sha256=5e156cf0952025104665e287d9a87099acb2e989b8c0b9d67719c19befc86a1e`；
  这是带 provenance 的可复验 dirty snapshot，不是干净提交对应的发布签名制品。
- 同日 QA12 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa12.json`（SHA-256
  `D10F40F27E401250E677AC80E89ECE7459F5170172499E9EB81E2D65DF6960DF`）。
- QA13 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa13.json`（SHA-256
  `EF1684856B274607A2C39F951D620803149495C9F323863CC096788528E16069`）。QA13 只确认 canonical parity
  漂移已修正，不改变 `offline_pending` 或最终 `v0.3.0` 发布门状态。
- QA14 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa14.json`（SHA-256
  `6856130808EDEB05C543DE24A42F2C17545725C5D6615338ECF52613B5CA3277`）。
- QA15 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa15.json`（SHA-256
  `B978D244A7440B3C102E1BDD8408110F511BE2C0209C4BC7DDB340EE56487808`），曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA15 核心 pytest 收集
  `1975` 项，`1974 passed / 1 skipped`；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时
  Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA15 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不发布最终 `v0.3.0`。
  provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
- QA16 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa16.json`（SHA-256
  `403B319255A9C2446012BD060C85BB2633DB9A4D7DE45CF4B9BB495D96B234EE`），曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA16 核心 pytest 收集
  `1976` 项，`1975 passed / 1 skipped`；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时
  Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA16 仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA16 复验 GPS_RAW_INT 缺失 `alt`、降级 fix、非法坐标、未知卫星哨兵和未来时间戳的原子
  fail-closed 语义，并在失效时恢复独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径回退
  和 canonical parity 继续有效。该快照只覆盖软件/协议门，不升级真实硬件、室外 GNSS、HIL/REAL、
  实飞或人工 Fly View 状态。
- QA17 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa17.json`（SHA-256
  `BF93CE4FFD7DEE0CA9D166614DAF8D6216BF0ACC7D09808DDF913B3E11D6FFA2`），曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA17 核心 pytest 收集
  `2000` 项，运行输出为 `1999 passed / 1 skipped`；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时
  Release/portable 主 EXE 仍为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA17 仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA17 已验证 GPI/GPS 原子解析与非法 sentinel 回归：无效坐标、未知卫星、缺失高度、降级 fix 或未来
  时间戳不会复用旧 raw 新鲜度/坐标，失效时恢复独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径
  回退和 canonical parity 继续有效。该快照只覆盖软件/协议门，不升级真实硬件、室外 GNSS、HIL/REAL、
  实飞或人工 Fly View 状态。
- QA18 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa18.json`（SHA-256
  `230A2EC5EC44982DE4563AEAEB949E32ACD85123D706C320E056EA7BF3951F7F`），脚本退出码 `0`，并已提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA18 核心 pytest 收集
  `2000` 项，运行输出为 `1999 passed / 1 skipped`（唯一跳过项是该 Windows 账户不支持符号链接）；这是当次
  全量运行输出统计，不是 artifact 的额外字段。QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当前
  当时 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA18 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA18 补充并验证 GPI `GLOBAL_POSITION_INT` 与 GPS 原子解析/非法 sentinel 语义：坐标、卫星数、
  高度、fix 和时间戳在同一帧内原子校验；任一非法或缺失字段都会清除对应 raw freshness/坐标，拒绝未来
  时间戳，并恢复独立 EKF/LOCAL 高度，live bridge 与此语义一致。SIM `arm_on_start`、Docker 标准路径
  回退和 canonical parity 继续有效。该历史快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实
  Pixhawk、Jetson、相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。
- QA19 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa19.json`（SHA-256
  `082092AB58848D498050901C5BD9768AABF2357569C91D72D531DD105E527515`），脚本退出码 `0`，并已提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA19 核心 pytest 收集
  `2001` 项；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
  diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时 Release/portable 主 EXE 为
  `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA19 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA19 补充并回归 Pixhawk `GPS_RAW_INT` per-source timestamp cursor 单调性：同一来源的旧/乱序观测不会回退
  freshness、坐标或高度游标；非法 sentinel 与未来时间戳仍原子 fail-closed，独立 EKF/LOCAL 高度恢复和 live
  bridge 语义保持一致。该历史快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实 Pixhawk、Jetson、
  相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。
 - QA20 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa20.json`（SHA-256
  `624B31E03C04D60CD0FF26386C23DC3E6FED19E011375C31DE8578E897ACE756`），脚本退出码 `0`，并已提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA20 核心 pytest 收集
  `2001` 项；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
  diff check、三个 C++ self-test 和 portable headless smoke 均通过。当前 Release/portable 主 EXE 仍为
  `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA20 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=294`、
  `worktree_clean=false`、`status_sha256=3b2fb046735714f18012b659cbd652dc0f84761084980ee3e312ee37f0fc9211`。
  QA20 延续并回归 Pixhawk `GPS_RAW_INT` per-source timestamp cursor 单调性：同一来源的旧/乱序观测不会回退
  freshness、坐标或高度游标；GPI/GPS 坐标、卫星数、高度、fix 与时间戳在同一帧内原子校验，非法
  sentinel 与未来时间戳清除 raw 状态并恢复独立 EKF/LOCAL 高度，live bridge 语义保持一致；同时修复
  `depth_grid_udp` 发送完成后才起算限流窗口的竞态，并以全量回归验证。
  该当前快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实 Pixhawk、Jetson、
  相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。
 - QA22 是当前 goal acceptance：`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa22.json`
   （SHA-256 `F38EE2065DCB57A1CA2C12A99AD1F401DC4D48DA0F87848D5B6BBDD2913B7ACF`，核心 pytest 收集 `2012` 项，
   QGC 自定义回归 `64/64`，脚本退出码 `0`，latest 与该文件字节一致）。software=`pass_with_environment_gap`、
   hardware=`offline_pending`、dirty=`295`、status SHA=`851a1abde3f79abd859ad032242cee0ee9bfd790663f615c3fe73b6b67d97c74`。
   回放 GPS 质量要求 vAcc≤10 m 且纬经度与 MAVLink fix/satellite 边界对齐；legacy observed_at 缺省、自定义 live policy
   与 position_healthy 语义差异分开记录，均不代表真实硬件通过。
- QA15 之前以当前源码完成的真实 Docker/PX4/Gazebo disposable AI armed-heartbeat 复验（独立 machine evidence）：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830T153110Z-qa13.json`（SHA-256
  `678C233C7164E123B0272CF76CC0831F1D601797F7E66877BC6CE463F4FE619C`），容器日志 SHA-256
  `04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。`gate_passed=true`、
  `telemetry.qualified=true`、`armed_observed=true`、`ai_steps=1`、`ai_policy_gate_passed=true`，
  stop/rm 清理成功；该证据仍仅覆盖 disposable SITL machine gate，不提升真实硬件、室外 GNSS、
  HIL/REAL、执行器、实飞或人工 Fly View 状态。
- QA15 同步收紧 GPS_RAW_INT 的原始质量语义：越界经纬度不再复用上一帧坐标，`satellites_visible=255`
  归一为未知，缺失 `alt` 或合法但降级的 `fix_type` 会清除旧 raw 新鲜度与 GPS-home 高度回退，未来观测
  时间统一 fail-closed；SIM manual-only 场景现在正确应用 `arm_on_start`，三份 PX4/SITL PowerShell
  验收脚本也统一支持 Docker PATH 与标准安装目录回退。上述回归只证明软件/协议行为，不提升真实
  Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞或人工 Fly View 状态。
- 此前在同一台式机上执行的带 `--exercise-arm`、GPS fix threshold=3 和 PX4 component=1
  的 Docker/PX4/Gazebo disposable AI 闭环（历史 ack-gps run）：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-ack-gps.json`（SHA-256
  `25947F39FF2FA66C02FBD0F2C4CB02C0CA2586701C6FA200B9AD148730A3D279`），容器日志 SHA-256
  `E848D1347A8064C92B39532F1F05BC9938CB8B50FC96BA31E5C0159E0B046220`。该 run 的
  `gate_passed=true`、`runtime_gate_passed=true`、`ai_policy_gate_passed=true`、
  `telemetry.qualified=true`、`armed_observed=true`、`ai_steps=1`，5 个 `COMMAND_ACK` 中
  4 个为目标 PX4 autopilot component=1 的合法 arm/disarm ACK，1 个无关 command 被结构化
  拒绝，且 stop/rm 清理均成功。它仍是 Docker/SITL machine evidence，真实 Pixhawk/Jetson/
  相机/室外 GNSS、执行器、HIL/REAL、实飞和人工 Fly View 继续保持 `not_claimed`。
- 新增 `scripts/create_release_gate_manifest.py`（四模式 E7 清单生成器）：四个 evidence
  路径、五个 parity hash 和人工/硬件状态都必须由调用方显式提供；脚本自动计算 SHA-256、
  写出 schema v1 清单并保持 fail-closed，不会把 goal snapshot、fixture 或 Docker 记录推断成
  缺失的 REPLAY/HIL/REAL。QA7 和 QA8 诊断报告均作为历史记录保留；QA8 诊断报告
  `artifacts/evaluation/release-gate-pre.8-report-final-qa8.json`
  （SHA-256 `2C609808BB8A6B154F92F1F93F4FC91508F5AA88D96DCB496044F49070229566`）明确为
  `gate_passed=false`（缺少四模式清单、REPLAY/HIL/REAL 和最终版本通道）；QA5/QA6/QA7 报告
  以及 QA3/QA4 更早报告均保留为历史 prerelease 门状态证据。
- 新增 `scripts/create_release_parity.py` 与 `configs/release-parity.json`：五组共享契约使用
  固定 framing 和显式源码列表计算，纳入实际 QGC qrc/QML/C++ header surface，并拒绝生成目录、
  越界路径和符号链接逃逸。当前 parity 输入 SHA-256 为
  `F585455FC0E0E36B163B1150BE1889611928FA118F7698FE22EEEE16341B28CC`（GPS 原始质量门、网关
  graceful-drain、QGC source-age/frame-hold、SIM world-origin、GPS sentinel/constant 与离线 depth-grid 初始化
  修正后重新生成；同字节审计快照为 `artifacts/evaluation/release-parity-pre.8-20260831-depth-grid-fix.json`，此前的
  `release-parity-pre.8-20260830-gps-sentinel-constants.json`、
  `gps-quality-gateway-qgc-age-world-origin.json`、`gps-quality-gateway-drain.json`、`gps-quality.json`、
  `gps-replay.json` 和 `release-parity-pre.8-20260830.json` 保留为历史快照）；
  契约修改后必须重算。
- QGC 原生 Fly View 取消状态机补上静态回归：一次 LCK/TGT 操作发送 `DEMOTE_TRK` 并进入
  TRK，下一次才发送当前目标的 `CANCEL_TRK`，待 ACK 期间不会留下旧方框或误取消其它 TRK。
- 统一 `ManualClock`/`RuntimeDescriptor` 接线后执行的完整 goal acceptance 历史快照为：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-clock-descriptor-retry.json`
  （SHA-256 `A67D85121E202BE028C819E275848DBC8844BC117C2CC06945706344062372B9`），
  核心回归收集 `1860` 项、QGC 自定义测试 `63/63`，软件状态仍为
  `pass_with_environment_gap`、硬件状态仍为 `offline_pending`；该文件保留为历史证据，
  不再代表当前 `latest`。
- 证据卡片空值格式化回归接入后执行的 goal acceptance 历史快照为：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-rebuilt-evidence-card.json`
  （SHA-256 `1EA3C005B02B8E7022C33BB4446D2E7C21585495BB66C8AACF7F5244E2AFA881`），
  核心回归为 `1860` 项、QGC 自定义测试 `63/63`；该文件同样保留为历史证据。
- 交互验收共享时钟/descriptor 回归接入后重新执行的完整 goal acceptance 历史快照：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-interactive-clock.json`
  （SHA-256 `5AC0A405BF6B32405903FFFB8E58D37DAF45F817979A5E5275762854FBDB363B`），
  核心回归为 `1861` 项、QGC 自定义测试 `63/63`，软件状态仍为
  `pass_with_environment_gap`、硬件状态仍为 `offline_pending`；当时同步为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现已由 GPS/回放快照取代。
- `tests/test_qgc_native_flyview_style.py` 新增目标落图回归：要求合格 GPS/目标身份/1σ
  gate、WGS-84 map projection、无效定位清除 marker、1σ 圆、证据卡片复制/保存/记录按钮，
  并确认该能力仍在原生 Fly View 单窗口内。
- `tests/test_target_event_replay.py` 新增 7 项回归，覆盖 bundle round-trip、按目标/时间筛选、
  manifest/event/media 篡改拒绝、GPS/range quality gate、非空目录保护、检查脚本输出和 schema
  结构；该批测试随全量回归一起通过。
- `simulator-interactive-acceptance --mode both` 的 VTOL/fixed-wing deterministic machine gate
  均为 `gate_passed=true`，每种模式均覆盖 AI 接受 2 次、QGC 手动帧 1 次、只读遥测转发 1 次、
  watchdog neutral 1 次；飞控/任务写入保持阻断，证据为
  `artifacts/evaluation/simulator-interactive-acceptance-pre.8.json`。主机缺少 `px4`/`gz`，
  因此 external 3-D runtime、桌面视觉和硬件 claims 仍为 `not_claimed`。
- 同一 interactive acceptance 在 2026-08-30 重新运行并写入
  `artifacts/evaluation/simulator-interactive-acceptance-20260830-rerun.json`（SHA-256
  `33FF690CE41B1E77E3B8D2EE035E36255AD9370F7BD35B248647A75DFFE52B03`），`gate_passed=true`，
  两个 mode 均通过 manual takeover、AI resume、watchdog neutral、只读遥测转发、身份/loopback
  和 blocked-write 检查；该 fixture 仍不提升外部 3-D、桌面视觉或硬件 claims。
- 交互验收 fixture 随后收口到共享 `ManualClock`/`RuntimeDescriptor`：QGC 路由 duration、AI、
  watchdog、飞行模型与 control receipt 全部使用同一 SIM 时间轴，并新增 descriptor/hash 绑定。
  新证据为 `artifacts/evaluation/simulator-interactive-acceptance-20260830-clock-descriptor.json`
  （SHA-256 `6C237C5B1759706B7E563398D0E839A0E90FA7DF6FFEB96C0F3CF9E33AE0DDF5`），
  `gate_passed=true`，VTOL/fixed-wing 均为 deterministic、router duration `0.3s`；该 fixture
  仍不提升外部 3-D、桌面视觉或硬件 claims。
- `artifacts/evaluation/simulator-docker-ai-acceptance-20260829-runtime.json` 保留为失败证据：
  Docker runtime 子门通过，但 `ai_policy_gate_passed=false`、`ai_steps=0`，车辆未报告
  `armed` heartbeat；该文件不能被写成 AI 控制通过，也不改变最终 `v0.3.0` 未发布状态。
- Docker Desktop AI acceptance 的 UDP hairpin 修复回归已封存于
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-fixed.json`（SHA-256
  `8C43D86830783AFBE1C5CF45C50B7EAA108555259DDD3BBD809FBC2A300AB8CC`），容器日志 SHA-256
  `614B3B400F54404D061224549B4D195C7DF31ECF1A581783390D0C86E48FF7C9`。control `24540`
  保持 loopback host publication，telemetry `14541` 改为 `container_to_host`/
  `outbound_only` 且不发布；因此 `runtime_gate_passed=true`、`telemetry.qualified=true`
  （4057 observed / 3956 accepted / 101 rejected，wildcard fallback=false），容器清理
  `removed=true`。车辆仍 `armed=false`，`ai_policy_gate_passed=false`、`ai_steps=0`，总
  `gate_passed=false`；该结果只证明 telemetry/runtime 修复，不是 AI 控制成功。
- 显式 disposable SITL arm 闭环已通过：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-arm-v6.json`（SHA-256
  `35A19F6FF3656C82F170AB2061A044CFFA0DB9C24EB125CE21914132E7684D62`），容器日志 SHA-256
  `5F34112A19F05FF13DEAAD270EE53444494F6AD444D0EA4A83F9EEF1BCCC9BA5`。显式
  `--exercise-arm` 在 disposable PX4/Gazebo 容器内完成四项 arm 预检参数、4 次有界
  `MAV_CMD_COMPONENT_ARM_DISARM` 尝试（前两次 temporary reject，后两次 accepted）、
  `armed_observed=true`、`ai_steps=1`/`ai_accepted=1`，并在 finally 发送 disarm；
  `runtime_gate_passed=true`、`ai_policy_gate_passed=true`、总 `gate_passed=true`。
  该证据仍只覆盖 Docker/PX4/Gazebo SITL machine gate，`real_control_acceptance`、
  物理执行器、真实 Pixhawk/Jetson/GNSS/相机、HIL/REAL、实飞和人工 QGC 视觉保持
  `not_claimed`。
- 台式机迁移后的 Docker 路径复验已通过：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-desktop-rerun.json`
  （SHA-256 `A0D561EBD3907B10578990E4713562749BE186CF8D7BF9AF5EF2FC902E4A62B5`），容器日志
  SHA-256 `8A1F4F7927B3D4AA5D9F1932102892E70F72EB5E2D06951B840451166F28D893`。通过仓库自动
  回退的 Docker Desktop `28.4.0` daemon，`runtime_gate_passed=true`、
  `telemetry.qualified=true`、`armed_observed=true`、`ai_policy_gate_passed=true`、
  `ai_steps=1`/`ai_accepted=1`，并完成 disarm 与容器 stop/rm 清理；该 machine gate
  仍不提升真实 Pixhawk/Jetson/GNSS、执行器、HIL/REAL、实飞或人工 QGC 视觉 claims。
- 2026-08-30 的完整 disposable PX4/Gazebo/QGC/Mission combined SIM gate（历史基线）在传感器 transport
  teardown 修复后重新跑通：
  `artifacts/evaluation/combined-sim-qgc-20260830T101014Z-56628/combined-evidence.json`
  （309,899 bytes，SHA-256
  `8423E667A9C9668EF8FE4B1AE5D4166E41F3E21F423ED2FD766EBBECB925C37D`），
  `gate_passed=true`。该 run 完成 600 帧同核 integrated SIM/RTSP、传感器 helper
  `helper_exit_code=0`、原生 QGC post-adoption runtime-status ACK、PX4/Gazebo 容器与传感器
  就绪、VTOL MC→FW→MC、7 项 INT-only Mission upload/readback/active、清理，以及原生 toolbar
  指示器精确点击后抽屉开/关（机器 smoke）。工具栏证据为
  `qgc-native-toolbar-indicator-evidence.json`（SHA-256
  `D7886948F4913A555B1C5F756258D5CB6ED82D35AF5019883C659972A0F0CB63`）。该门是 software-only
  SIM/PX4/Gazebo/Mission 证据；`operator_visual_acceptance=not_claimed`、桌面 GPU 视频像素/
  完整 QML rendering、真实 Pixhawk/Jetson/相机/室外 GNSS、执行器、HIL/REAL、实飞和生产检测
  准确率仍保持 `not_claimed`，最终 `v0.3.0` 不发布。此前 `20260830T062624Z-44576` 的成功
  证据继续作为历史记录保留。
 - 2026-09-01 当前 combined SIM gate 在 Docker Desktop 恢复后重新执行 900 帧并通过：
  `artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`
  （771,782 bytes，SHA-256
  `5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`），
  `gate_passed=true`。该 run 的相机/CameraInfo/NavSat readiness、VTOL MC→FW→MC、7 项
  INT-only Mission、Mission-ready 后 5 个 type-24 ACK、QGC 原生 QQuickItem VideoOutput/
  PiP/fixed-wing 抓帧和清理均通过；仍保持 `operator_visual_acceptance=not_claimed`，不提升
  人工 Fly View、室外 GNSS、真实 Pixhawk/执行器或 HIL/REAL。
- 当前 portable QGC 入口
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`
  以 `ground-control/MultiDetectGCS/tools/run_tests.py --binary <portable-exe> --verify-only
  --headless --timeout 45 --verbose` 完成启动冒烟，退出码 `0`；EXE SHA-256 为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`（46,575,104 bytes）。裸
  `Release/MultiDetectGCS.exe` 仍只是链接结果，交付时必须保留 portable 运行时目录。
- 台式机迁移后的完整 goal acceptance 已重新执行并生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-key-format.json`（SHA-256
  `3469C05C8C560BB1EFA52393C43BDA6C04F35759F9F1CB30CABAABA1BD623E84`）。结果为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`、
  `core_tests_collected=1857`、QGC 自定义测试 `63/63`、QGC 协议自测/QML 解析/diff
  检查均通过；QGC EXE 指纹与本节 portable 入口一致。由于当前台式机 Ethernet 2
  已断开且 `192.168.144.20` Jetson、`192.168.144.108` 相机均不可达，证据保留
  operator key（含严格 64 位十六进制格式校验）、ChArUco 标定、舵面安全验证、真实
  DET→TRK→LCK→TGT、Jetson soak 等 live gates，未把软件门提升为 HIL/REAL 或最终
  `v0.3.0`。
- 3-D deterministic control/asset gate 已按两个场景重新运行：VTOL
  `artifacts/evaluation/simulator-3d-control-acceptance-20260830-rerun.json`（SHA-256
  `AE338FFEB0833D6BC7B771E1D5562B923CEA21E1FD9C8FC88B2FB2A82A5A1B40`）与 fixed-wing
  `artifacts/evaluation/simulator-3d-control-acceptance-fixedwing-20260830-rerun.json`
  （SHA-256 `2BD93FE5B3591EE14791645E267200D92A9F79516F64A6DD91D42C4ED5051D40`）均为
  `gate_passed=true`、`deterministic_gate_passed=true`，manual/AI/watchdog/shared-authority
  和 truth-free 检查通过；两份证据均记录 `external_runtime_gate_passed=false`、`px4=false`、
  `gazebo=false`，所以 3-D 桌面渲染、人工 Fly View 和硬件字段继续保持 `not_claimed`。

### Tooling

- `SimulatorClosedLoop` 与 manifest `SimulatorScenarioRunner` 现在绑定统一的确定性
  `ManualClock` 和 `RuntimeDescriptor(mode=SIM)`：AI、watchdog、模型 telemetry、control
  receipt 与 evidence 共用一条 monotonic 时间轴，physics tick 成功后才推进时钟并对漂移
  fail-closed；scenario 结果新增 `runtime_descriptor` 与 `runtime_descriptor_hash`，为
  后续 HIL/REAL 同契约对齐提供可核验身份。
- `scripts/gazebo_sensor_stream.py` 现在在退出前排空 Image 回调、显式取消四个 Gazebo
  transport 订阅并提前销毁 `gz.transport13.Node`；修复了已输出完整帧后由 C++ callback
  executor teardown 触发 `std::terminate`（exit 134）的假失败。新增回归覆盖订阅清理与
  缺失 binding 的 fail-closed 语义；当前 600-frame combined SIM 证据中的
  `sensor_transport.helper_exit_code=0`。
- 新增 `multidetect-release-gate` / `scripts/check_release_gate.py` 与
  `configs/schemas/release-gate-manifest.schema.json`。它聚合 `REPLAY`、`SIM`、`HIL`、`REAL`
  四模式证据，重新计算证据 SHA-256，校验 `gate_passed`/runtime mode、同 UI/同核心/同任务
  契约/同安全策略/同 PX4 Mission path parity，并在 `not_claimed`、缺文件或 prerelease
  通道时保持 `release_allowed=false`。新增 `tests/test_release_gate.py` 四项回归。
- `scripts/build_multidetect_windows.ps1` 增加 `-ReleaseGateManifest` 与
  `-RequireReleaseGate`；最终 `channel=release` 会在 CMake 配置前强制 E7 证据，
  prerelease 可显式演练但不会绕过最终版本门。
- `multidetect.simulator probe --json` 增加 Docker daemon health 字段：`daemon_available`、
  `daemon_version`、`daemon_reason`。它区分 Docker 客户端在 PATH/安装目录可见与
  Docker Server 真正可连接两种状态；探测保持只读，不创建容器、不打开 UDP 端口。
  新增 3 项 `tests/test_simulator_isolation_probe.py` 回归覆盖 daemon 成功、daemon
  不可达和 CLI JSON 字段。

- 新增 `scripts/capture_qgc_native_style.ps1`：启动现有 portable
  `MultiDetectGCS.exe`，只在 exact QGC client rectangle 内捕获原生 Fly View，临时移开相交
  外部窗口并在 `finally` 中恢复窗口状态。该工具只生成 machine-smoke 证据，明确保留
  `operator_visual_acceptance=not_claimed`，不会把截图冒充人工验收。
- `scripts/run_goal_acceptance.ps1` 现在显式串接固定翼 software/HIL-preflight
  acceptance 与 `tests/test_qgc_native_flyview_style.py`，主 evidence 同时记录
  `native_qgc_flyview`、单窗口/单视频 surface、无私有主题和 Computer Use 证据边界。
- 目标验收的 QML 语法门改用 Qt 构建链同源的 `qmlcachegen --bare`；直接调用
  `qmlformat` 会因 QGC 自定义导入模块未在裸命令行的 import graph 中解析而误报失败。
- 真实 COCO YOLO26n raw ONNX 已按当前导出环境重新固定：Ultralytics `8.4.92`、ONNX
  `1.22.0`、opset 17，artifact SHA-256 为
  `7ff7b97cad2fbb68d4fc6a74ed5dad67beb91f535e8cc25d5f0d97ae42d2e410`；未改变
  `production_approved=false` 和飞控/物理输出闭锁。

### Added

- 新增 `src/multidetect/target_event_replay.py` 的 hash-bound 目标事件回放契约、
  `scripts/create_target_event_replay.py` 采集清单导入入口和 `scripts/inspect_target_event_replay.py`
  检查入口。它把视频帧/深度图、目标身份、捕获时间、
  GPS 质量与 WGS84 坐标、水平 `1σ`、实际测距、相对方位绑定到本地
  `manifest.json` + `events.jsonl` + `media/` bundle；加载时执行 canonical manifest、事件流和
  每个媒体文件的 SHA-256/字节数、路径 containment、连续序号、单调时间与 GPS/range quality
  gate。`configs/schemas/target-event-replay-manifest.schema.json` 同步描述结构；claims 固定
  为只读证据、不含飞控输出、不含 simulator truth，和 QGC 轻量 `target-snapshots.jsonl` 分开保存。

- `scripts/run_simulator_docker_ai_acceptance.py` 增加显式 `--exercise-arm`：默认仍为被动
  telemetry/AI bridge；启用后等待合格 heartbeat/GPS，应用 disposable SITL arm 预检，沿
  同一 loopback control socket 发送 `MAV_CMD_COMPONENT_ARM_DISARM`，等待真实 armed
  heartbeat 后执行共享 AI policy，并在退出前发送 disarm。所有命令、ACK、重试、参数和
  清理结果都写入 evidence；不会打开真实硬件路径。

- 新增 `scripts/run_simulator_docker_acceptance.py`：在
  `run_simulator_docker.py` 的 hash-bound 计划之上提供显式 `--execute` 外部运行门。它只启动
  pinned PX4/Gazebo disposable container，核对只读 world/model mount、bridge/loopback 端口、
  image digest、环境和 purpose label，被动收 `HEARTBEAT`/GPS 并在 finally 中 stop/rm；默认
  不执行 Docker。证据把 preflight、container runtime、telemetry、QGC visual 和 hardware
  分层，飞行控制发送计数固定为零。

- 新增 `src/multidetect/simulator/px4_launch.py` 与 `px4-launch-plan` CLI。它在 disposable job
  内复制/修补 PX4 `px4-rc.mavlink`，显式绑定 loopback `14540` control 与 `14541` telemetry，
  生成 POSIX `px4-sim-launch.sh` 和 `px4-launch.json`；scenario/stack-plan、源/生成脚本和 wrapper
  均带 SHA-256，PX4 source checkout 保持不变。dry-run 可生成诊断 template，strict readiness
  失败会阻断真实编排启动。
- `external-plan` 新增 `--px4-launch`/`--no-px4-launch`/`--px4-launch-strict`：可把 PX4/Gazebo
  argv/cwd/env/log/依赖顺序与 job-local wrapper 合并成 no-launch evidence，原始 PX4 command 仍
  保存在嵌套 `source_stack_plan`；该 planner 不调用 subprocess、不打开 socket。

- 新增仓库内 fixed-wing 3-D 场景 `coastal_patrol_fixed_wing_001`：
  `rc_cessna_mono_cam`（`rc_cessna` + `mono_cam`）模型、WGS-84 海岸巡逻 world、跑道/停机坪/
  进近信标、横风与故障时间轴；`run_simulator_3d_control_acceptance.py` 现按场景参数化，
  VTOL 与 fixed-wing 均执行 world/model/scenario hash 和共享 control gate。新增
  `src/multidetect/simulator/stack.py` 与 `stack-plan` CLI，生成不启动进程的 hash-bound
  PX4/Gazebo 命令、环境变量、端口和资产 provenance 预检。

- `SimulatorInteractiveRouter` 增加显式 `qgc_control_mode=read_only|sim_flight`。默认/fixture
  保持 `read_only`；真实 native QGC Fly View interactive job 默认选择 `sim_flight`，仅允许
  严格校验的 ARM/DISARM、VTOL transition、RTL/LAND、MISSION_START、PX4 mode 和最多 64 项
  `MISSION_ITEM_INT`，参数/坐标/身份/target/loopback 逐字段校验；未列出的 MAVLink、参数或
  FTP 写入继续 fail closed。router evidence 记录策略、allow-list、转发与拒绝计数。

- `src/multidetect/simulator/interactive_router.py` 为真实交互式 SIM 提供独立的
  literal-loopback QGC↔PX4 双向 UDP 代理。QGC 原生 isolated-HIL 端口（默认 `14649`）与
  router 输入（默认 `14650`）分离，PX4 telemetry 输入（默认 `14541`）与唯一控制输出
  `udpout:127.0.0.1:14540` 分离；`read_only` 策略只转发发现类只读帧并对 mission/parameter/
  flight write fail closed，`sim_flight` 则仅放行上一条 allow-list 所述的有界 ARM/模式/任务帧。
- 新增 `src/multidetect/simulator/ai_executor.py` 的 deterministic truth-free policy/executor
  接口与 `scripts/run_simulator_ai_acceptance.py` fixture：AI 只能提交有 envelope/watchdog
  的四轴 MANUAL_CONTROL，人工帧立即夺回 authority，GPS/链路/障碍故障进入 neutral/failsafe。
- 新增 `src/multidetect/simulator/live_ai_bridge.py` 与 `run` CLI 的
  `--ai-live`/`--live-ai`：仅在 `--interactive --ai-live` 显式 opt-in 时，将 interactive
  router 已校验、且已原样转发给 QGC 的 PX4 telemetry 送入 truth-free
  `SimulatorTelemetrySnapshot`，调用共享 `SimulatorAiExecutor`，再沿同一
  `SimulatorControlAuthority` → `SimulatorMavlinkControlAdapter` → `MANUAL_CONTROL` 路径输出。
  `ai.json`/`result.json.ai.live_bridge` 记录 observed/accepted/rejected frame、snapshot、AI
  step/accept/reject/failsafe/watchdog 计数；`interactive-router.json` 记录 telemetry sink 计数。
  默认关闭且不读取 `truth.json`；Docker passive acceptance 仍固定
  `flight_control_commands_sent=0`，不因此升级为真实 AI-control gate。
- `SimulatorTelemetryBridge` 增加独立 HEARTBEAT freshness lease：默认
  `heartbeat_timeout_s=2.0s`，超时将 `link_healthy=false`，经共享 executor/adapter 只发送一次
  canonical neutral 并进入 FAILSAFE；同一 stale 窗口的后续位置帧不重复发包。新 HEARTBEAT
  只恢复 telemetry lease，不自动解除 terminal FAILSAFE；内部 replay fixture 可传
  `heartbeat_timeout_s=None`，而 executor 自带 watchdog 仍保持有效。raw `GPS_RAW_INT` 也保留
  `vel/cog` 字段，保证速度观测在 frame/mapping 两条输入路径一致，并由 bridge tests 固定回归。
- interactive router 的 event-loop 现在统一拥有 live AI watchdog 调度：每轮
  `poll_watchdog()` 先经 `watchdog_sink` 驱动 bridge 的 HEARTBEAT freshness 和共享 executor
  watchdog，stale/超时只产生一次 canonical neutral/failsafe；无 sink 或 sink 异常则回退
  adapter `check_watchdog()`。`interactive-router.json` 新增 `watchdog_ticks`、
  `watchdog_sink_calls`、`watchdog_sink_errors`、`watchdog_sink_last_error`，并以测试固定
  sink 不重复发包、异常回退和 stale 单次 fail-safe 语义。
- `SimulatorOrchestrator` 持久化 `interactive-router.json`、`ai.json` 和 control-route 端点
  证据；dry-run 的 AI step 使用进程内 recording sink，不会打开真实 UDP/pymavlink。

- 新增 `src/multidetect/simulator/control_gateway.py`：为交互式 3-D SIM 提供独立的
  loopback-only QGC `MANUAL_CONTROL` 网关。它严格校验 QGC `255/190` 身份、SITL target 1
  和轴范围，并将输入送入与 AI 共用的 authority/adapter；不改变验收路由的 QGC 只读契约。
- `SimulatorOrchestrator` 现在在 SIM job 内启动共享 adapter 的 control-gateway 后台线程，
  写入 `control-gateway.json`，停止时对活动 AI 流发送 neutral/failsafe；新增网关拒绝、人工
  接管、生命周期和证据回归测试。

- 新增 `src/multidetect/simulator/flight_model.py`：确定性、truth-free 的 bounded VTOL/
  fixed-wing kinematic flight model，支持 seed、east/north 风场、可配置姿态/速度/高度边界、
  单调时间戳和 canonical evidence hash；它是 CI/协议 fallback，不替代 Gazebo 刚体动力学。
- 新增 `src/multidetect/simulator/closed_loop.py`：将 `DeterministicFlightModel` 接到同一个
  `SimulatorControlAuthority` → `SimulatorMavlinkControlAdapter` → `MANUAL_CONTROL` 路径，
  以进程内 MAV sink 运行 AI hover/track、人工夺权、watchdog/neutral/failsafe 和 telemetry
  回环；新增 CLI `python -m multidetect.simulator closed-loop`。
- 新增 `src/multidetect/simulator/interactive_acceptance.py`、
  `scripts/run_simulator_interactive_acceptance.py` 及 CLI
  `python -m multidetect.simulator interactive-acceptance`：复用 native QGC
  `MANUAL_CONTROL`/telemetry router、共享 authority/adapter 和 deterministic flight model，
  在不启动 PX4/Gazebo/QGC/UDP 对端的情况下覆盖 VTOL/fixed-wing、AI TRACK、手动接管与恢复、
  watchdog neutral/FAILSAFE、heartbeat 原字节转发、只读 PARAM_REQUEST_LIST 转发、mission
  write 阻断和 loopback/identity 拒绝计数。输出 claims 明确保留视觉/PX4/Gazebo/硬件/真实飞行
  `not_claimed`。
- 新增 `scripts/run_simulator_closed_loop_acceptance.py`：一次性覆盖 VTOL/fixed-wing、AI
  观测、人工接管/恢复和 truth-free evidence；新增
  `scripts/run_simulator_3d_control_acceptance.py` 及 PowerShell wrapper，将场景 manifest、
  Gazebo world/model 资产哈希与共享 manual/AI control gate 绑定。该 3-D gate 只做资产/协议
  machine check，不冒充 Gazebo/QGC 渲染或硬件验收。

- `src/multidetect/px4_mission.py` 新增 fixed-wing compiler：
  `FixedWingMissionGeometry`、`Px4FixedWingSafetyPolicy` 和
  `compile_fixed_wing_executable_mission(...)` 将 runtime-channel 几何与平台无关计划编译为
  hash-bound `MAVLink2/MISSION_ITEM_INT`（`TAKEOFF → waypoints → landing_approach → LAND`）。
  编译前 fail closed 检查 geofence/home、airframe allow-list、GPS/电池/能量 reserve、
  最小航段与 turn radius、landing approach/glide slope、起降航向及 landing tailwind/crosswind；
  `truth`/evaluator-only 字段不会进入 mission。

- 新增 `src/multidetect/fixed_wing_acceptance.py` 与
  `scripts/run_fixed_wing_software_acceptance.py`：使用生产目标池、Mode-3
  challenge/确认和固定翼姿态执行器跑通 `DET → TRK → LCK → TGT`、姿态限幅/slew、
  prestream/`OFFBOARD`、RC 取消及失锁恢复，并输出结构化 evidence。
- 新增 `tests/test_fixed_wing_acceptance.py`，覆盖状态链、控制门、原生 QGC UI 契约字段及
  CLI evidence 写入。

### Changed

- repository stack 且未提供 custom process specs 时，`SimulatorOrchestrator` 默认准备 PX4
  job-local wrapper；non-dry-run 默认 strict。`job.json`/`result.json` 现在持久化
  `px4_launch_enabled`、`strict_requested`、plan hash、artifact verification 与错误。实际启动只在
  wrapper readiness 通过后替换 PX4 process spec，防止 stock PX4 静默退回 `18570/14580`。
- SIM 证据门明确区分 wrapper preflight、external no-launch plan 与外部 runtime：
  `px4-launch.json.readiness.gate_passed` 不代表进程/telemetry；dry-run 的
  `external-stack.json.validation.gate_passed` 也不能替代 `validation.ready_to_launch`。只有 PID/
  exit、PX4 heartbeat、`14540/14541` socket、Gazebo camera/NavSat、QGC RTSP/metadata 和清理证据
  完整时，才记一次外部 SIM runtime gate。

- `SimulatorOrchestrator`/`python -m multidetect.simulator run` 新增
  `--qgc-control-mode {read_only,sim_flight}`；提供 QGC 且启用 `--interactive` 时默认
  `sim_flight`，headless/dry-run 默认 `read_only`。生成的 `job.json`、`result.json` 与
  `interactive-router.json` 均持久化实际策略和 bounded command/mission evidence，避免把
  只读 acceptance fixture 误当作可飞行链路。
- SIM 文档和运行示例补充 fixed-wing `coastal_patrol_fixed_wing_001` 与 `stack-plan` 预检；
  所有条目继续区分 software/protocol/asset machine gate 与 Gazebo/QGC 视觉、Pixhawk/Jetson
  HIL、GNSS/相机硬件及真实飞行验收，不提升 `v0.3.0` 发布状态。
- Docker Desktop 计划修复同端口 UDP hairpin：只发布 loopback control `24540`，telemetry
  `14541` 作为 `host.docker.internal` outbound-only 目标（`published=false`），并在 planner、
  container inspection、Docker acceptance fixture 和 AI acceptance fixture 中固定回归；
  旧 artifact 保留原始状态，不被新证据覆盖。

- `SimulatorOrchestrator` 新增 `--throttle-wire-mode {qgc_legacy,signed}`（默认
  `qgc_legacy`），并将同一模式贯穿 QGC/AI/closed-loop adapter；legacy 的
  `MANUAL_CONTROL.z` 为 `[0,1000]`、neutral `500`，signed 为 `[-1000,1000]`、neutral `0`。
  选择与端点写入 `job.json`、`control.json`、`result.json` 和 interactive-router adapter
  evidence，防止 wire 语义漂移。
- standalone `interactive_router` CLI 同步支持 `--throttle-wire-mode auto|qgc_legacy|signed`；
  `auto` 对非负 legacy/负值 signed 输入做兼容解码，adapter 输出仍固定为 legacy，证据顶层
  记录实际模式。
- `run_simulator_3d_control_acceptance.py` 现在把 interactive acceptance 子门原样嵌入
  `interactive_qgc_path`，并在顶层 `claims.native_qgc_interactive_path` 暴露子门结果；该字段
  只表示 native QGC MANUAL_CONTROL/telemetry 与共享 authority 协议回归，不提升视觉或硬件等级。
- interactive router 对跨来源 partial frame fail closed：丢弃并重置对应 decoder，证据保留
  `qgc_fragment_source_resets`/`px4_fragment_source_resets` 及
  `*_rejected_reasons.fragment_source_changed`，避免跨端口字节拼接。
- manifest `scenario_runner` 在 provider/AI/model step 异常时仍执行 close/failsafe 并写出
  完整 evidence，`terminal_reason=error:<ExceptionType>:<message>`；CLI 以退出码 `2` 暴露错误，
  不再因 evidence 已序列化而向 CI 假绿。正常 `frame_budget_complete` 仍返回 `0`。

### Verification

- Windows Docker Desktop 真实 VTOL gate 已通过：
  `artifacts/evaluation/simulator-docker-acceptance-20260828c.json`，SHA-256
  `B014B98EAE8602C0C0D4CD3DBA8DABC01FB0902F1C635A5CE26B274949706AF6`；容器日志 SHA-256
  `B35DC53623FF8E2F1DA9B390F732F4B4ADC2662A9E26B51E1F5F8825F1A1E192`。`gate_passed=true`、
  qualified GPS fix type 3、cleanup removed=true、flight-control commands=0；wildcard receive
  仅为 Windows Docker UDP proxy 的观测 fallback，host 发布仍是 `127.0.0.1`。此证据不提升
  QGC 桌面视觉、Pixhawk/Jetson/GNSS/相机硬件或真实飞行门。

- 同一 pinned image 的 fixed-wing `coastal_patrol_fixed_wing_001` Docker gate 也已通过：
  `artifacts/evaluation/simulator-docker-acceptance-fixedwing-20260828.json`，SHA-256
  `8EAFD249C5118E9703C5F20AE95D7B64FAF8450B627DC3A0911A785B587FC2F2`；日志 SHA-256
  `AB3A28ED802A4967ABF2DEEF6EF9788B7930212F713EDCEFAFA97118771CC301`。`PX4_SYS_AUTOSTART=4003`、
  `gz_rc_cessna_mono_cam`、qualified GPS fix type 3、cleanup removed=true 均在证据中绑定；
  仍不代表 Mission upload/readback、QGC 视觉、硬件或实飞。

- PX4 launch planner/wrapper、job containment、stock rc stanza patch、template/strict 行为、tamper
  验证、external-plan opt-in 和 orchestrator dry-run evidence 已加入 simulator 回归。就
  `px4_launch.py` wrapper/native 外部链而言，当前台式机 native probe 仍为 PX4/Gazebo 缺失，
  尚未取得 wrapper-backed PX4/Gazebo heartbeat/telemetry；Docker Desktop 的 VTOL/fixed-wing
  external telemetry gates 已在上方单独通过并封存。桌面 QGC 人工视觉、真实
  Pixhawk/Jetson/GNSS/相机、HIL/REAL 或实飞验收仍未完成。版本保持 `v0.3.0-pre.8`。

- `tests/test_simulator_interactive_router.py`、`tests/test_simulator_interactive_acceptance.py`、
  `tests/test_simulator_ai_executor.py`、`tests/test_simulator_orchestrator_interactive.py` 与
  SIM fixture gates 覆盖手动接管、
  AI RETURN/GPS gate、watchdog、端点身份、QGC native isolated-HIL 参数和停止时 neutral。
  router evidence 还固定 `qgc_identity`（`255/190`、target system/component）及 QGC/PX4
  datagram blocked 计数。
  这些是软件/协议 machine checks；人工 QGC 视觉、桌面 GPU、真实 Pixhawk/Jetson/GNSS/相机和
  真实飞行验收仍分别记账，不能由 fixture 代替。

- pre.8 deterministic closed-loop/flight-model/3-D contract checks 可用以下命令复跑：

  ```powershell
  cd D:\Projects\Multi-Detect
  $python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
  $env:PYTHONPATH = 'src'
  & $python -m pytest -q `
    tests\test_simulator_flight_model.py `
    tests\test_simulator_closed_loop.py `
    tests\test_simulator_closed_loop_acceptance.py `
    tests\test_simulator_3d_control_acceptance.py `
    tests\test_px4_fixed_wing_mission.py
  & $python scripts\run_simulator_closed_loop_acceptance.py `
    --out artifacts\evaluation\simulator-closed-loop-acceptance.json
  & $python scripts\run_simulator_3d_control_acceptance.py `
    --out artifacts\evaluation\simulator-3d-control.json
  & $python -m multidetect.simulator interactive-acceptance `
    --mode both `
    --out artifacts\evaluation\simulator-interactive-acceptance.json
  ```

  三个 acceptance JSON 的 `gate_passed` 应为 `true`；closed-loop 结果必须同时列出 `vtol`
  与 `fixed-wing`，3-D 结果必须保留 world `EARTH_WGS84`/`max_step_size=0.004`、
  `manual_and_ai_share_authority_path=true`、`native_qgc_interactive_path=true`，并把
  `visual_acceptance`/`hardware_acceptance` 保持为 `not_claimed`；interactive 结果还应有
  `qgc_manual_frames_accepted=1`、`qgc_read_only_frames_forwarded=1`、
  `qgc_mission_writes_allowed=false` 和 `telemetry_frames_rewritten=false`。

- scenario runner 的正常终态可用以下命令检查（应保存 JSON、
  `terminal_reason=frame_budget_complete` 并返回 exit code `0`）；provider/AI/model 注入异常
  的集成路径另由 runner 回归覆盖，异常时仍保存关闭后的 JSON、`terminal_reason` 以 `error:`
  开头并返回 exit code `2`：

  ```powershell
  & $python -m multidetect.simulator scenario `
    --scenario configs\scenarios\coastal_patrol_fixed_wing_001.scenario.json `
    --frames 8 --dt 0.1 --mode fixed-wing `
    --out artifacts\evaluation\simulator-scenario-fixed-wing.json
  ```

- `tests\test_px4_fixed_wing_mission.py`：12 passed，覆盖 fixed-wing compiler 的
  deterministic/hash-bound 输出、包络/围栏/能量/风/转弯/进近 fail-closed 检查和 truth 隔离；
  该结果不替代 PX4 upload/readback、Gazebo/QGC 视觉或真实飞行门。

- 固定翼 software/HIL-preflight acceptance 通过；transport 为内存记录器，
  `pixhawk_link_opened=false`、`mavlink_messages_sent=0`、`physical_output_enabled=false`。
- QGC 继续复用原生 `FlyView`/`FlyViewVideo`/`PipState`/toolbar/map surface 和
  `QGCButton`/`QGCLabel`/`QGCPalette`/`QGCToolInsets`/`ScreenTools`，没有第二窗口、第二
  视频 surface 或独立主题；人工 Computer Use/Fly View 验收仍为
  `operator_visual_acceptance=not_claimed`。

## [0.3.0-pre.8] - 2026-08-26

版本映射：release label `0.3.0-pre.8`、Python/wheel `0.3.0rc8`、CMake/PE numeric
`0.3.0`、channel `prerelease`。

### Changed

- QGC 状态工具栏改用官方 `/qmlimages/Analyze.svg` 与 `QGCColoredImage`，采用原生
  `ScreenTools.smallFontPointSize` 两行状态布局，移除 Multi-Detect 私有状态圆点。
- 状态详情仍通过 QGC 原生 `ToolIndicatorPage`/`SettingsGroupLayout` 抽屉呈现；REPLAY、SIM、
  HIL、REAL 共用同一 Fly View、toolbar、状态和核心算法，不创建第二窗口、第二视频面板或私有主题。

### Verification

- `tests/test_qgc_native_flyview_style.py`、`tests/test_qgc_native_style_capture_script.py`、
  `tests/test_qgc_desktop_capture_validation.py`：25 passed。
- 2026-08-27 combined SIM/QGC/Gazebo/VTOL machine gate 通过：
  `artifacts/evaluation/combined-sim-qgc-20260827T002714Z-61220/combined-evidence.json`
  (`309,240` bytes，SHA-256 `1E8C3271E44902B5F629524E5C04079B08A51F26B2ABEA68E0CB3306990B2EFD`；
  `gate_passed=true`，600 帧，签名 runtime-status/ACK 连续，VTOL Mission 与清理检查通过)。
  原生 QGC toolbar、PiP、视频 surface 的 exact PID/HWND machine smoke 通过；仍保留
  `operator_visual_acceptance=not_claimed`，不宣称人工 Computer Use 视觉验收。
- exact-client machine capture：
  `build/qgc-native-style-pre.8-cu-authorized-20260826/qgc-native-style.png`，`475,726` bytes，SHA-256
  `32C6461104D01200DF89F35B45C7CE87E58814E0E3273BA2B8596AB62CDD1263`；
  `single_native_surface_machine_check=true`。当前主机未暴露 `@oai/sky` Computer Use client，
  因此 `operator_visual_acceptance` 仍保持 `not_claimed`，不把截图冒充人工点验。

- pre.8 Windows Release/portable 在证据卡片空值格式化修复阶段完成的历史增量重建：主 EXE
  `46,552,576` bytes，SHA-256
  `600E98F31EFD8D9751F954EDA0473D0746543F1504245979A96EBB1F856D19DE`；RTSP helper
  `79,872` bytes，SHA-256 `50F17CF7E19542748035329585DBE59526D2739CB7BC41B65A72003C84C1F6A4`。
  portable 目录为 `1,013` 个文件、`529,265,549` bytes，`bin/` 为 `233` 个文件，Release 与
  portable 主 EXE 字节一致；构建 wrapper 退出码为 `0`。GPS/回放修复后的当前 pre.8
  构建已在本节顶部单独记录，不能用该历史指纹替代当前制品。

## [0.3.0-pre.7] - 2026-08-26

版本映射：release label `0.3.0-pre.7`、Python/wheel `0.3.0rc7`、CMake/PE numeric
`0.3.0`、channel `prerelease`。

### Changed

- 室外目标落图改用无新增依赖的 WGS-84 Vincenty direct geodesic，按椭球曲率处理不同纬度的
  南北/东西尺度，并保持经度跨日期变更线归一化。
- 非法地面测距置信区间现在 fail closed，不再以零误差悄悄发布坐标。
- QGC 自定义层继续严格复用原生 Fly View/Video/PiP/toolbar/map 和 QGC palette/controls；
  没有新增窗口、播放器或独立模拟器主题。

### Verification

- `tests/test_target_geolocation.py`：8 passed，覆盖椭球投影、曲率、高纬度、日期变更线和
  非法置信区间。
- combined SIM wrapper 现在从 `synthetic-fire-nx6-hil.manifest.json` 的 NCHW `input.shape` 自动
  推导 `--input-width/--input-height`，不再把模型尺寸硬编码为 `64x64`；专项静态回归通过。
- pre.7 Windows Release/portable 已重建：主 EXE `46,551,552` bytes，SHA-256
  `B24754D7751521632C3C1250D69B4476CD13B8A6DCA09248A79B26CCD93973EA`；RTSP helper
  `79,872` bytes，SHA-256 `769C4B0AD3DEEAE7398927B9159296D0C2172350F218663F632D5F6689B01D9D`。
- 600-frame combined PX4/Gazebo/QGC SIM machine gate 已通过：
  `artifacts/evaluation/combined-sim-qgc-20260826T201913Z-52120/combined-evidence.json`
  为 `309,608` bytes，SHA-256 `25B91825FE8426450E98D25DF55CDC7FE0641C950092A929B5CDE681E21EACCE`，
  `gate_passed=true`、7-item INT-only VTOL Mission、MC→FW→MC→land→disarm、最大离家距离
  `142.488 m`、最大相对高度 `24.735 m`；QGC 仍保持单一原生 Fly View surface。
- QGC 原生 UI 静态专项和 exact-client machine smoke 继续通过；原生截图
  `build/qgc-native-style-pre.7-20260827-034544/qgc-native-style.png` 为 `475,725` bytes，
  SHA-256 `1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`。
  `operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`，不把机器证据冒充人工
  Computer Use/Fly View 验收。

## [0.3.0-pre.6] - 2026-08-26

版本映射：release label `0.3.0-pre.6`、Python/wheel `0.3.0rc6`、CMake/PE numeric
`0.3.0`、channel `prerelease`。

### Changed

- 修复 `short_term_tracking` 的相机运动模型选择：先拟合 affine，再仅在 projective strength
  与相对残差/内点收益同时满足时采用 homography，避免普通 roll/zoom/pan 被误判为
  `background_homography_flow`。
- 固化 REPLAY/SIM/HIL/REAL 复用同一原生 QGC `FlyView`/`FlyViewVideo`/`PipState`/toolbar/map
  surface 的 UI 约束；自定义叠加继续使用 QGC controls/palette/insets，不引入 simulator-only 页面或主题。
- 生成与当前源码版本绑定的 QGC replay fixture；Ground Truth 与 hardware output 继续保持隔离。

### Verified / Evidence boundary

- Python 回归、ruff、compileall 与 `git diff --check` 已通过；QGC replay fixture 单元/CLI 回归已通过。
- Windows Release/portable 已完成并核对：主 EXE `46,551,552` bytes，SHA-256
  `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`；RTSP helper
  `79,872` bytes，SHA-256 `33935619A2A9385E96106CC73AD9ED3F2090FC2F49C5ED2A00EC3D5732DDF387`。
  portable 目录为 `1,013` files / `529,264,527` bytes，`bin/` 为 `233` files；Release 与
  portable 主 EXE 字节一致。
- `operator_visual_acceptance=not_claimed`；HIL、REAL、室外 GNSS/真实硬件和最终人工 Fly View 验收仍 pending。
- pre.6 combined SIM/QGC machine gate 已封存：`artifacts/evaluation/combined-sim-qgc-20260826T125250Z-62292/combined-evidence.json`
  为 `309,023` bytes，SHA-256 `7D6FCD1976242C85EF783E9D812D98475E2A723BAB396241801CACDDBCA0EDFC`，
  `gate_passed=true`，600 frames、7-item VTOL Mission、MC→FW→MC→land→disarm、GPS fix type `3`、
  `10` satellites、false geolocation `0`，最大离家距离 `143.728 m`、最大相对高度 `24.473 m`。
- 同轮原生 QGC 风格 exact-client machine capture 为
  `build/qgc-native-style-20260826-current3/qgc-native-style.png`，`476,031` bytes，SHA-256
  `653997AC48944DB232AFD1A8C9F5AA66C1C62436016E863F44B6D129FB1AF3E2`；该记录不提升为人工
  Computer Use/Fly View 视觉验收。
- 后续 current4 exact-client machine capture 为
  `build/qgc-native-style-20260826-current4/qgc-native-style.png`，`475,725` bytes，SHA-256
  `1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`；该记录同样保留
  `computer_use_tool_exposed=false` 、`operator_visual_acceptance=not_claimed` 和
  computer_use_operator_authorized=true (operator pre-approval recorded)
  `qml_rendering_proved=false`，不替代人工 Fly View 验收。
- 追加软件侧 HIL 基线：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260826T145956Z-69528.json`
  （`7,727` bytes，SHA-256 `0A6BD7E388E60F79D7371595481412DB21FCF7C71C18ACBFB69761680887CDFD`），
  `gate_passed=true`；它只覆盖 localhost/PX4 SITL、签名 operator loop、只读路由和状态机，
  `real_v6x_contacted=false`、`hardware_control_enabled=false`，不能替代真实台架 HIL。
- 本轮按原生 QGC 风格约束完成台式机 Release/portable 重建：构建脚本退出码 `0`，主 EXE
  `46,551,552` bytes，SHA-256 `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`；
  portable 与 Release 主 EXE 字节一致，RTSP helper 保持 `79,872` bytes、SHA-256
  `33935619A2A9385E96106CC73AD9ED3F2090FC2F49C5ED2A00EC3D5732DDF387`。
- 重建后 exact-client 原生 QGC machine capture 为
  `build/qgc-native-style-20260826-postbuild/qgc-native-style.png`，`476,031` bytes，SHA-256
  `653997AC48944DB232AFD1A8C9F5AA66C1C62436016E863F44B6D129FB1AF3E2`；仍不冒充 Computer Use
  人工视觉验收。

## [0.3.0-pre.5] - 2026-08-26

版本映射：release label `0.3.0-pre.5`、Python/wheel `0.3.0rc5`、CMake/PE numeric
`0.3.0`，channel `prerelease`。

### Added

- 为 `live-camera` 增加 profile-bound HIL/REAL CLI 回归：运行模式、签名 profile、HMAC key
  env、airframe hash、Pixhawk endpoint、scenario provenance、target component 和 RC rate
  的组合边界现在由测试固定下来。

### Changed

- `runtime_launcher.py` 继续作为 HIL/REAL 的唯一身份门：HIL 拒绝 signing identity 与
  flight-control opt-in；REAL signing key 只在本地从环境变量解码并计算 SHA-256；启动失败
  时清理 Pixhawk/frame source。
- 自定义 QGC Plan/Select 组件移除未使用的 `QtQuick.Controls` 导入，保持原生 QGC controls、
  palette、toolbar、map/PiP surface，不引入第二主题或 simulator-only 页面。

### Verified / Evidence boundary

- 项目 venv 全量回归通过：`pytest -q` 退出码 `0`，`compileall -q src tests scripts`、
  `ruff check src tests scripts` 和 `git diff --check` 均退出码 `0`。
- Windows Release/portable 构建退出码 `0`；主 EXE `46,551,552` bytes，SHA-256
  `D00B984C11C195A6DD16B95094D4629A790219F0CEF54591C0973AB6EA87610C`；RTSP helper
  `79,872` bytes，SHA-256 `B371577996B81D7F9CD4C1FA4B7D175D3944DBCF082F7CD7D666F8637BF60D73`。
- portable 目录为 `1,013` files / `529,264,519` bytes，`bin/` 为 `233` files；Release 与
  portable 主 EXE 字节一致。pre.4 的 600-frame combined SIM/QGC 证据继续作为基线，不被
  pre.5 源码变更冒充为新集成证据。
- 人工 Computer Use/Fly View 视觉验收继续单独记账，未将静态测试或 headless 日志写成人工结论。

### Verified — pre.5 authoritative artifacts

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `build/windows-v0.3.0-pre.5-release/Release/MultiDetectGCS.exe` | `46,551,552` | `D00B984C11C195A6DD16B95094D4629A790219F0CEF54591C0973AB6EA87610C` |
| `build/windows-v0.3.0-pre.5-release/portable-v0.3.0-pre.5/bin/MultiDetectGCS.exe` | `46,551,552` | `D00B984C11C195A6DD16B95094D4629A790219F0CEF54591C0973AB6EA87610C` |
| `build/windows-v0.3.0-pre.5-release/portable-v0.3.0-pre.5/bin/MultiDetectReplayRtspPublisher.exe` | `79,872` | `B371577996B81D7F9CD4C1FA4B7D175D3944DBCF082F7CD7D666F8637BF60D73` |

## [0.3.0-pre.4] - 2026-08-26

版本映射：release label `0.3.0-pre.4`、Python/wheel `0.3.0rc4`、CMake/PE numeric
`0.3.0`，channel `prerelease`。

### Added

- 新增确定性 PX4 VTOL Mission 层 `src/multidetect/px4_mission.py`：runtime-only
  `VtolMissionGeometry`、有界 `Px4MissionSafetyPolicy`、`px4-vtol-mission-int-v1` adapter、
  canonical MAVLink 2 `MISSION_ITEM_INT` 编译、完整 VTOL lifecycle 校验、hash-bound
  `MissionTransactionBindings` 与 upload/readback/commit/start transaction。
- 新增 `scripts/vtol_sitl_mission_acceptance.py` headless evaluator helper。它附着 combined runner
  已拥有的 container/router/PX4，完成 INT-only upload/readback、protocol hash 复核、exact PX4
  `AUTO Mission` 启动以及转换、航点、落地、disarm、GPS/geofence 观察；不创建第二个 simulator
  或第二套 UI。
- 扩展 `src/multidetect/px4_gazebo_qgc_router.py`：QGC 通道继续只读，evaluator 只获得
  identity-bound、mission-type-bound、item/command/frame/coordinate-bounded 的 Mission 协议和
  既定 VTOL 生命周期命令；`MISSION_CLEAR_ALL` 与无关写帧保持禁止。
- 新增 exact-client 桌面截图 validator `scripts/validate_qgc_desktop_capture.py`。combined wrapper
  通过 exact PID/HWND、Per-Monitor V2、`GetClientRect`/`ClientToScreen`、遮挡枚举、25 点
  `WindowFromPoint` ownership grid 和 `CopyFromScreen` 捕获真实桌面 client pixels，并恢复窗口与
  输入状态。
- 新增 capture/Mission/router/combined 的专项测试源码；全量 pytest 计数与静态检查结果见本版本
  `Verified` 段。

### Changed

- 把 QGC 原生 UI 规则提升为 pre.4 硬约束：只使用现有 `FlyView`、`PipState`、
  `FlightDisplayViewVideo`、`QGCPalette` 和 QGC controls/`ScreenTools`；PiP 只读，full state 才允许
  Multi-Detect 目标操作。禁止 `Window`/`ApplicationWindow`、第二视频 surface、独立模拟器、
  simulator-only QML 和自定义主题。
- pre.3 `PrintWindow` GPU 黑面截图继续保留为历史 machine smoke；pre.4 的 visible-video gate
  改为 video-only ROI 双帧 nonblack/nonuniform，加 RTSP streaming/decoding 与 native type 24
  sequence advancement 的 transport 绑定。temporal diff 仅作诊断，不冒充 exact source-frame identity。
- combined SIM/QGC wrapper 已接入同一 owned PX4/Gazebo/router/QGC 上的 Mission helper、任务活动同步点
  与 Mission evidence 槽位；真实 600-frame combined Mission run 的结果见本版本 `Evidence boundary`
  段。

### Verified

- 项目 venv 全量回归：`1513 passed in 28.21s`，exit code `0`；日志保存在
  `build/validation/pre4-full-pytest-20260826-summary.log`。
- Python `compileall`（`src/`、`tests/`、`scripts/`）exit `0`，13 个 PowerShell 脚本 AST
  解析 `0` errors，仓库 `git diff --check` exit `0`。

### Evidence boundary / Pending

- pre.4 Windows Release/portable 已完成 root 重建、portable 部署与 SHA-256 封存；权威 combined
  evidence 为 `artifacts/evaluation/combined-sim-qgc-20260826T081624Z-50796/combined-evidence.json`。
- pre.4 `river_rescue_vtol_001` 的 600-frame combined VTOL Mission 运行已通过：`gate_passed=true`、
  7-item INT-only upload/readback/hash、MC→FW→MC、land/disarm、GPS/geofence 与原生 QGC
  FlyView/toolbar machine smoke 均通过。它闭合的是该 SIM machine/E4 gate，不代表完整所有场景。
- 自动桌面取证至多允许声明 `transport_bound_visible_video_surface_proved`；当前不声明
  exact source-frame identity 或完整 QML semantics。`operator_visual_acceptance=not_claimed`、
  `qml_rendering_proved=false`，人工 Fly View 点验仍是独立发布门。
- `desktop_capture_gpu_surface_proved=false` 仍是独立诊断；不能据此否定 native QQuickItem video
  pixels，也不能把机器证据写成人工画面验收。
- 真实 `HIL`、`REAL`、室外相机/GNSS/飞行硬件以及最终发布门仍未完成，故版本继续保持 prerelease。
- 下列 pre.2/pre.3 构建、哈希、测试和 combined evidence 保留为历史版本事实，不覆盖、重命名或
  提升为 pre.4 证据。

### Verified — pre.4 authoritative artifacts

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `build/windows-v0.3.0-pre.4-release/Release/MultiDetectGCS.exe` | `46,551,552` | `B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142` |
| `build/windows-v0.3.0-pre.4-release/portable-v0.3.0-pre.4/bin/MultiDetectGCS.exe` | `46,551,552` | `B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142` |
| `build/windows-v0.3.0-pre.4-release/portable-v0.3.0-pre.4/bin/MultiDetectReplayRtspPublisher.exe` | `79,872` | `F6F8AC6DD87DAA6BA7CF477AC62BDE5889B724885AE5C9DB285A485050FEFE37` |

`gate_passed=true` 的 combined 运行命令、容器/router 状态、Mission 结果和 QGC 原生 surface
证据全部保存在上述 `combined-evidence.json` 及其同目录日志/截图中。`operator_visual_acceptance`
仍为 `not_claimed`；`v0.3.0` 的 HIL/REAL/室外硬件门继续阻塞。

## [0.3.0-pre.3] - 2026-08-25

### Added

- `integrated-replay-qgc` 的真实 QGC E4 验收记录：raw recording 经过与 `live-camera` 共用的
  production perception factory、LiveMissionRunner、RTSP 视频和 signed operator metadata，
  在实际便携版 MultiDetectGCS 进程中完成同帧 barrier。
- QGC Fly View 原生画面证据：使用原有 `FlightDisplayViewVideo`、`PipState`、QGC toolbar、
  QGC controls 和地图 PiP；不新建仿真专用 UI 或独立窗口。QGC 临时 profile 可用
  `QGCQml/MainFlyWindowIsMap=false` 让视频启动即为主 Fly View，避免 PiP 切换重启 RTSP session。
- 运行身份 badge 改为直接取 QGC `QGCPalette`（`brandingBlue`、`warningText`、`colorGreen`、`window`），
  不再维护独立的 Multi-Detect 颜色体系；REPLAY/SIM/HIL/REAL 共用同一个原生 Fly View layer。
- 目标框、目标落图卡片、状态 HUD、深度 PiP、LCK/TRK 操作按钮和选择提示统一改为 QGC
  `QGCPalette`/`QGCButton`/`QGCLabel`；只保留深度 false-colour legend 作为数据编码，不再额外
  引入一套独立的 UI 主题。
- `scripts/create_qgc_replay_acceptance_fixture.py` 修复 fixture 自身 `fixture_summary` 字段写入，
  正式 fixture 位于 `build/qgc-replay-fixture-v0.3.0-pre.3/`。
- 新增 `scripts/gazebo_camera_capture.py --persist-frames` 与
  `scripts/create_gazebo_raw_replay_fixture.py`：将 digest 固定的 Gazebo Harmonic `gz_x500_mono_cam`
  真实 RGB/NavSat payload 绑定到 raw-only recording，再经 shared production perception、
  `LiveMissionRunner`、RTSP helper 和真实便携版 QGC 完成 12 帧同帧 transport barrier；不读取
  Scenario Ground Truth，不生成硬件输出。
- 修复高帧率 Gazebo replay 在 Windows `time.monotonic` 子毫秒 tick 下的 pacer 假失败：
  `WallClockReplayPacer` 使用一个最小 timer quantum，并仍保留注入时钟不前进时的 fail-closed 检查。

### Verified

- `E3 BUILT`：Windows Release/portable 主 EXE `46,556,672` bytes，SHA-256
  `5F462F8EDAD3677AE3CE88C0EC1B49CA9ECCA6B0B25A7E504A80B0BC7BBC0FD3`；RTSP helper 为
  `75,264` bytes，SHA-256 `28CD41016BA93A406FC9B9E3B98B0809B4AB8675D9A04821E1DD64C495FC5C47`。
- `E4 INTEGRATED`：`build/e4-qgc-20260825-222239/e4.result.json` 报告
  `complete=true`、3 个 video frame、3 个 metadata barrier、最大 ACK→metadata 偏差 `0.0 ms`、
  QGC peer `127.0.0.1:14581`、RTSP helper exit `0`、EOS/summary 均收到。
- `E4 visual`：`build/e4-qgc-visual-20260825-224648/qgc-flyview-video-initial.png` 与
  `qgc-flyview-video-stable.png` 由实际 QGC 桌面画面检查；原生 QGC toolbar、RTSP 视频、地图
  PiP、REPLAY badge、M1/DEPTH overlay 均可见且无 UDP 模态错误。机器 result 中的
  `operator_visual_acceptance` 仍保留 `not_claimed`，人工检查单独在最终 evidence JSON 标记。
- 历史 pre.3 palette rebuild 的 native-QGC style smoke 位于 `build/e4-qgc-visual-20260826-024914/`；截图来自当时的 portable binary。
  RTSP helper 提前断开，未计入 E4 gate，`operator_visual_acceptance` 仍为 `not_claimed`。
- `E4 PX4 SITL signed metadata`：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json`
  记录真实便携 QGC 进程与 pinned fixed-wing PX4 SIH 的闭环：QGC/PX4 exit `0`、PX4 heartbeat `5`、
  signed metadata `37`、tracking `30 @ 20 Hz`、目标池 `3/2 pages`、challenge-bound `APPROVE`，
  QGC forbidden PX4 messages、file-mutating FTP、system-time、operator tunnel-to-PX4 和所有写入均为 `0`。
  端口 `14669/14666/14670/14667/14668/18570` 全部释放，保护端口 `14550` 未改变；真实 V6X/Jetson/G20
  未接触。operator metadata 只走严格 MAVLink 2 signing，没有 unsigned fallback。
- 隔离 HIL 的 QGC 关闭路径已稳定化：仍由原生 `MainWindow.qml::finishCloseProcess()` 负责
  link/video teardown；Windows acceptance 进程在 `QGCApplication::shutdown()` 后使用受控退出，
  避开 QML/global-singleton 销毁竞态，普通 QGC 关闭路径不变。此前 3 次真实 QGC + pinned PX4 SITL
  验收均为 QGC/PX4 exit `0`、`all_passed=true`、端口释放且无新 crash dump；随后两次 QGC palette
  rebuild 也通过同一门槛，当前 `stability_runs` 共 5 次，最新结果为 `20260825T190317Z-5732`，
  并追加到同一清单。 QGC acceptance 启动窗口固定为 45 秒，覆盖 Windows Qt Multimedia/GStreamer 冷启动。
- `E4 PX4 fixed-wing SIH Mission transaction`：`artifacts/evaluation/px4-fixed-wing-sitl-auto-mission-acceptance-20260825T170332Z-62660.json`
  记录 3-item Mission 的 `MISSION_COUNT`/`MISSION_REQUEST_INT` readback；顺序、命令、坐标、高度
  和参数契约通过，PX4 协议默认归一化后的 upload/readback canonical SHA-256 均为
  `14acad2d86fa047ae8af8388f7f792a018793fa65d30c9be62e425c33b484018`，`items_match=true`、
  `hash_match=true`。该 run 的 MISSION sequence 到 `2`、移动 `158.250773 m`，并保持
  Multi-Detect transmit `0`、payload action `0`、保护端口不变。
- 续验固定翼 QGC/PX4 软件 HIL：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json`
  通过 `gate_passed=true`、QGC/PX4 exit `0`、PX4 heartbeat `6`、signed metadata `38`、
  tracking `30 @ 20 Hz`、target pool `3/2 pages`、challenge-bound `APPROVE`，并保持
  forbidden PX4 messages、FTP、SYSTEM_TIME、operator tunnel-to-PX4、写入和 payload action 全部为 `0`。
  本次还修复 Windows wrapper 在 Docker Desktop 未加入 PATH 时直接调用 `docker` 的环境依赖：
  `scripts/run_px4_sitl_qgc_operator_acceptance.ps1` 现在优先使用 PATH，回退到标准 Docker Desktop
  安装路径，并由 `tests/test_px4_sitl_qgc_operator_acceptance_script.py` 锁定该行为。
- Gazebo Harmonic sensor 子门：digest 固定的 `px4io/px4-sitl-gazebo` headless 容器运行
  `gz_x500_mono_cam`，真实 Gazebo image/CameraInfo/NavSat topic 捕获 12/12 帧 `1280×960 RGB`，
  时间戳严格递增；结果 `artifacts/evaluation/gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json`
  的 SHA-256 为 `63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D`。该子门仍不计入
  QGC E4 同帧 barrier，`operator_visual_acceptance=not_claimed`。
- Gazebo→QGC 同帧 transport 子门：`build/qgc-replay-fixture-gazebo-v0.3.0-pre.3/` 由上述
  12 帧真实 Gazebo pixels 生成；`build/e4-gazebo-qgc-20260826/e4.result.json` 的 SHA-256 为
  `520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926`，记录
  `complete=true`、`12/12` video、`12/12` metadata barrier、最大偏差 `0.0 ms`、helper exit `0`。
  这是 transport 子门，不是人工 `operator_visual_acceptance`，也不是完整 fixed-wing/VTOL SIM、
  HIL 或 REAL 证据。
- 最终 pre.3 机器证据清单：`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json`，
  SHA-256 为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`；清单包含构建、根目录
  全量 Python `1330 collected / exit 0`、QGC custom/acceptance 精确选择集 `69 passed` 和本轮扩展的 `84 passed`
  QGC/Gazebo/replay 目标测试、C++ 自检、15 秒 portable smoke、Gazebo→QGC transport 和桌面画面指纹。全量回归日志为
  `build/validation/pre3-full-regression-continued-20260826.log`，SHA-256 为
  `57845C3D37460512104A7DB4F9D2F670D626B12C13E78417A1B353DF56838B8B`；算法 benchmark 使用
  contention-adjusted wall/CPU gate，同时保留原始 wall-clock 指标。
- 继续回归：根目录 Python 全量为 `1330 passed / exit 0`，日志
  `build/validation/pre3-full-regression-continued-20260826.log`，SHA-256
  `57845C3D37460512104A7DB4F9D2F670D626B12C13E78417A1B353DF56838B8B`；QGC custom/acceptance
  精确选择集为 `69 passed`，扩展选择集为 `84 passed / exit 0`。更新后的机器证据清单 SHA-256
  为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

### Pending

- Gazebo/PX4 SITL 视觉闭环、完整 fixed-wing/VTOL Mission planner/path、真实 Pixhawk/Jetson
  HIL、室外 GNSS 和受限飞行仍未完成；fixed-wing SIH 的 Mission upload/readback/hash 只闭合
  该 software-only 子门，因此 prerelease 后缀不能移除。

## [0.3.0-pre.2] - in progress

### Added

- Raw-only `RawSensorReplayBackend` 与对应的严格 manifest Schema。记录只允许受哈希绑定的
  image references 和归一化只读 `VehicleTelemetry`；Detection、Ground Truth、
  expectations 和未知字段均被拒绝。
- 必填 RFC 3339 UTC `started_at_utc`，用于将 recording 的 monotonic capture timeline 唯一
  锚定到 UTC，并纳入 canonical recording hash。
- `LiveMissionRunner.from_runtime_backend(...)`，由 raw-sensor backend 独占提供
  `FrameSource`、`TelemetryProvider` 和 `Clock`；确定性 descriptor 必须绑定
  `ManualClock`。
- `build_production_perception(...)` 生产感知 factory，供 `live-camera` 和 headless
  `integrated-replay` 共用同一 detector/verifier/safety/priority/environment 构建与
  manifest role gate。
- headless `integrated-replay` CLI：验证 raw recording 经上述 production perception factory
  和完整 `LiveMissionRunner` 运行，并输出绑定 descriptor/recording/mission/model-set 的
  run identity、session `RuntimeStatusMessage`、mission audit 与结构化 result。headless status
  对象不冒充为已完成 QGC 签名传输验收。

### Changed

- `LiveMissionRunner` 核心新鲜度、截止时间、审计和 wire timestamp 统一使用可注入
  runtime clock；延迟仪表使用独立 performance clock，避免推进 Replay 业务时间。
- `ReplayFrameSource` 只在图像字节、SHA-256 和尺寸再验证成功后推进共享
  `ManualClock`；`ReplayTelemetryProvider` 只暴露当前回放时刻之前的最新样本，
  stale/skew 均失败闭锁。

### Verified

- `E2 TESTED`：`RawSensorReplayBackend` 的 raw-only 输入、manifest/event/image 完整性、
  UTC anchor、相对路径包含性、时序、遥测新鲜度和 close/fail-closed 专项通过。
- `E2 TESTED`：`LiveMissionRunner` 不再直接读取 `time.monotonic/time.perf_counter/time.time`；
  runtime backend 绑定、确定性 ManualClock 门和回放 cadence 专项通过。
- `E2 TESTED`：production perception factory 在 live/replay 两个 launcher 间共用同一 model-set
  identity 逻辑；headless integrated Replay 验证完整录制消费、稳定运行身份、
  RuntimeStatus、审计/结果重复性以及 Pixhawk/display/flight-output 失败闭锁。
- 上述 `E2` 只证明 headless backend/perception/runner 软件契约。Replay 的默认静态 performance clock
  显示 `0 ms` 只是确定性占位，不是性能证据；可选异步组件也尚未完成
  严格 bit-for-bit 调度证明，因此 deterministic launcher 当前显式拒绝异步
  semantic-context runner。
- `E2 TESTED`：根目录 Python 全量套件 `1303 collected / exit 0`（wall time `24.2 s`）；
  QGC custom + offline 静态回归 `61 passed in 0.18 s`。
- `multi_detect-0.3.0rc2-py3-none-any.whl` 已在
  `build/wheel-smoke-v0.3.0-pre.2/` 构建，大小 `662,046` bytes，SHA-256
  `DD96AEA726F4BE09557CD0F701772FCF9EAB1E4F41182DE9A0587603CF72B8E2`；隔离安装验证
  metadata、`multidetect.__version__`、release label 为
  `0.3.0rc2 / 0.3.0rc2 / 0.3.0-pre.2`。
- `E3 BUILT`：2623-step Ninja 图完成 Release 链接，wrapper 继续完成 install/windeployqt
  并以退出码 `0` 结束；`MultiDetectOperatorProtocolSelfTest` 退出 `0`，DepthGrid Python
  fixture 生成 `44` 个 HMAC fragment 后，C++ 往返自检也退出 `0`。
- `E3 BUILT`：Release 与 portable 主 EXE 均为 `46,525,440` bytes，PE File/Product version
  均为 `0.3.0.0`，SHA-256 均为
  `D5A096F69DA89CAA58E0227B85A184C228B310BD5DCA314FDE906E7379125769`。portable 位于
  `build/windows-v0.3.0-pre.2-release/portable-v0.3.0-pre.2/bin/MultiDetectGCS.exe`，共有
  `1012` files、`529,158,529` bytes；依赖闭包检查通过。
- `E3 BUILT`：portable 在 offscreen/software backend 下存活 `15 s`，stdout/stderr 均
  `0 bytes`，随后只终止该 exact process object。
- `E3 BUILT`：机器可读证据清单
  `build/windows-v0.3.0-pre.2-release/verification/v0.3.0-pre.2-evidence.json` 的 SHA-256 为
  `D9D0EE425AEE6976A077B69B3EB8AD4C1C8F34FC69E1F41286AB28C9CCF5FC86`。

### Pending

- 同一 raw recording 的同步 QGC RTSP/Fly UI 视觉联调（`E4 INTEGRATED`）。
- PX4 SITL + Gazebo 视觉闭环、PX4 Mission 回读/hash 事务、固定翼/VTOL planner、
  HIL 与受限真实飞行。

## [0.3.0-pre.1] - 2026-08-25

### Added

- 产品 prerelease 版本清单和多平台版本映射。
- `RuntimeMode`、运行描述、统一 Clock 与 RuntimeBackend 契约。
- 自主任务文档、canonical hash、Scenario Manifest 和 Ground Truth 类型隔离基础。
- 显式 `ObservationReplayBackend` 命名和 pre-detection `RawSensorRuntimeBackend` 契约；旧
  `ReplayBackend`/`RuntimeBackend` 名称保持兼容。
- 签名运行环境状态以及同一个 MultiDetectGCS Fly UI 的环境标识。
- 同核模拟器架构与分层验收文档。
- Windows 构建 wrapper 自动定位 `patch.exe`、绑定 MSVC `ml64.exe`，并通过 batch `call`
  保证 `qt-cmake` 返回后继续执行自检、安装和 portable 部署。

### Verified

- `E2 TESTED`：根目录全量 `1260 tests collected / exit 0`。
- `E2 TESTED`：Runtime/Contracts/Scenario/ObservationReplay/RawSensor 边界与版本链专项
  `61 passed`（产品版本专项 `13 passed`），RuntimeStatus/operator 协议专项 `92 passed`，QGC
  custom + offline 静态回归 `61 passed`。
- Ruff、Python `compileall`、仓库 10 个 PowerShell 脚本 AST、仓库级 `git diff --check` 与
  Markdown 本地链接解析通过。
- `multi_detect-0.3.0rc1-py3-none-any.whl` 已构建，并在隔离 venv 中验证 metadata、
  `__version__` 与产品 release label。
- 以上只证明当前源码的单元、契约和静态边界；不证明原生 Windows 构建、完整 UI launcher、
  raw-sensor same-detector Replay、Gazebo、HIL 或实飞。
- `E3 BUILT`：2623-step Ninja 构建图完成 Release 链接，wrapper 完成 install/windeployqt 并以
  退出码 `0` 结束；两个 C++ 协议自检均退出 `0`。
- `E3 BUILT`：portable 主 EXE 位于
  `build/windows-v0.3.0-pre.1-release/portable-v0.3.0-pre.1/bin/MultiDetectGCS.exe`，大小
  `46,525,440` bytes，SHA-256
  `83BBAB5BD4D0C560E07DF1BBE79DE02F7815921DF2833524A598C74087F7349B`；完整依赖闭包存在，
  offscreen 启动存活 `15 s`、stderr `0 bytes`。

### Pending

- PX4 SITL + Gazebo 视觉闭环。
- RawSensorReplayBackend、完整 `LiveMissionRunner` Clock 注入、same-detector 集成 Replay launcher。
- PX4 Mission 上传后回读与 hash 事务。
- 固定翼任务 planner 和着陆闭环。
- VTOL 前/后转换与垂直起降闭环。
- HIL 与受限真实飞行。

## [0.2.0] - verified baseline

### Verified

- Windows Release 和完整便携运行目录已在迁移后的台式机验证。
- 历史构建证据、路径、SHA-256 和硬件验收边界保留在根 README 与
  `docs/qgc-windows-release.md`，不会被 prerelease 文档覆盖。

- VTOL vehicle-dynamics SIM 子门：新增 `scripts/vtol_sitl_scenario_acceptance.py` 与 `scripts/run_px4_gazebo_vtol_scenario_acceptance.ps1`，使用 digest 固定的 `standard_vtol` Gazebo/PX4 SITL，真实验证 GPS、ARM、VTOL→fixed-wing→multicopter 转换、DISARM 和无误定位；结果 `artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json`，SHA-256 `2E82C2453F2D7B28815235E02B06C793366782112E8329C0B679DC1EF01ECF6E`，容器日志 SHA-256 `022EB07077DEB3216966C5BD5C21C4CCDFB8B8942A4EB381AC386D06C7247E88`。该结果只闭合 VTOL 车辆子门，`operator_visual_acceptance=not_claimed`，不替代完整 Mission/Fly View/HIL/REAL。
- 机器证据清单 `build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` 已追加 `e4_vtol_sitl_scenario`，更新后 SHA-256 为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

## v0.3.0-pre.3 重构补充记录（2026-08-26）

### Changed

- QGC 界面收口到原生 `FlyView` / `PipView` / `FlightDisplayViewVideo`：不创建第二窗口，
  不维护自绘 FixedWing HUD。PiP 只保留只读 annotations；完整目标交互只在 full view 开放。
- 自定义层继续使用 `QGCPopupDialog`、`QGCSlider`、`QGCToolInsets`、`QGCButton`、
  `QGCLabel`、`QGCPalette` 与 `ScreenTools`，避免引入与 QGC 不一致的独立样式。
- E4 canonical frame order 明确为：实际 RTSP `frame_accepted` → signed type 23 send →
  native-controller state adoption → authenticated matching type 24 post-adoption ACK。
- RTSP publisher 改为 media/appsrc generation 生命周期。首帧前或流中发生正常 QGC
  disconnect/reconnect 时可以等待新 generation；旧 media cleanup 不得清除新 appsrc。
  同一 wire frame 跨 generation 重试仍只在实际 push 成功后计一次 `frame_accepted`。
- `REPLAY` / `SIM` 继续使用与 `HIL` / `REAL` 相同的状态模型、核心算法和 QGC UI；模拟器只替换
  输入 channel、动力学/传感器和时钟。

### Evidence boundary

- type 24 仅证明 QGC native controller 已采纳对应 type 23 状态；它不证明 QML render、操作者视觉验收
  或 runtime-mode policy acceptance。
- `operator_visual_acceptance=not_claimed` 继续是强制边界；不能用 transport ACK、源码测试、
  headless runner 或截图文件存在性代替人工 Fly View 点验。
- 本补充不声明 combined 300-frame 运行已通过。完整 REPLAY/SIM、PX4 Mission、HIL、REAL 与
  户外 GNSS gates 尚未全部闭合，最终 `v0.3.0` 仍不可发布。

### Build handoff

```powershell
cd D:\Projects\Multi-Detect
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_multidetect_windows.ps1 `
  -SkipConfigure -Configuration Release -Parallel 2 `
  -BuildSelfTests -InstallPortable
```

本轮 reconnect/type24 重构后的最终 EXE 大小与 SHA-256 尚待统一 portable rebuild。本文档前部保留的
pre.3 数值属于更早中间快照，不应作为本轮最终制品身份；权威值必须由最终构建生成的
`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` 给出。
预期制品路径为：

```text
build/windows-v0.3.0-pre.3-release/Release/MultiDetectGCS.exe
build/windows-v0.3.0-pre.3-release/portable-v0.3.0-pre.3/bin/MultiDetectGCS.exe
build/windows-v0.3.0-pre.3-release/portable-v0.3.0-pre.3/bin/MultiDetectReplayRtspPublisher.exe
```

## v0.3.0-pre.3 权威重建与 combined gate 补充（2026-08-26）

### Built

- 历史 pre.3 portable `MultiDetectGCS.exe` 为 `46,561,280` bytes，SHA-256
  `4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE`。
- 历史 pre.3 portable helper `MultiDetectReplayRtspPublisher.exe` 为 `79,872` bytes，SHA-256
  `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`。
- 这些值取代本文件前部 pre.3 中间快照和“待重建”状态，但不覆盖 pre.1/pre.2 历史记录。

### Integrated

- `artifacts/evaluation/combined-sim-qgc-20260826T013505Z-64000/combined-evidence.json`
  记录 `gate_passed=true`；文件为 `87,623` bytes，SHA-256
  `6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA`。
- Gazebo/RTSP 为 `300/300` 帧，helper exit `0`、EOS 和 summary 完整；signed type 23 sequence
  `1..300` 连续，QGC native post-adoption signed type 24 sequence `1..300` 连续。
- PX4 完成 ARM、multicopter→fixed-wing→multicopter、DISARM；GPS `qualified=true`、fix type `3`、
  `10` 星、`506` samples、最大跳变 `0.011 m`、无 false geolocation。
- UDP `14550` 前后未改变；所有测试端口释放，任务 container 已停止并删除。

### Native PiP evidence and boundary

- exact QGC PID/HWND 为 `40048 / 0x1C534FC`。`WindowFromPoint(286,937)` 命中同一 HWND/PID 后，
  orchestrator 才通过 `SendInput` 执行普通 PiP click。
- `QGCQml/MainFlyWindowIsMap` 从 `true(default)` 变为显式 `false`；QSettings 点击前/后 UTC mtime
  分别为 `2026-08-26T01:35:38.3155174Z` / `2026-08-26T01:35:40.4183819Z`，SHA-256 分别为
  `BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578` /
  `764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8`。
- 普通 PiP swap 不重启 video receiver；300-frame transport/metadata ACK 链保持连续。
- 截图只计 machine smoke；GPU-backed surface 经 `PrintWindow` 可能为黑面，故继续保持
  `operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

### Still pending

最终 `v0.3.0` 仍需真实 HIL、REAL、室外相机/GNSS 硬件、完整 PX4 Mission 与人工视觉验收。

## v0.3.0-pre.3 唯一权威 combined run 刷新（2026-08-26T02:08Z）

- 当时唯一成功 run（历史 pre.3）更新为
  `artifacts/evaluation/combined-sim-qgc-20260826T020829Z-57624`；`combined-evidence.json` 为
  `87,530` bytes、SHA-256 `62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F`、
  `gate_passed=true`。
- Release/portable QGC 为 `46,561,280` bytes、SHA-256
  `7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D`；RTSP helper 为
  `79,872` bytes、SHA-256 `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`。
  更早 `T013505Z-64000` / `4C3584F9...` 保留为历史 pre.3 记录，不再是当前制品身份。
- combined launcher 通过 `Remove-Item Env:` 真正删除
  `MULTIDETECT_DISABLE_VEHICLE_LINK`、`MULTIDETECT_HIL_UI_AUTO_EXERCISE`、
  `MULTIDETECT_HIL_UI_AUTO_EXIT`，而不是赋空字符串或 `0`；结果
  `acceptance_only_environment_absent_before_launch=true`（`env_absent=true`）、
  `hil_ui_auto_exercise_log_count=0`、`hil_ui_auto_exercise_disabled=true`。
- 300-frame sensor timeout 按 frame budget 动态变为 `180 s`。历史失败 run
  `combined-sim-qgc-20260826T020108Z-52536` 是固定 `100 s` timeout 导致的失败重试，
  `gate_passed=false`，不属于成功证据。
- 该历史 pre.3 run 的 300 个 type 23/type 24 sequence 全连续，helper exit `0` 且 EOS/summary 完整；
  QGC PiP swap 使用该历史 run 的 exact PID/HWND `55056 / 0x18D20DE`，普通 swap 不重启视频。
- 截图语义检查显示 badge `环境 SIM · HEALTHY`，但截图仍只属于 machine visual smoke；GPU surface
  可在 `PrintWindow` 中显示黑面，因此继续保持 `operator_visual_acceptance=not_claimed`、
  `qml_rendering_proved=false`。
- PX4 fixed-wing transition/return、GPS qualified、UDP `14550` 不变、端口释放和 container 删除均通过；
  最终 `v0.3.0` 仍需真实 HIL/REAL、室外硬件、完整 PX4 Mission 与人工视觉验收。

## v0.3.0-pre.7 - 2026-08-26

### Build

- 完成 Windows Release + portable 构建；`MultiDetectGCS.exe` 为 `46,551,552` bytes，Release 与
  portable SHA-256 均为 `B24754D7751521632C3C1250D69B4476CD13B8A6DCA09248A79B26CCD93973EA`。
- `MultiDetectReplayRtspPublisher.exe` 为 `79,872` bytes，SHA-256 为
  `769C4B0AD3DEEAE7398927B9159296D0C2172350F218663F632D5F6689B01D9D`。
- Python 全量测试、ruff、compileall、QGC 原生 UI 静态检查、Operator/DepthGrid/RTSP wire self-test
  通过。

### Native QGC UI

- 继续只在上游 QGC `FlyView`、`FlyViewVideo`、`PipState`、`FlightDisplayViewVideo`、toolbar 和
  map surface 上增量挂载 Multi-Detect；不引入独立主题、`Window`、第二视频输出或 simulator 页面。
- `capture_qgc_native_style.ps1` 成功完成 exact-client QGC desktop smoke；截图和 observation 已保存
  到 `build/qgc-native-style-pre.7-20260827-034544/`。该结果是 machine smoke，不替代人工视觉验收。

### Boundary

- 当时 manifest 仍为 `0.3.0-pre.7` (historical snapshot)；真实 HIL/REAL、室外 GNSS/相机硬件和最终人工 Fly View 验收仍是
  final `v0.3.0` 的阻塞门。
- Bound `scripts/vtol_selected_task_campaign.py` and `scripts/vtol_sitl_mission_acceptance.py` into
  the PX4 Mission release-parity contract. Refreshed the observed-session and shared-policy campaign
  receipts against the new parity identity; this does not promote the candidate or claim HIL/REAL,
  outdoor GNSS, hardware, or final-release acceptance.
# v0.3.0-pre.8 current field-link audit rebinding (2026-09-27)

- Added an optional current field-link preflight binding to
  `scripts/create_current_release_gate_audit.py` while preserving the historical
  field-link evidence embedded in the immutable release-gate report.
- Updated `verify_current_progress_index.py` to verify both bindings: the current
  read-only workstation probe and the historical report-internal probe. This keeps
  the release gate fail-closed without presenting stale field status as current.
- Rebound `docs/current-progress.json` and `docs/current-progress.md` to the
  2026-09-27 interface-bound preflight. The hardware route remains unavailable;
  no HIL/REAL/GNSS or final-release claim is made.
- 重新执行项目定义网段 `192.168.144.0/24` 的只读现场预检：
  `artifacts/evaluation/field-link-preflight-current-20260927-strict.json`（11,298 bytes，SHA-256
  `91D244BC9B769512D514FC3DB47C47C00404FBC06C3808D7EAB3D75B3F02439C`），绑定接口快照
  `artifacts/evaluation/field-link-interface-diagnostic-current-20260927.json`（4,379 bytes，SHA-256
  `67EE1B4DF6F08623763138AB6E08ED0E8BE4D4AB4578677553330551AE247D4B`）。当前 `Ethernet` 仍为
  `192.168.1.13/24`，目标网段无候选接口，Jetson/GR01/RTSP 均不可达，`hardware_link_ready=false`
  保持；新鲜证据已绑定到 `shared-policy-release-parity-drift-20260927-r15.json`。
- 保持现场 PowerShell wrapper 对网卡/路由对象严格 fail-closed；本轮使用项目定义网段的只读接口快照
  和直接 Python 预检保存证据，没有修改网卡、路由、DNS、设备或飞控状态。
- 按当前共享 planner/episode 重新运行多步任务精确参考：训练/验证/测试 9 案例贪心/参考 reward
  为 `89/90`，只有 1 个案例以增加预计耗时取得额外 reward，同贪心耗时上限没有提升；独立留出
  12 案例贪心/参考均为 `120/120`，改进案例和同耗时改进均为 `0`。因此没有把未经证明的
  多步参考或 learned weights 接入生产 SharedTaskPolicy，也没有宣称物理、HIL/REAL 或最终发布收益。
## 2026-09-27

- Rebound the prerelease release-gate diagnostic to the current `0.3.0-pre.8` parity after regenerating the external-runtime/AI join, the three-airframe PX4 reported-position response matrix, and the nine-case response campaign. The current report-only gate is `artifacts/evaluation/release-gate-current-20260927-r9/`; audit `docs/evidence/shared-policy-release-parity-drift-20260927-r20.json` verifies all four mode parity records are current while preserving the historical field-link snapshot separately from the current read-only probe. The gate remains fail-closed with 32 blockers: replay-set deployment coverage, the unavailable aircraft-subnet field link, QGC/Mission human acceptance, HIL/REAL hardware, and final release channel requirements remain unresolved.
- Refreshed the read-only field-link preflight as `artifacts/evaluation/field-link-preflight-current-20260927-r2.json` with a new interface diagnostic. The workstation still exposes only `192.168.1.13/24`; no `192.168.144.0/24` interface or Jetson/GR01/RTSP endpoint is reachable, so `hardware_link_ready=false` remains the correct result.
- Final repository validation after the rebinding passed `5081 passed, 7 skipped, 0 failed`; Ruff and `git diff --check` passed, and no disposable fault-injection containers remained. This confirms repository consistency only; it does not open QGC visual, HIL, REAL, outdoor GNSS, hardware, or final `0.3.0` release gates.
- 2026-09-27：从既有 PX4/Gazebo 原始 source-bound 记录重算并验证三机型 response matrix、6 次正向+9 次故障 campaign 与三机 runtime+AI join，全部重新绑定当前 parity（`terminal-continuation-v181`）。该步骤没有伪称新 planner 已经完成新的外部运行；新外部 PX4/Gazebo 重跑、HIL/REAL、室外 GNSS、硬件和最终 `0.3.0` 发布仍未完成。
- 2026-09-27：用当前 directional-wind planner 在固定 digest disposable PX4/Gazebo VTOL campaign 中完成 2/2 sorties；同一 SharedTaskPolicy/Mission 生命周期的 planner revalidation、Mission journal/hash-chain、commit/permit 和 exact-ID cleanup 全部通过，证据 `planner-external-rerun-20260927-r1.json`。这不是三机型完整 matrix，也不是 HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布。
- 2026-09-27：report-only release-gate 已重绑到 audit `r25` 与 `release-gate-current-20260927-r12`；四个 mode parity 当前一致，仍保留 32 个 fail-closed 阻断。当前 directional-wind planner 的 VTOL 外部 campaign 2/2 已通过，但三机型完整 external matrix、QGC 视觉、HIL/REAL、室外 GNSS、真实硬件和最终 `0.3.0` 仍未完成。
- 2026-09-27：重新执行当前共享策略 observed session `r17`，不再沿用旧 ranker/artifact 的观测记录。
  新证据 `artifacts/evaluation/shared-policy-observed-session-20260927-r17.json`（4343 bytes，
  SHA-256=`440ebd946d67857d2a345b5bcdc8de732ff58b32781798a3d7d0f1618dbbfc89`）使用当前
  `vtol-shared-policy-routes.synthetic-campaign.json`、VTOL profile-bound artifact 和 canonical
  ranker，按 `synthetic-east → synthetic-west` 完成两项任务。当前进度 verifier 现在会重新校验
  evidence/campaign/artifact/parity 文件身份、PolicyArtifact canonical hash、session canonical hash、
  每步 planner/ranker/outcome 绑定及 truth-free/no backend/no MAVLink/no actuator 字段；对应定向
  回归通过。该项仍是软件 observed-session 证据，不构成 external PX4/Gazebo、HIL、REAL、室外 GNSS、
  实飞或最终 `0.3.0` 发布证据。
- 2026-09-27：当前工作区完成带 JUnit 归档的全量 Python 回归：
  `artifacts/evaluation/full-regression-shared-policy-final-20260927/pytest.xml`，总计 5135 个
  tests，`5128 passed / 7 skipped / 0 failed`，退出码 0，耗时 348.29 秒，XML SHA-256=
  `bbebd926aab571b3cc264076387b2138d025706b218ca02a3d80f50c1894b2dc`。该回归覆盖当前
  RuntimeMode/Clock/Backend、Scenario、Mission、SharedTaskPolicy、external adapter 和进度索引；
  仅证明软件回归，不升级 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布状态。
## 2026-09-27 — SharedTaskPolicy decision identity and current parity rebinding

- `src/multidetect/autonomy/task_session.py` now fail-closes when a shared-policy
  decision is internally inconsistent: the selected task, `PolicyAction`, policy
  artifact canonical hash, truth-free marker, and planner revalidation marker must
  all agree before the observed session can continue. Added regression coverage for
  decision-identity drift.
- Regenerated `configs/release-parity.json` after the core change and kept the old
  evidence immutable. The current index now points to the new observed session `r18`,
  runtime matrix `r4`, vehicle matrix `r3`, external Mission matrix `r7`, and the
  decision-identity release audit. Rebound evidence only updates source identity
  metadata; it does not claim a new HIL, REAL, outdoor-GNSS, hardware, or flight run.
- Current verifier result: `passed`; product remains `0.3.0-pre.8`,
  `release_allowed=false`, `candidate_promoted=false`, and final `0.3.0` is not released.
- Full regression after the fixture migration passed: `5129 passed / 7 skipped / 0 failed`
  across 5136 tests; JUnit is recorded at
  `artifacts/evaluation/full-regression-shared-policy-decision-identity-20260927-r2/pytest.xml`.
- Rebound the prerelease release-gate wrapper to the current parity: the diagnostic-bound 2026-09-28
  field preflight, the current external runtime/AI join, and newly regenerated v182 PX4 response
  matrix/campaign. A real report-only wrapper run completed successfully; parity mismatches dropped to
  zero and the current audit records 32 blockers. Hardware link, replay review, QGC visual/Mission,
  HIL/REAL and final-channel gates remain fail-closed.
- Rebound the no-argument wrapper once more to the current positive-strata replay fixture and generated
  report-only r6 plus audit r5. The fixture remains `synthetic_demo`, so replay deployment-domain and
  review gates are still intentionally closed; no hardware, HIL/REAL or release claim was added.
- Rebound the authoritative report-only run to a historical field-link snapshot while the audit keeps
  the current r2 read-only probe. This preserves distinct historical/current evidence hashes and keeps
  the aircraft-subnet and hardware readiness gates fail-closed.
- 2026-09-28：将只读 `TargetEvidenceCard` 接入 integrated SIM/QGC 目标定位 acceptance，
  并让 combined-SIM verifier 重新检查卡片 schema、只读 claims、计数/错误列表和
  `run_identity_hash` 绑定。卡片仍只用于态势感知与回放记录，不进入 Mission 或 actuator；
  `v0.3.0-pre.8`、HIL/REAL、室外 GNSS 和最终发布门状态不变。
- 2026-09-28：修复 combined PX4/Gazebo/QGC 验收脚本的两个真实运行时问题：显式绑定容器内
  `PX4_MAVLINK_UDP_PORT`，并为共享 planner 传入带 provenance 的 synthetic SIM 能量模型；同时
  将 QGC 工具栏 DPI 线程上下文改为原始上下文记录、同线程恢复与有限重试。修复后外部运行已通过
  Docker/PX4/Gazebo、QGC 工具栏/视频、Mission 事务和能量连续性门；当前河流场景仍因
  `terrain_uncertainty_unbounded` 正确拒绝目标落图，未生成假坐标，故不构成外部组合 gate、HIL/REAL、
  室外 GNSS、硬件或最终 `0.3.0` 发布证据。

- 2026-09-28：能量 replay 对同一 PX4 `GLOBAL_POSITION_INT.time_boot_ms` 的重复样本做零时长去重，
  保持时间回退和覆盖缺口 fail-closed；新增回归测试覆盖重复时间戳，不改变能量模型或飞控授权边界。
- 2026-09-30：Gazebo depth contract 补齐距离语义门禁：输入必须显式声明 `ray_slant_m` 或
  `optical_axis_z_m`；光轴 Z 深度只有在 `fx/fy/cx/cy` 完整绑定时才会按像素射线换算为斜距，
  输出 sample 统一锁定为 `ray_slant_m`。新增离轴转换、缺失内参和 schema 缺字段回归；仍未接通
  真实 Gazebo depth topic，不提升 external SIM、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 门。
- 2026-09-30：接入仓库自有 Gazebo depth transport。x500、standard_vtol、rc_cessna 模型新增与
  RGB 同位姿的 `R_FLOAT32` depth camera；helper 在原 `MDGZSIM3` 后追加 hash-bound `MDGZDEP1`
  companion，严格绑定 RGB sequence/stamp/whole-record SHA、精确 scoped topic、完整校准 SHA、
  registration/coordinate-space/distance semantics 和不超过 100 ms 的因果时间偏差。主机
  `GazeboSimFrameSource` 按 frame_id 暂存 side-channel，`LiveMissionRunner` 接入同一个
  `MultiModalRangingEngine`，缺 companion、错绑定、坏 hash、过期 source 或不足有效像素均
  fail-closed。71 条 depth/helper/backend 回归通过；随后已完成 1 帧真实 PX4/Gazebo depth
  transport 运行，详见 `artifacts/evaluation/depth-external-20260930.json`；这不是完整 Mission、
  HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布通过。
## 2026-09-30

- VTOL 外部组合深度链路补齐：`river_rescue_vtol_001` 显式绑定 `front-depth`，场景 channel
  canonical SHA-256 更新为 `582e05422fdac8e5934d80f92a5e77d080dfee39c6e1d65221e4ff4593dba5d4`；
  combined readiness 现在同时检查 RGB/depth image + CameraInfo、NavSat 和完整 odometry
  publisher，integrated evidence 要求每个 RGB frame 都有认证的 `MDGZDEP1` companion。
- 目标定位 gate 接入现有 `gazebo_depth` direct measurement，但只接受 topic、calibration ID/SHA、
  registered-to-RGB、pixel alignment、FLOAT32 米制和完整 companion 数量全部一致的 transport；
  live audit 中嵌套 GPS quality/sample 会绑定到证据卡，卡片不可用时 fail-closed。
- terrain `terrain_uncertainty_unbounded` 仅在有新鲜 direct depth 时转入既有 degraded advisory
  solver；stale、horizon、direct inconsistency 等其它 INVALID 原因不放宽。新增 backend、combined
  script、集成 CLI、证据卡和 LiveMissionRunner 回归。完整 VTOL combined 外部 rerun 尚未执行，
  不提升 HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。
- 2026-10-01：基于当前 Gazebo 模型资产重新执行 fixed-wing 与 multirotor 的 same-container
  external PX4/Gazebo 深度 Mission 验收。两条链均复用 `SharedTaskPolicy`、planner revalidation、
  PX4 Mission transaction 和 2/2 RGB + 2/2 `MDGZDEP1` companion；新源证据为
  `artifacts/evaluation/external-fixed-wing-depth-shared-policy-20261001-r10.json` 与
  `artifacts/evaluation/external-multirotor-depth-shared-policy-20261001-r10.json`，当前索引绑定
  r11 不可覆盖副本。重新绑定的模型资产聚合 SHA-256 为
  `53c3db017228b3db4e8afd3b78706fda92fa8ee519e83a769729363a39d5e365`；旧 r7 证据降为历史，避免
  旧模型哈希继续被当作当前验收依据。

- 2026-10-01：`river_rescue_vtol_001` headless external VTOL response profile r5 在 700 s
  有界超时内通过 Mission、sensor、journal、cleanup 和 artifact integrity gates；Mission reached
  seq `0..6`，确认 fixed-wing/multicopter/landed/terminal disarm，最大距 home `275.388 m`、
  最大相对高度 `25.791 m`。深度 manifest content/provenance binding 同时通过。该证据不包含
  QGC 人工视觉、HIL/REAL、室外 GNSS 或硬件性能结论，最终 `0.3.0` 仍未发布。

- 2026-10-01：将 Mission timeout 合法范围上限从 600 s 提升到 700 s，并修复
  `GazeboSimFrameProvenance.depth_geometry` 的 tuple/list canonicalization，使内存 provenance
  与 JSON manifest 重载后的表示一致。parity 更新为
  `ec1f0daf58feb47df02051599f98b949d670718d7ca7363fd500c2a5cf6f51b4`；current-progress verifier
  与相关定向回归通过。
## 2026-10-01

- Added `verify_outdoor_depth_calibration_report.py` and an opt-in Jetson launch gate that
  binds a validation report to the exact calibration-document SHA-256. Strict deployment mode
  rejects advisory, stale, or hash-mismatched reports; it does not claim field/GNSS/HIL/REAL
  evidence and does not mutate calibration files.
## 2026-10-01

- Ran the predeclared heterogeneous development ranker experiment against the independent holdout.
  The 36-point coefficient search produced the same parent candidate; train/validation/test stayed
  at 40/40/40 and the 12-input holdout stayed at total reward 120 for both. The result is recorded
  as a no-gain software planning experiment and does not promote a model or alter HIL/REAL, outdoor
  GNSS, hardware, or final-release gates.

- Hardened `verify_current_progress_index.py` to re-open the current training,
  independent-holdout, candidate-ranker, trained-artifact external-SIM, campaign-summary,
  receipt, cleanup, and release-parity identities. Added positive binding and tamper-negative
  regression coverage; this strengthens evidence auditability without changing the fail-closed
  HIL/REAL, outdoor-GNSS, hardware, or final-release boundaries.

- Bound the trained candidate policy artifact to the current VTOL airframe identity and executed it
  through the existing external PX4/Gazebo two-task Mission campaign. Both sorties completed in the
  same telemetry session with planner revalidation, artifact identity, transaction journals, graceful
  router stop, and exact-ID cleanup. This proves model/artifact path reuse in software external SIM;
  it does not promote the candidate or claim HIL/REAL, outdoor GNSS, hardware, or final release.
  Evidence is retained under `artifacts/evaluation/shared-policy-trained-vtol-two-task-20261001-r1*`.

- Re-ran the shared task-ranker training path and a frozen-candidate independent holdout campaign.
  Training/validation/test rewards remained equal to the parent (26/28/35); the 12-input independent
  holdout also remained equal (total reward 120 for both parent and candidate). Input hashes and
  scenario families were disjoint, planner revalidation passed, and no candidate promotion or
  physical/HIL/REAL/outdoor-GNSS claim was made. Evidence is retained under
  `artifacts/evaluation/task-ranker-training-20261001-r1/` and
  `artifacts/evaluation/shared-policy-independent-sim-20261001-r1.json`.
- 2026-10-01：将任务序列 DFS 参考搜索提取为 `src/multidetect/autonomy/task_sequence_reference.py`，
  并让 `scripts/benchmark_task_sequences.py` 委托该库函数。参考目标显式绑定为
  `sum_priority_plus_one_then_count_then_minus_modeled_time_v1`，沿用原 planner、RETURN 约束、预算
  episode 与 Mission compiler；输入最多 6 项，节点超限 fail-closed。新增顺序不变性、GPS/runtime
  拒绝、任务上限和无 actuator 声明回归。该项仍是开发诊断，未接入 `SharedTaskPolicy`/PX4 campaign，
  不打开 learned model、物理、HIL/REAL、室外 GNSS、硬件或最终 `0.3.0` 发布门。

- 2026-10-01：按当前显式 source list 重新生成 `configs/release-parity.json` 并同步当前索引哈希，
  修正静态 parity 漂移。历史 external Mission 证据保持原样；当其源码身份无法匹配时 verifier
  继续 fail-closed，不把静态重算当作新的 PX4/Gazebo、HIL/REAL 或硬件运行。

- 2026-10-01：在冻结 independent holdout 上重跑库化序列参考器；12 个输入 greedy/reference 为
  `120/120`，改进案例 `0`，同贪心耗时上限改进 `0`。没有序列策略晋升或外部运行声明，继续使用
  现有 `SharedTaskPolicy`/PX4 路径。
- 2026-10-01：在当前 `0.3.0-pre.8` parity 下重新执行 VTOL SharedTaskPolicy 双任务 external
  PX4/Gazebo campaign（r31）。`synthetic-east` 与 `synthetic-west` 均完成，Mission
  upload/readback/commit/permit、planner revalidation、journal/hash-chain、受限 evaluator、
  loopback isolation 与 exact-ID cleanup 全部通过；live GPS 启动抖动的第一次失败也保留为独立
  失败证据，没有被覆盖。随后以 r31 receipt 生成三机型 Mission 与 vehicle-policy matrix r32。
  该结果仍是软件 external SIM；未宣称相机帧采集、HIL/REAL、室外 GNSS、真实硬件或最终
  `0.3.0` 发布。
- 2026-10-02：为通用 advisor 上下文补齐向后兼容的 `ContextSnapshot.airframe_profile_hash`。
  训练数据 loader 会把 case profile hash 注入没有该字段的旧 v1 context；若 context 已携带 hash，
  strict sequence reference 和 evaluator 都要求它与 case/策略 hash 完全一致，不一致直接
  fail-closed。新增 source-bound holdout r6
  `artifacts/evaluation/sequence-proposal-independent-holdout-20261002-r6.json`（SHA-256=
  `6bfdde05e7b3e23b152a4191f6297eed325524eb44fb8bf886497c21a12c9ebd`），12 cases、reward
  `120/120`、objective tie `12`；advisor 仍不部署、不进入 PX4 Mission/actuator，不打开 HIL/REAL、
  室外 GNSS、硬件或最终 `0.3.0` 发布门。
- 2026-10-02：修复 geolocation sensor transport provenance 契约漂移。`selection_mapping()` 输出的
  `scenario_manifest_hash` 现在由 `geolocation_accuracy` verifier 作为可选 SHA-256 字段严格校验，
  解决 replay truth join 的误报失败；同步生成新的 source-bound parity transition r2，旧证据不覆盖。
  完整 pytest、Ruff、current-progress `--check-sources` 均通过。该改动不代表新增 PX4/Gazebo 运行，
  不打开 HIL/REAL、室外 GNSS、真实硬件或最终 `0.3.0` 发布门。
- 2026-10-02：收紧 QGC 目标落图的 GNSS fail-closed 门。实时地图点、1σ 圆、文字卡片和复制坐标
  现在都要求与测距/目标定位同一原子 GNSS 质量及身份绑定；弱 fix、过期、缺失或不一致状态只显示
  “不可定位”，不会落假点。新增 `targetGeolocationGnssQualified` 状态契约与静态回归；按当前
  UI parity 做了 source-bound evidence transition，但没有把它写成新的 PX4/Gazebo、HIL/REAL、
  硬件或室外 GNSS 运行证据。
# v0.3.0-pre.8 — 2026-10-02

- 重新生成当前 parity 绑定的 PX4 reported-position response matrix/campaign r27：复用保留的
  6 个正向 Docker/PX4/Gazebo response 与 9 个 fail-closed fault source，matrix/campaign verifier
  均通过；明确记录为 source-bound 重算，不是新增外部运行，也不升级为 HIL/REAL/硬件或室外 GNSS 证据。
- 使用 r27 matrix/campaign 与当前 external evidence 重新运行 `run_current_release_gate_pre8.ps1`，
  生成 report-only manifest/report；四个 mode 的 parity identity 已绑定一致，但 replay-set、field-link、
  人工视觉/Mission、HIL/REAL 与最终 release 门仍保持 fail-closed，`release_allowed=false`。
- `docs/current-progress.json`、`docs/current-progress.md`、`docs/release-gate.md` 和本日志已同步到
  `shared-policy-release-parity-drift-20261002-gnss-display-r5.json` 与新 report/hash；r5 显式绑定
  当前只读 field-link probe，修正了历史报告路径与 current-progress probe 的审计关系，仍不改变
  `hardware_link_ready=false` 或任何 HIL/REAL/室外 GNSS 结论。
- 2026-10-02（0.3.0-pre.8）：新增 `ContextSnapshot.airframe_profile_hash` 的
  `SharedTaskPolicy` fail-closed 校验；parity receipt builder 现在 hash-gate runtime snapshot
  的 bytes、文件 SHA、契约 hash 与 release label，stable-but-stale 运行只保留为 retained-file
  audit。补充 policy/AI stale snapshot 回归测试，重新生成 `context-profile-r6` source-bound
  evidence，并保持 release gate `gate_passed=false`、`release_allowed=false`；未宣称 HIL/REAL、
  室外 GNSS、真实硬件或最终 `0.3.0`。
- 2026-10-03：修正 simulator AI flight parity receipt schema 与当前保留证据的契约漂移。
  schema 允许已验证 receipt 显式携带 `release_parity.verified=true`（仍不降低
  required hash/identity 校验），并将 `docs/current-progress.json` 重新绑定到当前 schema
  的 8591 bytes / SHA-256=`b5d13385a54df95bc873b90971594f2372b48a81a5046b77b3c704fccbf0d0c1`。
  当前索引重算、schema 回归和全量 pytest 均通过；这不改变 `0.3.0-pre.8` 的最终发布、
  HIL/REAL、真实硬件或室外 GNSS 门状态。
- 2026-10-03：新增当前源码的 `v0.3.0-pre.1` foundation acceptance revalidation。入口
  `scripts/verify_v030_pre1_foundation.py` 将 RuntimeMode、共享 ManualClock、RuntimeBackend、
  Scenario Manifest runtime channel、truth-free task contract、profile 隔离及确定性 Replay/
  Simulator 组合成可重复 receipt；`docs/evidence/v030-pre1-foundation-20261003-r5.json`
  两次生成字节一致且 `gate_passed=true`。它不启动 transport、不发送 MAVLink、不授予 actuator，
  也不关闭 HIL/REAL、硬件、室外 GNSS、人工视觉或最终 `0.3.0` 发布门。
- 2026-10-03：将 `v030-pre1-foundation-20261003-r5.json` 接入总进度 verifier。现在每次
  `verify_current_progress_index.py --check-sources` 都会重算 foundation receipt 的文件/规范
  哈希、当前 parity、源文件哈希、八项基础检查和无 actuator/final-release 边界，并以
  `v030_pre1_foundation_bound=true` 返回；新增 source-drift 负向回归。该项仍不替代 HIL/REAL、
  真实硬件、室外 GNSS、QGC 人工视觉或最终 `0.3.0` 发布门。
- 2026-10-03：完成 `0.3.0-pre.8` 当前证据链收口。VTOL SharedTaskPolicy receipt
  `qml-r6` 重新绑定到 `20261001-r31` summary；三机 vehicle/mission matrix 更新为
  `qml-r7`；AI-flight retained-layer receipt 更新为 `qml-r6`；release-gate retained-file
  audit 更新为 `qml-r6` 并绑定最新只读 field-link probe。`verify_current_progress_index.py`
  full/source-only 与 98 项定向回归均通过；仍有 32 个 release blocker，未宣称 HIL、REAL、
  户外 GNSS、硬件或最终 `0.3.0` 发布。
- 2026-10-03：新增 Gazebo + 真实 MultiQGC + JetsonAI 同链路入口。组合脚本可将 Jetson
  等价 safety ONNX/manifest 接入现有生产感知工厂，并在证据中记录模型与 manifest SHA-256；
  保留 PX4 Mission、watchdog、permit、QGC FlyView 和容器清理生命周期。模型输出仅为
  evidence，不直接授权飞控；使用说明见 `docs/gazebo-qgc-jetson-ai-sim.md`。
# Unreleased (`0.3.0-pre.8`)

- Added fresh current-parity PX4/Gazebo evidence for the shared OnboardAI path:
  VTOL SharedTaskPolicy mission with same-container wire-v3 RGB/Depth capture,
  realtime AI flight response, and fixed-wing/multirotor depth Mission reruns.
- Preserved fail-closed evidence rules: retained-file parity rebinding cannot
  upgrade an old run into a current runtime claim; the original failed AI run
  remains recorded separately from the successful isolated-port rerun.
- Kept the final release blocked. No Jetson/Pixhawk hardware, HIL/REAL,
  outdoor-GNSS, QGC human visual, metric-distance or original PromptEnvelope
  provenance claim is opened by these software-SIM results.
﻿- 2026-10-04：将 `SharedTaskPolicy` 接入真实外部 VTOL campaign，保留旧 ranker 接口并复用
  PX4 Mission upload/readback/commit/permit/cleanup 生命周期；完成 decision、task、plan、
  executable Mission 和 policy artifact 的 hash 绑定。对应 301 个 VTOL 回归与 28 个共享策略
  场景测试通过。由于本次改变了 PX4 mission path parity，所有历史外部证据均以不可覆盖的
  retained-file audit 方式重新绑定；未宣称新一轮 PX4/Gazebo、HIL、REAL、Jetson、室外 GNSS
  或最终 `0.3.0` 发布。
- 2026-10-04：新增一次外部 PX4/Gazebo `flight_mode_loss` 负向 campaign。arm、target
  和 AI bridge 在故障前正常运行，飞行模式丢失被观察到，`fail_closed=true`，控制
  authority 进入 `failsafe`，容器 cleanup 通过。正常 flight gate 按设计拒绝，非零退出码
  是预期行为。摘要为 `docs/evidence/simulator-ai-flight-mode-loss-20261004.json`；
  它不代表正常 flight 通过、HIL、REAL、Jetson、户外 GNSS 或最终发布证据。
- 2026-10-04：新增外部 PX4/Gazebo `gps_quality_loss` 负向 campaign。故障触发后停止转发
  后续 `GPS_RAW_INT`，没有修改 PX4 数据或放宽 GPS 阈值；共享 AI 继续视觉 TRACK，但不
  把旧 GPS 样本当成新的定位结果。`fault_observed=true`、`fail_closed=false`、cleanup
  通过，正常 flight gate 按设计拒绝。摘要为
  `docs/evidence/simulator-ai-gps-quality-loss-20261004.json`；这不代表 GPS fail-safe、
  HIL、REAL、Jetson、户外 GNSS 或最终发布验收。
- 2026-10-04：完成外部 PX4/Gazebo “GPS 退化 + 返航请求”负向 campaign。GPS 退化后，
  返航请求通过 live bridge 控制面进入共享 executor；策略返回
  `failsafe`，原因是 `return_gps_quality_insufficient`，`fail_closed=true`，容器 cleanup
  通过。摘要为 `docs/evidence/simulator-ai-gps-return-failsafe-20261004.json`；这不代表
  HIL、REAL、Jetson、户外 GNSS 或最终发布验收。
- 2026-10-04：完成外部 PX4/Gazebo 低电量负向 campaign。注入 `battery_remaining=5` 后，
  shared executor 进入 `failsafe`，原因是 `home_vector_unavailable`，没有在缺少返航向量时
  盲目发返航控制，`fail_closed=true`，容器 cleanup 通过。摘要为
  `docs/evidence/simulator-ai-battery-low-failsafe-20261004.json`；这不代表真实电池、
  HIL、REAL、Jetson、Pixhawk 或最终发布验收。
# 2026-10-04 OnboardAI r3 continuous task chain

- Added the adapter-owned model navigation compiler: bounded N/E intent is
  projected with WGS-84 and revalidated through the existing planner and PX4
  multirotor Mission compiler. The adapter now requires matching compilation,
  plan, wire and Mission hashes before replacement.
- Added task-phase receipts for `search → match_target → record_location →
  report`. GPS recording is rejected unless fix quality, horizontal accuracy,
  target bearing and target range satisfy the shared gates.
- Trained and replayed `onboard-policy-r3` on the local GPU as a synthetic
  bootstrap artifact. The software-only evidence is recorded in
  `docs/evidence/onboard-policy-chain-20261004.json`; it is not Jetson,
  external Gazebo, HIL, REAL or outdoor-GNSS evidence.
- 2026-10-04：完成一轮 fresh pinned PX4/Gazebo 外部传感器 SIM。真实 disposable
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  容器启动并观察到 heartbeat/qualified GPS；3 帧 BGR24 RGB 与 3 帧 FLOAT32_M depth
  companion 持久化且 wire hash 对齐，容器 stop/remove 清理通过。该轮不请求 Mission、ARM
  或物理输出，仍不宣称 Jetson/Pixhawk、HIL/REAL、室外 GNSS、人工 QGC 或最终 `0.3.0`。
  证据：`artifacts/evaluation/onboard-gazebo-sensor-20261004-r1/acceptance.json`，
  SHA-256=`640dcce7a172519b06cc405ec59105f08950e9cc2b558caefcef3843d0b08e98`。

- 2026-10-04：收紧 OnboardAI 红车软件闭环。模型导航 callback 现在必须绑定选中的
  `TaskCandidate`，独立重编译 WGS-84/Planner/PX4 Mission 并核对 plan/wire/mission hash；
  任务阶段回执明确为 `search → match_target → record_location → return_home → report`，
  返航必须有独立 `MISSION_COMPLETE` 到家确认。当前 r3 仍是 synthetic bootstrap，目标颜色
  通过结构化感知 `target_match_score` 进入，不把文本细节或软件回放写成像素级/Jetson证据。
- 2026-10-04：在现有共享 OnboardAI 契约上扩展 r4 结构化目标属性输入。实时事件现在可以同时
  携带检测类别、颜色、类别匹配和颜色匹配结果；40 维模型输入把这些字段送入与 Gazebo/
  Jetson 共用的高层策略，任务回执也会保留同样证据。旧 38 维 r3 ONNX 仍可读取，便于分阶段
  部署。RTX 4060 synthetic bootstrap（12,000 samples / 120 epochs）和软件回放通过，完整
  阶段为 `search → match_target → record_location → return_home → report`；这不是像素精度、
  Jetson、Pixhawk 实机、HIL、REAL、室外 GNSS 或最终 `0.3.0` 发布证据。证据：
  `docs/evidence/onboard-policy-chain-20261004-r4.json`。
- 2026-10-05：重新生成三机型 Mission、共享 policy vehicle/runtime matrix、训练回执、sensor
  campaign 和 AI-flight parity 的 current-parity retained-file 审计副本，历史证据保持不变。
  `docs/evidence/external-mission-matrix-20261005-current-parity-audit-r1.json` 及其来源绑定
  通过 `scripts/verify_current_progress_index.py --check-sources`；这不是新的 Docker/PX4/Gazebo
  外部重跑，不增加 HIL、REAL、Jetson、室外 GNSS 或最终 `0.3.0` 证据。
- 2026-10-05：继续推进 Prompt → 实时 OnboardAI → PX4 Mission 的同一条 SIM/Jetson 共享链路。
  `OnboardFramePerceptionAdapter` 现在逐候选执行类别、颜色和 metric-depth 质量门，在当前观测窗口内
  对目标颜色候选按有效距离选择 `nearest_observed`，并用 IoU 与 1 秒时间窗约束两帧确认；Gazebo
  `optical_axis_z_m` 深度会先转换成 ray-slant。新增 `MissionTransactionDispatcher`，模型产生的
  `PlannedMissionCompilation` 可显式交给外部 PX4 upload/readback/commit/permit 适配器；新增
  `OnboardRealtimeModelBridge.submit_runtime_event()` 作为返航/完成遥测进入同一状态机的入口。
  任务状态机同时支持上游经过标定和姿态求解的 N/E 偏移来记录目标 GPS，避免把相机相对角误当绝对北向角。
  这些改动仍是 `0.3.0-pre.8`，默认不 arm、不启动飞行，未宣称 Jetson/Pixhawk 实机、HIL、REAL、室外 GNSS
  或最终 `0.3.0`。
- 2026-10-05：新增 `OnboardAutonomousSortie` 任务级执行器，把共享实时模型链串成一次无需
  操作员逐步确认的 Prompt 任务：合格起飞、绿车不匹配后的搜索重规划、红车确认、GPS
  位置记录、返航完成和最终报告。Gazebo/PX4/Jetson 通过注入的生命周期回调接管 Mission
  start、返航与报告，模型仍没有 MAVLink/arm/执行器权限。同步让 `OnboardFramePerceptionAdapter`
  在有合格机头航向时把深度距离和相机相对方位投影为 N/E 偏移；没有航向或距离时继续不
  生成坐标。新增软件回放证据 `docs/evidence/onboard-policy-chain-autonomous-20261005-r1.json`，
  仍明确为 software SIM，不代表外部自动飞行、Jetson、HIL、REAL、室外 GNSS 或最终发布。
 - 2026-10-05：完成红车任务外部自动生命周期 r28 的真实帧回归。PX4/Gazebo、GPS、ARM、起飞门、
  20 个 RGB/Depth 帧处理和 20 次 Mission 事务通过；7 个红车候选在约 22 m 处被颜色适配器
  判为 `red`，但 raw YOLO confidence 仅 `0.01–0.02`，低于可执行门 `0.25`，因此共享任务链
  保持 `SEARCH`，没有伪造 GPS、RTL 或 REPORT。新增证据摘要
  `docs/evidence/external-red-car-autonomous-20261005-r28.json`；该结果是外部软件 SIM，
  不代表 Jetson、Pixhawk 硬件、HIL、REAL、室外 GNSS 或最终 `0.3.0`。

 - 2026-10-05：将 `multidetect.autonomy` 的感知适配器改为惰性导出，修复
  `perception_factory → autonomy → multimodal_ranging` 的循环导入；新增搜索重规划置信度回归，
  允许低于通用重规划门的候选继续左右搜索，但仍禁止进入目标记录阶段。
- 2026-10-05：建立 Gazebo 3-D 主线资产 manifest/schema/verifier，绑定
  `x500_mono_cam` 多旋翼、`standard_vtol_mono_cam` VTOL、`rc_cessna_mono_cam` 固定翼三套
  实体载具组合，以及各自的 world、map、DEM、RGB/Depth/PX4 telemetry/airspeed 契约和
  SHA-256。离线 verifier 通过 3 个场景，明确排除 billboard、网络下载和 evaluator truth；
  该结果只是资产/契约审计，不是外部 Gazebo/PX4、Jetson、Pixhawk、HIL、REAL 或最终发布通过。
  摘要：`docs/evidence/gazebo-3d-mainline-manifest-20261005.json`。

- 2026-10-05：尝试将同一 r14 ONNX 接入 `urban_inspection_multirotor_001` 3-D 外部 PX4/Gazebo
  传感器入口。仓库 preflight 通过，但本机 Docker `container inspect` 在 10 秒超时，容器未确认
  创建、telemetry/传感器未开始；保留 blocked evidence，不宣称外部 3-D 或飞行通过。
  摘要：`docs/evidence/external-onboard-r14-urban-3d-20261005.json`。

- 2026-10-05：在现有 OnboardAI 40 维输入和任务/飞行动作头上继续增量训练
  `onboard-policy-r14-autonomy-transitions`，父模型为 r13，使用 temporal transition augmentation
  覆盖目标不匹配后的搜索、匹配跟随、位置记录返航、报告以及 GPS/电量/障碍/推理故障安全转移。
  RTX 4060 训练评估准确率为 task `0.9772115385`、flight `0.9757019231`；独立结构化 holdout
  7/7 通过，软件 Prompt→search→match→record→return→report 回放通过。Onboard ONNX runner
  现在允许健康事件在 SEARCH/INSPECT_TARGET 阶段提出 HOLD/RETURN/LAND，再由 adapter 安全监督器
  做最终 PX4 否决。该候选仍未 promotion；证据仍是 truth-free software replay，不代表 3D
  Gazebo 主线、Jetson、Pixhawk、HIL、REAL、室外 GNSS 或最终 `0.3.0`。
  摘要：`docs/evidence/onboard-policy-r14-autonomy-transitions-20261006.json`。
- 2026-10-05：OnboardAI 在现有模型契约上完成 r17 teacher 一致性增量训练。修正 class/color
  任一未知或不匹配时仍产生高 `target_match_score` 的 teacher 标签冲突；r17 从 r16 继续继承，
  保持同一 40 维输入、任务/飞行动作头和安全适配器。RTX 4060 评估 task/flight 准确率为
  `0.9932242991/0.9936137072`，独立 holdout `7/7`，22-case guarded 泛化 `22/22`，回归
  MAE/P95/clip rate 也已记录。模型 SHA-256=`7434d94fb676427cdf34086f92b42f57cec5ac467b7942e7b116ea5c8bfdbed3`，
  证据为 `docs/evidence/onboard-policy-r17-teacher-consistent-20261005.json`。仍是 RTX 4060
  合成/软件验证，不是 Jetson/Pixhawk、HIL/REAL、真实 3D Gazebo 或最终 `0.3.0` 发布。

- 2026-10-05：OnboardAI 在现有模型契约上完成 r15/r16 增量训练。新候选没有把红车/绿车
  当作训练主分布，而是加入类别/颜色/目标类型仅作元数据的参数化属性组合、未知属性搜索、
  健康事件和 TAKEOFF 阶段安全转移；输入仍为同一 40 维向量，输出仍为同一任务头、飞行动作
  头和运动回归头。r16 模型 SHA-256=`478b2abf0c68c271971b45a7f21b2ad7c60e6913122f5176f872d604b1cacd12`，
  task/flight 评估准确率为 `0.9906542056/0.9909657321`，独立 holdout `7/7`，泛化压力
  guarded `22/22`。同时修正高度回归解码为统一的 120 m 上限，并记录 north/east/altitude/speed
  的 MAE、P95 和 clip rate。证据为
  `docs/evidence/onboard-policy-r16-phase-safe-20261005.json`。这仍是 RTX 4060 合成/软件
  验证，不是 Jetson/Pixhawk、HIL/REAL、真实 3D Gazebo 或最终 `0.3.0` 发布。

- 2026-10-05：真正的 3D Gazebo 主线继续与红车/绿车感知夹具分离；实体 x500、VTOL、固定翼
  模型、world、RGB/Depth/GPS/里程计和动力学 manifest 审计仍保持通过。外部 3D 运行因本机
  Docker Linux engine/API 不可达而未启动，未把 timeout 写成 Gazebo 通过，也未重启 Docker。
- 2026-10-05：修正 OnboardAI 3-D 渲染训练链：RGB/Depth payload、wire-v3 Gazebo 来源、同帧
  telemetry 时间戳和 source split group 现在全部 fail-closed 校验；深度只在检测框区域参与目标距离，
  没有检测时保持未知。训练器改为按 source group 切分，重复帧不会跨 train/evaluation 泄漏。r23
  使用同一 r17 parent、RTX 4060 和低学习率完成 9 个 RGB+Depth 3-D 传感器帧的 plumbing 微调，
  但 detector hits=0、标签仍是启发式 plumbing，候选不 promotion。该结果不代表视觉泛化、Jetson、
  Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布。
- 2026-10-05：训练管线现在保留 reviewed Gazebo episode 的显式 `train/evaluation/test`
  分区；`test` episode 单独评估，永远不会进入 optimizer 或 evaluation。无声明 split 的
  synthetic/plumbing 数据仍按 source group 切分。trajectory 的 `group_id` 改为已校验的
  `episode_group_sha256`，并且 training-ready 数据要求 `materialized_sources=true`，缺少可复现
  RGB/Depth manifest 时 fail-closed。新增 15 项训练回归；r24 仍未 promotion，结构化 holdout
  不能证明像素级 Gazebo、Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。
- 2026-10-05：收紧真实 3-D OnboardAI 训练入口。`training_ready=true` 现在必须同时绑定
  可由 `RawSensorReplayRecording` 重开的完整 raw recording manifest、RGB/Depth manifest、
  连续 episode 和独立 `multidetect.onboard-trajectory-label-review-v1` receipt；receipt 的
  canonical JSONL 标签逐行绑定 context/event、task/flight/regression、planner/Mission hash
  和 train/evaluation/test 分区，禁止 Gazebo truth、world pose、model ID 与自标注。现有仓库
  3-D 资料仍不足以成为训练完成证据，版本保持 `0.3.0-pre.8`，不 promotion、不宣称 Jetson、
  Pixhawk、HIL/REAL 或室外 GNSS。
- 2026-10-05：收紧 OnboardAI 训练就绪门槛。训练回执、label manifest、raw recording manifest
  和五类 planner/Mission proof artifact 现在都必须在数据集目录内，并逐项用实际文件 SHA-256
  绑定；`../` 路径、目录外绝对路径、被篡改的 proof 文件都会 fail-closed。RGB payload 还必须
  与同一 raw replay frame 逐帧相等，避免把独立但不匹配的传感器文件拼成“连续 3-D 轨迹”。
  这只改善训练证据完整性，不把当前短时 3-D plumbing 数据升级为训练完成、Jetson/Pixhawk、
  HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。
- 2026-10-05：在现有 r25 权重上完成 OnboardAI r26 真实 3-D 场景契约增量训练。RTX 4060 使用
  三类 3-D vehicle/world 结构化场景和 9 个已绑定 RGB+Depth 传感器帧，仍共用 40 维输入、任务/飞行动作
  输出及安全适配器；渲染源只作为受控重复权重，不被写成视觉真值。2,100-case 独立结构化保持集为
  task=`0.746190`、flight=`0.779524`、compatible=`0.950000`。由于 detector hits=0、可用深度中值=0、
  没有审核连续轨迹和 pixel test，候选保持 quarantined，未替换参考模型、未 promotion，也不代表
  外部 Gazebo/PX4、Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。证据：
  `docs/evidence/onboard-policy-r26-gazebo3d-scene-20261005.json`。
- 2026-10-05：把 OnboardAI 训练推进到真正的 3-D Gazebo 数据入口。VTOL 任务采集器现在可在同一
  容器/同一任务屏障后保存配对 RGB wire-v3 与 `FLOAT32_M` 深度；`train_onboard_model.py` 新增
  `--require-real-3d-capture`，会对唯一连续帧、有效深度、多个实体场景、检测结果、raw recording
  和独立轨迹复核做 fail-closed 检查。当前 9 帧 plumbing 数据被正确拒绝，不再把颜色示例、结构化
  teacher rows 或重复权重当成真正的 3-D OnboardAI 训练。没有启动新的 Docker/Gazebo 外部运行，
  也没有改变候选隔离、Jetson/Pixhawk/HIL/REAL 和最终 `0.3.0` 发布边界。
# 0.3.0-pre.8 — 2026-10-05

- Gazebo 3-D 主线新增仓库自有的 `multidetect_search_target_v1` 实体目标模型，并绑定到
  urban / river / coastal 三个 world；模型使用中性结构与碰撞几何，不把红/绿颜色示例当任务答案。
- mainline manifest 和离线 verifier 现在校验目标模型、配置、include 名称、位姿与 SHA-256。
- 训练边界仍保持 fail-closed：实体模型资产已准备，但没有把它写成外部 Gazebo、Jetson、
  Pixhawk、HIL/REAL 或最终 `0.3.0` 验收证据。
- 2026-10-05：新增实体 `multidetect_search_target_v1` 3-D 模型并绑定三个主线 world；训练入口要求
  training-ready episode 绑定可重放录制与独立审核，渲染帧和轨迹帧不得共享 RGB payload，来源还要
  绑定 Gazebo world、载具和模型资产哈希。红车/绿车示例不再定义 Gazebo 场景或 OnboardAI 身份。
- 2026-10-06：将 OnboardAI 训练主线切换为中性 3-D 场景语义。r27 从现有 r26 同一套
  40 维输入、任务头、飞行动作头和安全适配器继续增量训练，在 RTX 4060 上完成 35,100
  行、45 epochs；3-D teacher 的颜色字段保持未知，目标确认改由类别/跟踪一致性、通用
  匹配分数、Depth 与实时事件决定，因此红车/绿车只保留为示例，不再作为 Gazebo 训练身份。
  r27 的结构化 evaluation task/flight=`0.873445/0.880355`，neutral 3-D holdout=`7/7`，
  通用独立 holdout=`7/7`，但 test split=`0`，所以仍为 quarantined development candidate，
  未替换参考模型、未 promotion。当前没有重新启动外部 Gazebo/PX4，尚无足量独立 RGB/Depth
  像素训练证据，也不代表 Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 发布。
  使用同一 ONNX runtime 的 22 个 truth-free generalization pressure case 中，原始模型头通过
  15/22，安全 runner 保护后通过 22/22，其中 7 个是安全屏蔽成功而不是模型学习；报告为
  `artifacts/evaluation/onboard-policy-r27-generalization-20261006.json`。
  证据：`docs/evidence/onboard-policy-r27-generic-3d-20261006.json`。
- 2026-10-06：在 r33 之上完成两轮同一 OnboardAI 契约的增量复核。r34 的结构化 3-D holdout
  提升到 task/flight/compatible=`0.983333/0.993333/0.996667`，但 `bearing-right` 方向压力集
  失败；r35 降低学习率后改为 `bearing-left` 失败。两轮均未 promotion，当前参考保持左右方向
  都通过的 r33。比较证据：`docs/evidence/onboard-policy-r34-r35-rejection-20261006.json`。
  这些仍不是像素级 Gazebo 视觉、Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。
- 2026-10-06：继续训练同一套 OnboardAI，新增从主线实体 SDF 提取结构几何统计的 3-D 教师分布。
  RTX 4060 完成 r38/r39/r40 三轮候选；结构化 holdout 虽提高，但左右 bearing 通用压力集不稳定，
  r40 还损伤 evaluation flight accuracy，因此全部拒绝，当前引用仍保留 r33。训练没有使用红车/绿车
  颜色示例、world truth 或第二套模型；没有把结构化教师行写成 Gazebo 像素级成功，也没有宣称
  Jetson/Pixhawk、HIL/REAL、室外 GNSS 或最终 0.3.0。
- 2026-10-06：继续在现有 OnboardAI/r33 上做实体 3-D 几何增量训练，比较 r41/r42/r43/r44。
  四轮都保持同一 40 维契约、同一 SDF 几何来源和同一安全/PX4 Mission 边界；独立语义保持集
  均为 `7/7`，但 22-case 泛化均为 `21/22`，分别暴露左/右 bearing 导航符号失败。r42/r43
  的可选 navigation-sign loss 未修复方向问题，r44 延长训练出现结构化数据过拟合。因此候选
  全部拒绝，当前引用仍为 r33。证据：
  `docs/evidence/onboard-policy-r41-r44-geometry-bearing-candidate-comparison-20261006.json`。
  这仍不是 Gazebo 像素级 RGB/Depth 训练；外部 Gazebo/PX4、Jetson/Pixhawk、HIL/REAL、室外
  GNSS 和最终 `0.3.0` 均未宣称通过。
- 2026-10-06：完成 r45 低权重实体几何对照候选。r45 从 r33 继承同一 40 维契约，只加入 3,000
  条实体 SDF 几何教师和 6,000 条 bearing 锚点；独立保持集 `7/7`，但 22-case 泛化仍在
  `bearing-right` 失败（`21/22`），因此拒绝并保持 r33。该结果说明继续调整合成结构化权重
  不能替代真实 Gazebo/PX4 RGB/Depth/telemetry/trajectory 数据；证据：
  `docs/evidence/onboard-policy-r45-geometry-low-weight-rejection-20261006.json`。
- 2026-10-06：将 `run_onboard_policy_chain.py` 与 `run_external_onboard_model_acceptance.py` 的无参数默认模型统一到 hash-bound 的当前 r33 reference；历史 r4 不再作为新入口的隐式模型。外部 Gazebo、Jetson、Pixhawk、HIL/REAL、室外 GNSS 和最终 `0.3.0` 发布边界保持不变。
- 2026-10-06：把 3-D 主线的目标语义从历史“红车/绿车”示例中解耦。RGB/Depth 适配器现在
  只在 Prompt 明确要求时启用颜色过滤；中性实体默认按类别、连续跟踪、metric depth 和 GPS/
  安全门处理，搜索去重也不会吞掉“类别匹配但颜色未知”的新目标。新增中性目标增量训练入口，
  在 RTX 4060 上从当前 r33 训练 r46/r47；两者 3-D 结构化 holdout 分别为
  `0.9800/0.9825/0.9850` 与 `0.9875/0.9850/0.9900`，但都在独立 `bearing-right` 泛化用例失败，
  所以未 promotion，r33 reference 保持不变。当前无参数软件回放已通过
  `search → match_target → record_location → return_home → report`；仍不是外部 Gazebo、
  Jetson、Pixhawk、HIL/REAL 或最终 `0.3.0` 证据。
- 2026-10-06：从当前 r33 继续训练了 r48/r49 几何标签修复候选，仍保持同一套 40 维输入、
  20 维输出和 SIM/Jetson 共用 reference。r48/r49 的独立结构化 holdout 均为 `7/7`；
  900 行三类 3-D 场景结构化传感器 holdout 分别为 `0.9833/0.9756/0.9844` 和
  `0.9933/0.9900/0.9900`。但通用 22-case 方向评估仍有 `bearing-left` 失败，而且没有
  真实 Gazebo RGB/Depth 像素行，因此两者都未 promotion，r33 仍是当前 reference。完整哈希和
  证据见 `docs/evidence/onboard-policy-r48-r49-geometry-label-candidate-comparison-20261006.json`；
  不代表 Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。
- 2026-10-06：收紧 3-D Gazebo 训练采集入口。preflight 现在为每个实体场景生成同一套
  r33 OnboardAI reference + 现有 YOLO detector + PX4 Mission 事务 + disposable SITL
  自动飞行采集命令，明确要求起飞后的 RGB/Depth/CameraInfo/telemetry，而不是只采启动地面帧。
  该改动只生成待执行命令；当前 Docker daemon 不可达，未宣称外部 Gazebo、Jetson、Pixhawk、
  HIL/REAL、室外 GNSS 或最终 0.3.0 通过。证据：
  `docs/evidence/onboard-3d-capture-command-upgrade-20261006.json`。
- 2026-10-06：为“先训练 OnboardAI、再接入真实 Gazebo 3-D”增加只读采集审计器
  `scripts/audit_onboard_3d_capture_inventory.py`。它按当前实体 SDF mainline 的场景哈希、
  实际 RGB payload、完整 FLOAT32 depth companion、同帧 wire telemetry 和 truth-free provenance
  筛选历史记录；扫描 122 份 RGB manifest 后 `capture_eligible=0`、`training_ready=0`。
  因此旧场景、缺深度记录和颜色示例不会进入训练，当前 r33 reference、candidate promotion、
  Jetson/Pixhawk/HIL/REAL、室外 GNSS 和最终 `0.3.0` 边界均不变。报告：
  `artifacts/evaluation/onboard-3d-capture-inventory-20261006.json`，SHA-256=
  `895f54028793988aa978a796af7d29f8c845fefa675b4d55ddb8f55c545a22de`。
- 2026-10-06：在 RTX 4060 上从现有 r33 继续训练同一 40 维 OnboardAI，得到 r51 3-D 几何
  预训练候选（60,400 行、24 epochs）。独立 holdout=`7/7`、三类实体 structured holdout
  task/flight/compatible=`0.9978/0.9967/0.9989`，但通用方向集 `bearing-left` 失败，
  guarded=`21/22`，所以 r51 被拒绝、r33 reference 不变。该轮 `rendered_rgb_depth_rows=0`，
  没有把结构化 teacher 当 Gazebo 视觉训练，也没有新增外部 Gazebo/Jetson/Pixhawk/HIL/REAL、
  室外 GNSS 或最终发布证据。证据：`docs/evidence/onboard-policy-r51-3d-pretrain-rejection-20261006.json`。
- 2026-10-06：尝试启动本机 Docker Desktop 以继续实体 3-D Gazebo 采集，但后台日志确认
  `desktop-linux` daemon 在 `sailor-ingest.sock` reparse-point rename 处崩溃；当前权限无法移动
  或删除两个残留 socket。没有 factory reset、镜像/容器删除、WSL 数据改动或仓库文件改动，
  因此仍没有新增 Gazebo RGB/Depth/telemetry episode。证据：
  `docs/evidence/docker-desktop-socket-preflight-20261006.json`。
- 2026-10-06：把 `scripts/audit_onboard_3d_capture_inventory.py` 直接绑定到
  `run_onboard_3d_training_preflight.py`。每次训练预检现在都会审计当前实体 3-D 主线的 RGB
  manifest、实际 Gazebo payload、FLOAT32 Depth 配对、同帧 hash 和 truth-free provenance；
  本轮扫描 122 份历史 RGB manifest，`capture_eligible=0`、`training_ready=0`。这一步明确
  拒绝用红车/绿车例子、旧 RGB 或无 Depth 数据冒充 OnboardAI 的 3-D 视觉训练；r33 reference
  不变，外部 Gazebo/PX4、Jetson、Pixhawk、HIL/REAL 和最终 `0.3.0` 未宣称通过。新报告：
  `artifacts/evaluation/onboard-3d-training-preflight-20261006-r4.json`，SHA-256=
  `10b70bf648cf536945a469e02a778e7d775129adaac4e3fa75f6ad8c8619d50b`。
- 2026-10-06：完成 `vtol_selected_task_campaign.py` 的共享 OnboardAI 接入审计。`SharedTaskPolicy`
  现在复用既有任务编译和 PX4 Mission upload/readback/commit/permit 生命周期，Prompt 解析、
  planner 重验、airframe hash、policy/ranker hash、Mission hash 和每个 sortie 的 evidence
  都被绑定；GPS fix 不合格会在 Mission transaction 前 fail-closed，旧 ranker-only API 保持兼容。
  完整 `tests/test_vtol_sitl_mission_acceptance.py`、专项 shared-policy/Prompt/GPS/mismatch 测试和
  Ruff 均通过。该结果仍是 fixture/编排层证据，不是外部 Gazebo、Jetson、Pixhawk、HIL/REAL、
  室外 GNSS 或最终 `0.3.0` 通过。证据：
  `docs/evidence/shared-policy-external-campaign-integration-20261006.json`。
- 2026-10-06：重新由 ClusterYourCodex 调度当前实体 3-D simulation assets 到在线 Linux `nuc`，
  用严格 `docker info` 探针复核外部运行时。native `exit_code=104`，Docker daemon 不可达；
  没有启动 Gazebo/PX4、没有生成 RGB/Depth/telemetry，也没有改变 r33 reference 或 release gate。
  证据：`docs/evidence/cluster-docker-strict-probe-20261006.json`。
- 2026-10-06：用当前 r33 reference 的真实 ONNX 推理入口跑通一轮模型驱动的软件闭环：
  `search -> match_target -> record_location -> return_home -> report`。模型对不匹配候选输出
  `move_relative`，对匹配目标输出 `follow_target`，两个导航计划都经过现有 planner 重验；
  证据明确 `model_decision_used=true`、`fixed_colour_label_used=false`、`truth_json_used=false`。
  这不是把红车/绿车例子硬编码进 Gazebo，也不是外部 Gazebo/PX4、Jetson、Pixhawk、HIL/REAL、
  室外 GNSS 或最终 `0.3.0` 通过证据；当前输出仍标记为 software-SIM-only。证据：
  `docs/evidence/onboard-policy-chain-neutral-r33-20261006.json`。
- 2026-10-06：在 RTX 4060 上从现有 r33 继续训练 OnboardAI r52 3-D curriculum 候选，
  使用三类实体 SDF 场景、生命周期、左右 bearing 和中性目标 teacher，共 93,200 条样本、
  18 epochs，保持同一 40 维输入/任务头/飞行动作头。独立 holdout=`7/7`，结构化 3-D
  sensor holdout task/flight/compatible=`0.9967/0.9944/0.9978`；但通用方向压力集仍在
  `bearing-left` 失败（guarded=`21/22`），且 rendered RGB/Depth 行数为 0，所以候选拒绝、
  r33 reference 不变。没有使用 Gazebo truth 或红车/绿车视觉标签，不代表外部 Gazebo、Jetson、
  Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。证据：
  `docs/evidence/onboard-policy-r52-3d-curriculum-rejection-20261006.json`。
- 2026-10-06：继续在现有 r33 OnboardAI 上训练 r63 几何一致性候选。修复 3-D curriculum 的
  scene/event/phase/semantic 索引耦合，加入 FOLLOW_TARGET 的观测 bearing/range 几何辅助损失，
  保持 40 维输入、任务/飞行动作输出和 SIM/Jetson 共用入口不变。RTX 4060 训练 93,200 行、
  30 epochs；独立 holdout=`7/7`，900 条结构化 3-D sensor holdout task/flight/compatible=
  `0.997778/0.996667/0.998889`，22-case 泛化与方向/距离均 `22/22`。r63 只通过确定性结构化
  软件 SIM，不含真实 Gazebo RGB/Depth episode、外部 Gazebo/PX4、Jetson、Pixhawk、HIL/REAL 或
  室外 GNSS 证据，因此不 promotion，r33 继续作为 reference。证据：
  `docs/evidence/onboard-policy-r63-geometry-lr-20261006.json`。
## 0.3.0-pre.8 — 2026-10-06

- Expanded the neutral 3-D OnboardAI curriculum to cover all three repository-owned entity scenes,
  seven realtime events, nine task phases, and five generic semantic states in a Cartesian training
  contract. The curriculum remains incremental on the shared 40-feature model and does not use the
  illustrative red/green fixture as a Gazebo identity.
- Trained r64 and r65 on the local RTX 4060. r65 adds finite bearing-sign and bearing/range geometry
  losses while preserving the existing ONNX interface and planner/safety boundary.
- Added the auditable comparison receipt at
  `docs/evidence/onboard-policy-r64-r65-3d-cartesian-20261006.json`. The candidates remain quarantined:
  no pixel-level Gazebo dataset, external Gazebo runtime, Jetson/Pixhawk/HIL/REAL evidence, or final
  `0.3.0` promotion was claimed.
- 2026-10-06：在现有 OnboardAI 上继续做几何增量训练与运行时安全收口。新增 `target_geometry_frame`
  契约，明确相机相对 bearing 不能直接当 NED 世界方位；只有同帧 Depth、CameraInfo、相机姿态
  和标定变换齐全时才允许把 FOLLOW_TARGET 绑定到 N/E 计划。r67/r68/r69 使用同一 40 维模型、
  同一 ONNX 入口和 4060 训练链，独立全圆角度/8–420 米距离评估仍未通过，因此没有替换 r33
  reference，也没有 promotion。Gazebo 训练主线仍等待真实中性 3-D RGB/Depth/telemetry episode，
  红车/绿车不作为主线标签；没有外部 Gazebo、Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终
  `0.3.0` 发布声明。证据：`docs/evidence/onboard-navigation-geometry-20261006.json`。
- 2026-10-06：统一渲染数据生成器与 Jetson/SIM 的 OnboardAI 几何契约。`build_onboard_rendered_3d_dataset.py`
  现在复用 `ContextSnapshot + OnboardRuntimeEvent + OnboardModelInput` 的 40 维特征；有完整
  CameraInfo、Depth、姿态和 heading 时生成 calibrated NED geometry，缺少标定时保持
  `camera_relative` 并 fail-closed，不把相机相对角当成全球 NED 或 GPS/航点。新增 8 个 heading、
  pitch、Depth、偏移和无标定回归测试全部通过；这仍不是外部 Gazebo、Jetson、Pixhawk、HIL、REAL
  或室外 GNSS 验收。
- 2026-10-06：完成 r80 world-SDF 场景级 OnboardAI 增量候选训练，继续从 r33 父模型、同一 40 维输入和
  ONNX 输出契约开始，使用三套实体 3-D world 的 visual/collision 几何统计；候选保持 quarantine，
  因尚无真实 RGB/Depth/CameraInfo/pose/telemetry episode 和 reviewed trajectory，不替换 r33。
- 2026-10-06：修正实体 3-D Gazebo 训练数据的距离语义。Gazebo `optical_axis_z_m` depth 现在按
  CameraInfo 逐像素换算成与 Jetson 实时适配器一致的 `ray_slant_m`，缺少内参或未知语义直接
  fail-closed；新增 optical-axis parity、ray-slant 保持和缺内参回归。旧的 RGB/结构化数据训练
  尝试被严格门拒绝，没有生成或 promotion 新模型，r33 reference、SIM/HIL/REAL/室外 GNSS 与
  最终 `0.3.0` 边界不变。
- 2026-10-06：继续从 OnboardAI r33 reference 做第三轮中性 3-D 几何增量训练，使用 RTX 4060/CUDA
  训练 `251,000` 行并保持同一 40/20 ONNX 契约。候选未晋级；新增证据明确区分结构化 SDF
  bootstrap 与真实 Gazebo RGB/depth/CameraInfo/pose episode。导航评估器改按 raw head 的
  `40m` bounded next-step 语义计算，并记录 calibrated adapter 对 FOLLOW/GOTO 的 measured NED
  覆盖关系。没有声称 Gazebo、Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。
- 2026-10-06：完成一轮从 OnboardAI r33 reference 出发的几何超参探针（RTX 4060/CUDA、30 epochs、
  geometry weight `20.0`），验证较高几何权重只改善 bounded-step range、却损伤方向质量，
  候选继续隔离。训练仍为中性实体 SDF 的结构化 bootstrap，未使用红车/绿车颜色 fixture，
  也未声称真实 Gazebo、Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。
- 2026-10-06：把真实 Gazebo 3-D capture 接入 OnboardAI 训练前的数据绑定链：新增
  `build_gazebo_raw_recording_manifest.py`，严格校验 MDGZSIM3 RGB、MDGZDEP1 深度、场景/主线资产
  hash 和 truth-free provenance，保留原始 BGR24 来源并用无损 PNG 绑定既有 replay 事件。
  输出仍明确 `training_ready=false`，必须经过独立轨迹复核后才能从当前 r33 做增量训练。红车/绿车
  没有被写入主线模型；本机 Docker/Gazebo/PX4 仍未运行，因此没有新增外部 SIM、Jetson、Pixhawk、
  HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。
- 2026-10-06：`run_external_onboard_model_acceptance.py` 增加 recording binding 接口；外部
  PX4/Gazebo capture 成功后，可用 `--recording-output-directory` 在同一轮生成 recording，
  并绑定 RGB/Depth/Telemetry 到既有 OnboardAI 训练门。dry-run 只记录计划，不生成假 recording，
  不自动训练、不 promotion，也没有新增外部 runtime 或硬件验收声明。
- 2026-10-06：收紧 `run_onboard_3d_training_pipeline.py` 的候选输出验收：拒绝非空/链接候选目录，
  复核 config/report/父模型与数据哈希、reviewed real-3-D gate、独立 test split，并用 ONNX checker
  与 CPU runtime 复开唯一 `[1,40] -> [1,20]` 模型；训练子进程 `0` 不再足以制造 `candidate_trained`。
- 2026-10-07：修复真实 Gazebo 3-D RGB/Depth 进入 OnboardAI 训练前的零基序列、MDGZDEP1 v1
  兼容和只读深度转换阻塞；历史真实地面 capture 已绑定为 3 帧 recording、1 个连续 episode。
  实际 CUDA 严格训练入口拒绝未复核、样本不足、无独立 test 的轨迹；没有生成或晋级模型，
  r33 reference 与 release parity 不变。相关回归 88 项通过；不是本轮新增外部飞行/HIL/REAL。
- 2026-10-07：修复 DockerSimulatorPlan 到真实 Gazebo frame-set acceptance 的资产 bundle 传递，
  采集持久化回归恢复通过。发现工作区历史 acceptance 资产绑定与 checked-in release parity
  的 PX4 hash 尚未同步；本轮保持 parity 不变，不宣称 release gate 通过。
- 2026-10-07：按实体 3-D Gazebo 主线推进 OnboardAI 训练数据。隔离 annotation world 的真实
  `AnnotatedOriented3DBox_V` topic 已观察到 `label_id=42` 与 3-D center/size；同次运行保存了
  20 帧 RGB、FLOAT32 Depth、CameraInfo、NavSat、Odometry。新增
  `scripts/gazebo_annotation_stream.py`（只读 capture-only 标注）和
  `scripts/materialize_gazebo_annotation_sensor_stream.py`（复用 MDGZSIM3/MDGZDEP1 生产解析器）。
  RGB/Depth manifest 已生成并通过 dataset builder，但 Docker Desktop 随后发生 storage I/O，逐帧
  annotation hash binding 尚未封存；因此 `training_ready=false`，r33 仍是唯一父模型，没有声称
  Jetson/Pixhawk/HIL/REAL、室外 GNSS 或最终 `0.3.0`。
- 2026-10-07：纠正 annotation sensor staging 的 provenance 边界。原始 MDGZSIM3/MDGZDEP1
  保留，但 materialized-r4/r5 派生生产清单因伪造 host monotonic 时间、错误重绑定主线资产而
  标记为历史无效；新增 `materialized-r6-staging`，只保存 `gazebo_sensor` 时间、wire hash、
  实际 annotation binding 和原始 RGB/Depth，不生成可被训练器误认的生产 manifest。标注采集器
  改用 `gz.msgs10`，按精确 sensor timestamp 保存 3-D box 与 RGB_INT8 labels-map hash/可见框；
  尚未生成训练数据，`training_ready=false`。
- 2026-10-07：新增 `bind_gazebo_annotation_staging.py`。它按精确 Gazebo sensor timestamp 和 RGB
  payload SHA-256 绑定 v2 annotation observation，只接受真实可见的 RGB_INT8 labels-map、label-42
  的 3-D rendering-view geometry 和可审计 hash；缺帧、重复帧、最近时间替代、目标在相机后方或
  不可见时 fail-closed，输出保持 offline staging，未晋升为训练数据。
 - 2026-10-07：新增 `multidetect.onboard-3d-target-visibility-gate-v1`，把 detector 命中、FLOAT32
  深度测量和 calibrated camera/depth/pose 几何绑定为同帧训练前置条件；当前仓库为 0 行，训练入口
  继续 fail-closed。重新生成 `onboard-3d-training-pipeline-20261007-r2` preflight，Docker/Gazebo/PX4
  运行时仍不可用，r33 reference、r89 隔离和最终发布边界不变。
- 2026-10-07：从r33完成r90隔离开发训练（RTX 4060、105200条结构化状态、60epochs）。
  修复顺序场景块训练为seeded epoch shuffle，新增action-only父模型蒸馏；导航诊断明确区分完整
  目标偏移与自选40m短步，阈值不变。900条同条件新采样task/flight从83.44%/83.67%到
  99.78%/99.89%，但方向/距离仍不合格、r33不替换。训练不是渲染图像、外部Gazebo或实飞证明；
  README收敛为当前结果与人话目标，详细限制写入docs/onboard-training-status.md。
- 2026-10-07：新增三维可见实例标注到YOLO视觉训练格式的导出器，显式类别映射、完整录制
  分区、图像实际像素/尺寸验证、同帧hash绑定与lossless PNG。出口是未review的staging，
  不是实际视觉微调完成；不把40维策略监督或3-D scene truth误作视觉标签。
- 2026-10-08：完整NEW267训练phase实际native0（aa1ca2），旧launcher／worker退出；
  四阶段计数与API267、累计旧13＋新267＝280、source前后一致、candidate86及终态report
  保存并独立raw复算。封存 docs/evidence/onboard-navigation-recovery267-train-closed-20261008-r1.json。
  strict69→86／86，但动作55→27、数值0→8、STOP仍0／3，不作质量晋升；86图同scene仅3prompt。
  独立fresh reload已启动仍运行，完整terminal audit与59开发模型评估未完成；保留旧观察／失败，
  不改冻结输入、默认reference、产品版本、parity或实时／飞行／硬件／发布门。
- 2026-10-08：新增四类FP32线性动作读出组件，12,294参数；作者／root各89项CPU
  软件测试、Ruff与独立只读审查通过。支持last-nonzero prefix位置、known-only尺度loss和
  单条batch仍有效的类别权重，拒绝非法尺度、不硬补步长／角度，不伪造文本token。
  软件证据 docs/evidence/onboard-navigation-action-readout-software-20261008-r1.json；
  尚无真实feature／probe训练／sidecar重载／速度结果，不替换默认或飞行／发布门。
- 2026-10-08：新增有界原TRAIN注释索引，实际native0；65项测试／Ruff及独立精确
  786432前缀SHA复核通过，196records→120route/prompt、5scene、4077候选metadata帧。
  达候选预算partial停止，未重验全405MB／下载RGB或核rawposegold。10个typed -1/-2
  padding记录仅隔离，旧严格parser未改；两次真实native1及计划保留，不扩大训练动作。
  证据 docs/evidence/openfly-bounded-annotation-index-20261008-r1.json，训练准入与质量门不变。
- 2026-10-08：新R3现场特征实验实际加载原生崩溃native1，c10.dll事件／失败计划／日志
  封存，0新相机／feature／forward／训练／飞控，根因未证。R3入口26tests通过，NEW父
  Mission区间72及CPU现场审计19独立进程tests／Ruff通过；137联合尝试失败如实保留，
  不取消Torch隔离门。真实teacher数据与现场复算未完成，默认／版本／发布门不变。
- 2026-10-08：NEW feature recorder v2修复atime-only误报，仍拒绝size／mtime／inode漂移
  与等长内容SHA变化；保留原20＋新增6项CPUtests，root与独立26通过，Ruff／AST
  审查闭合。v2合同显式独立，frozen R3及v1consumer未替换；无实际新采集或模型运行。
- 2026-10-08：NEW exact-load-only诊断保留冻结loader，显式执行／只读预检分离，持有
  fault FD、bounded fsync phase日志与cleanup最终门；44作者及独立CPUtests／Ruff通过。
  R4计划实际发出，资源预检native1拒绝，0新load／模型／执行目录；8／16GiB仅本次
  保守策略，非已证模型容量或R3 OOM原因。旧失败与默认／版本／发布门保持不变。
- 2026-10-08：三子智能体并行完成NEW pinned元数据提取、唯一RX V4只读请求、事件／
  原参数consumer及独立review，249新CPUcase通过。actual只读运行保存4623原包、
  17参数CRC＋bytewise与PX4版本；COM_RC_IN_MODE1、容量-1与失联动作2保持原值。
  第20次日志输出超限未保存、整轮native1失败；owned清理／PID／40源窗口闭合。
  旧R3的24UNKNOWN包另CRC核、16事件metadata匹配，原sourcefalse保留、不定唯一根因。
  consumer审核修复非选中标签隐藏冲突版本的漏洞，37cases green，另存新派生报告；
  版本pre.8、默认r33及所有训练／飞行／硬件／晋升／正式发布门未改。
- 2026-10-08：NEW完整架次有限预算／V4采集／原件账本／CP-v2主入口及条件静态地图整路检查；五轮actual no-ARM均拒绝，源码副本和owned清理核验。R5同窗原件齐但校验末尾真过期，优化实际延迟，不续期。新增公开动作／世界表征模型复核；无新飞行、训练、晋升或正式发布。
