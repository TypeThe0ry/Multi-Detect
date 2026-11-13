# Altair v1 第一轮训练命令（采集数据未到位版：身份 + 规划完整集）

R=/data/projects/Multi-Detect-env/Multi-Detect，数据与参数依据 r21id-r3（lr 5e-5，1 epoch，accum 4，rank16/alpha32，从 r18 adapter 热启动）和 r24（lr 2.5e-5，train 5460）。

## 数据集（CPU，几分钟）
```
cd $R && PYTHONPATH=$R/src:$R .venv-vlm/bin/python -m scripts.altair.build_dataset \
  --source identity=$R/artifacts/r21id-r3/pipeline/r15-sft:22843 \
  --source planning=$R/artifacts/scratch/cx-planner-round/r24/dataset:5460 \
  --prefix-ids --crop-policy candidate_zoom_v2 --leak-scope same-source \
  --output $R/artifacts/datasets/altair-v1-r1
```
比例用行数本身，所以不重复、不丢行，总量 28303。`--prefix-ids` 解决 sample_id 碰撞；`--crop-policy` 固定为 r21id-r3/r24 训练用的 candidate_zoom_v2（文本规划行不读它）；`--leak-scope same-source` 只检查各源自己的 val/test 世界不得进自己的 train（规划世界名与身份世界名可能重名，跨源检查会误报；如果想严格跨源，去掉该参数，报错再看）。

## 训练（GPU0，约 7000 步）
预设 A = q/v r16，与热启动 adapter 的形状一致：
```
CUDA_VISIBLE_DEVICES=0 .venv-vlm/bin/python -m scripts.altair.train_altair --preset A \
  --dataset $R/artifacts/datasets/altair-v1-r1 --output $R/artifacts/models/altair-v1-r1-a \
  --accumulation 4 --epochs 1 --learning-rate 3e-5 --crop-policy candidate_zoom_v2 \
  --init-adapter $R/artifacts/models/slow-system-lora-v2-r21id-r3/adapter
```
热启动源取 r21id-r3（身份数据是主体）；学习率取 3e-5，介于 r21id-r3 的 5e-5 和 r24 的 2.5e-5 之间，让规划能学上而不冲掉身份。`--balance` 先不开（r21id-r3 和 r24 都没开，改变会让对比失去基准）。

## 评测
`bash scripts/altair/run_candidate_eval.sh $R/artifacts/models/altair-v1-r1-a/adapter v1-r1-a`

## 备选
若 A 的规划指标掉：对照冷启动 B/C，把 `--preset B` 或 `C`、去掉 `--init-adapter`、学习率改 5e-5（8000 步级）；这些预设的形状与热启动 adapter 不兼容，不能热启动。
