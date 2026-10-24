"""Offline audit of count episodes for obstacle sidesteps in recorded steps.jsonl."""
from __future__ import annotations

import argparse, json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.root.glob("**/episodes/*/steps.jsonl")):
        sidesteps = []
        final_history = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            action = record.get("action", {})
            if action.get("kind") == "translate" and abs(float(action.get("forward_m", 0))) < 1e-6 and abs(float(action.get("right_m", 0))) >= 10:
                sidesteps.append(record.get("step_index"))
            mission = record.get("labels", {}).get("onboard", {}).get("mission", {})
            final_history = mission.get("history", final_history)
        if not final_history or not any(item.get("skill") == "count" for item in final_history):
            continue
        count = next(item for item in reversed(final_history) if item.get("skill") == "count")
        rows.append({"episode": path.parent.name, "sidestep_step_indices": sidesteps,
                     "before_reason": count.get("reason"),
                     "after_reason": "coverage_partial" if sidesteps else count.get("reason"),
                     "after_count_is_lower_bound": bool(sidesteps) or bool(count.get("count_is_lower_bound", True))})
    result = {"count_episodes": len(rows),
              "affected_by_sidestep": sum(bool(row["sidestep_step_indices"]) for row in rows),
              "unchanged": sum(not row["sidestep_step_indices"] for row in rows),
              "episodes": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
