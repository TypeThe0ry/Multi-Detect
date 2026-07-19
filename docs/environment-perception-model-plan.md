# 环境风险感知模型计划

## 1. 为什么不能只用一个通用框模型

建筑和道路是大面积场景区域，电线是长而细且容易与背景同色的结构，储罐在航拍视角下具有
任意旋转方向。把三类问题全部压成普通水平框，会损失道路可通行区域、电线像素几何和储罐
方向信息，也会制造无法解释的“置信度”。因此环境感知拆成三个模型域，但输出统一进入同一个
只读环境上下文和目标元数据层。

## 2. 建筑与道路：NVIDIA CitySemSegFormer

候选来源为
[NVIDIA NGC CitySemSegFormer](https://catalog.ngc.nvidia.com/orgs/nvidia/tao/models/citysemsegformer)。
官方模型卡列出 road、building、person、car、truck、bus 等城市类别，并提供可部署 ONNX。
官方 `nvinfer_config.txt` 固定：

- 输入 `1×3×1024×1820`，RGB；
- offsets `123.675 / 116.28 / 103.53`；
- scale `0.01735207357279195`；
- 输出为 HWC 类别掩码，不是 Nx6 检测；
- 当前官方 ONNX 页面报告约 331 MB。

本项目只选择 road 和 building 作为低频场景上下文。`semantic_environment.py` 严格验证输入、
输出和类别 ID，按连通区域输出面积与包围框，但不生成模型未提供的置信度。它不进入人员/
车辆 ReID，也不产生控制输出。官方 `deployable_onnx_v1.0` 已下载到本地忽略目录：精确大小
347,158,912 字节，SHA-256 为
`94ace62e250ed0a3122a46df8573950510b60a90c1b511e53c40dbca2bea21fb`，与 NVIDIA NGC v2
元数据完全一致。ONNX checker 通过，实际输入为动态 batch `×3×1024×1820`，输出节点为
`output`、形状为动态 batch `×1024×1820×1` 的 int64 类别掩码。本地 Windows CPU 单黑帧
只作为可执行性检查，耗时 6.929 秒，不能代表 Jetson 性能。Orin TensorRT 构建、部署域 IoU、
实时延迟和热稳定测试尚未完成。

同一 ONNX 和隔离清单已原子同步到 Jetson，远端 SHA-256 与角色清单再次验证通过。Jetson
ONNX Runtime 当前只暴露 Azure/CPU provider，加载耗时 6.248 秒，因此保持实时开关关闭。
`scripts/build_jetson_semantic_context_engine.sh` 已准备 FP16 构建与完整溯源，并在识别进程运行时
拒绝并发构建；需要在维护窗口停下旧进程后才能执行。

原生 `TensorRtSemanticSession` 已接入 CLI 和 Jetson 启动器：仅接受静态
`1×3×1024×1820` float32 输入和 `1×1024×1820×1` int32/int64 类别掩码输出。启用时必须同时
提供 ONNX 清单、TensorRT 引擎、引擎 SHA-256 和目标机运行时溯源，缺一项即拒绝启动。当前
尚未在维护窗口生成实际引擎，因此实时开关继续关闭。

实时接入采用容量固定为 1 的异步最新帧工作器，默认每 0.5 秒最多提交一次；新帧只替换尚未
处理的旧帧，主检测循环不等待分割模型。超过 2 秒的结果会变为陈旧状态并清除可用区域。
道路/建筑结果通过认证的 `SCENE_CONTEXT_STATUS` 类型 17 发送到定制 QGC，每页最多两条；
QGC 完整收齐同一修订后才原子显示，并在 2 秒失鲜时清空。协议只有类别、框、面积和填充率，
没有置信度或目标身份字段，不能进入目标池、ReID、安全授权、飞控或载荷释放路径。

## 3. 电线：TTPLA 细结构分割

数据来源锁定为
[TTPLA 官方仓库](https://github.com/R3ab/ttpla_dataset)，提交
`72ddf48cfee6d25b89fa8063e4dcd44bad08cddb`。官方仓库提供电塔和电线的像素级 COCO 标注；
论文明确说明电线长、细、可能与背景同色，不能使用普通大目标检测指标代替。

计划训练单独的轻量分割模型，并要求：

- 数据授权、图像来源和重复样本审计；
- 按航线/场景分组切分，禁止相邻帧跨训练与测试集；
- 线像素召回率、连通性、中心线偏差和最小可见线宽；
- 烟雾、强光、道路标线、树枝和建筑边缘硬负样本；
- Jetson TensorRT FP16 实测和长时间门禁。

旧 YOLACT 权重只可作为研究基线，不直接作为生产模型。

## 4. 储罐：DOTA 航拍旋转框

训练工具候选为 Apache-2.0 的
[OpenMMLab MMRotate](https://github.com/open-mmlab/mmrotate)，锁定提交
`b030f38909fc431be7ecb90772ac30da9da29bcb`。其 DOTA/RTMDet-OBB 路线支持航拍旋转目标和
TensorRT 基准；DOTA 包含 storage-tank 类别。

在下载或训练 DOTA 数据前必须单独确认数据集使用许可。运行时保留 OBB 做去重和几何判断，
只在发送现有 QGC 元数据时生成保守水平外接框。储罐只是环境风险排除对象，不是灭火载荷
投放目标。

## 5. 当前状态

| 模型域 | 软件适配 | 模型工件 | Jetson 引擎 | 生产状态 |
|---|---:|---:|---:|---:|
| 建筑/道路语义 | 已接入有界低频实时链 | 官方 ONNX 已验哈希 | 未构建 | 禁用 |
| 电线细分割 | 契约已锁定 | 未训练 | 未构建 | 禁用 |
| 储罐 OBB | 架构已选 | 许可待确认 | 未构建 | 禁用 |

源地址、提交、元数据哈希和状态保存在
`configs/models/environment_model_source_lock.json`。Nx6 环境检测域只承担电线和储罐；道路与
建筑只走独立的类别掩码旁路，避免重复推理。任何缺少模型角色清单、源哈希、目标机
溯源或部署域指标的工件都不能把 `required_not_supplied` 改为已覆盖。所有环境输出保持只读，
真实飞控写入和物理释放关闭。
