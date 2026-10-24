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
# 命令行生成任务包

操作员可以用 `scripts/mission_package_tool.py` 生成、校验、签名和查看任务包。先设置仓库的 Python 路径：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
```

生成一个保守的场地任务包（区域格式为“名称:北,东,半径[:标签]”，预算格式为“任务秒,预留秒,能量 Wh,预留 Wh”）：

```powershell
python scripts/mission_package_tool.py new --id cones-demo --prompt '数交通锥后返航' `
  --zone field:25,0,20:open_field --home 0,0 --budget 600,180,120,30 `
  -o .\_scratch\cones-demo.json
```

`validate` 会调用机载 `load_mission_package`，并对内置 `plan` 执行 `plan_intent` 一致性检查；失败时逐条打印中文问题。未签名包可用会话的 `operator-link.json` 签名，也可以直接传 32 字节 HMAC 十六进制 key：

```powershell
python scripts/mission_package_tool.py validate .\_scratch\cones-demo.json
python scripts/mission_package_tool.py sign .\_scratch\cones-demo.json `
  --operator-link .\_scratch\operator-out\operator-link.json `
  -o .\_scratch\cones-demo.signed.json
```

`sign` 默认另存为 `.signed.json`，不会覆盖原件；只有明确传 `--in-place` 才会覆盖。`show` 输出签名状态、地点/区域计数和预算换算。续航估算采用 `docs/energy-model-calibration.md` 记录的保守 500 W 功率下限：`min(可用时间, 可用能量 × 3600 / 500)`，用于演示预算，不代表实机标定。
