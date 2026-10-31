#!/usr/bin/env python3
"""Run the external 15-task return-home probe against one local GGUF."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.eval_slow_system import LlamaServerRunner, _http_json  # noqa: E402
from multidetect.host_paths import executable_name  # noqa: E402
from multidetect.autonomy.mission_plan import PlanDecision  # noqa: E402
from scripts.onboard_policy import build_mission_from_text  # noqa: E402


def success_path(plan):
    by_id = {step.id: step for step in plan.steps}
    step, seen, path = plan.steps[0], set(), []
    while step is not None and step.id not in seen and len(path) < 16:
        seen.add(step.id)
        path.append(step.skill)
        step = by_id.get(step.on_success) if step.on_success else None
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9958)
    parser.add_argument("--decision-contract", action="store_true",
                        help="enable structured reject/clarify output (default: legacy MissionPlan)")
    args = parser.parse_args(argv)
    doc = json.loads(args.tasks.read_text(encoding="utf-8"))
    runner = LlamaServerRunner(args.model, None,
        executable=ROOT / "artifacts/cache/llama-cpp/b11552" / executable_name("llama-server"),
        port=args.port, max_new_tokens=768, crop_policy="none", identity_only=False,
        logprobs=False, wait_port_free=True, cache_ram_mib=0)
    rows = []
    try:
        for task in doc["tasks"]:
            def post(prompt, schema):
                body = {"model": "local", "temperature": 0, "max_tokens": 768,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": "mission_plan", "strict": True, "schema": schema}},
                    "cache_prompt": False}
                return _http_json(f"http://127.0.0.1:{args.port}/v1/chat/completions", body,
                                  timeout=120)["choices"][0]["message"].get("content", "")
            started = time.perf_counter()
            error = None
            try:
                plan, result = build_mission_from_text(task["text"], tuple(doc["refs"]), post,
                    sorted_keys=True, capabilities=frozenset(),
                    include_decision_contract=args.decision_contract)
                if isinstance(plan, PlanDecision):
                    predicted = []
                    correct_rejection = False
                else:
                    predicted = success_path(plan)
                    correct_rejection = bool(result.get("error")) and not predicted
            except Exception as exc:
                predicted, result, error, correct_rejection = [], {}, repr(exc), False
            accepted = predicted in task["expect"]
            row = {"id": task["id"], "text": task["text"], "predicted": predicted,
                   "ok": accepted, "correct_rejection": correct_rejection,
                   "error": error or result.get("error"), "repairs": result.get("repairs"),
                   "seconds": time.perf_counter() - started}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        runner.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
