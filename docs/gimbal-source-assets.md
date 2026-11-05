# x500_gimbal 源资产合同

gimbal 资产与普通 `x500_mono_cam` 使用独立 profile 和来源合同。合同覆盖 PX4 镜像里的四
个原始文件：

* `share/gz/models/x500_gimbal/model.sdf`：必须声明 `x500_gimbal` 组合模型，并 include
  `model://x500` 和 `model://gimbal`；此文件不要求直接声明 `camera_link`。
* `share/gz/models/gimbal/model.sdf`：必须声明 `camera_link` 和 `cgo3_mount_link`。
* `etc/init.d-posix/airframes/4019_gz_x500_gimbal`：airframe 身份由镜像路径名
  `4019_gz_x500_gimbal` 确定；正文必须设置默认值
  `PX4_SIM_MODEL=${PX4_SIM_MODEL:=x500_gimbal}`，并 source
  `etc/init.d-posix/airframes/4001_gz_x500`。
* `etc/init.d-posix/airframes/4001_gz_x500`：4019 启动脚本 source 的基础机型文件，纳入
  同一镜像 revision 的哈希闭包。

提取器不启动容器或仿真。先由镜像维护流程把仓库固定镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
导出到本地目录，再运行。本次快照已由该 digest 执行 `docker create` 并通过 `docker cp`
导出，导出根目录是 `/data/scratch/px4-gimbal-export/opt`：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 python scripts/extract_px4_x500_gimbal_assets.py \
  --image-root /path/to/exported/opt/px4-gazebo \
  --output simulation/gazebo/source-assets/x500_gimbal
```

输出目录只允许新建。合同标识为
`multidetect.px4-x500-gimbal-source-assets-v3`。`extraction.json` 记录 pinned image 引用、四个
image 内原始路径和各自 SHA-256；3-D 验收与独立 verifier 会重新检查这些值、组合模型引用、
gimbal 内必需 link、airframe 路径身份、`x500_gimbal` 默认值和基础 airframe source 关系。测试
使用仓库快照中的真实 airframe 文本作为输入，防止合成文本绕过这些检查。
