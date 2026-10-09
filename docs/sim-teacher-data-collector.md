# SIM Teacher 数据采集器

`sim_teacher` 用于在 Gazebo + PX4 SITL 中生成训练用的 `(observation, action, next observation)` 样本。采集器只面向仿真多旋翼（`x500_mono_cam`），每个 episode 使用独立容器并在退出时清理。运行阶段只写入带接收时间戳的原始流；SHA-256、完整性与统计检查由离线审计器完成。

## 快速运行

先准备 Docker、PX4/Gazebo 镜像、场景文件（`*.scenario.json`，以及关联的 `.runtime.json` / `.truth.json`）：

```powershell
C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe `
  scripts/collect_sim_teacher_episodes.py `
  --scenario configs/scenarios/urban_inspection_multirotor_001.scenario.json `
  --teacher explore --episodes 10 --steps-per-episode 100 --seed 7 `
  --out artifacts/datasets/sim-teacher/urban-explore
```

先检查参数和容器计划，不连接 Docker：

```powershell
... scripts/collect_sim_teacher_episodes.py ... --dry-run
```

采集完成后执行离线审计：

```powershell
C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe `
  scripts/audit_sim_teacher_dataset.py `
  --dataset artifacts/datasets/sim-teacher/urban-explore
```

审计器发现损坏、缺失文件、步骤不连续或 JSON 无法解析时返回非零退出码。

## Teacher 策略

* `explore` 使用固定种子生成随机 `translate`、`turn`、`hold`、`stop` 原语，并限制场景边界及最低/最高高度。
* `expert_search` 读取场景 truth 中的目标，使用相机内参做几何 FOV 判断，执行搜索、靠近 standoff 距离、保持记录再停止。每步附带任务文本（例如“找到红色车辆并记录位置”）及 `target_in_fov`、`target_bearing`、`target_range` 特权标签。

## 数据布局

```text
<out>/
  episodes/<episode_id>/
    episode.json       # episode 元数据与步数
    steps.jsonl        # 每步 obs_before、action、ACK、执行结果、obs_after
    mavlink.jsonl      # 原始 MAVLink 接收消息（逐条）
    rgb/*.png          # RGB 帧
    depth/*.npy        # float32、单位米的深度帧
  audit.json           # SHA-256 manifest 与统计
```

`steps.jsonl` 是追加写入的。观测记录包括相机信息、局部 NED 位姿、经纬高、姿态、速度、GPS 状态以及单调时钟/墙上时钟。审计报告还会记录帧与位姿时间偏差统计、动作类型直方图和成功率。时间偏差用于诊断，不会因为新鲜度不足而拒绝原始样本。

## 边界与已知限制

* 数据只代表 Gazebo/PX4 SITL；不能宣称真实 Jetson、Pixhawk 或户外飞行性能。
* 当前仅支持多旋翼 `x500_mono_cam`。真实硬件执行器、复杂相机畸变和动态目标模型尚未覆盖。
* PX4 安全围栏和 failsafe 保持启用；SIM 端点必须是 localhost，避免误连外部飞控。
* 审计器验证文件完整性和结构，不会证明策略标签或场景 truth 本身正确；请保留原始场景文件以便复现。
