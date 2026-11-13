# 身份推理输入尺寸：可选 672 配置

`OnboardPolicy` 支持身份专用配置 `identity_max_side`，省略时保持 448。它必须是 JSON 正整数；布尔、零、负数、浮点数、字符串和 null 均在创建模型或 proposer 前报错。虽然历史默认配置在组装时会被统一字符串化，此字段仍保留整数类型，不通过 `int()` 截断或隐式转换。

该字段控制 `LlamaServerRunner` 发送给身份模型的**全帧 JPEG 最长边上限**，保持宽高比且不放大小图。例如 1280×960 输入在默认配置下编码为 448×336，设置 672 后为 672×504。`candidate_zoom_v2` 的候选裁剪仍为 224×224；这不是放大裁剪尺寸的选项。模型内部的视觉编码、实际延时和显存仍需另外实测。

## 供评审的配置

下面是对现役配置的独立副本，仅改变输入分辨率。它保留 r21id-r3、r24 与规划前预检，可用于评估尺寸本身的影响，不修改现役配置文件，也不代表 r11 已通过替换门槛。

```python
import json
from pathlib import Path

source = Path("configs/onboard/sim-loop-v3-r21idr3-r24plan-defaults.json")
candidate = json.loads(source.read_text())
candidate["identity_max_side"] = 672
destination = Path(".tmp/identity-672-review.json")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(candidate, indent=2) + "\n")
```

候选副本的新增字段为：

```json
{"identity_max_side": 672}
```

以后评估经过准入检查的 r11 权重时，还必须在独立候选配置中明确填写其实际 `gguf` 路径；仅改变模型路径不会改变默认 448。本文没有推广 r11、生成现役替换配置或运行推理/飞行。

## 运行时边界

- `assemble_runtime_runners.make_identity_runner` 将这个整数传给身份 runner 的 `max_side`，重启与规划后切回身份都复用同一工厂。
- 分离规划模型的构造不接收此选项。规划通过 HTTP 文本消息提交，不走身份 JPEG 编码。没有分离规划模型时，同一个身份 runner 承担文本规划与后续身份图像请求；尺寸只参与图像请求的编码。
- 没有按模型路径缓存或共享 runner 实例。相同权重路径在不同尺寸的两个 policy 中创建独立实例；模型文件哈希缓存只缓存文件哈希，不缓存 runner。runner 的 verdict token 缓存保存文本标签的分词，不缓存图像或图像推理结果。
- 若调用者通过 Python 注入 `slow_runner`，并显式配置 `identity_max_side`，被注入对象的整数 `max_side` 必须完全匹配，否则报错。配置不会静默忽略，也不会修改外部共享对象。未指定该选项时，已有自定义 runner 的注入行为保持不变。
- 这是现有自由键值 policy 配置的一项；没有修改或重新生成 `configs/schemas/`。

## 验证

`tests/test_onboard_policy_identity_resolution.py` 覆盖省略/448/672、分离与共享规划、实际 HTTP JPEG 尺寸、固定裁剪尺寸、相同模型路径的实例隔离、身份重启、非法值在启动前拒绝及注入 runner 的匹配检查。测试替换服务器启动与 HTTP 传输，不加载模型、不调用 GPU、不占用 SITL 槽位。

聚焦测试与 CPU onboard gate 的实际结果记录在 `runs/progress-20261109-identity-runtime.md`。这些是配置/编码行为证据，不能代替 r11 的识别指标、运行显存或飞行回归。
