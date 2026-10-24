# 真实模式离线联调

`tests/test_onboard_real_mode_harness.py` 验证 `scripts/onboard_main.py real` 所依赖的真实模式安全边界，不启动 SITL、Docker 或 llama-server，也不访问真实设备。测试使用本地签名任务包、模拟 PX4 遥测/参数接口和合成相机帧；操作员 UDP/TUNNEL 协议闭环由 `tests/test_operator_link_integration.py` 覆盖。

在仓库根目录用 PowerShell 运行：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHONUTF8 = '1'
python -m pytest -q tests/test_onboard_real_mode_harness.py tests/test_operator_link_integration.py tests/test_onboard_main_operator_link.py
```

此套件验证签名包准入及篡改拒绝、PX4 preflight 对电池/参数不合格的阻断、带序号与时间戳的签名审批及重放拒绝，以及地面链路任务包/计划审批/遥测摘要的协议闭环。回归 onboard gate：

```powershell
python scripts/run_onboard_gate.py --workers 2
```

该 harness 不验证 PX4 固件的实际 MAVLink 收发、飞控参数语义/写入回读、解锁与 OFFBOARD 模式切换、动力系统和机体响应、相机标定/曝光时间同步、射频链路质量或现场失联行为。它不替代 SITL、台架/HIL、飞行前检查或受控实飞；只有这些后续验证才能覆盖飞控与硬件集成风险。
