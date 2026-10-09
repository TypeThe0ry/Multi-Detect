# 把三维相机与实时 OnboardAI 接起来

## 本轮完成了什么

新增实际 Gazebo 相机入口：每张新画面先进入独立图文推理分支，不再由旧检测器是否
发现新类别来决定 AI 能不能看到它。原文 Prompt、有限窗口连续 RGB，以及同一来源
报文覆盖的 GPS／姿态／速度等部分状态，进入同一次请求。模型在独立进程计算，
相机回调不等待加载或生成；普通新帧不反复取消尚未完成的推理。

本轮是接入代码与自动化验证，不是已经能自主完成任务的版本。任意 Prompt 可以进入
研究分支，但还没有变成任意任务的飞行权限、可行航线或成功证据。默认 r33 和原 COCO
检测器不替换，质量失败的图文 LoRA 不自动加载，没有进行新的动作训练。

## AI 看到了什么，哪些仍不知道

RGB 与原 BGR、报文、时间和状态精确绑定。记录器核对请求状态与已落盘源观测一致，
另一份内部合法但 GPS 不同的报文也会拒绝。完整来源保留在磁盘，模型只读实际覆盖的
事实；Gazebo 隐藏真值、训练标签和配置默认值不冒充实测状态。

HOME、飞机模式、电量、健康、风、净空及未经测量的 GPS 质量仍未知。这也是目前不能
把新分支直接交给飞机执行的原因之一。每次最多读取 1–4 帧，默认两帧；长记录不按
四帧切断，相同像素的新帧也保留。这是有限视觉历史，不是已经接通完整任务记忆和
实际动作反馈，缺失的执行历史不补造。

## 怎样核对处理过程

每次已确认提交的请求、原始回答、结构化提案和 backend metrics 独立保存。不合法 JSON
留下原字节及拒绝事件，不补答案。拍摄、接收、处理、模型完成与取回时刻分别记录，
不把旧图时间改成当前时间，也不把模拟器纳秒时钟当成主机时钟。

独立审查用官方 pinned processor 实际发现四帧预处理后可能超设定总像素约 43%。
新分支限制每图预算，并在生成前核验真实 image grid 总像素；两帧／四帧的真实 CPU
预处理通过。成功请求保留指标；冻结 worker 的子进程前处理异常只传异常类型，仍缺
该失败路径的完整 tensor/grid 回执，不能写成已解决。

模型提案、Mission 接受、飞机执行和任务完成是四件事，不能互相代替。记录器可接已有
typed 回执，但本轮没有飞行回流、合格 expert-action 标签或通信权限，`training_ready=false`。

## 实际验证结果

联合 444 项：443 通过、1 项 Windows symlink 权限跳过，原生退出码 0。十个新增 Python
文件 Ruff check／format-check 通过；真实入口预检退出 0、`planned_not_executed`，没有
启动模型、容器或飞行。测试包含相机／IPC／事务夹具和真实官方 CPU 预处理，不是新的
GPU 语义排名、Gazebo 飞行架次、Jetson 或硬件验收。

三名子智能体分别完成模型接缝、长记录和独立审查，主线程完成入口与联合验证。
最新远程诊断在 2026-10-07 07:01:57 UTC 观察到 Linux worker 离线，Controller 返回
`no_eligible_node`，没有派发新采集。旧镜像准备成功、被动采集失败证据保留不改。
[本轮封存证据](evidence/onboard-gazebo-live-shadow-development-20261007-r1.json)。

## 入口与后续顺序

仓库根目录可运行无进程预检，输出目录必须是新的：

```powershell
.venv-vlm/Scripts/python.exe scripts/run_onboard_gazebo_live_shadow.py `
  --prompt "检查前方是否有可安全观察的目标" `
  --snapshot "D:/Projects/Multi-Detect/artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203" `
  --model-sha256 7de1838c87a5349b016c26a1c3f7d2bc400a3d485f95ef39a7059ffd734977a0 `
  --output-directory "artifacts/evaluation/onboard-live-shadow-my-preflight"
```

`--execute` 才预热模型并启动受控 Docker 相机采集，目前未执行通过。
`--baseline-flight` 是另一个显式选择，仅让已有 r33／YOLO 路径在一次性 SITL 飞行，
新 VLM 仍旁观。baseline 飞了不证明这个 Prompt 被执行；保留 RGB／Depth 原载荷在启动
前限制为 1 GiB，这不是峰值 RAM 保证。UDP 14550 不接管。

下一步：保留原时间的真实 RGB／wire 做新接口 GPU 回放；恢复 Gazebo 后跑实际相机
请求；补实测 HOME／飞行状态／净空与同架次行动—反馈；接既有 Mission 编译、上传、
读回、许可和执行监督；审核独立训练／验证／测试轨迹后训练飞行动作。

目标仍是一次 Prompt 后自主完成任务，不是用户逐次批准方向。还需连续重规划、异常
处理、完整任务质量、SIM／Jetson 同模型与合同、HIL／REAL、室外 GNSS 和距离精度验证。
保持 `0.3.0-pre.8`，不晋升候选，不发布最终 `0.3.0`。
