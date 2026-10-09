# PX4 采集包搬运与完整性复核

适用：`v0.3.0-pre.8` / 工程迭代 v195，receipt schema v1。

采集成功后，将 **整个 base directory** 原样复制到接收机器，保留内部相对目录。
其中包括 `capture-summary.json`、`capture-receipt.json` 以及回执列出的六个基础源文件；
如果采集启用了共享策略，还必须包含回执列出的第七个 `policy_artifact` 文件。
不要单独复制 receipt，也不要替换 summary 内的历史绝对路径。

在仓库根目录运行（以下接收目录是示例，替换为真实目录）：

```powershell
$env:PYTHONPATH = 'src'
python -c 'from multidetect.px4_runtime_capture_receipt import verify_main; raise SystemExit(verify_main())' capture-receipt.json --base-directory 'D:\FieldEvidence\capture-001' --runtime-mode hil
```

REAL 包使用 `--runtime-mode real`。返回码 0 表示复核通过，2 表示拒绝，错误 JSON 写入 stderr。
安装了项目命令入口的环境也可使用 `multidetect-px4-runtime-capture-receipt-verify` 加同样参数。
不指定 base directory 时，以 receipt 所在目录为根；如果 receipt 位于包的子目录，必须显式指定包根。

验证器检查 summary 哈希/大小、completed 与各 gate 声明、角色顺序，以及历史源路径是否与回执相对路径
对应到同一个历史根目录。Windows 和 POSIX 绝对路径均作为历史元数据处理，绝不打开原目录。
随后仅在接收根目录内重开文件，重算大小和 SHA-256；缺失、篡改、符号链接或路径越界都会拒绝。

这是 **字节完整性回执**，不是数字签名。若有人同步重写源文件、摘要和回执，单凭回执不能证明作者或
采集真实性。需要通过可信渠道保存回执身份；该工具也不会重新执行源文件的完整语义 join。
共享策略 artifact 只绑定 policy/ranker/airframe/mission/safety 身份，不授予 actuator 权限。
现场 HIL/REAL、GNSS、人工 Fly View、飞行与最终发布验收须独立完成，不因 receipt 通过而升级。
