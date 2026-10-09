# OnboardAI 下一轮训练数据审计（2026-10-07）

本次只重开本地数据和已有结果，没有运行 GPU、模型、飞行或外部 SIM。结论是：可以沿已有开发 LoRA 做一次有真实像素事实的有限视觉 grounding 增量；当前数据不能支持动作策略或世界模型动力学训练。新数据仍需独立审核，模型质量门和发布门不因此通过。

## 有意义的当前增量

来源为 `artifacts/training/onboard-blender-sdf-replay-merged-20261007-r4-r2/manifest.json`。已实际重开全部 1,344 个 RGB、可见 mask、amodal mask 和 YOLO 标签，核对图像尺寸、像素数量、遮挡比例、半开 bbox、标签和 source/SDF/world SHA；不是仅检查 manifest 计数。

| 原分区 | 帧数 | clear / partial / fully occluded / absent | 原可见阈值正例 / 零 mask / 模糊 | 合格参考帧 |
| --- | ---: | --- | --- | ---: |
| train | 832 | 123 / 164 / 353 / 192 | 276 / 545 / 11 | 249 |
| val | 256 | 64 / 80 / 64 / 48 | 137 / 112 / 7 | 124 |
| test | 256 | 64 / 83 / 61 / 48 | 141 / 109 / 6 | 127 |

train 的 832 帧来自两组各 384 帧静态课程、16 帧旧 train clip 和 48 帧原 train clip suite。三种 train 几何为 original 288 / rover_long 272 / cargo_cart 272；RGB SHA 均唯一。merge 没有创造新像素，也不把每张静态图当独立动作 episode。

旧 grounding 数据为 110 条，train/val/test = 26/41/43，独立原 clip 数 = 2/3/3。全部实际 RGB、裁剪、mask/label 和独立审核已重开。旧 train 的 26 个当前画面已经包含于 r4 train；扣除这些当前画面和旧参考源后，不能把重复数据算作新的监督。

新 builder `scripts/build_onboard_vlm_grounding_increment_dataset.py` 预先固定 seed 20261019 和最多 128 条新增 train。实际候选池为 visible 232 / fully_occluded 345 / absent 192；新增配额为 43/43/42。原 train 当前画面 26、模糊帧 11、不够清晰作为自身参考的正例 26 均排除，不能按 val/test 模型失败选择训练图。旧 110 条记录及其 RGB/裁剪、模型输入原样保留；新数据总计 238 条，train/val/test = 154/41/43。

新增正例要求实际 visible pixels ≥256、visible fraction ≥0.25，并使用该当前 RGB 自身的可见矩形 crop 作 reference；这是 same-frame visual grounding，不是跨帧身份或时序记忆监督。原可见标签阈值仍为 ≥128 pixels 且 fraction ≥0.10，zero-mask 为 not_confirmed，介于其间的模糊样本不参与新增监督。reference bbox 是半开区间，不能对右下角再加 1。

新增负例使用同 world、同几何的合格 train reference，current 必须有实际零可见 target mask。离线场景 primitive 检查拒绝另一整组完全相同几何的实体；这不证明任意干扰物在视觉上不相似，必须独立查看真实 RGB。reference 表示几何外观范例，不表示与负例中的隐藏实体有真实实例身份对应。所有模型输入最多 reference/current 两图、原 Prompt、opaque IDs 和明确 unknown 的 aircraft_motion/GPS/NED；不含 mask、amodal、场景条件、源路径或动作答案。

新 train 与旧 grounding val/test 没有当前 RGB、world 或原 clip 交集。val 与 train 共享 original/rover/cart 三种几何；test 的 utility_van 不进入 train。旧 coastal/van 已用于开发诊断，因此保留的 test 不是新的完全未触碰 blind benchmark，也不能把复用 scene 叫独立世界提升。

## 为什么不是重复只学格式

已有 LoRA 确实执行过 26 个 train optimizer steps，但原 native 退出码为 1：保存后重新加载比较失败，原 failed report 未改写。另有独立 CPU reload 验证 native 0，不覆盖原 native 1。26 步 adapter 可作为隔离开发 warm-start，不能自动晋升或替换 r33/原 detector。

84 个固定 val/test 输入的实际原始输出已成对核对。仅抽合法 visibility 枚举的诊断：parent TP/TN/FP/FN = 35/22/4/23，candidate = 56/16/10/2；balanced accuracy 从 0.724801 到 0.790451，增加约 6.565 个百分点。但 candidate 的零 mask 假阳性从 4 增为 10，目标 ID 正确从 84 降为 73；parent 83 条 strict JSON/unknown 字段问题与 candidate 11 条 invalid 也说明大量 strict-score 增益来自格式学习。不能把这个诊断增益当 promotion。250 个交叉图对复用了 84 帧，不是 250 独立 episode。

下一轮能做的实质修正是：沿同一 adapter，在保持原 holdout 输入不动的情况下，用新增真实背景、实体屏障后零 mask 和 absent 图训练低层可见性，并保留旧 train 回放防忘记；正例自身 crop 避免随机材质造成错误指代。opaque ID 仍是协议字段，不是视觉智能或身份能力。训练后必须分别报告 strict-format、raw visibility、ID 正确率、零 mask 假阳性以及原回归，不能只报告 JSON 通过率。

## 动作和时序数据仍缺什么

旧 clip RGB 是实际离线 Blender 连续渲染，具有 frame_index/dt/visible/amodal，但没有真实 PX4 executed_action 或同步飞行状态。静态 r4 数据更没有可学习的 action → next RGB 对。将可见性、遮挡计划或 scene 坐标转成航向/速度标签会制造动作真值。

本机 5 份 trajectory-staging 的 403 个原始 RGB/depth payload 已重开；只有 4 份 recording、303 帧通过完整 episode/recording/profile/scenario 身份关联。urban r1 的 100 帧因身份 mismatch 被拒，不是 5/5 准入。四个有效 episode 均为 train，无独立时序 holdout、原始 HOME_POSITION 事件为 0、动作标签为 None。不同记录 canonical 40 features 中 34/38/34/35 项恒定，分别占 85%/95%/85%/87.5%；七个 context 字段为默认/代理值：airspeed、battery、estimated_flight_time、geofence、map_cache、wind_east、wind_north。原 raw GPS/姿态/速度有变化，不能笼统写成原遥测完全恒定。

当前 HOME 在各 episode 内固定为首 GPS，并非每帧重建成 aircraft_position，但缺实际 HOME_POSITION 来源。host_capture、gazebo_sensor、utc 三域不能混作同一个 dt；100 帧 urban flight 的 host span 约 39.906s，sensor span 约 3.268s。缺可信 epoch/clock mapping 和真实 Prompt/task/session 绑定，不能默认补齐 telemetry 后进行策略预训练。

最小新增动作语料必须保留：原始 Prompt/scope/task/profile/HOME；每个 RGB 的实际 payload SHA 和 source clock；同域有来源的 state、dt；原计划、提交/执行/完成 receipt 与实际控制动作的因果对应；实际 next RGB/next state；失败/取消/人工接管；整 episode 分区。Permit 不等于执行，install ACK 不等于到达，模型自述不等于 completion。现有 `RawSensorReplayRecording.from_manifest` 可重开 raw sources，`onboard_sequence_windows.py` 可保序和 padding，但不会制造缺失动作标签。

现有 Gazebo annotation observation 文件为空（0 bytes）；urban r8 staging 的 20 帧不能靠 prepared wrapper 升级为已绑定 annotation 真值，其 annotation_topics_joined/capture_authenticated/training_ready 均 false。旧 `gazebo_3d_annotation_frame_set.load_annotation_frame_set` 可严格按 sequence/frame_id/RGB SHA 接入未来真实标注；不能 timestamp-nearest join 或向 runtime policy 喂 truth。

## 关键可重开身份

| 实物 | bytes | SHA256 |
| --- | ---: | --- |
| r4-r2 `artifacts/training/onboard-blender-sdf-replay-merged-20261007-r4-r2/manifest.json` | 82,280,620 | `4704587af437ddc9ed022a265c59aac1cf3dbf258b3c50aa14e0a24c354e12fd` |
| 原 grounding r1 `artifacts/training/onboard-vlm-grounding-20261007-r1/manifest.pending-review.json` | 821,628 | `3cf95e401e3be5b9acd0d11e4b8aaee7fe4296e41b2889655fead7a2f34b6cfe` |
| 原 grounding r1 `review-receipt.json` | 4,920 | `d9abe01567f1a0f403e58acb5fc4e72fd32149be67fcab23203112d378ddf384` |
| 原 LoRA `artifacts/models/onboard-vlm-grounding-lora-dev-20261007-r1/report.json` | 957,405 | `6cfb56135f1f2d489565e2eedc8f3e31467b1f073cc6bee15018169f59916871` |
| 原 `adapter/adapter_model.safetensors` | 3,227,648 | `e31d71f1b0b89136574728603ef611fbd77bd6d0edb6d4445c803987640120bb` |
| 独立 reload `artifacts/evaluation/onboard-vlm-grounding-adapter-reload-20261007-r1/report.json` | 234,939 | `d790559bef872510f0795fe927b7e06c363a2dd0ca7a90174da14a64f6ca8973` |
| raw corpus `artifacts/evaluation/onboard-observation-corpus-audit-20261007-r1.json` | 169,258 | `77038c66878ce86d4cbb5d810baf0144b541e4e7778877184163d6194364cf53` |
| 新 grounding r2 `artifacts/training/onboard-vlm-grounding-20261007-r2/manifest.pending-review.json` | 2,488,216 | `047dcbe737c7bec48b5d5d0a363e4c4cc40cddff2b967b64d1ae23820a89ef6b` |

新输出目录为 `artifacts/training/onboard-vlm-grounding-20261007-r2`，实际 create-only CLI native 0；manifest 保留来源/父 adapter/两份新源码 hash，独立 review receipt 不修改 pending manifest。复核入口是 `validate_grounding_increment_dataset(..., require_review=True)`。两份新文件的 25 个针对性测试和 Ruff check/format 均 native 0。builder 27,227 bytes / SHA `2654e65c9295df8910a2316c16783e656cb1f83cc423c628fd55715598c47e80`；tests 16,574 bytes / SHA `af2ad4faf4b636f3b6868ae4ae62761bafb4f6092af6ee7808bddc36bb495cae`。本审计不自行签 review，不称新训练已完成，不改变任何 flight/hardware/promotion/release false flags。
