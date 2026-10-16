# r15 Fuel 真实外观采集计划

本批使用 `expert_search` teacher，20 个独立世界（16 train、4 val），每世界 4 episodes，共 80 episodes。所有模型来自只读 `artifacts/cache/gz-fuel/resolved` 的 CC0 Fuel 网格；不复用任何 test split 或 `gen_cnt2_*` 摆位。

## 世界与目标

| 世界 | 目标 | 成对难反例 | 外观 | split |
|---|---|---|---|---|
| gen_r15_w0000–w0007 | backpack | electrical_box、cardboard_box、standing/walking_person | 六种预设循环 | train |
| gen_r15_w0008–w0009 | construction_cone | fire_hydrant、mailbox | 六种预设循环 | train |
| gen_r15_w0010–w0011 | mailbox | fire_hydrant/electrical_box 或 cardboard_box | 六种预设循环 | train |
| gen_r15_w0012–w0013 | fire_hydrant | construction_cone、mailbox | 六种预设循环 | train |
| gen_r15_w0014 | suv | pickup（留出） | evening_low_sun | train |
| gen_r15_w0015 | jersey_barrier | stop_sign | haze | train |
| gen_r15_w0016–w0017 | backpack | electrical_box、cardboard_box、standing/walking_person | 六种预设循环 | val |
| gen_r15_w0018 | construction_cone | fire_hydrant、mailbox | noon_clear | val |
| gen_r15_w0019 | mailbox | electrical_box、cardboard_box | morning_low_sun | val |

`pickup`、`postbox`、`stop_sign` 是当前 catalog/生成器的 test 留出目标类；它们不作为 r15 真目标。`pickup` 在 w0014 只作为 SUV 的 forced distractor。`suv`、`standing_person`、`walking_person` 在当前规则中可作训练目标或干扰物；本批仅使用 SUV 目标和行人干扰物。`jersey_barrier` 是 train 类，可作为目标。所有世界均带 segmentation label，truth 中明确记录 confuser/forced_distractor。

## 小框到近景航线

前置相机标定为 1280×960、`fx=fy=539.936 px`。背包短边 0.263615 m，近似 `px_short = fx·S/D_slant`：12 px 对应斜距 11.9 m，16 px 为 8.9 m，24 px 为 5.9 m，32 px 为 4.5 m。每 episode 从目标斜距约 14–16 m（短边 <12 px）开始，沿目标径向悬停/平移，再经过 9、6、4.5 m（约 16、24、32 px）连续采集；末段保持 4 m 以内 80 帧。相机安装前伸 0.18 m、下偏 0.12 m，按 `front-camera-v1` 外参修正水平距离。

非背包目标按相同公式换算短边：mailbox 0.133 m（6.0→2.2 m）、fire_hydrant 0.424 m（19.1→7.1 m）、construction_cone 0.503 m（22.6→8.5 m）、suv 2.604 m（117.0→43.9 m）。起飞高度采用 18 m，先在目标斜距约 14–16 m（背包短边小于 12 px）建立远景，再下降/沿径向逼近至 9、6、4.5 m；末段保持 4 m 以内 80 帧，保证连续远→近观测。18 m 是起飞高度，不把 18 m 的垂直高度误当作目标斜距；teacher 航线需要下降到 4 m 级别。

## 采样预算

每 episode 约 560 帧（约 28 s、20 FPS），每世界约 2240 帧；总计约 44,800 帧。RGB+深度+分割和元数据按约 300 KiB/帧预留 15 GiB 磁盘。teacher 只用 `expert_search`，不启用 onboard 推理。

## 仿真恢复后的命令

```powershell
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\r15worlds\.tmp"
$env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONPATH="src"
python scripts/generate_r15_worlds.py
python scripts/validate_r15_worlds.py
python scripts/collect_sim_teacher_episodes.py --scenario configs/scenarios/generated-r15/gen_r15_w0000.scenario.json --teacher expert_search --episodes 4 --steps-per-episode 560 --rgb-size 1280x960 --depth-size 1280x960 --depth-format png16 --onboard-frame-format jpeg --onboard-frame-quality 90 --seed 20261016 --out artifacts/r15-episodes/gen_r15_w0000
# 对 w0001..w0019 重复上行命令，仅替换 scenario 与 out
```

Docker/Gazebo 当前不可用；上述命令仅在仿真环境恢复后执行。
