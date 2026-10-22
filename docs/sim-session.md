# 交互式 Gazebo 会话

`scripts/run_sim_session.py` 启动一个由当前命令独占的 PX4 SITL + Gazebo
容器。当前默认 viewer 是容器内的 Xvfb + x11vnc + noVNC，浏览器访问
`http://127.0.0.1:6080/vnc.html?autoconnect=1`。按 `Ctrl+C` 时只停止并删除这个容器。

## 启动

在仓库根目录运行：

```powershell
$env:PYTHONPATH="src;."
python scripts/run_sim_session.py
```

默认使用 `urban_inspection_multirotor_001.sdf` 和 `x500_mono_cam`。`--airframe` 会同时选择
PX4 runtime、Gazebo 模型、默认世界和 onboard collector profile；显式 `--world` 时只替换
世界文件，生成的 `gen_v2_w0016/w0031` 会按机型选择对应的固定翼或 VTOL scenario。

| airframe | 默认世界 | 起飞方法 | session runtime/scenario |
| --- | --- | --- | --- |
| `x500` / `x500_mono_cam` | `urban_inspection_multirotor_001.sdf` | PX4 默认多旋翼 | `urban_inspection_multirotor_001` |
| `standard_vtol` | `river_rescue_vtol_001.sdf` | `vtol` | 4+1 `standard_vtol_mono_cam` |
| `quadtailsitter` | `quadtailsitter_mono_cam_001.sdf` | `vtol` | `quadtailsitter_mono_cam` |
| `tiltrotor` | `tiltrotor_mono_cam_001.sdf` | `vtol` | `tiltrotor_mono_cam` |
| `rc_cessna` | `coastal_patrol_fixed_wing_001.sdf` | `runway` | 固定翼 `rc_cessna_mono_cam` |

固定翼的 `--takeoff-method hand` / `catapult` 会透传到 collector，并选择对应的 PX4
launch profile。VTOL onboard 配置中的 `vtol_search_mode`（`mc` 或 `fw`）以及
`vtol_fw_reentry_min_mc_steps`、`vtol_fw_transition_budget`、
`vtol_fw_reentry_candidate_max_steps` 会原样保留；需要混合搜索时在 defaults JSON
中设置 `vtol_search_mode: "fw"`。

五种机型的可复制启动命令：

```powershell
$env:PYTHONPATH="src;."
python scripts/run_sim_session.py --airframe x500 --gcs-port 14560
python scripts/run_sim_session.py --airframe standard_vtol --gcs-port 14562
python scripts/run_sim_session.py --airframe quadtailsitter --gcs-port 14564
python scripts/run_sim_session.py --airframe tiltrotor --gcs-port 14566
python scripts/run_sim_session.py --airframe rc_cessna --gcs-port 14568
```

按文档中已验证的生成场景运行固定翼和 VTOL：

```powershell
python scripts/run_sim_session.py --airframe rc_cessna --world gen_v2_w0031 --gcs-port 14568 `
  --onboard-task "Find the red fire truck and return home."
python scripts/run_sim_session.py --airframe standard_vtol --world gen_v2_w0016 --gcs-port 14562 `
  --onboard-task "Find the ambulance and return home."
python scripts/run_sim_session.py --airframe quadtailsitter --world gen_v2_w0031 --gcs-port 14564 `
  --onboard-task "Find the red fire truck and return home."
```

GCS prompt mission 使用同一 session，只需追加 `--operator-link`（可同时指定
`--onboard-task` 作为本地任务文本兼容入口）：

```powershell
python scripts/run_sim_session.py --airframe standard_vtol --operator-link --gcs-port 14562
```

常用选项：

- `--world <名称|路径>`：`simulation/gazebo/worlds` 下的 SDF 文件名，或仓库内路径；
- `--airframe <名称>`：`x500_mono_cam`、`standard_vtol`、`rc_cessna_runway` 等已有 profile；
- `--gcs-port 14560`：PX4 向宿主 `host.docker.internal:<port>` 主动发送 GCS MAVLink；端口不发布到 Docker，避免 Docker proxy 占用宿主 socket；
- `--viewer-port 6080`：noVNC HTTP 端口；
- `--build-viewer`：强制重建一次性派生 viewer 镜像，正常启动会复用缓存；
- `--onboard-task "..."`：启动后执行仓库的 onboard policy-chain smoke，并把任务文本与结果写入 `--onboard-out`；完整 live episode 还需要相机设备和签名 mission package，当前命令不会伪造这两项；
- `--exit-after-task`：任务成功或失败后清理容器并返回任务 exit code；
- `--snapshot-dir DIR`、`--snapshot-interval S`：把容器 `DISPLAY=:99` 截图复制到目录，默认间隔 10 秒；世界 ready 和任务结束各额外保存一张；
- `--check-heartbeat S`：在宿主绑定 GCS UDP 端口等待 HEARTBEAT，并写入 `heartbeat.json`；
- `--session-log FILE`：写入 JSON Lines 生命周期日志；
- `--viewer novnc`：浏览器 viewer（默认）。PX4 保持 `HEADLESS=1`，容器内的 GUI 客户端稍后用 `gz sim -g` 连接同一个 server；先尝试 ogre2，失败再退回 ogre。

例如：

```powershell
python scripts/run_sim_session.py --world gen_cnt2_w0000 --gcs-port 14560 `
  --onboard-task "Count the orange traffic cones in the site and report their positions."
```

启动后终端会打印世界文件、PX4 control/telemetry、GCS UDP 和浏览器 URL。
QGroundControl 或 MultiDetectGCS 绑定/连接 `127.0.0.1:14560`；手动控制仍使用
`127.0.0.1:14540`。

## 锁与验证

所有 Docker 操作都必须在共享 SITL 锁内执行（Git Bash）：

```bash
. /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire simgui
python scripts/run_sim_session.py ...
status=$?
sitl_release
exit $status
```

可用 `docker ps --filter label=multidetect.purpose=simulator-docker` 查看自己启动的容器，
用 `docker exec <name> ps -ef` 确认 `px4`、`Xvfb`、`x11vnc`、`websockify` 和 `gz sim -g`。
容器内用 `import -display :99 -window root /tmp/view.png` 截图，再通过 `docker cp`
复制到 scratch；图像应为非纯色且能看到场景/飞行器。

## 故障处理

若浏览器返回 404，先确认 `docker build` 已成功并检查 `docker logs <name>`。Gazebo
资源使用仓库绑定的模型和已缓存的 Fuel 资源，不会在会话开始时从 Fuel 下载。若世界加载
超时，查看 `docker logs <name>`；启动补丁已经把 PX4 等待次数从 30 秒级提高到 180 次。

本机实测 docker-desktop 没有 Ubuntu/WSLg；因此 WSLg 路径不再使用。浏览器 viewer 派生镜像
将 apt 安装固定在构建层，运行时不会下载包。

当前 Windows `netsh int ipv4 show excludedportrange protocol=udp` 输出没有覆盖
14560；脚本因此默认使用 14560。启动前会 fail-fast 检查
`artifacts/cache/gz-fuel/resolved` 和 onboard 所需的
`artifacts/cache/llama-cpp/b11552/llama-server.exe`，避免缺缓存时 Gazebo 无限等待。

## 本机实测结果（2026-10-22）

派生镜像已成功构建并缓存为 `multidetect/px4-gazebo-viewer:bcf8278e3321`
（本地 image digest 以 Docker inspect 为准）。rich world
`urban_inspection_multirotor_001.sdf` 的容器运行、noVNC HTTP `200` 和 X display
截图均通过；截图为非均匀图像（`rich.png`，131504 bytes，RGB channel spread 765）。
证据在 `D:/Projects/mdwt/_scratch/simgui/shots/rich.png` 和
`D:/Projects/mdwt/_scratch/simgui/rich-docker.log`。

`gen_cnt2_w0000.sdf` 的 Fuel cache 和 `llama-server` 现在由 worktree 的 `artifacts`
junction 提供。启动器会在缺少任一资源时立即报错，不再让 PX4 无限等待；缓存存在时应
继续用 `shots/count-late.png`、`count-late-docker.log` 做回归对比。

GCS 端口不再通过 Docker publish，占用问题已修复：PX4 额外 MAVLink instance 主动发送
到 `host.docker.internal:<gcs-port>`，宿主 pymavlink/QGC 绑定 `0.0.0.0:<gcs-port>`。
本轮重跑因共享 SITL 锁被 `relook3` lane 占用而中断，尚未声称新的 HEARTBEAT 或 live
onboard mission 成功；此前失败的 `onboard2/stderr.log` 保留作缺少 llama-server 的历史证据。
