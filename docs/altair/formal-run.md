# Altair v1 正式训练命令（工作站）

R=/data/projects/Multi-Detect-env/Multi-Detect

数据源：回放用 `artifacts/datasets/plan-sft-v4`（1500 行，r24 replay 来源）。rw 规划/身份、固定翼、真实视频四个数据集由 pass1/pass2 采集和 Deneb 抽检完成后，各用 `scripts/build_slow_system_sft.py` 生成，路径记为 $RW_PLAN、$RW_ID、$FW、$VID（必须同一 crop_policy）。

```
cd $R && .venv-vlm/bin/python -m scripts.altair.build_dataset \
  --source rw_plan=$RW_PLAN:0.40 --source rw_id=$RW_ID:0.20 --source fw=$FW:0.20 \
  --source video=$VID:0.10 --source replay=$R/artifacts/datasets/plan-sft-v4:0.10 \
  --output $R/artifacts/datasets/altair-v1
CUDA_VISIBLE_DEVICES=0 .venv-vlm/bin/python -m scripts.altair.train_altair --preset C \
  --dataset $R/artifacts/datasets/altair-v1 --output $R/artifacts/models/altair-v1-c
bash scripts/altair/merge_and_gguf.sh $R/artifacts/models/altair-v1-c/adapter $R/artifacts/models/altair-v1-c-gguf
```

混合比例是精确的，总量由最小的源按比例反推；回放源只有 1500 行时，总量上限约 15000 行，可用 `--max-train` 手动封顶。
