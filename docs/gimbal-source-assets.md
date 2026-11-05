# x500_gimbal 源资产合同

gimbal 资产与普通 `x500_mono_cam` 使用独立 profile 和来源合同。合同覆盖 PX4 镜像里的三
个原始文件：

* `share/gz/models/x500_gimbal/model.sdf`：必须声明 `x500_gimbal` 组合模型，并 include
  `model://x500` 和 `model://gimbal`；此文件不要求直接声明 `camera_link`。
* `share/gz/models/gimbal/model.sdf`：必须声明 `camera_link` 和 `cgo3_mount_link`。
* `etc/init.d-posix/airframes/4019_gz_x500_gimbal`：必须声明 PX4 autostart `4019` 和
  `gz_x500_gimbal`。

提取器不启动容器或仿真。先由镜像维护流程把仓库固定镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
导出到本地目录，再运行：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 python scripts/extract_px4_x500_gimbal_assets.py \
  --image-root /path/to/exported/opt/px4-gazebo \
  --output simulation/gazebo/source-assets/x500_gimbal
```

输出目录只允许新建。`extraction.json` 记录 pinned image 引用、三个 image 内原始路径和
各自 SHA-256；3-D 验收与独立 verifier 会重新检查这些值、组合模型引用、gimbal 内必需 link
和 4019 启动身份。
生成并提交该快照前，需由镜像维护者确认导出目录确实来自上述固定 image digest；仅凭本地
文件树本身无法证明其镜像来源。
