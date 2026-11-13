# r7 collection 失败归因（2026-11-05）

## 范围与证据边界

分析对象为 `/data/projects/Multi-Detect-env/runs/queue_r21id_r7_collect-2.out`，任务脚本为 `/data/projects/Multi-Detect-env/runs/queue_r21id_r7_collect.sh`。日志快照从 `2026-11-05 00:42:52 +08:00` 开始（第 1 行）；复核时文件共 9244 行，末尾仍是 worker 的失败 traceback，没有 `r7collect exit`，因此这是运行中快照，不代表最终结果。

快照中有 12 条“4 次尝试后失败”记录（表中“episode”是脚本 episode 索引），落在 9 个 world。启动 guard 记录可用内存 276.8339968 GB（第 2 行）。日志没有 load average、持续内存压力、OOM 或主机资源时序采样，所以“load 300+”与内存压力均无法由此日志核实。

以下归因仅基于可见失败栈：10 个 episode 的仿真时钟停滞、EKF/起飞等待超时及无帧超时，说明 PX4/Gazebo 或其输入管线未及时推进；症状与 PX4 starvation / 主机争用相符，但并不单独证明根因就是 CPU load。另外 2 个 episode 有 Docker 明确报告的 UDP 端口占用。没有发现指向 collector 逻辑、特定 world 内容或 scenario 配置的确定性错误。失败行前的 traceback 末行作为该 episode 放弃时记录的终态证据，不代表日志保留了每次重试的所有独立根因。

## 失败 world

| World | 失败 episode | 首要归因 | 日志证据 |
|---|---|---|---|
| `gen_r21id_r7_w0000` | `1` | PX4 starvation / 主机争用（高度疑似，负载值未采样） | 最终 traceback 为 primitive 中 PX4 仿真时钟停滞，step 13、wall timeout 45 s（第 1565–1577 行）。 |
| `gen_r21id_r7_w0002` | `1` | PX4 starvation / 主机争用（高度疑似；起飞阶段推进不足） | 最终错误为等待 `takeoff_reached` 超时 190 s（第 1947–1959 行）。同 world 的 episode 0 已有成功产物记录（第 1960–1961 行），不支持 world 必然不可用。 |
| `gen_r21id_r7_w0005` | `0` | PX4 starvation / 主机争用（高度疑似） | 最终错误为仿真时钟停滞，step 2、wall timeout 45 s（第 4661–4673 行）。同 world 的 episode 1 有成功产物（第 4833–4834 行）。 |
| `gen_r21id_r7_w0006` | `0`, `1` | PX4 starvation / 主机争用（高度疑似；跨两个 episode 重现） | episode 0 在 step 70 时钟停滞（第 5567–5579 行）；episode 1 在 step 26 时钟停滞（第 6757–6769 行）。 |
| `gen_r21id_r7_w0007` | `0`, `1` | PX4 starvation / 主机争用（高度疑似；两个 worker 均失败） | episode 0 最终未通过 `xy_position_control` 预臂健康检查（第 6085、6125–6126 行）；episode 1 在 step 3 时钟停滞（第 6229–6241 行）。 |
| `gen_r21id_r7_w0008` | `0`, `1` | PX4 starvation / 主机争用（高度疑似；故障阶段不同） | episode 1 等待起飞超时 200 s（第 6593–6605 行）；episode 0 的传感器流 15 s 无帧（第 8528–8541 行）。 |
| `gen_r21id_r7_w0009` | `0` | PX4 starvation / 主机争用（高度疑似；EKF/遥测未及时就绪） | 等待 `ekf_ok` 超时 90 s，仍缺 `GLOBAL_POSITION_INT`（第 7015–7034 行）。同 world 的 episode 1 已产出成功记录（第 8707–8708 行）。 |
| `gen_r21id_r7_w0013` | `0`, `1` | 主机端口冲突（已证实），与 PX4 starvation 分开 | worker 2 的容器启动多次被 Docker 拒绝：无法绑定 `127.0.0.1:14580/udp`，`address already in use`；episode 0 放弃记录在第 9153 行，episode 1 在第 9214 行。worker 2 的配置 control port 为 14580（启动命令第 5–63 行的参数规律；运行脚本设置见 `queue_r21id_r7_collect.sh`）。日志无法识别占用该端口的进程/容器。 |

### 分类别结论

- **主机争用 / PX4 starvation：** 10 个失败 episode 均有 PX4 时钟、预臂、起飞、EKF 或传感器流超时的运行时症状。将它们作为资源/仿真推进类失败优先重试较合理；“load 300+”及内存压力不能由此日志证实。启动时内存余量充足（第 2 行），但这不是失败时内存的测量值。
- **主机端口冲突：** `w0013:0` 和 `w0013:1` 在 Docker 建容器阶段确定性失败，原因是 worker 2 的 control UDP 端口 14580 已被占用（第 8976、8991、9006、9150、9166、9181、9196、9211、9227、9242 行）。这是运行环境端口冲突；不清楚占用者是否属于外部任务、残留容器或其他本机服务。worktree 没有证据支持把它归因于 scenario，也无法据此认定为 collector 缺陷。
- **真实 collector 缺陷：** 未见明确证据。失败落在不同采集等待阶段，且错误是超时而非解析、校验、路径、数据格式或程序逻辑异常。
- **world / scenario 缺陷：** 未见明确证据。失败 world 至少有数个对应 episode 成功（w0002、w0005、w0009；证据见上表），失败症状也不集中于固定场景阶段或固定动作。对其余 world，当前日志不足以证明或排除场景相关问题。
- **小而明确的代码修复：** 当前没有可由这些日志支持的修复，因此本次不改代码，也不新增测试。

## 重试清单

资源/仿真推进类重试，建议等主机负载回落后重新收集以下 episode：

`gen_r21id_r7_w0000:1`、`gen_r21id_r7_w0002:1`、`gen_r21id_r7_w0005:0`、`gen_r21id_r7_w0006:0`、`gen_r21id_r7_w0006:1`、`gen_r21id_r7_w0007:0`、`gen_r21id_r7_w0007:1`、`gen_r21id_r7_w0008:0`、`gen_r21id_r7_w0008:1`、`gen_r21id_r7_w0009:0`。

端口冲突 episode：`gen_r21id_r7_w0013:0`、`gen_r21id_r7_w0013:1`。重试前需确认 worker 2 的 UDP 14580 已释放，或在后续采集配置中为 worker 分配不冲突的 control port；本次不检查、不操作占用进程。

当前没有基于证据应排除重试的确定性 scenario/world 缺陷。任务仍在运行；后续新增失败需在任务结束后以最终日志补充，不要据此快照把尚未结束的 collection 标为最终失败。
