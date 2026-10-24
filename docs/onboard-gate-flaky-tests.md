# Onboard gate flaky 测试记录

## 清单与根因

| 测试 | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| `test_onboard_real_process.py::test_real_mode_process_loopback` | 高负载时 fake camera 触发 `watchdog_reason: stale_camera` | 回环 harness 没有显式传 `--camera-timeout`，继承生产默认 `0.75s`；Windows 调度延迟可超过该值 | 仅在 `run_real_mode_harness.py` 的 onboard 子进程传 `--camera-timeout 5`；生产默认值不变 |
| `test_replay_ground_projection.py::test_replay_prefers_segmentation_and_records_default_equivalence` | xdist 并发时 `cv2.error: Unknown C++ exception` | OpenCV native 线程池与 xdist worker 竞争 CPU/内存 | 测试显式调用 `cv2.setNumThreads(1)` |
| `test_policy_v2_training.py::test_training_script_runs_two_epoch_synthetic_smoke` | xdist worker `gw0/gw1` 崩溃，栈在 Torch autograd | CPU Torch 默认线程数和 batch 峰值在并发 worker 下放大资源占用 | 冒烟测试保存并恢复线程数，期间限制为 1，batch 从 2 降为 1 |
| `test_onboard_real_process.py::test_real_mode_process_loopback`（历史编码问题） | GBK `UnicodeDecodeError`，随后 `stdout=None` | 父 pytest 的 locale 解码子进程 UTF-8 输出 | harness 两个子进程明确使用 `encoding="utf-8", errors="replace"` |

历史日志中还见过 operator-link 的固定 `sleep(0.2)` 断言、路径断言和业务 `UnboundLocalError`；它们在串行运行或多份日志中仍失败，证据不足以归为本次负载 flaky，因此没有用资源参数掩盖。

## 复现方法

在仓库根目录启动两个受控 CPU 压力进程并记录 PID（测试完成后只按这些 PID 结束），然后运行：

```powershell
$pids = @()
1..2 | % { $p = Start-Process python -ArgumentList '-c', 'while True: pass' -PassThru; $pids += $p.Id }
$env:PYTHONUTF8 = '1'
python -m pytest -n 2 -q `
  tests/test_onboard_real_process.py::test_real_mode_process_loopback `
  tests/test_replay_ground_projection.py::test_replay_prefers_segmentation_and_records_default_equivalence `
  tests/test_policy_v2_training.py::test_training_script_runs_two_epoch_synthetic_smoke
Stop-Process -Id $pids -Force
```

完整 gate 使用：

```powershell
python scripts/run_onboard_gate.py --workers 2 2>&1 | Tee-Object D:/Projects/mdwt/_scratch/cx-gate-flakes/onboard-gate.log
```

gate 脚本固定 `CUDA_VISIBLE_DEVICES=-1`、`PYTHONUTF8=1`；生产 onboard 的 camera watchdog 默认仍为 `0.75s`。

## 结果

修复前的受控双 CPU 压力基线为 `1/1` 失败：real-process harness 的父 pytest 进程按 GBK 解码 UTF-8 输出，触发 `UnicodeDecodeError`（历史 gate 另有 stale-camera）。修复后同样压力下重复 5 轮，OpenCV、Torch worker crash、stale-camera 和编码失败均为 `0/5`；其中 1 轮遇到独立的 Windows `WinError 10013` 回环 socket 绑定失败，归因于本机端口访问策略而非上述测试逻辑。完整 gate 为 `1566 passed, 2 skipped`（2 workers，81 秒）。实际命令和日志保存在 `D:/Projects/mdwt/_scratch/cx-gate-flakes/`。
