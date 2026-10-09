"""Read-only Windows host memory snapshots; never model or GPU telemetry."""

from __future__ import annotations

import os
import sys
import time
from functools import lru_cache


@lru_cache(maxsize=1)
def structure_types(ctypes):
    dword, size_t, uint64 = ctypes.c_uint32, ctypes.c_size_t, ctypes.c_uint64

    class ProcessMemory(ctypes.Structure):
        _fields_ = [("cb", dword), ("PageFaultCount", dword)] + [
            (field, size_t)
            for field in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
                "PrivateUsage",
            )
        ]

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", dword), ("dwMemoryLoad", dword)] + [
            (field, uint64)
            for field in (
                "ullTotalPhys",
                "ullAvailPhys",
                "ullTotalPageFile",
                "ullAvailPageFile",
                "ullTotalVirtual",
                "ullAvailVirtual",
                "ullAvailExtendedVirtual",
            )
        ]

    return ProcessMemory, MemoryStatus


def snapshot(*, ctypes_module=None, kernel=None, platform=None):
    """Boundary snapshot plus process-lifetime OS peaks, not continuous sampling.

    MEMORYSTATUSEX's commit values are process-limited, not necessarily the
    system-wide commit totals. PrivateUsage is commit charge, not resident RAM.
    """
    if (sys.platform if platform is None else platform) != "win32":
        raise NotImplementedError("This evidence sampler requires Windows")
    if ctypes_module is None:
        import ctypes as ctypes_module
    ctypes = ctypes_module
    if kernel is None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    process_type, status_type = structure_types(ctypes)
    process, status = process_type(), status_type()
    process.cb, status.dwLength = ctypes.sizeof(process), ctypes.sizeof(status)
    current = kernel.GetCurrentProcess
    current.argtypes, current.restype = [], ctypes.c_void_p
    get_process = kernel.K32GetProcessMemoryInfo
    get_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(process_type), ctypes.c_uint32]
    get_process.restype = ctypes.c_int
    global_status = kernel.GlobalMemoryStatusEx
    global_status.argtypes, global_status.restype = [ctypes.POINTER(status_type)], ctypes.c_int
    if not get_process(current(), ctypes.byref(process), process.cb):
        raise OSError("K32GetProcessMemoryInfo failed; no snapshot accepted")
    if not global_status(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed; no snapshot accepted")
    if not (0 <= status.dwMemoryLoad <= 100):
        raise ValueError("Windows physical memory load is invalid")
    if (
        any(
            available > total
            for available, total in (
                (status.ullAvailPhys, status.ullTotalPhys),
                (status.ullAvailPageFile, status.ullTotalPageFile),
                (status.ullAvailVirtual, status.ullTotalVirtual),
            )
        )
        or process.WorkingSetSize > process.PeakWorkingSetSize
    ):
        raise ValueError("Windows memory counters are inconsistent")
    return {
        "pid": os.getpid(),
        "observed_at_unix_s": time.time(),
        "source": "K32GetProcessMemoryInfo_PROCESS_MEMORY_COUNTERS_EX_and_GlobalMemoryStatusEx",
        "process_working_set_bytes": int(process.WorkingSetSize),
        "process_peak_working_set_bytes": int(process.PeakWorkingSetSize),
        "process_private_commit_bytes": int(process.PrivateUsage),
        "process_peak_commit_bytes": int(process.PeakPagefileUsage),
        "host_physical_total_bytes": int(status.ullTotalPhys),
        "host_physical_available_bytes": int(status.ullAvailPhys),
        "host_physical_load_percent": int(status.dwMemoryLoad),
        "process_limited_commit_limit_bytes": int(status.ullTotalPageFile),
        "process_available_commit_bytes": int(status.ullAvailPageFile),
        "process_virtual_available_bytes": int(status.ullAvailVirtual),
        "continuous_sampling": False,
        "os_peak_scope": "process_lifetime",
        "GPU_memory_measured": False,
    }
