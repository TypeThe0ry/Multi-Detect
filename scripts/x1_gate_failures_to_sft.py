#!/usr/bin/env python3
"""Convert X1 model-gate failures into corrective SFT samples.

The input is the ``model-failures.jsonl`` written by ``scripts/x1_model_gate.sh``
for one or more gate labels.  Each converted sample keeps the exact request
messages that were sent to the model and pairs them with the oracle label.
Rows are deduplicated by request content, unreliable labels and non-model
failures are dropped, categories are capped explicitly, and the result is
written as train/val JSONL in the layout used by the flight-loop SFT data.

This script is CPU-only and intentionally depends on the standard library so
it can run on an old worktree base.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, OrderedDict, defaultdict
from itertools import combinations
from pathlib import Path

DECISION_SCHEMA = "x1.contingency.decision.v1"
TOOL_SCHEMA = "x1.contingency.tool.v1"
FEEDBACK_SCHEMA = "x1.gate-feedback-sft.v1"
DEFAULT_CAP = 100
DEFAULT_VAL_PERCENT = 15
ORACLE_LABELED = "oracle_labeled"
FAILURE_FILES = ("model-failures.jsonl", "failures.jsonl", "all-failures.jsonl")
FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "oracle_label",
        "oracle",
        "expected_action",
        "expected_label",
        "expected_tool",
        "expected_params",
        "ground_truth",
        "truth",
        "failure_reasons",
        "failure_scope",
        "label_status",
        "label_reason",
    }
)


def canonical_json(document) -> str:
    return json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def canonical_sha256(document) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def read_jsonl(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: row must be a JSON object")
            rows.append(row)
    return rows


def _gate_label(gate_dir: Path, gate_json: dict | None = None) -> str:
    if gate_json and isinstance(gate_json.get("label"), str) and gate_json["label"]:
        return gate_json["label"]
    return gate_dir.name


def find_failure_file(gate_dir: Path) -> Path:
    for name in FAILURE_FILES:
        candidate = gate_dir / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no model failure file under {gate_dir}")


def extract_request(messages):
    """Return (request, user_content) from the exact messages sent to the model."""
    if not isinstance(messages, list):
        return None, None, "messages_not_list"
    users = [row for row in messages if isinstance(row, dict) and row.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("content"), str):
        return None, None, "user_message_invalid"
    content = users[0]["content"]
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None, content, "user_content_not_json"
    if not isinstance(payload, dict):
        return None, content, "user_payload_not_object"
    request = payload.get("request")
    if not isinstance(request, dict):
        return None, content, "request_missing"
    return request, content, None


def _find_forbidden_key(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if lowered in FORBIDDEN_REQUEST_KEYS or "oracle" in lowered or lowered.startswith(
                "expected_"
            ):
                return f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
            found = _find_forbidden_key(item, f"{prefix}.{name}" if prefix else name)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_forbidden_key(item, f"{prefix}[{index}]")
            if found:
                return found
    return None


def normalize_target(label, request):
    """Validate an oracle label and echo request identity fields into it."""
    if not isinstance(label, dict):
        return None, "oracle_missing"
    target = dict(label)
    schema = target.get("schema")

    if schema == DECISION_SCHEMA:
        expected_request = request.get("request_id")
        expected_event = request.get("event_id")
        for key, expected in (("request_id", expected_request), ("event_id", expected_event)):
            if expected is None:
                return None, "request_identity_missing"
            if key in target and target[key] != expected:
                return None, "oracle_identity_mismatch"
            target[key] = expected
        state = request.get("state")
        if not isinstance(state, dict):
            return None, "request_state_missing"
        for key in ("mission_id", "revision"):
            expected = state.get(key)
            if expected is None:
                return None, "request_identity_missing"
            if key in target and target[key] != expected:
                return None, "oracle_identity_mismatch"
            target[key] = expected
        action = target.get("action")
        if not isinstance(action, str) or not action:
            return None, "oracle_action_missing"
        offered = request.get("actions")
        if isinstance(offered, list) and offered and action not in offered:
            return None, "oracle_action_not_offered"
        params = target.setdefault("params", {})
        if not isinstance(params, dict):
            return None, "oracle_params_invalid"
        return target, None

    if schema == TOOL_SCHEMA:
        expected_request = request.get("request_id")
        if expected_request is None:
            return None, "request_identity_missing"
        if "request_id" in target and target["request_id"] != expected_request:
            return None, "oracle_identity_mismatch"
        target["request_id"] = expected_request
        perception = request.get("perception")
        if not isinstance(perception, dict):
            return None, "request_perception_missing"
        expected_index = perception.get("call_index")
        if expected_index is None:
            return None, "request_identity_missing"
        if "call_index" in target and target["call_index"] != expected_index:
            return None, "oracle_identity_mismatch"
        target["call_index"] = expected_index
        tool = target.get("tool")
        if not isinstance(tool, str) or not tool:
            return None, "oracle_tool_missing"
        offered = perception.get("tools")
        if isinstance(offered, list) and offered and tool not in offered:
            return None, "oracle_tool_not_offered"
        args = target.setdefault("args", {})
        if not isinstance(args, dict):
            return None, "oracle_args_invalid"
        return target, None

    return None, "oracle_schema_invalid"


def load_holdout_identities(path: Path | None):
    """Collect episode/sample/request identities from a fixed holdout file."""
    episodes, samples, requests = set(), set(), set()
    if path is None:
        return episodes, samples, requests
    for row in read_jsonl(path):
        if isinstance(row.get("episode_id"), str):
            episodes.add(row["episode_id"])
        if isinstance(row.get("sample_id"), str):
            samples.add(row["sample_id"])
        if isinstance(row.get("request_id"), str):
            requests.add(row["request_id"])
        request, _, _ = extract_request(row.get("messages"))
        if request and isinstance(request.get("request_id"), str):
            requests.add(request["request_id"])
    return episodes, samples, requests


def _candidate(gate_dir: Path, label: str, row: dict, holdout):
    scope = row.get("failure_scope")
    if scope == "evidence_scope":
        return None, "evidence_scope"
    if scope == "infrastructure":
        return None, "infrastructure"
    if scope not in (None, "model"):
        return None, f"failure_scope:{scope}"

    status = row.get("label_status")
    if status != ORACLE_LABELED:
        return None, f"label_status:{status}"

    messages = row.get("messages")
    request, content, error = extract_request(messages)
    if error:
        return None, error

    forbidden = _find_forbidden_key(request)
    if forbidden:
        return None, "oracle_field_leak"

    target, error = normalize_target(row.get("oracle_label"), request)
    if error:
        return None, error
    try:
        target_json = canonical_json(target)
    except (TypeError, ValueError):
        return None, "oracle_not_serializable"
    if content and target_json in content:
        return None, "oracle_value_leak"

    request_id = request.get("request_id")
    episode_id = row.get("episode_id")
    sample_id = row.get("sample_id")
    holdout_episodes, holdout_samples, holdout_requests = holdout
    if (
        (isinstance(episode_id, str) and episode_id in holdout_episodes)
        or (isinstance(sample_id, str) and sample_id in holdout_samples)
        or (isinstance(request_id, str) and request_id in holdout_requests)
    ):
        return None, "holdout_collision"

    category = row.get("category")
    if not isinstance(category, str) or not category:
        category = target.get("action") if target.get("schema") == DECISION_SCHEMA else target.get(
            "tool"
        )
    category = str(category or "unknown")

    request_hash = canonical_sha256(request)
    converted = {
        "gate_label": label,
        "gate_dir": str(gate_dir),
        "category": category,
        "request_hash": request_hash,
        "source_episode_id": episode_id,
        "source_sample_id": sample_id,
        "request_id": request_id,
        "messages": [dict(message) for message in messages],
        "target": target,
        "target_json": target_json,
    }
    return converted, None


def _caps_for(categories, default_cap: int, overrides):
    caps = {category: default_cap for category in categories}
    caps.update(overrides)
    return caps


def _take_balanced(group, limit):
    """Pick up to ``limit`` rows, round-robin across gate labels and episodes."""
    buckets = defaultdict(list)
    for item in group:
        buckets[(item["gate_label"], str(item.get("source_episode_id") or ""))].append(item)
    for bucket in buckets.values():
        bucket.sort(
            key=lambda item: (
                str(item.get("source_sample_id") or ""),
                item["request_hash"],
            )
        )
    keys = sorted(buckets)
    chosen = []
    while len(chosen) < limit:
        progressed = False
        for key in keys:
            if buckets[key]:
                chosen.append(buckets[key].pop(0))
                progressed = True
                if len(chosen) >= limit:
                    break
        if not progressed:
            break
    return chosen


def _split_for(episode_id: str, val_percent: int) -> str:
    if val_percent <= 0:
        return "train"
    if val_percent >= 100:
        return "val"
    digest = hashlib.sha256(episode_id.encode("utf-8")).hexdigest()
    return "val" if int(digest[:8], 16) % 100 < val_percent else "train"


def convert(gates, *, cap=DEFAULT_CAP, category_caps=None, val_percent=DEFAULT_VAL_PERCENT,
            holdout_path: Path | None = None):
    """Convert gate directories into train/val rows plus a statistics manifest."""
    category_caps = dict(category_caps or {})
    holdout = load_holdout_identities(holdout_path)
    gates = [Path(path) for path in gates]

    seen = 0
    dedupe = {}
    dropped = Counter()
    duplicates = 0
    by_category = defaultdict(list)
    gate_stats = []
    kept_order = []

    for gate_dir in gates:
        gate_json = None
        gate_path = gate_dir / "gate.json"
        if gate_path.is_file():
            gate_json = json.loads(gate_path.read_text(encoding="utf-8"))
        label = _gate_label(gate_dir, gate_json)
        failure_file = find_failure_file(gate_dir)
        rows = read_jsonl(failure_file)
        gate_stats.append(
            {"label": label, "path": str(gate_dir), "failure_file": str(failure_file),
             "rows_read": len(rows)}
        )
        for row in rows:
            seen += 1
            converted, reason = _candidate(gate_dir, label, row, holdout)
            if reason:
                dropped[reason] += 1
                continue
            request_hash = converted["request_hash"]
            if request_hash in dedupe:
                duplicates += 1
                dropped["duplicate_request"] += 1
                continue
            dedupe[request_hash] = converted
            by_category[converted["category"]].append(converted)
            kept_order.append(converted)

    caps = _caps_for(by_category, cap, category_caps)
    kept = []
    category_stats = OrderedDict()
    for category in sorted(by_category):
        group = sorted(
            by_category[category],
            key=lambda item: (
                str(item.get("source_episode_id") or ""),
                str(item.get("source_sample_id") or ""),
                item["request_hash"],
            ),
        )
        limit = max(0, int(caps[category]))
        chosen = _take_balanced(group, limit)
        kept.extend(chosen)
        category_stats[category] = {
            "unique": len(group),
            "kept": len(chosen),
            "capped": max(0, len(group) - len(chosen)),
            "cap": limit,
        }
        dropped["category_cap"] += max(0, len(group) - len(chosen))

    # Episode-level split: all turns of one source episode stay on one side.
    # Prefix IDs with the gate label because case episode ids repeat per run.
    rows = []
    for item in kept:
        episode_id = f"{item['gate_label']}:{item.get('source_episode_id') or 'unknown'}"
        sample_id = f"{item['gate_label']}:{item.get('source_sample_id') or item['request_id']}"
        rows.append(
            {
                "sample_id": sample_id,
                "episode_id": episode_id,
                "split": _split_for(episode_id, val_percent),
                "category": item["category"],
                "source": "gate_feedback",
                "messages": [
                    *item["messages"],
                    {"role": "assistant", "content": item["target_json"]},
                ],
            }
        )

    # Keep every source episode on one side and pick validation episodes per
    # category so the split stays close to --val-percent without erasing a
    # category from either side.
    categories = defaultdict(Counter)
    for row in rows:
        categories[row["category"]][row["episode_id"]] += 1
    splits = {}
    for category in sorted(categories):
        episodes = sorted(categories[category])
        counts = categories[category]
        if val_percent <= 0 or len(episodes) == 1:
            for episode_id in episodes:
                splits[episode_id] = "train"
            continue
        if val_percent >= 100:
            for episode_id in episodes:
                splits[episode_id] = "val"
            continue
        if len(episodes) > 12:
            for episode_id in episodes:
                splits[episode_id] = _split_for(episode_id, val_percent)
            continue
        target = sum(counts.values()) * val_percent / 100
        best = None
        for size in range(1, len(episodes)):
            for subset in combinations(episodes, size):
                chosen = sum(counts[episode_id] for episode_id in subset)
                key = (abs(chosen - target), chosen, subset)
                if best is None or key < best[0]:
                    best = (key, subset)
        val_episodes = set(best[1])
        for episode_id in episodes:
            splits[episode_id] = "val" if episode_id in val_episodes else "train"
    if rows and val_percent > 0 and not any(value == "val" for value in splits.values()):
        splits[sorted(splits)[-1]] = "val"
    if rows and val_percent < 100 and not any(value == "train" for value in splits.values()):
        splits[sorted(splits)[0]] = "train"
    for row in rows:
        row["split"] = splits[row["episode_id"]]

    for row in rows:
        category_stats[row["category"]]["train"] = category_stats[row["category"]].get(
            "train", 0
        ) + (row["split"] == "train")
        category_stats[row["category"]]["val"] = category_stats[row["category"]].get("val", 0) + (
            row["split"] == "val"
        )

    rows.sort(key=lambda row: (row["split"], row["category"], row["episode_id"], row["sample_id"]))
    return {
        "rows": rows,
        "gates": gate_stats,
        "seen": seen,
        "unique": len(dedupe),
        "duplicates": duplicates,
        "dropped": dropped,
        "category_stats": category_stats,
        "caps": caps,
        "default_cap": cap,
        "category_caps": category_caps,
        "val_percent": val_percent,
        "holdout": str(holdout_path) if holdout_path else None,
        "kept_order": kept_order,
    }


def write_outputs(result, output_dir: Path, *, train_name="train.jsonl", val_name="val.jsonl"):
    output_dir = Path(output_dir)
    for name in (train_name, val_name):
        if "holdout" in Path(name).name.lower():
            raise ValueError("converted gate samples must not be written to a holdout file")
    output_dir.mkdir(parents=True, exist_ok=True)
    train_path = output_dir / train_name
    val_path = output_dir / val_name
    for path, split in ((train_path, "train"), (val_path, "val")):
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for row in result["rows"]:
                if row["split"] == split:
                    stream.write(canonical_json(row) + "\n")
    counts = Counter(row["split"] for row in result["rows"])
    manifest = {
        "schema": FEEDBACK_SCHEMA,
        "gates": result["gates"],
        "counts": {
            "rows_read": result["seen"],
            "unique_requests": result["unique"],
            "duplicate_requests": result["duplicates"],
            "kept": len(result["rows"]),
            "train": counts.get("train", 0),
            "val": counts.get("val", 0),
        },
        "default_cap": result["default_cap"],
        "category_caps": result["category_caps"],
        "caps": result["caps"],
        "val_percent": result["val_percent"],
        "holdout": result["holdout"],
        "categories": result["category_stats"],
        "dropped": dict(result["dropped"]),
        "sha256": {
            train_name: hashlib.sha256(train_path.read_bytes()).hexdigest(),
            val_name: hashlib.sha256(val_path.read_bytes()).hexdigest(),
        },
    }
    (output_dir / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return manifest


def format_table(result) -> str:
    lines = [
        "| 类别 | 去重后 | 保留 | 训练 | 验证 | 上限 | 截断 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category, stats in result["category_stats"].items():
        lines.append(
            f"| {category} | {stats['unique']} | {stats['kept']} | {stats.get('train', 0)} | "
            f"{stats.get('val', 0)} | {stats['cap']} | {stats['capped']} |"
        )
    counts = Counter(row["split"] for row in result["rows"])
    lines.append(
        f"| **合计** | {result['unique']} | {len(result['rows'])} | {counts.get('train', 0)} | "
        f"{counts.get('val', 0)} | — | {result['dropped'].get('category_cap', 0)} |"
    )
    dropped = ", ".join(f"{name}={count}" for name, count in sorted(result["dropped"].items()))
    lines += [
        "",
        f"读取 {result['seen']} 行，唯一请求 {result['unique']}，重复 {result['duplicates']}，"
        f"保留 {len(result['rows'])}（train={counts.get('train', 0)}, "
        f"val={counts.get('val', 0)}）。",
        f"丢弃：{dropped or '无'}",
    ]
    return "\n".join(lines)


def parse_category_caps(values):
    caps = {}
    for value in values or ():
        if "=" not in value:
            raise argparse.ArgumentTypeError("category cap must be CATEGORY=COUNT")
        name, number = value.split("=", 1)
        name = name.strip()
        if not name:
            raise argparse.ArgumentTypeError("category cap requires a category name")
        try:
            count = int(number)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("category cap must be an integer") from exc
        if count < 0:
            raise argparse.ArgumentTypeError("category cap must be non-negative")
        caps[name] = count
    return caps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gates", nargs="+", type=Path, help="gate output directories")
    parser.add_argument("--output", type=Path, required=True, help="directory for train/val JSONL")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP,
                        help=f"default per-category cap (default: {DEFAULT_CAP})")
    parser.add_argument("--category-cap", action="append", default=[], metavar="CATEGORY=COUNT",
                        help="override the cap for one category (repeatable)")
    parser.add_argument("--val-percent", type=int, default=DEFAULT_VAL_PERCENT,
                        help=f"validation percentage by episode (default: {DEFAULT_VAL_PERCENT})")
    parser.add_argument("--holdout", type=Path,
                        help="fixed evaluation holdout JSONL; matching gate samples are excluded")
    parser.add_argument("--train-name", default="train.jsonl")
    parser.add_argument("--val-name", default="val.jsonl")
    args = parser.parse_args(argv)
    if args.cap < 0:
        parser.error("--cap must be non-negative")
    if not 0 <= args.val_percent <= 100:
        parser.error("--val-percent must be between 0 and 100")
    try:
        category_caps = parse_category_caps(args.category_cap)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.holdout is not None and not args.holdout.is_file():
        parser.error("--holdout must be an existing JSONL file")
    try:
        result = convert(
            args.gates,
            cap=args.cap,
            category_caps=category_caps,
            val_percent=args.val_percent,
            holdout_path=args.holdout,
        )
        manifest = write_outputs(
            result, args.output, train_name=args.train_name, val_name=args.val_name
        )
    except (OSError, ValueError) as exc:
        print(f"x1 gate feedback: {exc}", file=sys.stderr)
        return 2
    print(format_table(result))
    print(canonical_json(manifest["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())