"""Explicit, process-local pread compatibility entry for the sealed pilot.

No package is installed or edited here. The standard-library parent reuses the
sealed supervisor; only its own child changes safe_open's loading backend.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from scripts import run_onboard_navigation_pilot_supervised as sup  # noqa: E402

CONTRACT = "multidetect.onboard-navigation-pread-runtime-v1"
FIXED_VERSIONS = {
    "torch": "2.6.0+cu124",
    "transformers": "4.57.6",
    "peft": "0.17.1",
    "accelerate": "1.12.0",
    "safetensors": "0.8.0",
}


def read_runtime(args):
    """Validate sources and versions without importing any heavy framework."""
    manifest_path = Path(args.runtime_manifest).resolve(strict=True)
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Runtime manifest exceeds cap")
    expected = args.runtime_manifest_sha256
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
        or sup.sha256_file(manifest_path) != expected
    ):
        raise ValueError("Runtime manifest SHA differs")
    manifest = sup.strict_json(manifest_path.read_bytes())
    if (
        not isinstance(manifest, dict)
        or manifest.get("contract") != CONTRACT
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("declared_loader_backend") != "pread"
        or manifest.get("package_upgrade_only_claim") is not False
        or manifest.get("plan_sha256") != args.plan_sha256
        or manifest.get("entrypoint_sha256") != sup.sha256_file(__file__)
    ):
        raise ValueError("Runtime manifest contract/plan/entrypoint differs")
    plan, root, _ = sup.read_plan(args.plan, args.plan_sha256)
    if (
        not Path(manifest["artifact_root"]).is_absolute()
        or Path(manifest["artifact_root"]).resolve(strict=True) != root
        or not manifest_path.is_relative_to(root)
        or not Path(manifest["python_executable"]).is_absolute()
        or Path(manifest["python_executable"]).resolve(strict=True)
        != Path(sys.executable).resolve(strict=True)
    ):
        raise ValueError("Runtime root/Python differs from this invocation")
    versions = manifest.get("package_versions")
    if (
        not isinstance(versions, dict)
        or set(versions) != set(FIXED_VERSIONS)
        or any(versions.get(name) != version for name, version in FIXED_VERSIONS.items())
    ):
        raise ValueError("Fixed runtime versions differ")
    for name, version in versions.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or importlib.metadata.version(name) != version
        ):
            raise ValueError("Actual runtime distribution version differs")
    bounds = manifest.get("source_files")
    if not isinstance(bounds, list) or not bounds or len(bounds) > 4096:
        raise ValueError("Runtime must bind a bounded nonempty source set")
    records = {}
    for bound in bounds:
        if not isinstance(bound, dict) or set(bound) != {"path", "bytes", "sha256"}:
            raise ValueError("Unexpected runtime source binding")
        actual = sup.file_record(bound["path"])
        if actual != bound or actual["path"] in records:
            raise ValueError("Runtime source changed/duplicated/noncanonical")
        records[actual["path"]] = actual
    site_packages = Path(sys.executable).resolve(strict=True).parent.parent / "Lib/site-packages"
    spec = importlib.util.find_spec("safetensors")
    if spec is None or spec.origin is None:
        raise ValueError("Safetensors origin is unavailable")
    origin = Path(spec.origin).resolve(strict=True)
    if not origin.is_relative_to(site_packages) or str(origin) not in records:
        raise ValueError("Safetensors must come from the bound new venv, not global packages")
    for source in (manifest_path, Path(__file__), Path(sys.executable)):
        actual = sup.file_record(source)
        records[actual["path"]] = actual
    return manifest, plan, root, [records[key] for key in sorted(records)]


def install_pread_loader():
    """Before Transformers import, return original handles with explicit pread."""
    import safetensors

    if safetensors.__version__ != "0.8.0":
        raise ValueError("Pread entry requires exactly safetensors 0.8.0")
    original = safetensors.safe_open
    signature = getattr(original, "__text_signature__", "") or ""
    if "backend" not in signature:
        raise ValueError("Safetensors does not declare a backend parameter")
    counts = {"safe_open_calls_attempted": 0, "safe_open_calls_returned": 0}

    def pread_open(*positional, **keywords):
        # load_file passes its default mmap explicitly. Override it rather than
        # silently preserving the crashing storage branch or rejecting reload.
        if len(positional) > 3:
            raise ValueError("The compatibility entry requires keyword backend")
        requested = keywords.get("backend", "mmap")
        keywords["backend"] = "pread"
        counts["safe_open_calls_attempted"] += 1
        sup.emit(
            "safe_open_pread_call_started",
            requested_backend=requested,
            effective_backend="pread",
            **counts,
        )
        handle = original(*positional, **keywords)
        counts["safe_open_calls_returned"] += 1
        sup.emit("safe_open_pread_call_returned", effective_backend="pread", **counts)
        return handle

    safetensors.safe_open = pread_open
    import safetensors.torch

    safetensors.torch.safe_open = pread_open
    sup.emit("explicit_pread_loader_installed", change_scope="this_child_only", **counts)
    return counts


def worker(args):
    """Only this private child may import Torch; no budget/model changes."""
    manifest, plan, root, before = read_runtime(args)
    output = sup.resolve_fresh(args.output_dir, root)
    sup.protect_sealed_directories(output, plan, root)
    protect_runtime_output(output, args, manifest)
    counts = None
    returned = 1
    try:
        sup.emit(
            "pread_runtime_verified",
            pid=os.getpid(),
            mode=args.mode,
            plan_sha256=args.plan_sha256,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
        )
        counts = install_pread_loader()
        if args.mode == "load-only":
            returned = sup.worker_load_only(args)
        else:
            from scripts import train_onboard_vlm_navigation_pilot as trainer

            returned = trainer.main(
                [
                    "--plan",
                    str(args.plan),
                    "--plan-sha256",
                    args.plan_sha256,
                    "--output-dir",
                    str(output),
                ]
            )
        _, _, _, after = read_runtime(args)
        if (
            before != after
            or counts["safe_open_calls_returned"] == 0
            or counts["safe_open_calls_attempted"] != counts["safe_open_calls_returned"]
            or type(returned) is not int
        ):
            raise ValueError("Runtime changed or no actual pread load returned")
        return returned
    except Exception:
        returned = 1
        raise
    finally:
        receipt = {
            "contract": CONTRACT,
            **sup.CLAIMS,
            "mode": args.mode,
            "pid": os.getpid(),
            "plan_sha256": args.plan_sha256,
            "runtime_manifest_sha256": args.runtime_manifest_sha256,
            "effective_loader_backend": "pread",
            "package_upgrade_only_claim": False,
            "change_scope": "this_child_only",
            "worker_returncode": returned,
            "observed_calls": counts,
        }
        sup.emit("explicit_pread_worker_finished", **receipt)
        if output.is_dir():
            sup.create_json(output / "pread-loader-report.json", receipt)


def protect_runtime_output(output, args, manifest):
    protected = [Path(args.runtime_manifest).resolve(strict=True).parent]
    protected.extend(
        Path(row["path"]).resolve(strict=True).parent for row in manifest["source_files"]
    )
    if any(output.is_relative_to(directory) for directory in protected):
        raise ValueError("Output cannot pollute a bound runtime directory")


def verify_loader_events(path, receipt, args):
    """Require ordered emitted loader observations, not an invented count report."""
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Loader stdout is missing or exceeds cap")
    installed, verified, finished = False, False, False
    attempted, returned = 0, 0
    for line in path.read_bytes().splitlines():
        if not line.lstrip().startswith(b"{"):
            continue
        row = sup.strict_json(line)
        event = row.get("event") if isinstance(row, dict) else None
        if event == "pread_runtime_verified":
            if (
                verified
                or installed
                or row.get("pid") != receipt.get("pid")
                or row.get("mode") != args.mode
                or row.get("plan_sha256") != args.plan_sha256
                or row.get("runtime_manifest_sha256") != args.runtime_manifest_sha256
            ):
                raise ValueError("Runtime event identity differs")
            verified = True
        elif event == "explicit_pread_loader_installed":
            if installed or not verified or row.get("change_scope") != "this_child_only":
                raise ValueError("Loader installation event differs")
            installed = True
        elif event in ("safe_open_pread_call_started", "safe_open_pread_call_returned"):
            if not installed or finished or row.get("effective_backend") != "pread":
                raise ValueError("Loader event scope/order/backend differs")
            if event.endswith("started") and attempted == returned:
                attempted += 1
            elif event.endswith("returned") and attempted == returned + 1:
                returned += 1
            else:
                raise ValueError("Loader event counts are out of order")
        elif event == "explicit_pread_worker_finished":
            if (
                finished
                or not installed
                or {k: v for k, v in row.items() if k != "event"} != receipt
            ):
                raise ValueError("Finished loader event differs from its report")
            finished = True
        else:
            continue
        if event not in ("pread_runtime_verified",) and (
            type(row.get("safe_open_calls_attempted")) is not int
            or type(row.get("safe_open_calls_returned")) is not int
            or row["safe_open_calls_attempted"] != attempted
            or row["safe_open_calls_returned"] != returned
        ):
            if event != "explicit_pread_worker_finished":
                raise ValueError("Loader event counters differ")
    observed = {"safe_open_calls_attempted": attempted, "safe_open_calls_returned": returned}
    if (
        not finished
        or not attempted
        or attempted != returned
        or receipt["observed_calls"] != observed
    ):
        raise ValueError("Loader events do not prove a completed pread load")
    return observed


def supervise(args, *, popen_factory=sup.subprocess.Popen):
    manifest, _, root, _ = read_runtime(args)
    for output in (args.output_dir, args.supervisor_dir):
        protect_runtime_output(sup.resolve_fresh(output, root), args, manifest)
    original_command, original_capture = sup.child_command, sup.capture_sources

    def capture(plan, root, path):
        _, _, _, runtime_sources = read_runtime(args)
        records = {row["path"]: row for row in original_capture(plan, root, path)}
        records.update({row["path"]: row for row in runtime_sources})
        return [records[key] for key in sorted(records)]

    def command(child_args, plan_path):
        return [
            sys.executable,
            "-u",
            "-B",
            "-X",
            "utf8",
            "-X",
            "faulthandler",
            str(Path(__file__).resolve()),
            "--worker",
            "--mode",
            child_args.mode,
            "--plan",
            str(plan_path),
            "--plan-sha256",
            child_args.plan_sha256,
            "--output-dir",
            str(child_args.output_dir),
            "--runtime-manifest",
            str(Path(args.runtime_manifest).resolve(strict=True)),
            "--runtime-manifest-sha256",
            args.runtime_manifest_sha256,
        ]

    try:
        sup.child_command, sup.capture_sources = command, capture
        returned = sup.supervise(args, popen_factory=popen_factory)
    finally:
        sup.child_command, sup.capture_sources = original_command, original_capture
    receipt_path = Path(args.output_dir) / "pread-loader-report.json"
    receipt = None
    valid = False
    failure = None
    try:
        if not receipt_path.is_file() or receipt_path.stat().st_size > 1024 * 1024:
            raise ValueError("Pread report missing or exceeds cap")
        receipt = sup.strict_json(receipt_path.read_bytes())
        observed = receipt.get("observed_calls") if isinstance(receipt, dict) else None
        valid = (
            isinstance(observed, dict)
            and all(
                type(observed.get(key)) is int and observed[key] > 0
                for key in ("safe_open_calls_attempted", "safe_open_calls_returned")
            )
            and observed["safe_open_calls_attempted"] == observed["safe_open_calls_returned"]
            and receipt.get("contract") == CONTRACT
            and receipt.get("mode") == args.mode
            and receipt.get("plan_sha256") == args.plan_sha256
            and receipt.get("runtime_manifest_sha256") == args.runtime_manifest_sha256
            and receipt.get("effective_loader_backend") == "pread"
            and receipt.get("change_scope") == "this_child_only"
            and receipt.get("package_upgrade_only_claim") is False
            and type(receipt.get("pid")) is int
            and receipt["pid"] > 0
            and type(receipt.get("worker_returncode")) is int
            and receipt["worker_returncode"] == 0
            and all(receipt.get(key) is False for key in sup.CLAIMS)
        )
        verify_loader_events(Path(args.supervisor_dir) / "stdout.bin", receipt, args)
    except Exception as exc:
        valid = False
        failure = {"type": type(exc).__name__, "message": str(exc)}
    result = {
        "contract": CONTRACT,
        **sup.CLAIMS,
        "mode": args.mode,
        "runtime_manifest_sha256": args.runtime_manifest_sha256,
        "sealed_supervisor_exit_code": returned,
        "pread_receipt_valid": valid,
        "pread_receipt_binding": sup.file_record(receipt_path) if receipt_path.is_file() else None,
        "pread_receipt_failure": failure,
        "observed_calls": receipt.get("observed_calls") if isinstance(receipt, dict) else None,
        "supervisor_exit_code": 0 if returned == 0 and valid else 1,
    }
    sup.create_json(Path(args.supervisor_dir) / "pread-completion.json", result)
    return result["supervisor_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--supervisor-dir", type=Path)
    parser.add_argument("--mode", choices=("load-only", "train"), default="load-only")
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            return worker(args)
        if args.supervisor_dir is None:
            raise ValueError("Parent requires a new --supervisor-dir")
        return supervise(args)
    except Exception as exc:
        sup.emit(
            "explicit_pread_invocation_failed", failure_type=type(exc).__name__, failure=str(exc)
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
