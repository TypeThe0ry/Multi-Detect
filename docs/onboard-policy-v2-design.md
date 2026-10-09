# Onboard Policy v2 设计

> 状态：设计稿。本文定义混合策略的接口、数据、训练和验收边界；不宣称已经完成训练、Jetson 部署、HIL 或真实飞行验证。

## 1. 目标与约束

用户输入一条自由文本，例如“帮我找到最近的红车”。低频 VLM（约 0.5–1 Hz）把原文解析为 typed TaskSpec，并把请求的 class、color、attribute 与已有 track 做 grounding。高频 small learned policy（5–20 Hz）只消费 structured scene state：tracks、range/geolocation、pose、battery、GPS quality、task phase、search memory/coverage map。它输出与 SIM teacher 相同语义的 translate、turn、hold、stop primitive。

policy 不读 RGB，不直接访问 MAVLink、arming 或 actuator。所有 intent 经过现有 planner、PX4 Mission compiler、SafetyRuleEngine 和 adapter。r33 仍是 fallback/baseline：configs/training/onboard-policy-current-reference-v1.json 记录 40 维输入、20 个输出、development_reference_not_promoted，且没有 Jetson、外部 Gazebo 或真实飞行证据。v2 只有在 held-out Gazebo worlds 上击败 constant-forward、scripted lawnmower、r33 和 VLM-only shadow，且 safety violation 不回归时才替换。

## 2. 模块图和数据流

~~~text
free-form prompt
  -> VLM/TaskSpec parser (0.5–1 Hz)
  -> TaskSpec + prompt resolver allowlist
RGB/depth/camera pose -> LiveMissionRunner
  -> detector + ReID -> UnifiedTargetPool
  -> range/geolocation + target matching
PX4 telemetry -> ContextSnapshot
以上共同进入 StructuredSceneStateBuilder
  + SearchMemoryGrid -> state history + masks
  -> PrimitivePolicy (5–20 Hz)
  -> typed PrimitiveProposal
  -> SafetyRuleEngine + planner
  -> onboard_navigation / px4_mission
  -> OnboardMissionAdapter -> SIM/HIL/REAL Mission executor
  -> ack/outcome/obs_after -> EpisodeRecorder/evidence
~~~

### 2.1 复用、扩展、新增

| 组件 | 处理方式 | 具体边界 |
|---|---|---|
| src/multidetect/live.py:LiveMissionRunner | 复用；在同一 frame 的 detector、tracker、range 结果一致后调用 state builder | 已串起 detector、UnifiedTargetPool、ReID、MultiModalRangingEngine、avoidance、mission、telemetry |
| src/multidetect/vision.py（perception_factory、YOLO26n COCO/VisDrone ONNX/TensorRT） | 复用 detector/runtime；不为 v2 建第二套 image pipeline | RGB/depth 只在 perception seam 使用，policy 输入仍是 structured state |
| src/multidetect/unified_tracking.py:UnifiedTargetPool | 复用 snapshots，不暴露 tracker 内部 Kalman 状态 | UnifiedTrackSnapshot 有 track_id、state、bbox、confidence、tracking_quality、missed count、ReID、primary、actionable；range/bearing 不在 snapshot 内，由 ranging/perception adapter 聚合 |
| src/multidetect/autonomy/perception_adapter.py:OnboardFramePerceptionAdapter | 扩展 v2 adapter | 同帧 RGB/depth/camera pose 严格匹配；缺 calibrated range 时保留诊断 bearing，不能生成 GPS |
| src/multidetect/multimodal_ranging.py、target_geolocation.py | 复用 | range validity、pose uncertainty、WGS-84 + sigma 作为观测；不合格 GPS 为 unknown |
| Depth Anything V2 集成（由现有 depth/ranging seam 提供） | 复用其 metric-depth 结果和 validity；不把 raw depth 送给 high-rate actor | depth 仅作为 range/obstacle structured fields |
| src/multidetect/autonomy/contracts.py:ContextSnapshot | 复用 v1 health 字段；加版本化 v2 adapter | battery、速度、风、GPS horizontal accuracy、geofence、camera/perception/map/DEM 已存在；vertical sigma 是 v2 adapter 的新增/可选字段 |
| src/multidetect/autonomy/onboard_agent.py | 复用 typed proposal、event、hash、fail-closed；加 primitive adapter | 保留 OnboardEventKind、OnboardResponse、OnboardTaskAction 和旧 40 维兼容 |
| src/multidetect/autonomy/onboard_task_state.py | 复用 hard gates | TAKEOFF→SEARCH→INSPECT/MATCH→RECORD→RETURN→REPORT/LAND；模型不能绕过 GPS、confidence、class/color gates |
| src/multidetect/autonomy/onboard_navigation.py、px4_mission.py | 复用 compiler/safety；扩展 bounded primitive/yaw 编译 | policy 只给 intent；compiler 负责 geofence、altitude、energy、Mission item、wire hash |
| src/multidetect/autonomy/onboard_adapter.py | 复用并增加 primitive dispatcher | adapter 才能安装/替换 Mission，ARM 仍只在 adapter/transport 侧 |
| src/multidetect/autonomy/prompt_resolution.py | 保留 registry/allowlist 为最终 authority | 当前按 prompt_id 查表，未知 prompt 必须 abstain |
| src/multidetect/autonomy/policy_contract.py | 复用 SharedTaskPolicy 的 truth-free、artifact hash、planner revalidation 语义 | 它负责高层 task selection；per-step navigation actor 是独立 contract |
| src/multidetect/autonomy/onboard_vlm_worker.py、onboard_vlm_provider.py、onboard_vlm_execution.py | 复用异步 worker、source binding、cancel/retire 和 ready/admission 检查 | VLM 结果只更新 TaskSpec/grounding，不能获得 Mission/ARM 权限 |
| src/multidetect/sim_teacher/ | 复用 teacher primitive、recording、audit；不修改并行实现 | 是离线数据输入 |
| 新 structured_scene_state.py | schema、normalization、valid masks、state builder | 只接收 truth-free runtime observation |
| 新 search_memory.py | deterministic coverage/obstacle/target evidence grid | memory 在 policy 外更新 |
| 新 primitive_policy.py | GRU/小型 Transformer runner + ONNX/TensorRT | 输出 typed primitive，不含 transport 权限 |
| 新 task_spec.py、vlm_task_grounder.py | VLM JSON parser、track grounding、cache、fallback | 只更新 TaskSpec/track match，不发 Mission |

## 3. Structured state schema

### 3.1 时间、历史和 unknown

固定 contract：multidetect.onboard-state-v2。每个 tick 有 schema、captured_at_s、dt_s、frame_id、task_spec_hash、valid_mask、reset_mask、truth_free=true。历史长度 T=16；10 Hz 约 1.6 s，5 Hz 约 3.2 s。沿用 src/multidetect/autonomy/onboard_history.py 的 bounded history、max gap 5 s、zero padding 和 valid/dt/reset mask。unknown 数值可以填 0，但必须有独立 valid bit；class/color match 使用 0=false、0.5=unknown、1=true。

### 3.2 字段、单位、归一化

| block | 字段 | 归一化 |
|---|---|---|
| health | battery (%)、estimated flight time (s)、ground/air speed (m/s)、wind N/E (m/s)、GPS fix、horizontal/vertical accuracy (m)、GPS age (s)、geofence/camera/perception/map/DEM valid、obstacle valid、inference/policy age | 延续 OnboardModelInput.feature_vector：%/100、time/3600、speed/50、wind/20、fix/6、accuracy/100；age clamp 0..5 后 /5 |
| pose | local N/E/D relative home (m)、relative altitude (m)、roll/pitch/heading、N/E/D velocity、position/attitude sigma | position /100、altitude /30、velocity /20；角度用 sin/cos |
| task | phase one-hot、search budget、return-required、requested class/color/attribute IDs、TaskSpec confidence | fixed vocabulary embedding；unknown 属性保留 mask |
| tracks K×18 | K=8，按 primary/actionable、quality、confidence 排序；state one-hot（DETECTED/TRACKING/LOCKED/OCCLUDED/REACQUIRING/LOST）、label、bbox、image velocity、age、missed、ReID、class/color match、以及从 perception_adapter/ranging 聚合的 range、bearing、range sigma、evidence age | bbox/velocity 为 image normalized；range /100 m clamp；bearing sin/cos；match 三态；无 track 用 mask |
| scene | primary/actionable、nearest obstacle distance/bearing、avoidance risk、camera motion confidence、ranging/geolocation validity、target moved/lost | distance /50 m；bearing sin/cos；flag 0/1 |
| memory 16×16×5 | 5 m cell、local NED/home 对齐；coverage age、visited count、obstacle probability、target evidence、planned cursor | age 0..60/60；count log1p/8；probability 0..1；越界 mask |
| event | TARGET_MOVED、TARGET_LOST、GPS_DEGRADED、BATTERY_LOW、OBSTACLE_RISK、INFERENCE_FAULT、MISSION_COMPLETE、NONE + severity | one-hot；severity /100 |

建议每 tick 约 1,600 个原始值，先 projection 到 128 维；memory 用小 CNN 得到 64 维。K、grid、vocabulary、feature order 写入 model artifact 后冻结。

### 3.3 构建顺序

1. LiveMissionRunner 产生 frame identity、detections、TargetPoolUpdate；
2. UnifiedTargetPool.snapshots() 产生 track slots；
3. MultiModalRangingEngine 产生 range/pose validity；只有 calibrated NED offset 才进 geolocation；
4. ContextSnapshot 提供 telemetry/health，缺样本保持 unknown；
5. OnboardTaskStateMachine.phase、accepted TaskSpec、event 进入 task/event block；
6. SearchMemoryGrid 在 action outcome 和 pose 确认后更新；
7. 同一 captured_at_s/frame_id 做一致性检查后才 append history；source-frame mismatch 丢弃 block，不拼接旧帧。

## 4. Action space 与执行路径

### 4.1 输出 contract

src/multidetect/sim_teacher/primitives.py:ActionPrimitive 是 teacher 和 policy 的语义 contract。源码只保证 kind、finite 数值、turn ≤360° 和 hold ≥0；下面是 v2 policy/safety 提议的更窄 bounds：

- translate：forward_m∈[-2,8]、right_m∈[-6,6]、up_m∈[-2,2]；
- turn：yaw_delta_deg∈[-90,90]；
- hold：seconds∈[0.2,2.0]；
- stop：零参数终止当前搜索/任务。

使用 kind categorical + bounded continuous heads 和 action mask。stop 不是 LAND；safety layer 可以缩短距离或替换为 hold/return。

### 4.2 teacher primitive 到 PX4 Mission

1. 校验 schema、model hash、时间戳和 bounds；
2. primitive_to_ned(action, yaw_deg) 把 body forward/right/up 变为 local NED；
3. primitive_to_flight_plan 把 N/E/D、yaw intent 映射到 bounded OnboardFlightPlan。现有 src/multidetect/autonomy/onboard_navigation_inference.py 已有 PlannedNavigationTarget(kind=turn_yaw, translation_frame=body_frd, yaw_delta_rad) 合同，可作为 adapter 输入；当前 OnboardFlightPlan 没有 yaw，当前 OnboardFlightAction 也没有 TURN，v2 必须新增版本化 yaw/heading seam，或生成 deterministic turn-in-place waypoint，不能把 yaw 塞进 speed/duration；
4. 进入 onboard_navigation.compile_model_flight_plan，复用 geodetic projection、multirotor safety policy、geofence/altitude/energy gates；
5. OnboardMissionAdapter 独立 recompile、比较 hash、执行 Mission transaction、ACK/readback、start permit；
6. 记录 proposed → accepted → committed → executed → completed。policy、VLM、teacher 都不能构造 MAVLink 或调用 ARM。

SIM 的 ack、outcome、obs_after 原样进入数据。timeout/rejected/collision 降低样本权重或作为 hard-negative，不能伪装成功。

## 5. Policy model、训练和评估

### 5.1 模型和 Orin 预算

首选 2-layer GRU hidden 256（约 0.8–1.2M 参数），或 2-layer 4-head causal Transformer，context 16。state projection 128，memory CNN 64；输出 4-kind categorical、forward/right/up/yaw/seconds heads、value/uncertainty head。导出 ONNX/TensorRT FP16，目标 policy p95 <20 ms、额外常驻内存 <150 MB，频率 5–20 Hz；这是待验证预算，不是既有测量。若达不到，policy 降到 5–10 Hz，但 safety tick 不降频。r33 只做旧模型 adapter/baseline，不复用其 20-output 语义。

### 5.2 BC → DAgger

**Behavior cloning**：

- 数据来自 artifacts/datasets/sim-teacher/**/episodes/*/steps.jsonl；只取 runtime 可获得的 obs_before，labels 的 privileged truth 不进输入；
- expert_search 提供 search/approach/hold/stop，explore 覆盖无目标、边界和障碍；
- 先按 whole world/scene/episode 切分，再 windowing，防止相邻窗口泄漏；
- action classification + bounded regression + stop/terminal loss；timeout/reject 降权，collision/violation 为 hard-negative；
- 目标：首轮 ≥5,000 episodes、每 episode 100–300 steps（约 0.5–1.5M transitions），≥10 种 task wording、≥6 个颜色/类别组合、≥4 种起始 pose/yaw、≥20% 含遮挡或暂时丢失。

**DAgger（只在 SIM）**：

- 当前 policy rollout 偏离、target lost/moved、GPS degraded、obstacle risk 时查询 privileged expert；
- 3 轮，每轮 ≥100,000 新 state，expert intervention 20–30%；
- target_in_fov、bearing、range、expert phase/action 留在 labels 只用于监督/审计；
- holdout worlds 固定且不写回训练；蒸馏前检查 teacher action 的 completion 和 safety。

audit_dataset 的 manifest、连续 step、文件引用、action histogram、success_rate、frame-pose skew 只是必要条件。还要检查 truth/teacher consistency、source-frame 对齐、outcome 与 pose 差值、primitive coverage、label leakage。

当前并行 collector 仍可能写入 sensor_status=awaiting_live_gazebo_stream 的 placeholder pose/GPS，而不是完整 Gazebo RGB/depth/attitude/telemetry；这类 episode 只能用于 schema/recorder smoke test，不能进入 BC/DAgger promotion set。采集 admission 必须检查真实 frame、pose、GPS、ACK、outcome 和 obs_after 都存在。

### 5.3 Held-out Gazebo protocol

按 whole world 划分 70% train、15% validation、15% test；同 world 的不同 seed 不算独立 world，至少 20 个完全未见 world。每个 test world 固定 10 seeds、prompt set 和 fault schedule。报告均值、median、95% bootstrap CI，并按目标 class/color、moved/lost、GPS degraded 分桶。

指标：task success（找到正确目标、完成位置记录、返航/报告）、time-to-find、path length、energy/battery delta、safety violations（碰撞、越界、低高度、compiler reject）、planner rejects/failsafe、target grounding precision/recall、policy/VLM p50/p95/p99 latency、RSS/GPU memory。

基线：constant-forward、scripted lawnmower、r33、VLM-only shadow、expert upper bound。promotion gate：v2 在主要 fault bucket 的 success 高于最佳 baseline；time/path 不以 safety 为代价恶化；safety violation 不高于最佳 baseline；每个 test world 都有 episode/audit artifact。否则继续 shadow/DAgger。

## 6. VLM 角色、候选和 fallback

### 6.1 候选

优先用统一 harness 比较：

1. Qwen3-VL-2B-Instruct：主要 grounding 候选，支持多图/视频时序与 2D/3D grounding。[GitHub](https://github.com/QwenLM/Qwen3-VL)，[model card](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct)
2. SmolVLM2-500M-Video-Instruct：低内存 shadow baseline；model card 的约 1.8 GB GPU RAM 是作者报告，不是 Orin 并发实测。[model card](https://huggingface.co/HuggingFaceTB/SmolVLM2-500M-Video-Instruct)
3. InternVL3.5-1B：约 1.1B grounding/embodied 候选；Orin/JetPack 兼容性未证实。[model card](https://huggingface.co/OpenGVLab/InternVL3_5-1B)
4. Qwen3.5-2B：TaskSpec/text+vision 候选。Jetson AI Lab 报告的 Orin Nano Q8_0 text agent 约 4.6 GB、约 20 token/s、TTFT 约 3 s，不代表 camera+detector 并发视觉延迟。[tutorial](https://www.jetson-ai-lab.com/tutorials/openclaw/)，[model card](https://huggingface.co/Qwen/Qwen3.5-2B)
5. Cosmos-Reason2-2B：官方 card 列 24 GB 最低要求，暂不作为 8GB 默认。[model card](https://huggingface.co/nvidia/Cosmos-Reason2-2B)

所有候选都要固定 revision、processor、quantization 和 SHA-256。Orin Nano 的 visual p95/p99、peak shared memory、detector concurrency、thermal throttling 目前未知，必须在 JetPack 6/TensorRT 8.6 上测。

### 6.2 TaskSpec 和 grounding

建议 contract：

~~~json
{
  "schema": "multidetect.task-spec-v1",
  "prompt_sha256": "sha256...",
  "intent": "search_and_report",
  "target": {"class": "car", "color": "red", "attributes": [], "relative": "nearest"},
  "search_area": {"frame": "local_ned", "radius_m": null},
  "record_location": true,
  "return_home": true,
  "confidence": 0.0,
  "unknown_fields": [],
  "resolver_id": "..."
}
~~~

VLM 输入原文、当前 RGB、最多 8 个 track crop/metadata、短 history 和已验证 receipt；输出严格 JSON。它应映射到 src/multidetect/autonomy/contracts.py 的 PromptEnvelope/ResolvedTaskSpec 候选，再由 PromptTaskResolver、SharedTaskPolicy 和 planner revalidate。grounding 为每个 track_id 返回 class_match、color_match、attribute_match（true/false/unknown）、score、evidence_frame_id、expiry。不能返回 GPS 或 waypoint。结果进入 OnboardRuntimeEvent.target_*_match，最终由 OnboardTaskStateMachine gates 决定。

### 6.3 VLM 不可用

- 已有未过期 TaskSpec：继续高频 policy；超过 10 s 标记 semantic stale，不接受新的 MATCH/RECORD；
- 初始化解析超时/进程退出：先查 PromptTaskResolver 的 registered prompt_id；没有 allowlisted template 则 typed abstain，不 ARM、不 takeoff；
- deterministic 中文 parser 只能是 development fallback；多目标、比较级、空间约束或歧义必须 unknown；
- worker 用非阻塞 submit/poll 和 hard cancel；stale prompt/frame/HOME/aircraft/clock 结果 retire。policy fault → HOLD，battery/GPS/obstacle safety fault → safety supervisor 的 HOLD/RETURN。

## 7. 失败处理和重规划责任

| 事件 | 来源 | 处理层 | 结果 |
|---|---|---|---|
| target moved | track 位置/速度、连续 range、UnifiedTargetPool | state builder 置 TARGET_MOVED；policy 更新 memory/reacquire | 短窗口 search；超时回 SEARCH，旧坐标不能 record |
| target lost/occluded | track state、missed count | tracker + event mask | bounded turn/translate；超预算退回 SEARCH |
| GPS degraded/pose stale | ContextSnapshot fix/accuracy/age、range validity | health/safety | 禁止 geolocation record；按 reserve/geofence HOLD 或 RETURN |
| battery low/return reserve | telemetry + planner endurance | SafetyRuleEngine/adapter | 立即 RETURN_HOME，覆盖 policy/VLM |
| obstacle risk | depth/avoidance/range、PX4 failsafe | avoidance/planner | 缩短/拒绝 primitive，HOLD/replan |
| detector/range/model fault | runtime health、timeout、invalid schema | adapter + INFERENCE_FAULT | 输出失效即 HOLD；持续 fault 或 reserve 不足 RETURN |
| VLM stale/unavailable | worker age/cancel/binding | VLM adapter/resolver | last TaskSpec 可继续；禁用新 MATCH/RECORD；初始失败 abstain |
| Mission/compiler reject | onboard_navigation/PX4 compiler | adapter | 不安装被拒 Mission，记录 reject，HOLD/RETURN |
| return complete | PX4 completion + route progress | OnboardTaskStateMachine | fresh completion 且 progress≥.99 才 REPORT/LAND |

所有事件保留 source time 和 processing time；旧 heartbeat 或模型自报 confidence 不能伪造完成证据。

## 8. SIM → REAL parity

一致项：TaskSpec、track slot order、normalization、history/mask、primitive kind/units/bounds、ActionPrimitive→NED、ContextSnapshot health semantics、GPS/range gates、planner/geofence/energy policy、Mission compiler、adapter transaction/readback、episode row 和 evidence 状态机。

差异项：SIM 可读 privileged truth 只生成 offline labels，REAL 不可读；SIM 是 localhost SITL/x500_mono_cam，REAL 是 Pixhawk PX4 与真实 home/GNSS/failsafe；SIM dynamics、clock、RGB/depth noise 与真实 motion blur、exposure、packet loss、camera calibration 不同；TensorRT kernels、quantization、JetPack version、thermal limits 必须分别测。SIM 成功不等于 outdoor GNSS、HIL 或真实飞行成功。

任何 truth label 只在离线 labels/scenario manifest 中存在，不进入 REAL state、VLM prompt 或 policy runtime。

## 9. 分阶段实施（每项 1–2 天）

1. Contract freeze（1 天）：冻结 v2 schema、TaskSpec、state block、primitive bounds、hash/version。验收：malformed/unknown/NaN/frame mismatch fail-closed；r33 仍能加载。
2. Teacher audit（1–2 天）：运行 scripts/audit_sim_teacher_dataset.py，补 world/episode split、teacher consistency、primitive coverage。验收：≥100 smoke episodes，文件引用正确，label leakage 为零。
3. StructuredSceneStateBuilder（2 天）：接 LiveMissionRunner、ContextSnapshot、UnifiedTargetPool、ranging、task state。验收：同 frame 在 replay/live 生成相同 canonical hash；缺 range/GPS 时 mask 正确。
4. SearchMemoryGrid（1–2 天）：实现 5 m、16×16 grid、age/coverage/obstacle/evidence、reset。验收：重复 pose 不增加 coverage；时间回退/越界不污染。
5. PrimitivePolicy baseline（2 天）：masked GRU/Transformer、ONNX export、RTX4060 profile。验收：bounds、mask、NaN→HOLD；达到开发机 profile budget。
6. Primitive→Mission adapter（2 天）：增加 yaw intent/turn seam，复用 onboard_navigation 和 OnboardMissionAdapter recompile/hash/readback。验收：每类 primitive 生成或安全拒绝 bounded Mission；policy 路径无 ARM/MAVLink。
7. BC v1（2 天）：expert_search + explore，0.5M steps，validation 和 rollout。验收：validation time-to-find 不劣 constant-forward，safety gates 全过。
8. DAgger SIM（2 天）：privileged expert、moved/lost/GPS/obstacle faults，一轮≥100k states。验收：intervention/outcome/labels 可审计；holdout 不写入。
9. VLM grounding shadow（2 天）：Qwen3-VL-2B quantized vs SmolVLM2/InternVL1B，严格 JSON、cache、expiry、cancel、fallback。验收：不可用不 ARM，stale 无 adapter side effect，记录 p50/p95/p99 和 malformed JSON。
10. Held-out Gazebo gate（2 天）：20+ 未见 worlds×10 seeds，对所有 baseline 和 v2 出报告。验收：满足 5.3 promotion gate，否则保持 shadow。
11. Jetson parity（2 天）：JetPack 6/TensorRT 8.6 锁定 hashes，测并发内存、thermal、p95/p99。验收：达到预算且 fallback 通过才进入 HIL。
12. HIL/REAL admission（2 天起）：先 read-only/shadow，再受限 Mission；保留 source snapshot、wire hash、ACK、completion receipt。验收：无完整 evidence 不得标 executed/completed，也不得宣称真实飞行性能。

## 10. Explicit non-claims

- r33 的 45.6k synthetic samples、40→20 MLP 或 46/208 OpenFly 结果不证明 v2 成功。
- VLM grounding/confidence/空间 reasoning 不等于 metric range、WGS-84 accuracy、collision avoidance 或 flight authorization。
- model card 或第三方内存数字不等于本项目 Orin Nano 视觉并发 benchmark。
- Gazebo/SITL episode、audit_dataset 完整性和 success_rate 不证明 privileged label 正确，更不证明 PX4 real vehicle 或 outdoor GNSS。
- SIM holdout 胜出不等于 HIL、Pixhawk、Jetson thermal、通信丢包、真实障碍或动态目标已通过。
- VLM/learned policy 不接 ARM、PWM、MAVLink，也不绕过 SafetyRuleEngine、planner、Mission compiler、geofence、battery reserve 或 task-state gates。
- 没有独立 world/episode holdout、可复现 hashes、latency/memory 证据和完整 executed/completed receipt，不得称候选为 production、field-proven 或 final release。

## 11. 实现状态

- 已实现任务 1 Contract freeze：`src/multidetect/autonomy/policy_v2/contracts.py`，包含 TaskSpec v1、StructuredSceneState v2、PolicyAction v2 的冻结 dataclass、严格 JSON、规范化哈希、有限值与边界校验。
- 已实现任务 3 StructuredSceneStateBuilder：`src/multidetect/autonomy/policy_v2/state_builder.py`，提供运行时输入和 `steps.jsonl` observation 的统一适配，缺失 range/GPS 使用 mask，状态保持 truth-free。
- 已实现任务 4 SearchMemoryGrid：`src/multidetect/autonomy/policy_v2/search_memory.py`，提供 5 m、16×16、5 通道的确定性记忆、reset、覆盖归一化，以及时间倒退和越界拒绝。
- 包导出位于 `src/multidetect/autonomy/policy_v2/__init__.py`；对应快速测试位于 `tests/test_policy_v2_contracts.py`、`tests/test_policy_v2_state_builder.py`、`tests/test_policy_v2_search_memory.py`。
- Implemented task 5 PrimitivePolicy baseline: `src/multidetect/autonomy/policy_v2/model.py` provides a 2-layer GRU with a SearchMemoryGrid CNN branch, masked action heads, bounded tanh outputs, value/uncertainty heads, NaN/invalid HOLD fail-closed handling, opset 17 ONNX export, and an ONNX Runtime validation wrapper.
- Implemented the task 7 BC preparation skeleton: `scripts/train_policy_v2_bc.py` admits only `sensor_status=live` episodes with real RGB PNG/depth NPY files, performs world/episode splitting before windows, excludes privileged labels from state inputs, down-weights timeout/rejected outcomes, and writes checkpoint, ONNX, hashes, split, metrics, and config report. `scripts/profile_policy_v2.py` reports provider latency percentiles and parameter count.
- Synthetic smoke verification used `.venv-train`: two training epochs completed and CPU ONNX profile measured p50 0.82345 ms, p95 2.7904 ms, p99 2.7925 ms, with 800971 parameters. The environment exposed `AzureExecutionProvider` and `CPUExecutionProvider` but no CUDA provider, so CUDA latency remains unmeasured. These numbers are synthetic smoke results, not a quality claim.
