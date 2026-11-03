"""Opt-in, deterministic CPU allocation for Docker SITL slot leases.

CPU IDs are logical host CPUs, not physical cores.  This planner does not
reserve CPUs against unrelated host processes or inspect/start Docker.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _positive_integer(raw: str, label: str) -> int:
    if re.fullmatch(r"[0-9]+", raw.strip()) is None or int(raw) < 1:
        raise ValueError(f"{label} must be a positive integer")
    return int(raw)


def _cpu_list(raw: str, label: str, cpu_count: int) -> tuple[int, ...]:
    cpus: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if re.fullmatch(r"[0-9]+(?:-[0-9]+)?", item) is None:
            raise ValueError(f"{label} must be a CPU list such as '0-5,12,14-19'")
        bounds = item.split("-")
        start, end = int(bounds[0]), int(bounds[-1])
        if start > end:
            raise ValueError(f"{label} has a reversed CPU range: {item}")
        if end >= cpu_count:
            raise ValueError(f"{label} CPU {end} is outside local host range 0-{cpu_count - 1}")
        # Validate bounds before expanding a possibly very large range.
        for cpu in range(start, end + 1):
            if cpu in cpus:
                raise ValueError(f"{label} has overlapping ranges/duplicate CPU {cpu}")
            cpus.add(cpu)
    return tuple(sorted(cpus))


def _format_cpus(cpus: tuple[int, ...]) -> str:
    ranges: list[str] = []
    start = end = cpus[0]
    for cpu in cpus[1:]:
        if cpu == end + 1:
            end = cpu
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = cpu
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(ranges)


@dataclass(frozen=True, slots=True)
class SimulatorCpuIsolation:
    mode: str
    slot_index: int
    slot_count: int
    cpuset_cpus: str
    cpu_shares: int
    cpu_pool: str
    cpus_per_slot: int

    @property
    def docker_options(self) -> tuple[str, ...]:
        return ("--cpuset-cpus", self.cpuset_cpus, "--cpu-shares", str(self.cpu_shares))

    def to_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "slot_index": self.slot_index,
            "slot_count": self.slot_count,
            "cpuset_cpus": self.cpuset_cpus,
            "cpu_shares": self.cpu_shares,
            "cpu_pool": self.cpu_pool,
            "cpus_per_slot": self.cpus_per_slot,
        }


def resolve_cpu_isolation(
    worker_index: int = 0, *, slot_index: int | None = None
) -> SimulatorCpuIsolation | None:
    """Prefer the leased global slot over a planned worker index.

    A supplied ``slot_index`` is for scheduler previews; runtime callers use
    MULTIDETECT_SITL_SLOT, inherited from the owner of the shared SITL lease.
    Validate the *whole* configured pool even when only slot zero is planned.
    """
    selector = os.environ.get("MULTIDETECT_SIM_CPUSET", "").strip()
    if not selector or selector.lower() in {"off", "false", "no"}:
        return None
    if isinstance(worker_index, bool) or not isinstance(worker_index, int) or worker_index < 0:
        raise ValueError("worker_index must be a non-negative integer")
    configured_slots = _positive_integer(
        os.environ.get("MULTIDETECT_SITL_SLOTS", "1"), "MULTIDETECT_SITL_SLOTS"
    )
    if slot_index is None:
        leased_slot = os.environ.get("MULTIDETECT_SITL_SLOT")
        if leased_slot is not None:
            if re.fullmatch(r"[0-9]+", leased_slot.strip()) is None:
                raise ValueError("MULTIDETECT_SITL_SLOT must be a non-negative integer")
            slot_index = int(leased_slot)
            if slot_index >= configured_slots:
                raise ValueError("MULTIDETECT_SITL_SLOT is outside MULTIDETECT_SITL_SLOTS")
        else:
            slot_index = worker_index
    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or slot_index < 0:
        raise ValueError("slot_index must be a non-negative integer")
    slot_count = max(configured_slots, slot_index + 1)
    cpu_count = os.cpu_count()
    if cpu_count is None or cpu_count < 1:
        raise ValueError("cannot determine local host CPU count for MULTIDETECT_SIM_CPUSET")
    shares = _positive_integer(
        os.environ.get("MULTIDETECT_SIM_CPU_SHARES", "4096"), "MULTIDETECT_SIM_CPU_SHARES"
    )
    if not 2 <= shares <= 262144:
        raise ValueError("MULTIDETECT_SIM_CPU_SHARES must be in 2..262144")
    pool_raw = os.environ.get("MULTIDETECT_SIM_CPU_POOL")
    if selector.lower() == "auto":
        pool = _cpu_list(
            pool_raw if pool_raw is not None else f"0-{cpu_count - 1}",
            "MULTIDETECT_SIM_CPU_POOL",
            cpu_count,
        )
        per_slot = _positive_integer(
            os.environ.get("MULTIDETECT_SIM_CPUS_PER_SLOT", "6"),
            "MULTIDETECT_SIM_CPUS_PER_SLOT",
        )
        required = slot_count * per_slot
        if len(pool) < required:
            raise ValueError(
                f"MULTIDETECT_SIM_CPU_POOL has insufficient CPUs: {len(pool)} available; "
                f"{slot_count} slots x {per_slot} CPUs require {required} disjoint CPUs"
            )
        start = slot_index * per_slot
        selected = pool[start : start + per_slot]
        mode = "auto"
    else:
        # A single inherited explicit set would bind concurrent slots to the
        # same CPUs.  Do not silently pretend that such a plan is isolated.
        if configured_slots > 1:
            raise ValueError(
                "explicit MULTIDETECT_SIM_CPUSET would overlap across SITL slots; "
                "use MULTIDETECT_SIM_CPUSET=auto with MULTIDETECT_SIM_CPU_POOL"
            )
        selected = _cpu_list(selector, "MULTIDETECT_SIM_CPUSET", cpu_count)
        pool = (
            _cpu_list(pool_raw, "MULTIDETECT_SIM_CPU_POOL", cpu_count)
            if pool_raw is not None else selected
        )
        if not set(selected).issubset(pool):
            raise ValueError(
                "MULTIDETECT_SIM_CPUSET contains CPUs outside MULTIDETECT_SIM_CPU_POOL"
            )
        per_slot = len(selected)
        mode = "explicit"
    return SimulatorCpuIsolation(
        mode, slot_index, slot_count, _format_cpus(selected), shares, _format_cpus(pool), per_slot
    )
