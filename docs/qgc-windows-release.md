# MultiDetectGCS Windows 发布说明

## 当前发布线：v0.2.0

`MultiDetectGCS` 从 `v0.2.0` 起使用连续的语义版本号。Windows AMD64 安装包只使用版本名：

```text
MultiDetectGCS-v<major>.<minor>.<patch>-windows-amd64.exe
```

功能名称属于发布说明，不进入安装包名、staging 目录名或正式交付路径。当前 `v0.2.0` 由统一仓库内的
`ground-control/MultiDetectGCS/custom/cmake/CustomOverrides.cmake` 定义，构建完成后安装包应归档到：

```text
artifacts/qgc/windows/MultiDetectGCS-v0.2.0-windows-amd64.exe
```

发布时在下表补录实际文件大小和 SHA-256；不要用旧安装包冒充新版本。

| 版本 | 文件 | 状态 |
| --- | --- | --- |
| `v0.2.0` | `MultiDetectGCS-v0.2.0-windows-amd64.exe` | 统一仓库构建验证中 |

## 安装与校验

在 PowerShell 中校验待安装的版本化归档：

```powershell
$package = '.\artifacts\qgc\windows\MultiDetectGCS-v0.2.0-windows-amd64.exe'
Get-FileHash -Algorithm SHA256 $package
```

仅当输出与对应发布记录完全一致时运行安装包。它是本地开发发布，当前未经过生产代码签名；安装前请确认来源，安装后再配置 operator key、MAVLink signing key、RTSP 和 Jetson 地址。不要把密钥、密码或环境文件提交到仓库。

## 可重复构建

当前唯一源码入口是统一仓库内的 `ground-control/MultiDetectGCS`。在已配置 Qt、Visual Studio Build Tools、Ninja 和 NSIS 的 Windows 构建环境中，先加载 `VsDevCmd.bat -arch=x64`，再从仓库根目录执行：

```powershell
cmake -S .\ground-control\MultiDetectGCS -B .\ground-control\MultiDetectGCS\build-v0.2.0-release -G Ninja `
  -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_TOOLCHAIN_FILE=C:\Users\TT\Qt\6.11.1\msvc2022_64\lib\cmake\Qt6\qt.toolchain.cmake
cmake --build .\ground-control\MultiDetectGCS\build-v0.2.0-release --target MultiDetectGCS MultiDetectOperatorProtocolSelfTest
cmake --install .\ground-control\MultiDetectGCS\build-v0.2.0-release --config Release --prefix .\ground-control\MultiDetectGCS\build-v0.2.0-release\staging
```

运行 `MultiDetectOperatorProtocolSelfTest.exe` 并确认退出码为 `0`。最后一步会生成命名为 `MultiDetectGCS-v0.2.0-windows-amd64.exe` 的安装包；归档后记录 EXE 与安装包的 SHA-256。构建目录、staging、依赖缓存和 `artifacts/` 均被 Git 忽略。

## 历史归档

`v0.2.0` 之前的本机开发安装包只保留为历史参考，不属于当前发布线。它们置于 `artifacts/qgc/windows/archive/`，采用不含功能后缀的归档名；不应用于新的部署或验证。

| 归档文件 | SHA-256 |
| --- | --- |
| `MultiDetectGCS-v0.1.0-archive-20260722T143430.exe` | `2D4D76865AD3F603DB9C03CC7B9D7847D9CD8109A29AAE1913D280EE72AA3846` |
| `MultiDetectGCS-v0.1.0-archive-20260722T150210.exe` | `EB400961AEB478A2B63DA68DE9CC0050DE4888C334E6A71BD3AAB541F5DFE15B` |
