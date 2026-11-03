# Linux 全量 pytest 失败分诊（2026-10-31）

## 执行范围

使用主环境 `/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python`，设置
`PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp CUDA_VISIBLE_DEVICES=''`，单进程执行：

```text
python -m pytest tests -q -p no:xdist --continue-on-collection-errors --tb=short
```

完整套件在约 35.8% 处进入单例长耗时，外层 20 分钟限时内未能取得最终汇总，因此以下数量是已复现、可定位的失败，不把进度条中的 `F` 字符当作精确失败数。回归闸门另行执行并完成到最终汇总。

## 汇总

| 类别 | 已确认数量 | 说明 |
|---|---:|---|
| A 缺本机数据/证据 | 54 个测试（其中 53 个来自 `test_current_progress_index.py`） | 共享 `artifacts/` 中的证据不在当前 worktree，或目标文件缺失 |
| B Linux/跨平台 | 0 | 本轮没有取得可归因于路径分隔符、换行、权限、`.exe` 或 PowerShell 的失败 |
| C 真实回归/其它 bug | 11 个收集/执行项 | 可选依赖未安装，以及无 torch 时训练脚本暴露 `Dataset` 名称错误 |
| D 沙箱限制 | 3 个 | 测试创建本地 socket 时被沙箱拒绝：`PermissionError: [Errno 1] Operation not permitted` |

## A：缺本机数据/证据

缺失项按数据分组如下。`artifacts/` 是指向主仓库共享目录的只读软链接；当前 worktree 的校验逻辑要求证据路径位于当前仓库根，导致部分外部目标同时表现为“越出仓库根”。

| 数据组 | 缺失路径/表现 | 受影响测试 |
|---|---|---|
| parity foundation | `artifacts/evaluation/parity-final-20261021-pre9-20261021-final2/v030-pre1-foundation.json` | `test_current_progress_index.py` 中 foundation 绑定及其漂移断言 |
| QGC combined-SIM | `artifacts/evaluation/parity-teacher-admission-20261021-pre9-20261021-final2/copies/combined-evidence-parity-r14-parity-r11--c46392abc2cf.json` | `test_current_progress_index.py` 的 QGC 绑定测试 |
| 外部 fixed-wing depth Mission | `external-fixed-wing-depth-shared-policy--27f1641dd414.json` 及其绑定证据 | `test_current_progress_index.py` fixed-wing 绑定/漂移测试 |
| 外部 mission matrix | `artifacts/evaluation/parity-final-20261021-pre9-20261021-final2/external-mission-matrix.json` | `test_external_mission_matrix_receipt.py`、`test_current_progress_index.py` |
| shared-policy VTOL/runtime/campaign | `shared-policy-current-vtol-campaign-2026-f8526547db73.json` 等 parity copies/receipt | `test_current_progress_index.py` 对应绑定测试 |
| release-gate、field-link、observed-session、replay/campaign | release-gate report/manifest、field-link report、observed-session、campaign、positive-replay 等产物 | `test_current_progress_index.py` 的绑定和缺失/漂移测试 |

建议：在具备完整证据包的主仓库工作树中运行，或由证据生成器恢复这些固定路径和 SHA；不要手工编辑受证据约束的 JSON。

## B：Linux/跨平台

本轮未确认 B 类失败。已执行的测试失败均没有给出 Windows 路径、换行、`.exe`、PowerShell 或大小写相关 traceback。后续若在完整套件完成后出现此类错误，应逐条记录测试名、断言中的平台假设和对应源文件。

## C：真实回归/其它 bug

| 测试/收集项 | 根因 | 涉及源文件 | 建议修法 |
|---|---|---|---|
| `test_fetch_and_audit_openfly_parquet.py`、`test_extract_openfly_parquet_images.py` | 主 `.venv` 未安装 `pyarrow`，导入阶段失败 | `tests/test_fetch_and_audit_openfly_parquet.py` | 将该测试组放入明确的 vision/data 测试依赖，或在缺依赖时显式 `importorskip` 并标注 optional |
| `test_mission_gcs_contract.py` | 未安装 `cryptography` | `tests/test_mission_gcs_contract.py` | 安装 security extra，或将依赖声明/跳过策略写入测试配置 |
| `test_open_vocab_detector.py` | 未安装 `PyYAML`（模块名 `yaml`） | `tests/test_open_vocab_detector.py` | 安装测试所需 YAML 依赖，或显式 optional skip |
| `test_build_onboard_openfly_navigation_dataset.py::test_complete_original_step_dataset_native_cpu` 及两个参数化用例 | 构建器导入 `yaml` 失败 | `scripts/build_onboard_openfly_navigation_dataset.py` | 在运行环境安装 PyYAML；代码本身不应静默替代解析器 |
| `test_onboard_navigation_feature_capture.py`、`..._v2.py`、`test_onboard_navigation_native_prefix_adapter.py` | 主 `.venv` 无 `torch` | 对应三个测试文件及其 VLM/导航实现 | 使用 `.venv-vlm` 或给测试增加明确依赖标记；CPU-only 不等于无 torch |
| `test_onboard_campaign_retry.py` 收集错误 | `scripts/train_policy_v2_bc.py` 在 torch 导入失败后仍定义 `class _TransitionDataset(Dataset[Transition])`，`Dataset` 未定义 | `scripts/train_policy_v2_bc.py` | 缺 torch 时在模块边界显式失败/跳过，或把 torch 相关定义延迟到依赖检查之后 |

这些项目前没有证据表明是 Linux 语义回归；其中依赖缺失属于测试环境/依赖声明问题，`Dataset` 项属于真实的无可选依赖导入缺陷。

## D：沙箱限制

回归闸门执行结果为 `3 failed, 75s`，均在创建本地 socket 时收到 `PermissionError: [Errno 1] Operation not permitted`：

| 测试 | 根因 | 涉及源文件 | 建议修法 |
|---|---|---|---|
| `tests/test_llama_server_logs.py::test_port_wait_ignores_time_wait_after_server_close` | 沙箱禁止 socket 创建 | `tests/test_llama_server_logs.py` 及被测 llama 日志/端口等待实现 | 在允许本地回环 socket 的环境运行；测试本身无需改行为 |
| `tests/test_onboard_main_operator_link.py::test_start_up_mission_is_bound_and_held_for_operator_approval` | 沙箱禁止本地通信 socket | `tests/test_onboard_main_operator_link.py` 及 operator-link 实现 | 在非受限 Linux 主机复跑 |
| `tests/test_replay_set.py::test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations` | `ThreadingHTTPServer` 创建 socket 被拒绝 | `tests/test_replay_set.py`、`src/multidetect/replay_annotation_workbench.py` | 在允许 loopback bind 的环境复跑 |

## 验证记录

- `scripts/run_onboard_gate.py -q -p no:warnings`：失败仅为上述 3 个沙箱 socket 限制；未见其它 gate 失败。
- `tests/test_gazebo_sensor_stream.py` 单独运行：`145 passed`（未复现 Linux 特有失败）。
- `tests/test_audit_onboard_navigation_dataset_overlap.py`：`46 passed`。
- `tests/test_audit_onboard_visual_dataset.py`：`29 passed`。
- `tests/test_build_onboard_openfly_original82_supervision.py`：`61 passed, 1 skipped`。

全量套件因长耗时未达到最终 pytest 汇总；因此本文件没有声称已覆盖剩余 64.2% 的测试，也没有把未观察到的失败推断为 B/C。

## 第二轮（全量）

2026-10-31 在同一主环境按测试文件分批执行。每个文件单独启动
`/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest <file> -q -p no:xdist --tb=short`，外层
`timeout 90s`；环境变量仍为 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp CUDA_VISIBLE_DEVICES=''`。
共扫描 810 个 `tests/test_*.py` 文件：660 个文件正常结束且无失败，134 个文件含失败或错误，14 个文件因无
torch 或 `huggingface_hub` 整模块跳过，另有 2 个文件触发单文件超时。文件级结果会受到同一套件中缺失证据、沙箱网络限制和可选依赖
缺失的影响，不能把失败文件数直接解释为产品回归数。

本轮发现的超长/卡住文件：

- `tests/test_onboard_fault_campaign.py`：90 秒超时。
- `tests/test_round_collect_preflight.py`：90 秒超时。

修复涉及的无 torch 测试现在以明确原因跳过；主环境中以下模块的收集不再报 `NameError` 或裸导入错误：
`test_onboard_navigation_feature_capture.py`、`test_onboard_navigation_feature_capture_v2.py`、
`test_onboard_navigation_native_prefix_adapter.py`、`test_policy_v2_model.py`、`test_policy_v2_training.py`。
其中后两个是既有 policy-v2 依赖门槛，跳过原因均为 `torch` 未安装。`test_onboard_campaign_retry.py` 可完整收集 11 个
测试；执行阶段仍受只读共享 `artifacts/` 和既有 mock 调用计数断言影响。
两个 parquet 测试文件在主环境中也会在收集边界跳过 `huggingface_hub`，避免其版本元数据缺失在 fixture setup
阶段变成错误；`pyarrow`、`cryptography`、`yaml` 的 skip 原因同样写在对应测试文件中。

回归闸门：

```text
scripts/run_onboard_gate.py -q -p no:warnings
3 failed, 约 72s
```

3 个失败均为当前沙箱禁止创建本地 socket（`PermissionError: [Errno 1] Operation not permitted`）：
`tests/test_llama_server_logs.py::test_port_wait_ignores_time_wait_after_server_close`、
`tests/test_onboard_main_operator_link.py::test_start_up_mission_is_bound_and_held_for_operator_approval`、
`tests/test_replay_set.py::test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations`。

## 2026-11-03 全量复测与分类

执行环境为主 `.venv`，设置 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp`，使用
`-p no:cacheprovider --continue-on-collection-errors --tb=short`。pytest 没有安装
`pytest-timeout` 插件；本轮全量进程由外层 1800 秒 timeout 结束在 100% 进度条后的汇总阶段，
因此没有可信的最终 pytest 总计。完整输出保存在工作树临时文件
`.tmp/pytest-full-20261103.log`（未纳入交付）。日志在修复前产生，尾部列出 528 个失败项和 284 个
collection/setup error 项；这些数目包含下表中后来修复的项，不能解释为修复后的当前失败总数。
本次禁止启动 SITL、Docker、GPU 训练；没有通过标记或删测试来隐藏环境限制。

| 测试/测试组 | 分类 | 证据/根因 | 修改或未修改 | 当前验证 |
|---|---|---|---|---|
| 22 个 teacher/operator/mission collector 文件的收集错误，包括 `test_run_onboard_teacher_sim_operator_v2.py`、`test_teacher_live_checkpoint_probe_v1.py` | 已修复：冻结身份级联更新 | HEAD 的 `edb89d87` 修改 `scripts/onboard_teacher_gazebo_session.py`，引入 Linux Docker bridge gateway telemetry endpoint；该文件为 V3 固定 hash 的基类，随后 V4/operator/V2 再固定上游源。原 guard 按字节数和 SHA-256 在导入时拒绝不匹配 | 依次刷新 V3→V4→operator→operator V2 的 pins 和测试里的身份断言；没有绕过 guard | 修复后相关集 207 passed、1 skipped；更晚一次相同集合运行出现 2 个 Docker bridge 探测后 session deadline 失败（见下行），冻结 guard 本身已通过导入和测试。
| `test_onboard_teacher_gazebo_session.py` stand-in mount 验证 | 已修复：测试 fixture 陈旧 | 生产计划在有 `fuel_resolved_root` 时增加只读 Fuel cache mount，native Docker inspect stand-in 未反映此 mount | stand-in 按生产计划补齐只读 mount，没有改生产验证条件 | 包含在 teacher/operator 相关定向验证中。
| `tests/test_onboard_campaign_retry.py`，特别是 `test_complete_replacement_reduces_missing` | 已修复：测试 mock 与当前行为陈旧 | 当前 campaign 在报告 provenance 构建中调用 git；旧 mock 把所有 `subprocess.run` 都算作 collector。默认报告路径指向只读共享 `artifacts/`。当前完整替换语义仍要求只发起一个 collector batch | 测试为报告指定 tmp 路径；仅统计命令包含 `collect_sim_teacher_episodes.py` 的调用；在测试边界 stub provenance；保留 1 batch 断言 | 当前文件单独运行 11 passed。
| `test_onboard_teacher_gazebo_session_v4.py::test_owned_session_reuses_frozen_lifecycle_and_closes_rx_on_partial_api_error`、`test_onboard_teacher_sim_operator.py::test_normal_one_change_all_protected_readbacks_and_closed_outputs` | 环境敏感的 Docker bridge 探测 | 较早合并批次及单独运行时，Docker network inspect 失败/超时后触发 session deadline；测试使用 mock native Docker，不启动容器，但生产 endpoint 规划会探测 bridge | 未调大 deadline 或放宽行为；保持测试 | 后续重新运行完整 teacher/operator 定向集合成功（exit 0，含上述两项）；本机 Docker probe 仍打印 fallback warning，真实 Docker/SITL 未运行。
| `test_operator_udp.py`、`test_alerts.py`、`test_px4_continuation_wire.py`、`test_field_link_preflight.py`、`test_simulator_orchestrator_*`、`test_live.py` 等 socket/loopback 测试 | 环境限制：沙箱禁止 socket | traceback 为 `PermissionError: [Errno 1] Operation not permitted`，包括 UDP socket、loopback 服务或 HTTP server 创建 | 保留测试；未加 skip，因为限制属于本次执行沙箱，不是测试的产品前置条件 | 全量基线日志可见；当前沙箱不能验证协议行为。
| `test_current_progress_index.py`（53 项）及 release/parity/evidence 绑定组；`test_external_mission_matrix_receipt.py` | 环境/证据缺失 | 固定证据位于共享 `artifacts/`；部分目标缺失，另一些解析到主仓库共享目录而越过当前 worktree 根；`artifacts/` 按要求只读 | 未改受证据约束 JSON，未写 artifacts | 未解决；需在具有相应完整证据包的正式仓库环境运行。
| VLM/training/navigation evidence suites（包括 `test_onboard_vlm_grounding_training_evidence.py`） | 环境/证据缺失 | 主 `.venv` 不含 torch；VLM evidence 路径指向共享 artifacts。部分测试明确以 `importorskip` 跳过 torch，而依赖已在 fixture setup 才导入的测试会报错 | 未改测试跳过策略或 evidence-bound files；本任务指定主 `.venv`，不切换到模型环境，不运行 GPU 训练 | 基线日志中有 torch 缺失错误及越出 worktree 的 evidence 路径错误；需 `.venv-vlm` 和完整证据才能验证。
| `test_bind_openfly_parquet_route_metadata.py`、`test_fetch_and_audit_openfly_parquet.py` 等 | 环境依赖缺失 | 主 `.venv` 缺少 `huggingface_hub` distribution metadata（fixture setup 中 `PackageNotFoundError`）；部分 parquet 用例还需要 pyarrow | 未联网安装依赖；已有显式 optional import skip 的测试保留其策略 | 全量基线日志中仍有 setup error；需准备项目声明的可选数据依赖后复验。
| `test_onboard_teacher_telemetry_capture_v2.py` 的 4 个真实 dialect 用例 | 环境路径陈旧/Windows fixture | fixture 硬编码 `.venv-gazebo-navigation-20261008-r1/Lib/site-packages/...` Windows 路径，并要求该本地 dialect 文件存在 | 本轮未改，因固定 dialect/source identity 和适用 fixture 应先确认设计意图；这是明确的跨平台可移植性候选问题，不归因于 Ubuntu 核心行为 | 日志显示 `Pinned local primary dialect required`；需要确定应将固定文件随测试夹具提供，还是把真实安装依赖测试限制在相应环境。
| `test_verify_onboard_navigation_recovery_identity.py` Windows argv 用例；`test_llama_server_lifecycle.py::test_windows_*` | Windows 专用行为测试在 Linux 上执行 | 前者 fixture 原先硬编码不存在的 `python.exe`，且解析器确实依赖 Windows `CommandLineToArgvW`；后三项调用 PowerShell/`ctypes.WinDLL` | 将 recovery fixture 绑定到当前解释器；对仅依赖 Windows API/路径语义的测试加 `skipif`，并补齐 unrelated-owner 的进程存活 mock；未改生产 Windows 行为 | Linux 上 `test_llama_server_lifecycle.py` 为 `3 skipped, 3 passed`，Windows argv 用例 `1 skipped`；需 Windows runner 验证实际 Windows 分支。
| `test_onboard_teacher_gazebo_observation_v3.py` 的 lazy import/setup 项 | 受冻结源 guard 级联影响，或尚待复核 | 全量基线日志中的 setup 尝试重新导入 V3，发生在 hash 刷新前；同文件另有 4 个逻辑测试没有从全量 summary 确认根因 | 没有为其余逻辑测试作推断 | 修复后未单独重跑此文件；当前分类未完成，须重跑并逐项看 traceback。
| `test_analyze_onboard_navigation_original59.py`（10 项）及其它没有上述明确 traceback 的失败 | 尚未分类：可能是回归，也可能是 evidence/fixture 缺失 | 全量日志列出失败名，但未在本轮为每个 failure 建立独立 traceback 分类；不能仅凭文件名归入环境问题 | 未改断言或实现；保留为待核查项 | 未逐项复跑，不能宣称通过。

## 修复后验证

| 命令 | 结果 |
|---|---|
| 主环境运行 `tests/test_onboard_campaign_retry.py` | `11 passed` |
| 主环境运行 teacher/operator 定向集合（V3、V4、operator V1/V2、worker/probe tests） | 最后运行 exit 0，全部通过，含 1 个 skip；未启动 Docker/SITL |
| 较早单独复跑两个 Docker deadline 用例 | 当时 `2 failed`；后续同两项纳入定向集合均通过，说明失败受 bridge 探测时间影响 |
| 单独运行 `tests/test_llama_server_lifecycle.py` | `3 skipped, 3 passed`；Windows API/路径语义测试在 Linux 上显式跳过 |
| 全量 pytest | 进度到 100%，1800 秒外层 timeout 在最终汇总时结束；日志基线为修复前，不能作为修复后全量结果 |

截至本记录，campaign 和冻结源 guard 的核心修复有通过的定向验证；全量套件仍未达到 green，也未完全分类。剩余真实回归候选、Windows 专用测试标记、缺失 dependencies/evidence、Docker 可用环境以及 socket 沙箱限制均已明确列出，不把未知项伪装成已修复。

## 2026-11-03 分片全量最终结果

为避免单个长文件阻塞全量汇总，按 `tests/test_*.py` 文件拆分执行。每个文件独立启动主环境
`/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest <file> -q -p no:cacheprovider
--continue-on-collection-errors --tb=short`，外层 `timeout 240s`，最多 8 个进程并行；环境变量为
`PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp CUDA_VISIBLE_DEVICES=''`。所有进程均在截止时间内结束，
没有文件级超时或遗漏结果。

| 项目 | 最终结果 |
|---|---:|
| 测试文件 | 827（827/827 有退出状态和 JUnit XML） |
| 收集/执行测试项 | 15,760 |
| 通过 | 14,770 |
| 失败 | 443 |
| 错误 | 143 |
| 跳过 | 404 |

上述数字来自分片 JUnit 汇总；原始日志和 XML 保存在 `.tmp/pytest-full-20261103-final/`，不纳入交付。失败/错误的确定分类如下：

| 分类 | 代表文件/测试 | 证据与处置 |
|---|---|---|
| 本次 Linux 沙箱限制 | `test_px4_continuation_wire.py`、`test_operator_udp.py`、`test_simulator_interactive_router.py`、`test_llama_server_logs.py`、`test_replay_set.py` 等 socket/HTTP 用例 | traceback 为 `PermissionError: [Errno 1] Operation not permitted`，发生在 `socket.socket` 或 `ThreadingHTTPServer` 创建处。保留测试和断言；需允许 loopback/UDP 的 Linux runner 复跑。未启动 SITL、Docker 或真实设备。 |
| 共享证据/产物不可用 | `test_current_progress_index.py`、`test_external_mission_matrix_receipt.py`、VLM/evidence binding 文件 | 目标解析到共享主仓库 `artifacts/` 或缺失固定证据，校验明确报越出当前 worktree 或文件不存在。`artifacts/` 只读，未改任何 evidence-bound JSON；需完整证据包环境复跑。 |
| 主 `.venv` 可选依赖缺失 | 以 torch/transformers/peft 训练与 VLM 文件为主，另有 parquet/huggingface 元数据用例 | 错误是 `ModuleNotFoundError`/`PackageNotFoundError`，不是产品断言。主环境无 torch；相关 CPU fixture 已改用带原因的 `pytest.importorskip`，不联网安装、不切换 `.venv-vlm`；应在声明的 VLM/data extra 环境运行。 |
| 固定本机 fixture 陈旧（已修复） | `test_onboard_teacher_telemetry_capture_v2.py` 的 4 个真实 dialect 用例 | 原 fixture 硬编码 Windows `.venv-.../Lib/site-packages`。改为从当前解释器解析 `pymavlink`，缺包时显式 `pytest.skip`；定向结果通过。 |
| Windows 专用行为（已标记） | `test_verify_onboard_navigation_recovery_identity.py::test_readonly_exact_chain_followup_closes_reload_not_original_failure` | 被测函数明确调用 Windows `CommandLineToArgvW`；Linux 上原先误执行并报 bounded command line。增加 `skipif(os.name != "nt")`，Windows 行为和专用 parser 测试保持不变；定向结果为显式 skip。 |
| 其余失败/错误 | 其余 failure/error 节点（包括固定身份/配置摘要、并行时序、历史 evidence 快照和若干真实断言） | 已逐文件保留在 JUnit/XML 中，不能从文件名安全推断为环境问题。未删除测试、未放宽断言、未修改受证据约束文件；后续应按 XML 中的具体 traceback 在有完整证据、允许网络和匹配依赖的环境逐项复验。 |

本轮修改文件：`tests/test_onboard_teacher_telemetry_capture_v2.py`（Linux 可移植 dialect fixture）、
`tests/test_verify_onboard_navigation_recovery_identity.py`（POSIX 显式跳过 Windows API 分支）。定向验证：
两文件合计通过，只有 Windows 专用分支显式跳过。全量分片结果用于最终覆盖和分类，未宣称在当前沙箱中 green。
