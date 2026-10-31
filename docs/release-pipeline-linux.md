# pre.9 发布流水线的 Linux 可运行性

本文按 `scripts/run_release_pre9.py` 的实际阶段和 `docs/release-pre9-runbook.md` 盘点。分类描述阶段的宿主平台能力，不代表本工作树已执行发布，也不改变 fail-closed、owner 或证据绑定要求。旧机器经验另见 `release-pipeline-lessons.md`：断点续跑会跳过已通过阶段；源码变化后应使用新运行目录；联合验收需要无人占用的前台桌面；证据重绑使用 r11 链。

| 阶段 | Linux 分类 | 理由与限制 |
|---|---|---|
| `preflight` | **(b) 小修后可跑** | Python 阶段，但原实现固定检查 C:/D:、MSVC/Visual Studio 和 `TEMP`。现按 POSIX 检查 `/` 与仓库所在卷、Linux 所需命令和 `TMPDIR=$repo/.tmp`；Windows 原检查保持不变。它仍会只读探测 Docker daemon。 |
| `version_bump` | **(a) 可直接跑** | 纯 Python/JSON 文件操作，宿主无关。它会改写 `configs/product-version.json`；该受证据约束文件只能在真实发布流程中按授权机制更新，本次没有运行。 |
| `parity` | **(a) 可直接跑** | Python 生成器与 source-only 校验，不要求 Windows。生成器会改写 `configs/release-parity.json`；本次按要求没有执行或重建 parity。 |
| `static_checks` | **(a) 可直接跑** | Ruff、Python onboard gate 和 pytest 均可在 Linux 运行；测试是否通过仍取决于当前源码与依赖。 |
| `sim_safety_acceptance` | **(a) 可直接跑** | Python 安全验收脚本，Linux/CPU 可运行；依然需要其验收输入满足要求。 |
| `docker_sim_acceptance` | **(a) 可直接跑** | Docker 验收可在 Linux 容器宿主运行；需要 Docker daemon、镜像和足够磁盘。它会启动模拟容器，不能作为本次跨平台检查的一部分实际运行。 |
| `windows_gcs_build` | **(c) 本质依赖 Windows** | 阶段调用 Windows PowerShell 构建脚本，目标为 MSVC、Qt 和 Windows portable 包；Linux 上的 MinGW/Qt 不能产出等价 Windows 构建。MSVC OpenSSL 依赖也属于该阶段。 |
| `combined_px4_gazebo_qgc_acceptance` | **(c) 本质依赖 Windows** | 阶段执行 PowerShell `.ps1`，并验收刚生成的 Windows GCS。人工/自动化 QGC 目视验收依赖 Windows 前台窗口、稳定 HWND/PID；headless、远程锁屏或 Linux QGC 不能替代该证据。 |
| `release_gate_manifest` | **(a) 可直接跑** | Python 生成器与报告检查可在 Linux 运行，且预期结果仍为 `release_allowed=false`。当前工作树的 `artifacts/` 是共享软链接且只读，因此不能在此处执行会写运行证据的阶段。 |
| `current_progress_rebind` | **(a) 可直接跑** | r11 重绑、finalize、verify、promote 都是 Python 脚本，没有 Windows 构建依赖；但它会更新受证据约束的进度索引并写证据副本，只能在真实发布流程、允许写入的主仓库中执行。 |
| `evidence_checks` | **(a) 可直接跑** | Python pytest 阶段，Linux 可运行；其通过条件依赖前序真实重绑及当前版本/parity 证据。 |

## 已做的跨平台调整

- preflight 保留 Windows 的 C:/D:、MSVC、工具和 `TEMP` 检查；POSIX 改查 `/` 与仓库卷，并以显式 `TMPDIR` 指向仓库 `.tmp` 作为临时目录约束。POSIX 不把 MSVC/Windows Qt 工具列为本机缺项。
- PowerShell 可执行文件名通过 `multidetect.host_paths.executable_name` 选择宿主后缀，Windows 仍为 `powershell.exe`。
- 日志和计划中的命令行格式按宿主选择：Windows 保持 `subprocess.list2cmdline`，POSIX 使用 `shlex.join`。实际执行仍以 argv 列表调用 subprocess，不经过 shell。

这些小修只解决调度器的宿主路径、临时目录和显示问题，不会把 Windows 构建或前台 QGC 验收伪装成 Linux 阶段，也不会降低发布门要求。
