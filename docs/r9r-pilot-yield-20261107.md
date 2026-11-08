# r9r pilot 视角产出审计（2026-11-07）

本审计只读 `artifacts/r21id-r9ra`，并使用 `scripts/audit_r21id_r9p_yield.py`。pilot 有 16 个 logical episode，8 个 episode complete（每个方向 4 个 world、每 world 2 个 episode），没有 logical failure 或 failed artifact。

## 结论

`car→mailbox` 与 `suv→postbox` 的 truth 几何帧都足够，失败发生在路线视角和旧 builder 产物之间：旧 r9r route 的四个 waypoint 只在目标正面前后横移 0.2–0.6 m，实际相机到目标正面夹角约为 4–11°，所以 `oblique`、`edge_truncated`、`recovery` 标签并没有代表不同几何视角。旧 builder 没有传 per-view cap，30 行全局预算先被 front 消耗；即使用当前 builder 以 `--confusable-negative-view-cap 30` 重建旧 RGB，旧帧仍都落在 front 附近，不能凭空产生其他视角的 reject。

生成器已改为按目标正面方位布点：`front=0°`、`oblique=30°`、`edge_truncated=65°`、`recovery=-30°`，并继续逐视角检查 truth bbox 短边至少 8 px。这个修复只影响 `r21id-r9r`；其他 profile 的 route 和 builder cap 保持不变。由于已有 RGB 是旧路线采集的，真正增加多视角 rows 仍需要沿新 route 采集；本次没有启动 collection。

## 逐 world / view funnel

每格依次为：`几何合格 RGB / 含该 pair 的唯一 proposal RGB / identity positive / rejection`。proposal matched 这里按 builder 输入中同一 pair 的唯一 `image_path` 统计，不等同于 YOLOE 原始 box 数；它是从现有 proposals 和 builder 行可复核的保守计数。

| world | pair | front | oblique | edge_truncated | recovery |
|---|---|---:|---:|---:|---:|
| w0000 | car→mailbox | 38 / 35 / 26 / 30 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |
| w0001 | car→mailbox | 38 / 33 / 33 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |
| w0002 | car→mailbox | 34 / 1 / 1 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |
| w0003 | car→mailbox | 42 / 33 / 33 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 34 / 0 / 0 / 0 |
| w0024 | suv→postbox | 46 / 5 / 0 / 5 | 34 / 0 / 0 / 0 | 34 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |
| w0025 | suv→postbox | 50 / 8 / 0 / 8 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |
| w0026 | suv→postbox | 42 / 10 / 0 / 17 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |
| w0027 | suv→postbox | 46 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 | 32 / 0 / 0 / 0 |

每格依次为几何合格 RGB / pair proposal matched 唯一 RGB / identity positive 行 / rejection 行。几何数由 audit 的旧采集 route 标签分组；其余三项来自最终 CPU builder 输出，按当前生成 sidecar 的 pose-nearest 视角分组。当前 builder 对新 sidecar 与旧采集帧重建后，目标 pair rows 全部派生为 front，未复现旧 manifest 中 SUV pair 的 1 oblique、1 recovery。

按方向汇总，审计脚本将 builder rows 与 geometry-qualified RGB 取交集后得到：

| pair | 几何合格 | identity positive | rejection | rejection view |
|---|---:|---:|---:|---|
| car→mailbox | 538 | 93 | 30 | 17 / 7 / 6 / 0 |
| suv→postbox | 572 | 0 | 23 | 15 / 1 / 2 / 5 |

旧 builder manifest 的原始 source_counts 是 `car→mailbox positive=127, reject front=30` 和 `suv→postbox reject front=30, oblique=1, recovery=1`。新的 CPU 重建将 SUV 方向结果变为 reject front=30，其余视角为 0；两个方向仍未达到 gate。`suv→postbox` 没有 identity positive，是因为现有 proposals 中没有同时形成可用的 SUV 同类 identity pair；这不是 geometry 缺帧。

## 每一级掉落原因

1. **路线到几何**：truth 投影合格帧数量为 538 和 572，四个视角均有约 32 帧/episode；因此不是目标太小、不可见或路线未到达。
2. **几何到 proposal**：旧采集的非 front 帧实际仍在目标正面方向，且重新读取新 sidecar 后不接近新的侧向 waypoint；这些帧没有形成对应 pair 的 proposal matched RGB。front 也有 proposal miss，原因是 YOLOE 没有给出与 confuser truth IoU≥0.3 的 box，或该帧在 RGB 去重后被丢弃。
3. **proposal 到 identity positive**：`car→mailbox` 保留下来的 93 张是已有 builder 的 `confusable_positive` 与几何合格 RGB 的交集；`suv→postbox` 为 0，原因是没有同类 SUV proposal 与 postbox candidate 同帧配对。
4. **proposal 到 rejection**：旧 manifest 的 builder command 没有 `--confusable-negative-view-cap`，所以 pair 的历史全局 30 行上限先由 front 消耗；当前 builder 对旧 RGB 显式启用 per-view cap 后，view metadata 仍来自旧 front-centered 轨迹，因此不会补出真实侧向 reject。其他掉落包括 proposal miss、near-duplicate 去重、task-equivalent drop 和同一 pair 的行冲突。

## route label 与相机几何核对

对 `w0000` 的实际 `steps.jsonl` 逐帧计算目标正面夹角：记录标签为 `front` 的帧为约 4–11°，`oblique` 为约 4.6–5.5°，`edge_truncated` 为约 4.7–5.5°，`recovery` 为约 4.2–5.2°。因此旧 route label 与相机几何不匹配。生成器现在用明确的 0°、30°、65°、−30° 方位，并在生成时用 `project_target_bbox` 检查每个 view 的短边至少 8 px；测试也锁定这些方位。

同帧出现的 `person→mailbox`、`person→postbox` 等 rows 不计入本次两个 pair 的 gate。它们使用独立 pair counter，可作为泛化训练数据，但不能补足 `car→mailbox` 或 `suv→postbox` 的 96 条 rejection；也不会改变本次方向 gate 的判断。

## 现有 proposals 的 CPU 重建

已在 `.tmp/r9r-builder-viewcap-final` 运行 CPU builder，输入为现有 `artifacts/r21id-r9ra/gpu-annotate/proposals.jsonl`，输出 manifest 的 train 总行数为 1690、`confusable_positive=473`、`confusable_negative=370`；目标 pair 的多视角 counts 没有增加，证明旧 RGB 本身不能验证新路线。

维护者要把同一重建结果写回 artifacts 时，使用下面的命令（本工作树不执行，因 `artifacts/` 只读）：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/build_slow_system_sft.py \
  --campaign artifacts/r21id-r9ra/episodes \
  --index .tmp/r9r-pilot-src/index.json \
  --output artifacts/r21id-r9ra/gpu-annotate/sft-rerun-20261107 \
  --proposals artifacts/r21id-r9ra/gpu-annotate/proposals.jsonl \
  --confusable-negatives --task-text-variants --crop-policy none \
  --min-visible-fraction 0.3 --confirm-min-side-px 8 \
  --confusable-negative-view-cap 30
```

这条命令只重建 builder，不启动 GPU、采集或训练。它写入一个新的 artifacts 子目录，避免覆盖旧产物。要让 counts 真正上升，先用更新后的 r9r sidecar 重新采集对应 world，再对新 RGB 运行同一命令。

