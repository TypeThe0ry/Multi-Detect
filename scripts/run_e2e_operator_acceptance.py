#!/usr/bin/env python3
"""Run and score the signed operator-link SIM acceptance flow.

Two modes:

* ``--evaluate RUN_DIR`` scores an existing run directory (no SITL).
* ``PACKAGE`` (positional) takes the shared SITL lock, starts
  ``run_sim_session.py --operator-link`` and ``fake_gcs_console.py``, waits for
  the session to end and then scores the run it just produced.

A run directory holds ``gcs-documents.jsonl`` (every complete document the
console received, in arrival order), ``gcs.out`` (console stdout: the same
documents plus ``onboard ack`` lines), ``session.out``/``session.jsonl`` and
``onboard/collector/...`` with the onboard ``mission_log/mission.jsonl`` and
the episode (``episode.json``, ``steps.jsonl``).

Ground arrival time is not stamped by the console.  The downlink is one
stop-and-wait queue and a status is built (``t`` = onboard clock) only when
nothing else is in flight, so a finding's ground arrival lies after the status
received just before it and no later than the ``t`` of the first status
received after it.  Latencies are reported as that bracket, on the onboard
clock.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(r"D:/Projects/mdwt/_scratch")
LOCK = SCRATCH / "sitl.lock"
DEFAULT_RADIUS_M = 10.0  # scripts/score_onboard_loop.py --radius-m default
sys.path.insert(0, str(ROOT))

ACK_RE = re.compile(r"^onboard ack kind=(\d+) index=(\d+) accepted=(True|False)\s*(.*)$")
MESSAGE_NAMES = {
    28: "mission_upload",
    34: "mission_heartbeat",
    37: "ground_command",
}
# Documents whose arrival at the ground is part of acceptance.
KEY_KINDS = ("mission_started", "plan", "plan_approved", "finding", "escalation", "final_summary")


class SitlLock:
    """Atomic mkdir lock compatible with ``sitl_lock.sh`` owner format."""

    def __init__(self, name: str, path: Path = LOCK, wait_s: float = 60.0) -> None:
        self.name, self.path, self.wait_s = name, Path(path), wait_s
        self.acquired = False

    def acquire(self, timeout_s: float = 0.0, wait_s: float | None = None) -> None:
        deadline = time.monotonic() + timeout_s if timeout_s else None
        interval = self.wait_s if wait_s is None else wait_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.path.mkdir()
            except FileExistsError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"SITL lock busy: {self.path}") from None
                if interval <= 0:
                    if deadline is None:
                        raise TimeoutError(f"SITL lock busy: {self.path}") from None
                    continue
                remaining = (deadline - time.monotonic()) if deadline else interval
                time.sleep(min(interval, max(0.1, remaining)))
                continue
            owner = f"{self.name} {os.getpid()} {datetime.now().astimezone().isoformat()}\n"
            (self.path / "owner").write_text(owner, encoding="utf-8")
            self.acquired = True
            return

    def release(self) -> None:
        if self.acquired:
            try:
                (self.path / "owner").unlink(missing_ok=True)
                self.path.rmdir()
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


# --------------------------------------------------------------------------- io


def _json_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _ground_documents(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Documents in ground arrival order: the console log, else console stdout."""
    docs = _json_lines(_read_lines(run_dir / "gcs-documents.jsonl"))
    if docs:
        return docs, "gcs-documents.jsonl"
    docs = _json_lines(_read_lines(run_dir / "gcs.out"))
    return docs, "gcs.out" if docs else "missing"


def _onboard_mission_log(run_dir: Path) -> tuple[list[dict[str, Any]], Path | None]:
    paths = sorted((run_dir / "onboard").rglob("mission_log/mission.jsonl"))
    if not paths:
        return [], None
    path = paths[-1]
    return _json_lines(_read_lines(path)), path


def _episode_dir(run_dir: Path) -> Path | None:
    episodes = sorted(
        p for p in (run_dir / "onboard" / "collector" / "episodes").glob("*") if p.is_dir()
    )
    return episodes[-1] if episodes else None


# ------------------------------------------------------------------ components


def _console_acks(lines: list[str]) -> dict[str, Any]:
    """Rejected onboard ACKs from console stdout, split at the first downlinked document.

    Rejections before the first document arrive while the aircraft has no
    admitted mission (e.g. heartbeats racing the upload) and are expected; a
    rejection after it is a link fault worth reading.
    """
    phase = "before_first_document"
    groups: dict[tuple[int, str, str], int] = {}
    accepted_command_acks = rejected_packets = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("{"):
            phase = "after_first_document"
            continue
        if stripped.startswith("rejected packet"):
            rejected_packets += 1
            continue
        match = ACK_RE.match(stripped)
        if not match:
            continue
        kind, accepted, detail = (
            int(match.group(1)),
            match.group(3) == "True",
            match.group(4).strip(),
        )
        if accepted:
            accepted_command_acks += kind == 37
            continue
        key = (kind, detail, phase)
        groups[key] = groups.get(key, 0) + 1
    rejected = [
        {
            "kind": kind,
            "message": MESSAGE_NAMES.get(kind, f"type_{kind}"),
            "detail": detail,
            "phase": phase,
            "count": count,
        }
        for (kind, detail, phase), count in groups.items()
    ]
    return {
        "rejected_acks": rejected,
        "rejected_ack_total": sum(item["count"] for item in rejected),
        "rejected_after_first_document": sum(
            item["count"] for item in rejected if item["phase"] == "after_first_document"
        ),
        "rejected_packets": rejected_packets,
        "ground_command_acks_accepted": accepted_command_acks,
    }


def _arrival_brackets(docs: list[dict[str, Any]]) -> list[tuple[float | None, float | None]]:
    """Per document, the onboard-clock window in which the ground received it."""
    previous: float | None = None
    lower: list[float | None] = []
    for doc in docs:
        lower.append(previous)
        if doc.get("kind") == "status" and _num(doc.get("t")) is not None:
            previous = _num(doc.get("t"))
    following: float | None = None
    upper: list[float | None] = [None] * len(docs)
    for index in range(len(docs) - 1, -1, -1):
        upper[index] = following
        doc = docs[index]
        if doc.get("kind") == "status" and _num(doc.get("t")) is not None:
            following = _num(doc.get("t"))
    return list(zip(lower, upper, strict=True))


def _final_summary(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Ground view of the final summary: whole document, or every page."""
    whole = next(
        (
            d
            for d in reversed(docs)
            if d.get("kind") == "final_summary" and isinstance(d.get("summary"), dict)
        ),
        None,
    )
    pages: dict[int, str] = {}
    totals: set[int] = set()
    page_docs = 0
    for doc in docs:
        if doc.get("kind") != "final_summary_page":
            continue
        page_docs += 1
        try:
            page, total = int(doc["page"]), int(doc["pages"])
        except (KeyError, TypeError, ValueError):
            continue
        totals.add(total)
        pages.setdefault(page, str(doc.get("body") or ""))
    pages_total = max(totals) if totals else None
    summary, source = None, None
    if whole is not None:
        summary = whole["summary"]
        source = (
            "final_summary_assembled_by_console"
            if whole.get("assembled_pages")
            else "final_summary"
        )
    elif pages_total and len(pages) == pages_total and set(pages) == set(range(1, pages_total + 1)):
        try:
            assembled = json.loads("".join(pages[i] for i in sorted(pages)))
        except json.JSONDecodeError:
            assembled = None
        if isinstance(assembled, dict):
            summary, source = assembled, "final_summary_pages_assembled_here"
    return {
        "complete": summary is not None,
        "source": source,
        "pages_total": pages_total,
        "pages_received": sorted(pages),
        "page_documents": page_docs,
        "duplicate_page_documents": max(0, page_docs - len(pages)),
        "summary": summary,
    }


def _plan(
    ground: list[dict[str, Any]], onboard: list[dict[str, Any]], acks: dict[str, Any]
) -> dict[str, Any]:
    def first(rows, kind):
        return next((r for r in rows if r.get("kind") == kind), None)

    plan_g, approved_g = first(ground, "plan"), first(ground, "plan_approved")
    plan = plan_g or first(onboard, "plan")
    approved = approved_g or first(onboard, "plan_approved")
    approve_event = next(
        (
            r
            for r in onboard + ground
            if r.get("kind") == "ground_event" and r.get("event_type") == "approve_plan"
        ),
        None,
    )
    sha = (plan or {}).get("plan_sha256")
    sha_match = bool(plan and approved and sha and approved.get("plan_sha256") == sha)
    latency = None
    if plan and approved:
        a, b = _num(plan.get("timestamp")), _num(approved.get("timestamp"))
        latency = None if a is None or b is None else b - a
    return {
        "received_at_ground": plan_g is not None,
        "plan_sha256": sha,
        "skills": [s.get("skill") for s in (plan or {}).get("steps") or [] if isinstance(s, dict)],
        "requires_approval": (plan or {}).get("requires_approval"),
        # plan_approved is emitted only after the signed type-37 ground command
        # passed the supervisor (mission id, signature, time window, sequence).
        "signed_command_accepted": approve_event is not None
        or acks["ground_command_acks_accepted"] > 0,
        "approved": sha_match,
        "approval_seen_at_ground": approved_g is not None,
        "approval_latency_s": _round(latency),
        "approval_latency_clock": "onboard plan record -> onboard plan_approved record",
    }


def _clock_alignment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    samples = []
    for doc in docs:
        if doc.get("kind") != "status" or _num(doc.get("t")) is None:
            continue
        received = _num(doc.get("ground_received_monotonic_s"))
        if received is None:
            received = _num(doc.get("received_at_monotonic"))
        if received is not None:
            samples.append(received - float(doc["t"]))
    if not samples:
        return {"method": "status_bracket", "offset_s": None, "sample_count": 0}
    return {
        "method": "status_median_offset",
        "offset_s": _round(sorted(samples)[len(samples) // 2]),
        "sample_count": len(samples),
    }


def _findings(
    ground: list[dict[str, Any]],
    brackets: list[tuple[float | None, float | None]],
    onboard: list[dict[str, Any]],
    alignment: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    out = []
    ground_by_seq: dict[Any, int] = {}
    for index, doc in enumerate(ground):
        if doc.get("kind") == "finding":
            ground_by_seq.setdefault(doc.get("sequence"), index)
    onboard_findings = [r for r in onboard if r.get("kind") == "finding"]
    if not onboard_findings:
        onboard_findings = [ground[i] for i in ground_by_seq.values()]
    for row in onboard_findings:
        index = ground_by_seq.get(row.get("sequence"))
        recorded = _num(row.get("timestamp"))
        item = {
            "id": row.get("id"),
            "sequence": row.get("sequence"),
            "label": row.get("label"),
            "track": row.get("track"),
            "n": _num(row.get("n")),
            "e": _num(row.get("e")),
            "recorded_t": recorded,
            "recorded_at": row.get("recorded_at"),
            "arrived_at_ground": index is not None,
            "latency_lower_s": None,
            "latency_upper_s": None,
            "latency_s": None,
            "latency_method": "status_bracket",
        }
        if index is not None and recorded is not None:
            received = _num(ground[index].get("ground_received_monotonic_s"))
            if received is None:
                received = _num(ground[index].get("received_at_monotonic"))
            if alignment and alignment.get("offset_s") is not None and received is not None:
                item["latency_s"] = _round(max(0.0, received - alignment["offset_s"] - recorded))
                item["latency_method"] = "ground_clock_aligned"
                item["latency_source"] = "status_median_monotonic_offset"
            lower, upper = brackets[index]
            item["latency_lower_s"] = _round(max(0.0, (lower or recorded) - recorded))
            item["latency_upper_s"] = _round(None if upper is None else upper - recorded)
        out.append(item)
    return out


def _truth(
    run_dir: Path, truth: Path | dict[str, Any] | None
) -> tuple[str | None, dict[str, Any] | None]:
    if isinstance(truth, dict):
        return None, truth
    if truth is not None:
        try:
            return Path(truth).name.split(".")[0], json.loads(
                Path(truth).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None, None
    episode = _episode_dir(run_dir)
    if episode is None:
        return None, None
    from scripts.score_onboard_loop import truth_for_episode

    return truth_for_episode(episode)


def _count(
    summary: dict[str, Any] | None,
    ground: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    truth: dict[str, Any] | None,
    radius_m: float,
) -> dict[str, Any]:
    reported, lower_bound, source, step_reasons = None, None, None, {}
    if summary is not None:
        counts = summary.get("counts") or {}
        if counts:
            reported = sum(int(v) for v in counts.values())
        lower_bound = summary.get("count_is_lower_bound")
        source = "ground_final_summary"
        for item in summary.get("history") or []:
            if isinstance(item, dict) and item.get("skill") == "count":
                step_reasons[str(item.get("step"))] = item.get("reason")
    if reported is None:
        status = next(
            (d for d in reversed(ground) if d.get("kind") == "status" and d.get("counts")), None
        )
        if status is not None:
            counts = status["counts"]
            reported = sum(int((v or {}).get("n", 0)) for v in counts.values())
            lower_bound = any(bool((v or {}).get("lower_bound", True)) for v in counts.values())
            source = "ground_status"
    arrived = [
        f for f in findings if f["arrived_at_ground"] and f["n"] is not None and f["e"] is not None
    ]
    if reported is None and arrived:
        reported, source = len(arrived), "ground_findings"
    result: dict[str, Any] = {
        "reported": reported,
        "source": source,
        "is_lower_bound": lower_bound,
        "step_reasons": step_reasons,
        "radius_m": radius_m,
        "expected": None,
        "matched": None,
        "false_findings": None,
        "position_errors_m": [],
        "count_exact": None,
    }
    members = [t for t in (truth or {}).get("targets", []) if t.get("count_member")]
    if not members:
        return result
    from scripts.score_onboard_loop import _count_metrics

    # Same one-to-one nearest matching as score_onboard_loop, fed with the
    # ground-received finding positions (full precision) and reported count.
    mission = {
        "counts": {"reported": reported} if reported is not None else {},
        "positions": [f"{f['n']},{f['e']}" for f in arrived],
    }
    metrics = _count_metrics([{"labels": {"onboard": {"mission": mission}}}], members, radius_m)
    result.update(
        expected=metrics["expected_count"],
        matched=metrics["matched_count"],
        false_findings=metrics["false_findings"],
        position_errors_m=metrics["count_position_errors_m"],
        count_exact=metrics["count_exact"],
    )
    return result


def _termination(
    summary: dict[str, Any] | None, ground: list[dict[str, Any]], onboard: list[dict[str, Any]]
) -> dict[str, Any]:
    if summary is not None:
        status = summary.get("status")
        reason = summary.get("terminal_reason") or (
            summary.get("reason") if status != "completed" else None
        )
        return {
            "status": status,
            "action": summary.get("terminal_action") or summary.get("action"),
            "reason": reason,
            "source": "ground_final_summary",
        }
    escalation = next((d for d in reversed(ground) if d.get("kind") == "escalation"), None)
    if escalation is not None:
        return {
            "status": "ended_early",
            "action": escalation.get("action"),
            "reason": escalation.get("reason"),
            "source": "ground_escalation",
        }
    onboard_summary = next(
        (r.get("summary") for r in reversed(onboard) if r.get("kind") == "final_summary"), None
    )
    if isinstance(onboard_summary, dict):
        return {
            "status": onboard_summary.get("status"),
            "action": onboard_summary.get("terminal_action") or onboard_summary.get("action"),
            "reason": onboard_summary.get("terminal_reason"),
            "source": "onboard_final_summary",
        }
    return {"status": "unknown", "action": None, "reason": None, "source": None}


def _return_home(
    summary: dict[str, Any] | None, onboard: list[dict[str, Any]], episode: Path | None
) -> dict[str, Any]:
    arrived, reason = None, None
    for item in (summary or {}).get("history") or []:
        if isinstance(item, dict) and item.get("skill") == "return_home":
            reason = item.get("reason")
            arrived = item.get("state") == "succeeded" and reason == "arrived"
    home = [0.0, 0.0]
    started = next((r for r in onboard if r.get("kind") == "mission_started"), None)
    package_home = ((started or {}).get("package") or {}).get("home")
    if (
        isinstance(package_home, list)
        and len(package_home) >= 2
        and all(_num(v) is not None for v in package_home[:2])
    ):
        home = [float(package_home[0]), float(package_home[1])]
    distance = landed = None
    if episode is not None:
        lines = _read_lines(episode / "steps.jsonl")
        last = _json_lines(lines[-1:]) if lines else []
        obs = (last[0].get("obs_after") or last[0].get("obs_before") or {}) if last else {}
        ned = (obs.get("pose") or {}).get("local_ned_m")
        if isinstance(ned, list) and len(ned) >= 2:
            distance = math.hypot(float(ned[0]) - home[0], float(ned[1]) - home[1])
        landed = obs.get("landed") if isinstance(obs.get("landed"), bool) else None
    return {
        "arrived_reported": arrived,
        "reason": reason,
        "source": "ground_final_summary" if reason is not None else None,
        "final_distance_home_m": _round(distance, 2),
        "final_landed": landed,
    }


def _task_validation(
    skills: list[str],
    summary: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    truth: dict[str, Any] | None,
    radius_m: float,
) -> tuple[str, dict[str, Any], dict[str, bool]]:
    """Select task-specific truth checks while keeping the count contract stable."""
    skill_set = set(skills)
    targets = (truth or {}).get("targets", [])
    if "count" in skill_set:
        return "count", {}, {"count_matches_truth": None}
    if skill_set & {"monitor", "patrol"}:
        expected = [
            t
            for t in targets
            if isinstance(t, dict)
            and (
                any(t.get(k) for k in ("alert_member", "monitor_member", "patrol_member"))
                or t.get("count_member") is True
                or t.get("true_target") is True
            )
        ]
        observed = list((summary or {}).get("alerts") or [])
        expected_points = []
        for target in expected:
            pose = target.get("pose") or {}
            north = _num(pose.get("y_north_m", pose.get("north_m")))
            east = _num(pose.get("x_east_m", pose.get("east_m")))
            if north is not None and east is not None:
                expected_points.append((north, east))
        observed_points = []
        for alert in observed:
            match = re.search(r"@\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", str(alert))
            if match:
                observed_points.append((float(match.group(1)), float(match.group(2))))
        matched = 0
        remaining = list(expected_points)
        for point in observed_points:
            candidates = [
                (math.dist(point, target), index) for index, target in enumerate(remaining)
            ]
            if candidates:
                distance, index = min(candidates)
                if distance <= radius_m:
                    matched += 1
                    remaining.pop(index)
        # Marker-only unit fixtures can validate precision/recall without positions.
        if expected and not expected_points and observed:
            expected_labels = [
                str(t.get("label") or t.get("class") or t.get("query") or "").casefold()
                for t in expected
            ]
            observed_labels = [
                str(a).split(":alert:", 1)[-1].split("@", 1)[0].casefold() for a in observed
            ]
            matched = sum(1 for label in observed_labels if label and label in expected_labels)
            observed_count = len(observed_labels)
            expected_count = len(expected_labels)
        else:
            observed_count, expected_count = len(observed_points), len(expected_points)
        precision = matched / observed_count if observed_count else (1.0 if not expected else 0.0)
        recall = (
            matched / expected_count if expected_count else (1.0 if not observed_count else 0.0)
        )
        metrics = {
            "expected_alerts": expected_count,
            "observed_alerts": observed_count,
            "matched_alerts": matched,
            "precision": precision,
            "recall": recall,
            "matching_radius_m": radius_m,
        }
        valid = bool(expected_count)
        return (
            "alerts",
            metrics,
            {
                "alert_precision": valid and precision == 1.0,
                "alert_recall": valid and recall == 1.0,
                "alerts_match_truth": valid and precision == 1.0 and recall == 1.0,
            },
        )
    if "inspect" in skill_set:
        expected = [
            t
            for t in targets
            if isinstance(t, dict)
            and (
                any(t.get(k) for k in ("inspect_member", "inspection_member"))
                or t.get("true_target") is True
            )
        ]
        expected_labels = [
            str(t.get("label") or t.get("class") or t.get("query") or "").casefold()
            for t in expected
        ]
        candidates = [f for f in findings if f.get("arrived_at_ground")]
        matched = 0
        errors = []
        for target, label in zip(expected, expected_labels, strict=False):
            pose = target.get("pose") or {}
            north = _num(pose.get("y_north_m", pose.get("north_m")))
            east = _num(pose.get("x_east_m", pose.get("east_m")))
            choices = [
                f for f in candidates if label and label in str(f.get("label") or "").casefold()
            ]
            if north is not None and east is not None:
                choices = [f for f in choices if f.get("n") is not None and f.get("e") is not None]
                choices.sort(key=lambda f: math.hypot(f["n"] - north, f["e"] - east))
                if choices:
                    distance = math.hypot(choices[0]["n"] - north, choices[0]["e"] - east)
                    if distance <= radius_m:
                        matched += 1
                        errors.append(_round(distance))
            elif choices:
                matched += 1
        recall = matched / len(expected) if expected else 0.0
        metrics = {
            "expected_inspections": len(expected),
            "matched_inspections": matched,
            "recall": recall,
            "position_errors_m": errors,
            "radius_m": radius_m,
        }
        return (
            "inspection",
            metrics,
            {"inspection_matches_truth": bool(expected) and matched == len(expected)},
        )
    return "generic", {}, {"count_matches_truth": None}


def _link(
    ground: list[dict[str, Any]],
    onboard: list[dict[str, Any]],
    acks: dict[str, Any],
    summary_view: dict[str, Any],
) -> dict[str, Any]:
    gaps = []
    seen = set()
    for row in onboard + ground:
        if row.get("kind") != "vehicle_telemetry_gap":
            continue
        key = (row.get("vehicle_start_s"), row.get("vehicle_end_s"))
        if key in seen:
            continue
        seen.add(key)
        gaps.append(
            {
                "vehicle_start_s": row.get("vehicle_start_s"),
                "vehicle_end_s": row.get("vehicle_end_s"),
                "gap_s": _round(_num(row.get("gap_s"))),
            }
        )
    statuses = [d for d in ground if d.get("kind") == "status"]
    supervisors = [d.get("supervisor") or {} for d in statuses]
    silent = [_num(s.get("link_silent_s")) for s in supervisors]
    silent = [s for s in silent if s is not None]
    ground_keys = {(d.get("kind"), d.get("sequence")) for d in ground}
    missing = [
        {"kind": r.get("kind"), "sequence": r.get("sequence")}
        for r in onboard
        if r.get("kind") in KEY_KINDS and (r.get("kind"), r.get("sequence")) not in ground_keys
    ]
    if summary_view["complete"]:
        # A paged summary carries no sequence; once assembled it has arrived.
        missing = [m for m in missing if m["kind"] != "final_summary"]
    return {
        **acks,
        "vehicle_telemetry_gaps": gaps,
        "status_documents": len(statuses),
        "status_link_down": sum(s.get("link") == "down" for s in supervisors),
        "max_link_silent_s": _round(max(silent)) if silent else None,
        "onboard_records_missing_at_ground": missing,
        "duplicate_final_summary_pages": summary_view["duplicate_page_documents"],
    }


# ------------------------------------------------------------------ evaluation


def evaluate_run(
    run_dir: Path, *, truth: Path | dict[str, Any] | None = None, radius_m: float = DEFAULT_RADIUS_M
) -> dict[str, Any]:
    """Score an existing run directory. Missing evidence fails its criterion."""
    run_dir = Path(run_dir)
    ground, ground_source = _ground_documents(run_dir)
    onboard, mission_log = _onboard_mission_log(run_dir)
    acks = _console_acks(_read_lines(run_dir / "gcs.out"))
    episode = _episode_dir(run_dir)
    world_id, truth_data = _truth(run_dir, truth)

    summary_view = _final_summary(ground)
    summary = summary_view.pop("summary")
    plan = _plan(ground, onboard, acks)
    timing_alignment = _clock_alignment(ground)
    findings = _findings(ground, _arrival_brackets(ground), onboard, timing_alignment)
    count = _count(summary, ground, findings, truth_data, radius_m)
    termination = _termination(summary, ground, onboard)
    return_home = _return_home(summary, onboard, episode)
    link = _link(ground, onboard, acks, summary_view)
    onboard_summary = next((r for r in reversed(onboard) if r.get("kind") == "final_summary"), None)
    # Cross-check: what the aircraft itself recorded as the outcome.
    termination["onboard_status"] = ((onboard_summary or {}).get("summary") or {}).get("status")
    summary_view["recorded_onboard"] = onboard_summary is not None
    summary_view["status"] = (summary or {}).get("status")

    task_kind, task_metrics, task_criteria = _task_validation(
        plan["skills"], summary, findings, truth_data, radius_m
    )
    criteria = {
        "plan_signed_and_approved": plan["received_at_ground"]
        and plan["signed_command_accepted"]
        and plan["approved"],
        "all_findings_at_ground": bool(findings) and all(f["arrived_at_ground"] for f in findings),
        "final_summary_complete": summary_view["complete"],
        "mission_completed": termination["status"] == "completed",
        "returned_home": return_home["arrived_reported"] is True,
        "no_rejected_acks_after_admission": link["rejected_after_first_document"] == 0,
    }
    criteria.update(
        {
            key: count["count_exact"] is True
            if value is None and key == "count_matches_truth"
            else value
            for key, value in task_criteria.items()
        }
    )
    verdict = "PASS" if all(criteria.values()) else "FAIL"
    mission_id = next((d.get("mission_id") for d in ground + onboard if d.get("mission_id")), None)
    return {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mission_id": mission_id,
        "world_id": world_id,
        "evidence": {
            "ground_documents": ground_source,
            "ground_document_count": len(ground),
            "onboard_mission_log": str(mission_log) if mission_log else None,
            "onboard_record_count": len(onboard),
            "episode": str(episode) if episode else None,
        },
        "plan": plan,
        "findings": findings,
        "final_summary": summary_view,
        "termination": termination,
        "count": count,
        "task_validation": {"task_kind": task_kind, **task_metrics},
        "return_home": return_home,
        "link": link,
        "timing_alignment": timing_alignment,
        "criteria": criteria,
        "verdict": verdict,
    }


# Kept as a small public alias for fixture/unit-test callers.
evaluate_report = evaluate_run


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    plan, count, term = report["plan"], report["count"], report["termination"]
    summary, link, ret = report["final_summary"], report["link"], report["return_home"]
    skills = ", ".join(map(str, plan["skills"])) or "—"
    pages = f"{summary['pages_received']}/{_fmt(summary['pages_total'])}"
    lines = [
        f"# E2E 操作员验收：**{report['verdict']}**",
        "",
        f"- 运行目录：`{report['run_dir']}`",
        f"- 任务：`{_fmt(report['mission_id'])}`，世界：`{_fmt(report['world_id'])}`",
        "",
        "| 判据 | 结果 |",
        "|---|---|",
    ]
    lines += [f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in report["criteria"].items()]
    lines += [
        "",
        "## 计划与审批",
        f"- 地面收到计划：{_fmt(plan['received_at_ground'])}；技能：{skills}",
        f"- 签名命令被机载接受：{_fmt(plan['signed_command_accepted'])}；"
        f"审批与计划哈希一致：{_fmt(plan['approved'])}",
        f"- 审批耗时：{_fmt(plan['approval_latency_s'])} s（{plan['approval_latency_clock']}）",
        "",
        "## Finding 下行延迟（机载时钟）",
        "",
        "| id | 标签 | N | E | 到达地面 | 延迟下界 s | 延迟上界 s |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in report["findings"]:
        n = "—" if f["n"] is None else f"{f['n']:.1f}"
        e = "—" if f["e"] is None else f"{f['e']:.1f}"
        lines.append(
            f"| {_fmt(f['id'])} | {_fmt(f['label'])} | {n} | {e} | {_fmt(f['arrived_at_ground'])} "
            f"| {_fmt(f['latency_lower_s'])} | {_fmt(f['latency_upper_s'])} |"
        )
    lines += [
        "",
        "## 最终摘要与终止",
        f"- 摘要完整：{_fmt(summary['complete'])}（来源 {_fmt(summary['source'])}；页 {pages}，"
        f"重复页 {summary['duplicate_page_documents']}；"
        f"机载已记录：{_fmt(summary['recorded_onboard'])}）",
        f"- 终止：{_fmt(term['status'])}；动作 {_fmt(term['action'])}；原因 {_fmt(term['reason'])}"
        f"（来源 {_fmt(term['source'])}；机载记录 {_fmt(term['onboard_status'])}）",
        f"- 返航到达（摘要报告）：{_fmt(ret['arrived_reported'])}；"
        f"末帧离家 {_fmt(ret['final_distance_home_m'])} m",
        "",
        "## 计数",
        f"- 报告 {_fmt(count['reported'])}（来源 {_fmt(count['source'])}，"
        f"下限：{_fmt(count['is_lower_bound'])}，步骤原因 {count['step_reasons'] or '—'}）",
        f"- 真值 {_fmt(count['expected'])}，{count['radius_m']} m 内匹配 {_fmt(count['matched'])}，"
        f"误报 {_fmt(count['false_findings'])}，位置误差 {count['position_errors_m']}",
        "",
        "## 链路",
        f"- 拒绝 ACK：共 {link['rejected_ack_total']}，"
        f"首个下行文档之后 {link['rejected_after_first_document']}；"
        f"无法解码的包 {link['rejected_packets']}",
    ]
    for item in link["rejected_acks"]:
        lines.append(
            f"  - {item['message']}（type {item['kind']}）×{item['count']} "
            f"[{item['phase']}]：{item['detail']}"
        )
    gaps = link["vehicle_telemetry_gaps"]
    lines += [
        f"- vehicle_telemetry_gap：{len(gaps)} 段 {gaps or ''}".rstrip(),
        f"- status 文档 {link['status_documents']}，link=down {link['status_link_down']}，"
        f"最大 link_silent_s {_fmt(link['max_link_silent_s'])}",
        f"- 机载记录但地面未收到：{link['onboard_records_missing_at_ground'] or '无'}",
    ]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], output: Path) -> tuple[Path, Path]:
    output.parent.mkdir(parents=True, exist_ok=True)
    js, md = output.with_suffix(".json"), output.with_suffix(".md")
    js.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md.write_text(render_markdown(report), encoding="utf-8")
    return js, md


# ------------------------------------------------------------------------ run


def run(args: argparse.Namespace) -> int:
    run_dir = args.output.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "onboard"
    out.mkdir(parents=True, exist_ok=True)
    session_cmd = [
        sys.executable,
        str(ROOT / "scripts/run_sim_session.py"),
        "--world",
        args.world,
        "--airframe",
        args.airframe,
        "--onboard-task",
        args.task,
        "--operator-link",
        "--exit-after-task",
        "--onboard-out",
        str(out),
        "--session-log",
        str(run_dir / "session.jsonl"),
        "--gcs-port",
        str(args.gcs_port),
    ]
    procs: list[subprocess.Popen] = []
    handles = []
    lock = SitlLock(args.lock_name)
    try:
        lock.acquire(args.lock_timeout)
        handles.append((run_dir / "session.out").open("w", encoding="utf-8"))
        session = subprocess.Popen(
            session_cmd, cwd=ROOT, stdout=handles[-1], stderr=subprocess.STDOUT
        )
        procs.append(session)
        link = out / "operator-link.json"
        deadline = time.monotonic() + args.link_timeout
        while not link.is_file() and session.poll() is None and time.monotonic() < deadline:
            time.sleep(0.25)
        if not link.is_file():
            raise RuntimeError("operator-link.json was not produced")
        gcs_cmd = [
            sys.executable,
            str(ROOT / "scripts/fake_gcs_console.py"),
            str(args.package),
            "--link",
            str(link),
            "--auto-approve",
            "--max-seconds",
            str(args.gcs_timeout),
            "--log",
            str(run_dir / "gcs-documents.jsonl"),
        ]
        handles.append((run_dir / "gcs.out").open("w", encoding="utf-8"))
        gcs = subprocess.Popen(gcs_cmd, cwd=ROOT, stdout=handles[-1], stderr=subprocess.STDOUT)
        procs.append(gcs)
        session.wait()
        gcs.terminate()
        gcs.wait(timeout=10)
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        for handle in handles:
            handle.close()
        # run_sim_session normally removes its container on a normal exit. On
        # SIGINT/termination it can be interrupted inside subprocess.run, so
        # remove only the uniquely named container printed by our session.
        try:
            text = (run_dir / "session.out").read_text(encoding="utf-8", errors="replace")
            for name in set(re.findall(r"multidetect-sim-session-[0-9a-f]{8}", text)):
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    cwd=ROOT,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError:
            pass
        lock.release()
    return _score(run_dir, args)


def _score(run_dir: Path, args: argparse.Namespace) -> int:
    report = evaluate_run(run_dir, truth=args.truth, radius_m=args.radius_m)
    js, md = write_report(report, args.report or run_dir / "operator-acceptance")
    print(json.dumps({k: report[k] for k in ("verdict", "criteria")}, ensure_ascii=False))
    print(f"report: {js}\nreport: {md}")
    return 0 if report["verdict"] == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("package", type=Path, nargs="?", help="mission package for a new SITL run")
    p.add_argument(
        "--evaluate", type=Path, metavar="RUN_DIR", help="score an existing run directory only"
    )
    p.add_argument(
        "--report", type=Path, help="report path stem (default RUN_DIR/operator-acceptance)"
    )
    p.add_argument(
        "--truth", type=Path, help="truth JSON (default: world truth via score_onboard_loop)"
    )
    p.add_argument("--radius-m", type=float, default=DEFAULT_RADIUS_M)
    p.add_argument("--world", default="gen_cnt2_w0000")
    p.add_argument("--airframe", default="x500_mono_cam")
    p.add_argument("--task", default="operator link mission")
    p.add_argument("--output", type=Path, default=SCRATCH / "e2e" / ("run-" + uuid.uuid4().hex[:8]))
    p.add_argument("--gcs-port", type=int, default=14560)
    p.add_argument("--link-timeout", type=float, default=120)
    p.add_argument("--gcs-timeout", type=float, default=2700)
    p.add_argument("--lock-name", default="e2e")
    p.add_argument("--lock-timeout", type=float, default=0)
    args = p.parse_args(argv)
    if args.evaluate is None and args.package is None:
        p.error("give PACKAGE to run a mission or --evaluate RUN_DIR to score one")
    try:
        if args.evaluate is not None:
            return _score(args.evaluate.resolve(), args)
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"acceptance failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
