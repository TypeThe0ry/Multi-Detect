# 切下一个预发布版本（0.3.0-pre.9）的正确流程

只读调查结果，经主线复核（2026-10-16）。核心原则：发布证据只能由生成器和真实验收运行产生，不能手改哈希、不能把旧运行改标成新版本、不能用 --allow-incomplete 宣称通过。HIL/REAL 与人工目视验收需要所有者和真实设备。

## 1. Release parity 到底检查什么

`src/multidetect/release_parity.py:3-21` 明确说明：parity 是四种运行模式共用契约的确定性 hash，不扫描整个仓库，也不把 `artifacts/`、`build/`、fixture 或生成文件自动纳入。

五个契约组和对应 hash 字段在 `src/multidetect/release_parity.py:39-60`：

- `ui_contract` → `ui_contract_hash`
- `core_algorithm` → `core_algorithm_hash`
- `task_contract` → `task_contract_hash`
- `safety_policy` → `safety_policy_hash`
- `px4_mission_path` → `px4_mission_path_hash`

当前 `configs/release-parity.json` 有：

- `schema_version`
- `product`
- `release_label`
- 五个 `parity` SHA-256
- 五组显式 `source_files`

当前五组文件数是：

- `core_algorithm`: 63
- `px4_mission_path`: 60
- `safety_policy`: 50
- `task_contract`: 30
- `ui_contract`: 36

具体配置结构见 `configs/release-parity.json:2-12`，源文件清单从 `configs/release-parity.json:12-261` 开始。

这不是 glob。`DEFAULT_FILE_LIST` 是代码里的显式列表，定义从 `src/multidetect/release_parity.py:61` 开始。当前已经显式包含：

- `src/multidetect/operator_link.py`
- `src/multidetect/operator_bridge.py`
- `src/multidetect/operator_protocol.py`
- `src/multidetect/operator_status.py`
- `src/multidetect/operator_tracking.py`

这些属于 `task_contract`，见 `src/multidetect/release_parity.py:240-286`，并对应 `configs/release-parity.json:192-222`。

同时，`operator_mavlink.py`、`operator_udp.py` 等也分别属于 PX4 或 safety 组；GCS `ground-control/MultiDetectGCS/custom/src/*` 中已列出的 C++、头文件和 QML 文件属于 `ui_contract`，见 `src/multidetect/release_parity.py:61-103` 和 `configs/release-parity.json:224-260`。

新 onboard 文件必须特别检查是否已经在显式列表里。当前 core 列表已经包含例如：

- `src/multidetect/autonomy/onboard_agent.py`
- `onboard_adapter.py`
- `onboard_navigation.py`
- `onboard_task_state.py`
- `onboard_onnx.py`
- `onboard_history.py`
- `onboard_history_onnx.py`
- `onboard_model_reference.py`

见 `src/multidetect/release_parity.py:130-153`。如果“今天新增”的文件不在该列表里，默认生成器不会发现它；必须先把它加入 `DEFAULT_FILE_LIST`，或通过审计过的 `--file-list-json` 提供完整列表。不能靠文件名猜测，也不能把整个目录改成 glob。

hash 算法在 `src/multidetect/release_parity.py:9-21`、`659-701`：

1. 使用 `MD-PARITY-V1\0` framing。
2. 每个组名、文件数量、相对路径和文件内容都带长度前缀。
3. 文件按 POSIX 相对路径排序。
4. 只把 CRLF 和单独 CR 转成 LF；不会去空格、补换行或做 Unicode 归一化。
5. 文件必须存在，路径必须在仓库根目录内。
6. `.git`、`build`、`artifacts`、`fixtures`、`__pycache__` 等路径会被拒绝，见 `src/multidetect/release_parity.py:482-551`。

`build_release_parity_document()` 从版本 manifest 读取产品和版本，生成只包含 schema、产品、release label、文件清单和五个 hash 的文档，见 `src/multidetect/release_parity.py:719-765`。序列化使用稳定排序和 LF，见 `768-781`；写入是临时文件加原子替换，见 `784-812`。

## 2. `docs/current-progress.json` 的作用

`docs/current-progress.json` 是当前状态和证据索引，不是 parity 算法的第二个来源。

`docs/development-update-policy.md:4-9` 规定状态来源优先级：

1. 当前源码、配置、测试和可复核证据；
2. `docs/current-progress.json`；
3. `docs/current-progress.md`；
4. `CHANGELOG.md`；
5. 历史文档和旧 artifact。

当前进度索引保存：

- 当前产品版本四元组；
- `configs/release-parity.json` 的路径、字节数、文件 SHA-256；
- 五个 contract hash；
- 当前 evidence、report、manifest 的路径和 hash；
- gate 结果、阻塞原因和 claims boundary。

`docs/current-progress.md:30-35` 明确写着当前 release label 是 `0.3.0-pre.8`，parity 文件和五组 hash 绑定在 `docs/current-progress.json`，当前 gate 仍为 `release_allowed=false`。

`verify_current_progress_index.py` 会重新计算源 parity，而不是只相信 JSON 中嵌入的 hash：

- `scripts/verify_current_progress_index.py:155-193`：读取 parity 的 `source_files`，重新调用 `build_release_parity_document()`，比较整个文档。
- `scripts/verify_current_progress_index.py:2120-2148`：检查 `current-progress.json` 的产品版本、parity 文件字节数、文件 SHA-256 和五组 hash。
- CLI 在 `scripts/verify_current_progress_index.py:2885-2907`：
  - `--check-sources-only`：只验证 parity 对当前源码是否正确；
  - `--check-sources`：在完整 current-progress 校验前重算 parity。

因此，旧的 current-progress 记录不能通过“把 JSON 里的 hash 改成新 hash”来伪造。旧运行应该保留并标记 stale；需要新证据时，必须从原始 source 重新运行或重新构建明确范围的 join。

## 3. 生成和刷新这些记录的脚本

### Parity

生成器是：

- `scripts/create_release_parity.py`
- 实现：`src/multidetect/release_parity.py:815-888`

命令行参数包括：

- `--root`
- `--out` / `--output`
- `--version-manifest`
- `--file-list-json`
- `--no-overwrite`

默认输出是 `configs/release-parity.json`，默认版本是 `configs/product-version.json`。

### Release-gate manifest 和 report

`create_release_gate_manifest.py` 只是包装器；真正的 writer 是 `src/multidetect/release_gate_manifest.py`。其文档 `src/multidetect/release_gate_manifest.py:1-100` 说明：

- 需要四种 mode evidence：REPLAY、SIM、HIL、REAL；
- 每个 evidence 必须标识 runtime mode；
- 每个 evidence 必须有明确的布尔 `gate_passed`；
- 缺失、`not_claimed`、pending 或 hash 不一致都会 fail closed；
- 不会扫描 `artifacts/` 自动挑一个文件；
- 不会把 fixture、dry-run 或 goal snapshot 升格成 release evidence。

包装命令：

```powershell
python .\scripts\create_release_gate_manifest.py `
  --out <new-manifest.json> `
  --parity-json .\configs\release-parity.json `
  --replay-evidence <fresh-replay-evidence.json> `
  --sim-evidence <fresh-sim-evidence.json> `
  --hil-evidence <fresh-hil-evidence.json> `
  --real-evidence <fresh-real-evidence.json> `
  --allow-incomplete `
  --no-overwrite
```

`--allow-incomplete` 只允许生成一个诊断 manifest；不会把 gate 变成通过。

报告评估器是：

```powershell
python .\scripts\check_release_gate.py `
  <new-manifest.json> `
  --version-manifest .\configs\product-version.json `
  --out <new-report.json> `
  --report-only
```

`src/multidetect/release_gate.py:6199-6231` 说明：

- `--report-only` 会写出 fail-closed 报告；
- 严格模式只有 `release_allowed=true` 才返回成功；
- prerelease 即使有软件 gate 通过，也不能因此宣称最终 `0.3.0` 已发布。

### 当前 release-gate wrapper

`scripts/run_current_release_gate_pre8.ps1:244-322` 收集 parity、REPLAY、SIM、HIL、REAL、replay-set、field link、combined SIM/QGC、PX4 等输入，然后调用 manifest writer。

`scripts/run_current_release_gate_pre8.ps1:324-358` 再调用 `check_release_gate.py --report-only`。

它的文件名虽然叫 `pre8`，但会从当前 `product-version.json` 动态生成版本后缀，见 `scripts/run_current_release_gate_pre8.ps1:226-237`。下一版可以继续使用它，但必须显式传入新版本的新 evidence 路径，不能依赖仍绑定 `pre.8` 的默认 artifact。

它会拒绝覆盖已有输出，见 `scripts/run_current_release_gate_pre8.ps1:192-202`。

### Current-progress 刷新

没有一个“自动从所有 artifact 推导 current-progress”的安全生成器。当前索引是受控、审计过的状态记录。相关维护脚本包括：

- `scripts/rebind_current_release_parity_index.py`
- `scripts/rebind_current_parity_artifacts.py`
- `scripts/rebind_current_release_gate_audit.py`
- `scripts/create_current_release_gate_audit.py`
- `scripts/verify_current_progress_index.py`

`rebind_current_release_parity_index.py:1-7` 明确说明它只能创建新的 source-bound 副本和索引，不会重跑 runtime，也不会把旧运行伪装成新运行；其写入逻辑拒绝覆盖，见该文件约 `33-40`。

`create_current_release_gate_audit.py:169-172` 也明确写着：重新按当前 parity 审计历史 report，不代表重新执行 runtime。

## 4. 哪些是纯 hash，哪些必须真实运行

纯 hash / 静态验证：

```powershell
python .\scripts\create_release_parity.py --root .
python .\scripts\verify_current_progress_index.py --root . --check-sources-only
python .\scripts\verify_current_progress_index.py --root . --check-sources
```

这些只能证明：

- 当前显式源文件存在；
- parity 文档没有过期；
- current-progress 中记录的字节数、文件 SHA 和五组 hash 一致；
- JSON/manifest 的绑定关系没有漂移。

它们不能证明新代码在 SIM、PX4、QGC、HIL 或真实硬件上工作。

### 软件/SIM acceptance

S4 软件安全 evidence 的入口是：

```powershell
python .\scripts\run_simulator_safety_acceptance.py `
  --out .\artifacts\evaluation\simulator-safety-acceptance-pre9.json
```

该脚本的帮助文本明确它是 software/SIM gate，只证明共享软件路径和 fail-closed 行为，不证明 Pixhawk、Jetson、camera、actuator 或人工视觉验收。

Docker PX4/Gazebo 外部 SIM 的入口是：

```powershell
python .\scripts\run_simulator_docker_acceptance.py `
  --scenario-manifest <scenario.json> `
  --evidence <fresh-sim-evidence.json> `
  --execute `
  --timeout <seconds>
```

`--execute` 才会真正启动 pinned PX4/Gazebo disposable container；不带它只能做 preflight。该脚本的参数和 claim boundary 可由：

```powershell
python .\scripts\run_simulator_docker_acceptance.py --help
```

查看。

真实执行需要 Docker Desktop、可用的 pinned image、scenario/assets、端口和运行环境。headless SIM 不能声称 QGC 人工视觉、HIL、REAL 或硬件。

### PX4/Gazebo/QGC combined acceptance

入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_px4_gazebo_qgc_combined_sim_acceptance.ps1 `
  -EvidencePath <fresh-combined-sim-qgc-evidence.json>
```

脚本默认：

- 900 sensor frames；
- 420 秒 router duration；
- 240 秒 Mission timeout；

见 `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1:1-22`。

它会读取当前：

- `configs/product-version.json`
- `configs/release-parity.json`

见该脚本 `41-54`。它必须在 Windows + Docker/PX4/Gazebo/QGC 环境真实运行。旧的 `combined-sim-qgc` 文件如果绑定的是 `pre.8` parity，不能通过修改 JSON 重新声明为 `pre.9`。

QGC operator acceptance 另有：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_px4_sitl_qgc_operator_acceptance.ps1 `
  -QgcBuildRoot <pre9-qgc-build-root> `
  -EvidencePath <fresh-qgc-operator-evidence.json>
```

该入口要求 prerelease 产品版本、Docker Desktop 和已经构建的 QGC EXE，见 `scripts/run_px4_sitl_qgc_operator_acceptance.ps1:12-70`。`-CaptureNativeVisualEvidence` 还需要真实的人工/owner 操作授权；offscreen boot 不能替代人工 Fly View acceptance。

### Windows GCS build

Windows 构建命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release `
  -BuildSelfTests `
  -InstallPortable `
  -Parallel 4
```

若要让构建强制检查 E7 manifest：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release `
  -BuildSelfTests `
  -InstallPortable `
  -Parallel 4 `
  -ReleaseGateManifest <fresh-manifest.json> `
  -RequireReleaseGate
```

`build_multidetect_windows.ps1:191-223`：

- 只接受 canonical `configs/product-version.json`；
- `-Version` 若传入，必须和 manifest 的 `release_label` 一致；
- release channel 或 `-RequireReleaseGate` 时必须有 E7 manifest。

`build_multidetect_windows.ps1:266-330` 要求：

- CMake；
- Git；
- patch；
- Qt；
- Visual Studio 2022 MSVC；
- GStreamer SDK；
- GCS `.venv\Scripts\python.exe`。

`build_multidetect_windows.ps1:351-433` 配置、编译、检查 EXE，并输出 Release EXE 的 SHA-256；`-InstallPortable` 还要验证 portable EXE 和 Replay RTSP helper。

Windows/QGC 变化的必跑命令也写在 `docs/development-update-policy.md:51-59`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release `
  -SkipConfigure `
  -BuildSelfTests `
  -InstallPortable `
  -Parallel 4
```

`-SkipConfigure` 只能在已有且匹配新版本/新 parity 的 CMake cache 时使用；新版本或源文件变化后，若 cache 没有重新配置，应去掉 `-SkipConfigure`。

## 5. 测试覆盖

主要防线：

- `tests/test_release_parity.py:69-190`：显式五组源文件、canonical 文档重算、当前源码一致性。
- `tests/test_release_parity.py:218-340`：排序稳定性、hash 变化、换行规范化、路径逃逸、生成目录拒绝、原子写入。
- `tests/test_release_parity.py:343-397`：CLI 生成和直接脚本运行。
- `tests/test_current_progress_index.py:16-45`：current-progress parity 重算和绑定。
- `tests/test_current_progress_index.py:167-183`、`629-706`：旧 parity、路径、文件 SHA 和 gate artifact 漂移必须失败。
- `tests/test_repository_progress_docs.py:34-59`、`80-106`：版本、current-progress、gate audit 和 fail-closed 约束。
- `tests/test_release_gate.py`：四 mode 缺失、跨 mode parity 不一致、JSON 篡改、SIM/安全 evidence 重验证。
- `tests/test_release_gate_manifest.py`：manifest source hash、相对路径、缺失/失败 evidence 和 `--allow-incomplete` 仍保持 fail-closed。
- `tests/test_current_release_gate_wrapper.py`：PowerShell wrapper 的输入、输出不覆盖和 fail-closed 默认值。
- `tests/test_product_version.py:16-32`：版本 manifest 是唯一版本源。
- `tests/test_product_version.py:35-52`：`release_label` 和 Python prerelease 序号必须一致。
- `tests/test_product_version.py:74-89`：`pyproject.toml` 使用动态版本。
- `tests/test_product_version.py:92-123`：QGC 使用 numeric version 和独立 human release label。
- `tests/test_product_version.py:125-145`：Windows build 使用 canonical manifest 和 release label。

## 6. 切 `0.3.0-pre.9` 的最小正确顺序

### A. Owner 先修改 canonical version

只改 `configs/product-version.json` 的 canonical 版本字段：

```json
{
  "schema_version": 1,
  "product": "Multi-Detect",
  "release_label": "0.3.0-pre.9",
  "numeric": "0.3.0",
  "python": "0.3.0rc9",
  "channel": "prerelease"
}
```

版本规则由 `src/multidetect/product_version.py:19-21`、`66-94` 强制：

- release label：`0.3.0-pre.9`
- Python 版本：`0.3.0rc9`
- numeric 仍为：`0.3.0`
- channel 仍为：`prerelease`

不要在 `pyproject.toml` 另写版本；它使用动态版本，见 `pyproject.toml` 的 dynamic version 配置和 `tests/test_product_version.py:74-89`。

`tests/test_product_version.py:20-31` 和 `tests/test_release_parity.py:72-76` 当前硬编码 `pre.8` 的期望值。切 `pre.9` 时，测试期望和对应当前文档记录必须随同版本变更更新，否则测试失败是正确行为。

### B. 更新发布记录

根据 `docs/development-update-policy.md:10-16`，同一变更集必须更新：

- `CHANGELOG.md` 的当前 release / Unreleased 记录；
- `docs/current-progress.md`；
- `docs/current-progress.json`；
- 如 release gate、构建流程或目录发生变化，再更新对应专题文档。

历史 evidence 不要覆盖。旧的 `pre.8` 记录保留，标记 stale 或 retained historical audit。

### C. 重新生成 parity

```powershell
python .\scripts\create_release_parity.py `
  --root . `
  --out .\configs\release-parity.json `
  --version-manifest .\configs\product-version.json
```

如果新 onboard 或 operator/GCS 文件不在当前显式列表，先更新 `DEFAULT_FILE_LIST` 或提供经过审计的 file-list JSON，再执行：

```powershell
python .\scripts\create_release_parity.py `
  --root . `
  --out .\configs\release-parity.json `
  --version-manifest .\configs\product-version.json `
  --file-list-json <audited-file-list.json>
```

不能手改五个 hash。`docs/development-update-policy.md:14-16` 明确要求 parity-bound 文件变化后重新运行生成器；`tests/test_release_parity.py:172-190` 会拒绝 canonical parity 与当前源不一致。

### D. 先做静态验证和回归

```powershell
python .\scripts\verify_current_progress_index.py `
  --root . `
  --check-sources-only
```

```powershell
python .\scripts\verify_current_progress_index.py `
  --root . `
  --check-sources
```

```powershell
python -m ruff check .
```

```powershell
python -m pytest -q
```

至少再跑一次聚焦集合：

```powershell
python -m pytest -q `
  tests\test_release_parity.py `
  tests\test_current_progress_index.py `
  tests\test_repository_progress_docs.py `
  tests\test_product_version.py `
  tests\test_release_gate.py `
  tests\test_release_gate_manifest.py `
  tests\test_current_release_gate_wrapper.py
```

### E. 重新运行真实 acceptance

按新 parity 和新版本生成新的文件：

1. 软件/SIM acceptance；
2. Docker PX4/Gazebo acceptance；
3. Windows PX4/Gazebo/QGC combined acceptance；
4. GCS Windows Release build；
5. 如果要声称 operator visual、HIL、REAL、hardware 或 outdoor GNSS，还必须由有相应设备和权限的 owner/operator 真实执行并审核。

每次 evidence 都要记录：

- release label；
- runtime mode；
- airframe/scenario/model/profile；
- 输入路径；
- 命令；
- exit code；
- 输出路径；
- 文件大小和 SHA-256；
- gate 结果；
- 未声明的 claims boundary。

这些要求见 `docs/development-update-policy.md:32-37`。

### F. 生成新的 E7 manifest 和 report

只有拿到新 parity 绑定的四 mode evidence 后，才运行：

```powershell
python .\scripts\create_release_gate_manifest.py `
  --out <pre9-manifest.json> `
  --parity-json .\configs\release-parity.json `
  --replay-evidence <pre9-replay.json> `
  --sim-evidence <pre9-sim.json> `
  --hil-evidence <pre9-hil.json> `
  --real-evidence <pre9-real.json> `
  --allow-incomplete `
  --no-overwrite
```

然后：

```powershell
python .\scripts\check_release_gate.py `
  <pre9-manifest.json> `
  --version-manifest .\configs\product-version.json `
  --out <pre9-report.json> `
  --report-only
```

对于 prerelease，`--report-only` 是诊断模式；报告中的 `gate_passed=false` 或 `release_allowed=false` 必须原样记录。不能通过 `--allow-incomplete`、旧 artifact rebind 或手改 JSON 来宣称 acceptance 通过。

最后才运行 Windows GCS build，并把新 EXE、portable tree、self-test、offscreen boot、人工视觉结果和 SHA-256 写入 current-progress。

当前环境是只读调查，且没有执行真实 Docker/PX4/GCS acceptance；因此不能诚实地产生 `pre.9` 的 SIM、PX4/QGC combined、Windows EXE 或 HIL/REAL evidence。只能完成 parity 的静态重算和验证；其余步骤必须由具备相应 Windows、Docker、PX4/Gazebo、QGC 和硬件条件的 owner 执行。