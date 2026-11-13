"""Mix slow-system SFT datasets into one Altair multi-task dataset (CPU only).

Each source is a dataset directory produced by ``scripts/build_slow_system_sft.py``
(``train.jsonl`` plus optional ``val.jsonl`` / ``test.jsonl`` and ``manifest.json``).
Rows are never rewritten (except ``--prefix-ids`` and repeated rows, which change only
the sample_id), so relative ``image_path`` values keep resolving against the repository
root. The mix is deterministic. By default ratios are exact without repetition (the
total is the largest size every source can supply); ``--total N`` fixes the size and
repeats small sources.

    python -m scripts.altair.build_dataset \
        --source planning=artifacts/datasets/X:0.40 --source identity=...:0.20 \
        --output artifacts/datasets/altair-v1 [--total 38000] [--prefix-ids]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def parse_source(text: str) -> tuple[str, Path, float]:
    name, _, rest = text.partition("=")
    path, _, ratio = rest.rpartition(":")
    if not name or not path or not ratio:
        raise ValueError(f"source must look like name=DIR:ratio, got {text!r}")
    value = float(ratio)
    if not value > 0:
        raise ValueError(f"ratio must be positive for {name}")
    return name, Path(path), value


def _crop_policy(directory: Path) -> str:
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return "none"
    return json.loads(manifest.read_text(encoding="utf-8")).get("crop_policy") or "none"


def mix(sources: list[tuple[str, Path, float]], *, seed: int = 20261113, max_train: int = 0,
        total: int = 0, prefix_ids: bool = False, crop_policy: str = "",
        leak_scope: str = "all") -> dict:
    """``total`` > 0 fixes the train size; sources too small for their share are then
    repeated (sample_id suffixed ``#dupN``), otherwise the size is the largest every
    source can supply without repetition."""
    if not sources:
        raise ValueError("at least one source is required")
    names = [name for name, _, _ in sources]
    if len(set(names)) != len(names):
        raise ValueError("duplicate source names")
    policies = {crop_policy} if crop_policy else {_crop_policy(path) for _, path, _ in sources}
    if len(policies) != 1:
        raise ValueError(f"sources disagree on crop_policy: {sorted(policies)}")
    total_ratio = sum(r for _, _, r in sources)
    pools = {}
    for name, path, _ in sources:
        rows = _load(path / "train.jsonl")
        if not rows:
            raise ValueError(f"source {name} has no train rows")
        if any(row.get("split") != "train" for row in rows):
            raise ValueError(f"source {name} train.jsonl has rows outside the train split")
        if prefix_ids:
            rows = [{**row, "sample_id": f"{name}:{row['sample_id']}"} for row in rows]
        pools[name] = rows
    # Largest total such that every source can supply its exact share.
    if total <= 0:
        total = min(len(pools[name]) * total_ratio / ratio for name, _, ratio in sources)
    if max_train:
        total = min(total, max_train)
    rng = random.Random(seed)
    train, counts = [], {}
    for name, _, ratio in sources:
        take = int(total * ratio / total_ratio)
        if take <= 0:
            raise ValueError(f"source {name} would contribute no rows")
        pool = list(pools[name])
        rng.shuffle(pool)
        chosen = []
        for cycle in range((take + len(pool) - 1) // len(pool)):
            if cycle:
                rng.shuffle(pool)
            part = pool[: take - len(chosen)]
            chosen.extend(
                part if cycle == 0 else [{**row, "sample_id": f"{row['sample_id']}#dup{cycle}"} for row in part]
            )
        counts[name] = len(chosen)
        train.extend(chosen)
    rng.shuffle(train)
    ids = [row["sample_id"] for row in train]
    if len(ids) != len(set(ids)):
        raise ValueError("sample_id collision across sources; rename before mixing")
    out = {"train": train}
    held_out = {}
    for split in ("val", "test"):
        merged = []
        for name, path, _ in sources:
            rows = _load(path / f"{split}.jsonl")
            if any(row.get("split") != split for row in rows):
                raise ValueError(f"source {name} {split}.jsonl has rows outside the {split} split")
            held_out[(split, name)] = {row["world"] for row in rows}
            merged.extend(rows)
        out[split] = merged
    train_worlds = {row["world"] for row in train}
    source_train_worlds = {name: {r["world"] for r in pools[name]} for name in pools}
    for (split, name), worlds in held_out.items():
        against = train_worlds if leak_scope == "all" else source_train_worlds[name]
        leaked = against & worlds
        if leaked:
            raise ValueError(f"{split} worlds of {name} leak into train: {sorted(leaked)[:5]}")
    return {"splits": out, "counts": counts, "crop_policy": policies.pop(), "seed": seed}


def write(result: dict, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    sizes = {}
    for split, rows in result["splits"].items():
        if not rows:
            continue
        text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        (output / f"{split}.jsonl").write_text(text, encoding="utf-8", newline="\n")
        digest.update(text.encode("utf-8"))
        sizes[split] = len(rows)
    manifest = {
        "schema": "multidetect.altair-mixed-dataset-v1",
        "crop_policy": result["crop_policy"],
        "seed": result["seed"],
        "train_source_counts": result["counts"],
        "split_sizes": sizes,
        "sha256": digest.hexdigest(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, help="name=DIR:ratio")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--max-train", type=int, default=0)
    parser.add_argument("--total", type=int, default=0, help="fixed train size; small sources are repeated")
    parser.add_argument("--prefix-ids", action="store_true", help="prefix train sample_ids with the source name")
    parser.add_argument("--crop-policy", default="", help="write this crop_policy instead of requiring sources to agree")
    parser.add_argument("--leak-scope", choices=("all", "same-source"), default="all",
                        help="world-leak check: val/test vs all train rows, or only the same source's train")
    args = parser.parse_args(argv)
    result = mix([parse_source(s) for s in args.source], seed=args.seed, max_train=args.max_train,
                 total=args.total, prefix_ids=args.prefix_ids, crop_policy=args.crop_policy,
                 leak_scope=args.leak_scope)
    print(json.dumps(write(result, args.output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
