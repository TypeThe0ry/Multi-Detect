"""Compile task texts into MissionPlans with a llama.cpp model and score them.

Text only: the prompt (``mission_planner.plan_prompt``) lists the skills, the
reference rules and two examples; decoding is constrained by the per-skill JSON
schema. Each plan is checked with ``validate_mission_plan``; on failure the model
gets one retry with the error. Scored against the expected skill sequence on the
success path of hand-written tasks (configs/onboard/plan-eval-tasks.json).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_plan import MissionPlan, validate_mission_plan  # noqa: E402
from multidetect.autonomy.mission_planner import (  # noqa: E402
    plan_json_schema,
    plan_prompt,
    repair_plan_document,
)
from multidetect.autonomy.serialization import AutonomyContractError  # noqa: E402

GGUF_DIR = (
    ROOT / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503"
)


def success_path(plan: MissionPlan) -> list[str]:
    """Skills executed when every step succeeds (bounded walk)."""
    by_id = {step.id: step for step in plan.steps}
    step, seen, path = plan.steps[0], set(), []
    while step is not None and step.id not in seen and len(path) < 16:
        seen.add(step.id)
        path.append(step.skill)
        step = by_id.get(step.on_success) if step.on_success else None
    return path


def compile_task(post, task: str, refs: tuple[str, ...], schema: dict[str, Any]):
    error, attempts = None, []
    for _ in range(2):
        started = time.perf_counter()
        raw = post(plan_prompt(task, refs=refs, error=error), schema)
        attempts.append(round(time.perf_counter() - started, 2))
        try:
            document, notes = repair_plan_document(json.loads(raw))
            return validate_mission_plan(document), raw, attempts, notes or None
        except (AutonomyContractError, ValueError) as exc:
            error = str(exc)[:200]
    return None, raw, attempts, error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "configs/onboard/plan-eval-tasks.json")
    parser.add_argument("--gguf", type=Path, default=GGUF_DIR / "Qwen3VL-2B-Instruct-Q4_K_M.gguf")
    parser.add_argument(
        "--mmproj", type=Path, default=GGUF_DIR / "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
    )
    parser.add_argument("--port", type=int, default=9957)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from scripts.eval_slow_system import LlamaServerRunner, _http_json

    document = json.loads(args.tasks.read_text(encoding="utf-8"))
    refs = tuple(document.get("refs", ()))
    schema = plan_json_schema()
    server = LlamaServerRunner(args.gguf, args.mmproj, port=args.port, max_new_tokens=768)

    def post(prompt: str, schema: dict[str, Any]) -> str:
        body = {
            "model": "local",
            "temperature": 0,
            "max_tokens": 768,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "mission_plan", "strict": True, "schema": schema},
            },
        }
        response = _http_json(f"http://127.0.0.1:{args.port}/v1/chat/completions", body)
        return response["choices"][0]["message"].get("content", "")

    rows = []
    try:
        for item in document["tasks"]:
            plan, raw, attempts, error = compile_task(post, item["text"], refs, schema)
            path = success_path(plan) if plan else None
            rows.append(
                {
                    "text": item["text"],
                    "expected": item["skills"],
                    "predicted": path,
                    "valid": plan is not None,
                    "exact": path == item["skills"] or path in item.get("alternatives", []),
                    "same_skills": path is not None and set(path) == set(item["skills"]),
                    "attempts_s": attempts,
                    "error": None if isinstance(error, list) else error,
                    "repairs": error if isinstance(error, list) else None,
                    "raw": raw,
                }
            )
            print(
                json.dumps(
                    {k: rows[-1][k] for k in ("text", "expected", "predicted", "valid")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        server.close()
    n = len(rows)
    summary = {
        "tasks": n,
        "valid_rate": sum(r["valid"] for r in rows) / n,
        "exact_sequence_rate": sum(r["exact"] for r in rows) / n,
        "same_skill_set_rate": sum(r["same_skills"] for r in rows) / n,
        "repaired_rate": sum(bool(r["repairs"]) for r in rows) / n,
        "first_try_valid_rate": sum(r["valid"] and len(r["attempts_s"]) == 1 for r in rows) / n,
        "median_latency_s": sorted(sum(r["attempts_s"]) for r in rows)[n // 2],
        "model": str(args.gguf.name),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
