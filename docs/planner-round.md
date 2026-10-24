# r21-planner 一键训练评测

`scripts/run_planner_round.py` 将规划 SFT 数据、LoRA 训练、Q5_K_M GGUF 导出、planning-v3/v4 对比和 15 条返航探测串成可断点续跑的五阶段流水线。阶段输出写入 `round-state.json`，只有输出文件存在且 SHA-256 未变化才会跳过。训练和导出显式使用 `D:/Projects/Multi-Detect/.venv-vlm/Scripts/python.exe`；系统 Python 只负责数据构造与编排。低磁盘导出使用 `--drop-intermediates`，会保留导出账本并删除本轮归属的 merged-HF/F16 中间物。

## Dry-run

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage all --dry-run --out D:/Projects/mdwt/_scratch/cx-planner-round/r21
```

该命令只打印阶段命令、解释器、输出路径、磁盘/内存门槛和锁路径，不训练、不导出、不启动 llama-server。可用 `--stage data --rows 10 --val-rows 2 --intent-count 1` 单独生成小样本数据验证 manifest/provenance。

## 正式运行

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage all `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r21 `
  --rows 2000 --val-rows 100 --intent-count 40 `
  --min-free-disk-gb 12 --min-memory-gb 8
```

预计数据构造少于 1 分钟；LoRA 训练约 2--4 小时（依数据量和 GPU）；导出约 1--3 小时；规划评测约 1--2 小时；报告少于 1 分钟。总墙钟约 4--9 小时。评测阶段会分别记录候选和 r14 的 planning-v3、planning-v4 结果以及 15 条返航探测，规划入口使用 `--cache-ram 0` 的默认设置。

## 替换判据

只有同时满足以下条件，报告才写“建议替换 r14”：候选 planning-v3 不低于 r14；planning-v4 的返航、未知地点或能力类别至少一个相对基线提升 10 个百分点；15 条返航探测全部产生 `return_home` 或被正确拒绝。报告同时列出总分、类别准确率/拒绝正确率、意图违规率、能力违规率和探测通过数。
