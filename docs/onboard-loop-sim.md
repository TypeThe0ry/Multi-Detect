# SIM 机载自主搜索闭环

`multidetect.autonomy.onboard_loop` 是纯逻辑核心：YOLOE 只提供候选框，tracker 保持稳定 ID；慢模型按新轨迹、尺寸变化和定时重检触发，单飞行中请求、带 TTL 和每分钟预算。身份匹配后由像素尺寸 gate 决定确认，输出 bounded translate/hold/stop primitive；超时进入 return。

`scripts/run_onboard_loop_sim.py` 是胶水入口，真实 SIM 时接现有 collector 的容器、MAVLink、frame server 和 executor；`--fake` 用于无 Docker 的 CPU smoke。loop 不读取 scenario/truth，truth 只在 episode 完成后由 campaign scorer 使用。

当前限制：深度清障尚未接入真实 frame metadata；默认使用安全高度和短腿搜索。ground position 是相机 bearing 加深度，缺深度时退化为高度平面交点。慢模型 HTTP、YOLOE 加载和 collector 生命周期保持在脚本层，便于 fake 测试。
