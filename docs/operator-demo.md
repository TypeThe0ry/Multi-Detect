# 地面站联合验收启动器

在仓库根目录执行：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
pwsh scripts/run_operator_demo.ps1 --gcs-exe build/windows-v0.2.0-release/portable-v0.2.0/bin/MultiDetectGCS.exe
```

启动器先获取 `D:\Projects\mdwt\_scratch\sitl.lock`，再启动带 `--operator-link` 的 Gazebo/PX4 会话。会话写出 `operator-link.json` 后，启动器把密钥和端口映射给真实 `MultiDetectGCS.exe`。浏览器打开 `http://127.0.0.1:6080/vnc.html?autoconnect=1` 查看机载画面。按 Ctrl-C 或退出地面站结束；只会清理本次记录的 PID 和唯一容器名。

## 验收步骤

1. 地面站出现操作员链路后，点击“任务包”，选择启动器生成的 `*.signed.json`（传入 `--task-package` 时会自动生成）。
2. 等待计划卡和摘要信息出现，核对任务 ID、计划哈希和风险提示；点击批准，确认机载开始执行。
3. 重复加载一个任务并点击拒绝，确认任务不解锁并进入安全状态。
4. 执行中分别点击暂停、恢复、中止，确认状态和 ACK 在摘要中可见。
5. 任务结束后查看最终摘要，核对状态、计数、发现项、返航/降落动作和时间线。

## 配置映射与限制

`operator_bind_host/port` 映射为 `MULTIDETECT_OPERATOR_UDP_HOST/PORT`；本地接收端口使用 `operator_port + 1`。`operator_hmac_key_hex` 映射为 `MULTIDETECT_OPERATOR_KEY` 和 `hmac:` 格式的 `MULTIDETECT_GROUND_COMMAND_KEY`；`operator_mavlink_signing_key_hex` 映射为 `MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX`；`operator_package_key_hex` 映射为 `MULTIDETECT_OPERATOR_SIGNING_KEY=hmac:<hex>`。地面站没有通过环境变量自动加载任务包的能力，所以仍需在界面点击“任务包”。

演示不会设置 UI 自动退出或自动审批变量。日志写入 `D:\Projects\mdwt\_scratch\cx-demo-launcher\sim-session.log`。先用 `--dry-run` 查看命令和打码后的环境变量：

```powershell
pwsh scripts/run_operator_demo.ps1 --dry-run
```

常见问题：找不到 GCS 时显式传 `--gcs-exe`；端口占用时停止自己上次遗留的会话或修改会话脚本参数；锁忙表示另一套 SITL 正在运行，勿删除锁目录；任务包签名失败通常是 JSON 已带不同 `key_id` 或会话密钥长度不对。
