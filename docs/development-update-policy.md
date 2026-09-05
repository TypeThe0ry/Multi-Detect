# 仓库更新规范

本规范从 v57 起生效。目标是让任何新会话、开发者或 CI 只读仓库就能判断“改了什么、验证到哪、
哪些能力仍未通过”，不再依赖聊天记录、口头说明或某台机器上的临时输出。

## 状态源顺序

出现冲突时按以下顺序处理：

1. 当前源码、配置、测试和可复核机器证据；
2. `docs/current-progress.json`；
3. `docs/current-progress.md`；
4. `CHANGELOG.md`；
5. 专题架构/操作文档；
6. 历史 README、旧 artifact 摘要和聊天记录。

历史记录只能解释当时状态，不能覆盖当前源码计算出的 parity、测试或发布门。

## 每次功能或修复的强制更新面

每个有意义的功能、行为修复、协议变化、算法变化或验收更新，必须在同一个变更集中包含：

1. **实现**：源码、配置、schema、asset 或构建脚本；
2. **回归**：至少一个能证明新行为的正向测试；风险路径必须包含负向/fail-closed 测试；
3. **CHANGELOG**：在根 `CHANGELOG.md` 的 `[Unreleased]` 最上方记录本轮迭代；
4. **当前进度**：同步更新 `docs/current-progress.md` 和 `docs/current-progress.json`；
5. **路线图**：完成、拆分或新增门禁时更新 `docs/implementation-roadmap.md`；
6. **架构/操作文档**：数据流、接口、目录、命令或硬件步骤变化时更新相应专题文档；
7. **README**：只有稳定入口、安装方式、主能力、版本或目录变化时才更新，禁止重新堆叠逐轮流水；
8. **共享契约**：被 parity 绑定的文件变化时重新运行 `scripts/create_release_parity.py` 并提交
   `configs/release-parity.json`；
9. **证据记录**：新增验收结论时记录输入身份、命令、输出路径、文件大小、SHA-256、门结果、
   负向边界和未声明范围。

只改代码、不更新进度与 CHANGELOG，视为未完成。只写文档、不跑对应验证，也视为未完成。

## 统一状态词

| 状态 | 含义 |
|---|---|
| `implemented` | 源码存在，尚不能推断已验证 |
| `automated_verified` | 单元/集成/协议测试通过 |
| `software_sim_passed` | 明确的 software/SIM 子门通过 |
| `external_sim_passed` | 外部 PX4/Gazebo 进程与输入身份通过 |
| `hil_passed` | 指定 HIL 设备/传输/版本的证据通过 |
| `real_passed` | 指定真实硬件和现场条件的证据通过 |
| `release_blocked` | 任一最终门未满足，禁止发布 |
| `release_allowed` | E7 strict、版本和所有必需证据同时通过 |

低层状态不能自动升级为高层状态。例如 software/SIM 精度通过不能写成“室外定位已完成”，QGC
offscreen boot 不能写成“人工 Fly View 已验收”，localhost UDP 不能写成“GR01 实链已通过”。

## 证据最小字段

每份进入进度记录的证据至少写明：

- 生成时间与产品版本；
- runtime mode、airframe、scenario/model/profile 身份；
- 输入文件路径、字节数与 SHA-256；
- release parity 五组哈希或其绑定摘要；
- 执行命令和退出码；
- 正向门结果和关键量化指标；
- 对应负向/fail-closed 结果；
- `hardware`、`HIL`、`REAL`、`flight`、`production model`、`manual visual` 是否声明；
- verifier 是否从原始绑定输入重新计算，而不是只比较嵌入摘要。

证据文件采用新路径写入，禁止覆盖历史证据。旧证据与当前 parity 不一致时，应保留并标记 stale，
随后从原始 source 重新运行或重建明确界定的 join；禁止直接改旧 JSON 里的 hash。

## artifact 与大型文件

- `artifacts/`、`build/`、模型、原始视频、BGR 帧和日志默认受 `.gitignore` 排除。
- 当前状态、摘要、结论和重放命令必须进入 `docs/current-progress.*`。
- 小型、可审计、对发布必要的 JSON 报告可在评审后显式 `git add -f`；不要批量强加整个目录。
- 模型、视频、原始帧等大型二进制应进入版本化制品存储或 Git LFS，并在仓库记录 URI、大小和
  SHA-256。
- 本机构建的 EXE/portable 不提交到源码历史；发布时通过 GitHub Release 或制品系统分发完整目录。

## 必跑命令

普通 Python/配置变更至少运行：

```powershell
python -m ruff check .
python -m pytest -q
python .\scripts\create_release_parity.py --root .
python -m pytest -q tests\test_release_parity.py
```

QGC C++/QML/protocol 变化还必须运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4
```

并记录 portable 主 EXE 的路径、字节数、SHA-256、native self-tests、offscreen boot 和是否做过人工
Fly View 检查。

## 提交前检查表

- [ ] 当前版本来自 `configs/product-version.json`，没有在多个文件手改出冲突版本。
- [ ] 新行为有正向测试；安全/拒绝路径有负向测试。
- [ ] `python -m ruff check .` 通过。
- [ ] 全量或风险相称的 pytest 通过，数量和唯一 skip 已记录。
- [ ] `configs/release-parity.json` 与当前绑定源码一致。
- [ ] 旧证据在 parity/asset 改变后被正确拒绝，没有静默复用。
- [ ] 新证据有 SHA-256、量化指标、verifier 和明确 claims 边界。
- [ ] `CHANGELOG.md`、`docs/current-progress.md`、`docs/current-progress.json` 已同步。
- [ ] 路线图和受影响专题文档已同步。
- [ ] README 仍是稳定入口，没有重新变成长篇开发流水。
- [ ] `git status` 中没有凭据、私钥、密码、缓存、build tree 或无关大型文件。
- [ ] E7 未通过时，文档明确写 `release_blocked`，没有提前去掉 prerelease。

## 推荐提交格式

```text
<type>(<area>): <outcome>

Implementation:
- ...

Verification:
- pytest: ...
- ruff: ...
- evidence: path / SHA-256 / gate

Claims not made:
- hardware / HIL / REAL / flight / production model / manual visual
```

常用 `type`：`feat`、`fix`、`refactor`、`test`、`docs`、`build`、`release`。
