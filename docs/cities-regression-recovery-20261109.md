# 城市首轮失败的两处修复

原输出 `runs/cities-final-20261109/output/cities` 保持只读。三地六次是独立城市门禁，不进入普通21任务的42架次分母。

上海两站首架并非 heartbeat 超时：陆家嘴 `025f32a4`、世纪公园 `fd583ce1` 都记录 heartbeat、px4_ready 和首张传感器帧，之后在 collector 的 profile-only takeoff 分支 `assert profile is not None` 失败。城市 suite 传 `x500`，但该 collector 的多旋翼起飞路径按规范名称 `x500_mono_cam` 分派。本提交只改城市三条任务的 airframe 名称；模型、180步、2400秒、survey coverage≥0.7和return_home断言不变。

新加坡首架 `9b5dc4a0` 的 Gazebo 则在 DART ConstructSdfCollision 中以 `BoxShape size[2] > 0` 断言终止，PX4等不到世界就绪，因此MAV收包0。原 world 的 `obstacle_23` 和 `obstacle_24` 是从原 scene.json 的零高度 footprint 生成的辅助AABB，尺寸分别67.50×23.60×0和88.50×43.90×0米。`obstacle_boxes` 现在在序列化精度下拒绝生成无体积辅助盒并明确警告，非有限数据报错；没有给它们编造高度。原始数据、footprint记录、完整建筑/地形碰撞网格、视觉网格及其它模型保留。

用原 scene 和只读共享模型重新运行生成器，写操作审计只允许新world输出目录。逐项XML比对证明：

- 陆家嘴和世纪公园world逐字节SHA不变：`c67b600aecb1f504a88c4958cf3501cd3669bd1841d93aaa462eff5e48449fea`、`0ec29ca76bebfc15387d1b705c0ca86a13a224dfb8505fc5f25ee1a85de643d3`。
- 新加坡只去掉上述两个非法辅助模型，其余world内容和164个保留模型完全一致。新SHA `e659a5e29895b4874899f43c8c0269854730935e629d77acbbe638dcaeaae586`；原SHA `d0af3f1de1ac944f0cd6fd79e7d8401115a5933966695bd09b662e5678fcad1a`。

20个focused测试通过（含原失败两条footprint、序列化为0的厚度、退化宽度、负高度、非有限高度及保留正常邻居的反例）。GIS依赖在独立 `/data/scratch/city-recovery-uv-cache` 环境中加载，未改共享运行Python环境。

新生成world及完整再现/差异证据在工作区 `runs/cities-recovery-20261109/{worlds,generate_worlds.py,world-regeneration.json}`。生成的SDF按仓库原规则忽略，冻结新运行source时必须从这些新资产复制并按上述SHA验证，不能使用旧新加坡world。陆家嘴单站及后续三地×2实际通过情况以新输出为准；focused或XML比对不冒充飞行通过。旧source、失败日志和NAS均未修改。
