# 0.3.0-pre.9 一键发布候选 Runbook

本 runbook 只描述候选流水线。它不会把候选版本说成最终 `0.3.0`：任何 HIL、REAL、硬件或人工 QGC 目视验收缺失时，报告必须保持 `release_allowed=false`。

## 一键命令

在 Windows PowerShell 中先执行：

```powershell
New-Item -ItemType Directory -Force D:\Projects\mdwt\releasepipe\.tmp | Out-Null
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\releasepipe\.tmp"
$env:CUDA_VISIBLE_DEVICES="-1"
$env:PYTHONPATH="src"
python .\scripts\run_release_pre9.py
```

先看完整计划而不写文件：

```powershell
python .\scripts\run_release_pre9.py --dry-run
```

失败后使用同一个运行目录断点续跑，例如 `--from-stage docker_sim_acceptance --run-dir artifacts\release-pre9-runs\20261017T000000Z`。只运行一个阶段使用 `--only-stage`。每个阶段有独立 `.log`、`.status.json`；日志和所有生成文件都在运行目录，文件会记录 SHA-256 或由验收器提供绑定摘要。

## 阶段与资源预算

| 阶段 | 预计耗时 | 预计额外磁盘 | 说明 |
|---|---:|---:|---|
| preflight | 1–3 分钟 | 只读 | 磁盘、Docker daemon、Qt/MSVC/GStreamer、Python 工具链 |
| version_bump | <1 分钟 | <1 MB | 运行时才把 canonical manifest 改为 `0.3.0-pre.9` |
| parity | <1 分钟 | <5 MB | 生成器重建 parity，再做 source-only 校验 |
| static_checks | 5–20 分钟 | <2 GB | ruff、onboard gate、release 相关 pytest |
| sim_safety_acceptance | 2–10 分钟 | <1 GB | software/SIM fail-closed 安全门 |
| docker_sim_acceptance | 10–30 分钟 | 5–15 GB | `--execute` 才启动 pinned PX4/Gazebo 容器 |
| combined_px4_gazebo_qgc_acceptance | 15–45 分钟 | 5–20 GB | 需要 Docker、PX4/Gazebo/QGC 环境 |
| windows_gcs_build | 20–60 分钟 | 10–30 GB | Release、portable、C++ self-tests，含 Ed25519 self-test target |
| release_gate_manifest | 1–3 分钟 | <50 MB | 新鲜诊断 manifest/report，缺失 owner 证据必为 false |
| current_progress_rebind | 1–3 分钟 | <50 MB | 仅在真实运行完成后使用受控 rebind 脚本 |

正常总耗时约 56–176 分钟，取决于 Docker 镜像缓存、PX4/QGC 场景和 Windows 编译速度；首次拉取镜像或依赖还会增加时间和磁盘。

## Owner 与硬件边界

HIL、REAL、Pixhawk/Jetson/camera/actuator、户外 GNSS、人工 QGC Fly View 目视检查都需要 owner/operator 和真实设备。headless SIM、offscreen boot、localhost UDP 不能替代这些验收。流水线创建的 `*-owner-required.json` 明确是待办诊断，不是通过证据；不得把旧 `pre.8` 文件重新标成 `pre.9`，也不得用 `--allow-incomplete` 宣称通过。

## 失败处理

流水线遇到非零退出码立即停止，并把错误写入该阶段 marker；修复环境后用相同 `--run-dir` 和 `--from-stage` 继续。preflight 失败时先处理清单中的缺项。Docker/QGC 或 Windows 构建失败时保留原始日志，确认资源和版本后重跑对应阶段。manifest/report 阶段即使生成 fail-closed 报告，也必须保留 `release_allowed=false`；最后的 current-progress rebind 不能代替真实验收。
