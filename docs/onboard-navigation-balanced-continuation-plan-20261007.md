# 导航 LoRA 均衡续训：最小实现方案（2026-10-07）

本文保存一次基于当前源码和已封存产物的接口调查，避免后续实现再次套用旧实验的固定门限。

**最新状态（2026-10-08）：旧完整流程失败，新流式流程正在运行。** 旧入口238项测试、独立CPU86验收通过；完整GPU流程却在精确加载底座和旧LoRA后，编码缓存阶段触发主机`MemoryError`，native exit 1，没有开始推理或optimizer更新。原失败证据和冻结来源保留；它不是CUDA OOM，具体内存根因尚未证实。NEW流式入口及相关模块287项测试通过，取消86条完整张量驻留，每次访问只重新编码单样本；新CPU86 phase退出0，GPU基线已完成86条编码并进入推理。280 draw与独立重载尚未通过，当前不替换默认飞行策略。

可执行计划：[均衡继续微调计划](../configs/training/onboard-navigation-balanced-continuation-20261007-r1.json)。入口为 `scripts/train_onboard_navigation_balanced.py`。新输出位于 `artifacts/models/onboard-navigation-balanced-20261007-r1/`；先行 CPU 验收单独保存于 `artifacts/models/onboard-navigation-balanced-cpu-20261007-r1/`。这仍是同场景训练集回代，不是独立泛化、Gazebo 自动飞行或 Jetson 实机验收。

NEW流式计划：[单样本按需编码继续微调](../configs/training/onboard-navigation-streamed-continuation-20261008-r1.json)，SHA-256 `eac75d2def168f5daa44b3bba713456c71f80d00a1b91933b8075a1dd32c20cc`。入口 `scripts/train_onboard_navigation_streamed.py`，输出 `artifacts/models/onboard-navigation-balanced-streamed-20261008-r1/`。底座、旧LoRA、86条样本、既定280次抽样和optimizer预算未变；旧入口与旧计划没有改动。四阶段仍顺序执行，只有真实native结果、源绑定、日志与诊断闭合后才写完成。

[NEW CPU阶段回执](evidence/onboard-navigation-streamed-continuation-cpu-20261008-r1.json)绑定实际CPU报告、进程观察、86条prepared记录与287项测试；独立只读核验重新读取180项来源，共4,993,288,005 bytes，无drift。它只证明本次CPU阶段通过，不代表GPU流程终态；独立审计未重算tensor/labels，也未实测进程内张量驻留。

## 目标与已有依据

在现有 Qwen3-VL-2B 导航 LoRA 上增加训练能力，不重新建立另一套模型。新训练与新推理都使用已冻结的 `src/multidetect/autonomy/onboard_navigation_actor.py` 和同一官方本地 processor。

已有依据分别是：

- [既有 checkpoint 的真实训练与失败边界](evidence/onboard-navigation-trained-adapter-20261007-r2.json)：原训练完成 86 次更新并保存 adapter，但原运行 native exit 为 1。
- [独立进程精确重载证据](evidence/onboard-navigation-exact-reload-20261007-r1.json)：保存的 112 个 FP32 LoRA tensor 可在冻结 BF16 base 上精确加载；原训练失败和旧 supervisor 的 PID 检查失败均保留，不改写为成功。
- [280 步均衡序列](evidence/onboard-navigation-balanced-schedule-20261007-r2.json)：86 个唯一训练样本，forward/left/right/stop 各 70 draw；这是数据准备，不是 optimizer 授权或训练。
- [新共享 Actor 的官方 CPU86 验证](evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json)：在相同保留离线源的两条输入工厂之间，86 对 prefix、344 对实际 CPU tensor 记录一致；没有模型推理、实际 live-camera 或 SIM/REAL 验收。

续训起点是 `artifacts/models/onboard-vlm-navigation-pilot-importpath-20261007-r2/adapter/`。预期参数 digest 为 `e50249e3e9c87e0cdef6e7207ab75c2930fae6d17c10d05ff68aec652d0638ca`；它与 adapter 文件本身的 SHA-256 是两种不同的绑定，不可混用。原始 BF16 base 参数 digest 为 `c04c7a37e3a717329d2f6b06c9fd7109f431e938e4d201c319e819662113aac6`。文件长度和文件 SHA 以以上证据及下一轮实际复验为准，不在本文复制完整 hash 表。

## 最小新增入口与逐阶段进程

本轮已新增以下入口、编码、日志和定向测试文件：

```text
scripts/train_onboard_navigation_balanced.py
tests/test_train_onboard_navigation_balanced.py
scripts/onboard_navigation_balanced_encoding.py
tests/test_onboard_navigation_balanced_encoding.py
scripts/onboard_navigation_balanced_journal.py
tests/test_onboard_navigation_balanced_journal.py
configs/training/onboard-navigation-balanced-continuation-20261007-r1.json
```

新增 CLI 使用标准库 supervisor 和顺序隔离的 CPU／GPU worker：

```text
源码、计划、数据、schedule、checkpoint 和 runtime admission
  → CPU worker：完整86个训练chat／prefix／response-only标签校验
  → baseline worker：新 Actor 下的 base / e50249 checkpoint 基线
  → train worker：加载 e50249，fresh AdamW，执行既定 280 draw
  → reload worker：新进程精确加载新产物并复核生成结果
  → supervisor：native exit + journal + source 前后绑定 + 进程身份验收
```

三个 GPU worker 顺序启动，上一进程终止并验收后才启动下一进程。不能再次在旧训练 `run()` 内构造第二个 GPU 模型来完成 fresh reload。任何阶段失败都保留独立报告、原生退出码和已发生的更新记录；不能自动重试训练以掩盖失败。

外部 CLI 只接显式 plan、plan SHA、输出和 supervisor 目录；内部 `--worker --phase baseline|train|reload` 由 supervisor 产生。不提供任意替代 checkpoint、任意超参数或自动 fallback。

输出必须是预声明 artifact 根目录下新的直接子目录；子阶段目录只能由本次 supervisor 管理。拒绝已有目录、越界路径和嵌套旧证据目录，不向 base、旧 adapter、dataset、schedule、review 或冻结源码目录写入。

Windows venv 的 `Popen.pid` 可能是 launcher，不是实际 Python worker。下一轮须采集**本次** live CIM 的可执行文件、完整 argv、父子链、创建时间和观察时间，与 worker 自报 PID、loader/constructor/warmup PID、stdout 及 native completion 绑定。不能复用历史 PID、取消 PID 门或只因报告写了 0 就接受。

## 可以复用的代码与不能直接套用的边界

| 作用 | 现有路径与函数 | 复用边界 |
|---|---|---|
| 原始数据和 gold admission | `scripts/train_onboard_vlm_navigation_pilot.py`：`load_plan()`、`verify_plan_sources()`、`admit_dataset()` | 传入原封存计划得到 86 个唯一样本；不能把新 280 步优化声明塞进旧计划。 |
| BF16 base / FP32 q/v LoRA 构造 | 同文件：`NavigationBackend.__init__()` | 同一 pinned snapshot、rank 4、alpha 8；构造后加载旧 default adapter，不叠加第二 adapter。 |
| 精确权重加载 | `scripts/verify_onboard_vlm_grounding_adapter.py`：`verify_config()`、`saved_parameter_digest()`、`load_saved_exactly()` | 校验 tensor 名称、112 个实际 FP32 tensor、shape、有限值及 digest；不转 dtype、不修复不匹配权重。 |
| trainable audit / loss / generation | `scripts/train_onboard_vlm_grounding.py`：`audit_trainable_parameters()`、`parameter_digest()`、`response_only_labels()`；backend 的 `loss()`、`backward_preflight()`、`generate()` | 新入口提供新 Actor 编码后的输入；generation 只消费 prefix，gold 仅用于独立标签和事后诊断。 |
| 内存、梯度、日志、诊断 | 旧导航 trainer：`check_memory()`、`finite_gradients()`、`append_event()`、`diagnose()` | 保留真实峰值、有限梯度和 fsync；诊断仍是训练集重代入，不是独立质量。 |
| 共享消息和实际 CPU 张量门 | `src/multidetect/autonomy/onboard_navigation_actor.py`；`scripts/verify_onboard_navigation_actor_inputs.py`：`official_processor()`、`validate_prefix_inputs()` | 所有新训练/推理统一入口；使用官方本地 processor，不把注入夹具报告当官方验收。 |
| streaming 读取机制 | `scripts/run_onboard_navigation_pilot_streaming.py`：`header_binding()`、`install_streaming_loader(allowed, openings)`；`scripts/onboard_navigation_streaming_storage.py` | 新入口自己绑定精确 allowlist：base、旧 warm-start adapter、本次新 saved adapter；保留实际 reader 审计。 |
| schedule 语义复验 | `scripts/build_onboard_navigation_balanced_schedule.py`：`build_draws()` | 用已语义审核的 targets 重算全部 draws/exposures 并比较；既定序列不能再 shuffle。 |

以下外层入口**不能直接复用**：

- `NavigationTrainingBudget.validate()` 要求完整默认预算与旧 100 步实验相同；其父类 `TrainingBudget.validate()` 也最多允许 200 步。新增独立 validator 明确授权 280 步，不修改或绕过旧 validator。
- 旧 `train_epoch()` 强制 86 个唯一样本并重新 shuffle；新增 draw loop 严格消费绑定 schedule。
- `scripts/run_onboard_navigation_pilot_supervised.py::journal_counters()` 拒绝重复 sample ID。新 journal 允许 schedule 指定的重复，不允许任意重复或遗漏。
- 旧 `NavigationBackend.encode(sample)` 使用旧监督消息，不能用于新 Actor。新增共享编码函数也应避免把同一可变消息对象反复交给 processor。
- `scripts/recover_onboard_navigation_adapter.py::reload_and_probe()` 固定要求旧 prepared inputs、旧 prefix 和旧 probe 输出一致，不能用于新协议的 candidate。
- 旧 streaming 的 `read_runtime()`、`checkpoint_paths()`、worker、supervise 和结果 verifier 绑定旧 contract/计划/输出。复用低层机制，新增实验 admission 与验收结构，不冒用旧运行身份。
- `scripts/run_onboard_navigation_pilot_streaming_no_warmup.py` 的旧报告声明 `training_budget_changed=False`。**新实验整体预算确实从 100 改为 280。** 新 wrapper/report 必须如实记录该变化，只能声明 loader 机制没有改变模型 dtype、量化、device map 或实际权重加载。不能把旧声明直接挂到新实验上。

allocator warmup 只可按绑定原函数源码 SHA 跳过可选 CUDA:0 预分配，不跳过权重加载。constructor 仍是原始初始化透传；`safe_open` alias、warmup 和 constructor hooks 都须在 `finally` 恢复，包括最终报告写入失败时。新实验使用新 schema 验证这些机制，不修改旧冻结实现或历史报告。

## 显式计划、统一编码与 optimizer 前门

NEW plan 至少绑定：原始 source plan、dataset/report/source review、r2 schedule、warm-start adapter/config、原训练报告和精确重载证据、共享 Actor source/contract/system prompt SHA、官方 CPU86 报告、新入口与测试、所有实际复用源码、解释器和实际 runtime 来源及版本、优化设置、诊断 probe 和 false claims。

新 CLI/test/runtime manifest 完成实现和定向 CPU 测试后才按实际最终字节封存 SHA；不存在的文件不能提前编造 hash。已知新 source 与产物绑定可查以上 durable receipts，运行前仍须重验。

新 validator 独立固定：280 draw / 280 optimizer step / 一个完整 balanced-schedule pass。唯一源样本仍为 86，四类各 70 draw。不得称为原 86 样本的单个完整 epoch；超过原预算是显式实验变更，不是原验收的延续。

首轮建议保持其他既有设置：rank 4、alpha 8、学习率 `2e-5`、clip norm 1、native BF16 frozen base / FP32 language q/v LoRA、单样本处理、图像 50176 像素、输入 4096 tokens、response 192 tokens、gradient checkpointing 和 CUDA reserved cap `8053063680` bytes。若改变，须写进另一显式计划，不以失败后自动降级改变本次实验。

原始 source review 用于数据 admission；schedule 的 `optimizer_authorized=False` 保持不动。本次新增计划须单独明确优化范围。若加独立训练 review receipt，令它绑定最终 plan SHA，并作为外部 admission 输入；避免 plan 与 receipt 相互绑定形成 SHA 循环。

新增共享编码函数的步骤：

1. 完成原始 86 个样本的 source semantic admission；均衡类别和 draw 信息不进入模型消息。
2. 从 `NavigationActorInput.from_supervision_sample()` 构造 actor，要求 source session 最终 verified。新推理使用 `from_observation()`，两者共用 serializer。
3. prefix 和 full **分别**创建新 messages、新 PIL 图像和新 `images_kwargs`；仅 full 追加通过原源码验证的 assistant target。调用后关闭本次图像。
4. 对 prefix/full 实际四个 CPU tensor 分别做 dtype、shape、image count、grid、finite、像素和 token cap 检查，不截断。
5. 要求 exact token prefix、attention prefix、visual grid 和 pixel bytes 一致，使用既有 `response_only_labels()`；prompt/图像/prefix 只可为 `-100`。
6. 保存 actor identity、prefix/full tensor SHA、labels SHA 和 target SHA。逐样本 prefix 必须与绑定 CPU86 报告一致。
7. 编码后的 source session 正常退出并最终 verified；baseline 后、创建 optimizer 前，再查计划、review、schedule、旧 checkpoint 和所有 source/runtime bytes。任何 drift 在 optimizer 创建前拒绝。

已通过的 CPU86 只验证新 prefix，不代替新增 full-chat/label 编码验证，也不证明旧 r1 prefix 或独立 live-camera 输入一致。旧 `onboard_navigation_supervision.py::_SYSTEM` 和 serializer 本来就有 system prompt、JSON 与导航协议；新 Actor 改变的是更明确的互斥/数量约束与共享消息边界。需要重测是因为实际协议消息不同，不是因为旧模型“没有 system prompt”或“未被告知 JSON”。

## 公平 baseline 与 fresh AdamW

统一新 Actor、官方 processor、prefix bytes、历史 RGB、dtype、SDPA、seed、generation 时间与 token 限制、gold 诊断。baseline 阶段不创建 optimizer。原四个 probe 只用于短 smoke 和 fresh-reload token 复核；本轮训练效果必须对**全部 86 个唯一训练样本**做成对诊断，不能只凭四例宣称改善：

- `base_new_actor`：同一模型用 `generate(parent=True)` 关闭 adapter，覆盖全部 86 个唯一样本。
- `checkpoint_e50249_new_actor`：精确加载旧 FP32 default adapter，用 `generate(parent=False)`，覆盖同样 86 个唯一样本。
- `candidate_new_actor`：280 步后覆盖同样 86 个唯一样本，仍用相同新 prefix。
- `fresh_reload_candidate_new_actor`：新进程加载新 saved adapter；四个原 probe 逐项复核参数 bytes、生成 token IDs、raw output 和诊断，不把这四例当作全体效果评估。

base、warm-start 和 candidate 的 86 条原始输出须逐样本可对照，记录严格 JSON/contract 有效性、action confusion、原始米/弧度误差、null/unknown 是否保留，以及截断/超时和实际生成边界。未知量不能替换成零，不以过采样的 280 draw 代替 86 unique 的效果统计；三组都明确标为 `resubstitution_only`，不是独立 holdout。

candidate 与 `checkpoint_e50249_new_actor` 的差异才用于本轮训练集重代入比较。旧协议输入结果只作历史旁证，不能当新 Actor 实验控制组。即使全部 86 个训练样本改善，也不能代替独立 holdout、promotion 或自动飞行质量。

旧 checkpoint 未保存 optimizer、scheduler 或 RNG state；旧源码只保存 adapter，旧目录也没有完整状态文件。因此这次只能称为**加载已有权重后使用 fresh AdamW 继续微调**，不是完整 optimizer-state resume：

```text
continuation_kind = warm_start_adapter_weights_with_fresh_optimizer
adapter_weights_loaded_exactly = true
optimizer_state_restored = false
scheduler_state_restored = false
rng_state_restored = false
prior_optimizer_steps_observed = 86
new_optimizer_steps_start_at = 0
```

旧 adapter 的实际 digest 加载后必须是 e50249，而不是 backend 构造时随机初始化的 `adapter_digest_initial`；新增报告另记 warm-start digest。backward-only preflight 和 baseline 不得改变该 digest，所有 frozen base/vision 参数继续无梯度。

完成上述门后新建 AdamW，验证初始 state 为空，只包含已审核的 112 个 FP32 LoRA 参数。显式记录学习率、weight decay、betas、eps、amsgrad、foreach/fused 等策略及实际 optimizer 源码绑定；旧调用 weight decay 为 0，本机实际默认 betas 为 `(0.9, 0.999)`、eps 为 `1e-8`。不把旧 86 步加到新 optimizer 内部计数。

## 新 draw journal、保存与完成标准

严格按已绑定 schedule 的 1..280 顺序，每 draw 一次 optimizer 更新。重复样本允许重复使用同一经审核输入，但日志的 `draw_index`、sample ID、class-cycle 信息和 `per_sample_exposure` 必须与该 draw 完全匹配；这些字段仅是外部日志，不是 actor 输入。

沿用 fsync 的四阶段证据：`attempted → returned → synchronized → completed`。新增 journal verifier 检查阶段顺序、draw 顺序、身份、曝光次数、无遗漏/额外 draw，最终四个计数都为 280。若尝试后抛异常或计数不一致，更新不确定性必须保留，不从缺失日志推断“零更新”。

训练结束核对：base digest 不变，trainable audit 不变，adapter digest 与 warm start 不同；create-only 保存 adapter，CPU saved tensor digest 精确等于已训练实际 digest。source/runtime/计划在前后边界复验，大文件按唯一规范路径去重；冲突长度或 SHA 直接拒绝。

fresh reload worker 禁止 optimizer/backward，在独立 BF16 base + FP32 default adapter 上精确加载新权重，复核四个原 probe 的相同新 Actor 输入和 candidate 输出。supervisor 要求实际 native exit 0、完整 journal、base/warm-start/candidate 三组全部 86 唯一样本的成对严格诊断、四 probe 重载一致、hook 恢复、reader 审计、source 前后 hash 和本次 process identity 全部一致，才可标记本实验完成；四 probe smoke 或重载通过不能单独满足效果评估门。

训练实现的最小 CPU 定向测试应能区分：280 schedule 重复是否合法、draw 顺序/曝光被篡改、旧100步门误用、checkpoint/dtype/hash漂移、prefix/full或response mask不一致、processor消息可变消费、baseline后source drift、mid-step不确定更新、create-only输出冲突、hook恢复失败、launcher与worker PID混淆，以及虚假 native成功报告。不需要为这份方案运行模型。

即使这些实现与本轮 train-only 实验将来全部通过，也只证明权重续训与工程证据闭合。`quality_claim`、`candidate_promoted`、`execution_authority`、`transport_created`、`mission_created`、物理/HIL/室外GNSS和最终发布门保持 false；独立数据、多场景、真实闭环和安全飞行验收另行完成。
