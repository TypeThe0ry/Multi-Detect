# OnboardAI 首批导航动作训练

目标是让现有图文模型学会：读到任务、看到当前环境后，选择下一步前进、转向或停止。
这不是给“红车／绿车”写一条固定答案，也不是另造一套无人机飞控。
本轮沿现有 Qwen3-VL-2B 底座增加独立导航 adapter；原 YOLO、跟踪、测距和 PX4
执行链继续保留。最终仍要求 Gazebo 与 Jetson 使用同一个受审模型和输入／执行契约。

## 已真正取得的素材与训练结果

最新执行：完整GPU底座加载已通过独立审核；原86步四阶段更新全部完成，底座参数不变，
FP32导航LoRA已变化并保存。最后同进程第二次底座重载遇CUDA失败，原报告仍退出1；
新独立GPU worker已精确恢复保存的adapter和原4个输出，退出0；外层监督器因Windows
venv启动器／worker PID不同退出1，原失败保留。新增只读身份检查已核对真实进程链并退出0，
独立字节审计也通过；未删除PID门、重训或改写原报告。112个FP32保存参数、原4个输入
及生成token精确恢复，只证明保存后结果可复现，不证明已学会可靠动作。
训练内4个probe严格输出／动作各0/4→1/4、尺度0/4；合法前进输出10m但gold是3m，
其余转向／STOP尚不合格。这不能当质量验收，不能用于自动飞行。
[完整加载证据](evidence/onboard-navigation-full-load-only-20261007-r2.json)、
[独立精确恢复证据](evidence/onboard-navigation-exact-reload-20261007-r1.json)、
[真实执行接线差异](onboard-navigation-execution-gap-20261007.md)。
[下一轮动作质量改进](onboard-navigation-quality-next-20261007.md)：每类70次／共280次draw
抽样表已生成并核对，原86个样本全覆盖；未执行280次训练，不改历史评分。
[均衡抽样准备证据](evidence/onboard-navigation-balanced-schedule-20261007-r2.json)。

新的共享导航actor输入基础已实现，官方本地Qwen3VLProcessor（Transformers4.57.6／
Torch2.6.0+cu124）在CPU上验证86个保留样本：监督投影与可观测输入构造的identity及
四个实际张量字节全部一致，原生退出0，129个来源前后不变；最长453token、每图最大
46080pixels，保持4096／50176上限。独立只读核验重开来源、重算actor identity并核对
保存的tensor记录和预算，退出0；未二次运行processor。新system不是旧训练prefix的
逐字节复现；两边都来自保留RGB，不是独立实时捕获。CUDA未初始化，无模型加载、推理
或参数更新；仍未接新trainer／导航live backend，默认模型和飞行权限不变。
[共享输入一致性证据](evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json)。

已经下载并重开 OpenFly 官方发布的原始训练表、3 条完整城市路线、86 张三维图像、
原始动作／姿态和生成代码。图片是建筑、道路与空间环境的渲染，不是固色卡片或状态 JSON。
训练表保留完整原件，实际解析 100,226 条记录；本次只取其中原索引 0、7、8 的完整路线。

| 本批内容 | 实际数量 |
| --- | ---: |
| 完整路线 | 3 |
| 原始 RGB 图片／逐步记录 | 86 |
| 前进 | 70 |
| 左转／右转 | 6／7 |
| 原路线停止 | 3 |
| 升降动作 | 0 |
| 独立场景 | 1 |

图片与姿态约 52.5 MiB；原训练表约 405.8 MB，保存在忽略的本地 artifacts 中，不把
大文件塞进 Git。下载器保留官方发布摘要、文件树、许可证原件和逐文件 SHA，命令可复现。
缺图片的路线被排除，失败导入 r1 原件仍保留；未补假图、未下载原 seen/unseen 留出集或 7B 权重。

## 纠正了会让模型学错的标签问题

原训练表会合并连续动作。部分合并标签与所选图像之后的实际下一步不对应，不能直接训练。
本次改用原始逐步记录：第 i 张图对应第 i 步姿态，原动作 i 对应姿态 i 到 i+1；
对距离、左右转符号与转角逐条验算。模型输入仅原文任务、当前图和最多一张最近过去图。
下一张图、目标真值、路线、姿态和正确动作不会混进输入。

距离使用经过当前场景生成配置核验的米，转角转换为弧度；未知时间保持未知，
不编造速度或飞行时长。原代码中的 15／2 是内部类别原型，实际执行转换另用 30°／3 米；
不能拿类别原型当物理幅度。单位与转换仅限已经核验的 AirSim 场景，不能推广给其它渲染器。
来源：[OpenFly 原规划代码](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/tool_ws/src/traj_gen/include/navi/pathsearch.hpp)、
[类别到动作转换](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/train/eval.py)。

这里的动作是外部路线的**规划动作**，不是本项目飞机已经执行的回执。
STOP 只表示路线停止，不代表降落、返航或任务完成。图像色彩／朝向与真实相机的
标定对应尚未证实；原像素不做悄悄“纠正”。

## 本轮训练能证明什么

首批数据已完成 CPU 来源、动作与输入隔离核验。所有路线来自同一个场景，按完整场景和
重复图像分组后只有一个组，因此全部保留为 train；没有伪造独立验证／测试集。
独立来源审核只允许一次受预算约束的 train-only 导航试训，不改变 loader 的
`training_ready=false`，也不授权模型进入飞行。

本轮已经封存数据、底座、源码、像素／显存和更新次数；训练入口的 50 项 CPU 检查通过。
原计划为完整 86 步、一轮，只更新独立语言 LoRA，不改变底座或现有默认策略。
父模型与候选可在预先选定的训练例上对比，但这种对比只是训练内诊断，不能称泛化准确率，
不能据此晋升。升降、障碍、动态目标、异常处理和返航没有被这 86 步覆盖。

## 加载失败与恢复过程的历史记录

首轮已在本机 RTX 4060 启动，原生工具退出码为 1；最后可见日志在模型加载阶段。
Windows 同时记录 `torch_cpu.dll / 0xc0000005` 访问异常。没有正常训练报告、步骤日志或
adapter，不能声称训练成功，也不把未知更新次数写成零。原失败目录完整保留。
[首轮失败证据](evidence/onboard-navigation-native-load-failure-20261007-r1.json)独立于后续加载诊断。

当时新增进程外监督入口拆开“加载”和“训练”：先只加载现有底座与导航 adapter，
不做推理、反向传播或参数更新；外进程保存完整 stdout／stderr、真实 PID 和退出码。
该入口的40项CPU检查与独立复核通过；首次单独加载也以 `0xc0000005` 退出。
完整堆栈定位到 `torch/storage.py` 的权重切片读取，底座加载尚未返回，也未附加 LoRA。
真实 worker PID 与 Windows 事件精确对应；26个绑定文件前后不变。
[加载诊断证据](evidence/onboard-navigation-load-only-failure-20261007-r1.json)单独封存，
不会改写原训练失败。堆栈定位不等于已经证明 C++ 根因或显存不足。

独立环境已实际测试显式 `pread`：旧0.4.5与新0.8.0的真实CPU切片均退出0、数值与
原文件完全一致；完整GPU加载仍退出1，明确报Windows页面文件太小（1455），首次
`safe_open`尚未返回。53个文件前后不变；独立审核通过仅说明留证可靠，不是加载成功。
[读取兼容实验与失败证据](evidence/onboard-navigation-pread-load-only-20261007-r1.json)。
事后C盘约200MB可用、页面文件在C盘；这是事后容量快照，不是错误瞬间峰值，也不能
用这次1455解释此前访问异常。没有清理文件、关闭其它程序或改动页面文件设置。

官方0.8.0即使选择pread仍先做整文件映射，Windows写时复制映射会占用整视图的提交额度。
独立“只读头部、逐张量读权重”入口已完成；reader82项、wrapper70项软件检查与联合236项
检查通过，独立审核通过。真实完整加载的两个头部打开成功，但tensor payload仍为零，
随后报CUDA OutOfMemoryError（约3.96GiB申请，错误文字称约6.93GiB可用）；模型和两个
结果门均退出1。55个文件前后不变，未初始化、推理、反向或更新，计数保持null。
[新入口失败证据](evidence/onboard-navigation-streaming-load-only-20261007-r1.json)单独封存。
随后新增仅子进程关闭可选整块缓存预热的入口；原精度、数据、设备映射、86步预算不变。
错误的精确运行帧和碎片化仍未证明，绕过预热也不保证后续训练内存足够；
[本机源码分析](research/onboard-navigation-allocator-warmup-20261007.md)。
来源：[safetensors 0.8.0实现](https://raw.githubusercontent.com/safetensors/safetensors/v0.8.0/bindings/python/src/lib.rs)、
[Windows映射行为](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-mapviewoffile)。
原环境与两次失败保持；不更换Torch、底座、训练数据、精度或预算。
[前序原生异常诊断](research/onboard-navigation-native-load-diagnostic-20261007.md)保留原结论。

并行核对了公开模型、许可与硬件条件：现有 Qwen 加导航 LoRA 仍是当前最短路线；
OpenFly 优先复用动作数据，大型世界模型只作为后续研究对照。
[模型选型与部署边界](onboard-pretrained-model-options-20261007.md)。

## 下一步向真实自主任务推进

1. 从已验证保存／恢复的导航候选继续改进动作质量，将已验证的共享actor接入新trainer与导航live backend，封存新的均衡训练计划。当前280次draw只是准备，原100步预算未变。
2. 补多个完整场景、升降与障碍／目标变化路线，建立真正隔离的开发留出集，评价动作选择而非仅 JSON 格式。
3. 回到本项目 Gazebo/PX4，记录模型建议、被执行器接受的计划、实际动作回执和后继图像，训练本域闭环。
4. 让同一模型进入 MultiQGC／Jetson 链路；一次 Prompt 后持续判断、行动、重规划和回报。
5. 分别完成 Jetson、HIL／REAL、室外 GNSS、测距与最终发布验收。

分布式 Gazebo 诊断在本轮返回 `no_eligible_node`，没有新仿真采集在运行。
首批小规模 CPU 核验和 GPU 试训使用本机已有缓存，避免为这 86 步传输数 GB 底座。
这不是 Gazebo 新架次、实飞或最终 `0.3.0` 验收；默认 r33、COCO 和 release parity 不替换。

## 本地复现入口

以下数据生成命令不会训练或驱动飞机。输出目录必须是新的，不覆盖已封存结果。

```powershell
.venv-vlm/Scripts/python.exe -B -X utf8 scripts/fetch_openfly_navigation_pilot.py --output-dir artifacts/training/openfly-navigation-source-new --episodes 3
.venv-vlm/Scripts/python.exe -B -X utf8 scripts/build_onboard_openfly_navigation_dataset.py --source-manifest artifacts/training/openfly-navigation-source-new/manifest.json --output-dir artifacts/training/onboard-openfly-navigation-dataset-new
```

本轮原件与数据位于 `artifacts/training/openfly-navigation-source-pilot-20261007-r2/`
和 `artifacts/training/onboard-openfly-navigation-dataset-20261007-r2/`；r1 均保留，不覆盖。
源码与检查在 [下载器](../scripts/fetch_openfly_navigation_pilot.py)、
[动作契约](../src/multidetect/autonomy/onboard_navigation_supervision.py)、
[数据构建器](../scripts/build_onboard_openfly_navigation_dataset.py)。
前序 [Gazebo 图像回放](onboard-live-vlm-retained-replay.md) 和
[相机接入](onboard-gazebo-live-shadow.md) 的证据保持独立。
