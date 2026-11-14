"""Size-stratified confirm/approach balancing for identity datasets.

In the identity training data the target box size predicts the decision
(approach targets are tiny, confirm targets are larger), so a model can learn
"small target -> approach" instead of looking at the evidence.  This tool cuts
the confirm/approach rows into area quantile bins (quantiles of the two classes
pooled) and, inside every bin, downsamples both classes to the same row count.
All other rows (reject, search_region, ask_clarify, rows without a usable box)
are kept untouched.

Works on a dataset directory (train.jsonl, optional val/test.jsonl and
manifest.json) and writes a new directory; val/test are copied unchanged.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

CONFIRM = "confirm_target"
APPROACH = "approach_track"


def _target(row: dict) -> dict:
    target = row.get("assistant_target")
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except ValueError:
            return {}
    return target if isinstance(target, dict) else {}


def decision(row: dict) -> str | None:
    return _target(row).get("decision")


def target_area(row: dict) -> float | None:
    """Normalized area of the focus candidate, or None when it cannot be found."""
    args = _target(row).get("args") or {}
    track = args.get("track_id")
    if track is None:
        return None
    for cand in row.get("candidates") or []:
        if cand.get("track_id") == track:
            box = cand.get("box_xyxy_normalized")
            if not box or len(box) != 4:
                return None
            width, height = box[2] - box[0], box[3] - box[1]
            if width <= 0 or height <= 0:
                return None
            return width * height
    return None


def _edges(areas: list[float], bins: int) -> list[float]:
    ordered = sorted(areas)
    edges = []
    for i in range(1, bins):
        edges.append(ordered[min(len(ordered) - 1, (i * len(ordered)) // bins)])
    return edges


def _bin(area: float, edges: list[float]) -> int:
    index = 0
    for edge in edges:
        if area >= edge:
            index += 1
    return index


def rebalance(rows: list[dict], *, bins: int = 8, seed: int = 7, trim: str = "both") -> tuple[list[dict], dict]:
    """trim: both = equalize both classes per bin; approach / confirm = only cut that class where it is the
    majority of the bin, never touch the other class (keeps rare large-box rows)."""
    rng = random.Random(seed)
    cohort: list[tuple[int, str, float]] = []
    for i, row in enumerate(rows):
        label = decision(row)
        if label in (CONFIRM, APPROACH):
            area = target_area(row)
            if area is not None:
                cohort.append((i, label, area))
    if not cohort:
        return list(rows), {"bins": 0, "kept": len(rows), "dropped": 0, "per_bin": []}
    edges = _edges([a for _, _, a in cohort], bins)
    grouped: dict[int, dict[str, list[int]]] = {}
    for i, label, area in cohort:
        grouped.setdefault(_bin(area, edges), {CONFIRM: [], APPROACH: []})[label].append(i)
    drop: set[int] = set()
    per_bin = []
    for b in sorted(grouped):
        sides = grouped[b]
        keep = min(len(sides[CONFIRM]), len(sides[APPROACH]))
        labels = (CONFIRM, APPROACH) if trim == "both" else ((APPROACH,) if trim == "approach" else (CONFIRM,))
        for label in labels:
            ids = sides[label]
            rng.shuffle(ids)
            drop.update(ids[keep:])
        per_bin.append({"bin": b, "confirm_before": len(sides[CONFIRM]),
                        "approach_before": len(sides[APPROACH]), "kept_each": keep})
    kept = [row for i, row in enumerate(rows) if i not in drop]
    return kept, {"bins": len(grouped), "edges": edges, "kept": len(kept),
                  "dropped": len(drop), "per_bin": per_bin}


def run(source: Path, output: Path, *, bins: int, seed: int, trim: str = "both") -> dict:
    rows = [json.loads(line) for line in (source / "train.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    kept, report = rebalance(rows, bins=bins, seed=seed, trim=trim)
    output.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kept)
    (output / "train.jsonl").write_text(text, encoding="utf-8", newline="\n")
    for name in ("val.jsonl", "test.jsonl"):
        if (source / name).is_file():
            shutil.copyfile(source / name, output / name)
    manifest = {}
    if (source / "manifest.json").is_file():
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["rebalance"] = {"source": str(source), "bins": bins, "seed": seed, "trim": trim, **report}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                          encoding="utf-8", newline="\n")
    return manifest["rebalance"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bins", type=int, default=8)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--trim", choices=["both", "approach", "confirm"], default="both")
    args = parser.parse_args(argv)
    report = run(args.source, args.output, bins=args.bins, seed=args.seed, trim=args.trim)
    print(json.dumps({k: v for k, v in report.items() if k != "per_bin"}, ensure_ascii=False))
    for item in report["per_bin"]:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
