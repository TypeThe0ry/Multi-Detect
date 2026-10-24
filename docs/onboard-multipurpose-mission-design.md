# 机载通用多用途任务设计

## 目标与边界

“通用多用途”不是把一个目标类别列表做得更长，而是把自由文本任务编译成可验证的任务计划。SLOW 在任务开始时生成 `MissionPlan`，计划由有界的 skill 组成；运行期间只在事件触发时对当前步骤提出决策或重规划。SLOW 永远不产生电机、PWM 或 MAVLink 指令。FAST、planner 和 safety layer 负责执行与最终否决。

## Skill 注册表

| Skill | 用途 | 主要参数 | 完成条件 | 感知需求 | 安全门槛 |
| --- | --- | --- | --- | --- | --- |
| `goto` | 到达符号位置 | `target_ref`, `altitude_m` | `arrived` | 可选 | 地理围栏、定位 |
| `area_search` | 区域搜索 | `region`, `query` | `region_covered` | YOLOE query | 区域边界、障碍物 |
| `count` | 计数 | `region`, `query`, `max_count` | `count_reported` | YOLOE + track | host 验证候选 |
| `inspect` | 环绕和近距检查 | `target_ref`, `query`, `views`, `standoff_m` | `views_captured` | YOLOE + VLM 属性问答 | 距离、速度、视角 |
| `patrol` | 重复巡逻 | `route_ref`, `query`, `loops` | `duration_elapsed` | YOLOE 或 prompt-free | 路线、循环上限 |
| `survey` | 网格测绘 | `region`, `overlap` | `region_covered` | 可无 query | 覆盖率、重叠率 |
| `follow` | 跟随运动目标 | `track_ref`, `query`, `duration_s` | `target_lost_timeout` | YOLOE + tracking | 丢失超时、速度边界 |
| `monitor` | 定点监视和告警 | `region`, `query`, `duration_s` | `duration_elapsed` | YOLOE 或 prompt-free，属性由 VLM 判断 | 悬停、续航 |
| `record` | 保存证据 | `query`, `max_count` | `views_captured` | 当前 FAST 观测 | 存储回执 |
| `deliver` | 投递或释放 payload | `target_ref`, `payload_ref`, 可选 `altitude_m` | `arrived` | 目标位置确认、释放握手完成并爬升 | 需要 `payload` capability，且 release authorization 默认关闭 |
| `hover` | 悬停等待 | `duration_s` | `duration_elapsed` | 无 | 位置和电量 |
| `return_home` | 返回 home | 无 | `arrived` | 无 | PX4 failsafe、定位 |
| `land` | 降落 | 可选 `target_ref` | `landed` | 无 | 降落区和 safety layer |

位置、区域、路线、轨迹和 payload 都是 `zone:A`、`home`、`track:7`、`poi:gate` 这样的符号引用，由 host 绑定；模型不能输出原始经纬度。query 最多 8 个词，供 YOLOE prompt 或 VLM 属性问题使用。

## 规划和执行

任务文本（例如“检查停车场内三辆红车，逐辆拍照后回家”）先由 SLOW 生成版本化计划。host 校验字段、参数边界、引用、终止步骤和图结构后才交给 FAST。每个步骤有 `on_success` 与 `on_fail`，最多 12 步；只有显式 `patrol` 循环计数允许环。运行中 `target_lost`、障碍、定位退化、异常发现等事件可以触发当前步骤重规划。

FAST 为每个步骤安装对应 YOLOE prompt set、tracking 目标和 planner 子目标。survey 使用 lawnmower 路径和照片重叠；inspect 使用 standoff 和多视角轨迹，VLM 只回答“门是否打开”“是否有损伤”等属性问题；monitor 和 patrol 可以先走 prompt-free anomaly discovery，再把事件交给 SLOW。现有 policy v2 的 bounded primitives、rollout 和 planner 继续作为执行边界。旧的 `onboard_decision.py` 决策保持兼容，`confirm_target` 在 `count` 中只能在 host 验证后增加计数，在 `inspect`/`record` 中只能产生证据请求。

报告由 host/FAST 根据真实观测填写，包含 finding query、count、evidence frame ids、track ids、符号位置、状态答案和 alerts。模型的文字声明不能直接成为完成条件或报告事实。Safety layer 对每个 primitive 做最后 veto；SLOW 超时、JSON 无效、OOM 或推理故障时保留最后一个安全 sub-goal，并按定位、电量和 PX4 failsafe 规则 hover 或 return home。

## Orin 8 GB 预算

SLOW 只在任务开始和事件触发时调用，不在每帧运行。FAST 的 YOLOE、tracking、GRU、planner 和 safety layer 保持实时路径；短 prompt、受限 token、JSON schema 和异步队列控制共享内存峰值。Qwen3-VL-2B Q4 GGUF 是当前候选，Orin 上仍需实机测量并保留系统、相机、并发峰值和安全余量。

## 数据与训练

数据按 find、count、inspect、patrol、survey、follow、monitor、deliver 等 mission family 划分。SIM world generator 需要真实任务组合和合理的 mesh 资产，当前正从彩色 box 迁移到 CC0 Gazebo Fuel meshes；每个 skill 使用独立 expert teacher。SFT 数据覆盖 `task text -> MissionPlan`，而不是只训练单次 target judgement。闭环评估按 family 统计区域覆盖率、计数误差、检查视角完成率、跟随丢失时间、巡逻告警召回、survey overlap、safe veto 率、超时和误返航；train/validation/test 按 world、路线和目标配置隔离。

## 当前实现状态

已实现：严格版本化 `MissionPlan` contract、13 个 skill registry、参数和 capability 校验、步骤引用及循环校验、终止步骤校验、`SkillStatus`/`MissionReport` 数据结构、旧决策到当前 skill 的解释映射，以及 CPU pytest 覆盖。

**机上执行器（2026-10-14）**：`src/multidetect/autonomy/mission_executor.py` 的 `MissionExecutor` 按 `on_success` / `on_fail` 逐步执行计划，每步有步数上限，报告只来自实际观测（发现、计数、证据帧、视角答案、告警）。已实现的技能：

| Skill | 实现 |
| --- | --- |
| `area_search` | 机上闭环（快系统候选 + 地面锚定轨迹 + 慢系统身份判断）直到确认 |
| `count` | 同一闭环，确认后不停止继续搜索；同一地面位置（4 m 内）只计一次 |
| `inspect` | 绕发现点按 `standoff_m` 均匀取 `views` 个视角，自动选能看到目标的高度，静态地图挡住的视角跳过；每个视角对准目标拍照并让慢系统判断最近的轨迹 |
| `goto` | `home` / `finding:N` / 主机提供的 `poi:`、`zone:` 引用，先调高度再分段飞，带障碍检查 |
| `hover` / `return_home` / `land` / `record` | 悬停计时；先爬回巡航高度再回家；停止；记录当前帧为证据 |

`survey`（在区域内按“之”字航线覆盖并统计覆盖率）、`patrol`（沿主机给出的航线飞若干圈，边飞边判断并告警）、`monitor`（到区域中心悬停并定时转向扫视，判断并告警）、`follow`（跟住上一步找到的目标并保持距离，丢失超时或到时结束）已实现（与 Codex 协作，`771b262`）。`deliver` 已实现为 gated skill：只有 executor 同时拿到显式 `payload` capability 和独立的 release authorization 才会执行。它解析已知位置或前一步 finding，飞到目标后下降到配置边界内的释放高度，保持并复核三维位置误差；随后只发出带 request id 的 payload release event/action，由 host 驱动硬件并通过 ack 接口确认。收到成功 ack 后才爬回巡航高度；位置漂移、障碍、拒绝或 ack 超时都会让步骤失败并沿 `on_fail` 分支。executor 本身不直接操作 payload 硬件。

机上从任务文字生成计划（`mission: "from_text"`，`8f4bf68`）：起飞时用同一个慢系统服务把任务句子编译成计划，失败就退回只有“返航”的安全计划；计划里不认识的地点会被拒绝并带着已知地点列表重试（`13d2202`），已知地点可带标签（如 `zone:site (场地 / the site)`）。感知步骤按该步骤的查询词判断候选。

仿真实飞（Gazebo + PX4 SITL，慢系统 v2-r4-prop）：“找到 → 绕飞 4 个视角 → 返航”在 gen_v2_w0035（SUV）和 gen_v2_w0038（邮筒）上都完整执行，分别 46 步和 32 步，定位误差约 0.5 m，4 个视角都拍到并得到身份答案，最后回到起飞点。

**从任务文字生成计划（2026-10-14）**：`src/multidetect/autonomy/mission_planner.py` 按技能注册表生成逐技能的严格 JSON schema（参数名、类型、范围都固定），提示里给出技能清单、引用规则和 6 个短示例；`repair_plan_document` 确定性修复常见错误（“失败就重试”的回边、缺少或悬空的终止步骤），然后再走 `validate_mission_plan`。在 30 条手写中英文任务（`configs/onboard/plan-eval-tasks.json`，含找、计数、检查、巡逻、测绘、监视、跟随、悬停、降落及组合）上，未微调的 Qwen3-VL-2B Q4_K_M：计划 100% 有效，技能序列 70% 正确（严格 20/30），开发机中位 1.5 s。最初只有 2 个示例时只有 53% 有效、23% 正确。剩余错误主要是多加“记录 / 检查”步骤（例如把“记录位置”理解成拍照记录）。

**计划与任务意图一致性检查（2026-10-23）**：`src/multidetect/autonomy/plan_intent.py` 在 `compile_mission_plan` 里、schema/引用/图结构校验之后和任务包检查之前运行，只看任务文本和计划，对任何规划模型都生效（机载 `from_text`、`plan_from_task.py` 评测走同一路径）。动机是 r14/r19 对比中与模型无关的三类错误：r19 把“到大门看一下，然后返航”规划成 `goto → land`；r14 在没提任何物体的任务里加 `inspect(query="truck")`；把地图上没有的地点（Raven Pier、青石码头）换成已知 ref。

| 规则 | 判定 | 处理 |
| --- | --- | --- |
| `return_intent_land` | 任务含未被否定的返航说法，且没有明确降落说法时，从首步沿 `on_success`/`on_fail` 可达的终止步骤里不能有 `land`（`land(target_ref=home)` 除外）。返航说法：返航/回航/归航/返场/撤回/回家/回来（不含“找回来”等）、“返回/回到/飞回/回 + 6 字内 起飞/起点/出发点/放飞/home/基地”；`return home/to launch/to base`、句末的 `return`、`come/head/fly/bring … back`（后面不接“to 非 home 地点”）、`… home`、`back to/at launch`、RTL/RTH。降落说法：降落/着陆/落地/落下/落在/落起降坪/原地落、`land`（`landing pad` 等名词除外）、`touch down`、`set/put … down`。同一分句里前面有 不/别/勿/无需/取消、not/don't/never/no need to/without 则不算。“起降坪”是地点，不算 home | 重试时把原因写进提示；最后一次尝试把这些 `land` 改成 `return_home` |
| `ungrounded_query` | area_search/count/inspect/record/follow 的 query 必须在任务里有依据：英文词（单复数、少量同义词表）出现在文本里，或该词的中文名（词表 `ZH_NAMES`）出现在文本里；`none/object/target` 等占位词一律无依据。英文任务按自身词判；中文任务里 query 的词都不在词表时判“无法判断”并放行，所以词表外的物体永远不会被拦。`record` 的 `current view`（当前画面）是数据约定，不查 | 重试；最后一次尝试删掉该步骤并把指向它的边接到它的后继 |
| 同上（patrol/monitor） | 这两个技能必须带 query；任务确实要求巡逻/监视（巡/监视/观察/盯/留意…，patrol/monitor/watch/observe/circuit/loop…）但没说找什么时只记 `intent_warnings`；任务根本没要求时（如“直接回放飞的位置”却规划了 `monitor(dog)`）按上一行拦截 | 同上 |
| `ungrounded_place` | goto/land/deliver 的 `target_ref`（home、zone:all、finding、track 除外）必须是任务提到的地点：ref 名或英文标签词出现在文本中，或中文标签的任一两字片段/末字（中文地点名中心词在后，“大门”也匹配“门口”）出现在文本中；只用来排除地点的分句（不是/顶替/代替/not/substitute/instead of）不算提到。标签字符串可用 `/` 分隔多个别名，例如 `门 / 大门 / 入口 / entrance / gate`；机上提示和依据检查会使用全部别名。内置词表还补充门/大门/入口/entrance/gate、码头/pier/dock、停机坪/起降坪/landing pad、仓库/warehouse/shed 等常用说法；同一别名若归属于多个 ref，会发出非阻塞 `place_alias_conflict` 告警并停用该别名 | 重试；**从不修复**，最后一次仍违反则不给计划，退回只返航的安全计划，report 带 `intent_violations` |

未知地点：`check_plan_refs` 原本就拒绝未知 ref 并带已知列表重试；现在没有任何已知地点时也照样检查（以前 `refs` 为空就跳过，`land(poi:up)` 会一直留到执行器 `unknown_ref` 才失败）。最后一次尝试把未知 ref 换成“唯一同类 ref”的旧修复仍保留，但换出来的地点若任务没提到，会被 `ungrounded_place` 拦下。区域（`region`）不做地点依据检查：实测正确序列的计划里区域与文本对不上的有 47 例，误拦太多。`compile_mission_plan(check_intent=False)` 可关掉检查。

离线回放（`_scratch/plan-intent/replay.py`、`synth.py`）：
- 期望计划零误拦：planning-v3 72 条带参数的期望计划、v1 30 条和 v2 60 条期望技能序列，0 条有发现；`build_plan_sft` 的 v1/v3/v5 共 8 个生成器各 1500 条、中英各一（24000 条合成计划），0 条有发现。
- 真实模型输出（r4–r19、base 共 43 个规划评测文件 2089 行）：技能序列正确的 1420 行中拦下 12 行，逐条核对都是真错误（数“木托盘”却用 `red car`/`cardboard box`，“收尾落起降坪”却 `land(poi:gate)`，“就在脚下着陆”却 `land(poi:shed)`，“先飞到起降坪”却 `goto(poi:gate)` 等）；序列错误的 589 行拦下 101 行；8 条应拒绝任务的 80 行拦下 38 行，其中 4 个未知地点任务 40 行全部被拒（36 行由地点规则、4 行由原有未知 ref 检查），其余是载荷能力任务，不属于本检查。
- 对比的 15 条返航探测 + 11 条回归任务（`tests/fixtures/plan_intent_replay.json`）：r19 的两条 `goto → land` 和回归包任务的 `goto → land` 被拦；r14 的 `truck`/`car`、r19 的 `none` 幻觉被拦；p05“到大门检查一下，再返回 home 点”两个模型的 `inspect(truck/red car)` 也被拦——原探测只比技能序列算它对，但任务没提物体，而且前面没有搜索步骤，`finding:1` 根本不存在。其余计划 0 误拦。

真实模型（GPU，temperature 0，`--cache-ram 0`）：同 26 条任务 r14、r19 都从 22/26 到 26/26；r19 在反馈后自己改对 3 条（p01、p05、回归包），r14 的幻觉在重试里不改，由最后一次修复删掉。planning-v3（80 条，无 mmproj）：r14 完整序列 0.6125 → 0.6625，r19 0.6875 → 0.7125；8 条应拒绝任务中 4 个未知地点任务全部拒绝（以前 r14、r19 各只拒 1 条）；72 条正常任务没有一条因此失去计划。r19 有一条从“对”变“错”：“收尾落起降坪”原计划落在 `poi:gate`（只是序列碰巧对），被拦后重试多了一个 survey。

尚未实现：生成计划的模型微调与机上接线、上表之外的技能、计数任务的测试世界（需要同类多实例场景）、payload 硬件驱动、真实 Orin 峰值测量、各 mission family 的教师数据和闭环训练。这些部分不能由本 contract 的模型输出直接宣称完成。

## 地面链路已有机制与机上任务的关系

下面是本次接入前已经存在的机制。它们各有用途，不能把“能收到飞控心跳”当成“操作员仍在监督这份任务”，也不能把地面告警回执当成继续飞行的授权。

| 机制 | 仓库证据 | 已有行为及边界 |
| --- | --- | --- |
| MultiQGC 原生任务编辑和上传 | `ground-control/MultiDetectGCS/custom/README.md:35` | Plan 页面已有区域框选和原生 Survey 航线，使用 QGC 原有保存、上传流程。这是飞控航点任务；此前没有在此处接入本节的机上任务包与失联策略。 |
| MultiQGC 与 Jetson 操作协议 | `src/multidetect/operator_protocol.py:79`、`src/multidetect/operator_mavlink.py:107`、`ground-control/MultiDetectGCS/custom/README.md:13` | MAVLink 2 `TUNNEL`，payload type 42000，带应用 HMAC 和外层签名；已有目标选择、授权确认及任务、安全、感知状态消息。该协议与本次机上监督器的事件输入是不同接口，不能假定现有 GCS 已能发送新的任务包或策略更新。 |
| 地面站心跳和直连租约 | `ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.cc:1242`、`src/multidetect/operator_udp.py:249`、`:415` | MultiQGC 直连每秒发送心跳；Jetson 只从通过认证的心跳建立地面元数据对端租约。它解决链路对端识别，没有替机上任务决定失联后继续、等待还是返航。 |
| 飞控遥测及其新鲜度 | `src/multidetect/pixhawk.py:380`、`:979`、`:1320` | 只读 provider 接收飞控心跳、位置和任务进度；心跳、`MISSION_CURRENT`、`MISSION_ITEM_REACHED` 分别计时。这里的飞控心跳丢失与操作员地面链路丢失必须分开。 |
| QGC 只读桥和隔离模拟路由 | `src/multidetect/qgc_readonly_bridge.py:183`、`src/multidetect/px4_sitl_qgc_router.py:65`、`src/multidetect/px4_gazebo_qgc_router.py:321` | QGC 只读流量走白名单；操作 TUNNEL 留在本机；隔离模拟的 evaluator 另有受限 Mission 上传及生命周期指令。不可从这些测试通道推导真实飞机已开放任意控制。 |
| PX4 Mission 上传、回读及持久事务 | `scripts/vtol_sitl_mission_acceptance.py:779`、`:937`、`src/multidetect/mission_journal.py:1` | 已有 `MISSION_COUNT → REQUEST_INT → ITEM_INT → ACK`、精确回读 hash、提交和启动许可；事务日志追加并 fsync。它记录飞控任务传输事务，不是本次机上执行结果日志。 |
| 告警下传及持久发件箱 | `src/multidetect/alerts.py:656`、`docs/data-link-alerts.md:3` | 火情告警已有 SQLite outbox、认证 UDP、匹配 ACK、重试和接收去重。它不承载任务变更或飞行命令；本次任务结果也不能只依赖及时下传。 |
| SIM teacher 心跳和 RC 输入设置 | `scripts/collect_sim_teacher_episodes.py:99`、`:726`、`:740`、`:1492` | collector 在收到飞控心跳、学到对端后，持续发送本机 GCS 心跳；SIM prearm 只设置 `COM_RC_IN_MODE=4`。本次不修改 collector，不将它的本机心跳误当成实际操作员在线。 |
| PX4 GCS 失联测试 | `scripts/run_px4_sitl_datalink_loss_acceptance.ps1:382`、`:486`、`:641` | 独立隔离 SITL 测试设置 `COM_DL_LOSS_T=5`、`NAV_DLL_ACT=1`，验证转 Hold/LOITER；重连后不会自动恢复 Mission。其他模拟验收有 `NAV_DLL_ACT=0`，例如 `scripts/run_simulator_docker_ai_acceptance.py:384`。这些均不自动配置真实飞机。 |

## 真实飞机的 PX4 失联参数配合

本节只是每次真实飞行前的配置建议，不执行参数写入，也不修改 SIM collector。操作员先在任务包里选择失联策略，再核对真实飞控当前固件和参数，使 PX4 不会在机上策略允许继续时抢先切换成另一个动作。电池、地理围栏、姿态、定位等独立保护继续开启；关闭“GCS 数据链失联动作”并不等于关闭这些保护。

以下枚举来自仓库随 MultiQGC 携带的 `ground-control/MultiDetectGCS/src/FirmwarePlugin/PX4/PX4ParameterFactMetaData.json:1`，不是本次联网查询的最新固件说明。该文件确认 `NAV_DLL_ACT=0/1/2/3` 分别为禁用 GCS 失联动作、Hold、Return、Land；真实部署仍须核对实际固件版本。`Terminate=5`、`Disarm=6` 不是这些任务策略的替代值。

| 操作员选择 | 真实飞机的 PX4 GCS 数据链失联建议 | 谁计时、谁执行 |
| --- | --- | --- |
| `continue` | `NAV_DLL_ACT=0`，避免 PX4 因地面站不在线终止自主任务。 | 机上继续执行，按预算、区域风险、监督要求和感知健康判断是否升级。持续离线本身没有累计到某时长就返航的暗规则。 |
| `continue_then_return` | 建议 `NAV_DLL_ACT=0`，由机上按任务包的 T 管理。 | 机上从最后一次有效地面心跳，或任务开始时尚无心跳的起点，计算静默时间；到 T 后返航。不能直接把 PX4 的连接超时当成任务计时器。 |
| `hold_then_return` | 建议 `NAV_DLL_ACT=0`，由机上先 Hold，等待至 T 后返航。单独设置 `NAV_DLL_ACT=1` 只能得到 Hold，不能保证 T 后返航。 | 机上识别失联后进入等待；有效重连可恢复等待中的任务；达到等待上限就返航。 |
| `return_now` | 可采用 `NAV_DLL_ACT=2` 作为飞控侧同方向的后备动作，同时保留机上监督器。 | 机上达到自己的失联判据后返航；PX4 达到 `COM_DL_LOSS_T` 后独立执行 Return。这里“立即”指判定失联后的动作，不能省掉链路超时判定。 |
| `land_now` | 可采用 `NAV_DLL_ACT=3` 作为飞控侧同方向的后备动作，同时保留机上监督器和降落条件检查。 | 机上判定失联后请求降落；PX4 达到自己的连接超时后执行 Land。起飞前须把这个选择与现场可降落条件一起确定。 |

`COM_DL_LOSS_T` 是 PX4 判定 GCS 连接丢失的秒数；随仓库携带的元数据给出范围 5–300 秒、默认 10 秒。不要把 `NAV_DLL_ACT=1` 误读成 1 秒超时。任务包的心跳新鲜度、继续静默 T、Hold 等待 T 是机上任务规则，三者应明确区分。对于机上负责延时的策略，不能让 PX4 在更早的固定时刻强制执行更激进的动作。还应检查真实飞控的 `COM_DLL_EXCEPT`：它可按 Mission、Auto、Offboard 等模式忽略数据链失联，可能让所选 PX4 后备动作在当前飞行模式里不触发。PX4 曾收到 GCS 后再丢失与起飞以来从未连接 GCS 的行为也应分别核对，机上策略不依赖先连接地面站。

### 实机起飞前 PX4 预检矩阵

机载入口在允许解锁前只读回下列参数和遥测。默认不会写 PX4；只有显式给出 `--apply-px4-params` 才会逐项写入并再次读回。任一阻断项失败都会拒绝解锁，报告同时写入任务目录的 `mission_log/mission.jsonl`。

| 项目 | `continue` / 定时策略 | `return_now` | `land_now` | 通过条件 |
| --- | --- | --- | --- | --- |
| `NAV_DLL_ACT` | `0`，机上监督器计时 | `2` Return | `3` Land | 必须与任务包方向一致；更激进值只有在任务包签名内的 `accepted_px4_overrides` 写明理由才允许 |
| `COM_DL_LOSS_T` | 读回 PX4 秒数，不能替代任务包 T | 同左 | 同左 | 在 PX4 元数据范围 5–300 秒内，并记录实际值 |
| `NAV_RCL_ACT` / `COM_RC_IN_MODE` | RC 仍按现场接管方案配置 | 同左 | 同左 | `NAV_RCL_ACT` 不得弱于任务动作；`COM_RC_IN_MODE` 必须是 0–8 的有效枚举，不能照抄 SIM 的 4 |
| `COM_OF_LOSS_T` / `COM_OBL_RC_ACT` | Offboard 丢失仍须 Hold/Return/Land 保护 | 同左 | 同左 | 读回并核对 0–60 秒和动作；不能用地面链路策略关闭机载控制保护 |
| `GF_ACTION` | Return 及以上 | Return 及以上 | Land 及以上 | 围栏动作不弱于独立安全要求；`GF_MAX_HOR_DIST` 覆盖 home 到运行矩形四角的最大距离，`GF_MAX_VER_DIST` 覆盖最大任务高度，0（禁用）阻断 |
| `BAT_LOW_THR` / `BAT_CRIT_THR` / `COM_LOW_BAT_ACT` | 低阈值高于临界阈值，临界阈值不低于任务包 reserve_energy_wh / energy_wh | 同左，临界时至少 Return | Land 策略可用 Land | 范围分别为 0.12–0.50、0.05–0.50；电池遥测、SOC 和电量时间戳新鲜 |
| `SYS_STATUS` / `ESTIMATOR_STATUS` / `GPS_RAW_INT` / `HOME_POSITION` | 传感器、估计器、GNSS、home 全部健康 | 同左 | 同左 | 必需传感器健康位齐全，估计器姿态/速度/位置标志齐全，GNSS 至少 3D fix、6 颗卫星且 home 已知 |

报告的每一行都包含 PX4 实际值、期望值，以及中文和英文解释。任务包若要接受会改变任务语义的 PX4 更保守动作，必须在签名覆盖的 `accepted_px4_overrides` 中填写参数、`accepted: true` 和现场理由；没有理由的覆盖仍然阻断。

`NAV_RCL_ACT` 管的是 RC 或手动输入丢失，由 `COM_RC_LOSS_T` 计时，与 GCS 数据链是两件事。`COM_RC_IN_MODE` 选择手动输入来源；当前 SIM collector 的值 4 表示忽略全部手动输入。这是既有隔离模拟配置，不能照抄为真实飞机的通用建议；真实飞机应根据实际 RC、MAVLink joystick 和接管方案决定，保留所需接管能力及其失联保护。

`COM_OF_LOSS_T` 管的是 Offboard 控制输入丢失，随仓库携带的元数据默认 1 秒、范围 0–60 秒，动作由 `COM_OBL_RC_ACT` 选择。地面站断线而机载计算机和飞控之间的控制流仍健康，和机载控制进程停止发送 setpoint，是两个故障。不要为允许无地面站完成任务而关闭或大幅延长机载 Offboard 失效保护；实际控制进程、定位或飞控链路故障仍由这条独立保护处理。既有 PX4 参数和板上实测快照可由 `pixhawk_parameters.py` 做只读备份与差异核对，但本功能不会自动改写它们。

## 自主任务包与失联处理（2026-10-15）

飞机可以在完全没有地面站通信的情况下完成任务。起飞前，操作员把任务、地点、区域、路线、返航点、安全范围和预算装入机载任务包，并明确选择本次任务的失联策略。飞行中由 `MissionSupervisor` 按这份选择执行；飞机可以因为预算、监督要求或感知异常而提前采取更保守的动作。地面站提供可选的上传、监看和命令通道，任务结果先保存在机上，通信恢复后再发送。

机载电池估算现在按距离、巡航高度、风和最近各飞行阶段功率计算返航能耗，并在返航加预留量触底时提前停止任务；无法支撑返航时升级 `land_now`。输入、配置、SIM 日志范围与实机校准步骤见[机载能源与返航余量](onboard-energy.md)。电池缺测时明确退回任务包时间预算。

风险等级只给操作员提供选择建议，不会偷偷改写策略。建议为：低风险 `low → continue`，中风险 `medium → continue_then_return`，高风险 `high → return_now`。任务包必须同时填写 `risk_level` 和明确的 `link_loss_policy`；缺少策略就拒绝起飞，不会把建议值自动补进去。即使填写高风险，操作员明确选择的 `continue` 也会保留，机上仍按实际预算、监督要求和感知健康决定是否升级。

### 五种策略和计时起点

`heartbeat_timeout_s` 表示地面心跳的新鲜度，默认 5 秒。最近一次有效心跳距现在达到这个时长，才认定失联。如果任务开始后一直没有心跳，就从监督器启动、任务被接纳的时刻开始计时。这里的心跳必须属于当前 `mission_id`，不能拿飞控心跳或其他任务的心跳代替。

下表中的 T 是 `link_loss_policy.timeout_s`，仅两种带等待的策略必须填写。所有计时使用机载单调时钟，不依赖地面站时钟。

| 操作员选择 | 认定失联后的行为 | T 从哪里开始算 |
| --- | --- | --- |
| `continue` | 继续执行已装载任务。离线几个小时本身也不会触发返航，预算和机上风险判断仍然有效。 | 没有失联时长上限。 |
| `continue_then_return` | 先继续，静默达到 T 后返航。只有先达到心跳失联门槛，才会执行失联动作。 | 从最后一次有效心跳开始；从未收到心跳时，从任务被接纳时开始。因此实际返航时刻是静默同时达到 T 和失联门槛之后。 |
| `hold_then_return` | 认定失联后先悬停等待，最多等待 T；到时返航。等待期间有效心跳恢复，可以继续任务。 | 从达到失联门槛的时刻开始。默认新鲜度 5 秒、T 为 30 秒时，最后一次心跳后第 5 秒开始等待，第 35 秒返航。 |
| `return_now` | 认定失联后先停稳、悬停一个控制周期，再执行返航。 | 不额外等待 T；仍须先达到失联门槛。 |
| `land_now` | 认定失联后选择执行器的 `land` 恢复步骤。当前实际能力限制见本节末尾。 | 不额外等待 T；仍须先达到失联门槛。 |

返航和降落选择会锁定。后续重连、恢复正常的预算估计或 `resume` 都不会重新启动已经放弃的原任务。`hold_then_return` 的等待悬停可以在重连后解除；操作员主动发出的暂停则必须收到 `resume` 才解除。

### 任务包结构与起飞检查

示例文件为 `configs/onboard/mission-package-example.json`。在 `scripts/onboard_policy.py` 使用的策略配置中引用它：

```json
{
  "mission_package": "configs/onboard/mission-package-example.json"
}
```

路径相对仓库根目录解释，也可以使用绝对路径。程序接口也接受直接传入任务包对象。没有 `mission_package` 时，原有单任务和 SIM 任务模板行为保持原样，不创建监督器或这套任务日志。

任务包必须包含下列内容：

| 字段 | 普通说明与结构 |
| --- | --- |
| `mission_id` | 本次任务的唯一标识；地面事件与日志都绑定它。 |
| `task_text` 或 `plan` | 至少提供一个。已有 `plan` 时直接校验并执行；只有任务文字时，先在机上编译成 `MissionPlan`，校验成功后才接纳。 |
| `places` | 命名地点，例如 `{"poi:gate": [40, 0]}`。 |
| `zones` | 圆形区域，例如 `{"zone:yard": {"centre_ne": [0, 0], "radius_m": 20, "risk_tag": "people", "oversight_required": true}}`。风险标签可以是 `people`、`no_fly_buffer` 等；标签本身不等于一个已经实施的禁飞围栏。 |
| `routes` | 命名路线，例如 `{"route:patrol": [[0, 0], [40, 0], [40, 20]]}`；每条路线至少一个点。 |
| `operating_area` | `north_min_m`、`north_max_m`、`east_min_m`、`east_max_m`、`min_altitude_m`、`max_altitude_m`，给出范围和高度上下限。 |
| `budget` | `time_s`、`reserve_time_s`、`energy_wh`、`reserve_energy_wh`，分别为可用时间、时间保留量、可用能量和能量保留量；总量必须大于保留量。 |
| `home` | 明确装载的返航位置，例如 `[0, 0]`。不会把失联时当前位置当成新的返航点。 |
| `risk_level`、`link_loss_policy` | 风险等级和操作员明确选定的策略，例如 `"medium"` 与 `{"action": "continue_then_return", "timeout_s": 60}`。 |
| `oversight_steps` | 需要操作员在线监督的计划步骤 ID 列表；没有时填写空列表。 |
| 可选 `ref_labels`、`heartbeat_timeout_s` | 前者给已知引用加上易读名字，供任务编译使用；后者设置地面心跳新鲜度。 |

地点、区域中心、路线点和 `home` 都使用同一局部坐标系的 `[north_m, east_m]`，单位是米，不是经纬度。高度使用米且向上为正；执行器收到的飞行姿态仍是 NED，第三个坐标向下为正。运行环境负责在装载任务前建立一致的坐标原点。

起飞接纳会检查必填字段、有限数值、预算与保留量、所有命名引用、返航点、区域和路线结构、区域整体是否在操作范围内，以及计划高度和本次起飞高度。监督步骤必须属于当前计划；未装载的地点、缺少返航点、缺少策略、错误引用类型和编译失败都会拒绝任务包。任务中产生的 `finding:N`、`track:N` 是机上动态结果引用，不能靠地面站临时补齐。

`finding:N` 可作为已发现目标的位置引用；`track:N` 只用于 `follow.track_ref`，不能拿来作为 `goto` 等步骤的位置。`survey` 和 `monitor` 必须使用机上已装载的命名区域，不能用没有具体中心和半径的 `zone:all`。这些组合会在起飞前拒绝，避免飞起来后才发现执行器无法解析。

### 飞行中的监督、暂停和升级

当前保守程度按 `continue → continue_then_return → hold_then_return → return_now → land_now` 排序。机上升级只能向右，不能向左；策略更新也只能收紧，不能延长已有返航期限。把“继续一会儿再返航”改成“先等待再返航”时，同样不能借此推迟原定返航时刻。每次判断都把时间、动作和原因追加到机载日志。

机上目前会在以下情况下升级为返航：剩余任务时间或上报剩余能量达到保留量；连续三次感知判断没有有效结论或置信度低于 0.5；失联时当前步骤要求操作员监督；失联时当前位置或下一段拟执行路径经过同时带有风险标签和 `oversight_required: true` 的区域。只有风险标签、没有监督要求的区域，不会单靠标签推翻操作员选择。剩余时间会继续随机载时钟扣减；实际剩余能量需要运行端提供估计，监督器不在这里推算电池耗电。

明确的 `abort` 会先停稳、悬停，再返回任务包中的 `home`；如果已经锁定更保守的降落动作，终止命令不会把它降回返航。`pause` 会暂停原任务。对两种带 T 的策略，主动暂停从首次收到暂停事件起另计 T，不能靠重连、重复暂停或最近一次心跳重置；到时仍未恢复就返航。`continue` 下主动暂停一直等到 `resume`，预算和健康检查仍可升级。`return_now`、`land_now` 下的主动暂停会立即进入相应恢复动作。

### 接入可选的地面事件和预算

策略对象提供 `ground_event(event)`。支持 `heartbeat`、`abort`、`pause`、`resume`、`policy_update`；所有事件必须带匹配的 `mission_id`。策略更新还要提供 `policy`，结构与任务包里的 `link_loss_policy` 相同。示例：

```python
policy.ground_event({"mission_id": "sortie-1", "type": "heartbeat"})
policy.ground_event({"mission_id": "sortie-1", "type": "pause"})
policy.ground_event({"mission_id": "sortie-1", "type": "resume"})
policy.ground_event({
    "mission_id": "sortie-1",
    "type": "policy_update",
    "policy": {"action": "return_now"}
})
```

省略 `timestamp` 时使用本机接收时刻；如需填写，它必须是当前机载单调时钟域内、本任务开始后且不在未来的时间，不能直接使用地面站墙上时钟。过期心跳不能更新当前有效心跳。任务 ID 不匹配、未知事件、错误时间或放松策略的请求会被拒绝并记录原因。任务 ID 校验不代替通信身份认证；实际传输适配层仍要验证来源和权限。

也可以在一次 `act(observation, pose_ned=..., yaw_deg=...)` 的观察中传入可选字段：

```python
observation["ground_events"] = [
    {"mission_id": "sortie-1", "type": "heartbeat"}
]
observation["mission_estimates"] = {
    "remaining_time_s": 300,
    "remaining_energy_wh": 80,
    "low_confidence_count": 0
}
```

两项都可以省略。需要悬停或恢复时，策略会先处理这些字段并输出动作，可以不经过图像和模型调用。配置里的 `ground_events` 只用于初始化事件；持续通信需要运行时适配层把收到的事件送入接口。这里没有自动把 MultiQGC 的 TUNNEL、UDP 或 MAVLink 消息转换成这些事件，也没有新增 GCS 任务包上传协议。

### 离线日志、重连补发和当前边界

启用任务包后，日志保存在工作目录的 `mission_log/`：`mission.jsonl` 逐条追加任务接纳、地面事件、估计、每次判断、感知结论和执行结果；每条记录写入后都会刷新并 `fsync`，同时带机载单调时间和 UTC 记录时间。`summary.json` 通过临时文件和原子替换保存最终汇总，汇总本身也进入 JSONL。已完成返航后如果再升级到降落，会保留旧日志并更新最后的汇总。没有下传链路仍然连续保存结果。重开已有日志时会检查任务 ID 和连续序号；不完整尾行会保留并拒绝直接续写，不能把损坏证据静默截掉。

可选传输通过 `policy.set_downlink_sender(sender)` 挂接。`sender(record)` 在后台线程执行，只有收到接收端确认后才应返回；返回 `False` 或抛出异常会保留该记录，等待后续通知或重连再试。有效任务心跳恢复后，队列从磁盘按顺序发送，确认游标保存在 `downlink-cursor.json`，进程重开后继续补发。队列不把整个离线历史放进内存，缓慢或卡住的网络回调不会阻塞飞行动作。

补发采用“至少一次”语义：发送成功后、确认游标落盘前如果中断，同一条结果可能再次发送。接收端按 `(mission_id, sequence)` 去重。对每次真正的新飞行应使用新的任务标识和工作目录；这份结果日志不负责从断电位置自动恢复执行器的飞行步骤。

本次 `operating_area` 和高度范围用于装载及起飞接纳校验，没有在这里实现每个动作的全程范围约束，也没有重复加入其他会话正在实现的 `SafetyEnvelope`。风险区域检查只处理失联监督升级，不能替代飞控地理围栏。电池、地理围栏、姿态和其他飞控保护仍须开启，PX4 参数配合按前一节执行。

`land_now` 已有策略选择、锁定和日志路径，但目前恢复步骤沿用现有执行器的 `land` 停止占位实现；这次没有验证真实下降、接地或解除武装。不能把收到停止动作或日志中的恢复完成视为飞机已真实降落。真实飞行的降落执行与反馈接入仍需在对应实现中完成。
尚未实现：生成计划的模型微调与机上接线、上表之外的技能、计数任务的测试世界（需要同类多实例场景）、payload 硬件驱动、真实 Orin 峰值测量、各 mission family 的教师数据和闭环训练。deliver 的硬件驱动仍由 host 负责；当前实现只提供 gated 的导航、事件和 ack contract，不宣称已经具备实际释放能力。这些部分不能由本 contract 的模型输出直接宣称完成。
