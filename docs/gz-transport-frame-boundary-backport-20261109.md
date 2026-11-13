# transport13 接收帧边界补丁与可复现交付

本交付只增加独立构建、运行时上下文和 CPU 组件验证入口，不改变 SITL 默认镜像、现役标签或机载配置。运行层继承现有 PX4 lifetime-r1 修复，只复制 transport SO，并在 Dockerfile 中检查 PX4 二进制仍为既定 SHA。镜像构建、实景门禁和推广分别执行；组件通过不等于飞行验收。

## 固定输入

`simulation/gz-transport/pins.json` 是输入清单，记录：

- 上游 transport13 13.5.0 提交 `7232d0b163b08f79dd390ad9913d6ea72efaf209`。
- 本地审查提交 `09266eb0eb381d090b9fa7f784636ef6eb0335a3` 对应的完整源码 tree `077fa3ca3dbafae34ad92d42669593c52c05d78a`；该本地提交不是可从上游获取的提交。
- 仓库内两文件补丁 SHA `5ccf19125b46baf8857b38a07713358eef892c5425ac2572eb9b3fa4839bf6ef`，以及两个修改文件和组件源码的 SHA。
- 本地不可变 SDK 镜像 ID `sha256:90c9627f3810dad72a73d40ba462e9c044d1c5e59b12b819a759da6b59e47d60`。
- 继承 PX4 修复的运行基底 ID `sha256:b5c576ce15b8a75935313bc3e8dc4cf9940778226b28e835064d7581c3fe72c6`，PX4 SHA `9c2280e005ad426f01cdffddf2114e1e4b13e26aa9f5996c4b52cdfa9e722d99`。
- 已验证的 RelWithDebInfo SO SHA `aff44fe0ab4e1452c78b65e97c76a3f99f9d132777e96840666186f3d8cc82fe`；参考原库 SHA `2b6de4e791e5d71ffe0560c486a91d4ed7bb6a5f63431d91ec781b85f431cbf6`。

SDK ID 是 workstation 本地内容 ID，不是可拉取的 registry digest。缺失时脚本直接失败，不自动 apt/pip 安装或替换 SDK。另一台机器需要先获得同一 SDK 镜像归档，或另行审查新的工具链 pin。

## 修复范围

原 `RecvMsgUpdate` 持 NodeShared 共享 S 后，固定读取四段，再按本地统计开关读取第五段，没有检查消息边界。已完成但过短的 ZMQ 消息会使下一次 recv 等待另一条消息；多余第五段也可能在下一轮被误当 topic。

补丁按 `message.more()`（RCVMORE 语义）读取当前消息，使用非阻塞 recv，验证四个必需段及可选、大小正确的 16 字节统计段。过短、超额或错误统计段会被拒绝，并排空当前消息。中途 EAGAIN/异常后，一个受 S 保护的 private 状态保证下一轮只丢弃剩余段，不把 suffix 当新消息。

Discovery 的统计版本检查保持原样。同配置 0/0 与 1/1 正常通信；混合 0/1、1/0 仍被拒绝。组件对坏帧使用明确标注的内部连接回调注入，不宣称扩展正常发现协议。

## 准备与构建

使用任意包含固定上游提交的本地 Git 缓存，例如 `/data/scratch/gz-transport13-clock-20261109`。脚本只读该输入，忽略并保留其工作区修改，在新输出目录克隆固定 Git 对象、应用补丁并检查完整 tree。输出必须位于 `/data`，不能复用已有目录或经过 symlink 父目录。

```bash
python scripts/gz_transport_backport.py prepare \
  --source /data/scratch/gz-transport13-clock-20261109 \
  --output /data/scratch/gz13-prepare-UNIQUE

python scripts/gz_transport_backport.py build \
  --source /data/scratch/gz-transport13-clock-20261109 \
  --output /data/scratch/gz13-build-UNIQUE --jobs 2 --cpuset 56,62
```

`build` 先核对基底标签和不可变 ID，随后只运行 sha256sum 验证原 PX4/transport 内容，绝不启动 PX4。构建复用固定 SDK，源码挂载 `/src` 只读，独立工作目录挂载 `/out`，采用已验证的 Ninja、RelWithDebInfo、`/out/build` 路径与 `gz-transport13` core target。没有安装步骤、网络或 GPU。

构建容器默认 2 CPU（可设 1–8）、8 GiB/Swap0、nice10、runc/networknone、capdropALL/no-new-privileges、只读根文件系统。示例 CPU 56/62 位于 workstation 的 SITL CPU 范围以外；其它主机可省略或显式指定可用 CPU。新构建 SO 必须与既定 SHA 相同，否则保留失败和实际 SHA，不自动放宽 pin。差异应先检查源码 tree、SDK、编译参数和 DWARF 的容器路径。

输出包括 `manifest.json`、源码副本、CMake/cache/compileDB、包与编译器版本、完整容器命令/资源/退出和 `runtime-context/`。原镜像和任何标签均不修改；工具本身不调用 docker build。

## 组件验证

可以验证刚构建的 SO，也可以导入已由独立构建得到的同一 SHA：

```bash
python scripts/gz_transport_backport.py validate \
  --source /data/scratch/gz-transport13-clock-20261109 \
  --library /data/scratch/gz13-build-UNIQUE/runtime-context/libgz-transport13.so.13.5.0 \
  --output /data/scratch/gz13-validation-UNIQUE --cpuset 56,62
```

验证使用 2 CPU、2 GiB/Swap0、networknone、独立分区；每个子进程有 7 秒截止。只挂载公共源码副本、验证资产、候选库和自有输出，不挂载主仓库、凭据、NAS、Docker socket 或显示设备。测试先确认实际加载 SO 的 SHA 并设置 LD_BIND_NOW，保留每条命令、stdout/stderr、JUnit、故障注入次数和接收次数。

- 原/候选各 10 个 raw 场景，共 20 次：正常四段、完整短消息、未提交 multipart、可选第五段、错误第五段、六段、三段、EAGAIN、EINTR、统计内容。
- 检查异常后后继正常 payload 不丢失；原库明确注入救回以安全退出，原阻塞不会被计作修复通过。
- 真实跨进程 typed Publisher/Subscriber 各四种统计配置，共八组；同配置严格检查三条正常消息顺序，混合配置严格要求无连接、无数据。
- 编译固定源码中的原样 `TopicsStatistics.*`，在两库各跑四项，要求无失败/禁用。

结果位于输出的 `work/results.json` 和 JUnit；主 `manifest.json` 只在完整检查通过后记录 `component_acceptance_pass=true`。空列表、缺失 XML、非零退出、超时或版本/SHA 不匹配均不构成通过。旧 API 使用 v4.3.0 公开头的编译证据属于冻结补丁审查记录；本入口运行的是当前固定 SDK 4.3.5 库，不宣称运行过历史库。

## 运行层与验收

`runtime-context/Dockerfile` 沿用现有 PX4 修复基底，仅复制 transport SO，检查继承的 PX4 SHA 和候选 SO SHA。它不更改入口、world、参数或配置。生成上下文不等于已构建/部署镜像；实际镜像应使用新标签并重新记录不可变 ID、库/PX4 SHA 和目标镜像依赖核验，禁止覆盖现役标签。

此协议组件修复不证明现场坏消息的生产者或最终卡顿根因。仍须以新镜像和冻结应用源码完成实景时钟及回归门禁，再决定默认配置、main 和 PR 的推广。planning-v5、返航探测、训练数据及现役模型不在本交付改动范围内。
