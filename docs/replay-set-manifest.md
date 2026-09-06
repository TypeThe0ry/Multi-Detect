# 分层回放集清单

`ReplaySetManifest` 是部署域火烟评估的输入契约。它把每个分层目录中的 raw-sensor
`recording.manifest.json`、人工真值 `ground-truth.jsonl` 和可选的模型输出
`predictions.jsonl` 绑定到同一个数据集版本，并在加载时重新计算文件哈希、读取 raw
recording、检查帧 ID 顺序和类别集合。

当前固定的六类覆盖标签为：

```text
day / night / haze / reflection / smoke_only / no_fire
```

## 目录约定

```text
<replay-root>/
├── day/
│   ├── recording.manifest.json
│   ├── events.jsonl                 # recording manifest 引用的 raw 事件流
│   ├── frame-*.png                  # events.jsonl 引用的图像
│   ├── ground-truth.jsonl
│   └── predictions.jsonl            # 可选；由实际模型回放生成
├── night/ ...
├── haze/ ...
├── reflection/ ...
├── smoke_only/ ...
├── no_fire/ ...
└── replay-set.manifest.json
```

`recording.manifest.json` 必须先通过现有 raw replay 校验；真值和预测 JSONL 的帧 ID
必须与 recording 中的帧按相同顺序一一对应。所有引用路径均相对于根清单目录，拒绝绝对
路径、`..` 穿越、哈希不匹配和字节数不匹配。

## 创建与检查

先在现场采集六份不同的 raw recording，再使用不可覆盖 intake 把来源固定到待标注目录：

```powershell
cd D:\Projects\Multi-Detect
python scripts/prepare_deployment_replay_set.py `
  --output artifacts\replay-sets\fire-smoke-field-v1 `
  --dataset-id fire-smoke-field-2026 --dataset-version v1 `
  --source day=D:\captures\day `
  --source night=D:\captures\night `
  --source haze=D:\captures\haze `
  --source reflection=D:\captures\reflection `
  --source smoke_only=D:\captures\smoke-only `
  --source no_fire=D:\captures\no-fire

python scripts/prepare_deployment_replay_set.py `
  --verify artifacts\replay-sets\fire-smoke-field-v1
```

intake 要求六个显式且不同的源目录/recording hash，不使用 mtime 或 glob 自动挑选。它只复制
`recording.manifest.json`、event stream 和被引用图像；已有的 `ground-truth.jsonl` 与
`predictions.jsonl` 不会被带入。输出 `intake.receipt.json` 对逐文件 SHA-256、字节数、recording hash、
帧数及 inventory 做 canonical 绑定，并固定标注/预测为 pending、全部 review/accuracy/release claims 为
false。输出目录已存在、任一 symlink/路径逃逸/hash 变化/重复录像/receipt 篡改都会失败，临时目录会清理。

随后生成逐帧人工标注工作区：

```powershell
python scripts/replay_annotation_workspace.py create `
  artifacts\replay-sets\fire-smoke-field-v1 `
  artifacts\replay-sets\fire-smoke-field-v1.annotations.json `
  --class-names flame,smoke

python scripts/replay_annotation_workspace.py inspect `
  artifacts\replay-sets\fire-smoke-field-v1 `
  artifacts\replay-sets\fire-smoke-field-v1.annotations.json
```

标注文件里每帧初始为 `{"status":"unannotated","objects":null}`。标注者必须逐帧改成：

```json
{"status":"positive","objects":[{"label":"flame","bbox":[0.1,0.2,0.4,0.6]}]}
```

或者经过明确检查后的负例：

```json
{"status":"negative","objects":[]}
```

完成后填写不同的 `annotator`、`reviewer` 以及 UTC `reviewed_at_utc`，再原子导出：

```powershell
python scripts/replay_annotation_workspace.py finalize `
  artifacts\replay-sets\fire-smoke-field-v1 `
  artifacts\replay-sets\fire-smoke-field-v1.annotations.json `
  artifacts\replay-sets\fire-smoke-field-v1-reviewed

python scripts/replay_annotation_workspace.py verify `
  artifacts\replay-sets\fire-smoke-field-v1-reviewed
```

finalize 会从 intake 重推 assignment，不信任编辑文件自报的 frame/image hash；任何 unannotated 帧、同一人
同时标注与复核、非法类别/bbox 或输入漂移都会失败。输出包含六份 `ground-truth.jsonl`、
`annotation-workspace.reviewed.json`、`annotation-review.receipt.json` 和 `replay-set.manifest.json`，但仍明确
`predictions_complete=false`、`accuracy_validated=false`、`release_ready=false`。

然后用一份 hash-bound 固定模型生成全帧 predictions：

```powershell
python scripts/run_replay_predictions.py run `
  artifacts\replay-sets\fire-smoke-field-v1-reviewed `
  artifacts\replay-sets\fire-smoke-field-v1-predicted `
  --model artifacts\models\fire-smoke.onnx `
  --model-manifest artifacts\models\fire-smoke.manifest.json `
  --model-format auto `
  --confidence-threshold 0.25 `
  --iou-threshold 0.45 `
  --maximum-detections 300 `
  --provider CUDAExecutionProvider `
  --provider CPUExecutionProvider

python scripts/run_replay_predictions.py verify `
  artifacts\replay-sets\fire-smoke-field-v1-predicted

python scripts/evaluate_replay_set.py `
  artifacts\replay-sets\fire-smoke-field-v1-predicted\replay-set.manifest.json `
  --prediction-run-receipt artifacts\replay-sets\fire-smoke-field-v1-predicted\prediction-run.receipt.json `
  --out artifacts\replay-sets\fire-smoke-field-v1-predicted\accuracy-report.json
```

安装后的等价入口是 `multidetect evaluate-replay-set`，参数与上面的 wrapper 相同；两条 CLI 都把
`--prediction-run-receipt` 交给同一个 evaluator/verifier，不存在安装路径降级。

accuracy 独立复核并通过后，运行 provenance-bound threshold calibration：

```powershell
python scripts/calibrate_replay_set.py `
  artifacts\replay-sets\fire-smoke-field-v1-predicted\replay-set.manifest.json `
  --prediction-run-receipt artifacts\replay-sets\fire-smoke-field-v1-predicted\prediction-run.receipt.json `
  --accuracy-report artifacts\replay-sets\fire-smoke-field-v1-predicted\accuracy-report.json `
  --reviewed --reviewer threshold-reviewer `
  --out artifacts\replay-sets\fire-smoke-field-v1-predicted\threshold-calibration.json

python scripts/verify_replay_set_calibration.py `
  artifacts\replay-sets\fire-smoke-field-v1-predicted\threshold-calibration.json `
  --out artifacts\replay-sets\fire-smoke-field-v1-predicted\threshold-calibration-verification.json
```

两个 provenance 参数必须同时提供。calibration 会先复验 prediction bundle，再让 readiness 新鲜重算 accuracy，
最后比较两者的模型与 prediction binding；输出固定 accuracy 文件 SHA、receipt byte/canonical hash、manifest、
model artifact/manifest、runtime inference、production approval 和 synthetic HIL。旧无绑定 calibration 仍可重算，
但 `provenance_complete=false`，不能解释为完成了新的 field provenance 门。

runner 会先复验 v108 annotation bundle 与模型 manifest/artifact，然后使用实时路径同一份
`ReplayFrameSource`、letterbox、ONNX/TensorRT detector、host NMS（仅 raw Ultralytics 模型）和
`JsonlPredictionWriter`。它不覆盖输入或输出目录；固定模型与 manifest 被复制进新 bundle，原 annotation
manifest 另存为 `annotation-source.replay-set.manifest.json`，新 `replay-set.manifest.json` 才绑定六份
`predictions.jsonl`。

`prediction-run.receipt.json` 绑定 annotation review canonical hash、两个 replay manifest、模型 artifact/manifest、
class/input/output、阈值与 NMS、provider selection、runtime 版本、每层 prediction SHA/字节/帧数和延迟统计。
`runtime_inference_executed`、`production_approved` 与 `synthetic_hil_only` 是不同事实；即使 predictions 齐全，
receipt 仍固定 `accuracy_validated=false`、`release_ready=false`。下一步必须运行 evaluator/readiness 并由独立人员
评审部署域指标。

提供 `--prediction-run-receipt` 后，evaluator 会先运行 prediction 独立 verifier，再从 receipt 派生模型 artifact
SHA、ID 与版本，并把 receipt byte/canonical hash、model manifest、runtime inference 和 synthetic HIL 事实写入
accuracy report。readiness 的 fresh recomputation 会再次复验该 receipt 并比较完整 binding；混用另一轮预测、
另一模型或自改 accuracy 元数据都会 fail-closed。

采集或标注尚未完成时也可以生成一个明确的 pending 清单：

```powershell
cd D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
python scripts/create_replay_set_manifest.py artifacts\replay-sets\fire-smoke-2026 `
  --out artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --dataset-id fire-smoke-field-2026 `
  --dataset-version v1 `
  --source-kind lab_recording `
  --allow-incomplete
```

`create_replay_set_manifest.py` 是可直接从 clean checkout 调用的 wrapper，即使没有先设置
`PYTHONPATH` 也会自动定位仓库内的 `src/`；上面的环境变量同时让后续
`python -m multidetect` 命令使用同一份源码。

结构检查和部署域覆盖检查分开执行：

```powershell
python -m multidetect validate-replay-set `
  artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --out artifacts\replay-sets\fire-smoke-2026\validation.json

python -m multidetect validate-replay-set `
  artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --require-complete
```

`validate-replay-set` 的默认返回码只表示结构和引用完整；`--require-complete` 还要求六
类标签全部出现、每个条目都标记为 `deployment_recording`、人工标注已复核。即使这些
条件满足，`accuracy_validated` 仍固定为 `false`，必须再运行逐帧 IoU evaluator 并复核
其结果后才能关闭实现路线中的部署域精度和阈值门。

## 离线正例 evaluator fixture

为了在没有现场图像和独立标注的情况下验证 evaluator 本身，仓库提供一个独立的正例
fixture 生成器。它只复制已校验的 raw replay 记录，并写入确定性的 `flame`/`smoke`
真值与匹配预测；输出仍然是 `source_kind=synthetic_demo`，不会改变原始 SIM 空基线，
也不会被总发布门当成部署域精度证据：

夹具的内容分层与名称严格对应：`day`、`night`、`haze`、`reflection` 每帧含固定
`flame+smoke`，`smoke_only` 每帧只含 `smoke`，`no_fire` 保持空负例。这样阈值扫描可以
单独暴露“仅有烟雾却产生 flame 预测”的类别错误，而不把它混入 IoU 聚合；夹具仍然只是
评估器契约测试数据。

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
python scripts/create_replay_set_positive_fixture.py `
  artifacts/replay-sets/sim-gazebo-2026 `
  artifacts/replay-sets/synthetic-positive-evaluator-pre8-deterministic-v3 `
  --dataset-id multidetect-synthetic-positive-evaluator `
  --dataset-version 0.3.0-pre.8
```

生成目录会包含 `replay-set.manifest.json`、`accuracy-report.json`、
`fixture-summary.json` 和六个原始分层目录。报告应显示 `metrics_evaluated=true`、每个
正例层的真值/预测计数大于零、整体 IoU precision/recall 为 `1.0`，同时保持
`metrics_gate_passed=false`、`accuracy_validated=false`；后两个字段是合约要求，因为
fixture 没有部署域来源和独立复核。当前 pre.8 生成快照保存在
`artifacts/replay-sets/synthetic-positive-evaluator-pre8-deterministic-v3/`，其 manifest
SHA-256 为 `854BE67B34940C9937A61AE8EC9AA19DA56B0BEC11E536EB5DE2EA8AF217A32E`，
accuracy report SHA-256 为 `2023CA574B3384080FE7D61C7572335D896EB5F7F40EAC7AFEF7E3E356789972`；
生成摘要 `artifacts/evaluation/synthetic-positive-replay-fixture-20260903-deterministic-v3.json`
的 SHA-256 为 `B0CC92DC3FE20A69228E9451396AB56459F2B2CD86DFD97BAD2703F6AFFE75E6`。用途仅是
evaluator、阈值扫描和严格哈希链路回归。对应的 readiness report-only 复核为
`artifacts/evaluation/synthetic-positive-replay-readiness-20260903-deterministic-v3.json`
（SHA-256 `0D80BCEC4426A33D09DD87D6E47B3100CC785E33CF93BD95FD0C0DEBEB6CDA9B`），其
`recomputed=true`、manifest/canonical hash binding 均为 `true`，但 `gate_passed=false`
仍由 synthetic_demo 与未复核标注准确阻断。

2026-09-04 的语义修正快照为
`artifacts/replay-sets/synthetic-positive-evaluator-pre8-deterministic-v5/`：manifest
SHA-256 `2F1305E7BA634B49932186EF5E04088285587D6A8702ECA3CDD0EC61BE2F4BF5`，accuracy report
SHA-256 `37E2958D9BC5AC3221F641BC6E40278CFBA48D724E1FA450B61051E5B336C63D`。独立重算的
`smoke_only` truth/prediction 各为 5 个 smoke 对象，aggregate precision/recall 为 `1.0`；
由于仍是 `synthetic_demo` 且未独立复核，`accuracy_validated=false`、`gate_passed=false`
保持不变。

2026-09-03 又为同一正例 fixture 生成了独立的 pre.8 threshold calibration：
`threshold-calibration-20260903-pre.8-positive-fixture.json`（SHA-256
`4CF5D9831729B692867D243203C7CF9F2B6935BA2E8050DB4372725597049E4C`）以及
`threshold-calibration-20260903-pre.8-positive-fixture-verification.json`（SHA-256
`BE978504F28A6D7553438E1FA2E93E81BAE37B02699C9445B0D073427E026557`）。校准报告的
`promotion_ready=false` 是预期值，独立 verification 的 `gate_passed=true` 只表示报告、
manifest/canonical 哈希和重算结果一致。随后将正例 accuracy report 与该 verification
绑定到单独的诊断 E7：
`artifacts/evaluation/release-gate-pre.8-current-20260903T131700Z-v6-bound-report.json`
（SHA-256 `46E1D0BBAAED0F0C321DF025F7CB7156B7A98F846CC5AE17CD84CCDB607844BC`），其
`positive_strata_coverage_passed=true`、aggregate IoU precision/recall 均为 `1.0`，但
`replay_set.gate_passed=false`、总 `gate_passed=false`、`release_allowed=false` 仍由
synthetic_demo、未独立复核、现场链路和其它 REAL/HIL 门准确阻断。严格输出
`release-gate-pre.8-current-20260903T131700Z-v6-bound-strict.json` 保留同样的 fail-closed
状态（SHA-256 `80C4E0370891004613B58CCCE00369F35480E898B9F5EEF75FFCD0F5FE9FF673`）；
该诊断绑定不替换 canonical `sim-gazebo-2026` 空正例基线，也不构成部署域精度或最终发布
证据。

### terminal continuation v7 的重复路径 readiness 快照（2026-09-04）

为核对迁移后生成器在另一输出目录中的可重复性，又对
`artifacts/replay-sets/sim-gazebo-2026-positive-evaluator-20260903-pre8/` 运行了同一份
readiness 检查。快照
`artifacts/evaluation/terminal-continuation-v7-replay-readiness.json` 为 11,263 bytes，
SHA-256 `E3FA53E95FA566BC9816FD4D9CFFC3902949B3D5176BC974E1A5F0ADFBA50EFC`；该目录的
manifest/accuracy report SHA-256 分别为
`854BE67B34940C9937A61AE8EC9AA19DA56B0BEC11E536EB5DE2EA8AF217A32E` 与
`2023CA574B3384080FE7D61C7572335D896EB5F7F40EAC7AFEF7E3E356789972`。它与上面的确定性
fixture 内容相同，只是输出目录不同；六层正例覆盖和 evaluator 重算为真，但
`source_kind=synthetic_demo`、`annotations_reviewed=false`，因此 readiness 仍是
`gate_passed=false`（20 个阻断项）。该快照用于迁移/路径 provenance 回归，不替换 canonical
SIM replay-set，也不关闭部署域 accuracy 门。

### terminal continuation v29 独立正向 evaluator 夹具（2026-09-04，pre.8）

当前 v29 还保留了一份不绑定发布门的正向 plumbing 夹具：
`artifacts/evaluation/terminal-continuation-v29/replay-set-positive-synthetic/`。它从已校验 raw
replay 派生五个正例 strata 和 `no_fire` 负例，manifest 为 4,975 bytes，SHA-256
`61E69B979C33F88B1E044F6192292E2CC55BF5EF6928645A7F28E16F5172BF46`。显式
`--reviewed --reviewer qa-team` 的 evaluator report 为 12,649 bytes，SHA-256
`6EF07FAFE7BA56E696498A856532BF46FE35227F22ADDC5C048FE4DE8C8048F2`；readiness report 为
10,940 bytes，SHA-256 `1EB519C6A5350007973742593A60F669F94AECFD646E2CEED5730BD4E507C13C`。
五层正例覆盖、帧对齐和 aggregate IoU precision/recall `1.0/1.0` 均可重算，但条目仍为
`synthetic_demo`、未独立复核且没有部署域录像，所以 `accuracy_validated=false`、
`gate_passed=false` 保持不变。该夹具只验证 evaluator 接线与哈希绑定，不能替换 canonical
SIM replay-set，也不能关闭 REPLAY、HIL/REAL 或最终 `v0.3.0` 发布门。

### 模型 artifact 绑定

新候选模型的 accuracy 报告应通过 `scripts/evaluate_replay_set.py` 携带
`--model-artifact`，并可补充 `--model-id`、`--model-version`。报告中的
`model_binding.artifact_sha256` 必须等于实际 ONNX/TensorRT 文件；模型审核工具会拒绝缺失、
不匹配或被替换的 hash，即使 replay 指标本身为正。已有未携带 `model_binding` 的历史报告
继续可读，但只用于兼容性和 evaluator plumbing，不能作为新模型晋级依据。

```powershell
$env:PYTHONPATH = 'src'
python scripts/evaluate_replay_set.py `
  artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --model-artifact models\fire-smoke.onnx `
  --model-id fire-candidate --model-version candidate-v1 `
  --reviewed --reviewer qa-team `
  --out artifacts\replay-sets\fire-smoke-2026\accuracy-report.json
```

## 采集阶段 readiness 检查

回放集采集过程中，可用只读 readiness 检查一次性查看部署来源、标注复核、预测流和正例
分母是否齐全。它会重新加载并校验 manifest 引用的每个文件；如果追加了 accuracy report，
还会读取其中编码的门限并重新运行 evaluator，不信任手工改写的 `gate_passed` 字段：

```powershell
$env:PYTHONPATH = 'src;.'
python scripts/check_replay_set_readiness.py `
  artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --accuracy-report artifacts\replay-sets\fire-smoke-2026\accuracy-report.json `
  --out artifacts\replay-sets\fire-smoke-2026\readiness.json
```

采集尚未完成时加 `--report-only`，命令返回 `0` 但报告仍保留 `gate_passed=false`、逐层
`checks`、`blocking_reasons` 和可执行的 `next_actions`。严格模式在任一部署或精度条件
未满足时返回 `2`。readiness 不会创建真值、预测或 `deployment_recording` 声明，也不会
打开相机、Pixhawk、网络或执行器。机器可读格式见
[`configs/schemas/replay-set-readiness.schema.json`](../configs/schemas/replay-set-readiness.schema.json)。

### 证据 JSON 的严格解析

`load_replay_set_manifest()`、readiness 对 accuracy report 的读取，以及 IoU evaluator 对
`ground-truth.jsonl` / `predictions.jsonl` 的读取，都使用拒绝重复对象成员和 `NaN/Infinity`
的解析器；manifest、recording、truth、prediction 和 accuracy report 的路径链上的符号链接也
会被拒绝，UTF-8 BOM 仍可读取。发布清单、原始/规范哈希绑定、帧级 IoU 与 evaluator 重算
因此不会被 JSON 的 last-key-wins、非有限浮点扩展或路径别名绕过。解析失败会保留具体原因并
让 readiness 保持 `gate_passed=false`。

当前 `artifacts/replay-sets/sim-gazebo-2026/` 的 readiness 预检会明确显示六层虽有完整
SIM 帧和预测文件，但来源仍为 `synthetic_demo`、未完成独立标注且正例分母为空，因此继续
保持关闭；替换为真实六层部署记录后再重新生成 manifest 和 accuracy report。

### 绑定到总 goal-acceptance

总验收脚本也支持把 readiness 作为同一份证据中的 `replay_set_readiness` 节点：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\run_goal_acceptance.ps1 `
  -ReplaySetManifest artifacts\replay-sets\sim-gazebo-2026\replay-set.manifest.json `
  -ReplaySetReadinessOutput artifacts\evaluation\replay-set-readiness-goal-acceptance-20260903-v6.json `
  -EvidencePath artifacts\evaluation\multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json `
  -FieldLinkPreflight artifacts\evaluation\field-link-preflight-20260903-followup.json `
  -SimulatorExternalEvidence artifacts\evaluation\simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json `
  -SimulatorExternalRevalidationOutput artifacts\evaluation\simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-goal-v6.json `
  -CombinedSimEvidence artifacts\evaluation\combined-sim-qgc-20260902-hwnd-fallback-v4.json `
  -CombinedSimReleaseModeEvidence artifacts\evaluation\release-mode-sim-20260902-pre.8-combined-qgc-v4.json `
  -RequireSimulatorExternalEvidence `
  -RequireCombinedSimEvidence
```

该绑定会保留 manifest/readiness/可选 accuracy report 的文件哈希、`verification_exit_code`、
`manifest_gate_passed`、`accuracy_gate_passed`、`blocking_reasons` 和 `next_actions`。当前
总快照
[`multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json)
（56,347 bytes，SHA-256 `CD979D0C0E6DE1FC6C777D0592049DDB289DE7E3DEAB1D2D4DCDA4AA36937AAD`）中，
 readiness 严格命令返回 `2`、`gate_passed=false`，因为当前 SIM 条目仍是合成来源、标注未复核、
没有正例和 accuracy report。传入 `-RequireReplaySet` 会将同样的 pending 状态变成总验收
fail-fast，适合最终 `release` 审计；默认模式只记录缺口，不把 SIM 数据写成部署域精度。

## 从 replay-set 做类别阈值校准

当六分层清单已经带有模型预测时，可以直接在同一份哈希绑定数据上扫描每个类别的
confidence threshold，并查看 day/night/haze/reflection/smoke-only/no-fire 各层的稳定性：

```powershell
cd D:\Projects\Multi-Detect
python scripts/calibrate_replay_set.py `
  artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --thresholds 0.10:0.90:0.02 `
  --current-thresholds flame=0.72,smoke=0.60 `
  --minimum-recall 0.70 `
  --beta 0.5 `
  --iou-threshold 0.5 `
  --stability-tolerance 0.10 `
  --out artifacts\replay-sets\fire-smoke-2026\threshold-calibration.json `
  --report-only
```

工具会先重新验证 recording、真值、预测的 SHA-256 和帧顺序，再调用与发布门相同的
IoU 匹配器。输出中的 `calibration.recommendations` 是按 `F_beta` 和 recall floor
选出的候选阈值，`strata` 则保留每个条件层的独立扫描；它们都是可审阅的调参证据，
不会自动改写模型 manifest、Jetson 配置或在线阈值。`promotion_ready` 只有在清单是
完整 `deployment_recording`、所有预测齐全且提供 `--reviewed --reviewer <id>` 时才会
为真。SIM 合成或 camera-only 清单会继续显示 `promotion_ready=false`，即使它们能
成功跑完整个扫描。

报告的 `stability` 汇总把每个类别的六个条件层推荐值放在同一处：包括已有/缺失
推荐的 strata、覆盖率、最小/最大/平均阈值、阈值范围以及
`stable_at_tolerance`。`--stability-tolerance` 默认是 `0.10`，表示同一类别在各层
推荐阈值的最大允许跨度；缺少正例的层（例如 `no_fire`）会显式列在
`missing_recommendation_strata`，不会被伪装成零阈值或被当成稳定通过。

若只想看已有阈值的对照结果，可把 `--current-thresholds none` 传入；采集阶段建议保留
`--report-only`，让 pending/空正例报告返回 0，同时在 JSON 中保留明确失败原因。
报告结构对应
[`configs/schemas/replay-set-calibration-report.schema.json`](../configs/schemas/replay-set-calibration-report.schema.json)。

生成报告后可用独立复核器重新加载 manifest、核对 raw/canonical SHA-256，并重跑报告中
编码的完整校准请求：

```powershell
python scripts/verify_replay_set_calibration.py `
  artifacts\replay-sets\fire-smoke-2026\threshold-calibration.json `
  --out artifacts\replay-sets\fire-smoke-2026\threshold-calibration-verification.json
```

复核器的 `gate_passed=true` 只表示报告内容与当前哈希绑定文件一致；它不会把报告里的
`promotion_ready=false` 升级成通过。复核输出结构见
[`configs/schemas/replay-set-calibration-verification.schema.json`](../configs/schemas/replay-set-calibration-verification.schema.json)。

如果要把这份独立复核接进 E7 `replay_set` 清单，生成发布门时追加：

```powershell
python scripts/create_release_gate_manifest.py `
  ... `
  --replay-set-manifest artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --replay-set-calibration-verification `
    artifacts\replay-sets\fire-smoke-2026\threshold-calibration-verification.json `
  --allow-incomplete
```

writer 会在写入前重新调用校准复核器，并把验证 artifact、校准报告和 replay-set 的
raw/canonical SHA-256，以及 report/recomputed 两项 provenance completeness 一起写入
`replay_set.calibration_verification`。聚合器每次读取都会再次重算哈希、确定性校准请求和 provenance；
旧 artifact 缺字段时按 false 读取。验证 artifact 的 `gate_passed=true` 只表示完整性门通过，
`report_promotion_ready` 仍可为 false，不会替代独立 IoU 精度门；最终 release 还要求两项 provenance
都为 true，并要求 report/recomputed promotion readiness 同时为 true；prerelease 则保持历史证据和
未批准的调参诊断可读。

当前 SIM/Gazebo 空基线的 report-only 结果已保存为
`artifacts/replay-sets/sim-gazebo-2026/threshold-calibration-20260902-pre.8-report-only.json`
（32,703 bytes，SHA-256
`CA32418F6C253D42800C4DCA4218EA9C0425DB4B87F712C054FA456A0275517D`）。它正确返回
`promotion_ready=false`，因为合成帧没有人工正例和独立复核；这份文件只验证校准工具、
哈希绑定和六分层输出接线。

该快照的独立复核结果已保存为
`artifacts/replay-sets/sim-gazebo-2026/threshold-calibration-20260902-pre.8-verification.json`
（856 bytes，SHA-256
`BDAB5475B2683A801B6DB43CA1CB55CDE416493D3128D7020C7AFE8D6446B0F7`）。复核结果为
`gate_passed=true`、manifest/raw/canonical hash 全部匹配、确定性重算匹配，但
`report_promotion_ready=false` 仍保持原始 SIM 空基线状态。

同一份 replay-set 已进一步绑定到 pre.8 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v1.json`
（10,680 bytes，SHA-256
`0661B9DF660613C038AD967D07EF3892325C1A44D4AB7E1E13CE9BB19A671762`），固定时间复核报告为
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v1-report.json`
（21,523 bytes，SHA-256
`045E7160950B123EB559FC8C89C250147D3F0AB173648BCA30B56C5A199A91FB`）。E7 复核现在会
直接显示 `replay_set.present=true`、六层 coverage 以及 raw/canonical hash；SIM 合成部署域
仍会使 `replay_set.gate_passed=false`，不会被误当成最终 REPLAY 精度通过。

本轮又把独立校准复核 artifact 接入 `replay_set.calibration_verification`，生成新的
诊断清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v2-calibration.json`
（11,490 bytes，SHA-256
`04ACDA079F66B3ECCB387124FA66BDE3DE8959A43328BBC8D8618C67DC908A92`）及固定时间复核报告
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v2-calibration-report.json`
（23,554 bytes，SHA-256
`E77388B10F71E13245AEE15CFEC04C3D8AEEAB9A1488C019FAD22DC3F82046DA`）。报告明确显示
`replay_set.calibration_verification.passed=true`、`report_promotion_ready=false`，而
`replay_set.gate_passed=false` 仍由 SIM 合成部署域覆盖/精度门阻断；这证明新增子绑定
生效，但没有抬高任何真实设备或最终发布结论。

## SIM/Gazebo 回放接线夹具

已有的 Gazebo/Harmonic 相机捕获可以先转换为六层 SIM staging，验证同一 raw-sensor
recording、哈希、帧对齐和 evaluator 接线：

```powershell
cd D:\Projects\Multi-Detect
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_sim_replay_set_fixture.py `
  artifacts\evaluation\gazebo-sitl-camera-20260826T042127Z-18016 `
  artifacts\replay-sets\sim-gazebo-2026 `
  --dataset-id multidetect-sim-gazebo-2026 `
  --dataset-version v0.3.0-pre.8 --frame-limit 5
& $py scripts\evaluate_replay_set.py `
  artifacts\replay-sets\sim-gazebo-2026\replay-set.manifest.json `
  --report-only --out artifacts\replay-sets\sim-gazebo-2026\accuracy-report.json
```

该脚本为六个条件目录复制同一组 Gazebo 原始帧，并写入显式空的
`ground-truth.jsonl`/`predictions.jsonl`。空基线只表示“没有独立真值，不猜测图像中的火焰或
烟雾”；它不会把模拟画面变成标注，也不会掩盖检测器精度未知。生成的条目使用
`source_kind=synthetic_demo`、`annotations_reviewed=false`，所以即使六层结构完整，
`deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`
仍是预期结果。当前 staging 产物位于
`artifacts/replay-sets/sim-gazebo-2026/`（每层 5 帧），对应的四模式中间清单为
`artifacts/evaluation/release-gate-pre.8-current-20260901-replay-staging.json`；该清单会
显示 `replay_set.present=true` 但 `passed=false`，把“已接线的 SIM 合成回放”和“尚未收集的
部署域证据”分开记录。现场采集完成后，应替换六层目录中的 recording、人工真值和模型预测，
再以 `source_kind=deployment_recording` 重新生成清单并进行独立复核。

Gazebo 转换器现在也对每帧 `navsat` 做 WGS-84 域校验：纬度必须严格落在 `(-90, 90)`、
经度必须在 `[-180, 180]`、海拔必须在 `[-1000, 100000]` 米；非法样本会计入
`invalid_navsat_count`，并写成 `position_healthy=false` 的未知遥测，不会把越界坐标提升
成可落图 GPS。该计数会沿着 `create_sim_replay_set_fixture.py` 进入回放证据，便于审计
“收到 NavSat”与“通过定位质量门”之间的差异。

2026-09-02 的当前空基线评估写入
`artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260902-precision-gate.json`
（9,178 bytes，SHA-256
`F474454CEE1A11F553C1A1C7EDA31D11E07D518AC1F2E898D2380FE3EC919553`）。其
`precision`/`recall` 聚合值为 undefined，因此即使目录结构完整或 review 字段被手工声明，
评估器也会保持 `metrics_gate_passed=false`；只有含正例真值、阈值合格预测和独立复核的
`deployment_recording` 才能关闭精度门。

### 2026-09-03 正向部署分层覆盖门

精度报告现在除了聚合 precision/recall，还必须给每个正向部署条件提供独立的真值对象和
检测器输出。固定的正向分层是 `day`、`night`、`haze`、`reflection`、`smoke_only`；
`no_fire` 仍是负例控制层，只用于误报回归，不计入正向分母。报告会写入
`positive_strata`、`positive_stratum_coverage` 和 `positive_strata_coverage_passed`，
发布门与 readiness 会同时检查五层的两个计数都大于零。这样一份只在白天有火焰、其余
条件为空的报告即使聚合指标为 1.0，也会保持关闭，避免部署条件被平均值掩盖。

本轮对当前 SIM 空基线重新生成了独立诊断：

- [`accuracy-report-20260903-positive-strata.json`](../artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260903-positive-strata.json)，SHA-256
  `7DE80E0E43D669A3D8DD0D0A4D625CBDCF86B23C25B7580C2ABDEBF0848500CF`；
- [`replay-set-readiness-20260903-positive-strata.json`](../artifacts/evaluation/replay-set-readiness-20260903-positive-strata.json)，SHA-256
  `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`。

两份报告都明确 `gate_passed=false`：当前六层仍是 `synthetic_demo`、没有人工正例和
预测，五层正向覆盖均为零。现场采集时必须在每个正向条件保留人工复核真值和同一 pinned
detector 的预测，再以 `source_kind=deployment_recording` 重建清单；不能用 `no_fire` 或
合成空帧填充缺失分层。

## 录制视频转换为 raw replay

现场接线尚未完成时，普通录像也可以先接入同一套 raw replay / 感知回放管线。使用
`scripts/create_video_replay_recording.py` 将视频逐帧解码为 PNG，并生成带图像哈希、严格
单调时间线和 `recording.manifest.json` 的新目录：

```powershell
cd D:\Projects\Multi-Detect
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_video_replay_recording.py `
  .\field\day.mkv `
  .\artifacts\replay-sets\fire-smoke-2026\day `
  --recording-id field-day-001 `
  --started-at-utc 2026-09-02T03:00:00Z `
  --fps 30 `
  --frame-limit 300
```

输出目录包含 `frames/frame-*.png`、`events.jsonl`、`recording.manifest.json` 和
`recording-summary.json`，可以直接交给 `RawSensorReplayRecording.from_manifest()` 或作为
上面六层回放集某一层的 raw recording。`--frame-stride` 按源帧抽样，`--frame-limit` 限制
输出帧数；容器没有可靠 FPS 时必须显式传入 `--fps`，避免时间轴被猜测。

视频转换器写入的是明确的 `camera_only_unknown_navigation` 记录：每条 telemetry 的
`position_healthy=false`，GPS 字段保持未知，`ground_truth_present=false`、
`predictions_present=false`、`deployment_domain_evidence_complete=false`、
`accuracy_validated=false` 和 `gate_passed=false`。它只固化像素和回放接线，不能单独关闭
室外 GNSS、目标落图、HIL、REAL 或最终发布门。若要做定位或部署域精度，必须把同一时间轴上
独立采集并通过质量门的 MAVLink/raw-sensor recording、人工真值和模型预测加入该层，再按
`source_kind=deployment_recording` 重新生成并独立复核清单；不能把这份 camera-only 输出
直接改标为部署记录。

### 将 GNSS capture 转成 telemetry sidecar

现场用 `scripts/capture_gnss_samples.py` 得到的报告是 raw MAVLink 质量证据，不能直接当作
回放 telemetry。使用 `scripts/gnss_capture_to_replay_telemetry.py` 先校验报告，再只转换
其中的 `GPS_RAW_INT`/`GPS2_RAW` 记录；`GLOBAL_POSITION_INT` 不会被提升为 raw GPS 质量。
未知的姿态、AGL、围栏、任务和执行器谓词保持未知或关闭：

```powershell
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\gnss_capture_to_replay_telemetry.py `
  .\field\gnss-capture.json `
  .\field\day-telemetry.jsonl `
  --time-origin-monotonic-s 18342.500 `
  --time-offset-s 0.250
```

默认以第一条 raw GPS 样本作为回放 `t=0`；当相机与 GNSS 分开启动时，用显式
`--time-origin-monotonic-s`/`--time-offset-s` 对齐。转换器拒绝报告篡改、无 raw GPS、负数或
重复时间戳，并原子写出严格 JSONL。输出每行遵循
[`configs/schemas/raw-replay-telemetry-sidecar.schema.json`](../configs/schemas/raw-replay-telemetry-sidecar.schema.json)，
但摘要仍明确 `deployment_domain_evidence_complete=false`、`accuracy_validated=false`、
`gate_passed=false`；它只是导航样本侧车，不是目标真值、室外精度或实飞验收。

### 将独立 GNSS/raw telemetry 合并进录像

视频转换器只负责固化像素，因此室外定位回放还需要一份独立的 telemetry sidecar。sidecar
必须是严格 JSONL，每行都是完整的 `RawReplayTelemetrySample`：

```json
{"schema_version":1,"kind":"telemetry","observed_at_s":0.0,"telemetry":{"altitude_agl_m":20.0,"roll_deg":0.0,"pitch_deg":0.0,"ground_speed_mps":0.0,"in_allowed_zone":true,"geofence_healthy":true,"position_healthy":true,"link_healthy":true,"flight_mode_allows_deploy":false,"release_zone_clear":null,"latitude_deg":1.3521,"longitude_deg":103.8198,"gps_fix_type":3,"satellites_visible":10,"gps_horizontal_accuracy_m":1.2,"gps_vertical_accuracy_m":2.5,"gps_observed_at_s":0.0,"attitude_observed_at_s":0.0,"position_observed_at_s":0.0}}
```

用 `scripts/join_raw_replay_telemetry.py` 生成新的 joined recording：

```powershell
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\join_raw_replay_telemetry.py `
  .\artifacts\replay-sets\fire-smoke-2026\day\recording.manifest.json `
  .\field\day-telemetry.jsonl `
  .\artifacts\replay-sets\fire-smoke-2026\day-joined `
  --recording-id field-day-001-joined `
  --maximum-telemetry-age-s 1.0
```

合并规则是“每个视频帧取其时间戳之前最新的一条 telemetry”，禁止未来样本、插值和超过
最大年龄的旧样本；输出会重新复制并校验图像、重算事件流/recording hash，并逐帧调用
canonical adaptive-ranging GPS quality policy，摘要中记录匹配年龄、GPS 合格帧数和失败原因。
如果源录像 manifest 已绑定 `scenario_id`、`scenario_manifest_hash` 和 `scenario_seed`，joined
manifest 会原样保留这组三元组，并把它计入新的 recording identity，确保同一 SIM/HIL 场景在
补入独立 GNSS 后仍可被 runtime descriptor 校验；没有源绑定时不会凭空生成 scenario。
即便所有帧都满足 `fix_type>=3`、卫星数、hAcc、vAcc 和 GPS age 门，合并动作仍保持
`deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`。
它证明的是“录像与独立遥测在同一 monotonic 时间轴上可复现”，不是人工真值、室外实飞或
最终 `v0.3.0` 发布证据。

## 部署域精度报告

使用下面的命令对清单中每个条目的预测流运行同一个
`multidetect.evaluation.evaluate_detections`，同时生成一个带回放集 raw SHA-256 和
canonical SHA-256 的独立报告：

```powershell
$env:PYTHONPATH = 'src;.'
python scripts/evaluate_replay_set.py `
  artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --iou-threshold 0.5 `
  --confidence-threshold 0.25 `
  --minimum-precision 0.70 `
  --minimum-recall 0.70 `
  --maximum-false-alarm-frame-rate 0.10 `
  --maximum-missed-detection-frame-rate 0.20 `
  --maximum-latency-p95-ms 100 `
  --reviewed --reviewer qa-team `
  --out artifacts\replay-sets\fire-smoke-2026\accuracy-report.json
```

报告把 `structurally_valid`、`predictions_complete`、`metrics_gate_passed`、
`reviewed` 和 `accuracy_validated` 分开记录。缺预测、指标不达阈值或没有显式
`--reviewed --reviewer` 时，`gate_passed=false`；不会因为目录完整或存在预测文件就
自动变成精度通过。评估器还会拒绝 precision 或 recall 为 `null` 的全空/全负聚合结果，
因为这类数据没有可计算的真值正样本和阈值内预测正样本；`no_fire` 负例仍可用于误报回归，
但不能单独关闭部署域精度门。`--report-only` 可在采集阶段保留退出码 0，同时继续输出
pending 原因。

此外，`positive_strata_coverage_passed` 必须为真，并且
`positive_stratum_coverage` 中五个正向分层各自同时存在真值对象和检测器预测；
readiness 的 `positive_truth_by_stratum` / `positive_prediction_by_stratum` 会给出同样的
逐层结果。E7 writer 和 evaluator 对声称通过的报告也执行这项检查，缺字段、零计数或
分层列表漂移都会 fail-closed。

若要把已复核的报告纳入 E7 清单，生成发布门时同时指定：

```powershell
python scripts/create_release_gate_manifest.py `
  --out artifacts\evaluation\release-gate-pre.8.json `
  --parity-json configs\release-parity.json `
  --replay <replay-evidence.json> --sim <sim-evidence.json> `
  --hil <hil-evidence.json> --real <real-evidence.json> `
  --replay-set-manifest artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --replay-set-accuracy-evidence artifacts\replay-sets\fire-smoke-2026\accuracy-report.json `
  --replay-visual passed --replay-mission passed `
  --sim-visual passed --sim-mission passed `
  --hil-visual passed --hil-mission passed --hil-hardware passed --hil-real-control passed `
  --real-visual passed --real-mission passed --real-hardware passed --real-real-control passed
```

`replay_set.accuracy_evidence` 会再次校验报告文件哈希、事件类型以及它绑定的两个
回放集摘要。预发布可以先只绑定回放集结构；最终 `release` 通道还要求这个精度报告
明确为 `gate_passed=true` 且 `accuracy_validated=true`。如果同时提供
`replay_set.calibration_verification`，它必须是独立复核器生成的完整性 artifact；该
子门通过不等于阈值已经获准发布，仍需满足上述精度和人工复核条件。

清单验证不会打开相机、Pixhawk、Docker、PX4、Gazebo 或执行器，也不会把合成数据、实验室
数据或目录存在性写成真实硬件/室外 GNSS 证据。完整机器可读结构见
[`configs/schemas/replay-set-manifest.schema.json`](../configs/schemas/replay-set-manifest.schema.json)，
精度报告格式见
[`configs/schemas/replay-set-accuracy-report.schema.json`](../configs/schemas/replay-set-accuracy-report.schema.json)。
