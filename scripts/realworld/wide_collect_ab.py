#!/usr/bin/env python3
"""Choose the bridge stabilization mode from an A/B batch and prepare or run a final batch."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.realworld import wide_collect_batch as batch  # noqa: E402


def select_mode(results_path, planned_modes):
    """Compare mean feasible-horizon P95 using the latest usable attempt per job."""
    samples = {}
    with results_path.open(encoding="utf-8") as results:
        for line_number, line in enumerate(results, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("expected a JSON object")
            except ValueError as exc:
                raise ValueError(f"{results_path}:{line_number}: {exc}") from exc
            job_id = record.get("job_id")
            # Results report bridge_stabilize. Only specs use gimbal_bridge_stabilize.
            enabled = record.get("bridge_stabilize")
            if (type(enabled) is not bool or not isinstance(job_id, str)
                    or enabled is not planned_modes.get(job_id) or record.get("voided_reason")):
                continue
            bridge_check = record.get("bridge_stabilize_check")
            if isinstance(bridge_check, dict) and bridge_check.get("passed") is False:
                continue
            horizon = record.get("horizon_acceptance")
            feasible = horizon.get("feasible_error") if isinstance(horizon, dict) else None
            p95 = feasible.get("p95_deg") if isinstance(feasible, dict) else None
            if batch.number(p95) and p95 >= 0:
                samples[job_id] = p95

    off = [value for job_id, value in samples.items() if not planned_modes[job_id]]
    on = [value for job_id, value in samples.items() if planned_modes[job_id]]
    if not on or not off:
        raise ValueError(f"need usable horizon P95 from both modes (on={len(on)}, off={len(off)})")
    on_mean, off_mean = statistics.mean(on), statistics.mean(off)
    return on_mean <= off_mean, {"on": (len(on), on_mean), "off": (len(off), off_mean)}


def prepare_final(ab_spec_path, final_spec_path, final_batch_id, final_output_root, final_seeds):
    spec = batch.read_object(ab_spec_path)
    ab_plan = batch.expand(spec)
    ab_root = Path(ab_plan["output_root"]).resolve()
    if final_batch_id == ab_plan["batch_id"] or final_output_root == ab_root:
        raise ValueError("the final batch ID and output root must differ from the A/B batch")
    results_path, choice_path = (ab_root / name for name in ("results.jsonl", "CHOICE.txt"))
    reserved = (ab_spec_path, results_path, choice_path, ab_root / "manifest.json",
                ab_root / "accepted.jsonl", final_output_root / "manifest.json",
                final_output_root / "results.jsonl", final_output_root / "accepted.jsonl")
    if final_spec_path.resolve() in {path.resolve() for path in reserved}:
        raise ValueError("the final spec path overlaps a batch input or output")
    if len(set(final_seeds)) != len(final_seeds):
        raise ValueError("final seeds must be unique")

    planned_modes = {job["job_id"]: job["inputs"].get("gimbal_bridge_stabilize")
                     for job in ab_plan["jobs"]}
    if any(type(mode) is not bool for mode in planned_modes.values()):
        raise ValueError("all A/B jobs must explicitly set gimbal_bridge_stabilize")
    enabled, scores = select_mode(results_path, planned_modes)
    final_spec = dict(spec, batch_id=final_batch_id, output_root=str(final_output_root),
                      jobs=[dict(spec["jobs"][0], seed=seed, gimbal_bridge_stabilize=enabled)
                            for seed in final_seeds])
    final_plan = batch.expand(final_spec)
    manifest = final_output_root / "manifest.json"
    if manifest.exists():
        existing = batch.read_object(manifest)
        if (existing["batch_id"] != final_batch_id
                or {job["job_id"] for job in existing["jobs"]}
                != {job["job_id"] for job in final_plan["jobs"]}):
            raise ValueError(f"final output root already has a different batch plan: {manifest}")

    final_spec_path.parent.mkdir(parents=True, exist_ok=True)
    batch.atomic_text(final_spec_path, json.dumps(final_spec, ensure_ascii=False, indent=2) + "\n")
    choice = "on" if enabled else "off"
    batch.atomic_text(choice_path, f"choice={choice}\n")
    return choice, scores


def wait_for_unit(unit):
    while True:
        result = subprocess.run(["systemctl", "is-active", "--", unit],
                                capture_output=True, text=True, check=False)
        state = result.stdout.strip()
        if result.returncode in (3, 4) and state in ("inactive", "failed", "unknown"):
            return
        if result.returncode != 0 and state not in ("activating", "deactivating"):
            detail = result.stderr.strip() or state or f"exit {result.returncode}"
            raise ValueError(f"could not check unit {unit}: {detail}")
        time.sleep(60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action, help_text in (("choose", "write CHOICE.txt and the final batch JSON"),
                              ("run", "choose and launch the final batch")):
        command = commands.add_parser(action, help=help_text)
        command.add_argument("--ab-spec", required=True, type=Path,
                             help="A/B batch spec whose first job templates the final jobs")
        command.add_argument("--final-spec", required=True, type=Path)
        command.add_argument("--final-batch-id", required=True)
        command.add_argument("--final-output-root", required=True, type=Path)
        command.add_argument("--final-seeds", required=True, nargs="+", type=int)
        if action == "run":
            command.add_argument("--wait-unit", help="systemd A/B unit to wait for before choosing")
            command.add_argument("--runner", type=Path,
                                 help="executable taking the final spec path as its sole argument "
                                      "(default: wide_collect_batch.py with the current Python)")
    args = parser.parse_args(argv)
    try:
        if args.action == "run" and args.wait_unit:
            wait_for_unit(args.wait_unit)
        final_spec = args.final_spec.absolute()
        choice, scores = prepare_final(args.ab_spec, final_spec, args.final_batch_id,
                                       args.final_output_root.resolve(), args.final_seeds)
        print(f"choice={choice}", flush=True)
        for mode, (count, mean) in scores.items():
            print(f"{mode}: {count} jobs, mean feasible horizon P95 {mean:.6g} deg",
                  file=sys.stderr)
        if args.action == "run":
            runner = ([str(args.runner.resolve())] if args.runner is not None else
                      [sys.executable, str(Path(batch.__file__).resolve())])
            rc = subprocess.run([*runner, str(final_spec)], cwd=batch.ROOT, check=False).returncode
            return rc if rc >= 0 else 128 - rc
        return 0
    except (KeyError, OSError, TypeError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
