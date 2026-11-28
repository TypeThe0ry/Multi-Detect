# X1FL 闭环回归门禁

同一轮顺序运行所选用例。一个本机 `llama-server` 加载同一 Altair GGUF，为起飞前规划和飞行中的决策提供服务。每次尝试先验证模型生成的任务图，再把场地清单绑定到任务和故障范围。起飞前用同一份模型原文重新校验同一个任务。飞行中的 action 由 Altair 提出，执行器只做准入和执行，PX4 保留原有安全动作。

## 一键门禁

在包含本补丁的 Multi-Detect 工作树根目录运行。工作站需要已部署的 Iowa SDF、场地清单、风速插件、模型服务器和 SITL 镜像。

```bash
GGUF=/absolute/path/to/altair-x1fl.gguf
export RTF_MATRIX_START_LOAD_MAX=60
scripts/x1_model_gate.sh "$GGUF" --cuda-device 1 --label x1fl-s31
scripts/x1_model_gate.sh "$GGUF" --label x1fl-s31-repeat \
  --baseline /data/scratch/cx/altair/x1-gate/x1fl-s31
```

模型服务器默认使用 CPU。`--cuda-device N` 指定 Altair 的物理卡，SITL 渲染使用既有的 GPU 选址与准入流程。可用 `--llama-server PATH` 指定与导出兼容的本地构建。seed 31 历史运行使用 `/data/scratch/llama-b11998/build-cuda/bin/llama-server` 和 `--port 9963`。

入口检查 GGUF 文件头，计算 SHA-256，并比对 `--sha256 HEX` 或导出目录中 `SHA256SUMS` 对该文件的条目。有发布摘要时请提供或保留它。仅有 GGUF 时，报告记录重复读取校验。两个阶段之间及运行后再校验摘要与文件身份。门禁先运行整套 plan-only 预检，成功后才启动 live suite。整个过程持有 X1 主机独占飞行锁。若现有 X1 作业持锁，退出码为 `75`，此时两个阶段都不启动。`RTF_MATRIX_START_LOAD_MAX`、共享 GPU 准入标记和调度状态由主机管理，脚本沿用套件的槽位轮试与资源重试。`0` 表示门禁通过，`1` 表示失败，`2` 表示命令或配置错误，`130` 表示中断。

原始运行写入工作树 `.tmp/`，随后复制到 `/data/scratch/cx/altair/x1-gate/<label>/`，可用 `--output-root DIR` 改址。标签目录必须是新的。`gate.json` 最后发布，是报告齐全的标记。该目录保留 `gate.md`、`gate.json`、原始套件产物、汇总 SFT 失败轮与单独的 `model-failures.jsonl` 和 `evidence-scope-rejections.jsonl`。原始审计内的路径保留采集时原文。`--baseline DIR` 读取之前的 `gate.json`，报告样本数、数值差、判定变化和门槛是否一致。终端打印一行训练组可转发的摘要。

## 指标和通过条件

用 `--profile PATH` 选择门禁阈值。默认文件为 `configs/simulation/x1-model-gate-v1.json`。门禁要求全部用例的既有规划、回执、执行效果和落地检查通过，并应用下表的模型阈值。分母为空显示无样本并判为失败。失链用例已观测到可评分的首轮、且 oracle 未要求工具时，工具首轮检查适用豁免，报告记录原因。`target_found` 与 `battery_drop` 各自必须采到 oracle 要求工具的事件首轮。

| 指标 | 默认阈值 | 计数方式与理由 |
| --- | ---: | --- |
| Schema 合法率 | 100% | 每个已发送模型轮。非法 wire 不能提供可执行决定 |
| 标识回显 | 100% | 决策回显本轮 `request_id` / `event_id`，并通过协议的 mission/revision 核对。工具协议用 `request_id` / `call_index` 关联本轮，无 `event_id` 字段 |
| 安静 cadence 多余动作 | 0 | 安静 cadence 请求中的非 `continue` action。缺少安静样本会失败 |
| 决策 deadline 超时率 | < 1% | 已知 deadline 结局中超时的比例，未知结局单独报告。100 次中 1 次即失败 |
| 模型轮延时 >5000 ms | < 1% | 有 backend 延时样本的轮。用独立尾延时回归指标补充票据时限，保留两种延时的直方图 |
| 事件工具优先 | 100% | 同 `build_x1_flight_loop_sft.policy` 判断当轮是否应调用只读工具，核对工具及票据顺序。每种事件样本少，单次偏离足以触发回归 |
| Retask 结构 | 100% | 所有 proposal 的 `params.plan` 必须是完整 `x1_plan`，通过 wire 及本次任务包的 `validate_plan` |
| Retask 时机 | 100% | 每次 proposal 都须在 oracle 确认证据满足条件后提出。证据跟随用例还各自要求拿到 retask 样本 |
| 低电量结构 | 100% | 低电量窗口末轮动作按 wire 评分。`return` / `land-now` 的 `params` 必须为空。错误包装的 `retask` 进入失败集。工具优先与标识分别评分 |

如果合规动作被执行器因缺少与本轮绑定的运动证明或净空证据而拒绝，报告保留其真实回执与未达成的飞行检查，并单列证据范围拒绝。模型阈值按模型输出与 oracle 计算。带有协议或时序错误的轮仍可进入模型失败集。整体门禁仍需要飞行验收通过。

## 独立运行闭环套件

在已应用本 lane 补丁的较新 Multi-Detect 工作树根目录运行。以下命令使用工作站上已有的 Iowa SDF、场地清单、部署模型和风速插件。用 `--world-root`、`--models-root`、`--site-inventory`、`--wind-plugin` 或 `--mission-package` 可替换相应的本机输入。默认模型服务器使用 CPU，SITL 渲染仍按原有流程申请 GPU。`--cuda-device N` 会显式把模型服务器放到物理卡 N。

```bash
GGUF=/absolute/path/to/altair-x1fl.gguf
export RTF_MATRIX_START_LOAD_MAX=60
scripts/run_x1_closed_loop_suite.sh --list-cases
scripts/run_x1_closed_loop_suite.sh --checkpoint "$GGUF" --plan-only \
  --output "$PWD/.tmp/x1-closed-loop-plan"
scripts/run_x1_closed_loop_suite.sh --checkpoint "$GGUF" \
  --output "$PWD/.tmp/x1-closed-loop-live"
```

用 `--case mc-hover-waypoint-return --case quiet-cadence` 选择一个子集。指定输出目录必须是仓库 `.tmp/` 下的新目录。用例按表顺序运行。默认从 slot 7、6、5 轮试，守卫检查占用、显存、磁盘与 `RTF_MATRIX_START_LOAD_MAX`。资源类失败每 30 秒重试，最多 8 次且计入用例时限。共享 GPU 标记由原有守卫读取，套件记录启动时的标记状态。模型服务器和每次尝试各自记录 PID，终止时只处理启动的子进程。清理时间计入用例时限。若达到强制结束阈值，应检查对应尝试日志和已记录的容器状态。

## 用例

下表为排程估计与硬上限，包含规划和仿真。模型启动另有 180 秒默认上限。飞行中沿用既有执行器和工具窗口时限。Cadence 列为巡航请求秒数，其他阶段按原有采样规则缩短间隔。

| 用例 | Cadence / 工具次数 | 预期观测 | 预估 / 硬上限 |
| --- | ---: | --- | ---: |
| `mc-hover-waypoint-return` | 2 / 0 | 垂直起飞后悬停、MC 航点到达、返航、MC 落地。有效 `continue` 且无额外动作 | 6 / 15 分钟 |
| `vtol-transect` | 1 / 0 | FW 航点通行、安静巡航有效 `continue`、零额外动作、返航落地 | 5 / 15 分钟 |
| `ramped-wind` | 0 / 2 | 插件斜坡注入、MAVLink 风观测、恢复、有效 `continue`、返航落地 | 5 / 15 分钟 |
| `mc-link-loss` | 0 / 1 | MC 实测失链、首轮按 oracle、Altair `return` 回执和 PX4 接管、返航落地 | 5 / 15 分钟 |
| `mc-battery-drop` | 0 / 1 | MC 段电量下降，所需工具先于合法终局动作，接纳 `return` 或 `land-now` 后确认相应落地 | 5 / 15 分钟 |
| `evidence-follow-event` | 0 / 3 | `target_found` 事件票据、按 oracle 优先调用工具、证据齐备后的合法 retask 安装记录、证据航线和落地 | 7 / 20 分钟 |
| `evidence-follow-cadence` | 1 / 3 | `target_found` 进入 cadence 请求，retask 安装和落地 | 7 / 20 分钟 |
| `quiet-cadence` | 4 / 0 | 只有周期请求，零额外动作、返航落地 | 5 / 15 分钟 |
| `long-range-transect` | 10 / 0 | 单航点约 3.5 km 去程、巡航 cadence 持续决策、零额外动作、返航落地 | 14 / 30 分钟 |
| `long-range-wind` | 10 / 0 | 同一单航点航线，巡航 cadence 持续决策，中途注入 `wind_step` 并观测、恢复、返航落地 | 14 / 30 分钟 |
| `corridor-dogleg` | 10 / 0 | K 短折线两个 POI、计划内 FW 返航、FW 巡航 `continue` 获准、MC 落地。需显式选中 | 5 / 15 分钟 |

`continue` 用例允许执行器因当前 MotionProof 不足而安全拒绝。验收要求真实 `continue`、相应回执、无额外执行动作及落地。无故障用例的每个适用 cadence tick 都需要满足决定与回执条件。证据范围拒绝记录保留原始回执。`return`、`land-now`、`retask` 需要执行器接纳及对应的 PX4 或航线安装证据。`corridor-dogleg` 是例外：它的正式验收只要求 FW 巡航 `continue` 被接纳，其余阶段按“走廊证明器未覆盖”记录，不作为通过条件。

`mc-hover-waypoint-return` 是唯一全程 MC 的用例，使用独立的 nominal-MC 编译器；实跑规划给出 `vtol_takeoff → x1_mc_reposition → return_home`。它不在 `ASSUMED_SIM` 范围内。`mc-link-loss` 与 `mc-battery-drop` 的 `mc` 指故障和决策所在的返航阶段。两者与两个 evidence-follow 用例都向模型提交 `Fly over the yard at 85 m AGL, then return home and land.`；模型给出垂直起飞、切 FW、飞越 `poi:yard`、切回 MC、返航落地的五步计划。失链和电池窗口分别是路由时钟 50–58 s 和 50–62 s，均单独绑定在同一 Iowa 航线上；实飞阶段仍按遥测判定，不以窗口推定模式。

`ASSUMED_SIM` 仍要求完整任务包、任务图、运动范围、故障窗口、插件、Iowa SDF/DEM、机型、镜像和经校验的场地边界匹配。只有模型 `goal` 和步骤命名（包括两条分支引用）被规范化；任务原句、报告、技能、参数、顺序、分支拓扑等仍参与绑定。`result.json` 的 `plan_json` 和 `raw_planner_output` 保留模型原文，`canonical_plan` 记录执行侧图。编译步骤、base hash、运动范围及初始决策请求／审计使用同一份规范化图。Altair 飞行中提交的 retask 本身不经改写。重新运行时应从新预检生成站点绑定，不复用旧 base hash。

`long-range-transect` 与 `long-range-wind` 使用独立任务包 `configs/simulation/x1-longrange-mission.json`，只有一个东北向航点 `poi:far-marker`（home 约 3.5 km，仍位于 6 km 世界的方形边界内），模型给出 `vtol_takeoff → fw_transition → fw_goto_waypoint → back_transition → return_home`。二者都带 `assumed=True`，读取独立的 `configs/simulation/x1-assumed-sim-bounds-iowa-longrange.json`；该文件只在 `ASSUMED_SIM` 入口被校验，声明 `SITL-only` 且把 `max_distance_from_home_m` 放宽到 4000 m，`configs/onboard/x1-planner.json` 的 1500 m 真机包线保持不变。不带该标志加载同一任务包仍会被 1500 m 上限拒绝。`long-range-wind` 的 `wind_step` 窗口在 `configs/simulation/x1-longrange-wind-faults-v1.json`。巡航 `continue` 仍必须通过既有 MotionProof 与能量/时间预算；本用例不新增多段走廊证明。

两个长航程用例保留为单航点回归，默认套件略过，需用 `--case` 显式选中。验收要求航点通过、返航落地、两轮以上巡航 cadence 决策，以及 PX4 仿真时钟从起飞完成到落地至少 600 秒。当前 3,465 m 航线按现有机型预算约需 146.8 Wh，可用能量为 59.2 Wh，预检会以 `X1_ENERGY` 拒绝。航点位于东北方向，正北 3.5 km 会越出当前 Iowa 方形场地。

`mc-battery-drop` 的 17% 注入只获准进入这个 SITL 试验，**不**代表原生动作会获准。观测门限至多 19%，现有故障 SOC 下界再扣 54 个百分点，因此该样本的能量下界为 0 Wh。执行器不能为了验收改发或放行动作；没有满足原生动作和落地证据时，该用例仍报失败。这次范围修订不改变现有通过条件。

### 多段固定翼走廊 `corridor-dogleg`（K 短折线）

`corridor-dogleg` 是 `ASSUMED_SIM`、仅 SITL 用例，须用 `--case` 显式选中。它用 `configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json` 绑定 `configs/simulation/x1-corridor-dogleg-mission.json`。任务是先向北飞往 `poi:first-beacon`，再向东北飞往 `poi:second-beacon`，然后以固定翼直接飞越 home，最后 `back_transition` 并 MC 落地，全程 75 m AGL。名称与方位一致，不再沿用“west/east 却指向北/东北”的旧标签。几何为 home `(0,0)`、`first-beacon (60,0)`、`second-beacon (30,52)`，三条 FW 腿合计 180.07 m；home、走廊矩形、预算与储备均保持不变。

本用例采用净空诊断选出的 K 候选。将巡航抬到 75 m AGL 并缩短末腿后，后过渡 505 m 跑出区仍落在原围栏与障碍库存内、不靠外移边界：最坏 DEM 余量 **+8.024 m**（后过渡末片 C4.16），最低预测过渡 AGL **48.024 m**，围栏最小余量 **+48.384 m**，FW 巡航最小障碍余量 **+59.844 m**，T2/T3 最小 DEM 余量 **+10.754 m**。旧 47 m 几何仍在后过渡扫掠上缺少地形余量（CPU 回放为 −23.95 m），保持拒绝侧对照。

**验收口径（协调方决定）**：正式验收只看 **FW 巡航阶段的 `continue` 被接纳**。起飞/MC、前过渡/MC、`back_transition`、MC 返航/approach 的 `continue` 不在走廊证明器覆盖范围内，如实记录为“走廊证明器未覆盖”；它们被拒绝不构成本用例通过，也不据此放宽任何判定。为这些阶段补走廊 `continue` 证明的设计选项 B 明确不做。CPU 复放中，K 在 7 个合规 FW 巡航样本全部被接纳，19.55 m/s 超速样本照常被 `current_fw_airspeed_limit` 拒绝，后过渡发令前仍为 FW 的补充入口也被接纳。

**能量警示**：K 在请求 #12 只剩 **0.085 Wh** 的模型余量（扣 14.8 Wh 储备后）。这个数字用于证明走廊 `continue` 的机制与净空，**不是可泛化的航程余量**，不能外推到更长航线、其他风或真实机体。K 的静态距离与能量余量只是本用例的准入条件；所有数值均为 SITL/`ASSUMED_SIM` 假设。

本用例采用 OP-A。FW 腿证明功率使用模板的 500 W，74 Wh 名义电池扣除 20% 储备后为 59.2 Wh，standoff 门关闭。选线时用 `x1_corridor_energy.max_one_way_m` 做能量量级校核，再用 `x1_static_violations` 校验实际计划的逐腿几何和预算，包括 `X1_ENERGY_PROOF`。在同一首个 FW tick 的证明层口径下，静态距离余量目标为至少 10%，按 `1 − (FW 总路程 / 2) / 证明层最大单程距离` 计算。单程距离表、风界及原生动作准入见[走廊 MotionProof 接口](x1-contingency-interface.md#多段固定翼走廊)，净空诊断见[走廊净空](x1-corridor-clearance.md)。

先在工作树 `.tmp/` 的新目录跑 `--plan-only`。预检通过后，SITL 使用同一模型和用例、另一个新的输出目录。

```bash
scripts/run_x1_closed_loop_suite.sh --checkpoint "$GGUF" --cuda-device 0 --case corridor-dogleg \
  --plan-only --output "$PWD/.tmp/x1-corridor-dogleg-plan" &&
scripts/run_x1_closed_loop_suite.sh --checkpoint "$GGUF" --cuda-device 0 --case corridor-dogleg \
  --output "$PWD/.tmp/x1-corridor-dogleg-live"
```

闭环验收核对 lowering、静态准入、FW 巡航 `continue` 至少两轮被多腿 MotionProof 接纳、完成去回程及 MC 落地，以及无额外动作。报告记录 FW 巡航接纳次数/总数、其余阶段的拒绝码分布并标注“未覆盖”、最大离家距离、PX4 仿真时钟从起飞完成至落地的耗时，以及失败时的模型、证明或基础设施原因。

`--recorded-output` 可用于该走廊的静态预检；带实时决策服务的独立 demo 不允许用它飞行，会在启动仿真前退出（`recorded output requires --plan-only or --dry-run`）。本次机制验证沿用套件已验证的做法：通过 `shared_post` 注入记录计划（**不是模型规划**）跑 plan-injected SITL，Altair 只做飞行决策。历史 47 m plan-injected 飞行已走通三腿执行机制与落地，但 16 次 `continue` 提议 0 次被证明层接纳；K 被采纳正是为了让 FW 巡航入口在净空和能量上可被证明。真实模型 `--plan-only` 是否给出同一计划不在本用例的验收范围内。

## 结果

- `CASE/result.json` 含逐次尝试、检查项、动作回执和原始 runner 结果。每个尝试保留 `worker.json`、`runner.log`、`preflight/result.json`，以及已进入飞行流程时的 `flight/result.json` 和 `decisions.jsonl`。
- `summary.json` 包含计数、检查项和延时直方图。`summary.md` 汇总动作、工具、retask、安静巡航额外动作、schema、超时和失败原因。空样本分母显示为无样本。
- `failures.jsonl` 按 SFT turn 结构保留原样请求和模型原文、执行器回执及原因。`messages` 内的 assistant 训练标签直接来自 `build_x1_flight_loop_sft.policy(request)`。用例期望可以额外标记偏差，oracle 只使用已发送的请求字段。先开票但还未发送的请求保留 base request，无法计算的标签留空并记录原因。`decisions.jsonl` 保存精确的已提交 HTTP 请求体。

运行退出码 `0` 表示所有用例符合请求的模式，`1` 表示有失败、预算耗尽或准入阻塞。`--plan-only` 的通过只涵盖规划和预检。

## 可选 systemd 示例

`docs/x1-model-gate.path.example` 与 `docs/x1-model-gate.service.example` 是未安装、未启用的示例。由主机管理员按需使用。示例监视 `ready` 文件的变更，服务读取 `/etc/x1-model-gate.conf` 中显式固定的 `X1_GATE_GGUF` 和 `X1_GATE_SHA256`。管理员需先确认工作目录、运行账号、调度配额和触发路径，再用已验证导出更新配置。繁忙退出按主机策略重触发。
