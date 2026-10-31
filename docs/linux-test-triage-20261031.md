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
