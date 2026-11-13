# r11-r2：仅准备的小目标训练候选

**未启动第二个训练，不切换现役模型。** 等r11完整训练、448评测与672成对评测的真实
结果，再由主协调者决定是否值得启动。这里完成的是CPU数据与待启动工程准备。

## 与r11的差别

`candidate_zoom_v2`给全帧加候选放大图；`slow_system_runtime.py`的候选输出固定224，
原图裁剪边长为`max(64, 3×框长边)`。r11把全帧448提高至672，但候选放大图仍224。
纯粹再训同样数据未必补到小目标错误。r2保持部署/训练默认448进行对照，新增两类
训练信号，并保留全部28,473条r10原始train replay：

- 原始焦点短边按现有评测计算方式`<12px`的训练图片，重取景为
  `max(32, 2×框长边)`、输出224，焦点框同步变换。裁剪只按可观察的focus_track_id
  取框，绝不按assistant_target选择目标。正例与拒绝负例各512；二者接受完全相同的
  几何变换，避免“单候选裁剪即正例”的额外偏差。
- 从r10未采纳的uncapped r9r **train**补充同target_id、同精确task_text、完整四视角
  的样本组。每种极性151组×4=604条，正负等量。这里不把中英文任务文本交替当成
  纯视角配对，也没有拿冻结四视角test或r9r val的错例回灌。

原r10合格标签焦点样本中小目标正例1,053、负例3,466；train-only合并候选池可用
小目标正例1,163、负例3,962。上述是明确标签白名单的分母，不代表所有训练行。
最终train **30,705**行，新增2,232行：1,024裁剪+1,208四视角replay。133个训练world
提供增量。val/test逐字节复制原r10版本，未增加或改写评测数据。

裁剪只能改变观察尺度、上下文和采样权重，不能创造原图缺失的细节；丢上下文或放大
模糊目标可能增加误收。8–12px附近的浮点边界按现有评测短边公式选取，索引保留
原坐标和测量值。是否有效必须看冻结评测，不能把数据量增加当成召回提升。

## 实际CPU产物与隔离证据

路径：`artifacts/r21id-r11-r2/prepared/`，约61MiB。

- `manifest.json`：配方、输入SHA256、输出SHA256、世界/视角/极性统计。
- `additions-index.jsonl`：逐条原sample_id、world/view、原图SHA、原图裁剪边界、输出SHA。
- `train.jsonl` SHA256：`cf75ae573df90fc8f441a0a8eb556592acd9ba4104b92f9debdf3fc2e46db9f0`。
- 新增样本对所有指定heldout按sample ID、原图路径、world三层隔离；冻结planning-v5
  126题、返航15题对完整文本及ID零重合。只有既有train进入采样，不改split来吸收验证样本。
- 原train中1,500条普通规划replay保留；不是上述冻结126/15题。None图像不当作一张
  共同的图片做泄漏检查，文本规划行也不冒充实体场景world。

真实CPU准备服务`md-r11-r2-cpu-prep`，PID1438321，40.256秒，日志
`runs/r11-r2-cpu-prep.out`。systemd记载内存峰值588.5MiB；MemoryMax=64GiB、SwapMax=0、
CUDA_VISIBLE_DEVICES为空。没有采集SITL、调用模型、操作GPU或结束已有进程。

## 用户门槛比较器

`compare_identity_user_gate.py`要求两种明示口径分开生成结果：

1. **model_control**：448/448或672/672，同处理与分辨率，描述权重对照。
2. **deployment_improvement**：可用现役r3-448对计划部署r11-672；必须明确配置和算力
   证据及SHA，标注包含处理收益。不能称为纯模型权重收益，也不能混用某些指标448、
   另一些672来假装一个部署配置全部过门。

每个口径必须覆盖v1、v3、冻结四视角、r9r val；同组预测sample ID/标签与输入完全对齐。
比较器从原始预测重算，校验实际命令、分辨率、裁剪、默认/显式variant、评测器/输入处理
代码、mmproj和结果/数据SHA。旧harness没记录输入来源时是待验收，不能自动猜448。

用户判断严格为`<12px`召回相对r3至少+10pp，其他已观测身份指标零退步，**没有旧2pp
退步容忍**。干扰物拒绝率越高越好；真目标误拒率/干扰物误收率越低越好。
旧四视角0.90、误收0.026等绝对门槛完整单列为diagnostic，不参与这次用户规则。

`pass_observed_scope`只是可比较样本上点估计满足；缺失组/处理来源/必需分母为0时
`pending_evidence`，缺样本专项不会伪标pass，小分母与置信边界另外列明。无观测的消防栓
专项不能据此声称通过，低样本恢复率也不是可靠总体结论。工具永不自动替换模型。

`evaluate_identity_user_pair.py`默认**仅打印8个实际待执行命令**，不会启动GPU。
以后明确传`--execute`才跑本地成对评测，并在执行前后核对输入/代码，自动记录真实
processing provenance及comparison-bundle。这避免事后为旧结果补写未经核实的处理来源。
部署口径还需`--deployment-json`提供`config`、`config_sha256`、`resource_evidence`、
`resource_evidence_sha256`及说明；最终应对应真正接入的运行配置。

现役assembly的身份runner尚未转发max_side配置，仍用448默认值。若采用672，须另行
接通配置、测算资源并回归；仅修改模型路径不能证明实际已部署672。

## 待启动队列

`queue_identity_r11_r2_prepared.sh`没有加入jobs.d，也未执行训练。默认直接退出；必须
先有r11与hires-pair完成标记、两个已核实比较报告、显式review JSON与`START_R11_R2=1`，
且进程实际处于MemoryMax≤64GiB的cgroup。启动前校验全部源/数据/裁剪图哈希。

review JSON包含decision=`train_r11_r2`、reason、r11_default_comparison及其_sha256、
r11_hires_comparison及其_sha256、prepared_manifest_sha256。由实际报告决定是否训练，
不能因脚本已经准备好就越过审阅。

后继已写进同一脚本：GPU1的r3热启动1epoch、rank16/alpha32/LR5e-5/accum4、BF16、
448/candidate_zoom_v2→导出→四套448/448成对评测→用户比较报告。只用GPU1，保持64GiB
上限；不自动覆盖现役配置。数据和训练方案的其它参数见配置文件。

## 验证结果

- 数据准备与比较器10项测试通过；包含world/ID/图像隔离、正负平衡、裁剪几何不读标签、
  输入/输出provenance、匹配和部署两种口径、处理跨组不可拼接，以及恰好+10pp的浮点边界。
- 浮点比较只有1e-12机器精度容差；0.8→0.9通过，0.6→0.69999999仍失败，没有统计性退步容忍。
- 独立CPU onboard gate PASS（128s），日志`runs/r11-r2-prep-cpu-gate.out`。
- 训练脚本缺少显式启动标志时exit2；pair工具dry-run打印8条命令且不创建推理输出目录。
  二者没有启动训练或推理进程。
