"""Re-admit explicit execution artifacts and enforce conservative group splits."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import require_text, strict_json_loads  # noqa: E402
from scripts.admit_sim_execution_record import admit  # noqa: E402


def validate_splits(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
        raise ValueError("1..512 admitted entries required")
    counts = dict.fromkeys(("train", "validation", "test"), 0)
    groups, duplicates = {}, set()
    for entry in entries:
        split = entry["split"]
        if split not in counts:
            raise ValueError("invalid split")
        family = require_text(entry["family"], "family")
        record = entry["record"]
        for field in ("mission_sha256", "journal_sha256"):
            value = record["inputs"][field]
            key = (field, value)
            if key in duplicates:
                raise ValueError(f"duplicate execution artifact: {field}")
            duplicates.add(key)
        # Group by any known shared source, not just the declared family.
        # Semantic task hashes omit volatile task IDs; scenario IDs additionally
        # keep related GPS/geometry perturbations from being split by accident.
        identities = {"declared_family": family, **record["grouping"]}
        for field, value in identities.items():
            key = (field, value)
            if key in groups and groups[key] != split:
                raise ValueError(f"group crosses splits: {field}")
            groups[key] = split
        counts[split] += 1
    return {
        "split_counts": counts,
        "all_splits_present": all(counts.values()),
        "known_group_leakage_detected": False,
        "semantic_independence_proven": False,
        "model_promotion_allowed": False,
    }


def build(path):
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("dataset manifest exceeds 4 MiB")
    manifest = strict_json_loads(raw.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "entries"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or not isinstance(manifest["entries"], list)
        or not 1 <= len(manifest["entries"]) <= 512
    ):
        raise ValueError("invalid execution dataset manifest")
    entries = []
    for row in manifest["entries"]:
        required = {
            "split",
            "family",
            "outer",
            "outer_sha256",
            "mission",
            "mission_sha256",
            "journal",
            "journal_sha256",
        }
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("invalid dataset entry fields")
        paths = {}
        for name in ("outer", "mission", "journal"):
            value = Path(require_text(row[name], name))
            paths[name] = value if value.is_absolute() else path.parent / value
        record = admit(
            paths["outer"],
            row["outer_sha256"],
            paths["mission"],
            row["mission_sha256"],
            paths["journal"],
            row["journal_sha256"],
        )
        entries.append({"family": row["family"], "split": row["split"], "record": record})
    split_checks = validate_splits(entries)
    return {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "entries": entries,
        "split_checks": split_checks,
        "fitting_performed": False,
        "dataset_kind": "successful_sim_receive_window_comparison",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build(args.manifest)
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result["split_checks"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
