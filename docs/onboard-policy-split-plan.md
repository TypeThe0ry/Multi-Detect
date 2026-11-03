# `onboard_policy.py` 拆分重构计划

## 目标与现状

`scripts/onboard_policy.py` 当前为 3179 行。它既是机载策略实现，也充当历史兼容入口：脚本和测试直接从这里导入类、函数、配置常量，并 monkeypatch 这里的依赖。拆分应逐步迁移实现，长期保留这个公开入口；每个阶段都可单独合并和回滚，不改变策略行为。

当前 `src/multidetect/autonomy/onboard_policy_parts/` 已有：`config.py`（49 行，`DEFAULTS`、`POSITION_DEFAULTS`、`llama_server_path`）、`proposer.py`（107 行，`YoloeProposer`）和 `vocabulary.py`（25 行，`VOCABULARY`）。主文件从这些模块导入并重新导出 `DEFAULTS`、`POSITION_DEFAULTS`、`YoloeProposer`、`VOCABULARY`。

## 文件结构和职责

| 行号（当前） | 定义 | 职责/主要依赖 |
| --- | --- | --- |
| 1–107 | 模块说明、标准库和 autonomy/simulator 导入 | 全局依赖入口；`ROOT` 将仓库根与 `src` 加入导入路径 |
| 109–179 | `_model_sha256`、`_planning_model_spec` | 模型文件哈希缓存和规划模型配置解析；依赖 `Path`、`hashlib`、JSON |
| 180–213 | `validate_sim_ground_events` | 仿真地面事件配置校验 |
| 214–273 | `SimGroundTimeline` | 仿真事件时间线与观测推进 |
| 274–330 | `_observation_timestamp_s`、`_vehicle_receipt_s`、`_enabled`、`perception_task_text`、`labelled_refs` | 时间戳/布尔解析、任务文本和引用格式化 |
| 331–378 | `build_mission_from_text` | 调用 `compile_mission_plan`，编译并验证任务计划 |
| 379–1496 | `OnboardPolicy.__init__` | 装配飞行器能力、任务包、安全包络、障碍物、loop、模型 runner、健康监控和 executor；依赖面最广 |
| 1497–1787 | `OnboardPolicy` 的监督器/操作员任务包方法 | 准入、拒绝、准备/安装计划、载荷释放确认 |
| 1788–1890 | 关闭、后台重启取消、地面事件、下行发送和任务摘要 | 生命周期与日志/地面接口 |
| 1891–2104 | `_slow_identity` | 慢模型请求、超时/重启恢复、健康记录 |
| 2105–2177 | `_run_near_depth` | 可选深度测距执行 |
| 2178–2326 | 飞行模式/相机模式、混合 VTOL 搜索、能量估计和遥测间隙 | 搜索模式协调及状态同步 |
| 2327–2671 | `_act_impl` | 单次观测的主要感知、搜索、执行和安全决策路径 |
| 2672–2897 | `act`、`_health_recovery_action` | 外层动作入口、健康故障恢复和安全动作 |
| 2931–3126 | `_act_mission` | MissionExecutor/MissionSupervisor 任务模式执行 |
| 3127–3137 | `_mission_plan_label` | 计划标签输出 |
| 3138–3179 | `build_onboard_policy`、`__all__` | 配置加载、策略工厂和兼容导出 |

范围说明：上述是按当前定义位置整理的结构图；改动开始前应重新确认行号和方法边界，避免因并行提交导致计划行号漂移。

## 导入者与 monkeypatch 约束

`rg` 搜索 `scripts/`、`tests/`、`src/` 后，运行时直接导入入口主要包括：

- 脚本：`onboard_main.py`、`collect_sim_teacher_episodes.py`、`run_mission_suite.py`、`run_planner_probe.py`、`cache_onboard_proposals.py`、`evaluate_small_object_tiling.py`、`bench_orin_onboard.py`、`export_proposer_for_orin.py`、`evaluate_yoloe_proposer_round.py`。
- 测试：`test_onboard_policy_package.py`、`test_onboard_policy_step_triangulation.py`、`test_onboard_policy_plan.py`、`test_onboard_model_split.py`、`test_onboard_operator_model_split.py`、`test_onboard_proposer_tiling.py`、`test_onboard_runtime_policy.py`、`test_onboard_main.py`、`test_onboard_health.py`、`test_onboard_ground_policy.py`、`test_place_memory_integration.py`、`test_near_depth.py`、`test_safety_envelope.py`，以及 `tests/onboard_factories.py`。
- `src/` 没有直接导入 `scripts.onboard_policy` 的运行时模块；`plan_validation.py` 直接使用 `onboard_policy_parts.vocabulary`。另有注释/源文本检查将该脚本路径视为契约，见 `test_onboard_loop.py` 和 `validate_step_triangulation_replay.py`。

已确认的 monkeypatch 目标和覆盖点：

| 旧入口目标 | 测试/用途 | 拆分要求 |
| --- | --- | --- |
| `YoloeProposer` | `test_onboard_policy_package.py`、`test_onboard_ground_policy.py`、`test_onboard_runtime_policy.py`、`test_onboard_main.py`、`test_onboard_proposer_tiling.py` | 构造器必须读取 `scripts.onboard_policy.YoloeProposer` 的当前值 |
| `compile_mission_plan` | `test_onboard_policy_package.py` | MissionPlan 编译调用必须读取旧入口当前值 |
| `build_mission_from_text` | `test_onboard_model_split.py`、`test_onboard_operator_model_split.py`、`test_onboard_health.py` | 构造/任务准入路径必须读取旧入口当前值 |
| `HealthMonitor` | `test_onboard_health.py` | 健康监控构造必须读取旧入口当前值 |
| `pixel_ground_point` | `test_place_memory_integration.py` | 像素投影必须读取旧入口当前值 |
| `time.perf_counter`、`hashlib.sha256`、`Path` | `test_onboard_health.py`、`test_onboard_model_split.py` | `time`/`hashlib` 模块对象的共享修改需保持；`Path` 这种名字绑定必须确保 patch 仍作用于哈希逻辑 |
| `OnboardPolicy` 方法 | `test_onboard_policy_plan.py`、`test_onboard_ground_policy.py` | `OnboardPolicy` 类仍从旧入口导出，类级 monkeypatch 的方法名/调用方式不变 |

**关键实现约束：re-export 本身不够。** 方法搬到新模块后，Python 会在新模块全局变量中解析 `YoloeProposer`、`HealthMonitor` 等名字；仅在旧入口写 `from new_module import Name`，不会让 monkeypatch 旧入口改变新模块的全局变量。迁移前须实现并测试一条兼容依赖通道（例如从 facade 动态读取被 patch 的名字，或明确注入兼容依赖对象），每次迁移均运行上述对应测试。不要用模块别名/`sys.modules` 替换方式让旧入口失去可预测的导出语义。

## 安全网与证据约束

- `tests/onboard_gate_files.py` 的 `_EXACT` 明确包含 `test_onboard_policy_step_triangulation.py`、`test_onboard_ground_policy.py`、`test_onboard_health.py`、`test_onboard_model_split.py`、`test_onboard_operator_model_split.py`、`test_safety_envelope.py`、`test_place_memory_integration.py`、`test_plan_intent.py`、`test_onboard_main.py` 等；`_PREFIXES` 还动态收入 `test_onboard_policy*`、`test_onboard_runtime*`、`test_onboard_loop*`、`test_replay_*`。以执行 `python scripts/run_onboard_gate.py -q -p no:warnings` 的实际收集结果为准。
- 关键专项还包括 `test_onboard_policy_package.py`、`test_onboard_policy_plan.py`、`test_onboard_proposer_tiling.py`、`test_onboard_runtime_policy.py`、`test_near_depth.py` 和 `test_onboard_loop.py`。拆分纯文档阶段无需运行；每个代码阶段运行对应专项，再运行完整 onboard gate。
- 黄金重放入口为 `scripts/check_golden_replay.py`，manifest/baseline 在 `docs/golden-replay-manifest.json` 和 `docs/golden-replay-baseline.json`。按 `docs/golden-replay.md`，架次文件不在这台机器；全部数据缺失时 checker 会逐项 WARNING 并以退出码 0 跳过，不能把该退出码误报为 replay 已验证。代码阶段仍运行 checker 记录可用性；合并前在具有真实黄金架次数据的环境运行，并要求零 divergence。禁止为了通过而更新 baseline；只有策略行为经过审查且有明确理由时才按既有流程更新。
- `src/multidetect/release_parity.py` 的 `DEFAULT_FILE_LIST` 显式包含 `scripts/onboard_policy.py`，并在 `core_algorithm` 组包含 `onboard_policy_parts/__init__.py`、`config.py`、`proposer.py`、`vocabulary.py`。`tests/test_release_parity.py` 断言这些文件列表和当前 canonical parity 文档一致。因此本文件拆分**会改变 parity 证据**：新建承载策略逻辑的模块必须加入相应契约组的文件列表，并经 `scripts/create_release_parity.py` 生成 `configs/release-parity.json`；该受证据约束文件不得手改。先确认组归属，再用 release parity 测试验证生成值。新文件若未加入列表，代码虽运行，parity 仍未覆盖其策略逻辑，不能视作完成。
- `git status` 初始显示 `artifacts/` 未跟踪且为共享软链接；本任务不触碰该路径。

## 分步实施

以下 diff 规模是新增/移动/兼容代码与测试的粗略净变化估算，不含 parity JSON 生成造成的格式/哈希行变化。真实搬移可能造成较大的删除+新增行数；各步以小范围、语义等价、可独立回滚为准。

| 阶段 | 移动内容与兼容层 | 阶段验证 | 预计 diff / 回滚边界 |
| --- | --- | --- | --- |
| 0. 兼容契约先行 | 暂不搬实现。新增测试明确旧入口 monkeypatch 能影响策略实际调用，并锁定旧导出名/`__all__`。整理可动态解析的依赖清单。 | `test_onboard_policy_package.py`、`test_onboard_health.py`、`test_onboard_model_split.py`、`test_onboard_proposer_tiling.py`、`test_place_memory_integration.py`；完整 onboard gate。 | 约 40–100 行；只涉及测试/兼容机制，无策略行为变化，可独立回滚。 |
| 1. 纯函数与时间线 | 把 `validate_sim_ground_events`、`SimGroundTimeline`、时间戳/布尔解析和 `labelled_refs` 移入 `onboard_policy_parts/simulation.py`、`helpers.py` 等小模块；原脚本保留符号级 re-export。`build_mission_from_text` 暂留 facade，避免其 `compile_mission_plan` patch 语义变化。 | `test_onboard_policy_package.py`、`test_plan_intent.py`、`test_onboard_policy_step_triangulation.py`；gate。 | 2–3 个新模块、总计约 300–450 行搬移，compat facade 约 15–35 行；不碰策略决策。 |
| 2. 哈希/模型配置辅助 | 将 `_model_sha256`、`_planning_model_spec` 搬到 `onboard_policy_parts/model_config.py`；facade 继续导出原名称，并为 `Path` / `hashlib` 保持旧 monkeypatch 语义（可把解析留在 facade，直到依赖通道可验证）。 | `test_onboard_model_split.py`，再跑 `test_onboard_policy_package.py`；gate。 | 1 个新模块约 80–130 行，facade/测试约 20–50 行；只迁移辅助功能，单独回滚。 |
| 3. 构造与任务包准入 | 抽出 `OnboardPolicy.__init__` 所属构造装配和 1497–1787 的 package/admission 方法到 `onboard_policy_parts/runtime.py` 或有界 mixin。旧 `OnboardPolicy` 导出仍指向同一公开类；patchable 构造依赖（`YoloeProposer`、`HealthMonitor`、`build_mission_from_text`、`compile_mission_plan`）必须经阶段 0 的动态兼容通道解析。先避免修改调用顺序和默认配置。 | `test_onboard_policy_package.py`、`test_onboard_model_split.py`、`test_onboard_operator_model_split.py`、`test_onboard_health.py`、`test_onboard_main.py`；gate。 | 2–4 个新模块、约 900–1300 行搬移，兼容层/测试净增约 80–180 行；移动大但职责集中，单次仅合并此阶段。 |
| 4. 生命周期、慢模型和可选深度 | 搬移 `close`、重启取消、ground event/downlink、任务摘要、`_slow_identity`、`_run_near_depth` 到 runtime support 模块；旧类方法保留兼容委托或继承式 API，公开方法名/返回值不变。与阶段 3 分开，便于定位后台线程/模型生命周期问题。 | `test_onboard_health.py`、`test_onboard_runtime_policy.py`、`test_near_depth.py`、`test_onboard_policy_package.py`；gate。 | 1–2 个新模块、约 450–650 行搬移，compat/test 约 50–120 行；可独立回滚。 |
| 5. 动作执行核心 | 分别迁移混合 VTOL/遥测方法、`_act_impl` 与 `act`/health recovery、`_act_mission`，按可审查的子提交逐块执行，不一次性移动所有动作逻辑。保持 `OnboardPolicy` 类 API、标签、动作优先级、异常行为和 patch seam。建议目标模块为 `execution.py`、`mission_execution.py`；一次只迁一个模块。 | 每块先跑 `test_onboard_policy_plan.py`、`test_onboard_ground_policy.py`、`test_onboard_policy_step_triangulation.py`、`test_onboard_runtime_policy.py`、`test_safety_envelope.py`、`test_place_memory_integration.py`、`test_onboard_health.py`、`test_onboard_loop.py` 中相关用例；每个子阶段后跑 gate。 | 每个子阶段约 300–800 行搬移，compat/test 约 50–150 行；3 个可分别合并/回滚的提交，不混合格式化或行为修复。 |
| 6. parity 与收尾 | 明确列出所有新增承载逻辑的模块到 `DEFAULT_FILE_LIST` 对应组，运行 `scripts/create_release_parity.py` 更新 parity JSON，再跑 `test_release_parity.py` 与 release gate。最后检查导入者和 `__all__`，确认 facade 只保留导出、兼容依赖解析和工厂；不删除 monkeypatch 所需旧路径。 | `tests/test_release_parity.py`、`tests/test_onboard_policy_package.py`、`python scripts/run_onboard_gate.py -q -p no:warnings`、`python scripts/check_golden_replay.py`（真实数据机器上必须有数据并零 divergence）。 | parity 清单/生成 JSON 可能改变，facade 约 100–250 行；新模块文件路径清单另计。保持最终收尾独立提交；出问题时可回滚 parity 与相应模块路径的一组提交。 |

### 每个代码阶段的合并门槛

1. 先跑对应专项，确认旧入口导入与 monkeypatch 行为；失败时在本阶段内修复，不改宽断言。
2. 跑完整 `python scripts/run_onboard_gate.py -q -p no:warnings`；使用任务环境约定的 `PYTHONPATH=src:.`、`PYTHONUTF8=1`、`TMPDIR=$PWD/.tmp` 和主 `.venv/bin/python`。单进程运行，不启动 SITL、Docker、模型服务或 GPU 工作。
3. 检查 `git diff --check`、`git status --short` 和实际 parity source list；不触碰 `artifacts/`。任何安全/动作输出变化都按行为变更处理，不以“纯拆分”名义略过审查。
4. 在有黄金架次数据的主环境运行 golden replay；当前环境若只报告全部缺失，应记录为未验证，并把有数据环境 replay 作为最终合并前置条件。
5. 代码与 parity 变更分阶段关联，确保回滚后 parity 文件清单/生成 JSON 与代码处于同一版本；不要提交只更新 hash 却遗漏新策略模块绑定的状态。

## 未纳入本任务

本文件只规划，不改代码、不运行策略测试、不更新 parity JSON 或黄金基线。兼容层最终是否能删，应在所有仓库调用方和 monkeypatch 测试完成迁移后另行决策；默认保留 `scripts.onboard_policy` 作为稳定 facade。

## 进度记录

- 第 1 步（helpers/simulation）、第 2 步（model_config）已合并，行为不变，回归闸门通过。
- 第 3 步（构造与任务包准入方法移入 runtime mixin）**未合并**：实现用 `globals().update(vars(scripts.onboard_policy))` 把 facade 的全局变量整体复制进 `src/` 下的新模块，形成 `src → scripts` 的反向依赖和循环导入，而且测试对 facade 名字的 monkeypatch 只对其中 3 个显式改成动态读取的名字生效，其余会静默失效。黄金重放数据不在这台机器上，无法用重放兜底验证。下一次做第 3 步前，先把这些方法依赖的名字显式导入（或通过参数注入），再按计划搬移。
- 第 3 步重评估（本次）：没有改运行代码。按当前文件重新核对，`OnboardPolicy.__init__` 位于第 208–1326 行，约 1,119 行；它同时装配 airframe/motion adapter、mission package 与签名密钥、安全包络、模型和健康监控、搜索策略、loop、MissionExecutor，以及启动期任务计划。计划中第 3 步原估 900–1,300 行搬移，实际依赖面与估算一致，且不是单一 runtime 子系统。
- 第 3 步重做（本次首个子阶段）：将 `OnboardPolicy.__init__` 中最终安全包络的装配逻辑移至 `onboard_policy_parts/runtime.py::build_safety_envelope`。helper 仅接收 options、airframe profile/adapter、mission package、home 与 ground projection，不读取 facade globals，也不导入 `scripts.*`；facade 仍负责按原顺序把同一 envelope 赋给 `motion_adapter.safety`。其余构造和任务包准入方法尚未迁移；本子阶段不代表第 3 步整体完成。新 runtime 模块加入 parity 文件清单及生成 parity JSON，仍属第 6 步收尾要求，本次不改受证据约束的 JSON。
- 本次不直接抽出 `__init__` 和 operator-package 方法：除大量可从 `multidetect.*` 导入的领域类型外，构造路径还调用 facade 定义的 `build_mission_from_text`，并在函数内部延迟导入 `scripts.eval_slow_system.LlamaServerRunner`、`_http_json`；任务准入方法也依赖这些 runner/helper。若只把方法搬进新模块，必须将这些可替换依赖逐项作为类属性/构造依赖注入，并确保 facade 子类属性在运行时读取；若把辅助实现一并迁走，则会扩展到慢模型运行时和计划编译流程，超出本阶段的窄范围。`build_mission_from_text` 自身还延迟导入 `scripts.slow_system_runtime`，因此不适合作为新模块中的直接实现依赖。新模块不得导入 `scripts.*`，也不得复制 facade globals。
- 已用 `rg` 核对现存测试对 `scripts.onboard_policy` 的 monkeypatch。需保留的实际调用关系如下：
  - `YoloeProposer`：`test_onboard_policy_package.py`、`test_onboard_ground_policy.py` 直接 patch facade；`test_onboard_main.py`、`test_onboard_runtime_policy.py` 用字符串路径 patch。均须影响 `OnboardPolicy.__init__` 创建 proposer 的路径。
  - `HealthMonitor`：`test_onboard_health.py` 在构造后检查监控器并在失败路径检查记录；均须影响 `__init__` 的健康监控构造路径。
  - `build_mission_from_text`：`test_onboard_model_split.py`、`test_onboard_operator_model_split.py`、`test_onboard_health.py` patch facade；分别覆盖启动规划、operator package 规划、失败记录及取消/错误路径，必须影响构造或 `prepare_operator_package` 内实际规划调用。
  - `compile_mission_plan`：`test_onboard_policy_package.py` patch facade；目前由仍在 facade 的 `build_mission_from_text` 查找，若该 helper 保留在 facade，此 patch 路径可继续成立。
  - `pixel_ground_point`：`test_place_memory_integration.py` patch facade；本阶段若不迁移后续感知/投影方法，该路径不受影响。
  - `time.perf_counter`：`test_onboard_health.py` patch `onboard_policy.time` 的共享模块对象；保持新旧模块引用同一 `time` 模块时可继续生效。
  - `hashlib.sha256`、`Path`：`test_onboard_model_split.py` 仍覆盖 facade 导出的对象；属于第 2 步 `model_config.py` 的契约，本阶段不得破坏。
- 建议替代切分：把第 3 步拆成可独立验证的小步骤。先从 `__init__` 中选没有 facade 专属实现的纯领域构造器/计算片段，建立明确的依赖参数对象，并以 facade 子类类属性注入需要 monkeypatch 的 `YoloeProposer`、`HealthMonitor` 等依赖；每次只搬一个有边界的子系统。将任务计划编译/慢模型 runner 保留在 facade 的兼容边界，后续单独设计不依赖 `scripts.*` 的 runtime adapter 后再迁移。开始任何方法搬移前，为上列每个 patch 目标补充或确认测试，证明 patch 修改的是实际执行路径。若找不到职责足够小且不牵涉这些 facade 专属 helper 的首个片段，应继续只拆纯辅助函数，不推进 runtime。
