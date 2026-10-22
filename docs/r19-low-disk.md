# r19 低磁盘 round

## 结论

`run_r15_round.py --low-disk` 让每个 episode 的落盘量下降约 82%（离线样例：每 step 833,069 B → 150,469 B）。下游 SFT 构建、proposal cache、训练和导出都能读。segmentation 仍是无损 PNG，逐像素一致。整轮的峰值仍由导出阶段决定：合并后的 HF 权重和 F16 GGUF 必须同时存在，约 7.17 GiB，低磁盘模式没法降低这一项，只是用完后不再保留。按 r15 实测的平均 episode 长度，低磁盘整轮峰值约 7.6 GiB，跑完后剩余约 1.6 GiB；普通模式峰值和剩余都约 9.3 GiB。

## 低磁盘模式改了什么

| 项 | 普通 | `--low-disk` | 依据 |
|---|---|---|---|
| RGB | PNG | JPEG q90（`--rgb-format jpeg --rgb-quality 90`） | `cache_onboard_proposals.py` 用 `cv2.imread` 读，`build_slow_system_sft.py` 和训练用 PIL 读，都按内容解码，不看扩展名 |
| depth | png16 | 不落盘（`--no-depth`） | 本轮没有任何消费者读 depth |
| segmentation | PNG | PNG（不变，无损） | SFT 标签由它生成 |
| MAVLink | `mavlink.jsonl` | `mavlink.jsonl.gz`（`--compress-mavlink`） | replay、能耗标定和 smoke 读取器都已支持 `.gz` |
| 已消费的 segmentation | 保留 | build_sft 完成后记录哈希，然后删除 | 见下文“清理边界” |
| 导出中间件 | 保留 merged-hf 和 F16 | 先记哈希再删除（`--drop-intermediates`） | 只保留 Q5_K_M |

实现细节：

- `--no-depth` 只控制 recorder 是否写 depth，不改变传感器流。frame server 的 `--depth-contract-json` 是必填参数，非 x500 机型也会在 wire 上发送 MDGZDEP1 depth 帧。如果在流层面去掉 depth，reader 会读错帧边界，所以 collector 照常请求和解码 depth，只是不写盘。开启 `--video-hz` 时，连续视频同样不写 depth。
- 低磁盘 episode 会在 `episode.json` 里写明 `rgb_format`、`rgb_quality` 和 `depth_recorded: false`。默认 episode 的元数据格式保持不变。
- live 审计（`audit_dataset`）现在接受 `.png`、`.jpg` 和 `.jpeg`，并按扩展名核对文件头（PNG 签名或 JPEG SOI），所以改了后缀的 PNG 或 JSON 占位文件仍会被拒。只有声明了 `depth_recorded: false` 的 episode 可以不带 depth；这种 episode 如果反而引用了 depth，也会被拒。

## 每个 episode 的磁盘对比（离线样例）

样例取三个真实的 r15 episode（只读，共 56 steps，长度分别为 4、9、43 steps），用 `EpisodeRecorder` 按两种模式重新录制。普通模式使用本轮原有参数：PNG RGB、png16 depth、PNG seg、明文 MAVLink。低磁盘模式：JPEG q90、无 depth、PNG seg、gzip MAVLink。脚本和输出位于 `D:\Projects\mdwt\_scratch\r19_low_disk_sample.py` 和 `r19-low-disk-sample.json`，两种输出都通过了 `audit_dataset` 的 live 审计。

| episode | steps | 普通 (B) | 低磁盘 (B) | 降幅 |
|---|---:|---:|---:|---:|
| w0000/ep-2 | 4 | 4,555,900 | 770,687 | 83.1% |
| w0008/ep-2 | 9 | 9,394,148 | 1,442,505 | 84.6% |
| w0006/ep-1 | 43 | 32,701,800 | 6,213,059 | 81.0% |

每 step 分项（B）：

| 分项 | 普通 | 低磁盘 |
|---|---:|---:|
| MAVLink | 605,868 | 90,055（gz） |
| RGB | 123,550 | 42,321 |
| depth | 85,479 | 0 |
| segmentation | 9,916 | 9,916；build_sft 消费后清理为 0 |
| steps.jsonl + episode.json | 8,256 | 8,176 |
| 合计 | 833,069 | 150,469 |

MAVLink 是最大的一项。旧版测量只统计了媒体文件（RGB、depth 和 seg 共 0.187 GiB），漏掉了 r15 episodes 里 775 MiB 的 `mavlink.jsonl`。

下游可读性，在同一份样例的低磁盘输出上逐帧验证（112 帧）：

- `cv2.imread` 读出的形状为 960×1280×3，PIL 的 mode 为 `RGB`，满足 build_sft 的 `opened.mode == "RGB"` 过滤条件。
- `load_labels_map` 读出的 segmentation 与原 PNG 逐像素相等。
- `load_episode` 能从 `mavlink.jsonl.gz` 重建 replay。
- 相对原 PNG，JPEG 的最差 PSNR 为 38.5 dB。

## 整轮估算（20 worlds × 4 episodes）

r15 已完成的 79 个 episode 平均每个 11.5 steps（expert_search 找到目标后提前结束）。120 steps 是预算上限。

| 场景 | episodes 普通 | episodes 低磁盘 | 整轮峰值 普通 / 低磁盘 | 跑完剩余 普通 / 低磁盘 |
|---|---:|---:|---:|---:|
| 实测平均约 12 steps | 0.74 GiB | 0.13 GiB | 9.34 / 7.56 GiB | 9.34 / 1.56 GiB |
| 上限 120 steps | 7.45 GiB | 1.35 GiB | 16.04 / 8.77 GiB | 16.04 / 2.77 GiB |

导出常数取自 `r15-round/merged-gguf` 的实测值：merged-hf 4,255,140,312 B，F16 3,447,350,272 B，Q5_K_M 1,257,880,576 B。proposal cache、SFT、adapter 和评测 JSON 合计不到 0.25 GiB（r15 实测约 60 MiB）。`--dry-run` 会打印上表中对应 `--steps-per-episode` 的数值。在低磁盘模式下，每个阶段开始前还会检查“当前可用空间 − 本阶段新增峰值 ≥ `--min-free-gb`”；导出阶段按 7.17 GiB 计。估算没有计入失败重试留下的 attempt 目录：r15 有 130 个 episode 目录，其中 79 个被采纳。

## 清理边界

segmentation 清理（`cleanup_consumed_segmentation`）在 build_sft 成功后执行。一个文件只有同时满足以下条件才会被删除：

1. 它所在的 episode 在本轮 `round-state.json` 里是 complete；`episode.json` 位于 `--episodes` 之下，且 SHA-256 与采集时记录的一致。其他运行的 episode、`--extra-episodes`、失败的 attempt 和已有数据都不满足这一条。
2. 它位于该 episode 的 `seg/` 下，并且被该 episode 自己的 `steps.jsonl` 引用。
3. build_sft 的 `r15-additions/manifest.json` 在 `source_sha256` 中列出了这个文件，哈希与当前文件一致，也就是说标签确实是从这些字节生成的。被去重跳过、没有被消费的 mask 会保留。

删除前，每个文件的路径、大小和 SHA-256 会原子写入 `<out>/low-disk-cleanup.json`，同时记录 SFT manifest 的哈希。删除后再把对应条目标为 `removed`。清理可以重入，不会丢失已有记录。如果之后 build_sft 输出的哈希不再匹配，需要重建，而清理已经删过文件，runner 会直接报错停止，不会在缺 mask 的情况下静默重建。

RGB 不清理，因为 SFT 行通过 `image_path` 引用它，训练要读。depth 在低磁盘模式下本来就不写。

导出的 `--drop-intermediates` 只删本脚本自己创建的 `merged-hf`：目录里必须有 `.export-slow-system-owner.json` 标记，已存在但没有标记的目录会被拒绝，不覆盖也不删除。删除顺序是 F16 生成后删 merged-hf，量化后删 F16 和 `merge.py`。删除前把每个文件的大小和 SHA-256 写入 `export-intermediates.json`。

## 运行

```powershell
$env:PYTHONPATH = "src;."
python scripts/run_r15_round.py --dry-run --stage all --low-disk
python scripts/run_r15_round.py --stage all --low-disk `
  --scenarios D:/Projects/Multi-Detect/artifacts/r19-round/scenarios `
  --episodes D:/Projects/Multi-Detect/artifacts/r19-episodes `
  --out D:/Projects/Multi-Detect/artifacts/r19-round `
  --collector-min-free-gb 2 --min-free-gb 2
```

`--collector-min-free-gb` 单独设置 collector 的磁盘门禁，默认等于 `--min-free-gb`。D 盘只有十几 GB 空闲时，按上表，至少要留出约 8 GiB（平均长度）到 9 GiB（120 steps 上限）的余量给导出峰值。
