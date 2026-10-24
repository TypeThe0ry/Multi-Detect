# 真实模式离线联调

`tests/test_onboard_real_mode_harness.py` 验证 `scripts/onboard_main.py real` 所依赖的真实模式安全边界，不启动 SITL、Docker 或 llama-server，也不访问真实设备。测试使用本地签名任务包、模拟 PX4 遥测/参数接口和合成相机帧；操作员 UDP/TUNNEL 协议闭环由 `tests/test_operator_link_integration.py` 覆盖。

进程级闭环由 `scripts/run_real_mode_harness.py` 和 `tests/test_onboard_real_process.py` 覆盖。它启动独立的 `onboard_main.py real` 子进程，PX4 端使用本地 UDP socket + pymavlink 编解码器发送心跳、位置、电池、HOME、估计器/GNSS 和参数回读，处理模式/解锁/降落命令及 setpoint；地面站子进程复用 `scripts/fake_gcs_console.py` 的签名 TUNNEL 逻辑。显式设置 `MULTIDETECT_ONBOARD_TEST_HOOK` 才会替换相机和策略，默认生产入口不受影响。

测试 hook 有硬性安全限制：环境变量必须精确等于 `scripts.onboard_real_test_hook`，且车辆 endpoint 必须是 `udp`/`tcp` 的 `127.0.0.1`、`localhost` 或 `::1` 回环地址。串口、局域网/公网地址、其他模块名和 replay 模式都会在导入 hook 前拒绝启动。hook 启用时 stderr 会打印中文警告，mission log、final summary 以及 operator link 的 plan/status JSON 带有 `test_hook_active: true` 和中文说明；这使离线替换在审计输出中可见。未设置变量时生产路径保持原行为。

在仓库根目录用 PowerShell 运行：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHONUTF8 = '1'
python -m pytest -q tests/test_onboard_real_mode_harness.py tests/test_operator_link_integration.py tests/test_onboard_main_operator_link.py

# 只跑完整进程闭环（通常 25 秒以内）
python -m pytest -q tests/test_onboard_real_process.py
```

此套件验证签名包准入及篡改拒绝、PX4 preflight 对电池/参数不合格的阻断、带序号与时间戳的签名审批及重放拒绝，以及地面链路任务包/计划审批/遥测摘要的协议闭环。回归 onboard gate：

```powershell
python scripts/run_onboard_gate.py --workers 2
```

测试还保留未签名包拒绝、审批超时不解锁、preflight 失败不解锁和地面心跳中断的组件级回归断言；进程 harness 的主路径使用完整签名审批和正常心跳。它不验证 PX4 固件的真实参数语义、射频质量、动力系统、相机曝光同步或真实失联处置时序，也不替代 SITL、台架/HIL、飞行前检查或受控实飞。
