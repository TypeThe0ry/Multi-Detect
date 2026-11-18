# 真实地景广域批量采集

`wide_collect_batch.py` 把采集规格展开为独立作业，为每次尝试准备场景，再调用 RTF 矩阵执行器。它复用已构建的场景、世界和航线，不负责生成广域覆盖航线。

## CLI

从仓库根目录运行，规格中的相对路径均以仓库根目录为基准，而非规格文件所在目录。

```bash
python scripts/realworld/wide_collect_batch.py SPEC [--plan-only] [--matrix-command PATH]
```

- `SPEC`：JSON 规格文件路径。
- `--plan-only`：只展开规格并打印批次 manifest，不检查本地资产是否齐全、不写文件，也不启动矩阵执行器或模拟器。场景和资产校验在每次尝试开始时进行。
- `--matrix-command PATH`：替换默认的 `scripts/realworld/rtf_matrix.sh` 执行器；仅在实际执行时调用。

## JSON 规格

顶层为对象，`jobs` 为作业数组。

| 顶层字段 | 含义 |
| --- | --- |
| `batch_id` | 可选。省略时使用确定性默认值，不随调用时间变化。 |
| `gpu` | 默认 `1`，仅允许 `1` 或 `3`。 |
| `slots` | 默认 `[5, 6, 7]`。按数组顺序为去重后的作业轮流分配 slot，重试不推进轮转。 |
| `output_root` | 默认 `/data/datasets/multidetect-sim-assets/wide-collect/batches/<batch_id>`。指定时，此路径就是批次目录，不再附加 `batch_id`。 |
| `share_gpu` | 可选布尔值。沿用矩阵兼容配置；批次和单轮模式的外部 GPU 进程门控不允许用此项绕过。 |

负载门控和重排参数也位于顶层，不改变作业 ID：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `start_load_max` | `60` | 派发时允许的主机 1 分钟 loadavg 上限（绝对值，不除以核数）。 |
| `start_wait_s` | `3600` | 每次开飞门控的最长等待秒数。 |
| `poll_interval_s` | `2` | 门控和跑中监控的采样间隔秒数。 |
| `abort_rtf` | `0.8` | 跑中 RTF 滑窗中位数的作废阈值。 |
| `abort_load_max` | `120` | 跑中主机 1 分钟 loadavg 的作废阈值。 |
| `abort_duration_s` | `90` | 任一条件必须连续超限的秒数。 |
| `rtf_window_s` | `30` | RTF 滑动窗口秒数。 |
| `max_retries` | `3` | 每个作业因负载或 RTF 作废后允许的最大重排次数；首次尝试不计入。 |
| `retry_backoff_s` | `30` | 首次重排的退避秒数，后续按 `30 × 2^(重排次数−1)` 增长。 |
| `retry_backoff_max_s` | `600` | 指数退避的上限秒数。 |

每个作业的字段：

| 字段 | 含义 |
| --- | --- |
| `site` | 必填，站点名。 |
| `altitude_m` | 必填，请求的巡航离地高度（AGL，单位米），范围为 `(0, 120]`。 |
| `time_of_day`、`weather` | 必填，按下表选择已有预设。 |
| `seed` | 必填，传给运行时和采集器的作业种子。 |
| `scenario` | 可选，显式场景文件路径或站点名。省略时按 `site` 和天气预设解析场景。 |
| `max_artifact_bytes` | 可选，单次尝试的产物大小上限，默认 `8589934592` 字节（8 GiB）。 |

`resolved_root`、`overlay_root`、`variant_model_root`、`variant_world_root`、`variant_map_root`、`variant_terrain_root` 和 `server_config` 均为可选路径，可在顶层设置以供作业继承，也可在单个作业中覆盖。世界、地图和地形覆盖路径应指向与场景文件对应的同一变体。

### 天气和高度

| `time_of_day` | `weather` | 已构建的预设 |
| --- | --- | --- |
| `noon` | `clear` | `clear_noon` |
| `noon` | `partly_cloudy`、`overcast`、`rain`、`windy` | 分别为同名预设 |
| `morning`、`evening`、`dusk`、`night` | `clear` | 分别为与 `time_of_day` 同名的预设 |

其他组合不支持。作业选用与预设匹配的已有世界；即使显式指定 `scenario`，也应使用与所选预设匹配的场景。批次不会重新生成世界，也不会用作业 `seed` 重新播种世界。

`altitude_m` 用于按数字高程模型（DEM）调整已有航线的巡航航点。它不是全航段的实测 AGL，不是飞行安全认证，也不会生成广域覆盖航线。

## Iowa 6 km 示例

以下示例沿用 `.tmp/rtf-speedup-run/manifest-v9-ab2.json` 中的 V9 场景和资产路径。V9 场景、SDF、地图和 DEM 的文件名主干均为 `rw_farm_iowa_w6km__rtf_v9__clear_noon`；不要用缺少 `__rtf_v9__` 的基础场景替换。运行前需有示例引用的本地 `.tmp` 资产。

```json
{
  "batch_id": "iowa-6km-v9",
  "gpu": 1,
  "slots": [5, 6, 7],
  "start_load_max": 60,
  "abort_rtf": 0.8,
  "abort_load_max": 120,
  "abort_duration_s": 90,
  "max_retries": 3,
  "output_root": ".tmp/wide-collect/iowa-6km-v9",
  "resolved_root": ".tmp/realworld-wide-6km/models",
  "overlay_root": ".tmp/rtf-speedup-run/variants/V9/models",
  "variant_world_root": ".tmp/rtf-speedup-run/variants/V9/worlds",
  "variant_map_root": ".tmp/rtf-speedup-run/variants/V9/assets/maps",
  "variant_terrain_root": ".tmp/rtf-speedup-run/variants/V9/assets/terrain",
  "server_config": ".tmp/rtf-speedup-run/variants/V9/server.config",
  "jobs": [
    {
      "site": "rw_farm_iowa",
      "scenario": ".tmp/rtf-speedup-run/variants/V9/scenario/rw_farm_iowa_w6km__rtf_v9__clear_noon.scenario.json",
      "altitude_m": 90,
      "time_of_day": "noon",
      "weather": "clear",
      "seed": 42
    }
  ]
}
```

例如，将该规格用作 `iowa-6km-v9.json`，先检查展开计划，再用同一规格执行：

```bash
python scripts/realworld/wide_collect_batch.py iowa-6km-v9.json --plan-only
python scripts/realworld/wide_collect_batch.py iowa-6km-v9.json
```

## 重跑、容量和产物

作业 ID 是规范化作业参数及资产路径的 SHA-256，不含 GPU、slot 和调度阈值；相同输入去重。再次运行同一批次时，在负载/GPU 探测前跳过已接纳作业，保留其采集产物。未接纳作业创建新的 `attempt-N`；因负载或 RTF 作废的重排计数和退避截止时间跨调用恢复，达到重试上限后不再自动派发。普通验收失败每次调用仍只尝试一次。资产文件实际哈希随尝试记录；替换原路径下的资产后需要用新路径或新批次目录才能重跑已接纳作业。

派发前要求 loadavg1 不高于 `start_load_max`，且所选 GPU 没有其他图形或计算进程。GPU 占用通过只读 `nvidia-smi` XML 查询确认；探测失败也会等待。条件不满足时写入 `WAITING` JSON，记录状态、原因和负载，最多等待 `start_wait_s` 秒；超时返回 `75`，不开飞。门控不终止任何外部进程。单轮模式取得 GPU UUID 协作锁后还会复查，避免批次预检与实际开飞之间的空隙。

跑中从派发开始监测负载，并持续订阅 `/world/default/stats`，直到本轮飞行结束。RTF 与 `sample_stats` 使用相同的 `Δsim/Δreal` 差分指标，在最近 `rtf_window_s` 秒内取中位数。RTF 低于 `abort_rtf` 连续 `abort_duration_s` 秒时标记 `voided_reason=rtf_drop`；负载高于 `abort_load_max` 连续同样时长时标记 `voided_reason=load_spike`。两项持续计时独立，恢复正常会重置；缺失或过期 stats 不当作 RTF 为零，也不延续低 RTF 计时。负载监控没有预热等待。

作废后立即停止本轮拥有的飞行/采样进程和按唯一尝试前缀确认的容器，容器操作使用固定 ID；仅删除本轮 `flight/episodes/`，保留 `guard.json`、结果和诊断日志。作业带退避时间回到队尾，其他已就绪作业可以先执行。默认最多首次尝试加 3 次重排，等待依次为 30、60、120 秒。每次尝试的 `results.jsonl` 记录 `start_load`、`max_load`、`voided_reason`、`requeue_count`、`retry_exhausted` 和 `next_retry_at`，便于区分低质量轮次、重排和重试耗尽。

派发前若批次目录所在磁盘可用空间低于 **100 GB**（十进制，`100000000000` 字节），写入 `PAUSED`、停止派发并返回 `75`。单次尝试的 `max_artifact_bytes` 在轮次结束后检查，超限的尝试不予接纳。容量检查不是写入时的硬配额，单轮仍可能写出超过上限的数据。所有请求作业都已接纳时返回 `0`；本次尝试有拒收时返回 `1`。

矩阵单轮模式按 GPU UUID 租用协作锁，默认锁目录为 `/data/scratch/multidetect-rtf-gpu-locks`；其他不使用该锁的进程仍由矩阵预检排查。长航程 runner 沿用现有 SITL slot 租约。

直接使用单轮模式时，可通过环境变量配置相同门控和监控阈值：

```bash
RTF_MATRIX_SINGLE_ROUND=1 \
RTF_MATRIX_START_LOAD_MAX=60 \
RTF_MATRIX_START_WAIT_S=3600 \
RTF_MATRIX_POLL_INTERVAL_S=2 \
RTF_MATRIX_ABORT_RTF=0.8 \
RTF_MATRIX_ABORT_LOAD_MAX=120 \
RTF_MATRIX_ABORT_DURATION_S=90 \
RTF_MATRIX_RTF_WINDOW_S=30 \
scripts/realworld/rtf_matrix.sh --manifest ONE_ROW.json --out ATTEMPT_DIR
```

`RTF_MATRIX_START_WAIT_S` 未设置时沿用 `RTF_MATRIX_WAIT_S`。单轮自身不负责重排：门控超时返回 `75`，跑中作废返回 `76`，由批次调度器记录和重排。原有非单轮矩阵模式保留其既有预检和重试方式。

```text
<batch>/
  manifest.json                     展开的批次 manifest
  accepted.jsonl                    已接纳的作业
  results.jsonl                     尝试结果，包括失败
  PAUSED                            达到暂停条件时写入
  WAITING                           门控或退避等待状态及原因
  .lock                             批次执行锁
  jobs/<job_id>/
    attempt-N/
      manifest.json                 本次尝试的矩阵 manifest
      scenario/                     本次尝试使用的暂存场景
      matrix.stdout.log             矩阵执行日志
      result.json                   矩阵逐轮结果（若已生成）
      batch-result.json             外层判定及中断恢复检查点
      stats.json                    运行统计（若已生成）
      guard.json                    整轮负载/RTF 监控摘要（若已生成）
      flight/                       飞行与采集产物（若已生成）
```

失败尝试删除 `flight/episodes/`，保留诊断日志和结果。只有矩阵 `valid_round: true`、路线完成、RTF 和距离达标、episode 完成且有 RGB 帧、采样与采集容器 ID 一致、产物未超限的轮次才会写入 `accepted.jsonl`。记录包括 world 与 V9 配置哈希、代码版本、GPU/slot、RTF 中位数与整窗 `Δsim/Δreal`、RGB 帧数及产物字节数。批次接纳不等同于现场运行验收；外层不独立验证实测 AGL、净空、6 秒采集节奏、巡航窗口覆盖或预期覆盖范围。

门控与重排可用假负载、假 RTF 序列和临时目录离线验证；这些单元测试不启动 Docker/GPU，也不替代真实飞行环境中的运行验收。
