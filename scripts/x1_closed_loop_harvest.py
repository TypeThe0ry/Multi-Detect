"""Harvest failed model turns and additive metrics from X1 decision audit rows.

Use the X1 contract checkout when the calling workspace lacks the oracle.
The input is one attempt's JSONL records in append order. Only sent turns with
an oracle answer and recorded messages carry an assistant training target.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path


# The suite can be staged before the contract sources land in its worktree.
# Scope the extra search paths to these imports.
_local_oracle = Path(__file__).with_name("build_x1_flight_loop_sft.py")
_contract_root = Path("/data/scratch/cx-wc-sampling")
_contract_paths = (
    [str(_contract_root / "src"), str(_contract_root)]
    if not _local_oracle.is_file() and (_contract_root / "scripts" / _local_oracle.name).is_file()
    else []
)
_original_path = sys.path.copy() if _contract_paths else None
sys.path[:0] = _contract_paths
try:
    from multidetect.autonomy.serialization import canonical_json, strict_json_loads
    from multidetect.autonomy.x1_contingency_executor import DECISION_SCHEMA, MAX_DECISION_BYTES, decode_decision
    from multidetect.autonomy.x1_perception_tools import TOOL_SCHEMA, ToolCall
    from scripts.build_x1_flight_loop_sft import ADMITTED, policy
finally:
    if _original_path is not None:
        sys.path[:] = _original_path

_ADMITTED_STATUSES = tuple(ADMITTED | {"applied"})


# Inclusive upper bounds and an overflow bucket. Backend and receipt times
# have different starting points and are never added together.
LATENCY_BOUNDS_MS = (50, 100, 250, 500, 1000, 2000, 3000, 5000)
_LATENCY_KEYS = tuple(f"le_{bound}" for bound in LATENCY_BOUNDS_MS) + (f"over_{LATENCY_BOUNDS_MS[-1]}",)
_COUNTS = (
    "attempts", "requests", "requests_opened", "requests_unsent", "turns_sent",
    "turn_results", "receipts", "missing_results", "missing_receipts", "backend_errors",
    "late_results", "disrupted_requests", "orphan_results", "duplicate_trace_rows",
    "invalid_trace_rows", "schema_valid", "identity_valid", "oracle_labeled_turns",
    "oracle_unlabelable_turns", "expected_action_turns", "action_matches",
    "expected_tool_turns", "tool_matches", "tool_use_correct", "quiet_cruise_turns",
    "quiet_unnecessary_actions", "quiet_unnecessary_tools", "retasks_proposed",
    "retasks_required", "retasks_installed", "required_retasks_installed",
    "deadline_on_time", "deadline_missed", "deadline_unknown", "failure_rows",
)


def _metrics():
    return {
        **dict.fromkeys(_COUNTS, 0),
        "turn_latency_ms_histogram": dict.fromkeys(_LATENCY_KEYS, 0),
        "receipt_latency_ms_histogram": dict.fromkeys(_LATENCY_KEYS, 0),
        "receipt_status_counts": {},
        "failure_reason_counts": {},
    }


def merge_metrics(metrics: Iterable[dict]) -> dict:
    """Sum counts and histograms across attempts, including empty input."""
    merged = _metrics()
    for attempt in metrics:
        for key, value in merged.items():
            if isinstance(value, dict):
                for bucket, count in attempt[key].items():
                    value[bucket] = value.get(bucket, 0) + count
            else:
                merged[key] += attempt[key]
    return merged


def _count(histogram, key):
    histogram[key] = histogram.get(key, 0) + 1


def _ms(value):
    if type(value) in (int, float) and value >= 0 and (type(value) is int or math.isfinite(value)):
        return value
    return None


def _record_latency(histogram, value):
    value = _ms(value)
    if value is not None:
        bucket = next((f"le_{bound}" for bound in LATENCY_BOUNDS_MS if value <= bound), _LATENCY_KEYS[-1])
        histogram[bucket] += 1


def _deadline_budget(request):
    if not isinstance(request, dict) or not isinstance(request.get("state"), dict):
        return None
    start = _ms(request["state"].get("t_ms"))
    deadline = _ms(request.get("deadline_ms"))
    return None if start is None or deadline is None else deadline - start


def _deadline_outcome(request, receipt):
    if receipt.get("reason") in ("decision_timeout", "window_timeout"):
        return "missed"
    elapsed = _ms(receipt.get("latency_ms"))
    budget = _deadline_budget(request)
    if elapsed is None or budget is None:
        return "unknown"
    return "missed" if elapsed > budget else "on_time"


def _disrupted(receipt):
    reason = receipt.get("reason")
    return isinstance(reason, str) and (
        reason in {
            "preempted_by_event", "collector_exit", "cancelled", "stale_reply",
            "superseded_mission_or_event", "decision_or_frame_stale",
        }
        or (reason.startswith(("cadence_", "event_")) and reason.endswith("_changed"))
    )


def _oracle(request):
    if not isinstance(request, dict):
        return None, "missing_request"
    if not isinstance(request.get("perception"), dict):
        return None, "missing_perception"
    try:
        return policy(request), None
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
        return None, "incomplete_or_invalid_request"


def _decode(raw, request):
    """Return parsed JSON, reply kind, and whether identity and offer match."""
    parsed = None
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DECISION_BYTES:
            return parsed, None, False, False
        parsed = strict_json_loads(raw)
        if not isinstance(parsed, dict):
            return None, None, False, False
        canonical_json(parsed).encode("utf-8")
        if parsed.get("schema") == TOOL_SCHEMA:
            call = ToolCall.parse(parsed)
            perception = request.get("perception") or {}
            identity = (
                call.request_id == request.get("request_id")
                and type(perception.get("call_index")) is int
                and call.call_index == perception["call_index"]
            )
            offered = perception.get("calls_remaining", 0) > 0 and call.tool in perception.get("tools", ())
            return parsed, "tool", identity, offered
        if parsed.get("schema") == DECISION_SCHEMA:
            decision = decode_decision(raw, capabilities=frozenset(request.get("capabilities", ())))
            state = request.get("state") or {}
            identity = all(
                type(parsed[key]) is type(value) and parsed[key] == value
                for key, value in {
                    "request_id": request.get("request_id"), "event_id": request.get("event_id"),
                    "mission_id": state.get("mission_id"), "revision": state.get("revision"),
                }.items()
            )
            return parsed, "action", identity, decision.action in request.get("actions", ())
    except (TypeError, ValueError, RecursionError, AttributeError):
        pass
    return parsed if isinstance(parsed, dict) else None, None, False, False


def _prompt(sent):
    messages = sent.get("messages")
    if messages is None and isinstance(sent.get("body"), dict):
        messages = sent["body"].get("messages")
    return messages if isinstance(messages, list) else []


def _backend_error(result):
    error = result.get("backend_error")
    return result.get("error") if error is None else error


def _failure(rows, metrics, context, *, request_id, call_index, reasons, label,
             label_error, sent=None, result=None, receipt=None, base_request=None):
    prompt = _prompt(sent) if sent is not None else []
    messages = list(prompt)
    if label is not None and prompt:
        messages.append({"role": "assistant", "content": canonical_json(label)})
    row = {
        **context,
        "sample_id": f"{request_id}:{call_index}" if sent is not None else f"{request_id}:unsent",
        "request_id": request_id,
        "call_index": call_index,
        "sent": sent is not None,
        "messages": messages,
        "oracle_label": label,
        "label_status": "oracle_labeled" if label is not None else "unlabelable",
        "label_reason": label_error,
        "failure_reasons": reasons,
        "request": sent.get("request") if sent is not None else None,
        "model_output": result.get("model_output") if result is not None else None,
        "backend_error": _backend_error(result) if result is not None else None,
        "turn_latency_ms": result.get("latency_ms") if result is not None else None,
        "receipt": receipt,
    }
    if sent is None:
        row["base_request"] = base_request
    else:
        row.update({key: sent[key] for key in ("body", "body_json") if key in sent})
    rows.append(deepcopy(row))
    metrics["failure_rows"] += 1
    for reason in reasons:
        _count(metrics["failure_reason_counts"], reason)


def _score_turn(metrics, request, label, raw):
    parsed, kind, identity, offered = _decode(raw, request)
    observed = parsed or {}
    metrics["schema_valid"] += kind is not None
    metrics["identity_valid"] += identity
    metrics["retasks_proposed"] += observed.get("action") == "retask"
    expected_tool = label is not None and label["schema"] == TOOL_SCHEMA
    expected_action = label.get("action") if label is not None else None
    tool_match = kind == "tool" and identity and offered and expected_tool and observed["tool"] == label["tool"]
    action_match = (
        kind == "action" and identity and offered and observed["action"] == expected_action
        and (expected_action != "loiter" or observed["params"] == label["params"])
    )
    metrics["oracle_labeled_turns"] += label is not None
    metrics["oracle_unlabelable_turns"] += label is None
    metrics["expected_tool_turns"] += expected_tool
    metrics["expected_action_turns"] += expected_action is not None
    metrics["tool_matches"] += tool_match
    metrics["action_matches"] += action_match
    metrics["tool_use_correct"] += tool_match or (
        expected_action is not None and kind == "action" and identity and offered
    )
    metrics["retasks_required"] += expected_action == "retask"

    progress = request.get("mission_progress") or {}
    perception = request.get("perception") or {}
    if (expected_action == "continue" and request.get("trigger", "cadence") == "cadence"
            and isinstance(progress, dict) and progress.get("phase") == "cruise"
            and isinstance(perception, dict) and perception.get("call_index") == 0):
        metrics["quiet_cruise_turns"] += 1
        action = observed.get("action")
        metrics["quiet_unnecessary_actions"] += isinstance(action, str) and action != "continue"
        metrics["quiet_unnecessary_tools"] += observed.get("schema") == TOOL_SCHEMA
    reasons = []
    if kind is None:
        if raw is not None:
            reasons.append("invalid_schema")
    elif not identity:
        reasons.append("identity_mismatch")
    elif not offered:
        reasons.append("not_offered_or_budget_exhausted")
    if label is None:
        reasons.append("oracle_unlabelable")
    elif expected_tool and not tool_match:
        reasons.append("tool_mismatch")
    elif expected_action is not None and not action_match:
        reasons.append("action_mismatch")
    return reasons, kind, observed.get("action") if kind == "action" else None, action_match


def harvest(
    trace_rows: Iterable[dict], *, case_id, attempt, fault=None,
    expected_by_request: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[list[dict], dict]:
    """Return failure SFT rows and additive metrics for one audit stream.

    Match/action/schema counts describe the observed model reply even if its
    receipt failed or it arrived late. Installation and deadline timing use
    the receipt. Quiet cruise means first-turn cadence cruise with an oracle
    continue. A missing receipt gives unknown deadline timing.
    The optional case expectations flag admitted receipt actions outside the
    supplied tuple for a request. The policy still supplies the SFT label.
    """
    rows, metrics = [], _metrics()
    metrics["attempts"] = 1
    case_expectations = expected_by_request if expected_by_request is not None else {}
    context = dict(case_id=case_id, attempt=attempt, fault=fault, episode_id=f"{case_id}:{attempt}",
                   category=case_id, split="harvest", source="closed_loop")
    tickets = defaultdict(lambda: {"turn_sent": {}, "turn_result": {}})
    for position, row in enumerate(trace_rows):
        if not isinstance(row, dict):
            metrics["invalid_trace_rows"] += 1
            continue
        kind = row.get("kind")
        if kind not in ("request_opened", "turn_sent", "turn_result", "receipt"):
            continue
        request_id = row.get("request_id")
        index = row.get("call_index")
        is_turn = kind in ("turn_sent", "turn_result")
        if not isinstance(request_id, str) or not request_id or (is_turn and (type(index) is not int or index < 0)):
            metrics["invalid_trace_rows"] += 1
            continue
        ticket = tickets[request_id]
        destination, key = (ticket[kind], index) if is_turn else (ticket, kind)
        if key in destination:
            metrics["duplicate_trace_rows"] += 1
        else:
            destination[key] = position, row

    for request_id, ticket in tickets.items():
        _, opened = ticket.get("request_opened", (None, None))
        receipt_position, receipt = ticket.get("receipt", (None, None))
        sent_turns, results = ticket["turn_sent"], ticket["turn_result"]
        base = opened.get("base_request", opened.get("request")) if opened is not None else None
        metrics["requests"] += 1
        metrics["requests_opened"] += opened is not None
        metrics["receipts"] += receipt is not None
        metrics["missing_receipts"] += receipt is None
        metrics["orphan_results"] += len(results.keys() - sent_turns.keys())
        metrics["turns_sent"] += len(sent_turns)
        request = base if isinstance(base, dict) else (
            next(iter(sent_turns.values()))[1].get("request") if sent_turns else None
        )
        deadline = _deadline_outcome(request, receipt or {})
        metrics[f"deadline_{deadline}"] += 1
        disrupted = _disrupted(receipt or {})
        metrics["disrupted_requests"] += disrupted
        installed = (receipt or {}).get("status") == "installed" and receipt.get("action") == "retask"
        metrics["retasks_installed"] += installed
        if receipt is not None:
            status = receipt.get("status")
            _count(metrics["receipt_status_counts"], status if isinstance(status, str) else "unknown")
            _record_latency(metrics["receipt_latency_ms_histogram"], receipt.get("latency_ms"))
        terminal_reasons = []
        if receipt is None:
            terminal_reasons.append("missing_receipt")
        elif receipt.get("status") not in _ADMITTED_STATUSES:
            terminal_reasons.append("receipt_not_admitted")
        else:
            expected_actions = case_expectations.get(request_id)
            if expected_actions is not None and receipt.get("action") not in expected_actions:
                terminal_reasons.append("case_action_mismatch")
        if disrupted:
            terminal_reasons.append("request_disrupted")
        if deadline == "missed":
            terminal_reasons.append("deadline_missed")
        if not sent_turns and opened is not None:
            metrics["requests_unsent"] += 1
            label, label_error = _oracle(base)
            reasons = ["request_unsent", *terminal_reasons]
            if label is None:
                reasons.append("oracle_unlabelable")
            _failure(rows, metrics, context, request_id=request_id, call_index=None,
                     reasons=reasons, label=label, label_error=label_error, receipt=receipt, base_request=base)
            continue

        last_index = max(sent_turns, default=-1)
        for index, (_, sent) in sorted(sent_turns.items()):
            result_position, result = results.get(index, (None, None))
            sent_request = sent.get("request")
            label, label_error = _oracle(sent_request)
            raw = (result or {}).get("model_output")
            reasons, reply_kind, action, action_match = _score_turn(
                metrics, sent_request if isinstance(sent_request, dict) else {}, label, raw,
            )
            metrics["turn_results"] += result is not None
            metrics["missing_results"] += result is None
            if result is None:
                reasons.insert(0, "missing_turn_result")
            else:
                _record_latency(metrics["turn_latency_ms_histogram"], result.get("latency_ms"))
                if _backend_error(result) is not None:
                    metrics["backend_errors"] += 1
                    reasons.insert(0, "backend_error")
                elif raw is None:
                    reasons.insert(0, "missing_model_output")
                # Receipts can close a ticket while a backend worker is draining.
                if receipt_position is not None and result_position > receipt_position:
                    metrics["late_results"] += 1
                    reasons.append("result_after_receipt")
            if not _prompt(sent):
                reasons.append("missing_sent_messages")
            if reply_kind == "tool" and index + 1 not in sent_turns:
                reasons.append("missing_followup_turn")
            if index == last_index:
                metrics["required_retasks_installed"] += installed and action_match and action == "retask"
                if action == "retask" and not installed:
                    reasons.append("retask_not_installed")
                if (action is not None and receipt is not None
                        and receipt.get("status") in _ADMITTED_STATUSES and receipt.get("action") != action):
                    reasons.append("receipt_action_mismatch")
                reasons.extend(terminal_reasons)
            if reasons:
                _failure(rows, metrics, context, request_id=request_id, call_index=index,
                         reasons=reasons, label=label, label_error=label_error, sent=sent,
                         result=result, receipt=receipt)
    return rows, metrics
