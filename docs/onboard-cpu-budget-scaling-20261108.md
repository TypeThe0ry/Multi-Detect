# Onboard association 候选规模扫描（2026-11-08）

## 结论

在 CPU-only 主机上对生产 `OnboardLoop.update_tracks()` 做了 16、32、64、128 candidates × 同数量 tracks 的固定几何扫描。该合成压力输入下 p50 从 0.716 ms 增至 2,005.451 ms；128 档出现明显非线性成本。结果只描述此压力 workload，不能当作实际 detector 候选分布或端到端机载延迟。

每个规模都以相同输入重复调用真实 association 入口，并对返回 track 的 ID、bbox、step、hits、ground 和 appearance 字段逐项与确定性基准快照比较；全部一致。这里没有实现 association 优化，因此等价性是重复运行确定性检查，不是新旧算法的差分证明。

**原始 RGB 与部署预处理尚未完成。** `tests/fixtures/onboard_replay/` 当前只有 `episode.json` 和 `steps.jsonl`，其中仅 1 条 metadata row，没有 RGB 文件或 `rgb_path`。因此本轮无法用固定原始 RGB 驱动预处理，也无法从真实 detector 输出生成候选；benchmark 显式记录 `original_rgb_frames: 0`，未用合成像素冒充 replay 图像。满足完整目标前，需要先提供该 replay 对应的原始帧及尺寸/编码元数据。

## 测量口径

- 入口：`src/multidetect/autonomy/onboard_loop.py` 的 `OnboardLoop.update_tracks()`，不修改生产实现。
- 每档输入：N 个候选框与 N 个已有 track，N 为 16、32、64、128；相机尺寸 640×480，候选框固定 16×16、互相分隔；无 ground projection、无 RGB appearance。候选几何是合成压力输入，不来自 replay detector。
- 每档先 warmup 2 次，再采样 10 次；单次耗时用 `time.perf_counter_ns()`。p95 使用 nearest-rank；10 样本下即最大值，尾部估计较粗。
- RSS 是同一进程每档结束时的 `/proc/self/status` `VmRSS` 绝对值，不是该档增量；进程 `ru_maxrss` 高水位为 54,360 KiB。运行未启用 GPU、模型服务、Docker 或 SITL。
- replay metadata 合并 SHA-256：`1cfc7ac6eee87a4232f8a54f719e0e38f6c9bb505c882a00643e1cc6d8627af4`。

## 结果

| Candidates | Tracks | p50 (ms) | p95 (ms) | RSS 档后 (KiB) | 输出项 | 输出 SHA-256 |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 16 | 16 | 0.716217 | 1.549982 | 49,524 | 16 | `a014d7ef9db943a6eed7c713d0f74f573c0dead835a1f6d5730bab77b93249b4` |
| 32 | 32 | 2.330788 | 5.380207 | 49,680 | 32 | `bba6e3cb12fe93974aface997b51d9f84c72092841479a08d9cfa19bec133d7c` |
| 64 | 64 | 11.623489 | 12.891920 | 50,720 | 64 | `e863c214aa6fabda608b9026ec4ed6e686bd89c2a93fa7222f74c9940fcc70be` |
| 128 | 128 | 2,005.451451 | 2,313.918895 | 57,184 | 128 | `575dc08a72624a7ffbdef43d58d415a1319eecec8f8ffcf650ed01101876c12d` |

128 档 p95 约 2.31 s，足以说明该最坏配对规模需要单独调查；单轮扫描不足以区分算法增长、输入结构影响与主机调度噪声。下一次真实帧复测应保留部署候选上限/过滤配置，并按固定 detector 输出逐项核对 association 结果。

## 复现

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/profile_onboard_association_scaling.py \
  --repeats 10 --warmup 2 --output .tmp/onboard-association-scaling.json
```

窄测试：`tests/test_profile_onboard_association_scaling.py`。本轮没有修改 `onboard_loop.py`、既有 CPU 报告、任务 expected、安全阈值、timeout/retry 或 `artifacts/`。

## 建议的下一步

取得 `tests/fixtures/onboard_replay/` 对应的原始 RGB replay 帧并补充只读 provenance（帧 ID、原始字节 hash、编码/尺寸）；随后用部署一致的 CPU-only 图像 decode/preprocess 和本地可用 detector 输出驱动候选规模扫描，保留 p50/p95/RSS 和逐项输出比较。不得以合成帧替代原始帧，也不得修改任务 expected、安全阈值、timeout/retry 或共享 `artifacts/`。
