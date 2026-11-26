# X1FL 闭环预验收

同一轮顺序运行所选用例。一个本机 `llama-server` 加载同一 Altair GGUF，为起飞前规划和飞行中的决策提供服务。每次尝试先验证模型生成的任务图，再把场地清单绑定到任务和故障范围。起飞前用同一份模型原文重新校验同一个任务。飞行中的 action 由 Altair 提出，执行器只做准入和执行，PX4 保留原有安全动作。

## 运行

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
| `mc-link-loss` | 0 / 0 | MC 实测失链、Altair `return` 回执和 PX4 接管、返航落地 | 5 / 15 分钟 |
| `mc-battery-drop` | 0 / 1 | MC 段电量下降，接纳 `return` 或 `land-now` 后确认相应落地 | 5 / 15 分钟 |
| `evidence-follow-event` | 0 / 3 | `target_found` 事件票据、工具证据、retask 安装记录、证据航线和落地 | 7 / 20 分钟 |
| `evidence-follow-cadence` | 1 / 3 | `target_found` 进入 cadence 请求，retask 安装和落地 | 7 / 20 分钟 |
| `quiet-cadence` | 4 / 0 | 只有周期请求，零额外动作、返航落地 | 5 / 15 分钟 |

`continue` 用例允许执行器因当前 MotionProof 不足而安全拒绝。验收仍要求真实 `continue`、相应回执、无额外执行动作及落地。无故障用例的每个适用 cadence tick 都需要满足决定与回执条件。拒绝会进入失败样本。`return`、`land-now`、`retask` 需要执行器接纳及对应的 PX4 或航线安装证据。

MC nominal 编译器仅供套件的 Iowa SIM 用例使用，保留既有 FW 编译路径。MC cadence 仍受执行器 MotionProof 约束。绑定 native／retask 的 `ASSUMED_SIM` 模板要求模型任务包与任务图完整哈希匹配，并核对运动范围、故障窗口、Iowa 场景和镜像。模型改变任务图元数据也可能触发 `blocked_evidence_scope`。新 MC 电池时间窗需要独立作用域证据才能起飞。`--plan-only` 产出待比较的模型任务、base hash 和运动范围。已知的模板绑定不能通过更换失败样本标签来解决。

## 结果

- `CASE/result.json` 含逐次尝试、检查项、动作回执和原始 runner 结果。每个尝试保留 `worker.json`、`runner.log`、`preflight/result.json`，以及已进入飞行流程时的 `flight/result.json` 和 `decisions.jsonl`。
- `summary.json` 包含计数、检查项和延时直方图。`summary.md` 汇总动作、工具、retask、安静巡航额外动作、schema、超时和失败原因。空样本分母显示为无样本。
- `failures.jsonl` 按 SFT turn 结构保留原样请求和模型原文、执行器回执及原因。`messages` 内的 assistant 训练标签直接来自 `build_x1_flight_loop_sft.policy(request)`。用例期望可以额外标记偏差，oracle 只使用已发送的请求字段。先开票但还未发送的请求保留 base request，无法计算的标签留空并记录原因。`decisions.jsonl` 保存精确的已提交 HTTP 请求体。

运行退出码 `0` 表示所有用例符合请求的模式，`1` 表示有失败、预算耗尽或准入阻塞。`--plan-only` 的通过只涵盖规划和预检。
