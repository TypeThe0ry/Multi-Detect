# r9r SUV→postbox 正例缺失诊断（2026-11-07）

## 结论与 positive 的含义

根因是 **builder 的同帧正例配对使用了错误的类别相等判断**。SUV truth 的 `class="suv"`，但 `category_words=["car", "suv"]`；`_target_category()` 优先取第一个词，得到 `car`。旧代码用 `_target_category(q["target"]) == "suv"` 找正例，因而漏掉实际可见的 SUV。同一个 SUV 可以为 `car→postbox` 贡献正例，却不能为 `suv→postbox` 贡献正例。

`A→B` 是“任务要求 A、拒绝 B”的方向，不是物体变换。该方向的 positive 是任务找 A、focus 为 A，决策为 `confirm_target` 或 `approach_track`；rejection 是同任务下 focus 为 B、决策为 `reject_track`。所以 **`suv→postbox` 的正例对象是 SUV，postbox 是干扰物**。原 world 中恰有一个 `true_target=true` 的 SUV 和一个 `confuser=true, true_target=false` 的 postbox；`--teacher-target confuser` 让采集路线围绕 postbox，不会把 builder 的任务真值改成 postbox。

若任务改为“找 postbox”，postbox 可以成为正例，那是反向 `postbox→suv`。原 pilot 最终 JSONL 已有该反向 150 条 postbox 正例，分四视角为 31 / 41 / 37 / 41。把这 150 条计入 `suv→postbox` 会混淆任务语义。

本次只修改 builder 的正例匹配和回归测试，**不修改生成器、world、truth、相机或路线；不需要重新采集，也不需要重新跑 YOLOE**。现有 pilot RGB/segmentation/proposal cache 足以重建；正在采集的 full worlds 可继续使用。full annotate 必须使用修复后的 builder，再审计产量，不能把 pilot 的结果当作 full 验收。

如果 full annotate 从另一临时工作树或快照启动，需要先同步这处 builder 修复；当前工作树的修改不会自动更新那些副本。已经产生的旧 SFT 可用原 cache 重新做 CPU build。正例数量改变后，r10 的 replay/additions 比例和各 view 覆盖应重新按最终 JSONL 核对。

## 输入与证据绑定

- world：`configs/scenarios/generated-r21id-r9r`，48 个 world，两个方向各 24；每方向 18 train / 6 val。
- 本次 pilot：`artifacts/r21id-r9rb/episodes` 中 annotate manifest 声明的 `w0000–w0003`、`w0024–w0027`，全部按原 pilot 的 train 切分重建；没有把后来采集的其他 world 混入。
- proposal：`artifacts/r21id-r9rb/gpu-annotate/proposals.jsonl`，原 cache 共 1,598 张 RGB、2,866 个 box；缓存已经过 onboard proposer 的筛选，不能据此判断被缓存前筛掉的原始 detector box。
- 原输出：同目录 `sft/train.jsonl`、`sft/manifest.json`，4,372 行。`source_counts` 是生成/筛选过程的计数，不能直接当作最终 JSONL 行数。
- annotate 配方：`run.json` 中为 `--confusable-negatives --task-text-variants --crop-policy none --min-visible-fraction 0.3 --confirm-min-side-px 8 --confusable-negative-view-cap 30`，没有开启 unseen holdout、task-variant positive cap 或 balance upsampling。

原 `run.json` 的 index 位于另一个临时工作树，不在当前 configs 下；实际只读找到了 `/data/projects/Multi-Detect-env/runs/src-r21r9r2/configs/scenarios/generated-r21id-r9r/pilot-index.json`，SHA256 为 `727ffe9c16a3ab3084a0facaa0dbf2c5278d486238d1194496fab54a3453b13e`，与原 manifest 一致。当前 truth、teacher-intent、SDF 的哈希也全部匹配 manifest。恢复该 index 到本树 `.tmp`，在同目录用符号链接引用原 sidecars，避免 index 搬到 `.tmp` 后找不到 truth。恢复件与原 index 字节一致；只把 full index 缩为 8 个 world 而不改顶层 `count=48`，虽不改变 builder 选帧逻辑，但其哈希不会与原 pilot 一致。

全部 artifacts 只读。逐帧审计、恢复的 index 和 CPU builder 输出均放在 `.tmp/r9r-suv-postbox-20261107/`。

## world、episode 与 proposal 到 builder 的路径

四个 SUV world 的主目标均为源尺寸 SUV `5.020146 × 2.603742 × 2.156538 m`，postbox 为 `0.681827 × 0.684864 × 1.524 m`，postbox 在 SUV 以东 3.5 m。`target_combo.category="suv"`，主任务中英文均明确要求 SUV；`task_text_variants` 包含 `suv` 和 `postbox`。生成器已经写入正确的任务与两个对象，不应通过修改 `category_words` 顺序来修复 builder。

注意 `w0026` 另有一个 `asset_slug="postbox"` 的普通 distractor：`distractor_0026_02`。原反向 150 条 postbox 正例中，124 条 focus 为四个 world 的指定 confuser，26 条为这个额外 postbox；原 `suv→postbox` 的 120 条拒绝中，相应为 97 / 23 条。pair 按任务类别成立，不要求 focus 必须有 `confuser=true`。下文类别 funnel 对所有 postbox 取并集，唯一 RGB 只计一次；生成计数则可能在同帧有两个 postbox 时计两次。

`run_r9p_gpu_annotate.py` 先调用 `cache_onboard_proposals.py`，再调用 CPU builder。proposal key 使用存放 campaign 的仓库根目录，因此原 key 为 `artifacts/r21id-r9rb/episodes/...jpg`；builder 的 `_relative()` 在 worktree 中也能得到同样的 key，不是路径错配。

builder 中有两条独立的候选路径：

1. `_eligible_frames()` 对 complete episode 的 before/after RGB 检查 skew、文件存在性、RGB 模式，按 world 做 dHash 近重复去重。本 pilot 有 segmentation，候选框来自真实可见像素（component area≥4 px、bbox 两边≥8 px），不是从完整几何投影猜测可见性。此路径产出 `true_target` 和 `confusable_positive/negative`。`--min-visible-fraction 0.3` 用于无 segmentation 的投影分支，不是本 pilot 的额外掉落条件。
2. `_proposal_rows()` 独立给 YOLOE cache 的框贴标签，短边≥10 px，取与 segmentation scene 的最高 IoU，IoU≥0.3 才算匹配；真实 SUV 命中产生 `proposal_true`。它不会为这些 rows 写入 `confusable_pair_type`。`--confirm-min-side-px 8` 控制 confirm/approach，不能把正例变成拒绝，也不是这里零正例的原因。

因此，“没有 `suv→postbox` positive”不能等价为“YOLOE 从未提出 SUV”，更不能等价为“postbox 从未可见”。本次分别审计 segmentation 可见、cache 匹配、配对生成及最终保留行。

原 JSONL 已可直接否定“没有看到或没有提出 SUV”：四个 SUV world 有普通 `true_target` 正例 144 条，`proposal_true` 正例 128 条，全部为 `confirm_target`。其中四个有效 view 的普通正例为 27 / 39 / 32 / 29，proposal 正例为 23 / 32 / 29 / 27；两者各另有 17 条 view 为空，不能摊入四个 view。原 `car→postbox` 最终正例为 107 条，其 focus 都是这些 world 的真 SUV（四视角 19 / 32 / 28 / 28）；旧类别判断把它们放进了父类 car 的配对。

postbox 也不是从未被 YOLOE 提出或未匹配：原最终输出有 81 条与 postbox scene 匹配的 `proposal_distractor`，按 `w0024 / w0025 / w0026 / w0027` 为 12 / 21 / 27 / 21 条；这些是框/行数，不是唯一 RGB 数。`w0026` 的 27 条中，6 条为指定 confuser、21 条为额外 postbox。

## 逐帧 funnel：缺口位于类别配对

8 个 pilot world 有 16 个 complete episode，另有 `w0003/ep-0/attempt-0` 的一次 failed 尝试。cache 包含该失败尝试的 50 张 RGB；builder 跳过它。complete episodes 共 1,548 张有效 RGB，dHash 去重后 builder 保留 499 张，与原 manifest 的 `eligible_frames=499` 一致。以下仅统计其中 4 个 SUV world，view 使用实际 observation pose 与原 teacher-intent 最近 waypoint 派生，与 builder 一致；view 为空的过渡帧单列。

**去重前的唯一 RGB：**可见要求真实 segmentation component area≥4 px、bbox 短边≥8 px。postbox 列包含同类别的两个可能对象，按 RGB 取并集。

| view | RGB | SUV 可见 | postbox 可见 | 两类同帧可见 |
|---|---:|---:|---:|---:|
| front | 184 | 175 | 164 | 164 |
| oblique | 184 | 184 | 184 | 184 |
| edge_truncated | 190 | 190 | 190 | 190 |
| recovery | 174 | 158 | 158 | 158 |
| view 为空 | 40 | 40 | 0 | 0 |
| 合计 | 774 | 747 | 696 | 696 |

**builder 去重后：**proposal 匹配按缓存框短边≥10 px、与 segmentation scene 的最高 IoU≥0.3 计算；每列仍按唯一 RGB 计数，不按 box 数计数。所有 150 张保留 RGB 均有对应 cache key。

| view | 保留 RGB | 两类同帧可见 | SUV proposal 命中 | postbox proposal 命中 | 两类 proposal 均命中 | 旧配对找到 SUV |
|---|---:|---:|---:|---:|---:|---:|
| front | 29 | 24 | 23 | 20 | 17 | 0 |
| oblique | 39 | 39 | 32 | 18 | 17 | 0 |
| edge_truncated | 32 | 32 | 28 | 30 | 27 | 0 |
| recovery | 33 | 29 | 27 | 10 | 10 | 0 |
| view 为空 | 17 | 0 | 17 | 0 | 0 | 0 |
| 合计 | 150 | 124 | 127 | 78 | 71 | 0 |

即使额外要求两类都被 YOLOE 命中，仍有 71 张去重后的同帧 RGB，而旧配对找不到任何 SUV。实际 confusable pairing 不要求该 proposal 条件，所以其正确输入是 124 张同帧可见 RGB。由此可逐层排除“完全没看到”“完全没 proposal”“完全没匹配”；剩余的全量掉落发生在 `_target_category(SUV) == "suv"` 这个错误比较。

| SUV world | 原 RGB | 去重后 RGB | 两类同帧可见 | 两类 proposal 均命中 | 旧配对找到 SUV | 修复后可配对 RGB |
|---|---:|---:|---:|---:|---:|---:|
| w0024 | 182 | 27 | 24 | 10 | 0 | 24 |
| w0025 | 192 | 44 | 37 | 20 | 0 | 37 |
| w0026 | 198 | 45 | 36 | 20 | 0 | 36 |
| w0027 | 200 | 34 | 27 | 21 | 0 | 27 |

修复后的语义判断接受全部 124 张有效同帧 RGB，覆盖四个 world；同帧第二个 postbox 可再生成一次相同 SUV 正例，所以生成行数与唯一 RGB 数不能混用。detector 在部分 recovery 帧仍漏检，这是后续 detector 覆盖问题，但不能解释旧 builder 四视角全部零正例。

## 修复后 CPU 重建实测

最终代码用原 RGB/segmentation/cache 和原 pilot index 完整重建，输出为 `.tmp/r9r-suv-postbox-20261107/sft-final/`；manifest 记录的 builder SHA256 与当前脚本一致（`93138302b2979094b0c547b91c52bb6c3f256c3eeef5b711db57443fdac71488`），index 哈希与原 pilot 一致。没有使用新采集的其他 world。

最终 `train.jsonl` 的 SHA256 为 `0792df3894e8c818f4670b45afc3cb923c9e2168002f55fd0d3def16430f4d5f`。已核对原 manifest 与新 manifest 中全部共同数据输入的哈希、原 world split、输出文件哈希及无冲突检查；除 builder 代码修复和临时 index 的存放路径外，所用数据与配方未改变。

| pair | view | 原 positive | 修复后 positive | 原 rejection | 修复后 rejection |
|---|---|---:|---:|---:|---:|
| car→mailbox | front | 45 | 44 | 30 | 30 |
| car→mailbox | oblique | 49 | 50 | 30 | 30 |
| car→mailbox | edge_truncated | 45 | 53 | 30 | 30 |
| car→mailbox | recovery | 68 | 68 | 30 | 30 |
| suv→postbox | front | 0 | 17 | 30 | 30 |
| suv→postbox | oblique | 0 | 27 | 30 | 30 |
| suv→postbox | edge_truncated | 0 | 21 | 30 | 30 |
| suv→postbox | recovery | 0 | 23 | 30 | 30 |

SUV 方向生成计数为 **150**，对应 **124 张同帧可见 RGB**（额外 postbox 贡献 26 次重复配对）；最终 pair-tag 正例为 **88 行 / 88 张 RGB / 4 个 world**，全部 focus 为该 world 的真 SUV、全部 `confirm_target`。四个 view 各自均覆盖全部 4 个 SUV world。按 world 的最终正例为 `w0024=16、w0025=20、w0026=29、w0027=23`。生成 150 行后没有全部留下 pair 标签，是同帧重复配对、与普通正例或其他 pair 正例的内容去重所致，不是再次漏匹配。

`car→mailbox` 的生成正例仍为 327，最终 pair-tag 行数从 207 变为 215。这不是新图像或 car 内容的增减：新增正例改变了相同 seed 下 shuffle 的排列，去重保留的来源标签可随之变化。用 `_sample_content_key()`（world、RGB、task、完整 candidates、assistant target、balance-copy）逐条对照，**原 4,372 条模型可见输入/标签全部保留**；总行数变为 **4,433**，新增 **61 条不同的模型内容**，最终标签分别为 `suv→postbox=47、suv→pickup=10、mailbox→person=4`。后两类也是原先首词比较漏掉的 SUV 子类或 postbox/mailbox 别名匹配；不能把 88 条新 pair-tag 行都声称为 88 条新增内容。

所有 rejection 模型内容保留，总数仍为 2,492；confirm 从 1,857 增至 1,918；search 10、clarify 13 不变。train 仍为 8 个原 pilot world，val/test 为空，没有跨 split，`conflicting_decision_groups=0`。这次 CPU 实测证明无需重新采集即可恢复该 pair 的正例，但不足以替代 full 的独立 val 验收。

## 现有 cache 的 CPU 重建方法

下面从当前 index 重建原 pilot 的输入，在 `.tmp` 下建独立目录；SHA256 断言绑定原 pilot。它不运行采集、detector 或训练，也不向 artifacts 写入。若目录已经存在，应另选新的输出目录。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

scenarios = Path("configs/scenarios/generated-r21id-r9r").resolve()
folder = Path(".tmp/r9r-suv-postbox-rebuild/input")
folder.mkdir(parents=True, exist_ok=False)
doc = json.loads((scenarios / "index.json").read_text(encoding="utf-8"))
doc["worlds"] = [w for w in doc["worlds"] if int(w["world_id"][-4:]) % 24 < 4]
doc["count"] = len(doc["worlds"])
assert doc["count"] == 8
assert all(w["split"] == "train" for w in doc["worlds"])
payload = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
assert hashlib.sha256(payload).hexdigest() == "727ffe9c16a3ab3084a0facaa0dbf2c5278d486238d1194496fab54a3453b13e"
(folder / "pilot-index.json").write_bytes(payload)
for world in doc["worlds"]:
    for suffix in ("truth.json", "teacher_intent.json", "scenario.json"):
        name = f"{world['world_id']}.{suffix}"
        (folder / name).symlink_to(scenarios / name)
PY
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/build_slow_system_sft.py \
  --campaign artifacts/r21id-r9rb/episodes \
  --index .tmp/r9r-suv-postbox-rebuild/input/pilot-index.json \
  --output .tmp/r9r-suv-postbox-rebuild/sft \
  --proposals artifacts/r21id-r9rb/gpu-annotate/proposals.jsonl \
  --confusable-negatives --task-text-variants --crop-policy none \
  --min-visible-fraction 0.3 --confirm-min-side-px 8 \
  --confusable-negative-view-cap 30
```

## 代码修复与去重口径

`CONFUSABLE_NEGATIVES` 按被拒绝候选的类别查表，`postbox` 项已经含有 `suv`，所以 SUV 方向的拒绝分支会正常执行。它不是缺少 `suv→postbox` 的配置项，而是在该分支内查找同帧正例时漏掉了 SUV；新增或改写配对表不能解决这个类别比较错误。

`scripts/build_slow_system_sft.py` 中，将 hard-negative pairing 的同类候选查找改为已有的 `_category_task_equivalent(near_category, candidate)`，并明确排除 `held_out_slugs` 中的正例 focus。该函数基于任务的 descriptor 与 `task_equivalent()`，支持“找 car 可接受 SUV”等父子类别语义，也能准确处理“找 SUV”；普通 hatchback 和 pickup 不会因此变成 SUV 正例。保留 `_target_category()` 的既有用途，避免全局重定义类别而改变其他 reject 方向与预算。本 pilot 的 held-out 集合为空，holdout guard 不改变其重建产量。

正例在每个可用的同帧配对上生成，不受每 view 30 条的 rejection cap 限制。后续 negative-ratio 筛选保留正例，近重复帧筛选发生在配对之前；最终 `_dedupe_rows()` 会合并模型输入/目标相同的行，且不把 `label_type`、pair tag 当成模型内容。因此同一正确 SUV 决策有时保留为普通 `true_target`，有时保留为 `confusable_positive`，pair-tag 行数可能小于生成计数。应同时报告生成计数、最终 pair-tag 行数、唯一 image/world 覆盖，不能为了给 pair 补数字而制造重复行。

测试 `test_confusable_rejects_keep_other_focus_when_primary_target_matches_task` 原先用了不符合生成 truth 的 `["suv", "suv"]`。现改为真实 `["car", "suv"]`，并增加 hatchback/pickup 干扰物；断言 `suv→postbox` 恰好生成一个 SUV 正例、保留正确 target/focus/pair tag，postbox 仍拒绝、无决策冲突。SUV 主任务用另一种措辞，保证正例不会因与普通行完全相同而在测试中被去重掩盖。另测显式 holdout 的 SUV 作为其他允许 world 的 distractor 出现时，也不能经新语义匹配成为训练 focus。修复前该测试明确因缺失 `confusable_pair:suv->postbox:positive` 失败，修复后通过。

## 验证与使用边界

相关测试（单进程）：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_slow_system_sft.py tests/test_r21id_r9r_worlds.py -q -p no:xdist
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/run_onboard_gate.py -q -p no:warnings -p no:xdist
```

前者 24 项通过；后者在最终代码上 PASS，1730 passed / 13 skipped，175 秒。没有放宽或删除断言，现有 Pillow 弃用告警不影响结果。

没有启动 SITL、Docker、GPU 推理或训练，没有联网下载，没有修改共享 artifacts 或提交。此修复解决配对漏标；pilot 只有每方向 4 个独立 world，不能证明 full 的每 view 覆盖、YOLOE 召回或未见 world 泛化已经达标。原 r10 计划中“可能没有 SUV 同类 proposal”的解释应由本报告的可见性、proposal 与类别配对证据替代。
