# Altair v1 训练计划（Qwen3-VL-2B 特化机载慢系统）

## 命名
- Altair = Qwen3-VL-2B 特化机载慢系统。现役：Altair-0.24p（r24 规划）、Altair-0.21i（r21id-r3 身份）。
- Altair v1 = 规划+身份合并的单个多任务 LoRA，并新增固定翼巡航/过渡。
- Vega = YOLOE 快检测器（r2 为 Vega v1）；Deneb = 工作站教师（官方 Qwen3-VL-8B + YOLOE，不上机）。

## 数据（CPU 先行，不占 GPU）
| 来源 | 占比 | 说明 |
|---|---|---|
| rw 真实地形采集：规划样本 | 40% | pass1(hover3)+pass2(hover3p2)，先过 filter_episodes.py，剔除运动补偿开时的差 episode |
| rw 采集：身份样本 | 20% | 同上 |
| 固定翼巡航/过渡 | 20% | --fw-cruise-observe 采集（含 MC→FW→MC 过渡） |
| 真实视频（Deneb 伪标签） | 10% | 先抽检 200 帧，一致率 ≥0.9 才入训 |
| 回放：r24/r21id-r3 旧训练集 | 10% | 防遗忘 |
- 按世界/episode 切分 train/val（val 整个世界留出，防泄漏）。
- 三角翼+云台样本优先；x500 等只保留少量。

## 训练配置（GPU0 单卡 20GB 够用）
- 基座：官方 Qwen3-VL-2B-Instruct；视觉塔冻结，只训语言侧。
- LoRA：r=32, alpha=64, 目标 q/k/v/o + gate/up/down（MLP）。
- bf16，bs1×accum16，lr 1e-4（常数；现有训练器无调度器，不为此改动现役训练代码），2 epoch，最大边 448。
- 对照：A=q/v r16（复现 r24 配方）、B=q/k/v/o r16、C=全层 r32（主推）。主线 C，GPU 有空时再跑 A/B。
- 产物：LoRA → 合并 → Q5_K_M GGUF，与现役同格式，可直接换进 configs/onboard。

## 评测与门槛
- 沿用 r24 / r21id-r3 的验收脚本与阈值：规划不低于 Altair-0.24p，身份不低于 Altair-0.21i，FW 新增项单独设阈值（巡航保持、过渡时机、拒绝预检不退化）。
- 再跑 sim-loop-v3 回环（含规划前确定性拒绝预检）。
- 任何一项不过，不替换现役模型，仅留作 v1 候选。

## GPU 排期建议（交算力线程）
- Vega v1(r2) 预计 11-14 凌晨占 GPU1–3，所以 Altair 放 GPU0：需要让 GPU0 的伪标签循环让出约 6–10 小时；若不让，则等 r2 主训完成后用 GPU1。
- 数据集构建完成（pass1 预计 11-14 02:00 左右）后才开训；之前只做脚本与冒烟（≤50 步）。

## 脚本（scripts/altair/）
- `build_dataset.py`：多源按比例混合（精确比例、不过采样、要求 crop_policy 一致、val/test 世界不得泄漏进 train）。
- `train_altair.py`：包装现有 `train_slow_system_lora.py`，只换 LoRA 目标层与默认值（`--preset A|B|C`，默认 C）。
- `eval_altair.py`：验收门槛判定（相对现役不退化 + 绝对阈值，固定翼无门槛/缺指标一律不通过）。
- `merge_and_gguf.sh`：合并 → F16 GGUF → 量化（视觉塔冻结，mmproj 不变）。
- 已知缺口：eval_slow_system.py 输出到 eval_altair 报告格式的转换，待冒烟产出后补。
