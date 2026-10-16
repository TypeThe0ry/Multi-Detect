# 飞行身份评测集 v2：YOLOE v3 口径

这份评测集用于给下一轮慢模型 r15 建立固定的 YOLOE v3 候选框基线。它把同一批飞行录像帧上的候选框与 PANOPTIC_RGB8 分割真值关联，再用与 `flight-identity-eval-v1` 相同的身份行结构、固定规划/身份 prompt 和 `candidate_zoom_v2` 裁剪交给慢模型。生产基线使用 r12 的 `fast_identity` 单 token 判定。

构建只接收场景索引标记为 `test` 的 world。候选缓存按图像键读取：v3 为 `yoloe-v3-round-eval/r3/new-cache.jsonl`，对照为旧的 `v2r3seg-dagger-r8-cfs.jsonl`。任务文本取同一场景 index 的 `task_text_en`，这样每个缓存帧都能构造“假设这次问了模型”的请求；它与 v1 的口径不同，v1 只收录飞行过程中实际发生的慢模型调用。

关联规则写入每个数据集的 `manifest.json`：分割框采用 `boxes_from_segmentation(min_area_px=1, min_box_px=1)`；候选与可见真值框最大 IoU ≥ 0.30 才关联。未达到主阈值但 IoU 或“交集面积/真值框面积”≥ 0.10 的候选计为 ambiguous 并排除主集；两者均低于 0.10 的候选计为背景误报，写入 `unmatched.jsonl`，默认不进入 `test.jsonl`。为避免相邻帧和多候选让单个物体占满数据，每个 `(episode, truth_target_id, candidate size bucket)` 最多保留 3 个最高分候选。尺寸桶为 `<12`、`12-16`、`16-20`、`20-32`、`>=32` 像素短边。

当前构建统计（缓存均为 6,462 个唯一图像键，测试 split 命中 356 帧、12 个 episode）：

| 集合 | 主集行数 | 真目标 | 干扰物 | 背景误报 | ambiguous | `<12` / `12-16` / `16-20` / `20-32` / `>=32` |
|---|---:|---:|---:|---:|---:|---|
| v3prop | 233 | 117 | 116 | 6 | 2 | 77 / 44 / 36 / 40 / 36 |
| v2prop | 214 | 107 | 107 | 10 | 3 | 66 / 42 / 34 / 38 / 34 |

评测结果保存在 `artifacts/benchmarks/flight-identity-eval-v2-v3prop-r12-fast.json`、`flight-identity-eval-v2-v2prop-r12-fast.json`；r14 参考结果另存为 v3prop 的 `r14-fast` 文件。输出 JSON 包含整体指标和 `size_buckets` 分层指标。

构建和评测使用的核心命令（Windows PowerShell）如下，运行前先创建 `.tmp` 并设置 `TEMP`、`TMP`、`TMPDIR` 和 `PYTHONPATH=src`：

```powershell
python scripts/build_proposal_identity_eval.py `
  --source-root D:/Projects/Multi-Detect `
  --v3-cache D:/Projects/mdwt/yoloe-retrain/tmp/yoloe-v3-round-eval/r3/new-cache.jsonl `
  --old-cache D:/Projects/Multi-Detect/artifacts/datasets/onboard-proposals/v2r3seg-dagger-r8-cfs.jsonl `
  --out-v3 D:/Projects/Multi-Detect/artifacts/datasets/flight-identity-eval-v2-v3prop `
  --out-v2 D:/Projects/Multi-Detect/artifacts/datasets/flight-identity-eval-v2-v2prop `
  --max-per-object-bucket 3

python scripts/eval_slow_system.py --dataset <dataset> --data-root D:/Projects/Multi-Detect `
  --backend gguf --device cuda:0 --identity-only --fast-identity `
  --crop-policy candidate_zoom_v2 --gguf-model <r12.gguf> --mmproj <mmproj> `
  --llama-server <llama-server.exe> --output <benchmark.json>
```

