# 云台回归套件扩展（2026-11-07）

本次把 `configs/onboard/suite-gimbal-r21idr3-r24plan.json` 从 3 个任务扩展到 7 个任务。所有任务使用 `x500_gimbal`，用于覆盖云台控制在不同任务语义下的行为：

| 云台任务 | 对应普通任务 | 覆盖内容 |
| --- | --- | --- |
| `gimbal-find-suv-land` | `find-suv-land` | 搜索目标、记录位置并降落 |
| `gimbal-monitor-backpacks` | `monitor-backpacks` | 监视目标并返航 |
| `gimbal-operator-pause-resume` | `operator-pause-resume` | 操作员暂停后恢复，验证监督器 hold/continue 事件 |
| `gimbal-count-fire-hydrants` | `count-fire-hydrants` | 计数并报告位置 |

原有的 `gimbal-inspect-postbox`、`gimbal-survey-site-overlap` 和 `gimbal-count-cones` 保持不变。新增变体复制对应普通任务的 `expect` 规则、场景索引、世界和步数限制。云台本身改变的是相机姿态和控制输入，因此没有证据支持放宽或改写任务完成、计数、监视或监督器断言；这次扩展的目的正是用同一通过规则检查云台控制是否改变任务结果。`gimbal-operator-pause-resume` 只把 mission package 的 `mission_id` 改成变体自己的 ID，以保持日志身份唯一，监督器规则保持一致。

每个 7 个任务都带有：

```json
"recording": {
  "per_step": ["gimbal_joint_state", "propeller_joint_state"]
}
```

套件 runner 对 `x500_gimbal` 强制检查这个配置。采集器现有的 gimbal-aware observation path 会在每个 step 的 `obs_before` 和 `obs_after` 中保存云台关节角，以及四个螺旋桨的角度/转速 joint state；因此该字段是可审查的套件契约，而不是只写在任务名称中的约定。普通 fixed-mount 任务不受这条校验影响。

验证范围：

- 运行相关 Python 单测和 `run_mission_suite.py --dry-run` 配置检查；
- 未启动 SITL、Docker、模型服务或 GPU 训练；
- 未修改 `artifacts/` 共享只读产物。

