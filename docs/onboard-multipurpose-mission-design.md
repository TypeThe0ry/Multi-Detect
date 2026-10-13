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
| `deliver` | 投递或释放 payload | `target_ref`, `payload_ref` | `arrived` | 目标确认 | `payload` capability 默认关闭 |
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

`survey`、`patrol`、`follow`、`monitor`、`deliver` 目前按 `skill_not_implemented` 走 `on_fail`。任务计划暂由主机模板生成（`plan_for_find_task`、`plan_for_find_and_inspect`），慢系统从任务文字生成计划还没训练。

仿真实飞（Gazebo + PX4 SITL，慢系统 v2-r4-prop）：“找到 → 绕飞 4 个视角 → 返航”在 gen_v2_w0035（SUV）和 gen_v2_w0038（邮筒）上都完整执行，分别 46 步和 32 步，定位误差约 0.5 m，4 个视角都拍到并得到身份答案，最后回到起飞点。

**从任务文字生成计划（2026-10-14）**：`src/multidetect/autonomy/mission_planner.py` 按技能注册表生成逐技能的严格 JSON schema（参数名、类型、范围都固定），提示里给出技能清单、引用规则和 6 个短示例；`repair_plan_document` 确定性修复常见错误（“失败就重试”的回边、缺少或悬空的终止步骤），然后再走 `validate_mission_plan`。在 30 条手写中英文任务（`configs/onboard/plan-eval-tasks.json`，含找、计数、检查、巡逻、测绘、监视、跟随、悬停、降落及组合）上，未微调的 Qwen3-VL-2B Q4_K_M：计划 100% 有效，技能序列 70% 正确（严格 20/30），开发机中位 1.5 s。最初只有 2 个示例时只有 53% 有效、23% 正确。剩余错误主要是多加“记录 / 检查”步骤（例如把“记录位置”理解成拍照记录）。

尚未实现：生成计划的模型微调与机上接线、上表之外的技能、计数任务的测试世界（需要同类多实例场景）、payload 硬件驱动、真实 Orin 峰值测量、各 mission family 的教师数据和闭环训练。这些部分不能由本 contract 的模型输出直接宣称完成。
