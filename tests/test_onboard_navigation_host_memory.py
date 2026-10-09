"""WinAPI injection tests, not live host memory or GPU measurements."""

import ctypes
from types import SimpleNamespace

import pytest

from scripts import onboard_navigation_host_memory as memory


class Function:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


def kernel(*, process_ok=True, global_ok=True, change=None):
    def process(handle, pointer, size):
        record = pointer._obj
        assert handle == 123 and size == ctypes.sizeof(record) == record.cb
        record.WorkingSetSize = 10
        record.PeakWorkingSetSize = 20
        record.PagefileUsage = 7
        record.PrivateUsage = 11
        record.PeakPagefileUsage = 30
        if change == "working_set":
            record.WorkingSetSize = 21
        return int(process_ok)

    def status(pointer):
        record = pointer._obj
        assert record.dwLength == ctypes.sizeof(record)
        record.dwMemoryLoad = 60
        record.ullTotalPhys, record.ullAvailPhys = 100, 40
        record.ullTotalPageFile, record.ullAvailPageFile = 200, 50
        record.ullTotalVirtual, record.ullAvailVirtual = 1000, 700
        if change == "load":
            record.dwMemoryLoad = 101
        elif change in {"Phys", "PageFile", "Virtual"}:
            setattr(record, "ullAvail" + change, getattr(record, "ullTotal" + change) + 1)
        return int(global_ok)

    return SimpleNamespace(
        GetCurrentProcess=Function(lambda: 123),
        K32GetProcessMemoryInfo=Function(process),
        GlobalMemoryStatusEx=Function(status),
    )


def test_native_layouts_have_DWORD32_SIZE_T_and_status64():
    process, status = memory.structure_types(ctypes)
    assert process.cb.offset == 0 and process.PeakWorkingSetSize.offset == 8
    assert ctypes.sizeof(process) == 8 + 9 * ctypes.sizeof(ctypes.c_size_t)
    assert ctypes.sizeof(status) == 64 and status.ullTotalPhys.offset == 8
    assert memory.structure_types(ctypes) == (process, status)


def test_commit_is_private_usage_not_resident_or_PagefileUsage():
    result = memory.snapshot(ctypes_module=ctypes, kernel=kernel(), platform="win32")
    assert result["process_working_set_bytes"] == 10
    assert result["process_private_commit_bytes"] == 11
    assert result["process_peak_commit_bytes"] == 30
    assert result["process_limited_commit_limit_bytes"] == 200
    assert result["process_available_commit_bytes"] == 50
    assert result["continuous_sampling"] is False and result["GPU_memory_measured"] is False
    assert result["os_peak_scope"] == "process_lifetime"


@pytest.mark.parametrize("failed", ["process", "global"])
def test_API_failure_is_not_accepted_or_replaced_with_zero(failed):
    with pytest.raises(OSError, match="failed"):
        memory.snapshot(
            ctypes_module=ctypes,
            kernel=kernel(process_ok=failed != "process", global_ok=failed != "global"),
            platform="win32",
        )


@pytest.mark.parametrize("change", ["load", "Phys", "PageFile", "Virtual", "working_set"])
def test_inconsistent_memory_values_fail_closed(change):
    with pytest.raises(ValueError, match="invalid|inconsistent"):
        memory.snapshot(ctypes_module=ctypes, kernel=kernel(change=change), platform="win32")


def test_non_Windows_rejected_before_loading_any_API():
    with pytest.raises(NotImplementedError):
        memory.snapshot(platform="linux")
