"""Check the onboard decision golden replay against a compact baseline."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "golden-replay-manifest.json"
BASELINE = ROOT / "docs" / "golden-replay-baseline.json"
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

_spec = importlib.util.spec_from_file_location(
    "replay_onboard_episode", ROOT / "scripts" / "replay_onboard_episode.py"
)
_replay_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(_replay_mod)
replay = _replay_mod.replay


def resolve_episode(raw: str, data_root: Path | None = None) -> Path:
    path = Path(raw)
    candidates = [ROOT / path] if not path.is_absolute() else [path]
    if data_root:
        candidates.insert(0, data_root / path)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


def sha256_steps(episode: Path) -> str:
    return hashlib.sha256((episode / "steps.jsonl").read_bytes()).hexdigest()


def _num(value: Any) -> int:
    return int(round(float(value or 0.0) * 10))


def fingerprint(document: dict[str, Any]) -> dict[str, Any]:
    trace = document.get("trace", [])
    steps = []
    for item in trace:
        action = item.get("action") or {}
        safety = item.get("safety") or {}
        parameters = [
            _num(action.get(k))
            for k in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")
        ]
        safety_code = (
            "r" if safety.get("rejected")
            else "b" if safety.get("status") == "bypassed" else "a"
        )
        steps.append(
            [
                item.get("step"),
                action.get("kind"),
                [[index, value] for index, value in enumerate(parameters) if value],
                safety_code,
                bool(safety.get("changed_to_hold_stop")),
                sorted(safety.get("reasons") or []),
            ]
        )
    return {
        "steps": steps,
        "terminal": document.get("metrics", {}).get("coverage_complete_replay", False),
    }


def divergence(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any] | None:
    left, right = expected.get("steps", []), actual.get("steps", [])
    for index in range(min(len(left), len(right))):
        if left[index] == right[index]:
            continue
        a, b = left[index], right[index]
        if a[3:] != b[3:]:
            kind = "safety_rejection_changed"
        elif a[1:3] != b[1:3]:
            kind = "action_changed"
        else:
            kind = "task_termination_changed"
        return {
            "index": index,
            "step": b[0] if b else a[0],
            "type": kind,
            "expected": a,
            "actual": b,
        }
    if len(left) != len(right) or expected.get("terminal") != actual.get("terminal"):
        return {
            "index": min(len(left), len(right)),
            "step": None,
            "type": "task_termination_changed",
            "expected_length": len(left),
            "actual_length": len(right),
        }
    return None


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def check(
    *,
    update: bool = False,
    reason: str | None = None,
    data_root: Path | None = None,
    replay_fn: Callable[..., dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    replay_fn = replay_fn or replay
    if data_root is None and not all(resolve_episode(entry["path"]).is_dir() for entry in load_manifest()["episodes"]):
        configured_root = __import__("os").environ.get("MULTIDETECT_DATA_ROOT")
        if configured_root:
            data_root = Path(configured_root).expanduser()
    manifest = load_manifest()
    baseline = (
        json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {"episodes": {}}
    )
    results: list[dict[str, Any]] = []
    missing = 0
    plan_sources: dict[str, int] = {}
    for entry in manifest["episodes"]:
        episode = resolve_episode(entry["path"], data_root)
        row = {"id": entry["id"], "path": entry["path"], "status": "ok"}
        if not (episode / "steps.jsonl").is_file():
            row.update(status="missing", message=f"缺少架次数据: {episode}")
            missing += 1
            results.append(row)
            continue
        digest = sha256_steps(episode)
        if entry.get("sha256") and digest != entry["sha256"]:
            row.update(
                status="hash_mismatch",
                message=f"steps.jsonl sha256 不匹配: {episode}",
                actual_sha256=digest,
            )
            results.append(row)
            continue
        replayed = replay_fn(episode, mode="logged", safety_exit=True)
        source = str(replayed.get("plan_source", "unknown"))
        plan_sources[source] = plan_sources.get(source, 0) + 1
        actual = fingerprint(replayed)
        expected = baseline.get("episodes", {}).get(entry["id"])
        if update:
            baseline.setdefault("episodes", {})[entry["id"]] = {
                "path": entry["path"],
                "sha256": digest,
                "fingerprint": actual,
            }
            row["updated"] = True
        elif expected is None:
            row.update(status="baseline_missing", message="基线缺少该架次")
        else:
            diff = divergence(expected["fingerprint"], actual)
            if diff:
                row.update(status="diverged", divergence=diff)
        results.append(row)
    if update:
        if not reason or not reason.strip():
            raise ValueError("--update-baseline 必须同时提供非空 --reason")
        baseline["schema_version"] = 1
        baseline["updated_reason"] = reason.strip()
        baseline["updated_by"] = "scripts/check_golden_replay.py"
        BASELINE.write_text(
            json.dumps(baseline, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
        )
    divergences = [
        r for r in results if r["status"] in {"diverged", "baseline_missing", "hash_mismatch"}
    ]
    summary = {
        "episodes": len(results),
        "checked": len(results) - missing,
        "missing": missing,
        "plan_sources": plan_sources,
        "fallback_count": plan_sources.get("fallback_count", 0),
        "divergences": len(divergences),
        "results": results,
    }
    return (1 if divergences and not (missing == len(results) and not update) else 0), summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--reason")
    ap.add_argument("--data-root", type=Path, help="optional root for paths in the manifest")
    args = ap.parse_args()
    try:
        code, summary = check(
            update=args.update_baseline, reason=args.reason, data_root=args.data_root
        )
    except Exception as exc:
        print(f"golden replay failed: {exc}", file=sys.stderr)
        return 2
    for row in summary["results"]:
        if row["status"] == "missing":
            print(f"WARNING {row['id']}: {row['message']}")
        elif row["status"] == "diverged":
            d = row["divergence"]
            print(f"DIVERGENCE {row['id']}: step={d['step']} type={d['type']}")
        elif row["status"] == "hash_mismatch":
            print(f"ERROR {row['id']}: {row['message']}")
    print(
        f"golden replay: checked={summary['checked']} missing={summary['missing']} "
        f"divergences={summary['divergences']}"
        f" fallback_count={summary['fallback_count']}"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
