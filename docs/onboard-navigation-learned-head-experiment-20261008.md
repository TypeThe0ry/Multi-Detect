# 同一导航底座上的学习型动作头：有界替代实验

日期：2026-10-08。本文只给出已读源码支持的实验方案，没有实现、训练或运行动作头，也没有测得它的延迟。当前生成式导航路线和默认模型不变；正在进行的CPU86与NEW267工作有各自计划，本文不修改或授权它们。

## 为什么研究这个变化

用户需要模型及时判断下一段行动，再根据新画面继续判断。模型先看图、随后逐token输出完整JSON，可能把大量时间花在文本表达上；动作头则在一次看图和语言融合后，直接学习预测动作与尺度，宿主只按协议包装结果，不决定方向或填固定距离。

现有两种计时必须分开读：

- [官方CPU输入生命周期诊断](evidence/onboard-navigation-closed-sample-cpu-profile-20261008-r1.json)中，5个样本的新闭合路径平均16.74秒，属于约15秒级的离线训练输入编码与来源校验，包含prefix/full及标签路径。旧→新固定顺序、各一次，不能证明普遍加速，也不是实时相机推理的必需开销。
- [完整生成基线](evidence/onboard-navigation-streamed-baseline-phase-20261008-r1.json)中，旧LoRA平均6.3428秒、p95为9.1591秒，生成57–96个suffix token。计时从prefix移到GPU之后开始，包含generate、同步、token回传和decode；未单独拆vision/prefill/增量decode，也不包含CPU编码。

二者不是同一次端到端测量，不能相加声称实测总延迟，也不能把6.34秒全部叫作JSON解码时间。若实际在线链仍要串行等待这样的输入处理和生成，就无法及时消费持续变化的画面；但在线prefix-only路径到底多快仍待实测。移除文本生成也不会自动移除图像处理、视觉模型、语言prefill、来源准入和执行前核对。

## 可复用的特征接口

现有[NavigationBackend](D:/Projects/Multi-Detect/scripts/train_onboard_vlm_navigation_pilot.py:369)使用本地Qwen3-VL-2B及语言q/v LoRA。固定模型[配置](D:/Projects/Multi-Detect/artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203/config.json:15)给出2048维文本特征、28层和151936词表。

本地固定版本[Qwen3VLModel.forward](D:/Projects/Multi-Detect/.venv-vlm/Lib/site-packages/transformers/models/qwen3_vl/modeling_qwen3_vl.py:1223)完成图像embedding、DeepStack及语言融合，返回最终归一化的`last_hidden_state`；[条件生成forward](D:/Projects/Multi-Detect/.venv-vlm/Lib/site-packages/transformers/models/qwen3_vl/modeling_qwen3_vl.py:1362)随后才运行词表`lm_head`。该modeling文件当前SHA为`dd63ed3b124232735b3dca1bfa28f9d6b0d3f7182afcb75dde8f3e724b2b22da`。

新实验可经[PEFT get_base_model](D:/Projects/Multi-Detect/.venv-vlm/Lib/site-packages/peft/peft_model.py:944)取得已装入LoRA的条件生成模型，再调用其完整多模态`.model`，取prefix最后一个有效token的2048维特征。不要仅调用裸`language_model/get_decoder`而漏掉视觉融合，也不必保存所有层的hidden states。普通LoRA在未传`adapter_names`时的[混合adapter hook为空操作](D:/Projects/Multi-Detect/.venv-vlm/Lib/site-packages/peft/tuners/lora/model.py:448)；这一静态事实不替代实际层状态、参数身份及特征路径的验收。

## 输入与可学范围不变

仍用冻结[Actor消息](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_navigation_actor.py:246)：原任务、至多一张最近过去RGB、当前RGB，顺序和标记保持一致。仅用prefix抽特征；gold只进入loss，不从含gold的full chat末端取特征。外层scope、源帧时间、位姿、来源回执不塞入Actor。每请求使用新的prefix，`past_key_values=None`、`use_cache=False`，不跨请求缓存KV/视觉特征；需要独占引擎，避免共享可变状态。

当前[原86条标签](D:/Projects/Multi-Detect/artifacts/training/onboard-openfly-navigation-dataset-20261007-r2/samples.jsonl:1)只有70条前进`[3,0,0]`米、6条左转−30°、7条右转+30°、3条序列停止，无未知尺度、其他距离、垂直或横移动作。均衡重复曝光不增加这种覆盖。新82张多场景原图及几何检查也不等于已验收训练集或独立质量结果，不能借其名称补出尺度多样性。

最小首轮可训练四分类`forward/turn_yaw_left/turn_yaw_right/sequence_stop`，加条件距离与转角幅值回归。距离、角度由head学习，不硬编码3米或30°。左右转是旋转机头，不是左右平移；类名到FRD轴与yaw符号的转换只是协议表达。若扩展一般三维行动，需要真实FRD三分量、有符号yaw及unknown标签，使用相应条件回归和有效标签mask；目前不能宣称已学会任意尺度、未知量判断或全任务完成。

## 最小训练、保存与重载变化

首轮先精确加载既有base＋LoRA并冻结二者，仅训练小FP32 head；用类别均衡损失及条件尺度损失，记录各类错误，不让70条前进再次掩盖转向与停止。后续是否联合更新q/v LoRA，必须另立预算及检查点，不能混入当前冻结训练。

保持head为sidecar，独立保存`navigation_head.safetensors`与配置，绑定base/LoRA文件及实际参数身份、特征位置/维度、类别顺序、单位、head结构和训练来源。[旧audit](D:/Projects/Multi-Detect/scripts/train_onboard_vlm_grounding.py:266)只允许语言q/v LoRA；[旧save/reload路径](D:/Projects/Multi-Detect/scripts/train_onboard_vlm_grounding.py:532)不保存新head，不能把旧adapter重载通过当成整个新模型通过。将head注册进旧模型还会改变现有base digest含义。

新bundle应独立核对head文件SHA、实际加载tensor名称/shape/dtype/bytes及参数摘要，确认base＋LoRA身份不变，并在同一prefix上比较保存前后的实际head输出。若联合训练LoRA，则新adapter和head都要保存、重载和绑定；只保留head配置或预加载身份字符串不够。

## 结果协议与飞行边界

可复用[PlannedNavigationTarget](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_navigation_inference.py:80)七字段及finite/范围/null约束；不补confidence/reason，不把未知变零，stop不是到达、悬停、降落或返航。head输出越界或非finite应拒绝，不能事后偷偷裁剪成合格动作。

现有[NavigationInferenceResult](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_navigation_inference.py:287)要求真实生成token、数量及raw decode。不能把宿主生成的JSON伪装为模型文本，更不能填假IDs。head应有独立结果provenance：实际原始logits/尺度输出、head weights/config SHA、prefix/Actor身份、request/scope/来源绑定、完成时间与耗时。复用[实验backend的准入思路](D:/Projects/Multi-Detect/scripts/onboard_navigation_inference_backend.py:119)，保留期限、帧龄和实际adapter状态检查；它仍不授予transport或飞行权限，不补缺失姿态/高度，不新增飞控。

## 连续推进与换模型的公平对照

| 路线 | 能继续复用什么 | 新成本与边界 |
| --- | --- | --- |
| 当前生成式导航 | 已保存LoRA、共享Actor、真实token/raw诊断 | 仍要验证延迟、尺度、转向/stop和独立质量；现有KV cache已经在用 |
| 同底座学习型head | 同一输入、base＋LoRA、来源与目标契约 | 要实际训练、独立重载及新结果协议；仍有视觉/prefill成本，无现成速度收益 |
| 已核实公开权重的更宽导航模型 | 可能提供更成熟导航能力或离线教师 | 不是当前LoRA的叠加；输入、动作/单位、外部oracle、许可、内存和部署重新验收 |

第三条仅以[既有官方发布查验](onboard-pretrained-navigation-model-options-20261008.md)为候选依据；公开metadata不等于本地加载，论文Jetson或远端服务器数字不替本机实测。先比较第二条不等于保护现架构，也不排除第三条；任一路线达到目标都应按证据选择，而非默认晋升。

## 怎样证明真正更及时、更正确

在同一真实硬件、固定权重/processor与同一Actor输入上配对生成式与head：分别记录CPU准入/预处理、H2D、视觉＋语言prefill、增量decode或head、D2H/包装；再测源帧到被接受动作的总p50/p95、超期率、主机内存和显存。明确cold/warm顺序及同步边界，不把进度日志间隔或OS全生命周期峰值当成独立阶段指标。

用去重、跨场景的独立划分比较方向、FRD误差、yaw误差、stop/unknown失败和安全拒绝率。保留旧严格评分与新head数值指标的区别：宿主保证JSON格式，不是模型格式能力变好；常数标签拟合，不是任意尺度能力。随后才验证Gazebo真实画面闭环、连续任务和Jetson端到端迁移。本文不产生实现/训练/延迟验收；quality、promotion、execution、SIM/REAL、硬件及release门仍未通过。
