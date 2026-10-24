# SIM 架次静态复盘报告

`scripts/render_episode_report.py` 读取已保存的 `steps.jsonl`、`episode.json`、任务摘要、RGB 帧和可解析的世界 truth，生成单文件 HTML。脚本只读输入；HTML、内联 SVG/JavaScript 和可选关键帧都写入 `--out` 指定的位置，不访问外网、不启动模型或模拟器。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/render_episode_report.py <episode-dir> --out <report.html>
```

输入也可以是 campaign 或 benchmark 根目录。发现多个 episode 时，会在 `--out` 目录下按架次名批量生成：

```powershell
python scripts/render_episode_report.py <campaign-or-benchmark> --out <report-directory>
python scripts/render_episode_report.py <episode-dir> --out <report.html> --no-thumbnails
python scripts/render_episode_report.py <episode-dir> --truth <world.truth.json> --match-radius-m 10 --out <report.html>
```

真值默认使用 `score_onboard_loop.truth_for_episode` 的 world ID 和场景查找规则；`--truth` 可指定单个 truth 文件。匹配半径默认 10 m，计数位置关联使用 scorer 的一对一最近邻逻辑。真值表对每个 `true_target` / `count_member` 显示是否被计入、最近 ground estimate、距离和可从记录推导的慢模型线索；线索用于人工定位，不替代正式 episode scorer。

报告包含北向朝上的轨迹和工作区域、计数真值与 ground estimate、可读到时的 SDF 物体轮廓、高度随仿真时间变化、逐步动作/慢模型/计数/补看/安全事件表、最终 mission 摘要及代码版本和配置 SHA-256。RGB 缩略图默认最多选取开头、中段和末尾关键帧；关闭缩略图不会影响其余报告。若 SDF 路径已失效，会省略障碍物轮廓但保留架次复盘内容。

示意截图位置：打开生成的 HTML 后，页面首屏是最终摘要和俯视轨迹；继续向下可见高度曲线与决策时间线，底部是真值对比和关键帧。要留存截图，可用浏览器“打印为 PDF”或系统截图工具；HTML 本身不依赖浏览器扩展。

输出路径示例（样例放在 worktree 外 scratch，避免把大报告加入提交）：

```text
D:/Projects/mdwt/_scratch/cx-episode-viewer/hydrant-missed.html
```

定向测试：

```powershell
python -m pytest -q -p no:warnings tests/test_render_episode_report.py
```
