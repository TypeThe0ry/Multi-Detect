# 三模式统一多目标感知与模拟任务系统

## 1. 目标与边界

本规格定义同一套 Jetson、Pixhawk 和 QGroundControl 软件如何支持巡检、灭火载荷投放
仿真以及高楼/消耗性平台接近仿真。三个模式共享视频、目标池、跟踪、测距、避障和审计，
不能实现成三套互不兼容的程序。

当前批准范围包括真实摄像头和 Jetson 推理、只读 Pixhawk 遥测、QGC 目标选择，以及
SITL/软件 HIL 建议量。以下输出继续由编译期门禁禁止：真实飞控写入、物理舵机控制、
物理载荷释放。滑动确认、人工授权或任务模式切换均不能绕过编译门禁。

当前硬件传感器基线只有一台 RGB RTSP 摄像头，不安装热成像设备。因此本阶段不存在
热像检测、RGB/热像融合或热成像热点类别。文中的“多模态测距”是 RGB 相机、Pixhawk
姿态/GPS/高度、空速、DEM 与可选激光测距之间的融合，不代表存在热成像。

### 1.1 不变量

- `MULTIDETECT_FLIGHT_CONTROL_WRITES=0` 时不得构造或发送飞控控制消息。
- `MULTIDETECT_PHYSICAL_RELEASE=0` 时不得构造或发送物理舵机/载荷释放消息。
- 模式 2 的候选列表可以显示和选择火、烟、人、车、建筑等目标，但“可选择”不等于“可投放”。
  有效灭火投放目标只能是经过独立 RGB 复核的火焰/着火区域，或与该火区形成明确空间复合
  关系的着火车辆/建筑；人员、烟雾和普通车辆必须强制禁投。
- 模式 2、模式 3 都必须先人工选择目标，再完成一次绑定目标 ID、修订号和模式的不可复用
  滑动确认。切换目标、重捕获、遮挡、失锁或超时立即使确认失效。
- 模式 3 在 SITL/HIL 中允许任意人工框选目标，包括人员和车辆，用于验证目标保持、居中和
  中止逻辑；该许可不能传播到真实飞行或执行器接口。
- 证据不足时目标必须保持 `LOST`，不能为了连续显示而绑定到相似目标。
- 模式 3 进入接近仿真后，一旦遮挡、失锁、避障风险无效或通信陈旧，必须立即进入
  `ABORT_CLIMB_SIM`；禁止只凭最后位置继续接近。

## 2. 统一感知与跟踪底座

### 2.1 感知源

1. RGB 火灾专用检测：火焰和烟雾；阴燃区域、烧毁区域需在有合格数据和独立验收后加入。
2. 通用检测：人员、车辆、建筑、道路、电线、储罐和其他经模型清单批准的类别。模式 2/3
   可显示这些类别供人工选择；模式 1 的任务告警输出仅使用火焰和烟雾。
3. 人工框选：允许初始化不在检测类别内的任意视觉目标。
4. 飞机运动：Pixhawk 姿态、角速度、航向、位置、地速和空速的只读时间同步样本。

### 2.2 跟踪架构

检测目标使用类似 BoT-SORT 的多线索关联：

- 二维恒速 Kalman 状态预测、协方差传播和归一化创新门控；
- IoU、中心距离、检测置信度和类别一致性；
- 相机运动补偿；
- 外观 ReID 特征库和指数更新；
- 高/低置信度级联；低置信度观测只能延续已有轨迹，不能独立创建新身份；
- 每一级采用确定性的矩形 Hungarian 全局最小代价分配，不使用逐候选贪心抢占；
- 遮挡期间的协方差扩张和身份冻结。

人工框选目标使用低延迟单目标跟踪器保持帧间位置，并同时建立外观模板、颜色描述子和
ReID 特征。单目标跟踪器失效后，重捕获器按以下顺序搜索：

1. Kalman 预测区域；
2. 扩大的局部区域；
3. 全画面同类别检测和外观匹配；
4. 任意目标的全画面多尺度外观匹配。

重捕获必须同时满足运动门控、外观相似度、尺度变化和连续多帧确认。多个候选相近时保持
`REACQUIRING`，不能选择分数略高但身份不明确的候选。

### 2.3 多目标池

目标记录至少包含：

- `target_id`、类别和来源；
- 当前框、预测框、速度和协方差；
- ReID/外观特征及更新时间；
- 可见、遮挡、重捕获或丢失状态；
- 跟踪质量、检测置信度和身份置信度；
- 最后可见帧、最后位置和失锁时间；
- 是否为当前主目标。

系统至少同时维持 10 条轨迹。切换主目标不能删除其他轨迹；被切换到后台的目标继续更新，
再次切回时优先使用原 `target_id`。

统一状态机：

```text
DETECTED -> LOCKED -> TRACKING -> OCCLUDED -> REACQUIRING -> RECOVERED
                                                       \-> LOST
```

## 3. 模式 1：巡检

模式 1 不挂载任务载荷，Pixhawk 执行经过批准的巡检航线，Jetson 只读感知。

- 未选择目标时持续识别火焰和烟雾，进行巡检、告警和火情多目标跟踪；通用检测器即使在后台
  运行，也不能把人员或车辆作为模式 1 的任务告警目标。
- 选择目标后进行监测和跟踪，不因视觉框自行大幅改变真实航向。
- 目标离开视野后生成 `RETURN_TO_OBSERVE_ADVISORY`，包含建议方向、所需转弯半径和证据
  新鲜度；当前阶段不发送飞控命令。
- 掉头重访只在 SITL 中验证，并受地理围栏、最低空速、最大坡度和操作者确认约束。
- MAVLink 元数据上报目标 ID、类别、框、置信度、跟踪质量、身份质量和告警。

```text
PATROL -> DETECTED -> LOCKED_MONITOR -> OCCLUDED -> REACQUIRING
       -> TRACKING / LOST -> PATROL
```

## 4. 模式 2：灭火载荷投放仿真

### 4.1 目标和安全条件

模式 2 自动识别火、烟、人、车、建筑等候选，操作者可以选择任一候选查看和跟踪。选择人员、
烟雾或普通车辆时，界面必须明确显示 `TARGET_NOT_PAYLOAD_ELIGIBLE`，不得创建投放授权。

选择火焰/着火区域时，该目标必须由同一 RGB 视频上的独立火情复核模型确认，并同时通过多帧
持续性、空间一致性和人员排除；不能把同一检测结果复制一次冒充独立证据。选择车辆或建筑时，
只有在它与一个已复核火区形成唯一、稳定的空间复合关系时，才能把该火区作为真正的灭火投放
瞄准点；车辆/建筑框本身不是落点。存在多个相近火区、关联歧义或人员进入排除区时必须拒绝。

通过目标资格检查后，系统发出一次性滑动挑战。滑动确认绑定所选目标 ID、所选目标修订号、
实际火区瞄准目标 ID/修订号、模式 `PAYLOAD_HIL` 和短时有效期。目标切换、任一目标修订变化、
遮挡、失锁、证据陈旧或超时都使确认失效。滑动确认之后仍须通过完整安全规则和独立人工授权；
它不能直接触发舵机或模拟释放。

```text
自动检测 -> 人工选择 -> 投放资格解析 -> 滑动确认 -> 火情多帧确认
        -> 测距有效 -> 落点误差估算 -> 安全规则 -> 人工授权
        -> 模拟释放 -> 结果确认
```

### 4.2 只读解算输出

- 目标距离、方位和 95% 置信区间；
- 预计落点和误差椭圆；
- `TOO_EARLY / WINDOW / TOO_LATE / INVALID` 建议；
- 每个安全规则的通过、否决或未知原因；
- 软件 HIL 模拟舵机和惰性载荷状态。

```text
LOCKED -> SAFE -> WAIT_AUTH -> SIM_ARMED -> SIM_RELEASE_REQUESTED
       -> SIM_RELEASED -> CONFIRMED / ABORTED
```

## 5. 模式 3：高楼/消耗性平台接近仿真

### 5.1 目标选择

模式 3 自动识别火、烟、人、车、建筑等候选；SITL/HIL 中任何经人工选择的目标都可作为接近
仿真对象，包括建筑、火点、人员和车辆。
目标类别必须醒目显示，且进入接近仿真前必须完成一次不可复用的滑动确认：

- 滑动必须从起点连续到终点，不能用普通点击代替；
- 确认令牌绑定目标 ID、目标修订号、模式和短时有效期；
- 切换目标、目标重捕获、遮挡、失锁或令牌超时都会使确认失效；
- 确认只允许产生 SITL/HIL 建议量，不能解除真实写入门禁。

### 5.2 接近仿真

- 计算目标中心与相机光轴的角度误差；
- 生成俯仰、航向和坡度的受限居中建议；
- 验证受地理围栏、最小高度、最大坡度和最小空速约束的接近走廊；
- 接近期间不得执行身份切换或仅凭最后位置外推；
- 遮挡、失锁、通信陈旧、测距无效或避障风险达到 `AVOID` 时立即输出
  `ABORT_CLIMB_SIM`；
- 模拟拉高建议必须受最大俯仰、失速裕度和地理围栏约束。

```text
SEARCH -> TARGET_LOCKED -> SLIDE_CONFIRM_REQUIRED -> CORRIDOR_VALID
       -> CENTERING_SIM -> APPROACH_SIM -> COMPLETE / ABORT_CLIMB_SIM
```

真实自动接近、真实拉高和执行器写入不属于当前批准阶段。

## 6. 轻量单目视觉避障

单目模块是风险估计器，不宣称提供精确绝对深度。它使用：

1. FAST/ORB 稀疏特征和金字塔 LK 光流；
2. Pixhawk 角速度/姿态对光流进行旋转去除；
3. RANSAC 估计相机运动与异常运动簇；
4. 光流发散、目标框膨胀率和焦点扩张估算碰撞时间 `TTC`；
5. 左、中、右和上方视野分区；
6. 地平线、地面纹理、低照度、眩光和低纹理有效性检查；
7. 时序滤波和迟滞，避免单帧噪声引发状态抖动。

输出契约：

- `CLEAR / CAUTION / AVOID / INVALID`；
- 各分区风险、TTC 范围和置信度；
- 数据新鲜度、有效特征数和姿态补偿状态；
- 建议规避方向，仅作为 QGC 提示或 SITL/HIL 输入。

模式 1 中避障只产生告警和规避建议。模式 3 接近仿真中，`AVOID` 或 `INVALID` 触发
`ABORT_CLIMB_SIM`。真实飞机在完成独立避障传感器和飞行验收前不能使用单目结果闭环控制。

## 7. 多模态只读测距

SLAM/VIO 只提供相对运动和局部结构，不能独立证明绝对尺度。绝对距离按以下顺序融合：

```text
相机射线 + 相机/机体外参 + Pixhawk姿态 + GPS/高度
         + DEM/地面交点 + VIO/SLAM相对运动 + 可选激光测距
```

空速用于预测飞机运动、接近时间和软件 HIL 投放窗口，不直接当作目标距离。

每个测距结果必须包含距离、方位、95% 置信区间、数据新鲜度、传感器一致性，以及
`VALID / DEGRADED / INVALID` 状态。

当前融合门禁规定：只有相机地面交点与至少一种独立绝对测距手段一致时才输出 `VALID`；
仅有相机/高度投影时输出 `DEGRADED`；两种绝对距离相互冲突时直接输出 `INVALID`。三种或
更多来源中可以剔除一个离群值，但结果继续保持 `DEGRADED`。VIO/SLAM 未证明绝对尺度时
不得参与绝对距离融合。任何姿态、画面、高度或直接测距超时，以及射线不能安全向下穿过
地面平面时，均不发布距离。

## 8. QGC 交互与元数据

QGC 需要显示：

- 三种任务模式和当前阶段；
- 主目标及后台目标列表；
- 目标框、预测框、目标 ID、类别、检测/跟踪/ReID 质量；
- 遮挡时长、重捕获范围和失锁原因；
- 距离、方位、置信区间和测距有效性；
- 单目避障各分区风险和 TTC；
- 所有安全否决原因；
- 模式 3 的滑动确认和有效期。

视频和元数据继续分离传输。目标选择、切换、取消和滑动确认使用经过认证、关联和防重放的
元数据协议；当前协议不得携带真实飞控或舵机命令。

## 9. 验收指标

| 编号 | 指标 | 目标 |
|---|---|---|
| TRK-001 | 同时维护的目标轨迹 | 至少 10 个 |
| TRK-002 | 主目标切换响应 | 不超过 200 ms |
| TRK-003 | 跟踪元数据输出 | 不低于 15 Hz |
| TRK-004 | 短时遮挡恢复 | 不超过 0.5 s |
| TRK-005 | 离开画面后正常重现的重捕获 | 不超过 2 s |
| TRK-006 | 身份不确定行为 | 保持 `REACQUIRING/LOST`，不强行切换 |
| TRK-007 | 部署域身份质量 | 通过复核身份标注报告 IDF1、ID Switch 和碎片化 |
| LAT-001 | 相机到 QGC 叠加中位延迟 | 不超过 250 ms |
| LAT-002 | 相机到 QGC 叠加 P95 延迟 | 不超过 400 ms |
| OBS-001 | 单目风险输出 | 不低于 20 Hz |
| OBS-002 | 单目模块 P95 处理时间 | 不超过 25 ms/帧 |
| OBS-003 | 低纹理、低照度或姿态陈旧 | 输出 `INVALID` |
| RUN-001 | Jetson 连续运行 | 60 分钟无持续帧积压或内存增长 |
| SAFE-001 | 模式 2 人员/车辆重叠 | 强制否决 |
| SAFE-002 | 模式 3 未滑动确认 | 不产生接近建议 |
| SAFE-003 | 模式 3 遮挡、失锁或避障风险 | 立即 `ABORT_CLIMB_SIM` |
| SAFE-004 | 当前生产构建 | 零真实飞控写入、零物理舵机/载荷输出 |

## 10. 验证矩阵与阶段门禁

必须覆盖：目标交叉、相似衣着/车型、部分和完全遮挡、出画再进入、相机快速转动、低照度、
烟雾、反光、数据包乱序、Jetson 重启、QGC 重启、姿态陈旧、低纹理、TTC 突变和目标切换。

阶段顺序：

1. 离线数据集和录制视频；
2. Windows 本地摄像头；
3. Jetson RTSP 实时只读运行；
4. PX4 SITL 和软件 HIL；
5. 断桨、断执行器、惰性载荷约束台架；
6. 真实写入和物理执行器需要独立目标、独立评审和明确授权，本规格不自动批准该阶段。

## 11. 当前实现证据

截至 2026-07-15，以下只读核心已实现并通过本地回归；标注为部署项的源码使用无重启方式
同步到 Jetson，当前识别进程不重启、配置不切换：

- `src/multidetect/unified_tracking.py`：有界多目标池、逐轴白噪声加速度恒速 Kalman 预测/校正、
  协方差传播与创新距离门控、高/低置信度级联、矩形 Hungarian 全局关联、低置信度新身份
  抑制、累计相机运动补偿、多锁定、
  单主目标、保守 ReID 重捕获和 `LOST` 身份冻结；光流/模板提示只能修正预测框，不能形成
  身份观测，也不能单独恢复 `LOST` 目标。已锁定且具有严格 ReID 证据的目标可跨全画面
  重搜索；多个候选外观近似时拒绝猜测，旧身份继续保持 `LOST`。
- `src/multidetect/selection_target_pool.py`：把 QGC/操作端框选映射为稳定的统一目标 ID；
  任意未分类物体可建立 `manual` 轨迹，切换主目标时原目标继续后台锁定，取消仅解除当前目标。
  相似候选证据不足时拒绝强行绑定；该通道只写跟踪元数据，不产生飞控、舵机或载荷控制。
- `src/multidetect/short_term_tracking.py`：目标框内稀疏特征、一次批量前向/反向 LK、
  前后向误差门控、稳健位移中值、相机/速度残差，以及低特征时带相关峰值和次峰差门限的
  模板回退。10 个目标共享两次 LK 调用，避免逐目标重复计算。
- `src/multidetect/monocular_avoidance.py`：稀疏光流、RANSAC 相机运动估计、左/中/右分区、
  径向膨胀 TTC，以及陈旧、帧间隔、低特征或补偿失败时的 `INVALID` 闭锁。
- `src/multidetect/monocular_acceptance.py`：使用真实 OpenCV LK/RANSAC 和合成图像复核静态
  场景、相机平移补偿、中心障碍逼近、陈旧证据闭锁与处理性能；输出始终为建议，明确不提供
  绝对深度、飞控或执行器能力。
- `src/multidetect/live.py`：上述模块已接入逐帧实时循环；任何局部跟踪或 ReID 异常均降级到
  Kalman/运动预测并写入审计，不停止主检测，不产生飞控或执行器输出。人员和车辆 ReID
  具有相互独立的有界调度：核心库默认逐帧，Jetson 启动器在稳定轨迹上默认 `stride=2`，
  人员使用偶数相位、车辆使用奇数相位，避免两套模型在同一稳定帧形成周期性尾延迟尖峰；
  任一域最长 100 ms 必须执行一次，且该域出现 `OCCLUDED/REACQUIRING/LOST` 轨迹时立即
  越过降频门禁。每域单独报告推理次数、稳定期跳帧数、恢复强制次数和 P50/P95 延迟；目标池、
  Kalman、相机运动补偿和短时跟踪仍逐帧工作，因此降频不等于停止轨迹更新。没有对应人员或
  车辆候选框的帧不会调用该域编码器，也不会伪计为推理；它单独进入 `no_candidate_frames`，
  且不刷新 100 ms 时间门，保证候选重新出现时立即执行。
- `configs/models/ultralytics_yolo26n_coco80.json`：通用 COCO80 候选检测器已完成制品哈希、
  Nx6 输出契约和本地/Jetson CPU 加载验证，可提供人员、汽车、公交和卡车基础检测；电线和
  储罐仍是 Nx6/OBB 覆盖缺口，道路和建筑由独立语义上下文提供。
- CLI 和 Jetson 启动器现支持独立 `environment_risk_evidence` 模型域，把电线和储罐候选送入
  同一个目标池，同时禁止该模型声明火烟、人员或车辆等受保护类别，避免跨域
  身份和阈值污染。环境模型必须具有角色/哈希清单和目标机 TensorRT 溯源；当前契约位于
  `configs/models/environment_risk_detector_contract.json`，状态明确为
  `required_not_supplied`，因此该域仍默认关闭且不能宣称已经覆盖。修复后的完整类别阈值表还会
  保留 COCO 汽车、公交、卡车等检测；此前 CLI 只列出火烟/人员阈值而误删其他 COCO 类别。
- 环境域进一步按几何任务拆分：NVIDIA CitySemSegFormer 提供低频 road/building 类别掩码，
  TTPLA 用于训练电线细结构分割，DOTA/MMRotate OBB 用于储罐航拍旋转框。官方来源、提交和
  元数据哈希已固定在 `configs/models/environment_model_source_lock.json`；完整选择与验收见
  `docs/environment-perception-model-plan.md`。`src/multidetect/semantic_environment.py` 已实现
  CitySemSegFormer 官方 `1×3×1024×1820` RGB 预处理、单输入/输出形状门禁、类别 ID 校验和
  连通区域提取；类别掩码不含概率，因此明确不伪造检测置信度，也不进入 ReID 或控制路径。
  官方 ONNX 已按 NGC 摘要完成本地下载和结构/单帧执行验证；实时循环使用容量 1、默认 2 Hz
  的最新帧异步工作器，不阻塞主检测，也不会持续积压。Orin TensorRT 与部署域测试仍未完成。
  原生 TensorRT 静态会话、引擎溯源门和 Jetson 启动参数已经完成；实际 FP16 引擎仍需在维护
  窗口离线构建和基准测试，因此实时语义开关继续保持关闭。
- `src/multidetect/appearance_reid.py` 与 `src/multidetect/vehicle_reid.py`：人员和车辆采用两套
  相互隔离、哈希固定的 ReID 编码器。车辆候选仅允许 `vehicle/car/bus/truck`，不处理人员、
  摩托车或自行车；任一编码器失败只影响对应身份域并退化到运动预测，不能触发控制。
  OpenVINO 车辆制品的真实 ONNX 输入为动态 batch，描述清单已由错误的静态 batch=1 修正为
  `batch×3×208×208`，与 b1–b8 TensorRT 构建契约保持一致。
  实时 CLI 现在默认要求每个启用的 ReID 域同时提供目标机 TensorRT 引擎；只给 ONNX 会在
  打开摄像头前拒绝启动。`--allow-nonrealtime-reid` 仅用于显式实验，状态固定披露
  `onnx_nonrealtime`、实时准入失败和积帧风险已由操作者接受；Jetson 启动器不使用该豁免。
- `scripts/build_jetson_common_detector_engine.sh`、`scripts/build_jetson_reid_engine.sh` 和
  `scripts/build_jetson_vehicle_reid_engine.sh`：均在识别进程运行时拒绝并发 TensorRT 构建，
  只允许计划维护窗口生成带完整性校验的候选引擎。`src/multidetect/engine_provenance.py`
  还会把源 ONNX、引擎哈希、Jetson 型号、L4T、CUDA、TensorRT 和输入形状写入原子溯源
  清单；启动器启用任一新模型前必须在当前目标机重新验证全部字段和只读能力边界。
- `reid-tensorrt-bench` 与 `scripts/run_jetson_perception_engine_maintenance.sh`：维护脚本要求
  精确的地面停机确认、拒绝自行停止/重启在线识别并使用进程锁；三个引擎构建及溯源通过后，
  双 ReID 必须实际完成混合批处理、域隔离、重复特征稳定性与 P50/P95 延迟门禁。默认同时约束
  稳定期错峰延迟和恢复期两域串行延迟；仅有 `.engine` 文件不构成实时准入证据。
- `src/multidetect/patrol_advisory.py`：把统一主目标映射为模式 1 的巡检、锁定监测、遮挡、
  重捕获和丢失状态；仅在 `LOST` 时根据最后图像方位、地速、最大坡度、证据新鲜度、定位、
  围栏和链路健康生成返回观察建议。建议始终要求操作者确认和 SITL 验证，不含航线或飞控命令。
- `src/multidetect/multimodal_ranging.py`：实现相机内参、Brown-Conrady 畸变反解、相机到
  机体外参、Pixhawk 姿态、AGL/DEM/地面平面、带绝对尺度的 VIO 和可选激光测距融合；
  使用时间同步、年龄、归一化残差和最大一致子集门禁，输出斜距/地距、相对/绝对方位、
  N/E 偏移、95% 区间、新鲜度、一致性与 `VALID/DEGRADED/INVALID`。相机标定 JSON 采用
  严格模式：字段缺失、拼写错误或未知字段均拒绝启动，禁止使用猜测内参。实时循环只对当前
  统一主目标求解，并校验画面、姿态、位置和 AGL 的独立时间戳；该模块只读且无飞控、舵机
  或载荷接口。
- `src/multidetect/camera_calibration.py`：新增 ChArUco 多视角内参标定和可打印标定板生成。
  标定必须提供安装俯仰/偏航/滚转及视轴不确定度，不能由厂商 FOV 猜测；至少 20 个有效视图
  还要同时通过空间覆盖、板体尺度、远近变化、姿态倾斜、重投影误差、焦距标准差、焦距物理域
  和主点门禁。失败只写诊断报告，全部通过才原子写出可由严格测距加载器读取的 schema-v1
  标定文件。32 帧物理相机投影合成验收恢复 `fx=899.34/fy=909.66`，相对设定
  `900/910` 均小于 0.2%；这只验证算法实现，不替代三体摄像头实拍标定和外部距离交叉验证。
- `src/multidetect/deployment_planner.py`：模式 2 已从旧静风画面投影升级为身份绑定的多模态
  软件 HIL 解算。统一目标 ID、来源帧、类别和目标框必须与任务跟踪目标一致；测距必须为
  `VALID`，风速、空速、N/E 地速和时间戳必须新鲜且相互一致。二次阻力点质点积分使用载荷
  质量、阻力系数、参考面积、空气密度和释放延迟，有限差分传播测距、方位、风、速度、高度
  和载荷标定不确定度，输出预计 N/E 落点、二维 95% 误差椭圆及
  `TOO_EARLY/WINDOW/TOO_LATE/INVALID`。只有误差椭圆、横向走廊和时机同时满足才允许安全
  规则进入 `WINDOW`；仍只驱动 FakePayloadPort/软件 HIL，真实舵机和飞控写入保持关闭。
- `src/multidetect/rgb_fire_corroboration.py`：主火情模型和独立 RGB 复核模型采用不同的模型
  清单角色与不同制品 SHA-256。复核器先剥离主模型携带的全部自报复核字段，再按类别、置信度、
  IoU 和一对一匹配生成版本化证据契约；跟踪器会再次校验契约版本、模型版本、类别、数值域和
  两个不同制品哈希。仅有 `independent_rgb_corroborated=true` 的旧布尔标记不再有效。
- `src/multidetect/payload_target_gate.py`：模式 2 已实现独立的人工目标资格和滑动确认核心。
  火点选择解析到已复核火轨迹；车辆/建筑只有与唯一、稳定的已复核火轨迹形成空间复合关系时
  才能继续，且真正瞄准点仍是火区框。人员、烟雾、普通车辆、歧义关联、遮挡和陈旧目标均失败
  关闭。一次性滑动令牌同时绑定框选命令、所选目标 ID/修订和火区瞄准目标 ID/修订；滑动确认
  只生成短时有效的 `PayloadTargetIntent`，任务控制器还会在安全规则和独立授权前再次核对实际
  火区目标。当前核心仍为软件 HIL，不包含真实执行器。
- 模式 2 操作端协议已完成 Python、UDP 和相邻定制 QGC 闭环：`PAYLOAD_TARGET_CHALLENGE`
  （101 字节）、`PAYLOAD_TARGET_CONFIRMATION`（106 字节）、`PAYLOAD_TARGET_ACK`（50 字节）和
  `PAYLOAD_TARGET_STATUS`（80 字节）均使用现有 HMAC、签名 MAVLink2 `TUNNEL`、单调序列和
  对端绑定。确认同时绑定当前框选 UUID、所选目标稳定身份、唯一火区瞄准身份、会话、连续滑动
  证据和短时 TTL；目标切换、遮挡、失锁、证据陈旧、关联歧义或身份变化都会撤销挑战/意图。
  `--payload-target-hil` 强制要求操作端 UDP、统一目标池、独立 RGB 火情复核模型和可部署任务，
  并与模式 3 `--approach-hil` 互斥。QGC 已加入原生风格模式 2 状态和连续滑动控件；滑动 ACK
  之后仍只进入火区安全规则与独立授权流程，不能直接触发模拟或物理释放。Python/C++ 双端使用
  同一组四帧黄金向量逐字节验证；协议自测、21 项定制 UI/安全测试、QML AOT 和完整 Windows
  Release 链接均通过。最新 `MultiDetectGCS.exe` SHA-256 为
  `985FC06429C28F5A9E26A7C286112B7084D7D9C3275D313A4B2CA7074F73FA0B`。
- `src/multidetect/approach_hil.py`：模式 3 的独立软件 HIL 状态机已实现。任何类别都可作为
  人工框选对象，但必须是当前锁定主目标；连续滑动令牌绑定目标 ID、修订号和短时有效期，
  失败尝试或使用后的令牌不能复用。相机内参把目标中心换算为光轴偏差，并给出有界航向、
  俯仰和坡度建议。只有目标持续 `TRACKING`、多模态距离为 `VALID` 且绑定同一帧、遥测/围栏/
  链路健康、空速/高度/姿态在域内、单目避障不是 `AVOID/INVALID` 时才进入居中或接近仿真；
  遮挡、重捕获、恢复、失锁、目标修订变化、确认过期或任一安全证据失效都会进入粘滞
  `ABORT_CLIMB_SIM`，重新选择目标前不能自行恢复。所有建议均无飞控或物理输出接口。
- `src/multidetect/approach_acceptance.py` 与
  `scripts/run_mode3_approach_hil_acceptance.py` 已把模式 3 固化为端到端签名 HIL。车辆和人员
  分别完成目标选择 ACK、目标绑定连续滑动 ACK、三帧居中和 `APPROACH_SIM` 状态回传；遮挡、
  失锁、避障 `AVOID`、避障 `INVALID` 均立即进入并锁存 `ABORT_CLIMB_SIM`，目标切换要求新
  滑动，旧确认不能复用。滑动授权采用半开有效区间，在 `expires_at` 到达的精确时刻即失效，
  模式 2 和模式 3 的边界语义一致。修复后 Windows 证据为
  `artifacts/evaluation/mode3-post-expiry-fix-20260715T070837Z-windows.json`，SHA-256 为
  `A92BBCCF1726E1C0F70C79CBA35D84AC4D2A726AA422049F356972160C617A36`；Jetson 证据为
  `/home/jetson/Multi-Detect/artifacts/evaluation/mode3-post-expiry-fix-20260715T070837Z-jetson.json`，
  SHA-256 为 `e543c5bbd21cbfec0702f1733daca6826da5fadffe477fb34c8681db559c62ad`。
  两端均未连接摄像头、GPU 推理或 Pixhawk，飞控和真实执行器能力恒为关闭，Jetson PID 7767
  未重启。
- `src/multidetect/pixhawk.py`：只读接收 `GLOBAL_POSITION_INT` 的 N/E 速度、`VFR_HUD`
  空速和 `WIND_COV` 风速，并分别保存接收时间；未增加任何 MAVLink 发送、参数写入或流率
  请求。软件 HIL 配置中的弹道参数明确标记为 synthetic，不能替代断执行器台架标定。
- `PATROL_STATUS` 操作端消息：通过现有 HMAC 认证、签名 MAVLink2 `TUNNEL` 通道发送巡检
  阶段、主目标 ID/状态/框/类别/置信度/跟踪质量、目标池计数和返回观察建议。完整应用载荷
  110 字节，不超过 128 字节上限；消息与视频、框选 ACK、任务状态和安全状态相互独立。
- `TARGET_POOL_STATUS` 操作端消息：把统一目标池以每页最多 2 条、完整认证载荷最多 100 字节
  的只读分页发送到 QGC。页面携带目标 ID、类别、统一跟踪状态、置信度、质量、锁定/主目标、
  可处置和 ReID 确认标志；QGC 只在同一修订的全部页面到齐且目标 ID 唯一时原子替换快照，
  拒绝旧修订、页数冲突和重复目标，1 秒失鲜即清空。主页沿用原生紧凑状态面板，显示主目标
  和最多 5 个后台目标；该消息没有选择、授权、飞控或执行器能力。
- `SCENE_CONTEXT_STATUS`（类型 17）操作端消息：把 road/building 类别区域以每页最多 2 条、
  完整认证载荷最多 86 字节发送到 QGC。页面携带源帧哈希、源时间、`VALID/INVALID/STALE`、
  外接框、画面面积占比和框内填充率；协议故意不提供置信度，也明确没有目标身份权、选择、
  授权、飞控或载荷能力。QGC 只在同一修订的全部页面到齐后原子替换场景快照，2 秒失鲜或
  无效状态立即清空区域；视频叠加只以低透明度显示“道路/建筑·场景提示”，不会混入目标框。
- PX4 SITL + QGC + Jetson 元数据 HIL 驱动已扩展为发送 3 条轨迹的 2 个目标池分页；验收脚本
  还会连续生成 30 个 `TRACK_STATUS`，时间间隔 50 ms（20 Hz）。验收脚本要求 QGC 日志出现
  `target-pool snapshot complete revision=3 tracks=3 pages=2`、实际收到至少 30 个类型 3 跟踪包、
  总计至少认证 37 个元数据包，并继续证明操作端 `TUNNEL` 不进入 PX4。驱动静态检查、连续
  元数据自解码、分页自解码、协议自测和
  Windows Release 编译已通过；为不启动当前桌面 GUI，本轮没有执行完整 Docker/QGC 运行，
  因此真实 SITL 运行证据仍待受控窗口生成。
- QGC 控制器现按 `TRACK_STATUS` 源端单调发送时间维护最近最多 60 个样本的实收速率，主页在
  已认证链路旁显示 Hz；连续 1 秒没有跟踪包即清零，不能继续显示陈旧速率。完整 HIL 脚本
  会解析 QGC 自己记录的 30 样本速率并要求不低于 15 Hz，而不是只相信发送端声明。
- `RANGE_STATUS` 操作端消息：以不低于 15 Hz 的独立心跳发送当前主目标的测距有效性、来源、
  拒绝原因、斜距/地距、两组 95% 区间、相对/绝对方位、N/E 偏移、新鲜度和一致性。完整认证
  载荷 109 字节，严格拒绝未知原因位、来源冲突、畸形区间以及携带距离的 `INVALID` 状态；
  数据契约固定为只读建议，飞控写入和物理释放标志恒为关闭。
- 相邻定制 QGC 已完成 `RANGE_STATUS` 严格解码、单调序列门禁、1 秒失鲜清除和原生风格的
  距离/95% 区间/质量显示；协议黄金向量自测和 Windows Release 完整编译均通过。该构建仅
  生成在开发构建目录，未自动启动或替换操作员当前运行版本。
- `RELEASE_STATUS` 操作端消息：独立于任务状态和授权命令，以 15 Hz 上限发送模式 2 的
  `INVALID/TOO_EARLY/WINDOW/TOO_LATE`、目标与测距帧绑定、预计 N/E 落点、横纵误差、
  95% 误差椭圆、地距区间、下降时间、提前量、一致性和否决原因。完整认证载荷 115 字节；
  未知原因位、伪造绑定位、畸形区间和缺少完整绑定几何的 `WINDOW` 均被两端拒绝。定制 QGC
  采用原生状态行显示时序、落点和误差，1 秒失鲜自动清除；协议黄金向量自测和完整 Windows
  Release 编译均通过。该消息没有授权、飞控、舵机或物理释放能力。
- 模式 3 操作端协议已完成 Python 端和 UDP 实环：`APPROACH_CHALLENGE`（89 字节）、
  `APPROACH_CONFIRMATION`（94 字节）、`APPROACH_ACK`（50 字节）和 `APPROACH_STATUS`
  （70 字节）均使用现有 HMAC、签名 MAVLink2 `TUNNEL` 与单调序列门禁。确认绑定当前框选
  UUID、目标令牌、目标修订、会话和短时 TTL；服务端拒绝普通点击、非连续/未完成滑动、错绑
  对端、陈旧命令、重复序列、令牌内容冲突及已消费挑战。实时桥已具备挑战/状态限频发布和
  合法确认入队能力。实时循环现已把当前框选 UUID、统一主轨迹和稳定目标修订绑定为一个
  Mode-3 会话，消费合法确认后才驱动 `CENTERING_SIM/APPROACH_SIM`；框选变化会先清空旧
  挑战，遮挡、重捕获、恢复、失锁、测距/避障/遥测失效会锁存 `ABORT_CLIMB_SIM`。相邻定制
  QGC 已完成 12–15 类型的严格解码、Python/C++ 黄金向量、1 秒失鲜清除、Plan 第三模式、
  原生状态行和真实连续滑动控件；滑动必须从左端连续到 98% 以上并持续至少 600 ms。完整
  Windows Release 编译和协议自测均通过，所有输出仍为 SITL/HIL 建议。2026-07-15 最新
  `MultiDetectGCS.exe` SHA-256 为
  `985FC06429C28F5A9E26A7C286112B7084D7D9C3275D313A4B2CA7074F73FA0B`；QGC 接收端还会
  依据连续 `TRACK_STATUS` 的源时间戳计算实际元数据频率，1 秒失鲜即清零。闭环 HIL 驱动
  已生成 30 个 50 ms 间隔的跟踪包（20 Hz）和 3 条轨迹的 2 页目标池，但未在本轮自动启动
  QGC，因此 QGC 日志侧的 `>=15 Hz`、原子分页聚合和连续滑动仍保留为人工启动后的实测门禁。

该阶段 Python 全套为 923 项测试通过，Ruff 检查和格式门通过；定制 QGC 的协议黄金向量、
21 项静态安全/UI 测试、QML AOT 编译和完整 Windows Release 链接均通过。没有启动 QGC GUI，
也没有重启 Jetson 实时进程；90 个 Python 文件已在远端暂存区通过 `compileall` 后逐文件原子
替换并复核 SHA-256，备份位于
`/home/jetson/Multi-Detect/.codex-backups/20260715T062731Z-mode2-target-gate/src/multidetect`，
源码清单 SHA-256 为 `38c6015a68eff100dac5524852a7d343c388dd3479cb7ca6aebf905706bcdd68`，
PID 仍为 7767。Jetson 独立轻量进程已从更新后的源码成功导入模式 2 协调器，确认线协议类型
18–21 和 `--payload-target-hil` CLI 开关存在，且物理动作能力保持关闭。由于未重启，在线进程
仍运行旧巡检参数，新模式 2 开关尚未在该进程中生效。

`src/multidetect/payload_target_acceptance.py` 与
`scripts/run_mode2_payload_hil_acceptance.py` 已把模式 2 固化为一条可重复端到端软件 HIL。
Windows 和 Jetson 均在同一隔离 UDP 会话中完成签名目标选择 ACK、唯一独立 RGB 火点解析、
连续滑动 ACK、确认状态回传、独立授权 ACK 和恰好一次 `FakePayloadPort` 模拟释放；人员、无火情
证据的普通车辆、目标切换、滑动超时及滑动后人员进入排除区均失败闭锁；滑动授权在精确到期
时刻立即失效。修复后 Windows 证据为
`artifacts/evaluation/mode2-post-expiry-fix-20260715T070836Z-windows.json`，SHA-256 为
`49B65FCEAD6E3577E826AA9FA0B95C159B05DAE7F992F236136CF4D4FF6E37C6`；Jetson 证据为
`/home/jetson/Multi-Detect/artifacts/evaluation/mode2-post-expiry-fix-20260715T070836Z-jetson.json`，
SHA-256 为 `d478261df728c907822e62350d0087e7c69b2fb441439549511513fcea457398`。Jetson
Python 3.10 兼容性已由实跑验证；全过程没有摄像头、GPU 推理、Pixhawk 连接、飞控写入或真实
载荷接口，PID 7767 的启动身份保持不变。

确定性 640×360、10 轨迹合成运动基准，Jetson 实时识别 PID 7767 并行运行：

- 短时跟踪，分析宽度 320、隔帧光流：100 帧，中位 5.984 ms，P95 12.615 ms，
  最大 23.879 ms；50 个计算帧均维持 10 条提示。
- 轻量避障，分析宽度 320：80 帧，中位 8.413 ms，P95 9.921 ms，最大 18.416 ms；
  80 帧均得到有效相机运动补偿。
- 多模态测距，AGL+DEM+相机地面交点+激光的 2,000 次 Jetson 并行基准：中位
  0.5183 ms，P95 0.5556 ms，最大 2.0154 ms；实时识别 PID 7767 全程未重启。
- 模式 2 多模态弹道与误差椭圆解算，本地 1,000 次确定性基准：中位 0.6054 ms，P95
  0.7304 ms，最大 0.9879 ms；该结果只证明软件计算余量，不是落点精度或实飞证据。
- 统一目标池硬验收在 Windows 与 Jetson（PID 7767 并行运行）均维持 10 条轨迹；最新
  恒速 Kalman + 置信度级联 + Hungarian 源码在 Jetson 独立进程的 10 目标关联 P95 为
  8.723 ms，对应约 114.6 Hz 计算余量，重复主目标切换最大 0.0152 ms。短遮挡保持原 ID；
  无 ReID 证据时
  旧轨迹保持 `LOST` 并创建新 ID，强 ReID 证据下恢复原 ID。证据位于
  `artifacts/evaluation/jetson-unified-global-assignment-acceptance-20260715T074833.json`；该确定性
  合成验收不是复杂真实场景准确率证明，且同步源码要到受控重启后才进入在线 PID。
- Windows 确定性验收进一步连续运行 300 帧并强制检查全部 10 个 `target_id` 不变，周期性
  切换两个已锁定主/后台目标；两辆具有独立 ReID 的车辆交叉、短遮挡且检测顺序反转后保持
  原身份。一个已锁定 LOST 目标面对两个近似全画面候选时阻止 2 个歧义关联，旧目标保持
  `LOST`。恒速 Kalman 在 0.1 秒检测空窗中的归一化位置误差门限为 0.025，并增加了收敛后
  突发干扰物创新门控回归。以上仍是合成确定性证据，不替代部署域视频指标。
- 专门构造的二目标/二观测贪心反例中，逐边贪心总代价为 1.00，矩形 Hungarian 全局解为
  0.31；另一个级联验收证明置信度 0.20 的观测可以延续已有轨迹，但同帧远处的 0.20 候选
  不会建立新 ID。阈值均已进入 Windows CLI、Jetson 启动器和运行摘要，便于部署域标定。
- `src/multidetect/tracking_evaluation.py` 和 `evaluate-tracking` 已提供严格身份时间线、逐帧
  Hungarian 匹配、全局 IDF1/IDSW/碎片化/MOTA、遮挡与出画恢复率及延迟门禁。示例报告
  `artifacts/evaluation/tracking-identity-calculator-demo-20260715.json` 明确标记为
  `synthetic_demo` 和 `deployment_domain_evidence_complete=false`；仓库仍没有经过复核的部署域
  身份录像标注，不能把该示例满分解释为真实精度。详细格式见
  `docs/tracking-identity-evaluation.md`。
- 上述身份评测代码和示例已原子同步到 Jetson，远端编译与验收门槛通过，在线 PID 7767
  同步前后未变化。证据位于
  `artifacts/evaluation/jetson-tracking-evaluator-verification-20260715T001111Z.json`；其输入仍是
  `synthetic_demo`，不是部署域准确率证据，且未重启的在线进程尚未载入新代码。
- 旧实时进程在未启用这两个新模块时实测约 25.08 FPS。基于独立耗时只能说明新配置具有
  达到 15 Hz 的性能余量，不能替代受控重启后的端到端 RTSP 验收。

Jetson 启动器下一次受控重启默认启用目标池、320 宽轻量避障和 320 宽隔帧短时跟踪；
通用检测、人员 ReID 和车辆 ReID 在各自 TensorRT 引擎完成计划维护窗口构建前仍默认关闭。
2026-07-15 实机预检确认三个固定哈希 ONNX 均已位于 Orin NX，TensorRT 8.6.2 可用，但三个
目标机引擎均不存在；三个构建器在 PID 7767 运行时均以退出码 3 拒绝并发构建。机器可读证据为
`artifacts/evaluation/jetson-new-perception-engine-preflight-20260715.json`，其
`ready_to_enable=false`，不得据此宣称新模型已经部署。
60 分钟 Jetson 门禁已固定为至少 54000 帧、处理速率不低于 15 FPS、推理 P95 不超过
66.7 ms、捕获队列高水位不超过 1；60 秒预热后按秒采样进程 RSS，缺少时间序列或稳健首尾
增长超过 256 MB 都失败，并报告 RSS 峰值及每小时趋势。旧 30 分钟证据不再通过。真实 RTSP 60 分钟复跑、
低纹理、烟雾、强光、快速姿态、车辆部署域 ReID 指标和 QGC
端到端延迟仍属于未完成验收项。

正在运行且未重启的旧配置 PID 7767 已额外完成最近连续 3600 秒只读观察：61,448 个预测帧，
17.069 FPS，推理中位 29.714 ms、P95 30.474 ms、最大 41.463 ms，最高温度 51.968°C，
该窗口审计中未出现相机或推理失败事件。由于该进程未启用统一目标池、轻量避障和多模态测距，
且退出前无法取得最终队列/重连计数、未测 QGC 视频叠加延迟，因此证据明确标记 `passed=false`，
只作为真实 RTSP 旧配置性能基线，不能满足完整门禁。证据位于
`artifacts/evaluation/jetson-live-last-hour-observation-20260715.json`。

当前源码回归共 923 项测试通过，`ruff` 全量检查与格式门通过；框选、目标池、通用检测候选、双域
ReID、巡检建议、实时测距、模式 2 目标门和全部只读状态消息均已通过本地回归。正在运行的 PID 7767
未重启，因此新的启动参数和模块开关要到下一次受控维护窗口才生效；在提供实机相机标定前，
启动器保持多模态测距默认关闭。后台目标明细的限频分页、QGC 原子聚合和原生状态列表已完成。
真实 RTSP 测距、独立绝对距离源、真实载荷台架参数、模式 2 落点精度、模式 3
真实 SITL 飞行场景联调、模式 2 QGC↔Jetson 实机触摸连续滑动验收和
端到端延迟仍属于未完成验收项。

身份证据链现已补齐运行时日志入口：`--identity-tracking-log-out` 在统一目标池启用时逐帧写出
评测器可直接读取的轨迹 ID、状态和框；Jetson 启动器在下一次受控启动后生成该日志。
识别与录像脚本现在都强制要求共享 `TRACKING_EVIDENCE_SESSION_ID`。`prepare-tracking-review`
会校验会话 UUID、源视频 SHA256/字节数、录像清单 v2 和单调时间窗，再生成不可直接评测的待复核
草稿，默认拒绝覆盖人工工作。真实同会话录像、逐帧视频对齐和第二人身份复核仍未完成，因此目标
继续保持活动。

可选的 `record-rtsp-evidence` 使用 Jetson 现有 Python GStreamer 绑定将 H.265 RTSP 码流直拷贝
到 Matroska，不解码、不重编码、不进入实时识别/QGC 链路，RTSP URI 只从环境变量读取且不写入
清单。该工具保持独立、默认关闭，录制结束需通过 EOS、非空文件、SHA256 和全帧媒体强校验；
录制只有在显式提供与实时识别相同的证据会话 UUID 时才会启动。
Jetson 预检已确认 Python 3.10 导入、GStreamer 1.20.3、所需五个码流直拷贝元件、CLI 和脚本
语法全部可用；期间没有连接摄像头，PID 7767 未变化。机器可读证据位于
`artifacts/evaluation/jetson-rtsp-stream-copy-recorder-preflight-20260715T010146Z.json`。

真实摄像头 5 秒直拷贝冒烟测试也已完成：输出 H.265 Main Profile Matroska 68,191 字节，
103/103 帧可解码，25 FPS、1280×720，SHA256 一致且清单无 RTSP URI。并发 PID 7767 未重启、
RSS 未变化，最近 500 条审计无相机读取、推理或目标池失败。证据位于
`artifacts/evaluation/jetson-real-rtsp-stream-copy-smoke-20260715T010559Z.json`。旧进程没有身份
日志，因此该录像只证明低开销采集链路可用，不是跟踪精度证据。
该旧冒烟清单早于会话绑定 schema v2，也会被新的复核包生成器拒绝。

会话绑定源码已原子同步到 Jetson，远端 Python 编译、脚本语法、CLI 入口和共享 UUID 防误配门禁
通过，受保护在线进程仍为 PID 7767 且未重启。机器可读记录位于
`artifacts/evaluation/jetson-evidence-session-binding-sync-20260715T013127Z.json`。因此新协议已在磁盘
就绪，但尚未由在线进程加载；不能把旧录像和旧进程数据声明成同会话身份评测证据。

短时遮挡恢复已从“仅使用上一帧模板”升级为最后可靠观测模板库：只有 DETECTED、LOCKED、
TRACKING、RECOVERED 且本帧真实命中的轨迹才能刷新模板；OCCLUDED/REACQUIRING 使用有时效上限
的保留模板并按状态扩大搜索窗，LOST 轨迹完全禁止相关滤波盲跟随。集成测试覆盖完整遮挡、目标
横向移动后恢复原 `track_id`、模板过期拒绝恢复和 LOST 不生成运动提示。该提示仍只修正预测，
不能单独构成身份观测或控制依据。
本地 820 项回归和 Jetson 远端编译、OpenCV/NumPy 导入、CLI/启动器门禁均通过，在线 PID
仍为 7767。机器可读记录位于
`artifacts/evaluation/jetson-retained-template-reacquisition-sync-20260715T014904Z.json`；真实摄像头
遮挡恢复时延和准确率仍需维护窗口实测。

统一目标池现提供独立 `unified-tracking-bench` 命令，完全不打开摄像头、模型或 Pixhawk，并同时
报告关联 P50/P95/P99/最大值、整段墙钟吞吐、周期主目标切换和身份安全场景。Jetson 在在线 PID
7767 并行运行时以 nice 10 完成 3000 帧、10 目标和 51 次切换：整段吞吐 119.152 Hz，关联
P95 8.538 ms、P99 8.693 ms，最大重复切换延迟 0.0264 ms；短遮挡恢复 0.15 秒，强 ReID
重捕获 0.20 秒，近似身份未被强行恢复。PID 与 RSS 前后均为 7767 / 754864 KB。原始报告与
验证记录分别位于
`artifacts/evaluation/jetson-unified-tracking-core-bench-20260715T015954Z.json` 和
`artifacts/evaluation/jetson-unified-tracking-core-bench-verification-20260715T015954Z.json`。
该合成框基准只证明元数据核心满足 10 目标、15 Hz 和切换延迟预算，不证明真实检测/ReID 精度、
视频叠加延迟或完整 Jetson 管线吞吐。

短时图像跟踪另有 `short-term-tracking-bench`：它在内存生成 10 个独立纹理目标，通过真实
OpenCV 前后向 LK 光流和模板相关处理 300 帧，并让一个目标完全消失 13 帧后横向跳变 60 像素。
Jetson nice 10 实测 150 次 OpenCV 更新全部为 OK：处理 P95 8.567 ms、P99 10.214 ms，完整
图像跟踪循环 P95 10.784 ms、墙钟速率 154.58 Hz，按实际部署 stride=2 输出提示 15 Hz；
0.467 秒后观察到保留模板提示并由目标池恢复原 ID，缓存峰值为 10/16，INVALID 为 0。PID/RSS
仍为 7767 / 754864 KB。报告与验证记录位于
`artifacts/evaluation/jetson-short-term-image-tracking-bench-20260715T021158Z.json` 和
`artifacts/evaluation/jetson-short-term-image-tracking-bench-verification-20260715T021158Z.json`。
这比纯框基准更接近运行路径，但输入仍是合成图像，不替代真实烟雾、遮挡、运动模糊和 ReID
录像验收。

轻量单目避障新增独立 `monocular-avoidance-bench` 图像级门禁。它用静态纹理背景验证无误报，
用全画面平移验证 RANSAC 相机运动补偿，用中心纹理块径向放大验证 `AVOID` 和分区 TTC，并强制
陈旧证据进入 `INVALID`。Jetson nice 10 实测 300 帧中 `CLEAR/AVOID=150/150`、`INVALID=0`，
处理 P95 8.312 ms、完整循环 P95 8.981 ms、墙钟速率 131.45 Hz；中心逼近 TTC 为 0.843 秒，
相机平移场景保持 `CLEAR`。PID/RSS 前后仍为 7767 / 754864 KB。报告与验证记录位于
`artifacts/evaluation/jetson-monocular-avoidance-image-bench-20260715T022651Z.json` 和
`artifacts/evaluation/jetson-monocular-avoidance-image-bench-verification-20260715T022651Z.json`。
该基准证明算法实现和性能余量，不证明真实场景障碍召回率、尺度距离或闭环避障能力；真实飞控
控制继续禁用。

两套固定哈希 ReID 制品新增 `reid-onnx-cpu-bench`：在同一帧构造 4 人、4 车以及火焰/烟雾
混合候选，分别检查 256/512 维 L2 归一化嵌入、类别域隔离、重复确定性和 CPU 延迟。Windows
两次重复验证的余弦距离均约为 `1.11e-16`，证明同输入输出稳定，但 CPU 串行 P95 达
11016.93 ms，明确不满足 66.7 ms 实时预算。Jetson 以 CPU 4/5 亲和、nice 19 完成一次兼容性
冒烟，人员/车辆分别为 854.93/323.00 ms，总计 1177.92 ms，同样判定
`realtime_budget_passed=false`；在线 PID 7767 未重启，RSS 755128→755084 KB。报告与验证记录
位于 `artifacts/evaluation/jetson-reid-onnx-cpu-smoke-20260715T024808Z.json` 和
`artifacts/evaluation/jetson-reid-onnx-cpu-smoke-verification-20260715T024808Z.json`。该结果只证明
真实制品可执行和身份域正确，不证明部署域 IDF1，也不能替代维护窗口内的目标机 TensorRT 引擎
构建与 15 Hz 联合管线测试。
