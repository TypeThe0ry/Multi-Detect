# 让AI从真实行动结果学习：三维轨迹数据

我们需要的是“无人机看到什么、决定做什么、实际做了什么、之后看到了什么”。只写
预定航点或把计划位移当成已经飞过，不足以训练你要求的实时自主判断。

本轮已经实现新的数据合同和原文件核验入口，**尚未采集新的教师飞行轨迹，也没有
据此开始新训练**。它扩展现有行动接口，不修改已有模型、旧监督数据或默认飞行策略。

## 每一步现在可以记录什么

教师提议 → 安全批准 → 唯一提交 → 执行结果 → 真实前后画面与位姿。

这些阶段分开保存：只有提议、发送失败、超时和未知结果也能留下，不能改写成成功。
教师要求前进一段距离，而飞机实际只移动了一部分时，命令和实测反馈分别保留，不用
实际位移“修好”原始命令。当前最小合同不支持安全裁剪后偷偷换目标，身份变化会拒绝。

三维位移明确为机体前／右／下，保存完整四元数姿态；支持明确的NED／ENU转换及
Gazebo原始FLU／ENU坐标适配，不只使用水平朝向。STOP仍只是停止序列，不是悬停、
等待、任务完成、返航或降落；当前共享原语没有表达的任务事件不能硬塞进STOP。

画面的编号、时间和来源、原始图像文件、位姿、提交UUID与执行端点都要绑定记录。
只有完整字段不会自动获得专家或训练资格。实测端点差也不能证明中间路径安全，或
证明该位移完全由这次命令造成。

## 原文件核验入口已可用

[入口脚本](../scripts/build_onboard_executed_trajectory_bundle.py)要求显式manifest SHA，
重开原文件检查字节／SHA与JSONPointer字段，保留原始JSON数值表示。输出只能写到
新目录，写后核对预期字节；同一次提交不能换个step编号重复计数，同episode不能换
飞机或时钟。107项合同＋33项集成测试通过；连同CPU诊断20项，独立160项通过。

已经有符合新合同的源记录时，PowerShell入口如下。这里的路径是使用格式示例，
不是声称仓库已经存在这些真实教师记录：

```powershell
$manifest = 'D:/Projects/Multi-Detect/artifacts/teacher-run-new/manifest.json'
$manifestHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
& 'C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe' -I -B -X utf8 'D:/Projects/Multi-Detect/scripts/build_onboard_executed_trajectory_bundle.py' --manifest $manifest --manifest-sha256 $manifestHash --artifact-root 'D:/Projects/Multi-Detect' --output-directory 'D:/Projects/Multi-Detect/artifacts/teacher-bundle-new'
```

输出`steps.json`和`source-audit.json`。后者的training_ready／train_admitted仍为false：
文件哈希认证内容没有变，不认证生产者、专家标签、像素、执行因果或飞行权限。
本入口不构造Actor、不运行模型、不拟合归一化、不发送飞控。

完整字段与纯软件例子见[合同](../src/multidetect/autonomy/onboard_executed_trajectory.py)和
[集成测试](../tests/test_build_onboard_executed_trajectory_bundle.py)。源记录应按
pose／events文件 → frames文件 → outcome文件组织，frame中不写自己的文件SHA，
避免自哈希循环。坐标转换后的记录必须保留原始来源和转换证据，不能重标原wire。

## 下一轮真实采集怎样接

当前只观察入口明确不允许飞控，不能往其callback加命令。现有multirotor编译器每次
生成完整起飞→航点→盘旋→降落，不能每个小动作都编译一次，再把整架次位移算成
这一个小动作的反馈。

第一轮应新建独占Gazebo采集入口：一次受审teacher Mission、一次起飞，按真实Mission
sequence和实际稳定端点切分动作。保存上传／readback／commit／permit、唯一事务、
wire hash、sequence、实际发送／ACK以及后继画面。ACK或航点reached不等于稳定端点。
需要完整姿态、原sensor stamp、pose／RGB同步及全路径安全来源；不能用单像素深度
假装已覆盖整条路线。SIM世界几何可以为独立teacher／SIM安全检查提供资料，不进入Actor。

已有VTOL飞行相机入口只能作宏观飞行校准，它固定机型和场景，不是x500升降／横移／
转向的小动作入口。持续空中闭环还要新增受审航段执行seam，不能靠反复整架次起降。

真实教师采集完成后，才做专家与世界分组审核、学习型三维动作输出、训练和完整未见
world验证。当前软件回执见[记录](evidence/onboard-executed-trajectory-software-20261008-r1.json)，
模型诊断见[动作头结果](onboard-navigation-head-diagnostics-20261008.md)。这不是实际飞行、
Jetson或正式发布完成。
