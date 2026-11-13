# 身份轮次决策报告工具

`scripts/report_identity_round.py` 只读既有评测，输出一份 Markdown 和一份 JSON，支持任意多个候选。不会启动推理、SITL、Docker 或 GPU 工作。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
mkdir -p .tmp
$PY scripts/report_identity_round.py \
  --baseline r21id-r3=artifacts/r21id-r3/pipeline \
  --candidate r21id-r4=artifacts/r21id-r4/pipeline \
  --candidate r21id-r5=artifacts/r21id-r5/pipeline \
  --views r21id-r3=artifacts/evaluation/r21id-r3-view-eval \
  --views r21id-r4=artifacts/evaluation/r21id-r4-view-eval \
  --views r21id-r5=artifacts/evaluation/r21id-r5-view-eval \
  --output-json .tmp/identity-round.json \
  --output-md .tmp/identity-round.md
```

轮次参数格式为 `标签=目录`。`--candidate` 可重复；标签不得重复或与基线相同。默认四视角目录是 `artifacts/evaluation/<标签>-view-eval`。每个目录中 `identity-report.json` 的 **candidate 侧必须对应该标签**，样本预测文件必须名为 `<标签>-predictions.json`；旧报告缺少模型标签，工具不能自动验证模型归属，调用者需确认目录对应关系。工具重新判断门槛，不采用旧报告的 `candidate_pass` 或 `decision`。

默认数据集：v1 使用 `artifacts/datasets/flight-identity-eval-v1/test.jsonl`，v3 使用 `artifacts/datasets/flight-identity-eval-v2-v3prop/test.jsonl`，四视角使用基线 pipeline 下的 `r15-additions/test.jsonl`。可分别通过 `--v1-dataset`、`--v3-dataset`、`--view-dataset` 覆盖。必须使用与预测对应的冻结集合，样本 ID 不一致、重复或 JSON 损坏会报错退出，不会伪装成 pending。

按 `docs/slow-system-r21id-plan.md` 的 r5 验收段，报告分开列出：

- 硬门槛：v1/v3 拒绝率分别不低于基线减 0.02；v3 `<12 px` 召回不低于 r4 实测值减 0.03；四视角一致率 ≥0.833、真目标误拒 ≤0.042、干扰物误收 ≤0.075。
- 绝对门槛：四视角一致率 ≥0.90、真目标误拒 ≤0.08、干扰物误收 ≤0.026。

默认基线应指定 r3，小目标召回参考标签默认为 `r21id-r4`，通过 `--recall-reference` 可改变标签名称。参考轮次必须包含在输入轮次中，否则对应门槛为 pending。两种门槛各自汇总为 pass/fail/pending：已知失败优先显示 fail；没有失败但有缺失则 pending。`evidence_status` 单独反映输入/差分是否齐全。固定集门槛通过不代表完成未见 world 留出测试，也不会自动建议替换现役模型。

缺文件、缺指标、无计分样本均明确写为 pending；新增误收数量用 null 表示未知，不当作零。指标取既有 JSON 的数值精度，不先按展示位数取整。百分点使用绝对差值乘 100。

新增误收复用 `report_identity_errors.py` 的样本对齐、接受判定、truth 类别回查、候选短边尺寸档及四视角筛选。按实际评测的任务文本 × 候选类别 × 尺寸档 × view 汇总，保留完整 sample ID、world 和两侧决策。已存在误收与正例不进入新增清单；没有视角字段的真实飞行行用 unknown。这里的细分尺寸档与 eval JSON 中 `<12` 等指标分档不同，沿用错误报告的 `<8 / 8-12 / 12-16 / 16-24 / 24-32 / >=32`。

输出推荐放在 `.tmp/`，工具拒绝向共享 `artifacts/` 写报告。测试：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp \
  /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_report_identity_round.py tests/test_report_identity_errors.py -q -p no:xdist
```
