"""Create a hash-bound, fail-closed audit of an existing release-gate report."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.field_link_preflight import verify_field_link_report


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"JSON object required: {path}")
    return document


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()


def _resolve_bound_path(base_path: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is missing or empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_path.parent / path
    return path.resolve(strict=True)


def _field_probe(root: Path, field_path: Path) -> dict[str, Any]:
    verified = verify_field_link_report(field_path, require_ready=False)
    if verified.get("gate_passed") is not True:
        raise ValueError(f"field preflight verification failed: {verified.get('reasons')}")
    field = _load(field_path)
    summary = field.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("field preflight summary is missing")
    diagnostic = field.get("interface_diagnostic")
    if not isinstance(diagnostic, dict) or diagnostic.get("required") is not True:
        raise ValueError("field interface diagnostic binding is missing")
    diagnostic_path = _resolve_bound_path(
        field_path, diagnostic.get("report_path"), "field interface diagnostic"
    )
    diagnostic_sha = diagnostic.get("report_sha256")
    diagnostic_bytes = diagnostic.get("report_bytes")
    if diagnostic_sha != _sha256(diagnostic_path):
        raise ValueError("field interface diagnostic digest mismatch")
    if diagnostic_bytes != diagnostic_path.stat().st_size:
        raise ValueError("field interface diagnostic byte count mismatch")
    if diagnostic.get("verified") is not True or diagnostic.get("read_only") is not True:
        raise ValueError("field interface diagnostic must be verified and read-only")
    if summary.get("hardware_link_ready") is not False:
        raise ValueError("field probe override may not claim hardware readiness")
    return {
        "preflight_path": _relative(root, field_path),
        "preflight_bytes": field_path.stat().st_size,
        "preflight_sha256": _sha256(field_path),
        "interface_diagnostic_path": _relative(root, diagnostic_path),
        "interface_diagnostic_bytes": diagnostic_path.stat().st_size,
        "interface_diagnostic_sha256": diagnostic_sha,
        "hardware_link_ready": False,
        "read_only": True,
    }


def create_audit(
    root: Path,
    report_path: Path,
    parity_path: Path,
    current_field_link_path: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    report_path = report_path.resolve(strict=True)
    parity_path = parity_path.resolve(strict=True)
    report = _load(report_path)
    parity = _load(parity_path)["parity"]
    manifest_path = Path(report["manifest_path"]).resolve(strict=True)
    reasons = report["release_blocked_reasons"]
    modes = report["modes"]
    if (
        report.get("gate_passed") is not False
        or report.get("release_allowed") is not False
        or not isinstance(reasons, list)
        or not reasons
        or set(modes) != {"replay", "sim", "hil", "real"}
    ):
        raise ValueError("report is not a complete fail-closed release-gate result")
    mismatches = {
        mode: sorted(
            key for key, value in parity.items() if modes[mode]["parity"].get(key) != value
        )
        for mode in ("replay", "sim", "hil", "real")
    }
    field = report["real_field_link"]["report"]
    field_path = Path(field["path"]).resolve(strict=True)
    if field["sha256"] != _sha256(field_path):
        raise ValueError("field preflight digest mismatch")
    if field.get("bytes") != field_path.stat().st_size:
        raise ValueError("field preflight byte count mismatch")
    # The raw preflight remains authoritative.  Revalidate it even if the
    # release-gate projection already contains a successful diagnostic check.
    # A missing projection can use the verified raw binding; an explicit bad
    # projection must never silently fall back to that raw binding.
    field_probe = _field_probe(root, field_path)
    checks = field.get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError("field preflight checks must be an object")
    if "interface_diagnostic" in checks:
        diagnostic = checks["interface_diagnostic"]
        if (
            not isinstance(diagnostic, dict)
            or diagnostic.get("gate_passed") is not True
            or diagnostic.get("read_only") is not True
        ):
            raise ValueError("field interface diagnostic projection is not verified and read-only")
        diagnostic_path = _resolve_bound_path(
            field_path, diagnostic.get("report_path"), "field interface diagnostic projection"
        )
        if (
            _relative(root, diagnostic_path) != field_probe["interface_diagnostic_path"]
            or diagnostic.get("report_sha256") != field_probe["interface_diagnostic_sha256"]
            or diagnostic.get("report_bytes") != field_probe["interface_diagnostic_bytes"]
        ):
            raise ValueError("field interface diagnostic projection binding mismatch")
    audit = {
        "schema_version": 1,
        "product_version": report["product_version"]["release_label"],
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_runtime_evaluated_at_utc": report["evaluated_at_utc"],
        "runtime_reexecuted": False,
        "parity_rebind_only": True,
        "evidence_scope": "retained_files_audit",
        "evaluated_at_utc": report["evaluated_at_utc"],
        "gate_passed": False,
        "release_allowed": False,
        "blocker_count": len(reasons),
        "release_blocked_reasons": reasons,
        "report_path": _relative(root, report_path),
        "report_bytes": report_path.stat().st_size,
        "report_sha256": _sha256(report_path),
        "manifest_path": _relative(root, manifest_path),
        "manifest_bytes": manifest_path.stat().st_size,
        "manifest_sha256": _sha256(manifest_path),
        "current_parity_file": {
            "path": _relative(root, parity_path),
            "bytes": parity_path.stat().st_size,
            "sha256": _sha256(parity_path),
        },
        "current_parity": parity,
        "mode_parity_mismatches": mismatches,
        "current_release_parity_bound": not any(mismatches.values()),
        "scope": (
            "Retained historical release-gate report re-evaluated against current parity; "
            "no runtime re-execution. Old mode parity and external source drift remain "
            "release blockers."
        ),
        "field_link_probe": field_probe,
    }
    if current_field_link_path is not None:
        current_path = current_field_link_path.resolve(strict=True)
        current_probe = _field_probe(root, current_path)
        if (
            current_path == field_path
            or current_probe["preflight_sha256"] == field_probe["preflight_sha256"]
        ):
            raise ValueError(
                "current field-link probe must be a distinct evidence artifact from the "
                "historical release-gate field-link binding"
            )
        audit["current_field_link_probe"] = current_probe
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--parity", type=Path, default=Path("configs/release-parity.json"))
    parser.add_argument(
        "--current-field-link-preflight",
        type=Path,
        help="Optional newer read-only field-link preflight to bind as current status.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    report_path = args.report if args.report.is_absolute() else root / args.report
    parity_path = args.parity if args.parity.is_absolute() else root / args.parity
    current_field_link_path = (
        None
        if args.current_field_link_preflight is None
        else (
            args.current_field_link_preflight
            if args.current_field_link_preflight.is_absolute()
            else root / args.current_field_link_preflight
        )
    )
    output = args.out if args.out.is_absolute() else root / args.out
    if output.exists():
        parser.error(f"refusing to overwrite: {output}")
    audit = create_audit(root, report_path, parity_path, current_field_link_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "audit": str(output),
                "blocker_count": audit["blocker_count"],
                "parity_bound": audit["current_release_parity_bound"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
