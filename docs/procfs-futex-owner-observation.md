# 失败阶段的 procfs futex-owner 候选观测

本候选基于`767369810338ff44a37250ec4eb1a2f82b26d703`，只新增独立模块、CPU fixture和测试，**没有接入capture_stall，没有改运行中的767、45秒阈值、GDB顺序/超时或任务断言**。它提供候选地址/字段/owner映射，不能据此宣布NodeShared mutex身份或死锁环。

## 接口与读取上限

```python
from scripts.procfs_futex_owner import observe_known_tasks

observation = observe_known_tasks(
    last_failure_sample,
    verified_layout_proofs,
    candidate_pid=identified_gz_gdb_host_pid,
    proc_root="/proc",
)
```

输入只接受现有失败capture已识别的host-proc任务行，形状为：

```json
{
  "process_threads": {
    "collection_path": "host_proc",
    "threads": {
      "px4": [{"pid": 123, "tid": 123, "starttime_ticks": 456}],
      "gz": [
        {"pid": 789, "tid": 789, "starttime_ticks": 101112},
        {"pid": 789, "tid": 790, "starttime_ticks": 101113}
      ]
    }
  }
}
```

- `pid/tid`是host procfs ID，main行必须`tid==pid`。每个process需要唯一main行及正整数`starttime_ticks`。不是把container-local PID当host PID。
- `candidate_pid`必须明确指定且存在于已识别gz主线程行；建议使用同一capture中`document["gz_gdb"]["pid"]`，不重新发现全机进程，不取syscall寄存器猜PID。未指定时只取任务元信息，不开mem。
- 最多4个已识别process、512条已识别task行。仅读取这些行的status/NSpid/syscall，不枚举其它活进程；owner匹配也只查同一process的已知行。
- 唯一选定GZ主线程当前必须是睡眠态S，且x86_64 syscall202为FUTEX_WAIT或WAIT_BITSET，expected=2、地址8字节对齐且低canonical。地址只取syscall的uaddr，不接受用户任意地址参数，不使用GDB的$rdi。
- 仅在精确libc布局证明匹配、目标ELF64/little/x86_64、地址处于rw映射时，打开mem **O_RDONLY**；从uaddr起读固定20字节两次，每次观测最多**40字节总进程内存**。没有扫描/任意dump/ptrace/代码调用/内存写入。
- 两次读之间重读syscall，前后核对S态；lock/count/owner/nusers/kind不一致则标changed。仅普通recursive kind=1、lock=2、count>=1、owner>0、nusers>=1时给stable candidate。非递归、robust、PI等布局不推断owner。
- 字段owner是namespace TID；只在同process status.NSpid最后一列唯一匹配时报告host LWP候选。不是拿owner数字直接当host TID。
- 每个proc目录先以fd绑定原PID inode，随后经`/proc/self/fd/<fd>`相对读取；读取前后核对`(pid,starttime)`。身份变化则丢弃候选，防数值PID复用重定向。

文本单次读取上限：stat2048B、status4096B、syscall256B、maps256KiB；libc是磁盘文件SHA校验（stat大小<=8MiB），不是进程内存dump。不保存maps全文/libc字节/原始内存dump，只输出结构字段及受限syscall文本。

内部250ms是**有界读取之间检查的软预算**，不承诺内核procfs读或调度的硬wall-time deadline。实际自有child读数约5ms。如果后继诊断需要硬wall bound，应由root另行采用独立观测helper的总超时，而不是延长原GDB预算。当前模块不创建外部进程、不改原捕获的时序。

## 现有_read_host_threads如何安全接线（尚未实施）

`px4_clock_stall_capture.py::_read_host_threads`当前从每个task/stat解析`rest`；其下标19就是starttime（stat字段22）。未来应在**原本读取该行时**加：

```python
"starttime_ticks": int(rest[19]) if len(rest) > 19 else None,
```

main行的值用来绑定process身份；不可在失败后的新时刻才补一个当前starttime伪称旧sample身份。现有container fallback没有host身份证明，应继续拒绝mem观测；缺字段、进程退出、PID复用一律只写error。

root审查后若接线，建议仅在已检测失败并完成两次原timed samples/GDB之后，对最后sample调用**一次**，附加独立`procfs_futex_owner_candidate`字段。这样保持现有GDB顺序/预算与45秒阈值，不用新候选改变任何通过/失败断言。不要因为第一次不是futex就扫描其它地址；不要自动attach补权限。

## 布局证明

`verified_layout_proofs`是已审查JSON对象列表。必须包含以下字段，且目标实际映射libc文件的SHA精确相同：

```json
{
  "architecture": "x86_64",
  "endian": "little",
  "sizeof_mutex": 40,
  "alignof_mutex": 8,
  "offsets": {"lock": 0, "count": 4, "owner": 8, "nusers": 12, "kind": 16},
  "recursive_kind": 1,
  "glibc_version": "实际运行版本",
  "libc_sha256": "实际运行libc的64位SHA256",
  "verification": "owned-pthread-child-raw-owner-roundtrip",
  "proof_id": "关联自有child/helper和真实原始字段验证的标识"
}
```

不能仅用glibc版本字符串或host头文件猜布局。`tests/fixtures/procfs_mutex_child.c`打印真实sizeof/offsetof，构造recursive mutex并由worker锁两次，main进入pthread_mutex_lock；worker约1.8秒后正常释放，整个child有7秒alarm。父进程procfs读数必须与已知地址、worker TID和count2/kind1一致，再保留证明。helper还提供仅供拒绝路径测试的`PROBE_DENY=1`，使**自有child**关闭dumpability。

权限由Linux的ptrace访问检查控制，即使不调用ptrace也可能读不到syscall/mem；见[proc_pid_syscall](https://man7.org/linux/man-pages/man5/proc_pid_syscall.5.html)、[proc_pid_mem](https://man7.org/linux/man-pages/man5/proc_pid_mem.5.html)。拒绝/布局不支持只进入observation_error，保留已读取的futex候选来源，绝不改任务结果或提升权限。

## 实际验证证据（2026-11-09）

证据目录`/data/scratch/procfs-futex-owner-20261109/`，没有真实Gazebo/model metrics。

- Host glibc2.43：自有pthread child roundtrip通过；不将这个布局证明用于b5。
- Ubuntu24 SDK image编译最小C helper后，在精确b5 image运行：runc、network none、1CPU、256MiB RAM/Swap0、pids32、read-only rootfs、cap-drop ALL、no-new-privileges、无GPU/模型/Gazebo/SITL。所有child正常释放退出。
- b5实际glibc2.39，libc SHA `8db37cf3f2169f59a0f07ef1fea308c35656668c64c8ff294e1860f4121eb161`；sizeof40、align8、offsets0/4/8/12/16、kind1，真实40B字段与已知owner/count2匹配。见`b5-proof/layout-proof.json`和`roundtrip.json`。
- Host只读取**本次新建CPU container的helper**，namespace TID映射host LWP成功；见`host-b5-namespace-roundtrip.json`。没有读取当前诊断或其它既有活进程。
- b5 cap-drop ALL父进程观察PROBE_DENY自有child：真实PermissionError，进程内存读取0字节，child正常退出；见`b5-denied-roundtrip.json`。
- 11项必要CPU测试覆盖WAIT解析/ABI/地址限制、container PID拒绝、任务限量、缺starttime、未识别candidate、真实owner、SHA不匹配、stale identity、denied mem、缺candidate只读元信息。ruff检查通过；未运行共享门禁/GPU/SITL。

限制：procfs多次读取不是原子快照，两个相同样本也不能证明从未短暂解锁重入。一个futex uaddr可能属于其它同步对象；kind/count字段吻合仍只是假设。owner映射只覆盖已经识别的task行，缺失就保留unmapped。必须结合新的full-GDB线程栈和独立NodeShared地址证明，才能考虑闭合锁环或制定修复。
