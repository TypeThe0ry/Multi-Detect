"""Compile task texts into MissionPlans with a llama.cpp model and score them.

Text only: the prompt (``mission_planner.plan_prompt``) lists the skills, the
reference rules and six examples; decoding is constrained by the per-skill JSON
schema. Each plan is checked with ``validate_mission_plan``; on failure the model
gets one retry with the error. Scored against the expected skill sequence on the
success path of hand-written tasks (configs/onboard/plan-eval-tasks.json).
Tasks marked ``expect_invalid: true`` instead require host rejection or repair
to a single return_home fallback. Per-task ``refs`` override the document refs.
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
from multidetect.host_paths import executable_name  # noqa: E402
from multidetect.autonomy.mission_planner import (  # noqa: E402
    PLAN_PROMPT_VARIANTS,
    compile_mission_plan,
    plan_json_schema,
)
from multidetect.autonomy.plan_intent import check_plan_intent  # noqa: E402
from multidetect.autonomy.capability_contract import capability_violations  # noqa: E402
from multidetect.autonomy.mission_package import MissionPackage  # noqa: E402
from multidetect.autonomy.mission_plan import validate_mission_plan, validate_plan_decision  # noqa: E402
from multidetect.autonomy.plan_validation import validate_plan_for_execution  # noqa: E402
from multidetect.autonomy.plan_pregate import preflight_plan_task  # noqa: E402

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
    *, prompt_variant: str = "default", capabilities: frozenset[str] = frozenset(),
    airframe: str | None = None, decision_contract: bool = False,
    plan_pregate: bool = False,
):
    if plan_pregate:
        decision = preflight_plan_task(task, refs, capabilities, airframe)
        if decision is not None:
            raw = json.dumps({
                "plan_version": decision.plan_version,
                "decision": decision.decision,
                "reason_code": decision.reason_code,
                "reason": decision.reason,
                "missing_capabilities": list(decision.missing_capabilities),
                "unresolved_refs": list(decision.unresolved_refs),
                "question": decision.question,
            }, ensure_ascii=False)
            return decision, raw, [0.0], None
    plan, report = compile_mission_plan(
        post, task, refs=refs, schema=schema, prompt_variant=prompt_variant,
        capabilities=capabilities, airframe=airframe,
        include_decision_contract=decision_contract,
    )
    return plan, report["raw"], report["attempts"], report["repairs"] or report["error"]


def _execution_validation(plan: MissionPlan, item: dict[str, Any], prompt: str):
    capabilities = frozenset(item.get("capabilities", ()))
    package = MissionPackage(
        document={
            "capabilities": sorted(capabilities),
            "home": [0.0, 0.0],
            "operating_area": {
                "north_min_m": -1000.0, "north_max_m": 1000.0,
                "east_min_m": -1000.0, "east_max_m": 1000.0,
                "min_altitude_m": 0.0, "max_altitude_m": 1000.0,
            },
            "zones": {}, "routes": {},
            "budget": {"time_s": 100000.0, "reserve_time_s": 0.0,
                       "energy_wh": 100000.0, "reserve_energy_wh": 0.0},
        },
        refs={
            "poi:gate": (20.0, 0.0), "poi:pad": (0.0, 20.0),
            "zone:lot": (0.0, 0.0, 30.0),
        },
        plan=None,
    )
    return validate_plan_for_execution(plan, package, prompt, capabilities=capabilities)


def score_task(
    item: dict[str, Any], plan: MissionPlan | None, outcome: Any, *, raw: str | None = None,
) -> dict[str, Any]:
    """Score a sequence, or a host rejection/repair to the safe home fallback.

    Invalid tasks carry ``expect_invalid: true`` and may omit ``skills``. A
    repaired operational plan is not a refusal, even if its references changed.
    An unmodified home plan alone is also not evidence of host rejection.
    """
    decision = None
    if raw is not None:
        try:
            decision = validate_plan_decision(raw)
        except (TypeError, ValueError, KeyError):
            pass
    if decision is not None:
        valid_code = item.get("reason_code")
        if valid_code is None:
            expected_findings = item.get("expected_findings", ())
            if expected_findings:
                valid_code = next(iter(expected_findings))
            else:
                valid_code = {
                    "missing_payload_drop": "missing_capability",
                    "capability": "missing_capability",
                    "unknown_place": "unknown_place",
                    "unknown_location": "unknown_place",
                    "query_ungrounded": "ungrounded_query",
                    "ungrounded_query": "ungrounded_query",
                    "ambiguous_task": "ambiguous_task",
                    "unsafe_request": "unsafe_request",
                }.get(str(item.get("category", "")))
        correct = bool(item.get("expect_invalid", False)) and decision.decision in {"reject", "clarify"} and (valid_code is None or valid_code == decision.reason_code)
        return {"expected": item.get("skills", []), "predicted": [], "valid": True,
                "exact": correct, "same_skills": correct, "expect_invalid": bool(item.get("expect_invalid", False)),
                "correct_rejection": correct, "decision": decision.decision,
                "reason_code": decision.reason_code, "validator_decision": decision.decision,
                "intent_violations": 0, "capability_violations": 0}
    # The compiler may repair an ungrounded query in its final attempt. Score
    # the original model candidate so live evaluation matches offline replay.
    if raw is not None and item.get("expect_invalid", False):
        try:
            plan = validate_mission_plan(
                raw, capabilities=frozenset(item.get("capabilities", ()))
            )
        except (TypeError, ValueError, KeyError):
            plan = None
    path = success_path(plan) if plan is not None else None
    expected = item.get("skills", [])
    intent = check_plan_intent(plan, item.get("text", ""), item.get("refs")) if plan else []
    caps = item.get("capabilities", ())
    capability = capability_violations(
        plan, caps, airframe="airframe:fixed_wing" if "airframe:fixed_wing" in caps else None
    ) if plan else []
    if item.get("expect_invalid", False):
        validator_categories = {
            "fixed_wing_hover",
            "fixed_wing_target_land",
            "missing_payload_drop",
            "query_ungrounded",
        }
        uses_execution_validator = item.get("category") in validator_categories
        validation = (
            _execution_validation(plan, item, item.get("text", ""))
            if uses_execution_validator and plan
            else None
        )
        correctly_blocked = bool(
            validation and validation.decision in {"reject", "clarify"}
        )
        accepted = plan is None or (
            len(plan.steps) == 1
            and path == ["return_home"]
            and isinstance(outcome, list)
            and bool(outcome)
        )
        if uses_execution_validator and plan is not None:
            accepted = correctly_blocked
        return {
            "expected": expected, "predicted": path, "valid": plan is not None,
            "exact": accepted, "same_skills": accepted, "expect_invalid": True,
            "correct_rejection": accepted,
            "intent_violations": sum(bool(f.blocking) for f in intent),
            "capability_violations": len(capability) if validation and validation.decision in {"accept", "repair"} else 0,
            "validator_decision": validation.decision if validation else ("reject" if plan is None else None),
        }
    return {
        "expected": expected, "predicted": path, "valid": plan is not None,
        "exact": path == expected or path in item.get("alternatives", []),
        "same_skills": path is not None and set(path) == set(expected),
        "correct_rejection": False,
        "intent_violations": sum(bool(f.blocking) for f in intent),
        "capability_violations": len(capability),
    }


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
        # Invalid tasks contribute to language accuracy, not skill confusion.
        expected = [] if row.get("expect_invalid") else row["expected"]
        predicted = [] if row.get("expect_invalid") else row.get("predicted") or []
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
    parser.add_argument(
        "--no-mmproj",
        action="store_true",
        help="run the text-only planner without a vision projector (deployed split parity)",
    )
    parser.add_argument("--port", type=int, default=9957)
    parser.add_argument(
        "--prompt-variant", choices=PLAN_PROMPT_VARIANTS, default="default",
        help="experimental prompt ablation (default keeps the exact training prompt)",
    )
    parser.add_argument("--decision-contract", action="store_true",
                        help="enable structured reject/clarify output (default: legacy MissionPlan)")
    parser.add_argument("--plan-pregate", action="store_true",
                        help="run deterministic host preflight before starting the model")
    parser.add_argument(
        "--llama-server", type=Path,
        default=ROOT / "artifacts/cache/llama-cpp/b11552" / executable_name("llama-server"),
        help="existing llama-server executable (no downloads)",
    )
    parser.add_argument("--device", default="cuda:0", help="cpu or cuda[:index]")
    parser.add_argument(
        "--no-prompt-cache", action="store_true",
        help="disable HTTP prompt caching for full-prefill latency measurements",
    )
    parser.add_argument(
        "--cache-ram-mib", type=int, default=0,
        help="llama-server --cache-ram in MiB (0 disables the host prompt-state cache; the "
        "server default of 8 GiB fills host RAM over a long evaluation)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, help="directory for llama-server logs")
    parser.add_argument("--keep-server-logs", type=int, default=3)
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
    schema = plan_json_schema(include_decision_contract=args.decision_contract)
    if args.sorted_keys:
        from scripts.slow_system_runtime import trained_key_order_schema

        schema = trained_key_order_schema(schema)
    needs_model = not args.plan_pregate or any(
        preflight_plan_task(
            item["text"], item.get("refs", tuple(document.get("refs", ()))),
            frozenset(item.get("capabilities", ())),
            "airframe:fixed_wing" if "airframe:fixed_wing" in item.get("capabilities", ()) else None,
        ) is None
        for item in document["tasks"]
    )
    server = (
        LlamaServerRunner(
            args.gguf, None if args.no_mmproj else args.mmproj,
            executable=args.llama_server, port=args.port,
            max_new_tokens=768, device=args.device, cache_ram_mib=args.cache_ram_mib,
            log_dir=args.log_dir or (args.output.parent / "llama-server-logs"),
            keep_logs=args.keep_server_logs,
        )
        if needs_model else None
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
            task_refs = tuple(item.get("refs", refs))
            capabilities = frozenset(item.get("capabilities", ()))
            airframe = "airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None
            task_schema = plan_json_schema(
                capabilities=capabilities, include_decision_contract=args.decision_contract
            )
            if args.sorted_keys:
                task_schema = trained_key_order_schema(task_schema)
            request_usage.clear()
            plan, raw, attempts, error = compile_task(
                post, item["text"], task_refs, task_schema, prompt_variant=args.prompt_variant,
                capabilities=capabilities, airframe=airframe,
                decision_contract=args.decision_contract,
                plan_pregate=args.plan_pregate,
            )
            rows.append(
                {
                    "text": item["text"],
                    "source": (
                        "host_pregate" if args.plan_pregate and not request_usage else "model"
                    ),
                    "planner_source": (
                        "host_pregate" if args.plan_pregate and not request_usage else "model"
                    ),
                    **score_task(
                        {**item, "refs": item.get("refs", task_refs)}, plan, error, raw=raw
                    ),
                    "attempts_s": attempts,
                    "error": None if isinstance(error, list) else error,
                    "repairs": error if isinstance(error, list) else None,
                    "raw": raw,
                    "prompt_variant": args.prompt_variant,
                    "mmproj_mode": "none" if args.no_mmproj else "configured",
                    "request_usage": [dict(usage) for usage in request_usage],
                    "prompt_tokens": request_usage[0]["prompt_tokens"] if request_usage else None,
                    "prompt_tokens_total": _usage_total(request_usage, "prompt_tokens"),
                    "completion_tokens_total": _usage_total(request_usage, "completion_tokens"),
                    "latency_s": sum(attempts),
                }
            )
            if "language" in item:
                rows[-1]["language"] = item["language"]
            if "category" in item:
                rows[-1]["category"] = item["category"]
            print(
                json.dumps(
                    {k: rows[-1][k] for k in ("text", "expected", "predicted", "valid")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        if server is not None:
            server.close()
    n = len(rows)
    summary = {
        "tasks": n,
        "host_pregate": sum(row.get("source") == "host_pregate" for row in rows),
        "model_processed": sum(row.get("source") != "host_pregate" for row in rows),
        "valid_rate": sum(r["valid"] for r in rows) / n,
        "exact_sequence_rate": sum(r["exact"] for r in rows) / n,
        "same_skill_set_rate": sum(r["same_skills"] for r in rows) / n,
        "repaired_rate": sum(bool(r["repairs"]) for r in rows) / n,
        "first_try_valid_rate": sum(r["valid"] and len(r["attempts_s"]) == 1 for r in rows) / n,
        "median_latency_s": sorted(sum(r["attempts_s"]) for r in rows)[n // 2],
        "model": str(args.gguf.name),
        "mmproj_mode": "none" if args.no_mmproj else "configured",
        "prompt_variant": args.prompt_variant,
        "prompt_cache": not args.no_prompt_cache,
        "cache_ram_mib": args.cache_ram_mib,
        "mean_latency_s": _mean_available(rows, "latency_s"),
        "mean_prompt_tokens": _mean_available(rows, "prompt_tokens"),
        "mean_prompt_tokens_total": _mean_available(rows, "prompt_tokens_total"),
        "mean_completion_tokens_total": _mean_available(rows, "completion_tokens_total"),
        "tasks_with_prompt_usage": sum(r["prompt_tokens_total"] is not None for r in rows),
    }
    diagnosis = compute_diagnosis(rows)
    if any(row.get("category") for row in rows):
        groups = {}
        for category in sorted({row.get("category", "uncategorized") for row in rows}):
            group = [row for row in rows if row.get("category", "uncategorized") == category]
            invalid = [row for row in group if row.get("expect_invalid")]
            groups[category] = {
                "count": len(group), "accuracy": sum(bool(row["exact"]) for row in group) / len(group),
                "rejection_correct_rate": (sum(bool(row.get("correct_rejection")) for row in invalid) / len(invalid) if invalid else None),
                "intent_violation_rate": sum(bool(row.get("intent_violations")) for row in group) / len(group),
                "capability_violation_rate": sum(bool(row.get("capability_violations")) for row in group) / len(group),
            }
        summary["category_metrics"] = groups
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
