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

默认使用 `urban_inspection_multirotor_001.sdf` 和 `x500_mono_cam`。常用选项：

- `--world <名称|路径>`：`simulation/gazebo/worlds` 下的 SDF 文件名，或仓库内路径；
- `--airframe <名称>`：`x500_mono_cam`、`standard_vtol`、`rc_cessna_runway` 等已有 profile；
- `--gcs-port 14560`：仅本次会话发布 GCS UDP 端口。Windows 上 14550 被排除，验收脚本仍禁止使用 14550；
- `--viewer-port 6080`：noVNC HTTP 端口；
- `--build-viewer`：强制重建一次性派生 viewer 镜像，正常启动会复用缓存；
- `--onboard-task "..."`：启动后执行仓库的 onboard policy-chain smoke，并把任务文本与结果写入 `--onboard-out`；完整 live episode 还需要相机设备和签名 mission package，当前命令不会伪造这两项；
- `--viewer novnc`：浏览器 viewer（默认）。PX4 保持 `HEADLESS=1`，容器内的 GUI 客户端稍后用 `gz sim -g` 连接同一个 server；先尝试 ogre2，失败再退回 ogre。

例如：

```powershell
python scripts/run_sim_session.py --world gen_cnt2_w0000 --gcs-port 14560 `
  --onboard-task "Count the orange traffic cones in the site and report their positions."
```

启动后终端会打印世界文件、PX4 control/telemetry、GCS UDP 和浏览器 URL。
QGroundControl 或 MultiDetectGCS 连接 `127.0.0.1:14560`；手动控制仍使用
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
14560；脚本因此默认使用 14560。若本地策略或其他进程仍占用端口，用 `--gcs-port`
换一个空闲端口，并在终端看到 `PX4 heartbeat: received` 后再连接 GCS。
