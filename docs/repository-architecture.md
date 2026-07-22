# 统一仓库架构

## 目标

`Multi-Detect` 是唯一的工作仓库。机载感知/控制代码与 MultiDetectGCS 源码在同一 Git 根目录中维护，
但仍通过目录边界保持 Python/Jetson 和 Qt/C++/QML 的独立构建工具链。

```text
Multi-Detect/
├── src/multidetect/                 # Jetson 实时感知、融合测距、目标定位、审计
├── scripts/                         # 部署、标定、离线验证
├── tests/                           # Python 回归
├── configs/                         # 运行与模型配置
├── deploy/                          # Jetson/现场部署资料
├── docs/                            # 系统、操作、发布和架构文档
├── ground-control/
│   ├── README.md                    # GCS 导入边界与版本规则
│   └── MultiDetectGCS/              # 定制 QGroundControl 源码
│       ├── custom/                  # Multi-Detect QML、协议和产品配置
│       ├── src/                     # QGroundControl C++ 模块
│       ├── cmake/                   # Qt/CMake/安装器逻辑
│       ├── tools/ 和 test/          # QGC 工具与回归
│       └── .multidetect-upstream.json # 上游基线锚点
└── artifacts/                       # 本机交付物；Git 忽略
```

## Git 与发布边界

- 仅根目录 `Multi-Detect/.git` 是 Git 元数据；`ground-control/MultiDetectGCS` 不含嵌套 Git 仓库。
- QGC 的嵌套 `.gitignore` 与根 `.gitignore` 一起排除构建目录、CPM 缓存、Qt/Android 生成物、虚拟环境、日志和安装包。
- Windows 安装包只保存在 `artifacts/qgc/windows/`，文件名必须为 `MultiDetectGCS-v<version>-windows-amd64.exe`。
- MultiDetectGCS 从 `v0.2.0` 起使用语义版本；功能名称不进入安装包文件名。
- 原 `QGroundControl-MultiDetect` 目录不再是开发入口，保留为迁移回退副本，待统一仓库稳定后再由项目负责人决定归档或删除。
