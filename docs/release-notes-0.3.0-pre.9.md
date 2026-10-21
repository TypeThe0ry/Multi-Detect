# 0.3.0-pre.9 发布说明

## 1. 一句话总结与状态

pre.9 把机上身份模型、计数与定位、固定翼/eVTOL 作业、地面站签名、Orin 上板入口和 SITL 验收工具链收进同一候选版本，重点是让仿真闭环可复核、失败原因可追踪。

这是**预发布候选**，不是最终 0.3.0。发布门报告为 `release_allowed=false`：仍需所有者完成不锁屏桌面上的 PX4/Gazebo/QGC 联合验收、Orin Nano 实测、HIL/真实飞行和 QGC 人工目视；仿真数字不能代替这些验收。当前地面站签名构建还必须使用 MSVC 版 OpenSSL，不能用 MSYS2 MinGW 版。

## 2. 新能力

### 机上 AI 与身份模型 r18

- 默认身份模型换为 r18（r17 修复数据加全量回放 r12）；真实飞行集身份准确率 92.2%，干扰物拒绝 97.4%，闭环找目标成功率与 r12 相同且少一次误确认。
- 默认开启步间三角化，`ground_height_sigma_m=5.0`；地面模型故意偏高 2 m 时，闭环位置误差中位数由 3.8 m 降至 2.0 m，平均误差由 4.1 m 降至 2.8 m。
- 搜索结束前会回访跟丢的“值得靠近”位置（r18 加回访的数锥桶两次均为 3/3）；计数收尾前还会对只在远处、小框下被否定过的位置补看一次（有次数和预算上限，尚待更多 SITL 架次确认）。

### 计数与搜索

- 完成一整轮覆盖后以 `coverage_complete` 收尾并返航，不再默认耗尽步数预算；已计数位置按位置去重，避免新航迹编号反复接近。
- 计数只有在覆盖完成且没有待确认目标时才是完整数量；`budget_exhausted`、`max_count` 或仍有待回访目标时，摘要中的 `count_is_lower_bound` 为 true。
- 同框目标只有在两条轨迹之后都未移开且框不嵌套时才作为两个目标证据；搜索航向偏差超过 5° 会按实测航向重新同步，障碍余量圈允许先爬升或远离后恢复搜索。

### 定位

- `ground_extent_correction` 默认开启：有任务典型尺寸的大目标，单视地面点沿底部射线推向预期中心；行人、标志、电线杆、锥桶等不平移，多视角融合不重复平移。
- 2026-10-20 SITL 中，w0031 消防车误差由 5.8--7.3 m 降至 3.2 m / 2.7 m；关联仍使用原始地面锚点。

### 固定翼

- 固定翼搜索使用按相机覆盖和转弯半径规划的割草机航线；候选接近改为目标边缘过顶盘旋，确认后直接交给 PX4 RTL 返航降落。
- 固定翼过顶单次确认要求同一位置跨航迹的真实确认票多于拒绝票；否则需要再次确认。固定翼与 VTOL 的坐标命令统一由 PX4 home 换算为 WGS84 经纬度和海拔。
- SITL 已覆盖 `advanced_plane`、`rc_cessna` 滑跑/手抛/弹射；手抛和弹射仍是 Gazebo 力脉冲近似，不能当作真实物理手抛证明。

### eVTOL：4+1、尾座式与混合搜索

- 能悬停的 VTOL 默认 `vtol_hover_ops: true`：4+1 和尾座式在多旋翼模式完成搜索、接近、确认，再由 PX4 RTL 垂直降落。
- 可选 `vtol_search_mode: "fw"`：固定翼割草机搜索，发现候选后回切多旋翼接近/确认；候选被拒后至少驻留 3 步，再按条件回到原固定翼航线。每个任务最多回切 3 次，默认仍为 `"mc"`。
- 2026-10-20 SITL 位置误差：standard_vtol（4+1）3.1 m、quadtailsitter（尾座式）4.8 m；混合搜索 w0016 救护车 1.7 m，被拒后回固定翼版本 0.94 m，均着陆。

### 地面站签名

- 任务包在真实/飞行/HIL 运行时要求 Ed25519 签名。签名自测已通过；构建脚本支持 `-OpenSSLRoot` 或 `MULTIDETECT_OPENSSL_ROOT`，便携包带 `libcrypto-3-x64.dll`。
- 必须使用 MSVC 版 OpenSSL；MSYS2 MinGW OpenSSL 不可替代。签名自测未通过的构建不能上传签名任务。

### Orin 上板

- 新增 SSH 公钥部署、安装、benchmark 和结果回收的一键入口，默认使用 r18 并开启步间三角化；结果归档到 `artifacts/orin/<UTC时间>/`。
- 上板清单固定了 ONNX/GGUF/mmproj/Depth Anything 文件的 hash、JetPack/TensorRT 自检、engine 构建、r18/r14 切换和 tegrastats 归档要求。

### 仿真与验收工具链

- 发布流水线已覆盖版本/parity、静态检查、仿真安全门、Docker 仿真验收、Windows 地面站构建和签名自测；SIM 任务套件完成 15 类全量回归。
- Windows 下 llama-server 绑定 Job Object，父进程退出自动清理；启动前只清理本仓库 artifacts 模型的孤儿进程。SITL 原语超时改按 PX4 仿真时钟计算，并保留失败时的容器日志。
- 修复仿真磁力计 roll/pitch 对调和 GNSS/空速延迟失配；同一固定翼航线航向误差由 +1.9°（p95 6.3°）降至 -0.1°（p95 1.1°）。

## 3. 修复的重要问题

- `c53c390`：修复 llama-server 孤儿检查的 PowerShell 拼接错误，避免策略启动即 `KeyError`。
- `26417d8`：SIM 地面事件时间线把当前位置传给监督器，避免安全类任务因缺返航几何而提前返航。
- `258aa98`：修复带监督器计数日志的集合和 dataclass JSON 编码崩溃。
- `3a636ed`：慢模型没有给出判断时，任务日志记录 `perception_error` 及原因，便于区分服务端故障和答案解析失败。
- `2c56f70`：搜索被障碍中断前回访已判定值得靠近但跟丢的目标。
- `8977db1`：原语超时按 PX4 仿真时钟计算，并容忍健康悬停标签缺少检测数。
- `17807a8`：最近视角截断时后退取景；搜索按实测航向重同步，避免飞出区域。
- `f19d95c`、`6ab8e7b`：计数按已计数位置过滤新轨迹，远处重见先按位置去重。
- `8af6dcf`：修复 VTOL 回切约束、转换中重复重发、墙钟截断悬停和缺时间戳时仿真时钟停走。
- `aba95e1`：把 Gazebo 模型的 Fuel 在线贴图改为本地贴图，避免网络慢导致 PX4 等不到世界。
- `a44ae78`、`1edb294`：修复仿真磁力计和传感器延迟失配；`9b62c10`：修复固定翼过顶接近和 RTL 绕圈。
- `8fe6329`：发现 Windows 地面站注册表设置下锁文件目录不存在导致指令序号取不到；签名上传仍需带签名的 MSVC 构建。

## 4. SITL 实测结果

### 固定翼 / VTOL 全机型回归（2026-10-18）

| 机型 | 起飞方式 | 盘旋空速（m/s） | 离 home 最远（m） |
|---|---|---:|---:|
| `advanced_plane` | 滑跑 | 19.7 / 17.8 | 336 |
| `rc_cessna` | 滑跑 | 13.6 / 12.1 | 329 |
| `rc_cessna` | 手抛（力脉冲近似） | 14.0 / 11.5 | 331 |
| `rc_cessna` | 弹射（力脉冲近似） | 12.7 / 10.9 | 332 |
| `standard_vtol`（4+1） | VTOL | 20.5 / 18.4 | 142 |
| `quadtailsitter` | VTOL | 22.1 / 18.6 | 125 |
| `tiltrotor` | VTOL | 23.9 / 22.3 | 128 |

### 有目标定位与混合搜索（2026-10-20）

| 场景 | 位置误差 |
|---|---:|
| 固定翼 w0016 救护车（两架） | 1.7 m、6.4 m |
| 固定翼 w0031 消防车（单视大目标修正后） | 3.2 m、2.7 m |
| standard_vtol（4+1） | 3.1 m |
| quadtailsitter（尾座式） | 4.8 m |
| standard_vtol 混合搜索，w0016 救护车 | 1.7 m；被拒后回固定翼版本 0.94 m |

### SIM 任务套件回归（2026-10-20）

| 任务 | 结果 |
|---|---|
| 数锥桶 | 3/3，0 误报，第 92 步覆盖完成 |
| 数消防栓 | 2/3，0 误报 |
| 监视背包 | 找到 2/3，0 误报 |
| 沿围栏巡逻背包 | 完成 |
| 检查邮筒 | 完成 |
| 找 SUV 并降落 | 完成 |
| 测绘覆盖 | 覆盖率 1.0 |
| 无通信计数 | 3/3，0 误报，回家 |
| 无通信巡逻超时 | 静默/暂停超时后返航 |
| 高风险监视断链 | 按断链策略返航 |
| 人工中止 | 第 120 s 中止后返航 |
| 人工暂停/恢复 | 恢复后继续执行 |
| 电量预留返航 | 剩余时间到预留线返航 |

全量回归每类只跑 1 架次；数消防栓漏掉的目标只在 17 m 外看到一次，被 r18 判为“不是”。

## 5. 已知限制

- 所有固定翼、eVTOL、定位和任务套件数字均为 PX4/Gazebo SITL，不是 HIL、实飞、真实相机或真实降落区保证。
- 尾座式悬停相机光轴抬高约 20°；30 m 高度主要看到约 75 m 以外地面，当前应先远后近，根本改善需要云台或下视相机。
- 远处小目标可能一次被 r18 否定；小目标复查开关 `small_reject_recheck_px` 测试无收益，保持关闭。关键任务仍应跑 2--3 架次确认随机性。
- 连续视频 RGB-SLAM/视觉惯性测距尚未接入机上；近距深度也未成为默认能力。
- Orin 尚无实测基准；`deliver` 需要单独授权，当前不要用于真实投递。
- `vtol_search_mode: "fw"` 只适用于能悬停的 VTOL；转换期间不推进机上 AI 循环。

## 6. 需要所有者完成的验收

### 联合 PX4/Gazebo/QGC

在不锁屏、没人操作的桌面上，于主仓库执行：

```powershell
python scripts/run_release_pre9.py --only-stage combined_px4_gazebo_qgc_acceptance --run-dir artifacts/release-pre9-runs/20261020-final
```

### Orin bring-up

开发机公钥必须已写入板上 `~/.ssh/authorized_keys`，板上用户必须能免密 `sudo -n`。拿到 IP 和用户名后执行：

```powershell
$env:PYTHONPATH="src;."; python scripts/bringup_orin.py --host <板子IP> --user <用户名> --key $HOME/.ssh/id_ed25519
```

只检查命令、不连接板子：

```powershell
$env:PYTHONPATH="src;."; python scripts/bringup_orin.py --host <板子IP> --user <用户名> --key $HOME/.ssh/id_ed25519 --dry-run
```

### HIL / 真实飞行 / QGC 人工目视

发布门没有用仿真替代这一步。所有者需在真实设备上按现场批准的 HIL/实飞流程完成起飞、链路、返航、降落和 QGC 目视检查，并保存任务日志、QGC 观察结果和失败原因；完成前 `release_allowed` 保持 `false`。固定翼/eVTOL 的 SITL 冒烟命令只能作为设备前的流程参考，不能宣称 HIL 或实飞通过。

## 7. 升级与使用注意

- `vtol_hover_ops`：能悬停 VTOL 默认 `true`，搜索、接近、确认留在 MC；设为 `false` 才回到旧的固定翼作业方式。
- `vtol_search_mode`：默认 `"mc"`，行为不变；设为 `"fw"` 才启用固定翼搜索、MC 接近确认和最多 3 次回切。`vtol_fw_reentry_min_mc_steps` 默认驻留 3 步，`vtol_fw_transition_budget` 控制回切次数。
- `ground_extent_correction`：默认开启；只修正有典型尺寸的大目标，关联锚点和多视角融合规则不变。
- 计数覆盖完成：看到 `reason=coverage_complete` 且没有待回访目标时，数量不是下限；看到 `budget_exhausted`、`max_count` 或 `count_is_lower_bound=true` 时按下限读取。
- 计数二次观察：计数收尾前会对仅远处/小框被拒的位置做有预算上限的回访；`count_rejection_revisit_attempts` 和 `count_rejection_revisit_range_m` 控制回访次数与范围。`small_reject_recheck_px` 仍默认关闭。
- 仿真专用 PX4 参数：仿真使用 `SENS_EN_MAGSIM=1`、`SENS_GPS0_DELAY=0`、`EKF2_ASP_DELAY=0`，并移除 Gazebo 磁力计系统；这些是仿真修复，不要直接当作真机延迟配置。
- 固定翼无测距仪降落需 `FW_LND_USETER=0`、`FW_LND_ABORT=0`；真机仍须按实测设备和飞行安全流程确认参数。
- 地面站签名构建必须保留 `libcrypto-3-x64.dll`，并用 MSVC OpenSSL 自测通过后再上传签名任务。
