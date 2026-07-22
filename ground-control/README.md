# MultiDetectGCS 源码

`MultiDetectGCS/` 是统一仓库中的定制 QGroundControl 源码。它包含上游 QGroundControl 基线、Multi-Detect 的 `custom/` 应用层、协议自检和桌面/Android 构建配置。

## 版本与发布

- 产品名：`MultiDetectGCS`
- 当前起始版本：`v0.2.0`
- Windows 文件名：`MultiDetectGCS-v<major>.<minor>.<patch>-windows-amd64.exe`
- 构建目录、CPM 缓存、虚拟环境和安装包均被 Git 忽略；只将源码、配置、文档和可重复构建说明纳入版本控制。

Windows 版本由 `MultiDetectGCS/custom/cmake/CustomOverrides.cmake` 的
`QGC_APP_VERSION_OVERRIDE` 决定。每次新版本必须同时更新该值、根 README 的发布链接、
`docs/qgc-windows-release.md` 中的哈希和归档文件名。

## 来源与边界

该目录从原本独立的 `QGroundControl-MultiDetect` 工作树导入；其上游锚点保存在
`MultiDetectGCS/.multidetect-upstream.json`。迁入时没有复制嵌套 `.git`、构建目录、CPM 缓存、
虚拟环境、临时 staging 或旧安装包。原工作树被保留在原位置，仅作为迁移回退副本；之后以本目录
作为 MultiDetectGCS 的唯一开发入口。

`.multidetect-gcs-root` 是 QGC 工具使用的源码根目录标记：QGC 脚本会在这里停止向上查找，
而 Git 元数据仍由外层统一仓库拥有。
