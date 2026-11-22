# SIM teacher 重场景内存保护

采集脚本 `scripts/collect_sim_teacher_episodes.py` 支持按 episode 保护 gz-sim Sensors（ogre2）在重场景中的增长：

- `MD_SIM_CONTAINER_MEMORY_GB`：可选的容器内存上限，设置为正数 GiB 后给每个新容器增加 `--memory=Ng` 和 `--memory-swap=Ng`。未设置时保持原行为。`MD_GIMBAL_INVOCATION` 分支继续固定使用 64g，不被该变量覆盖。
- `MD_SIM_SERVER_RSS_SOFT_LIMIT_GB`：可选的 gz sim 服务端 RSS 软上限。采集期间约每 20 秒执行一次容器内 `ps`，把服务端 RSS（MiB）、`wall_time` 和 `px4_sim_time_boot_ms` 追加到 episode 目录的 `server_rss.jsonl`。采样命令或写文件失败会写入带 `error` 的行，不影响采集。
- 当 RSS 超过软上限时，当前 episode 在已有帧之后提前结束，`episode.json` 的状态为 `aborted_server_rss`，`reason` 为 `server_rss_soft_limit`，并由主循环启动下一个 episode 的新容器；若步数已达标而只在降落等待阶段触发，则跳过降落并以 `status=complete`、`end_reason=server_rss_soft_limit` 收尾。下游构建器可用 `--include-incomplete` 纳入这些保留下来的未完成 episode。
- `--max-episode-sim-seconds N`：按 PX4 `time_boot_ms` 设置可选的仿真时长上限。达到上限后保留当前帧并结束该 episode（`status=truncated`），随后继续下一个 episode；若步数已达标而只在降落等待阶段触发，则以 `status=complete`、`end_reason=max_episode_sim_seconds` 收尾。默认不设置，原有 episode 时长行为不变。

Zurich 等重场景建议先使用 `MD_SIM_SERVER_RSS_SOFT_LIMIT_GB=40` 和 `MD_SIM_CONTAINER_MEMORY_GB=64`，例如：

```bash
MD_SIM_SERVER_RSS_SOFT_LIMIT_GB=40 \
MD_SIM_CONTAINER_MEMORY_GB=64 \
python scripts/collect_sim_teacher_episodes.py \
  --fw-cruise-observe --max-episode-sim-seconds 120 ...
```

`120` 秒只是可测的拆段示例；应结合路线覆盖和实际 `server_rss.jsonl` 斜率调整。默认不设置这些变量时，采集器行为保持不变。
