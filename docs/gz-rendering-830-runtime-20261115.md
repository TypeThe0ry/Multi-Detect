# gz-rendering 8.3.0 GPU 运行时候选（2026-11-15）

本次把 GPU 仿真默认引用切换为
`multidetect/px4-sitl-gazebo:gz-rendering-830-r1`。候选以生产镜像
`multidetect/px4-sitl-gazebo:transport-frame-09266eb0eb38` 为基底，只升级
`libgz-rendering8`、`libgz-rendering8-ogre2`、`libgz-rendering8-core-dev`、
`libgz-rendering8-dev`、`libgz-rendering8-ogre2-dev` 到 8.3.0；依赖带入
`libgz-common5` 5.9.0 和 `libgz-math7` 7.7.0。PX4、修补后的 gz-transport
13.5.0、gz-sensors 8.2.2 和 gz-sim 8.14.0 保持不变。构建配方是
[`simulation/gz-rendering/Dockerfile.runtime`](../simulation/gz-rendering/Dockerfile.runtime)。

| Berlin clu3m，同 seed/steps | 生产镜像 8.2.3 | 候选镜像 8.3.0 |
|---|---:|---:|
| sim 20 s 的 server RSS | 8.4 GB | 6.2 GB |
| 最后观测 | 447 s / 52.5 GB | 353 s / 6.2 GB |
| 观测趋势 | 约 +0.1 GB/s，触及 52 GB soft limit | 20–353 s 基本保持平坦 |

原始证据：`/data/projects/Multi-Detect-env/md-scratch/gzup-evidence/` 下的
`rss-baseline-berlin.jsonl`、`rss-candA-berlin.jsonl`、`rss-compare-berlin.txt`；
镜像内容证据是 `r1-inspect.json`，生产对照是 `prod-inspect.json`。r1 比旧契约多一层，
RootFS 为 12 层；契约中的平台、RootFS 和 Config 逐字段取自 r1 inspect，
没有手写层摘要。完整来源和内容字段见
[`simulation/gz-transport/runtime-image-contract.json`](../simulation/gz-transport/runtime-image-contract.json)。

这只是运行时内容候选，**不代表飞行验收通过，也不自动推广为经过完整验收的发布版本**。
尚未验证飞行 acceptance、与旧镜像逐像素相等的帧输出，也没有升级或验证
`gz-sensors`、`gz-sim`、`gz-transport`；另有独立的 candidate B。CPU 默认仍是原始
`PINNED_IMAGE`，本文件只描述 GPU 默认。

需要回滚时，设置
`MULTIDETECT_SIM_IMAGE=multidetect/px4-sitl-gazebo:transport-frame-09266eb0eb38`，
或恢复 `src/multidetect/simulator/px4_image.py` 中的 `DEFAULT_GPU_IMAGE`。旧镜像仍保留在本机磁盘，
不会因本次切换自动删除。
