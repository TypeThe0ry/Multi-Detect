"""Native v2 worker replay on real stored RGB; explicitly no flight or training.

The replay adapter labels its offline clock and missing navigation truthfully.
It is NOT a live input builder and never creates a Mission/transport. Original
probe/training evidence stays immutable. Every output directory is exclusive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
for _path in (_REPOSITORY_ROOT, _SOURCE_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.onboard_vlm_backend import QwenVlmBackendFactory  # noqa: E402
from multidetect.autonomy.onboard_vlm_runtime_contract import OnboardVlmRuntimeRequest  # noqa: E402
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmProcessWorker  # noqa: E402
from scripts.onboard_vlm_shadow_input import prepare_shadow_case  # noqa: E402
from scripts.probe_onboard_vlm import load_cases, require_model_identity  # noqa: E402


def _record(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def run(args) -> int:
    root = args.repository_root.resolve(strict=True)
    output = args.output_directory.resolve()
    if output.exists():
        raise FileExistsError("runtime replay must not overwrite existing evidence")
    if output.drive.upper() != "D:":
        raise ValueError("this workstation's caches/evidence must stay on D:")
    _document, cases = load_cases(args.cases, args.cases_sha256, root)
    requested = set(args.case_id)
    selected = [case for case in cases if case["case_id"] in requested]
    if {case["case_id"] for case in selected} != requested:
        raise ValueError("requested case identity does not exist in the sealed manifest")
    factory = QwenVlmBackendFactory(
        str(args.snapshot.resolve(strict=True)), args.model_sha256, 8192, args.max_new_tokens
    )
    require_model_identity("Qwen/Qwen3-VL-2B-Instruct", args.snapshot.name)
    sources = [_record(args.cases)] + [
        _record(path)
        for path in (
            Path(__file__),
            root / "src/multidetect/autonomy/onboard_vlm_runtime_contract.py",
            root / "src/multidetect/autonomy/onboard_vlm_backend.py",
            root / "src/multidetect/autonomy/onboard_vlm_worker.py",
            root / "src/multidetect/autonomy/onboard_multimodal.py",
            root / "scripts/onboard_vlm_shadow_input.py",
        )
    ]
    sources.extend(_record(path) for path in sorted(args.snapshot.glob("*.json")))
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": "multidetect.onboard-vlm-process-runtime-replay-v1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_id": "Qwen/Qwen3-VL-2B-Instruct",
        "model_revision": args.snapshot.name,
        "model_sha256": args.model_sha256,
        "source_records": sources,
        "mode": "offline_actual_RGB_replay_unknown_navigation_no_transport",
        "scope_clock": "explicit_offline_replay_not_live_sensor_or_host_clock_proof",
        "adapter_loaded": False,
        "vlm_finetuning_performed": False,
        "flight_action_training_performed": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "autonomous_flight_verified": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "jetson_evidence": False,
        "outdoor_gnss_evidence": False,
        "mission_compiled": False,
        "transport_created": False,
        "execution_authority": False,
        "cases": [],
    }
    all_valid = True
    worker = OnboardVlmProcessWorker(factory, startup_timeout_s=120)
    try:
        worker.start()
        started = time.monotonic()
        while worker.state == "starting" and time.monotonic() - started <= 125:
            time.sleep(0.02)
        if worker.state != "idle":
            report["startup_error"] = worker.error or worker.state
            all_valid = False
        else:
            report["startup_total_s"] = time.monotonic() - started
            report["worker_pid"] = worker.process_id
            for case in selected:
                request = OnboardVlmRuntimeRequest(
                    prepare_shadow_case(case, repository_root=root, deadline_s=args.deadline_s),
                    max_total_pixels=args.max_total_pixels,
                )
                row = {
                    "case_id": case["case_id"],
                    "model_input": request.model_input.to_mapping(),
                    "runtime_request": request.to_mapping(),
                    "request_sha256": request.canonical_hash,
                    "frame_source_records": [
                        _record(root / frame["path"]) for frame in case["frames"]
                    ],
                }
                print(f"native v2 request started: {case['case_id']}", flush=True)
                generation = worker.submit(
                    request, observed_now_s=request.model_input.requested_at_s
                )
                began = time.monotonic()
                while worker.state == "pending" and time.monotonic() - began < (
                    args.deadline_s + 2
                ):
                    time.sleep(0.01)
                row["generation"] = generation
                row["worker_state"] = worker.state
                row["worker_error"] = worker.error
                result = worker.take_result()
                row["strict_intent_accepted"] = result is not None
                row["native_attempt"] = worker.last_attempt
                row["result"] = None if result is None else result.to_mapping()
                all_valid = all_valid and result is not None
                report["cases"].append(row)
                print(
                    f"native v2 request finished: {case['case_id']} "
                    f"accepted={result is not None} state={row['worker_state']}",
                    flush=True,
                )
                if worker.state != "idle":
                    break  # no hidden restart/JSON repair/fallback policy
    finally:
        worker.close()
        report["worker_closed"] = worker.state == "closed"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["native_generations_observed"] = sum(
            row["native_attempt"] is not None for row in report["cases"]
        )
        report["strict_intents_accepted"] = sum(
            row["strict_intent_accepted"] for row in report["cases"]
        )
        report["requested_cases_completed"] = len(report["cases"]) == len(selected)
        report["source_records_unchanged"] = all(
            _record(Path(record["path"])) == record for record in sources
        )
        # A strict native parse is a wiring result, not an autonomous-flight
        # quality or performance gate. Keep those separate even on exit 0.
        code = (
            0
            if all_valid
            and report["requested_cases_completed"]
            and report["source_records_unchanged"]
            else 1
        )
        report["native_exit_code"] = code
        payload = (
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode()
        with (output / "report.json").open("xb") as stream:
            stream.write(payload)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--cases-sha256", required=True)
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--deadline-s", type=float, default=30.0)
    parser.add_argument("--max-total-pixels", type=int, default=401408)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
