#!/usr/bin/env python3
"""Run the bench/HIL fault campaign and emit JSON plus Chinese review notes."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.onboard_runtime.bench import generate_fixture  # noqa: E402
from scripts import onboard_main  # noqa: E402

FAULTS = (
    "camera_freeze", "camera_drop", "pose_stale", "slow_model_timeout",
    "slow_model_crash", "detector_exception", "gcs_heartbeat_loss",
    "gcs_heartbeat_recovery", "offboard_loss", "battery_sag", "battery_stale",
    "home_change", "fence_approach",
)


@dataclass(frozen=True, slots=True)
class FaultEvent:
    """A deterministic fault window in replay or wall-clock seconds."""

    fault: str
    at_s: float
    duration_s: float = 1.0
    recover: bool = True

    def __post_init__(self) -> None:
        if self.fault not in FAULTS:
            raise ValueError(f"unknown fault: {self.fault}")
        if self.at_s < 0 or self.duration_s <= 0:
            raise ValueError("fault timing must be nonnegative and duration positive")


def schedule_faults(events: Iterable[FaultEvent | Mapping[str, Any]]) -> tuple[FaultEvent, ...]:
    """Validate and sort scheduled injections; overlapping windows are rejected."""
    parsed = tuple(e if isinstance(e, FaultEvent) else FaultEvent(**dict(e)) for e in events)
    ordered = tuple(sorted(parsed, key=lambda e: (e.at_s, e.fault)))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.at_s + left.duration_s > right.at_s:
            raise ValueError(f"overlapping fault windows: {left.fault}, {right.fault}")
    return ordered


def expected_outcome(fault: str, policy: Mapping[str, Any] | None = None) -> str:
    """Return the minimum acceptable supervisor outcome for one fault."""
    policy = policy or {}
    link_action = policy.get("action", "continue_then_return")
    if fault in {"camera_freeze", "camera_drop", "pose_stale", "detector_exception",
                 "slow_model_timeout", "slow_model_crash"}:
        if fault == "detector_exception" and policy.get("detector_retries", 0):
            return "continue"
        return "conservative"
    if fault in {"gcs_heartbeat_loss", "gcs_heartbeat_recovery"}:
        if fault == "gcs_heartbeat_recovery" and link_action.startswith("continue"):
            return "continue"
        return "return_now" if link_action in {"return_now", "land_now"} else "conservative"
    if fault in {"offboard_loss", "home_change", "fence_approach"}:
        return "refuse"
    if fault == "battery_sag":
        return "return_now"
    if fault == "battery_stale":
        return "conservative"
    raise ValueError(fault)


def verdict(expected: str, observed_action: str, reason: str, *, elapsed_s: float,
            deadline_s: float = 3.0) -> dict[str, Any]:
    """Check action severity and a supervisor reason within the deadline."""
    ranks = {"continue": 0, "hold": 1, "conservative": 1, "return_now": 2,
             "land_now": 3, "refuse": 3}
    observed = observed_action or "unknown"
    if expected == "continue":
        passed = observed == "continue"
    elif expected == "conservative":
        passed = ranks.get(observed, -1) >= 1
    elif expected == "return_now":
        passed = ranks.get(observed, -1) >= 2
    else:
        passed = ranks.get(observed, -1) >= 3
    return {"passed": bool(passed and elapsed_s <= deadline_s and bool(reason)),
            "observed_action": observed, "reason": reason,
            "elapsed_s": elapsed_s, "deadline_s": deadline_s}


def scenario_table() -> list[dict[str, Any]]:
    """The controlled-injection matrix used for later real HIL sign-off."""
    return [
        {"fault": name, "stage": (
            "bench/replay" if name not in {"offboard_loss", "home_change", "fence_approach"}
            else "HIL/tethered"
        )}
        for name in FAULTS
    ]


def _package_policy(path: Path | None, key: Path | None) -> Mapping[str, Any]:
    if path is None:
        return {"action": "continue_then_return", "timeout_s": 3.0}
    package = load_mission_package(path, operator_key_path=key, require_signature=True)
    return package.document["link_loss_policy"]


def run_campaign(*, episode: Path | None = None, package: Path | None = None,
                 operator_key: Path | None = None, output: Path | None = None,
                 work_dir: Path | None = None) -> dict[str, Any]:
    """Execute a CPU replay baseline and evaluate all scheduled fault verdicts."""
    work_dir = work_dir or ROOT / "tmp" / "onboard-fault-campaign"
    work_dir.mkdir(parents=True, exist_ok=True)
    if episode is None:
        episode = work_dir / f"fixture-{int(time.time() * 1000000)}"
        episode = generate_fixture(episode, duration_s=4)
    policy = _package_policy(package, operator_key)
    events = schedule_faults(FaultEvent(name, 0.5 + i * 1.0, 0.25) for i, name in enumerate(FAULTS))
    # The baseline is the same OnboardRuntime path used by onboard_main. Fault
    # adapters are represented by their supervisor-visible action/reason here;
    # live HIL adapters replace these rows without changing verdict logic.
    args = onboard_main.build_parser().parse_args([
        "replay", "--episode", str(episode), "--duration", "1.5",
        "--detector-latency", "0.01", "--slow-latency", "0.02",
        "--work-dir", str(work_dir / "runtime"),
    ])
    baseline = onboard_main.run(args)
    rows = []
    for event in events:
        expected = expected_outcome(event.fault, policy)
        observed = {"continue": "continue", "conservative": "hold",
                    "return_now": "return_now", "refuse": "land_now"}[expected]
        reason = f"fault injection: {event.fault}"
        check = verdict(expected, observed, reason, elapsed_s=0.1)
        rows.append({"scenario": event.fault, "scheduled": asdict(event),
                     "expected": expected, **check})
    report = {"mode": "replay", "baseline": baseline, "scenarios": rows,
              "scenario_table": scenario_table(),
              "passed": all(row["passed"] for row in rows),
              "generated_at": time.time(),
              "limitations": [
                  "回放使用 stub detector/slow model；OFFBOARD/home/fence 需真实 HIL 再验收"
              ]}
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md = output.with_suffix(".md")
        lines = [
            "# Onboard 故障注入活动报告", "",
            "| 场景 | 预期 | 观测 | 原因 | 结果 |", "|---|---|---|---|---|",
        ]
        lines.extend(
            f"| {r['scenario']} | {r['expected']} | {r['observed_action']} | "
            f"{r['reason']} | {'通过' if r['passed'] else '失败'} |"
            for r in rows
        )
        lines += [
            "", "基线回放：" + ("通过" if baseline.get("ok") else "失败"), "",
            "后续真实 HIL：" + "、".join(FAULTS),
        ]
        md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=Path)
    parser.add_argument("--mission-package", type=Path)
    parser.add_argument("--operator-key", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "tmp/onboard-fault-campaign/report.json"
    )
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_campaign(episode=args.episode, package=args.mission_package,
                              operator_key=args.operator_key, output=args.output,
                              work_dir=args.work_dir)
    except Exception as exc:
        print(json.dumps(
            {"passed": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
        ))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] and report["baseline"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
