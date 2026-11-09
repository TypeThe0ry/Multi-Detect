# 三角翼集成飞行门禁记录（2026-11-08）

固定相机与云台变体本轮均通过完整严格冒烟飞行；这两次结果不替代最终集成版本的 21 任务双次基线，也不批准实飞。

`wt/cx-delta-fw` 先通过 `3b817dd1` 合入环境线，保留共用身份确认、云台和飞行逻辑；没有调整门禁阈值。固定相机与云台飞行分别在 22:42、23:12（北京时间）启动；使用的 runtime 代码相同，期间只追加了 `4303051b` 真机前检查清单。两版自然完成并清理自家容器后，工作树 fast-forward 到主集成版本 `ca99a8ac`；该版本包括城市、slot 队列、调度器候选构建及镜像 ID 校验。集成版本的完整 CPU 门禁已由主任务验证通过（76 s），本次仅补结果文档，不重复运行。

## 严格飞行结果

| 指标 | 固定相机 | 云台 |
|---|---:|---:|
| 最终 status / collector 返回码 | pass / 0 | pass / 0 |
| 起飞、RTL、落地 | 全通过 | 全通过 |
| FW 持续段、重定位、指标门禁 | 全通过 | 全通过 |
| 超速恢复、恢复后饱和门禁 | 全通过 | 全通过 |
| FW 高度误差 p95 | 0.805 m | 0.955 m |
| FW 电机饱和比例 | 0 | 0 |
| 回转换电机全停时长 | 0 s | 0 s |
| 回转换高度损失 | 0 m | 0 m |
| 回 MC 后爬升 | 10.65 m | 16.13 m |
| 相机自遮挡比例 / 门禁 | 0 / 通过 | 0 / 通过 |
| 云台 yaw / roll / pitch 误差 | 不适用 | 0.096° / 0.196° / 2.673° |

全部 8 个三角翼严格字段均为 true：`fw_sustain_success`、`fw_reposition_success`、`fw_metrics_pass`、`fw_overspeed_recovery_success`、`fw_overspeed_saturation_success`、`back_transition_success`、`post_transition_climb_success`、`self_occlusion_success`。云台检查从稳定 MC 悬停下发 yaw=0°、roll=0°、pitch=−45°，保留命令前后关节状态与图像时间戳；三轴误差均在既有 4° 门槛内。

## 原始证据与资源边界

- 固定相机目录：工作树 `.tmp/merge-gate-20261108b-fixed/`，episode `episode-0000-814f4133`；原始最终 JSON 在同名 `.log`。
- 云台目录：工作树 `.tmp/merge-gate-20261108c-gimbal/`，episode `episode-0000-8260ffef`；原始最终 JSON 在同名 `.log`。
- 项目父目录 `runs/delta-merge-gate-summary-20261108.json` 保存两版汇总；`delta-merge-gate-{fixed,gimbal}-20261108.json` 保留完整最终 JSON；`delta-merge-gate-20261108.status` 保存退出码。
- 两次均使用原 `multidetect/px4-sitl-gazebo:gpu`，ID `sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2`。**没有使用 lockstep-lifetime-r1 候选**，不能把本结果计入候选时钟的十次验证。
- GPU0 only；两版顺序使用全局 `scripts/sitl_lock.py`，固定版 slot3（14590/14591），云台版 slot4（14600/14601）。自启动 unit 设置 MemoryMax=64 GiB、MemorySwapMax=0。
- 初始启动被旧默认 GPU avoid=0 拒绝，未产生实际飞行；保留原始失败输出，显式 avoid=2,3 后在新目录重跑。云台第一次锁排队受旧 FIFO 头阻塞，停止的只有本轮自行启动且仍在等待的 unit；之后显式 slot4 / priority201 立即取空闲专属槽位，没有占用或抢占 slot0–2，没有重复固定版飞行。

## 2026-11-09：最终候选 PX4 镜像双变体复验

为覆盖时钟修复后的实际运行组合，另在冻结源码 `21dcab8b0f22e48a8c13a6c5472fd3b8b5e57047`、不可变镜像 `sha256:b5c576ce15b8a75935313bc3e8dc4cf9940778226b28e835064d7581c3fe72c6` 上重跑固定相机与云台两版。两次均首次实际飞行通过，无 retry；原镜像的历史结果保留独立记录。

| 指标 | 固定相机 `091e36db` | 云台 `8e19af28` |
|---|---:|---:|
| status / collector 返回码 | pass / 0 | pass / 0 |
| 8 项严格字段、RTL、落地 | 全部 true | 全部 true |
| FW 高度误差 p95 | 1.192 m | 0.962 m |
| 电机饱和比例 / 回转换电机全停 | 0 / 0 s | 0 / 0 s |
| 回转换高度损失 | 0 m | 0.065 m |
| 回 MC 后爬升 | 17.74 m | 7.21 m |
| 相机自遮挡比例 | 0 | 0 |
| 云台 yaw / roll / pitch 误差 | 不适用 | 0.419° / 0.296° / 2.756° |
| LOCAL_POSITION_NED 最大接收间隔 | 0.329 s | 0.364 s |

主任务从原始最终 JSON、episode 元数据与 MAVLink 再次独立核对源码、镜像、严格字段及落地结果，均通过。运行使用 GPU0、64 GiB/Swap0、全局锁的专用 slot3/4 串行执行。仅等待阶段的最初调度因旧 unknown-scope 票据阻塞而退役，未产生飞行；保留空目录和身份守卫审计，再以 priority201 申请同一专用槽位，未抢占 0–2 或修改其它票据。

项目父目录 `runs/delta-candidate-recheck-20261109/output-r2/` 保存 `fixed.log`、`gimbal.log`、两个 episode 和 `acceptance.json`；`independent-acceptance.json` 及两个 `*-independent-review.json` 保存主任务的只读复核。双变体通过不替代 w0000 十次或最终 21×2 基线，也不证明此前机型专属 AI 语义误确认已解决。

## 合 main 前仍需完成

- 最终集成 head 的 21 任务各 2 次回归必须通过。三角翼的独立视点 `two_views` 规则影响共用身份层，不能只凭旧 env-only 基线放行。
- main 合并、push 与 PR #1 转正式须由主任务在最终基线和发布审查通过后执行。本子任务没有执行这些动作。
- 发布审查发现 6 个 CAD 派生 STL，已存在远程 `wt/cx-delta-fw`，本轮未新增、未删除、未改写历史；精确 blob 对照在项目父目录 `runs/delta-publish-asset-audit-20261108.json`。传播到 main 前仍须核对公开范围。
- 真机保护与未实测项见 [真机前检查清单](delta-real-flight-precheck-20261108.md)。`FD_FAIL_P=60°`、`FD_FAIL_R=70°`、0.3 s 持续时间以及 `CBRK_FLIGHTTERM=121212` 已从本轮固定版实例实读；真机仍须重新导出和验证。

## 2026-11-09：630918 transport候选复验及证据边界

另一次独立复验使用冻结 `6f05947031df137904010fba8f691824c59b6811` 和不可变镜像 `sha256:630918d335967a9ac130cde821080e7b45193bc1a876d10f9508d657ece2bcb0`。固定 `fa0527b2`、云台 `1d83eda7` 各一个原始episode、无retry，collector均0/complete。独立从原始帧/depth、steps、MAVLink重算原评价器，固定15项/云台16项required fields及各8项严格字段全部true，重算与原判定差异为空；未改阈值或原输出。

这一通过范围须保留：`rtl_success`仅代表进入RTL；最终落地距起飞原点分别48.611m/24.607m，末条HEARTBEAT仍armed=true，没有精准回家落地或自动disarm证据。logger明确off；fixed的普通短时重定位参考量仍为false，不能与已通过的严格FW环形重定位混用。只有fixed留存独立docker inspect，云台依据同一冻结launcher和不可变镜像launch事件，不声称两份独立inspect齐全。

原始资料在项目父目录 `runs/delta-transport-recheck-20261109/output/`；独立复算目录 `runs/delta-transport-independent-audit-20261109/` 的 `audit.json` SHA256为 `f0bf3a8665059fa883b1de289e18d9af0f12dd9feadeae26846ee4ffefe86621`。原 `run-manifest.json` 中残留的中间overall=false/active_child_pid没有被修饰，最终authority为acceptance、collector原终态与原Invocation journal。

[真机前清单的630范围补充](delta-real-flight-precheck-20261108.md#scope-630918-20261109)列明组件版本、旧445实例参数实读与当前候选未重新实读的区别，以及签名、实装标定、三角翼空中重启和自动disarm的未验收项。两次6f/630 smoke不能直接写成最终8c整套源码、原21×2或真机保护已通过；main/push/PR放行仍由最终真实门禁决定。
