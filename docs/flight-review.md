# 离线飞行复盘

`scripts/render_flight_review.py` 读取一段已录制的 onboard episode，输出可直接用浏览器打开的单文件 HTML。只处理本地记录，不启动模型、GPU、模拟器或 Docker，不访问网络。生成阶段需要 Pillow。

```powershell
python scripts/render_flight_review.py <episode-directory> --out tmp/flight-review.html
```

输入目录包含 `steps.jsonl`，通常还包含 `episode.json` 和 `rgb/`。帧优先来自 `obs_before.rgb_path`；若该文件不存在，会查找 campaign 下同名的 `<episode-id>-onboard-frames/frame_0000.png` 等文件。也可以用 `--frames-dir` 指定帧目录。输入可以是只含一个 episode 的父目录；含多个 episode 时会列出候选目录供选择。

任务的区域和路线若没有保存在 episode 中，可以显式提供原始配置：

```powershell
python scripts/render_flight_review.py <episode-directory> `
  --config configs/onboard/sim-mission-from-text-monitor-backpack-v2r8multi.json `
  --out tmp/monitor-flight-review.html
```

支持配置中的 `refs`：`zone:*` 为 `[north, east, radius]`，`route:*` 为 `[[north, east], ...]`。默认输出到当前工作树的 `tmp/<episode-id>-flight-review.html`。

页面包含俯视 SVG、步骤时间线、慢模型判定及置信度、执行器步骤切换和结果、告警、发现位置、计数、计划以及最终报告。展开一个步骤，点击慢模型调用即可查看缩小的 JPEG 帧、原始坐标系下的焦点框，以及包含少量周边区域的裁剪图。相同帧只嵌入一次。

真值默认复用 `score_onboard_loop.truth_for_episode` 的 world ID 和场景目录解析规则；可通过 `--truth <file>` 或 `--truth-dir <directory>` 覆盖。地面投影复用 `replay_alert_votes` 的相机投影，最终评分复用 `score_onboard_loop.score_episode`。缺少图像或真值时会保留其余内容。

图上的真值标记是慢模型肯定票与最近真值对象的**地面位置关联**，默认半径 10 米（`--match-radius-m` 可调）。任务类别命中标为“已匹配”，类别不符标为“误报”，未命中的任务对象标为“漏报”，其余对象标为“干扰物”。这类空间关联不等于正式的 find 成功判定；最终报告中的 scorer 还会检查确认帧的可见框重叠等条件。

HTML 默认硬上限为 15 MiB（`--max-html-mb`），缩小和压缩图像后仍超限时会减少内嵌图片，保留调用记录。若仅文本记录本身已经超限，则报错，不写出超限文件。HTML 的样式、脚本、SVG 和 JPEG 均内嵌，不依赖 CDN 或外部资源。

验证命令：

```powershell
python -m pytest -q -p no:warnings tests/test_render_flight_review.py tests/test_score_onboard_loop.py
python -m ruff check scripts/render_flight_review.py tests/test_render_flight_review.py
```

测试会在工作树 `tmp/` 下创建并清理小型合成 episode；如果本机有 Node，还会执行页面内联脚本并模拟点击，检查帧、裁剪图与 SVG 焦点框。
