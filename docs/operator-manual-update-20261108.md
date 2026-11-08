# 操作员手册更新记录（2026-11-08）

本次只更新操作员文档，没有启动 SITL、Docker、模型服务、GPU、训练、HIL、release-parity 或真实飞行，也没有写入、移动或删除 `artifacts/`。工作树内只产生了本记录和 `docs/operator-manual.md` 的文档修改。

## 章节变更

- 在 `docs/operator-manual.md` 开头新增“新操作员：离线仿真首飞前流程”，按新操作员顺序覆盖：解释器/依赖/模型前置条件、固定相机与云台机型选择、任务/world 选择、只读 help 与 suite dry-run、交互启动边界、运行状态判读、暂停/恢复/中止/返航、结果目录和 episode 证据、pre-arm/首帧/15 秒 frame wait/PX4 clock stall/Gazebo stall/主机负载/任务语义失败分诊。
- 明确 `x500_mono_cam` 可以没有 gimbal；`x500_gimbal` 才要求逐步 `gimbal_joint_state` 与 `propeller_joint_state`。云台角度和 propeller rotation 被标为 Gazebo 仿真状态，不作为真实硬件或任务完成证明。
- 将 operator abort、return-home 请求、PX4 ACK/mode、fresh `landed_state=1` 和 fresh disarm 分开说明，明确 `action_ack`、`return_home` 标签、collector `status=complete` 与已落地/已 disarm 不等价。
- 故障表逐项给出观察、下一步证据位置和证据不足边界；引用 2026-11-07 的 clock/sensor/host-trace/Accel 调查时保留“相关但未证明因果”的结论。

## 命令核验结果

以下命令均为只读检查或 dry-run，使用仿真脚本要求的 VLM 环境；没有启动仿真：

```text
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_sim_session.py --help
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/fake_gcs_console.py --help
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_e2e_operator_suite.py --help
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_operator_demo.py --help
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_operator_demo.py --airframe x500_mono_cam --world gen_cnt2_w0000 --operator-out "$PWD/.tmp/operator-out" --dry-run
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/mission_package_tool.py --help
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/mission_package_tool.py validate configs/onboard/mission-package-count-cones-example.json
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_e2e_operator_suite.py configs/onboard/suite-e2e-operator.json --dry-run
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_mission_suite.py --suite configs/onboard/suite-gimbal-r21idr3-r24plan.json --out .tmp/read-only-dry-run --workers 1 --dry-run
```

结果：所有 `--help` 正常输出；任务包校验输出“校验通过：任务包格式有效，内置计划意图一致。”；E2E suite dry-run 输出 5 个任务的 session/GCS/evaluate 命令；gimbal suite dry-run 输出 7 个任务配置和命令，包含 `x500_gimbal` 及 `gimbal_joint_state`/`propeller_joint_state` 记录要求；operator demo dry-run 渲染了 SIM/GCS 命令，没有取得 SITL 锁或启动会话。检查了两个 suite 引用的 GGUF 文件和两个 Python 解释器均可读。未执行任何会连接 Docker 的非 dry-run 命令。

相关 CPU-only 测试：

```text
PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp \
  /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_mission_suite.py tests/test_e2e_operator_suite.py -q -p no:xdist
```

结果：`52 passed`。Docker network inspect 的既有 warning 触发 loopback fallback，但没有启动仿真，也不影响测试结果。

## 源代码和配置依据

- `scripts/run_sim_session.py`：`--world`、`--airframe`、`--takeoff-method {runway,hand,catapult,vtol}`、`--operator-link`、`--onboard-out`、`--session-log` 等帮助文本；机型/world 解析和起飞方式约束；collector 输出根目录写法。
- `scripts/run_operator_demo.py`：`--world`、`--airframe`、`--gcs-exe`、`--task-package`、`--operator-out`、`--dry-run`；dry-run 只渲染 SIM/GCS 命令，实际模式才取得 SITL 锁并启动会话。
- `scripts/run_e2e_operator_suite.py`：suite 位置参数、`--output`、`--dry-run`；每个任务目录和 `suite-results.json`/`.md` 的命名。
- `scripts/run_mission_suite.py`：`--suite`、`--out`、`--only`、`--episodes`、`--workers`、`--dry-run`；对 `x500_gimbal` 强制 `recording.per_step == ["gimbal_joint_state", "propeller_joint_state"]`；输出 `<out>/<mission-id>/campaign/<world>/episodes/episode-0000-<短哈希>/`。
- `scripts/collect_sim_teacher_episodes.py`：首帧 readiness 的 45 秒预算、step 前/后 fresh frame 的 15 秒等待、Linux PX4 simulation-clock stall 的默认 45 秒有界等待、`failure_class` 与 `frame_wait_timeline` 诊断字段。
- `configs/airframes/x500_mono_cam.json` 与 `configs/airframes/x500_gimbal.json`：固定向下相机和三轴 gimbal mount 的差异；`configs/onboard/suite-e2e-operator.json`、`configs/onboard/suite-gimbal-r21idr3-r24plan.json`：当前任务/world/期望和 gimbal 记录契约。
- `src/multidetect/autonomy/mission_supervisor.py`、`docs/operator-abort-wall-20261107.md`、`docs/land-completion-analysis-20261105.md`：pause/resume/abort/return 状态与“命令接受不等于动作完成”的边界。
- `docs/gz-idle-stall-20261107.md`、`docs/gz-idle-stall-followup-20261107.md`、`docs/accel-timeout-crash-20261107.md`、`docs/timeout-host-trace-20261107.md`、`docs/nightly-review-29-head-20261107.md`、`docs/gimbal-coverage-followup-20261107.md`：已知失败表现、当前矩阵、云台记录和未证实因果。

## 文档链接检查

对 `docs/operator-manual.md` 和本文件中的 Markdown 相对链接做静态检查：链接目标均存在；没有发现指向 `artifacts/` 的新链接，也没有发现断链。命令入口和参数另以实际 `--help` 输出人工核对。未对 Windows GCS 可执行文件、Docker 镜像可拉取性或现场 GPU 运行作声称。

## 待现场确认

- 需要允许 Docker/SITL 的 workstation 才能确认新操作员从启动到成功/失败 episode 的现场时序、GCS 人工按钮文案和实际 `landed_state`/disarm 证据；本次只完成静态和 dry-run 核验。
- 需要新鲜 episode 才能判断具体 sensor/frame/clock 失败类别；旧 28-head 样本缺少部分 `frame_wait_timeline`/`host_pressure.jsonl`，不能补推唯一根因。
- `MultiDetectGCS` 可执行文件路径、端口和显示器/noVNC 可用性仍由现场构建环境确认；手册保留 `--gcs-exe <路径>` 占位符，没有臆造路径或启动结果。
