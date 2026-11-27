# X1 模型门禁失败反馈与趋势

这两个脚本把 `scripts/x1_model_gate.sh` 的失败输出变成可用于 Altair 训练线的纠错样本和人工趋势摘要。它们只做 CPU 数据处理，不启动模型、飞控、SITL、Docker 或 GPU。执行侧仍只校验包络合法性并准入/拒绝原提案；本工具不生成或改写动作，只把 oracle 标签整理成 SFT 目标。

## 输入

门禁目录由 `scripts/x1_model_gate.sh` 写出，关键文件是：

- `gate.json` / `gate.md`：指标、判定、证据范围拒绝、失败范围与覆盖计数。
- `model-failures.jsonl`：逐轮模型失败，含发往模型的 `messages`、`oracle_label`、`label_status`、`failure_scope`、`failure_reasons`。
- `evidence-scope-rejections.jsonl`：证据范围拒绝，**不作为纠正样本**。
- `failures.jsonl`：全部失败轮；模型失败以外的范围也会在这里出现。

当前真实样本在 `/data/scratch/cx/altair/x1-gate/<label>/`，例如 `x1fl3r-s31` 与 `x1fl4r-s31`。较早一轮的 `x1fl-s31-closedloop/` 没有 `gate.json`，但转换脚本会回退读取其中的 `all-failures.jsonl`；趋势脚本需要带 `gate.json` 的门禁目录。

## 转换成 SFT

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$PWD/.tmp"
python scripts/x1_gate_failures_to_sft.py \
  /data/scratch/cx/altair/x1-gate/x1fl3r-s31 \
  /data/scratch/cx/altair/x1-gate/x1fl4r-s31 \
  --output .tmp/x1-gate-feedback-sft \
  --cap 100 \
  --category-cap evidence-follow-cadence=60 \
  --val-percent 15 \
  --holdout /data/scratch/cx/altair/x1-flight-loop/x1-flight-loop-sft-data/x1-flight-loop-holdout.jsonl
```

输出：

- `train.jsonl` / `val.jsonl`：与 `scripts/build_x1_flight_loop_sft.py` 相同的外层格式，每行有 `sample_id`、`episode_id`、`split`、`category`、`source`、`messages`。`source` 为 `gate_feedback`。`messages` 是原样保留的系统/用户请求，再加一条 `assistant`，内容为规范化后的 oracle 决策或工具 JSON。
- `manifest.json`：门禁读取行数、去重数、丢弃原因、每类上限、类别计数和文件 SHA-256。

规则：

1. **提示保持原样。** 直接使用失败行里的 `messages`，不重新拼接请求。
2. **目标只来自 oracle。** `label_status` 必须是 `oracle_labeled`。决策目标回填并校验 `request_id`、`event_id`、`mission_id`、`revision`；工具目标回填并校验 `request_id`、`call_index`。与请求不一致的标签直接丢弃。
3. **去重。** 以规范化请求内容的 SHA-256 为键，同一请求只保留第一次出现的行。
4. **排除非模型失败。** `failure_scope` 为 `evidence_scope` 或 `infrastructure` 的行不进入训练；缺标签、非法 schema、动作/工具未在请求中提供、请求内出现 oracle/expected 字段的行也丢弃。
5. **逐类上限。** `--cap` 是默认上限，`--category-cap CATEGORY=COUNT` 可逐类覆盖。选取时在门禁标签和源架次之间轮转，避免大类别或某一个门禁占满。表格会打印去重后、保留、训练、验证、上限和截断数量。
6. **训练/验证按架次切分。** 同一源 `episode_id` 不会跨 split；每个类别优先保证两侧都出现（只有一个架次时进训练）。`--val-percent` 控制验证比例。
7. **留出防泄漏。** 传入 `--holdout` 后，任何 `episode_id`、`sample_id` 或 `request_id` 与固定 56 架次评测集重合的门禁样本都会被丢弃；脚本只写 `train`/`val`，从不写 `holdout`。请求内部出现 `oracle_label`、`expected_*`、`ground_truth` 等字段时也会拒绝。

## 趋势表

```bash
python scripts/x1_gate_trend.py \
  /data/scratch/cx/altair/x1-gate/x1fl3r-s31 \
  /data/scratch/cx/altair/x1-gate/x1fl4r-s31 \
  --output .tmp/x1-gate-trend.md
```

Markdown 包含：

- 每个指标的逐标签数值、判定，以及相对首个标签和上一个标签的差值。
- 按 `failure_reasons` 统计的模型失败原因计数。
- 证据范围拒绝、模型失败、基础设施失败和拒绝原因计数。
- 覆盖计数（发送轮次、超时未知、中断请求、retask 与电池结构样本数等）。

这是模型门禁后的人工摘要，不替代 `gate.md`。

## 测试

```bash
TMPDIR="$PWD/.tmp" python -m pytest tests/test_x1_gate_feedback.py -q -p no:xdist
```

测试构造小型合成门禁目录，覆盖转换、去重、逐类上限、留出泄漏防护、按架次切分和趋势差值。测试不启动外部进程。

## 已知限制

- oracle 标签本身来自门禁回放，不是新增飞行证据；转换只修正格式和身份回显，不证明真实鲁棒性。
- `failure_reasons` 是逐轮诊断码，一个失败轮可有多个原因，因此原因计数总和可以大于模型失败行数。
- 较早日志若缺少 `gate.json`，趋势脚本无法读取该目录；转换脚本仍可处理其中的失败 JSONL。