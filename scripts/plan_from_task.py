"""Compile task texts into MissionPlans with a llama.cpp model and score them.

Text only: the prompt (``mission_planner.plan_prompt``) lists the skills, the
reference rules and six examples; decoding is constrained by the per-skill JSON
schema. Each plan is checked with ``validate_mission_plan``; on failure the model
gets one retry with the error. Scored against the expected skill sequence on the
success path of hand-written tasks (configs/onboard/plan-eval-tasks.json).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_plan import MissionPlan  # noqa: E402
from multidetect.autonomy.mission_planner import (  # noqa: E402
    PLAN_PROMPT_VARIANTS,
    compile_mission_plan,
    plan_json_schema,
)

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


def compile_task(
    post, task: str, refs: tuple[str, ...], schema: dict[str, Any],
    *, prompt_variant: str = "default",
):
    plan, report = compile_mission_plan(
        post, task, refs=refs, schema=schema, prompt_variant=prompt_variant
    )
    return plan, report["raw"], report["attempts"], report["repairs"] or report["error"]


def _usage_total(requests: list[dict[str, int | None]], key: str) -> int | None:
    """Unknown usage is unknown, rather than a zero that understates retry cost."""
    values = [item[key] for item in requests]
    return sum(values) if values and all(value is not None for value in values) else None


def _mean_available(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if row[key] is not None]
    return sum(values) / len(values) if values else None


def compute_diagnosis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize task-level recall, aligned skill errors and language accuracy."""
    expected_counts: Counter[str] = Counter()
    recalled_counts: Counter[str] = Counter()
    substitutions: Counter[tuple[str | None, str | None]] = Counter()
    language_counts: Counter[str] = Counter()
    language_matches: Counter[str] = Counter()
    for row in rows:
        expected = row["expected"]
        predicted = row.get("predicted") or []
        expected_counts.update(set(expected))
        recalled_counts.update(set(expected) & set(predicted))
        for opcode, i, j, k, end in SequenceMatcher(
            a=expected, b=predicted, autojunk=False
        ).get_opcodes():
            if opcode == "replace":
                substitutions.update(zip_longest(expected[i:j], predicted[k:end]))
            elif opcode == "delete":
                substitutions.update((skill, None) for skill in expected[i:j])
            elif opcode == "insert":
                substitutions.update((None, skill) for skill in predicted[k:end])
        language = row.get("language") or (
            "zh"
            if re.search(
                r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]", row["text"]
            )
            else "en"
        )
        language_counts[language] += 1
        language_matches[language] += bool(row["exact"])
    return {
        "expected_skill_recall": {
            skill: recalled_counts[skill] / count
            for skill, count in sorted(expected_counts.items())
        },
        "substitutions": [
            {"expected_skill": expected, "predicted_skill": predicted, "count": count}
            for (expected, predicted), count in sorted(
                substitutions.items(),
                key=lambda item: (-item[1], item[0][0] or "", item[0][1] or ""),
            )
        ],
        "language_exact_match_rate": {
            language: language_matches[language] / count
            for language, count in sorted(language_counts.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "configs/onboard/plan-eval-tasks.json")
    parser.add_argument("--gguf", type=Path, default=GGUF_DIR / "Qwen3VL-2B-Instruct-Q4_K_M.gguf")
    parser.add_argument(
        "--mmproj", type=Path, default=GGUF_DIR / "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
    )
    parser.add_argument("--port", type=int, default=9957)
    parser.add_argument(
        "--prompt-variant", choices=PLAN_PROMPT_VARIANTS, default="default",
        help="experimental prompt ablation (default keeps the exact training prompt)",
    )
    parser.add_argument(
        "--llama-server", type=Path,
        default=ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe",
        help="existing llama-server executable (no downloads)",
    )
    parser.add_argument("--device", default="cuda:0", help="cpu or cuda[:index]")
    parser.add_argument(
        "--no-prompt-cache", action="store_true",
        help="disable HTTP prompt caching for full-prefill latency measurements",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--sorted-keys",
        action="store_true",
        help="constrain keys in sorted order (models fine-tuned on sort_keys answers)",
    )
    args = parser.parse_args(argv)
    from scripts.eval_slow_system import LlamaServerRunner, _http_json

    document = json.loads(args.tasks.read_text(encoding="utf-8"))
    if not document["tasks"]:
        parser.error("the task file must contain at least one task")
    refs = tuple(document.get("refs", ()))
    schema = plan_json_schema()
    if args.sorted_keys:
        from scripts.slow_system_runtime import trained_key_order_schema

        schema = trained_key_order_schema(schema)
    server = LlamaServerRunner(
        args.gguf, args.mmproj, executable=args.llama_server, port=args.port,
        max_new_tokens=768, device=args.device,
    )
    request_usage: list[dict[str, int | None]] = []

    def post(prompt: str, schema: dict[str, Any]) -> str:
        measured: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None}
        request_usage.append(measured)
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
        if args.no_prompt_cache:
            body["cache_prompt"] = False
        response = _http_json(f"http://127.0.0.1:{args.port}/v1/chat/completions", body)
        usage = response.get("usage") or {}
        for key in measured:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                measured[key] = value
        return response["choices"][0]["message"].get("content", "")

    rows = []
    try:
        for item in document["tasks"]:
            request_usage.clear()
            plan, raw, attempts, error = compile_task(
                post, item["text"], refs, schema, prompt_variant=args.prompt_variant,
            )
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
                    "prompt_variant": args.prompt_variant,
                    "request_usage": [dict(usage) for usage in request_usage],
                    "prompt_tokens": request_usage[0]["prompt_tokens"] if request_usage else None,
                    "prompt_tokens_total": _usage_total(request_usage, "prompt_tokens"),
                    "completion_tokens_total": _usage_total(request_usage, "completion_tokens"),
                    "latency_s": sum(attempts),
                }
            )
            if "language" in item:
                rows[-1]["language"] = item["language"]
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
        "prompt_variant": args.prompt_variant,
        "prompt_cache": not args.no_prompt_cache,
        "mean_latency_s": _mean_available(rows, "latency_s"),
        "mean_prompt_tokens": _mean_available(rows, "prompt_tokens"),
        "mean_prompt_tokens_total": _mean_available(rows, "prompt_tokens_total"),
        "mean_completion_tokens_total": _mean_available(rows, "completion_tokens_total"),
        "tasks_with_prompt_usage": sum(r["prompt_tokens_total"] is not None for r in rows),
    }
    diagnosis = compute_diagnosis(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"summary": summary, "diagnosis": diagnosis, "rows": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    top = diagnosis["substitutions"][:5]
    print(
        "Top skill substitutions: " + (
            "; ".join(
                f"{item['expected_skill'] or '(insert)'} -> "
                f"{item['predicted_skill'] or '(delete)'}: {item['count']}"
                for item in top
            ) if top else "none"
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
