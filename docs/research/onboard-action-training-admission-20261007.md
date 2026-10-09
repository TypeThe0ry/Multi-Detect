# OnboardAI 动作训练准入：最小 loader 规格 — 2026-10-07

目标是一次**外部导航动作监督的开发试验**：沿当前可追溯 Qwen3-VL 2B 底座训练独立 adapter/动作 head，不重复只学五字段 grounding。本页是下一轮实现规格，不是已审核数据、已训练结果或永久架构约束；本轮未下载大数据/权重、未运行 GPU/容器。

## 最小记录与模型输入

建议新版本 `multidetect.onboard-navigation-supervision-v1`，一条记录对应原 episode 的一个 step；严格 JSON、拒绝 duplicate keys/NaN/未知字段，hash 对 exact bytes 重算。以下字段是 **loader schema**，不是现 live request 的替代品。

| 字段 | 类型与准入规则 |
| --- | --- |
| `sample_id`, `episode_id`, `scene_id`, `step_index` | 来源绑定的 opaque IDs；step 为非负整数、同 episode 严格连续；不得按图片路径排序猜动作对齐。episode key 包含 source revision，防不同发布同名混样。 |
| `source` | `repository_id/revision/original_split/annotation_record_id`；原 annotation、pose、RGB、转换代码各有 `{path,bytes,sha256}`；附原许可与审核 receipt SHA。metadata hash 不替代实际重开。 |
| `prompt` | `{text_original,utf8_sha256,source_record_sha256}`；保留原指令，不由后处理增加目标坐标/正确方向/任务答案；oracle/assistant 条件另记离线元数据，禁止混入本试验输入。 |
| `frames[]` | 初期 **current + 至多一个严格更早 RGB**；每项 `{frame_id,step_index,path,bytes,sha256,width,height,encoding,source_clock_id,source_stamp,stamp_unit}`。未知 clock/stamp 为 `null`，不能拿文件名、step/fps、host 加载时间补成 sensor time。单条 history 不跨 episode/scene。 |
| `pose` | `{position_xyz,yaw,coordinate_frame,length_unit,yaw_unit,origin,source_record_sha256}`；`origin` 区分 planned pose / simulated observation / measured observation，不能把规划坐标称实际遥测。原坐标/未知单位保留；可进标签校验，但初期不进 RGB+Prompt 模型输入。 |
| `planned_action` | `{raw,raw_record_sha256,kind,translation_m,yaw_delta_rad,unit_verification_sha256}`；`kind` 仅 `translate/turn_yaw/stop`。`translation_m=[forward,right,down]` 仅在坐标、符号、单位已验证后允许；不适用量为 `null`，不凭空填零。LAND、未知 action、单位冲突先隔离并统计，不重标签成 STOP。 |
| `alignment` | `observation_step/action_step/next_step`、证据 SHA；必须证实动作是此 observation 后的下一 planned motion，而非上一动作结果。用相邻 pose/yaw 验算符号、幅度、顺序；允许有来源的容差，容差在读评估结果前固定。原 terminal STOP 可 `next_step=null`，但须原记录证据，禁止复制尾图/补 stop。下一 pose/RGB 只做离线校验，绝不能成为当前输入。 |
| `partition`, `label_provenance` | 整 episode/scene 的 train/dev-validation/dev-test；标签固定为 `external_planned_navigation_supervision`。另记缺失/排除理由；不产生 `executed_action`、expert label、任务 completed 或 training-ready。 |

**实际 processor 入参仅原 Prompt 与已验证 RGB。** opaque sample/source/split IDs、scene、目标绝对位置、未来动作/路线、mask/amodal、`aim_landmark` 与标签只在 host 离线审计。引用 ID 如训练确需输出，由 host 提供当前帧 ID，不训练模型猜哈希或猜未校准 target ID。metadata 不应通过文件名、模板或消息正文泄漏给模型。

## 时间与动作边界

无可信 `dt` 仍可学 **下一步位移、yaw 增量及 STOP 分类**，它是 ordered-step 行为克隆，不是速度/动力学/飞行控制训练。不得除以默认 1 秒，不生成加速度、duration、下一时刻遥测、奖励或控制频率。原 yaw 单位/坐标未知则相应样本不进入 metric loss；只有原 action kind 可验证时，可单独做类别监督并保留 missingness，不能伪称已验证位移。

`STOP` 仅表示外部导航序列停止；它不证明 LAND、回家、抵达、任务完成，更不是 `OnboardTaskAction.HOLD` 的自然语义真值。现 `OnboardFlightPlan` 是 North/East offsets 与 altitude，**无 yaw-only intent**；初期可离线学习 yaw auxiliary target，但不能执行它，也不能将转头变成移动/降落。相对 down 不是现 `altitude_m`，必须有实测姿态、坐标/HOME/高度基准才能转换；缺测保持拒绝。现 [v2 parser](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_vlm_runtime_contract.py:402) 禁无校准 `reference_id` 与未消费的 `speed_mps/duration_s`。因此先使用独立导航 target schema，不伪造完整 task phase/confidence/flight-intent gold 来迎合现 parser；未来新增 typed intent 需单独版本/测试，不能放宽旧合同。

## 准入、预算与独立评估

- **先 CPU preflight，后 optimizer：** 少量完整 episode 验证发布 SHA/RGB 可解码、原 action→next pose 对齐、同名冲突、重复/未来/倒序、坐标符号与单位。OpenFly 示例 negative-index history、未来动作选关键帧、复制尾帧/合成 STOP 不照抄；任何 material mismatch 必须隔离或停下，不能用免责声明变成正确标签。
- **首批预算建议上限 64 个完整原 train episode、最多 4096 个合格 steps、每例最多两图。** 超长 episode 在分区前整段排除，不截成“新独立 episode”；记录实际 counts、bytes、动作分布与缺失率。这是待冻结的开发预算，不是声称已有这些合格数据。大库只按 allowlist 限量读。基座、adapter 父 SHA、来源准入 receipt、像素/token/VRAM cap、最大 updates 与固定评估规则必须在 GPU 前封存；8GB 可行性未证实，不能默认复用 7B 原生路径。
- **不触碰原 `seen/unseen` 或本仓库 grounding val/test 的 optimizer/选样。** 首批从原 train 内按整个 scene 分组划分新开发 train/dev-validation/dev-test；同 episode 不跨组，相同/近重复 RGB 与同轨迹变体一起分组。场景不足就报告只能做 episode-held-out，不宣称 unseen-scene 泛化；这些是开发 holdout，不是新的 blind benchmark。分区 hash 在模型结果前冻结。
- **评价学习内容而非格式：** 按分区/scene/原 episode 报 primitive macro-F1、STOP precision/recall 与非 STOP→STOP 混淆、有效 metric translation MAE、yaw 圆周误差、非法/未知率；有效样本分母与 excluded 单列。与父模型/多数类 baseline 同 exact RGB/Prompt 对比，加入无历史消融，并检查原 grounding 遗忘。不能凭 JSON 合法率晋升，也不把相关 steps 当独立 flights 做显著性结论。初期没有闭环回报，不能报告 navigation success/collision-free/任务成功率。

**本轮可以实现：** create-only train-only loader/审核报告、无未来 RGB+Prompt 的 planned-action supervised dataset、预算封存后独立 Qwen 导航 adapter 或小 head。**不能做：** 混入原测试失败样本、移植 Llama/OpenVLA LoRA 到 Qwen、从 scenario truth 填现场状态、将 teacher/规划动作/ACK 当 PX4 executed、无 dt 的速度/动力学 loss、直接发 MAVLink/arming/actuator 或绕安全事务。

后续真正动作闭环仍接 [live request](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_live_vlm_contract.py:293) 与 [episode recorder](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_live_episode.py:106)，分别保留 proposed/accepted/实际 received/executed/completed 与真实后继 RGB/state。外部计划标签只能启动有意义的学习，不能自动升级为该链路的执行事实或飞机权限。

依据：[OpenFly 锁定 builder（未来帧/单位差异）](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/train/dataset_builder/vln/vln_dataset_builder.py)、[OpenFly 论文 action 定义](https://arxiv.org/html/2502.18041v3#S5.SS3.SSS3)、[实际无 clock/dt 的小型 pose 原件](https://huggingface.co/datasets/IPEC-COMMUNITY/OpenFly/blob/a12316d56a4e35a32ad626fb725ed7089937a1c4/Image/env_airsim_16/astar_data/high_average/2025-01-18_23-38-59_526011/pose.jsonl)、[AeroVLA 锁定 wrapper（目标坐标提示/宽松 action parser）](https://github.com/XuPeng23/AeroVLA/blob/2c5ae0987a484ab92f00dd9d9ed493cb3e98e492/src/model_wrapper/aerovla_wrapper_ui.py#L78)、[TravelUAV 论文（人工模拟轨迹/后采图/oracle/backtracking）](https://arxiv.org/html/2410.07087v2#S4)。完整候选身份/许可及未验证边界见 [前一份调查](D:/Projects/Multi-Detect/docs/research/onboard-uav-action-model-candidates-20261007.md)，本页不重新选型。
