# 交互式 Gazebo 会话

`scripts/run_sim_session.py` 启动一个由当前命令独占的 PX4 SITL + Gazebo
容器。Gazebo 客户端通过 Docker Desktop 的 WSLg 显示在 Windows 桌面；按
`Ctrl+C` 时只停止并删除这个容器。

## 启动

在仓库根目录运行：

```powershell
$env:PYTHONPATH="src;."
python scripts/run_sim_session.py
```

默认使用 `urban_inspection_multirotor_001.sdf` 和 `x500_mono_cam`。常用选项：

- `--world <名称|路径>`：`simulation/gazebo/worlds` 下的 SDF 文件名，或仓库内路径；
- `--airframe <名称>`：`x500_mono_cam`、`standard_vtol`、`rc_cessna_runway` 等已有 profile；
- `--gcs-port 14550`：仅本次会话发布 GCS UDP 端口。验收脚本仍禁止使用 14550；
- `--onboard-task "..."`：启动后执行仓库的 onboard policy-chain smoke，并把任务文本与结果写入 `--onboard-out`；完整 live episode 还需要相机设备和签名 mission package，当前命令不会伪造这两项；
- `--viewer wslg`：WSLg 桌面显示（默认）。`novnc` 目前明确报错，因为镜像没有预装无头 VNC 层。

例如：

```powershell
python scripts/run_sim_session.py --world gen_cnt2_w0000 --gcs-port 14550 `
  --onboard-task "Count the orange traffic cones in the site and report their positions."
```

启动后终端会打印世界文件、PX4 control/telemetry、GCS UDP 和 WSLg 环境变量。
QGroundControl 或 MultiDetectGCS 连接 `127.0.0.1:14550`；手动控制仍使用
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
用 `docker exec <name> ps -ef` 确认 `px4` 和 Gazebo 客户端进程。WSLg 的 X11 socket
和 `/mnt/wslg` 会以只读方式挂入容器。Linux/WSL 中存在 `/dev/dxg` 时尝试 GPU；否则
自动设置 `LIBGL_ALWAYS_SOFTWARE=1`。

## 故障处理

若窗口没有出现，先检查 Docker Desktop 的 WSL2/WSLg 集成和 Windows 图形驱动，再确认
`DISPLAY=:0`、`WAYLAND_DISPLAY=wayland-0` 和 `/run/desktop/mnt/host/wslg` 可用。Gazebo
资源使用仓库绑定的模型和已缓存的 Fuel 资源，不会在会话开始时从 Fuel 下载。若世界加载
超时，查看 `docker logs <name>`；启动补丁已经把 PX4 等待次数从 30 秒级提高到 180 次。

`--viewer novnc` 暂未提供，因为当前 pinned 镜像没有预装 Xvfb/VNC/noVNC，运行时下载依赖
会违反离线资源约束。应先构建并固定一个派生镜像后再接入该选项。
