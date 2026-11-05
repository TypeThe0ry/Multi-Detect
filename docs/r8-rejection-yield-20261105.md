# r8 mailbox / postbox 拒绝样本产出审计（2026-11-05）

本报告只读检查 `artifacts/r21id-r8a`，并把离线 builder 输出临时写到 `.tmp/r8-yield-diagnosis/`；验证后已清理。没有改动 artifacts，没有启动 SITL、Docker、GPU 或训练。

## 结论

r8 的缺口有两层原因。采集侧把 `expert_search` 的 teacher 目标设成真实车辆，四视角 intent 也围绕真实目标；小 mailbox 在约 80 m 几何下没有形成 builder 可用的 segmentation 框，postbox 只在少数途经帧形成带 view 的候选。标注侧还有两个窄范围问题：`suv→postbox` 不在 confusable 映射表中，`car→mailbox` 会被“真实目标也属于 car”条件连同干扰物拒绝一起跳过。

已修复 builder 的这两个规则，并加回归测试。该修复恢复显式配对 hard-negative 标签，但无法从不可见的 mailbox 图像制造样本。YOLOE 的 r8 proposals 没有留在 artifacts 中，故不能据此判断当时某帧有没有被 detector 提议；现有 prompt vocabulary 包含 `mailbox` 和 `post box`，没有证据支持先改 detector query。

## 逐级追踪

| world / episode | Confuser 是否进入可用视野 | Detector proposal | Identity candidate | Label builder 结果 |
|---|---|---|---|---|
| `w0001:0` hatchback + mailbox，最终 attempt 3 | 79 steps、158 张 RGB 中没有 mailbox 达到 builder 的 segmentation 阈值（至少 4 个像素且 bbox 两边各 ≥8 px）。真实目标地面距离约 80.09 m；mailbox 在真目标旁约 3.5 m，但模型仅 0.133 × 0.355 × 1.067 m。 | round-state 只有 `collect` 阶段，没有 proposals cache；采集命令是 `expert_search --teacher-target true`。没有 detector 输出可查。 | 未形成可供 builder 使用的 mailbox segmentation box，也就没有 mailbox identity candidate。 | 默认 `distractor` 与 `car→mailbox` 配对拒绝均为 0 行。这里首先是几何/teacher 访问问题，不是标签 builder 把一个可用候选删掉。 |
| `w0002:1` SUV + postbox，attempt 0 | 有 postbox segmentation 候选，但主要来自 search 途经帧；审计可计入 view 的方向样本为 3 行、2 张 RGB，全部 oblique。其余可见候选没有有效 view。真目标地面距离约 69.99 m；postbox 在 SUV 旁约 3.5 m。 | 同样没有 proposals cache；teacher 目标为真实 SUV。未记录 detector 是否曾提议 postbox。 | 有。离线 builder 从 segmentation 形成 `confuser_0002_00_00` 的候选；例如 `rgb/000024_after.jpg` 中 postbox 是 `track-2`，bbox 约 8 × 13 px，派生 view 为 oblique。 | 原 builder 仍可产生普通 `distractor / reject_track` 行，但显式 `suv→postbox` hard-negative 被映射表漏掉。修复后该 direction 有 9 张 paired-confuser RGB 标签行，其中只有 2 张带 view，且都是 oblique；没有增加新的 view 方向或独立 source RGB。 |

两个 world 的 `teacher_intent.json` 都只声明围绕真实目标的 front / oblique / edge_truncated / recovery waypoint，没有 confuser visit。episode 的实际命令也都使用 `--teacher-target true`。collector 支持 `--teacher-target confuser`，可把 look-alike 设为 expert search 目标；r9 应为 paired confuser 增加这类采集，并为其配置对应的 observation waypoints。

### Detector 与 builder 边界

`artifacts/r21id-r8a/pipeline/round-state.json` 只记录 `collect`，没有 `annotate` 或 `build_sft` outputs。r8 audit 的 segmentation-only builder 运行也没有传 `--proposals`。因此 r8 链路没有可核验的 detector proposal 结果，不能说 detector 漏检或命中了。正常 r15 的 `annotate` 阶段会用 `scripts/cache_onboard_proposals.py` 缓存 onboard FAST proposals，再由 `build_slow_system_sft.py --proposals` 消费；YOLOE 使用的通用词表含 `mailbox`、`post box`，但词表存在不代表模型实际给出了框。

builder 的丢弃点则可由代码确定：`CONFUSABLE_NEGATIVES` 原本没有 `postbox → suv` 这一方向；同时 `car → mailbox` 的生成分支会因为同帧真实 hatchback 与 `car` 语义等价而跳过整个 paired-negative 生成。这个条件检查的是同帧的真实目标，却把焦点不同的 mailbox 候选也跳过了。决策冲突键包含 focused candidate，所以 car 正例和 mailbox 拒绝可以同时保留。

修复后在同一批 r8 segmentation 上重建，train 行数从 849 变为 932。修复新增了 `suv→postbox` hard-negative 标签，但 view-qualified paired-confuser source 仍只有两张 oblique RGB；`car→mailbox` 仍是 0，因为当前 episode 没有可用 mailbox segmentation candidate。标签行或 balance copy 均不作为独立帧。

## `w0002:0` 四次失败

round-state 将 `gen_r21id_r8_w0002:0` 记为 `failed`、4 次尝试且没有最终 complete path。逐个 episode artifact 的 `failure` 字段为：

| attempt | 结果 |
|---|---|
| 0 | PX4 仿真时钟在 primitive 的 step 0 停滞，wall timeout 45 s。 |
| 1 | PX4 仿真时钟在 step 48 停滞，wall timeout 45 s。 |
| 2 | PX4 仿真时钟在 step 9 停滞，wall timeout 45 s。 |
| 3 | sensor wire stream 提前结束：期望 276 bytes，收到 0。 |

这是仿真时钟/传感器传输失败，不是 world 几何或 builder 丢标签；该 logical episode 必须排除，不能从其失败 artifact 计样本。

## r9 世界规模与采集设计

规模按 **独立且带 view 的 hard-negative RGB** 估计，不按 episode 的设计帧数、重复标签行或 view balance 上采样估计。postbox 方向的 r7+r8 基数是 44 张（front / oblique / edge_truncated / recovery = 11 / 13 / 13 / 7），分布在 11 个合格 episode，平均 4 张/episode；r8 单个已完成 episode 只产出 2 张，故用它作保守规划 yield。mailbox 方向 r7+r8 仍为 0，不能从它自己的历史 yield 推出有限样本量；以下 mailbox 数量是使用 postbox 的 2 张/episode 作代理，必须先经过 pilot 验证。

| 方向 | r9 建议 | 估算口径 | 预期总量 / view |
|---|---|---|---|
| car→mailbox | 30 worlds × 2 episodes = 60 episodes。前 2 worlds 纳入 pilot；每 world 分别采 1 次 true-target flight、1 次 confuser-target flight。 | 代理按 r8 实际正 yield 2 张/episode，约 120 张新 RGB。若 pilot 未达到该 yield 或某 view 没有 paired label，先改布局/intent，再扩量。 | 按已观测 postbox view 比例估约 30 / 35.5 / 35.5 / 19；需要逐方向实际验收到总数 ≥96 且每 view ≥8。 |
| SUV→postbox | 18 worlds × 2 episodes = 36 episodes。每 world 同样覆盖 true-target 与 confuser-target flight。 | 按保守 2 张/episode 估 72 张新增；加 r7+r8 的 44 张，约 116 张。相比 52 张缺口保留约 20 张余量。 | 按现有 11 / 13 / 13 / 7 的 view 比例分摊，约为 29 / 34 / 34 / 19，均高于 8。 |

world 和 intent 要同时修：

- mailbox 使用正面宽边朝向相机的放置角度，并把 confuser observation standoff 控制在约 10–15 m slant range，或换用更大的 mailbox 资产；当前窄边只有 0.133 m，在 r8 的约 80 m 处小于 1 px 量级，无法形成 8 px bbox。保持真实车辆与 mailbox 同帧可见。
- postbox 已经能形成边缘大小的 segmentation box；将 confuser route 拉近到约 25–30 m，并让四个 waypoint 围绕 paired confuser 布置，避免只靠 target-centered route 的途经帧。
- `teacher_intent` 增加以 confuser 为中心的 front、oblique、edge_truncated、recovery 观测点。当前 collector 的 `--teacher-target` 是整次运行的全局参数，true 与 confuser flight 应分开采集并作为两个 episode roots 合并。
- 正常执行 r9 的 proposals annotate 阶段，按 paired-confuser mask 与 detector boxes 的 IoU 核验 proposal 覆盖。若近距离仍无 proposal，届时再据 cache 检查 detector recall；不要仅凭 prompt 词表改 query。
- 每批收集后从 builder 的 `confusable_pair_type` 过滤，按 RGB 内容哈希去重，并逐 view 检查；只追加真实采集，直到每个方向至少 96 张独立 RGB、四个 view 各至少 8 张。缺 view 时补采该 view，不用 balance copy 补门槛。

因此 r9 的实际修复路径是：**修 world 的可见尺寸和 teacher confuser visits，保留本次 builder 修复，并用实际 proposal cache 验 detector。** 查询词不是已证实的问题。30 个 mailbox worlds 是基于代理 yield 的条件预算，不是产出保证；如果 pilot 仍是 0，就必须继续改场景或路线，增加同样的 world 不会解决不可见性。

## 修复与验证

`scripts/build_slow_system_sft.py` 将 `suv` 加入 postbox 的近类请求映射，并移除了错误地因 true-target 类别匹配而跳过其他 focused candidate 的分支。`tests/test_slow_system_sft.py` 新增真实车/SUV 与 mailbox/postbox 同帧的 builder 回归，验证 hard reject 关注 `track-2`、同一任务的正例关注 `track-1`，且没有模型输入决策冲突。

执行检查：

- `tests/test_slow_system_sft.py -q -p no:xdist`：21 passed。
- `scripts/run_onboard_gate.py -q -p no:warnings`：PASS，134 秒。
- 用 r8 artifacts 做修复前/后 segmentation-only 离线重建，输入只读；输出已在验证后清理。
- 未运行 SITL、Docker、GPU、训练或联网下载。
