# PX4 lockstep GPU 默认运行镜像推广

本变更只在隔离分支准备。合入前必须由维护者核对同一候选内容的 w0000 原始 10 次、21 任务各 2 次，以及三角翼 fixed/gimbal 两个严格门禁全部通过。组件测试或部分实景通过不授权提前推广。本次没有修改现役 env/main、运行快照、Docker 标签或已有作业。

## 已查明的入口与缺口

- `src/multidetect/simulator/px4_image.py` 的旧 `PINNED_IMAGE` 是 bcf 上游镜像。它还被 gimbal/airframe 资产来源契约使用，不能直接改成新运行镜像。
- `scripts/run_simulator_docker.py:build_docker_simulator_plan` 原来以该值为形参默认；即使随后实际选择了 GPU，镜像也没有随之改变。CLI 同样提前绑定旧值。
- `scripts/collect_sim_teacher_episodes.py:CollectionPlan` 原默认相同，CLI 只有 `MULTIDETECT_SIM_IMAGE` 显式覆盖才改变镜像。nightly 与 smoke 最终进入该 collector。因此验收作业 export 候选镜像不能修复普通新启动的默认值。
- `simulation/docker/gpu/Dockerfile` 仅在 bcf 加 EGL 图形依赖，没有应用 scheduler 修复。旧构建脚本还以可变 `:gpu` 为构建基底。

实际只读 Docker inspect 证明：bcf 有 8 层，445 有 9 层且前 8 层完全相同；b5 有 10 层且前 9 层完全等于445。入口均为 `/opt/px4-gazebo/bin/px4-entrypoint.sh`。候选层仅复制 PX4 二进制，见此前构建/ready 证据；这次没有重建或重新飞行。

## 最小推广方式

库/API、CLI、collector 共用 `resolve_runtime_image`。选择顺序为显式 API/CLI image、`MULTIDETECT_SIM_IMAGE`、实际 GPU/CPU 默认。先按既有规则解析 GPU（包括显式 device），GPU 默认选完整不可变本地 ID：

`sha256:b5c576ce15b8a75935313bc3e8dc4cf9940778226b28e835064d7581c3fe72c6`

`CollectionPlan` 创建时冻结实际 image，原始 metadata、`collector_container_started` MAVLink 头和 Docker argv 使用同一值；之后改变环境变量不会使命令与记录分叉。Docker/AI acceptance 的默认入口也允许中央选择。`PINNED_IMAGE` 保持 bcf 作为来源契约，避免错误宣称资产来自新镜像。

原镜像 A/B 保留，例如 GPU 作业用 `--image multidetect/px4-sitl-gazebo:gpu` 或 `MULTIDETECT_SIM_IMAGE=multidetect/px4-sitl-gazebo:gpu`；需要不可变旧内容时可显式指定 `sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2`。显式 API 参数高于环境变量。缺少 b5 本地镜像时不能静默退回旧镜像；必须恢复原候选镜像或审核重建内容。

仅更新持有本次源码的后续启动会改变默认值。旧 worktree 和已创建容器不受影响，没有全局 retag `:gpu`。若 launcher 仍显式 export 旧镜像，它继续作为明确覆盖生效；推广时必须核对启动环境，而不能仅看源码默认。

CPU 默认仍为 bcf，**尚未覆盖本次修复**。交互 browser viewer 的独立软件渲染镜像、旧的固定镜像验收/回放工具也不在本次 GPU 默认推广范围。445 的额外层是图形支持，因此未来可评估给 CPU 生成同一二进制覆盖层，或验证 GPU 镜像在无 NVIDIA 请求的软件渲染模式运行；两者均需 CPU 原始任务门禁，此次不切换。

## 固定来源和安全重建

- PX4 commit `83c4f4e580558816f0e398918f66358988004696`。
- 最终 lifetime-r1 patch SHA256 `43bfe32d5d7572b866b3d411c2394d941b6bcc8e19ace5bdd6e230845cdf0559`；保留 native cond_wait，不是原样27a21488。
- 基底 `multidetect/px4-sitl-gazebo@sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2`，脚本另核对完整本地 image ID。
- 接受候选的二进制 SHA256 `9c2280e005ad426f01cdffddf2114e1e4b13e26aa9f5996c4b52cdfa9e722d99`。

复用 `scripts/build_px4_lockstep_candidate.sh`：先硬校验补丁，固定源码/子模块/基底，运行既有三方 RED/GREEN 与上游测试，然后编译、核对二进制并执行 ldd。默认8 CPU，最多12；64 GiB内存、内存+swap同上限，无GPU。运行镜像仅继承原GPU运行基底并COPY px4，不继承编译环境私有库。

输出标签改为每次全新 `lockstep-lifetime-r1-rebuild-<UTC>-<PID>`；拒绝既存目标，禁止写 `:gpu` 或当前候选标签。编译镜像也用唯一新标签，输出保存在 `clock-candidate-rebuilds/<stamp>/`，不覆盖原 `clock-candidate-artifacts` 的证据。以下是未来单独执行的重建命令，本次未执行：

```bash
PX4_BUILD_JOBS=8 scripts/build_px4_lockstep_candidate.sh /data/scratch/px4-clock-build-20261108
```

不承诺全 Docker image ID 可逐位复现：Docker 历史、编译器/apt/Python依赖和含调试信息的绝对构建路径会影响重建。固定源码/补丁不能替代二进制比较。重建二进制如不等于9c，脚本保留输出并在产生运行镜像前失败；需审查真实编译器、包版本、路径等差异并重新验收，不能降低预期hash或将新ID自动写入默认值。即使二进制一致，新镜像ID也须审核基底层、Config和唯一COPY变化，再显式更新源码默认，不能通过可变标签偷换运行内容。

## 验证和维护者推广核对

本分支运行无GPU的 pytest：GPU/CPU/显式device实际判定、API/CLI/env优先级、collector冻结metadata和命令一致性、拒绝改动补丁及覆盖已有镜像；并运行现有Docker planner、collector preflight和两层acceptance回归。现有回归及首批新测试共143项通过；补充两个活动标签拒绝用例后，新测试16项再次全过（合计覆盖145项）。仅既有模拟网络 sitl-private 的预期 inspect fallback warning。测试日志位于工作区 `runs/px4-runtime-default-focused-20261109.log`，不把测试结果冒充飞行门禁。

维护者在合入前确认三个实景门禁原始图像身份、源版本、无重试/缺失和严格断言，并确认运行内容与候选b5相同。当前飞行使用的21dc源码与本分支基底fd4316运行逻辑等价，本分支仅改变默认镜像选择和重建约束；显式b5验收路径应保持原内容。合入后用无override的GPU dry-run检查生成plan/collector头选择b5，再按正常推广流程处理main与发布。未全部通过时本分支保持隔离。
