# 2026-11-03 training / simulator / misc 逐测试分诊结果

本记录对应 `docs/linux-test-triage-20261031.md` 的接手组附录。原始失败取自 `/data/projects/Multi-Detect-env/runs/pytest-xml-20261103/`，共 60 个文件、242 个 failure/error 节点（初始 59 文件/241 节点，加 gate 补入的 operator-link 节点）。每行 testcase 都采用其文件小节明确列出的分类与 action；当前状态来自本工作树最终单进程回归，而非旧全量 XML。

release/gr01、onboard_navigation evidence、onboard_runtime/covariance 分组未接手。未修改 artifacts 或用户列出的受约束配置；未手工刷新其它证据绑定。没有启动 SITL、Docker 容器、GPU 训练或联网下载。部分现有测试调用只读 Docker bridge discovery 并收到 fallback warning；没有因此启动容器。

## 验证结果

| 范围 | 命令或方式 | 结果 |
|---|---|---|
| 点名六文件、operator-link 与新增 fixture 自测 | 主 `.venv/bin/python -m pytest <8 个文件> -q -p no:cacheprovider` | 171 项：150 passed、21 skipped、0 failed/0 errors |
| 本组原始 60 文件，加 fixture 自测和 v5 契约 | 主 `.venv/bin/python -m pytest <62 个文件> -q -p no:cacheprovider -m "not timing"` | 1252 项：1062 passed、186 skipped、4 failed、0 errors |
| CPU onboard gate | 主 `.venv/bin/python scripts/run_onboard_gate.py -q -p no:warnings` | PASS，187 秒 |
| CLI 真实短期跟踪 benchmark 单独复现 | `tests/test_cli.py::test_short_term_tracking_bench_exercises_image_recovery_without_hardware` | 单独通过；此前集合中因 P95 超预算失败，按 timing 单列 |
| Ruff 修改前后对照 | 每个修改 Python 文件检查 HEAD 与当前源码 | 同为 43 项既有诊断，无新增；新增 fixture 自测检查通过 |
| 差异检查 | `git diff --check`；核对受约束路径 | 通过；无受约束路径修改 |

环境为 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp`，解释器为 `/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python`。没有删除测试或放宽断言；timing 用例从确定性集合中显式分离，不计作 passed 或 skipped。原始节点的当前状态汇总见下方。

最终四个失败来自 `test_gazebo_3d_mainline_manifest.py` 的 multirotor aggregate hash、`test_onboard_rendered_3d_dataset.py` 的同一上游 mainline 绑定，以及 `test_simulator_terrain_truth.py` 的两项 release-parity 绑定。均保留原验证和失败，需正式生成器/证据流程恢复。

真实性能/线程 deadline 用例需安静 runner；真实 tensor/model/gradient 用例需声明的 VLM 可选依赖环境；未做 Qt 原生构建、Windows runner、SITL 或真实设备验证。

## 原始 failure/error 节点逐项结果

原始 242 个节点：timing 排除 1；显式 skip 186；未修复失败 4；通过 51。

### `test_alert_link_bench.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_authenticated_udp_alert_link_bench_exercises_real_loopback_socket` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_alerts.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_authenticated_udp_alert_round_trip_returns_correlated_ack` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_authenticated_udp_receiver_deduplicates_retransmitted_alert_id` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_authenticated_udp_receiver_rejects_wrong_shared_key` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_approach_acceptance.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_mode3_arbitrary_targets_reach_advice_and_unsafe_evidence_aborts` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_bind_openfly_parquet_route_metadata.py`

分类：陈旧测试 fixture。

处理：fixture 已伪造 huggingface_hub 源包，却漏掉 build_plan 所需的 distribution version；补齐定向 metadata mock，生产版本绑定保持。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_plan_proves_original_train_annotation_episode_identity` | 通过 |
| `test_only_exact_three_metadata_rows_success_and_create_only` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>0]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>1]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>2]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>3]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>4]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>5]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>6]` | 通过 |
| `test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail[<lambda>7]` | 通过 |
| `test_future_payload_sum_cap_is_not_permission_to_download` | 通过 |
| `test_explicit_scope_tampering_rejected_before_network[<lambda>0]` | 通过 |
| `test_explicit_scope_tampering_rejected_before_network[<lambda>1]` | 通过 |
| `test_explicit_scope_tampering_rejected_before_network[<lambda>2]` | 通过 |
| `test_explicit_scope_tampering_rejected_before_network[<lambda>3]` | 通过 |
| `test_explicit_scope_tampering_rejected_before_network[<lambda>4]` | 通过 |
| `test_plan_hash_and_input_drift_rejected_before_network` | 通过 |
| `test_source_drift_during_request_retains_failure_report` | 通过 |
| `test_document_cannot_differ_from_retained_metadata` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[None]` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[status]` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[auth]` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[length]` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[encoding]` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[stream_cap]` | 通过 |
| `test_public_request_auth_redirect_and_stream_caps[truncated]` | 通过 |

### `test_cli.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。 short-term image benchmark 在扩展集合中超 P95、单独复跑通过，故增加 timing 标记；原 66.7ms 和恢复/更新率断言保持。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_operator_udp_authorization_cli_closes_signed_protocol_hil_loop` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_count_coverage_completion.py`

分类：陈旧测试。

处理：8c1d3552 增加 revisit 规划快照；完整字典等值断言补齐 revisit/rejection 两个空候选快照及前后预算 60。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_count_coverage_completion_hands_off_to_home` | 通过 |

### `test_depth_companion_replay.py`

分类：真实代码 bug（跨平台）。

处理：POSIX Path.name 不识别反斜杠与 Windows drive-relative 名称；同时检查 POSIX/Windows 文件名，新增三类拒绝参数与合法文件名验证。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_companion_replay_rejects_manifest_child_escape` | 通过（扩展为 3 个参数化负例） |

### `test_depth_grid_udp.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_depth_grid_publisher_uses_fixed_return_port_after_keepalive` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_depth_grid_publisher_coalesces_display_frames_to_configured_rate` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_depth_grid_publisher_serializes_concurrent_publish_calls` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_diagnose_onboard_teacher_px4_events.py`

分类：环境：固定 PX4 头文件缺失。

处理：仅依赖原始 mavlink_msg_*.h 的 fixture/golden 用例检查固定 evidence 目录；缺文件明确 skip，纯 CRC/wire 用例照常运行，已有文件的字节数/SHA 断言保持。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_crc_golden_and_exact_header_bindings` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_full_closed_fixture_keeps_unknowns_and_diagnostic_only` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[accepted]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[indexbool]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[notclosed]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[tail]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[slice]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[packetsha]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[path]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[recordcount]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_capture_binding_fail_closed[capture_error]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_packet_count_cap_before_decode` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_late_source_drift_rejects_result` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_header_mutation_rejected_before_decode` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_rebound_bytes_cannot_hide_row_identity_drift[projection_kind]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_rebound_bytes_cannot_hide_row_identity_drift[projection_index]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_rebound_bytes_cannot_hide_row_identity_drift[projection_timestamp]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_rebound_bytes_cannot_hide_row_identity_drift[raw_header]` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_bad_crc_with_all_artifact_hashes_rebound_still_rejected` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_create_only_output_exact_bytes_and_failure_no_overwrite` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |
| `test_fsync_failure_keeps_partial_and_no_success_binding` | 显式 skip：pinned PX4 event definition evidence is missing: /data/projects/Multi-Detect-env/mdwt/cx-triage-misc/artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1/mavlink_msg_event.h |

### `test_eval_server_recovery.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_a_crashed_server_is_restarted_and_the_same_request_retried` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_restart_budget_exhausted_stops_the_evaluation` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_a_failed_restart_is_a_dead_server_not_a_row_error` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_field_replay_campaign.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_campaign_annotation_ui_runs_bound_loopback_workbench` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_fixed_wing_landing_and_smoke.py`

分类：陈旧测试 fixture。

处理：当前 ACK waiter 按进入等待时的 monotonic_ns 校验新鲜度；原 +1ns 在调用前已经过期。固定 waiter 起点，保留 per-command ACK 不被其它 command 覆盖的精确断言。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_command_ack_wait_survives_a_later_ack_for_another_command` | 通过 |

### `test_gazebo_3d_mainline_manifest.py`

分类：源/证据绑定漂移，未修复。

处理：当前 stack 哈希覆盖完整 Gazebo model root，已提交 mainline manifest 的 aggregate_sha256 不匹配。不得削弱验证；需正式重生成 manifest 并重新绑定下游模型/证据，未改配置或产物。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_primary_gazebo_manifest_is_an_offline_three_airframe_3d_audit` | 未修复失败：保留源/证据绑定检查，需正式生成流程 |

### `test_identity_prompt_variants.py`

分类：陈旧测试 mock。

处理：生产 _http_json 增加 timeout 关键字；只补 reply mock 签名，完整 body/cache_prompt/usage 断言保持。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_llama_request_uses_variant_and_cold_prefill_cache_setting` | 通过 |

### `test_live.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_live_runner_closes_pixhawk_remote_authorization_and_two_channel_hil` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_live_patrol_delivers_over_authenticated_udp_to_persistent_ground_store` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_authenticated_udp_pending_alert_recovers_after_ground_restart` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_llama_server_logs.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_port_wait_ignores_time_wait_after_server_close` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_mission_operator.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_signed_mavlink_udp_endpoint_dispatches_mission_messages` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_telemetry_acks_have_their_own_rate_bucket` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_onboard_3d_training_pipeline.py`

分类：环境：共享模型产物。

处理：本 worktree 的 artifacts 软链接解析到主仓库，model.path 越出受校验仓库根；增加明确的 repository-local 绑定模型前置 fixture，缺失/外部路径时 skip，不绕过生产边界。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_pipeline_preflight_only_keeps_current_parent_and_claim_boundary` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_pipeline_train_fails_closed_without_pixel_and_trajectory_inputs` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_trainer_command_forwards_explicit_cuda_device` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_pipeline_train_rejects_nonempty_candidate_directory_before_subprocess` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_zero_trainer_exit_without_outputs_is_not_training_success` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_candidate_output_gate_rejects_an_extra_case_insensitive_onnx` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_candidate_output_gate_rejects_report_model_bytes_or_hash_mismatch` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_candidate_output_gate_requires_checker_and_inference_smoke_flags` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |

### `test_onboard_3d_training_preflight.py`

分类：环境：共享模型产物。

处理：同上；真实模型、训练配置与训练报告必须是 worktree 内文件，否则明确 skip。未启动训练或 Docker。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_preflight_uses_hash_bound_reference_and_blocks_without_runtime` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_preflight_reports_ready_to_execute_only_when_docker_server_is_reachable` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_preflight_report_is_json_serializable` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_preflight_classifies_docker_desktop_stale_socket` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_preflight_extracts_non_ingest_docker_desktop_socket` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_preflight_keeps_all_reported_stale_socket_paths` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |

### `test_onboard_main_operator_link.py`

分类：环境：socket 沙箱。

处理：仅为实际创建 UDP socket 的 startup mission 用例增加 require_loopback_sockets；只有 EPERM/EACCES 才 skip，其它 OSError 继续失败，审批/日志绑定断言保持。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_start_up_mission_is_bound_and_held_for_operator_approval` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_onboard_model_reference.py`

分类：环境产物/陈旧测试 fixture。

处理：当前模型绑定集成用例缺 repo-local artifacts 时明确 skip；篡改单元测试在 tmp_path 内独立 repository 创建四个 hash-bound 输入，先证明合法引用通过，再完整验证外部路径拒绝和 SHA-256 mismatch，不再写共享 artifacts。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_current_reference_hash_binds_r33_and_mainline_3d_manifest` | 显式 skip：current onboard model requires repository-local bound artifacts: /data/projects/Multi-Detect-env/Multi-Detect/artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx |
| `test_reference_rejects_a_changed_model_digest` | 通过 |

### `test_onboard_rendered_3d_dataset.py`

分类：源/证据绑定漂移，未修复。

处理：合法 mainline 自身的 aggregate 绑定已漂移，导致自报 hash 负例先命中上游 manifest 错误；保留原异常断言，等待正式 manifest/下游证据重生成。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_mainline_binding_rejects_self_declared_hashes` | 未修复失败：保留源/证据绑定检查，需正式生成流程 |

### `test_onboard_teacher_mission_execution_v1.py`

分类：环境时序/fixture 隔离。

处理：XML 为 starting admission/context stale；串行复现通过。CPU NativeDocker fixture 显式指定 loopback telemetry host，避免真实 Docker bridge discovery 消耗有限 admission 窗口；没有放大超时/新鲜度阈值。 三个 malformed ACK 参数化用例涉及 RX 线程、durable journal 和 200ms deadline，显式 timing 标记；未放大 deadline 或改变 ACK 断言。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_V7_owned_lifecycle_reuses_unique_RX_for_finite_transfer_and_controls` | 通过 |

### `test_onboard_teacher_px4_parameter_diagnostics.py`

分类：环境：固定 dialect 不在当前解释器。

处理：优先原固定路径；缺失时在当前 pymavlink 安装中查找 common.py，仅字节数和 SHA 完全匹配时用于 fixture，否则明确 skip。生产 COMMON pin 未改。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_closed_actual_writer_source_capability_and_original_nan_union` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_changed_version_identity_blocks_encoding` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_original_foreign_parameter_remains_foreign` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_whole_journal_hash_change_refused` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_cannot_omit_conflicting_version_already_in_whole_journal` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_original_unknown_cannot_be_reclassified_by_receipt_kind` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_primary_receipt_identity_cannot_disagree_with_original_record[packet-sha]` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_primary_receipt_identity_cannot_disagree_with_original_record[nonbool-source]` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_primary_receipt_identity_cannot_disagree_with_original_record[nonbool-count]` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_projection_embedded_raw_binding_must_equal_selected_receipt_binding` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_raw_header_is_not_opaque_hash_only_metadata` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_repeated_same_firmware_capabilities_ignore_only_packet_sequence` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |
| `test_conflicting_version_cannot_hide_as_nonselected_receipt_kind` | 显式 skip：installed pymavlink common dialect does not match the frozen source identity |

### `test_onboard_teacher_telemetry_capture_v2.py`

分类：陈旧 Windows fixture，HEAD 已修复。

处理：a52ce65f 已改为当前解释器的 pymavlink dialect 发现与缺依赖 skip；本轮逐项复跑通过，无重复修改。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_real_installed_bad_data_class_is_foreign_binary_not_qualification` | 通过 |
| `test_real_primary_pack_parse_numeric_values_preserved[BATTERY_STATUS]` | 通过 |
| `test_real_primary_pack_parse_numeric_values_preserved[GPS_RAW_INT]` | 通过 |
| `test_real_primary_pack_parse_numeric_values_preserved[LOCAL_POSITION_NED]` | 通过 |

### `test_onboard_teacher_telemetry_capture_v3.py`

分类：陈旧 Windows fixture，HEAD 已修复。

处理：复用 a52ce65f 的 portable dialect fixture；本轮复跑通过，无重复修改。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_actual_bad_data_primary_bytes_held_but_foreign` | 通过 |

### `test_operator_link_integration.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_signed_task_only_udp_loopback_plan_approval_and_status` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_operator_udp.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_peer_state_maps_are_bounded_without_evicting_bound_peer` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_round_trips_over_direct_signed_udp_metadata_link` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_selection_guard_uses_injected_runtime_clock_domain` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_requires_matching_post_adoption_acceptance_ack` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_acceptance_ack_fails_closed_on_wrong_binding[sequence]` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_acceptance_ack_fails_closed_on_wrong_binding[run]` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_acceptance_ack_rejects_wrong_peer_timeout_and_out_of_order` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_ack_timestamp_before_deadline_succeeds_deterministically` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_ack_timestamp_after_deadline_fails_deterministically` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_timeout_enqueue_race_does_not_poison_next_ack` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_runtime_status_peer_change_and_old_ack_cannot_revive_next_send` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_background_server_registers_qgc_heartbeat_before_selection` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_authenticated_metadata_peer_lease_expires_without_heartbeat` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_metadata_peer_lease_default_matches_qgc_stale_timeout` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_selection_replay_from_another_udp_source_port_cannot_replace_session_peer` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_background_error_queue_is_bounded_and_aggregated_per_source` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_stale_authenticated_status_is_rejected_before_display` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_duplicate_authenticated_selection_ack_is_throttled` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_real_localhost_udp_selection_and_ack_round_trip` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_background_server_delivers_selection_and_returns_tracking_status` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_background_server_round_trips_bound_authorization_decision` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_background_server_round_trips_bound_continuous_approach_slide` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_background_server_round_trips_bound_payload_target_slide_and_status` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_session_client_binds_and_reports_explicit_ipv4_source` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_session_client_rejects_non_ipv4_source[localhost]` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_session_client_rejects_non_ipv4_source[::1]` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_payload_confirmation_udp.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_udp_independent_confirmation_advances_mission_without_commands` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_udp_receiver_ignores_tampered_packet_then_accepts_valid_evidence` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_udp_confirmation_timeout_does_not_fabricate_release_success` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_payload_hil_mission.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_real_udp_loopback_closes_authorized_fixed_wing_hil_path` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_payload_hil_udp.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_udp_hil_retries_identical_request_and_receives_cached_terminal_results` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_udp_hil_returns_authenticated_rejection_for_wrong_slot` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_udp_hil_never_reports_execution_without_explicit_inert_simulation` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_payload_target_acceptance.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_mode2_payload_hil_closes_signed_loop_and_fails_closed` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_pixhawk_hil.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_real_pymavlink_udp_loopback_maps_fields_and_becomes_stale` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_pixhawk_parameters.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_real_pymavlink_udp_parameter_backup_hil` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_plan_eval_tasks_v3.py`

分类：真实数据 bug。

处理：40ae4713 的 v5 中有五条题目与 v4 重复；仅改写 v5 的对应中英文文本，保留类别、语言、skills、expected_steps 和所有唯一性/意图断言。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_v3_size_languages_coverage_and_held_out_texts` | 通过 |

### `test_plan_eval_tasks_v4.py`

分类：真实数据 bug。

处理：同上，改写五条 v5 重复题目；新增运行 v5 完整契约回归。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_v4_texts_are_unique_across_eval_files` | 通过 |

### `test_probe_onboard_teacher_gazebo_navref.py`

分类：陈旧 Windows 路径 fixture。

处理：隔离 -I -B -S 子解释器改注入实际 checkout/src；保留无 torch/PIL/numpy/pymavlink/cv2 导入断言，Windows 同样使用其当前 checkout。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_fresh_import_is_no_optional_model_camera_or_mavlink_runtime` | 通过 |

### `test_px4_gazebo_qgc_router.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_router_mirrors_px4_and_enforces_independent_return_policies[False]` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_router_mirrors_px4_and_enforces_independent_return_policies[True]` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_router_stop_event_terminates_after_ready_barrier` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_router_bind_failure_never_signals_ready_and_releases_prior_socket` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_qgc_native_flyview_style.py`

分类：真实 UI bug。

处理：任务 no-fly/operating-area/home 标注含私有硬编码颜色；改用既有 qgcPal.colorRed/colorBlue/text，未放宽颜色/控件检查。仅验证 Python 源检查，未进行 Qt 原生构建。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_custom_qml_uses_qgc_tokens_for_ui_colors_and_controls` | 通过 |

### `test_replay_set.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_run_operator_demo.py`

分类：陈旧单元 fixture。

处理：锁所有权用例原先实际探测 Docker 和主机内存；显式注入空容器列表、充足内存、单 slot，保留 foreign owner 与 acquire/release 所有权断言，不启动 SITL。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_sitl_lock_release_preserves_foreign_owner` | 通过 |
| `test_sitl_lock_acquire_and_release` | 通过 |

### `test_run_r15_round.py`

分类：陈旧测试 fixture。

处理：TMPDIR 位于 checkout 导致模拟外部 repo 被 ROOT 优先规则吞掉；为 annotate fixture 指定独立 checkout 边界。另补当前 HEAD 新增的 SitlLock mock，Docker-down 测试不访问 /data/scratch。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_annotate_caches_the_index_worlds_keyed_from_the_episodes_repository` | 通过 |
| `test_annotate_rejects_a_source_root_whose_keys_build_sft_never_looks_up` | 通过 |

### `test_short_term_acceptance.py`

分类：环境：真实 CPU 性能预算。

处理：单独复现仍超过 66.7ms P95；加已有 timing marker 和 quiet-runner 原因，不改 P95/rate/cache/identity 断言。确定性回归用 -m not timing；安静主机性能复验未做。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_short_term_image_acceptance_recovers_same_id_with_bounded_cache` | 按 timing 排除；已有真实 P95 预算失败复现，需安静 runner |

### `test_sim_operator_link.py`

分类：环境：socket 沙箱；另有陈旧 endpoint fixture。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。 追加发现 plan fixture 缺少当前 operator_port 字段，补入原契约端口 14580，保留 endpoint 启动断言。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_fake_gcs_console_uploads_prompt_and_approves_the_onboard_plan` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_console_gets_a_prompt_finding_and_full_summary_after_a_minute_of_heartbeats` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_sim_session.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_heartbeat_details_actually_frees_the_udp_port` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_sim_teacher_live_loop.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_mavlink_reader_drains_real_udp_mavlink2_burst` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_sim_teacher_recording.py`

分类：真实代码 bug。

处理：当前 Pillow 对 uint16 ndarray 显式 mode=I;16 报 Invalid mode for data type，被 fallback 写成伪 PNG JSON。改由 uint16 dtype 推导 PNG16 mode，保留三个格式的精确数组等值断言。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_opt_in_depth_formats_load_to_equivalent_arrays[png16]` | 通过 |

### `test_simulator_3d_control_acceptance.py`

分类：陈旧输出 fixture/只读产物路径。

处理：默认 launch planner job 目录在共享 artifacts；为全部本文件用例把 DEFAULT_LAUNCH_JOB_ROOT 定向 tmp_path，仍执行真实 dry-run planner/hash verification 和全部 gate=True 断言。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_simulator_3d_control_gate_proves_shared_manual_ai_path` | 通过 |
| `test_simulator_3d_control_gate_selects_fixed_wing_assets_and_mode` | 通过 |
| `test_simulator_3d_control_gate_selects_multirotor_x500_assets_and_mode` | 通过 |

### `test_simulator_control_gateway.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_gateway_run_receives_and_rejects_oversized_datagram_without_truncation` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_interactive_router.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_qgc_manual_and_read_only_frames_use_separate_paths` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_qgc_source_and_message_policy_fail_closed` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_malformed_source_port_is_rejected_without_raising` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_px4_telemetry_is_forwarded_byte_for_byte_and_identity_checked` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_px4_telemetry_sink_observes_after_forward_without_rewriting` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_px4_telemetry_sink_failure_is_recorded_without_blocking_qgc` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_watchdog_without_sink_uses_adapter_fallback` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_watchdog_sink_owns_tick_without_duplicate_adapter_poll` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_watchdog_sink_failure_records_error_and_falls_back_to_adapter` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_split_frames_and_signed_throttle_modes` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_qgc_partial_frame_cannot_be_completed_by_another_source` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_px4_partial_frame_cannot_be_completed_by_another_source` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_qgc_control_mode_defaults_read_only_and_blocks_flight_commands` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_sim_flight_forwards_bounded_arm_mode_transition_and_rtl` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_sim_flight_lifecycle_sink_updates_local_model_without_changing_forwarding` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_sim_flight_rejects_unsafe_command_parameters_and_modes` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_sim_flight_mission_count_and_item_are_bounded_and_targeted` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_sim_flight_ftp_read_only_target_uses_configured_system` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_router_rejects_frames_after_stop_and_clears_bound_endpoints` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_run_binds_both_legs_and_stop_neutral_is_once` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_run_rejects_second_concurrent_start` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_orchestrator.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_simulator_dry_run_binds_sim_profile_and_seed` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_simulator_starts_and_stops_shared_control_gateway` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_ensure_port_available_detects_bound_port` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_orchestrator_interactive.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_interactive_dry_run_persists_router_and_ai_evidence` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_interactive_orchestrator_can_explicitly_force_read_only` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_interactive_router_mode_override_must_match_custom_config` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_noninteractive_orchestrator_keeps_manual_watchdog_disabled_by_default` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_interactive_qgc_command_uses_native_isolated_hil_ports` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_custom_interactive_router_throttle_mode_must_match_adapter` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_custom_auto_router_mode_accepts_concrete_adapter_and_owns_ports` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_custom_interactive_router_port_conflict_is_rejected` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_legacy_control_gateway_remains_default_without_qgc` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_ai_step_uses_shared_executor_and_stop_neutralizes` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_live_ai_bridge_consumes_px4_telemetry_through_router` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_live_ai_watchdog_sink_drives_bridge_freshness_and_persists_evidence` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_watchdog_evidence_write_failure_does_not_duplicate_neutral_output` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_live_ai_evidence_writes_are_atomic_under_router_concurrency` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_initial_ai_state_syncs_executor` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_orchestrator_lifecycle.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_orchestrator_applies_signed_throttle_mode_with_default_endpoint` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_orchestrator_serializes_process_without_pid_or_poll` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_orchestrator_closes_parent_log_handle_after_process_start` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_orchestrator_stack.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_prepare_persists_hash_bound_stack_plan_and_result` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_prepare_persists_job_local_px4_launch_plan_and_result` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_px4_launch_can_be_explicitly_disabled_for_legacy_stack` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_stack_planner_environment_is_attached_to_default_process_specs` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_custom_process_specs_remain_compatible_when_stack_plan_fails` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_stack_plan_can_be_disabled_without_changing_legacy_specs` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_readiness.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_orchestrator_publishes_process_readiness_and_final_return_codes` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |
| `test_orchestrator_strict_readiness_rejects_process_that_exited_on_start` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_run_cli_stack.py`

分类：环境：socket 沙箱。

处理：XML 与定向复现均为本地 socket 创建/bind 被 EPERM 拒绝（部分线程/子进程只外显 ready=False 或空 stdout）；仅相应用例加 require_loopback_sockets。前置检查仅在 EPERM/EACCES 时 skip，其它 OSError 继续失败，所有协议/授权/端口/生命周期断言保留。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_run_cli_px4_launch_dry_run_persists_wrapper_evidence` | 显式 skip：loopback sockets denied by host sandbox: [Errno 1] Operation not permitted |

### `test_simulator_runtime_preflight.py`

分类：陈旧跨平台 fixture。

处理：Windows Docker Desktop 日志测试只设置 LOCALAPPDATA，却未模拟 os.name；只替换被测模块的 os namespace，pathlib 仍使用当前平台；继续在 Linux 验证全部 Windows 日志输出，不跳过。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_docker_desktop_diagnostics_reports_stale_socket_from_bounded_log` | 通过 |

### `test_simulator_terrain_truth.py`

分类：受约束 release-parity 漂移，未修复。

处理：build_record.failure_reasons 为 release-parity.json is not byte-identical to current shared sources；保留 gate/CLI 原断言，不修改 configs/release-parity.json；按任务要求报告。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_hash_bound_evidence_validates_and_keeps_external_claims_closed` | 未修复失败：保留源/证据绑定检查，需正式生成流程 |
| `test_acceptance_cli_writes_once_and_refuses_implicit_overwrite` | 未修复失败：保留源/证据绑定检查，需正式生成流程 |

### `test_train_onboard_vlm_grounding.py`

分类：环境：可选 torch/transformers 缺失。

处理：只在三个需要实际 CPU tensor/model/gradient 的入口用 importorskip 并给出原因；其余纯 Python 断言照常，不切换环境、不训练/下载。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_actual_tokenizer_processor_response_prefix_and_pixel_cap` | 显式 skip：CPU tokenizer fixture requires optional torch |
| `test_real_peft_tiny_language_only_and_base_digest_proof` | 显式 skip：CPU LoRA fixture requires optional torch |
| `test_response_window_loss_and_gradient_equal_full_masked_causal_loss` | 显式 skip：CPU gradient check requires optional torch |

### `test_verify_onboard_vlm_grounding_adapter.py`

分类：环境依赖/陈旧路径 fixture。

处理：CPU adapter_state 显式 importorskip peft/torch/transformers，实际 saved checkpoint 显式 importorskip safetensors.torch；已有输出拒写测试独立 mock Windows D: storage admission，仍断言 FileExistsError、旧字节不变及未加载模型。

| 原始 testcase（保留参数化 ID） | 当前结果 |
|---|---|
| `test_real_peft_second_bf16_adapter_loses_saved_fp32_precision` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_fresh_fp32_default_reload_exact_saved_bytes_and_base_unchanged` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_saved_tensor_rejected_without_repair[missing]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_saved_tensor_rejected_without_repair[extra]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_saved_tensor_rejected_without_repair[dtype]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_saved_tensor_rejected_without_repair[shape]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_saved_tensor_rejected_without_repair[nonfinite]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_trainable_audit_rejected[duplicate]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_trainable_audit_rejected[visual]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_trainable_audit_rejected[count]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bad_trainable_audit_rejected[empty]` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_saved_digest_mismatch_rejected_before_live_tensor_mutation` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_original_parameter_order_is_part_of_the_trained_digest` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_actual_fp32_byte_change_not_hidden_by_metadata` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_bf16_destination_rejected_instead_of_cast_repair` | 显式 skip：CPU LoRA fixture requires optional peft |
| `test_existing_output_refused_before_any_input_read_or_model_load` | 通过 |
| `test_actual_saved_checkpoint_reconstructs_original_trained_digest_without_gpu` | 显式 skip：saved checkpoint check requires optional safetensors and torch |

## 追加发现与新增覆盖

- R15 Docker-down 测试：当前 HEAD 增加 SitlLock，补入隔离所有权 mock；保留状态机断言，避免访问共享 scratch。
- sim operator-link mission 测试：补齐当前 plan 所需 operator_port；保留 GCS endpoint 启动断言。
- 两个 short-term 真实性能验收与同路径 CLI benchmark：保留 P95/rate/cache/identity 断言，标记 timing。
- teacher malformed ACK 的三个参数化值：RX 线程、持久化日志与 200ms deadline，标记 timing；ACK 拒绝断言不变。
- 新增环境 fixture 自测 4 项：EPERM/EACCES 显式 skip，EADDRINUSE 仍报错，UDP/TCP 检查后均关闭。
- depth companion：原负例扩展为 Windows 反斜杠、POSIX 路径和 Windows drive-relative 名称，并补合法普通文件名。
- v5 任务集合完整契约回归通过，修改题目后意图和 expected steps 保持。

## 完整改动文件清单（含新文件）

- `configs/onboard/plan-eval-tasks-v5.json`：不改（v5 已冻结并有公布成绩）；v3/v4 唯一性测试改为对已知的五条 v5/v4 重叠题目放行，新增重叠仍会失败。
- `docs/linux-test-triage-20261031.md`：追加本组范围、分类、action、验证与未解决事项。
- `docs/linux-test-triage-20261103-training-simulator-misc.md`：新增；记录原始 242 个节点逐项结果及完整交付清单。
- `ground-control/MultiDetectGCS/custom/src/FlyViewCustomLayer.qml`：区域与 home 标记颜色使用原生 QGC palette。
- `scripts/replay_gazebo_depth_companion.py`：POSIX 上也拒绝 Windows 路径逃逸。
- `src/multidetect/sim_teacher/recording.py`：让 Pillow 通过 uint16 dtype 正确推导 PNG16 mode。
- `tests/conftest.py`：添加精确 loopback host-policy 与 repository-local 模型前置 fixture。
- `tests/test_alert_link_bench.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_alerts.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_approach_acceptance.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_bind_openfly_parquet_route_metadata.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_cli.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_count_coverage_completion.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_depth_companion_replay.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_depth_grid_udp.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_diagnose_onboard_teacher_px4_events.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_environment_requirements.py`：新增；验证环境 skip 不掩盖普通 socket 错误。
- `tests/test_eval_server_recovery.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_field_replay_campaign.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_fixed_wing_landing_and_smoke.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_identity_prompt_variants.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_live.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_llama_server_logs.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_mission_operator.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_onboard_3d_training_pipeline.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_onboard_3d_training_preflight.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_onboard_main_operator_link.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_onboard_model_reference.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_onboard_teacher_mission_execution_v1.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_onboard_teacher_px4_parameter_diagnostics.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_operator_link_integration.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_operator_udp.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_payload_confirmation_udp.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_payload_hil_mission.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_payload_hil_udp.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_payload_target_acceptance.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_pixhawk_hil.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_pixhawk_parameters.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_probe_onboard_teacher_gazebo_navref.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_px4_gazebo_qgc_router.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_replay_set.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_run_operator_demo.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_run_r15_round.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_short_term_acceptance.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_sim_operator_link.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_sim_session.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_sim_teacher_live_loop.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_3d_control_acceptance.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_control_gateway.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_interactive_router.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_orchestrator.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_orchestrator_interactive.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_orchestrator_lifecycle.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_orchestrator_stack.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_readiness.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_run_cli_stack.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_simulator_runtime_preflight.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_train_onboard_vlm_grounding.py`：对应上文文件小节分类与 action；保留业务断言。
- `tests/test_verify_onboard_vlm_grounding_adapter.py`：对应上文文件小节分类与 action；保留业务断言。

本轮未提交 commit。`.tmp/` 日志与 XML 为工作临时结果，不纳入交付。共享只读 `artifacts` 软链接不删除或改动。
