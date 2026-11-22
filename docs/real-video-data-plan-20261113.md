# 用真实无人机视频缩小仿真到真机差距（方案与流水线）

范围：只做数据侧。所有 GPU 作业由「训练/算力」线程排期；数据落 `$MD_REAL_VIDEO_ROOT`（缺省 `/mnt/nas/AdminHome/md-real-video`），只追加、不删除。代码在 `scripts/real_video/`，测试 `tests/test_real_video_pipeline.py`（CPU）。

## 1. 数据源与许可（tier 决定能怎么用）
登记表见 `scripts/real_video/sources.yaml`。许可证来自整理时的记忆与主页口径，**下载前要在主页复核并写 `verified_on`**。

| 档 | 含义 | 来源 | 用法 |
|---|---|---|---|
| A | 允许训练、可随模型发布（署名） | YouTube 标 Creative Commons 的视频（逐条记录上传者与许可）、TartanAir（CC BY 4.0） | 伪标签训练、风格/域随机化、运动统计 |
| B | 仅研究/非商用 | VisDrone、UAVDT、AU-AIR、Mid-Air、UAV123、DOTA、ERA | 只做评测、域差测量、不随产品发布的实验；其权重不得进入生产镜像 |
| C | 仿真/工具 | Aerial-Gym | 不属于本线 |

要点：YouTube 的 CC 标记是上传者自称，视频里可能含第三方素材，所以只当「低风险」而非「无风险」，署名清单由 manifest 自动可导出。

## 2. 创新用法（按性价比排序）
1. **双教师共识伪标签再蒸馏到快系统**（`consensus.py`、`pseudo_label.py`）。YOLOE（快、开放词表）和 Qwen3-VL（慢、语义强）各自检测，同类且 IoU≥0.5 才算标签；单教师框进「不确定池」，训练时当忽略区，不当负样本。这样不会把某个教师的系统性错误直接烤进 YOLOE。后续：密集抽帧后用跟踪把「单教师但连续出现」的框提升为标签。
2. **先量域差，再决定补什么**（`domain_gap.py`）。对 rw_* 仿真帧和真实帧算低成本描述子（颜色、亮度、傅里叶细节谱），输出「仿真已覆盖」和「仿真缺口」两组真实帧（逆光、雾霾、低照度、压缩纹理…），反过来指导仿真世界的天气/光照/材质补充，而不是盲目加数据。
3. **傅里叶域适配（FDA）零训练风格迁移**（`fda_augment.py`）。把仿真帧低频幅度谱换成随机真实帧的，保留仿真精确标注；纯 numpy，不占 GPU。作为第一档域随机化，比 GAN 便宜，先验证有没有用再考虑扩散/CycleGAN。
4. **从视频估计相机/云台运动分布，反哺云台策略**（`camera_motion.py`）。同一个函数跑真实视频和仿真录像，比较俯仰率/偏航率/滚转率/抖动的分位数，用来校准仿真里云台指令与机体扰动的幅度和频谱，并找出仿真没覆盖的运动区间（如快速俯仰追车）。三角翼的悬停（机头朝上）与巡航两种姿态要分别统计。
5. **真实背景 + 仿真前景合成（第二阶段，未实现）**：用 Gazebo 的分割掩码把我们自己的目标（SUV、消防栓、红衣人）抠出来贴到真实背景上，标签精确且背景真实。需要先确认仿真录制里有掩码输出。
6. **「现实检查集」**：从 A 档视频里人工核对约 300 帧，作为判断以上任何手段是否真有用的唯一尺子；没有它，仿真内指标提升不能算真机收益。

## 3. 运行顺序
```
fetch_youtube_cc.py      # CPU，网络，下载 A 档 CC 视频 + manifest
extract_frames.py        # CPU，抽帧/去糊/去重 -> frames.jsonl
camera_motion.py         # CPU，运动统计（真实 vs 仿真）
domain_gap.py            # CPU，域差与缺口排序
fda_augment.py           # CPU，仿真->真实风格帧
pseudo_label.py          # GPU，双教师打标（需要排期）
```

## 4. 给算力线程的 GPU 需求（排期用）
- 作业：`pseudo_label.py --source youtube_cc --classes person,car,fire_hydrant,traffic_cone`，YOLOE-11s + Qwen3-VL-8B（bf16 约 17 GB，要整卡 20 GB 余量；YOLOE 很小）。
- 规模：先 2000 帧试跑，估计 1~2 GPU 小时；通过抽检（见下）再放量到 2 万帧。
- 断点续跑，已存在的标签不覆盖；不占用 GPU2/3。
- 验收抽检：人工看 200 帧共识框，共识框精度 ≥0.9 才进入训练集。
- 之后才是训练作业：把 `labels` 并入检测数据（A 档），B 档只进评测，三角翼带云台视角的片段优先。

## 5. 已知限制
- 本线程运行在云端容器，无法访问 NAS；流水线写好后需在 workstation 上执行（下载/抽帧/运动/域差为 CPU 作业，不占 GPU）。
- `pseudo_label.py` 的 YOLOE/Qwen3-VL 加载代码未在 GPU 上跑过，只测了解析与共识逻辑；首次试跑可能要按 workstation 上的 ultralytics/transformers 版本微调。
