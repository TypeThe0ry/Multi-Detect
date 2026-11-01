# r21id-r4 已采集架次质量审计

审计日期：2026-11-01。审计只读取 artifacts/r21id-r4c、round-state、r21id-r4 index 与场景 truth/intent；观察帧门槛为每视角至少 8 帧。

## 结论

round-state 有 48 个逻辑架次，其中 status=complete 31 个；逐帧审计后合格 15 个，覆盖 8 个 world（gen_r21id_r4_w0004, gen_r21id_r4_w0010, gen_r21id_r4_w0012, gen_r21id_r4_w0013, gen_r21id_r4_w0020, gen_r21id_r4_w0021, gen_r21id_r4_w0022, gen_r21id_r4_w0023）。

失败逻辑架次 17 个；对应失败 episode artifact 95 个。短边档位这里只能核对 index 的设计 estimate（采集 steps 没有 bbox/短边字段），不会把设计值冒充实测像素。

当前不能进入 annotate/build_sft：合格覆盖只有 8/24 world、15 个 episode，且 pickup world 没有 SUV 任务文本对应的反向 pickup→suv 拒绝覆盖；设计要求的 24 world/96 视角组尚未满足。已有合格架次可作为增量数据，但不能作为本轮完整采集完成信号。

## 按世界

| world | 目标/干扰物 | complete | 合格 | 失败逻辑/失败 artifact | index 短边档 | 问题 |
|---|---|---:|---:|---:|---|---|
| gen_r21id_r4_w0000 | pickup / suv | 2 | 0 | 0/8 | 8-12（设计 10.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0001 | pickup / suv | 2 | 0 | 0/0 | 12-16（设计 14.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0002 | pickup / suv | 2 | 0 | 0/0 | 8-12（设计 10.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0003 | pickup / suv | 2 | 0 | 0/16 | 12-16（设计 14.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0004 | pickup / suv | 2 | 2 | 0/16 | 24-32（设计 28.0 px） | - |
| gen_r21id_r4_w0005 | pickup / suv | 0 | 0 | 2/0 | >=32（设计 40.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0006 | suv / pickup | 2 | 0 | 0/8 | 8-12（设计 10.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0007 | suv / pickup | 2 | 0 | 0/0 | 12-16（设计 14.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0008 | suv / pickup | 2 | 0 | 0/2 | 8-12（设计 10.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0009 | suv / pickup | 2 | 0 | 0/8 | 12-16（设计 14.0 px） | 目标距离不在设计距离 ±25%；缺视角: front, oblique, edge_truncated, recovery |
| gen_r21id_r4_w0010 | suv / pickup | 2 | 2 | 0/8 | 24-32（设计 28.0 px） | - |
| gen_r21id_r4_w0011 | suv / pickup | 0 | 0 | 2/0 | >=32（设计 40.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0012 | postbox / hatchback | 2 | 2 | 0/8 | 8-12（设计 10.0 px） | - |
| gen_r21id_r4_w0013 | postbox / hatchback | 1 | 1 | 1/8 | 12-16（设计 14.0 px） | - |
| gen_r21id_r4_w0014 | postbox / hatchback | 0 | 0 | 2/0 | 24-32（设计 28.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0015 | postbox / hatchback | 0 | 0 | 2/0 | >=32（设计 40.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0016 | person / postbox | 0 | 0 | 2/0 | 24-32（设计 28.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0017 | person / postbox | 0 | 0 | 2/0 | >=32（设计 40.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0018 | person / postbox | 0 | 0 | 2/0 | 24-32（设计 28.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0019 | person / postbox | 0 | 0 | 2/0 | >=32（设计 40.0 px） | 没有合格 complete 架次 |
| gen_r21id_r4_w0020 | ambulance / suv | 2 | 2 | 0/5 | 24-32（设计 28.0 px） | - |
| gen_r21id_r4_w0021 | ambulance / suv | 2 | 2 | 0/0 | >=32（设计 40.0 px） | - |
| gen_r21id_r4_w0022 | ambulance / suv | 2 | 2 | 0/0 | 24-32（设计 28.0 px） | - |
| gen_r21id_r4_w0023 | ambulance / suv | 2 | 2 | 0/8 | >=32（设计 40.0 px） | - |

## 合格架次明细

| 架次 | world | 任务 | 四视角观察帧 | 设计距离/观测均值（m） | 短边设计档 |
|---|---|---|---|---:|---|
| gen_r21id_r4_w0004:0 | gen_r21id_r4_w0004 | 找到皮卡并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 35.4/35.0 | 24-32（28.0 px） |
| gen_r21id_r4_w0004:1 | gen_r21id_r4_w0004 | 找到皮卡并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 35.4/35.0 | 24-32（28.0 px） |
| gen_r21id_r4_w0010:0 | gen_r21id_r4_w0010 | 找到SUV并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 38.7/38.2 | 24-32（28.0 px） |
| gen_r21id_r4_w0010:1 | gen_r21id_r4_w0010 | 找到SUV并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 38.7/38.3 | 24-32（28.0 px） |
| gen_r21id_r4_w0012:0 | gen_r21id_r4_w0012 | 找到邮筒并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 36.8/35.9 | 8-12（10.0 px） |
| gen_r21id_r4_w0012:1 | gen_r21id_r4_w0012 | 找到邮筒并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 36.8/35.8 | 8-12（10.0 px） |
| gen_r21id_r4_w0013:0 | gen_r21id_r4_w0013 | 找到邮筒并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 26.3/25.8 | 12-16（14.0 px） |
| gen_r21id_r4_w0020:0 | gen_r21id_r4_w0020 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 52.0/50.5 | 24-32（28.0 px） |
| gen_r21id_r4_w0020:1 | gen_r21id_r4_w0020 | 找到 SUV 并记录位置 | front=8, oblique=22, edge_truncated=8, recovery=9 | 52.0/51.0 | 24-32（28.0 px） |
| gen_r21id_r4_w0021:0 | gen_r21id_r4_w0021 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 32.1/31.9 | >=32（40.0 px） |
| gen_r21id_r4_w0021:1 | gen_r21id_r4_w0021 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 32.1/31.9 | >=32（40.0 px） |
| gen_r21id_r4_w0022:0 | gen_r21id_r4_w0022 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=9 | 52.0/50.9 | 24-32（28.0 px） |
| gen_r21id_r4_w0022:1 | gen_r21id_r4_w0022 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 52.0/50.9 | 24-32（28.0 px） |
| gen_r21id_r4_w0023:0 | gen_r21id_r4_w0023 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 32.1/31.7 | >=32（40.0 px） |
| gen_r21id_r4_w0023:1 | gen_r21id_r4_w0023 | 找到 SUV 并记录位置 | front=8, oblique=8, edge_truncated=8, recovery=8 | 32.1/31.7 | >=32（40.0 px） |

## 失败原因

| 归一化原因 | 失败 artifact 数 |
|---|---:|
| PX4 未确认 armed heartbeat | 78 |
| episode wall-clock timeout | 14 |
| 等待降落超时 | 3 |

失败原因按 world 交叉：
- gen_r21id_r4_w0000：PX4 未确认 armed heartbeat=3, episode wall-clock timeout=5。
- gen_r21id_r4_w0003：PX4 未确认 armed heartbeat=15, episode wall-clock timeout=1。
- gen_r21id_r4_w0004：PX4 未确认 armed heartbeat=16。
- gen_r21id_r4_w0006：PX4 未确认 armed heartbeat=1, episode wall-clock timeout=7。
- gen_r21id_r4_w0008：PX4 未确认 armed heartbeat=1, episode wall-clock timeout=1。
- gen_r21id_r4_w0009：PX4 未确认 armed heartbeat=8。
- gen_r21id_r4_w0010：PX4 未确认 armed heartbeat=8。
- gen_r21id_r4_w0012：PX4 未确认 armed heartbeat=8。
- gen_r21id_r4_w0013：PX4 未确认 armed heartbeat=8。
- gen_r21id_r4_w0020：PX4 未确认 armed heartbeat=2, 等待降落超时=3。
- gen_r21id_r4_w0023：PX4 未确认 armed heartbeat=8。

armed heartbeat 失败横跨 pickup、SUV、postbox 和 ambulance 多类 world，现有证据更像 PX4/SITL 启动或连接稳定性问题，不能归因于某一类目标摆放。降落超时目前只出现在 w0020（ambulance/SUV 对照），应单独复跑确认是否与该 world 的出生点、目标布局或返航路径有关；仅凭本轮失败 artifact 不能下结论。

## 重新生成或修改清单

- w0000–w0003、w0006–w0009：虽有 complete，实际只有 search/front，没有四个 observe 视角；需要重新采集，不能按 status 放行。
- w0005、w0011、w0014–w0019：当前没有合格 complete；失败主要是 armed heartbeat，person/postbox 大小档世界还没有有效四视角记录，需先排查运行稳定性后重采。
- w0004、w0010、w0012、w0013、w0020–w0023：已有合格记录，但同一 world 的覆盖不完整（例如 w0013 只有一个逻辑 episode），应补齐计划数量并保留四视角。
- pickup/SUV 配对：当前合格任务文本统计没有证明 pickup→suv hard reject；必须补采 pickup world 的 SUV 请求（以及 SUV world 的 pickup 请求作为对称对照），否则不能宣称关键拒绝方向已覆盖。

## 审计口径

- complete 架次按 round-state 的最终 path 审计；失败原因按对应 world/ep 下所有 status=failed 的 episode.json artifact 统计，同时保留逻辑失败架次数量。
- 每个视角只计 phase=observe、target_in_fov=true 且 RGB 文件存在的帧；距离使用现有 smoke 检查的目标地面距离 ±25% 判定。
- index 短边档检查的是 target_short_side_px_estimate 是否落在声明档位；steps 没有目标 bbox，因此未声称实测短边像素合格。
