# OnboardAI：把下一段航线交给飞控

更新时间：2026-10-07。当前产品仍为 `0.3.0-pre.8`。

## 本轮得到的能力

AI 看图后提出了一个目标，无人机的位置可能已经改变。新的开发接入路径保留模型当时
选出的真实地理目标，在确认暂停后用新收到的位置计算下一段航线，再上传、读回、
选择下一航点并请求恢复 Mission。等待、上传成功和模型自称完成，都不会被记成已飞到目标。

这是一条可调用、可取消的主机接入路径，不是已经完成的 Gazebo 自主飞行产品。
目前实际联调用的是独立软件 peer：报文通过真正的本机 UDP socket，并使用 MAVLink2
编码与校验，但 peer 没有飞机动力学。原 VLM 回答、原 Mission 授权和净空仍是明确标出的
测试数据，不能当作三维场景实测或用于动作训练的飞行轨迹。

## 接入路径

```text
原任务编译 → 原主机编写记录 → 原 Mission 事务与授权
                                      ↓
保留原图像、回答、时间和目标 → 单次暂停尝试
                                      ↓
真实收包确认 HOLD → 较新位置与原始导航事实 → 新完整航线净空审查
                                      ↓
独立子 Mission 授权 → 上传 → 完整读回 → 主机事务提交
                                      ↓
选择 cursor 1 → 请求 AUTO.MISSION → 新收包确认模式
```

原编写记录必须在原事务之前建立；封存原 `START_PERMITTED` journal 后，仍需主机独立
注册来源。记录的 hash 不证明来源可信，默认受信来源为空。封存不覆盖同名文件，不回填
早期时间。Windows 断电后的目录持久性尚未验证。

子航段为“较新当前位置 → 原目标 → 有限驻留”，不偷偷插入起飞、返 HOME 或降落。
子授权须绑定同一任务、Prompt、机体、车辆、当前状态、Plan、围栏与完整 wire；新航线的
净空不能沿用旧航线的 hash。主机的 commit 是本地 journal 提交，不虚构第二次 PX4 commit。

取消会永久退休本次 token。每次发送前重新核对 generation、当前主机门、期限、人工模式
和原始事实的新鲜度；已经在 sender 内的调用不能伪装成“从未发送”。旧队列里的 HOLD
或 cursor 留作原报文证据，不作为本次发送的确认。空队列判断还检查实际 socket，避免
零 timeout 在读取之前返回 None。队列 cut 不证明特定 SET_MODE 的车辆端因果关系，回执
保持 `command_causality_proven=false`。

包括 ITEM 回复在内的所有发送都先检查排队报文；请求后出现的人工模式会阻断下一次
航点写入。保守 drain 会保留但不重放已排队的重复或流水化请求，可能触发有界重试或
recovery。这是可用性限制，不能为了继续上传而跳过人工接管检查。

原 HOLD 心跳、空中状态、cursor、HOME、参数和位置各保留原接收时间。每阶段完整重验，
每次写入仍检查这些原时间与编译 context/parent 的期限；较新的 wire 心跳不会刷新旧
净空或旧位置。当前开发 compiler 默认 1 秒窗口且只能收紧，慢链会拒绝恢复；它尚未
经过真实机体、网络和推理延迟标定，也不是“1 秒是物理定律”的声明。

## 验证与当前环境

软件测试覆盖原编写记录、单次 HOLD 准入、严格 wire 事务、取消、到期、错误读回、ACK
丢失、人工接管和回执不可变性。实际内核 UDP 联调另保存原始 bytes、SHA、来源、接收
序号和时间；它与纯 Python 夹具分开计数。精确测试结果、源码与产物身份见
[本轮封存回执](evidence/onboard-continuation-host-development-20261007-r1.json)。

本地最新只读诊断中 Docker 客户端可用，daemon 查询仍超时，状态未知；当时 Docker
Desktop/backend 进程存在，不因此声称 daemon 健康或镜像可用。未重启服务、移动 Docker
数据、检查镜像或启动 Gazebo。此前远端 Snap launcher 权限失败的原证据保持不变。
追加独立 15 秒只读查询仍超时；原短探测器拒绝超出 5 秒的参数，这次没有修改它。
短探测器 native2、长查询的 shell invocation1 和 Docker 自身无退出码分别保留，
不把超时或生成一份诊断 JSON 记成 daemon 查询成功。

## 还缺什么

- 真实 Gazebo 3-D RGB、实际 PX4 导航事实及同源整段净空 producer；不是软件 peer 的坐标。
- 独占在线 writer/来源注册、发送前持久化消费和崩溃恢复；当前 token 只在进程内一次使用。
- 已恢复子 Mission 再成为下一段父 Mission 的连续链，以及 HOLD 超时监督与真实返航。
- 原 whole-sortie dispatcher、在线回执与下一次 VLM 记忆自动接线。
- 完整、同步且来源合格的 `RGB/state/action/dt/next RGB` 飞行 episode，再进行动作微调。
- 任意任务语义、Jetson 延迟与内存、HIL/REAL、室外 GNSS 及最终发布验收。

不会因本轮通信测试替换 r33 默认策略、COCO detector 或晋升失败 LoRA。VLM 和世界模型
是已有算法的候选扩展，选型与实际权重状态分别见
[VLM 研究](research/onboard-vlm-candidates-20261007.md)和
[世界模型研究](research/onboard-world-model-candidates-20261007.md)。本轮没有新增训练、
CUDA 推理或世界模型运行；当前证据不能证明无人机已经自主完成任务。
