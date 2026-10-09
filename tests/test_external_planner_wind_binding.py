import json
import pathlib
from types import SimpleNamespace

import pytest

from scripts.audit_external_planner_wind_binding import audit_report


def _runtime():
    return SimpleNamespace(
        canonical_hash="a" * 64,
        world=SimpleNamespace(wind_north_mps=3.0, wind_east_mps=-1.5, gust_mps=2.0),
    )


def _report(tmp_path):
    context = {
        "wind_north_mps": 3.0,
        "wind_east_mps": -1.5,
        "snapshot_id": "ctx-1",
    }
    from multidetect.autonomy.serialization import canonical_sha256

    sortie = {
        "scenario": {"runtime_input_sha256": "a" * 64},
        "documents": {
            "context_snapshot": {
                "document": context,
                "sha256": canonical_sha256(context),
            }
        },
    }
    path = tmp_path / "mission-001.json"
    path.write_text(json.dumps(sortie), encoding="utf-8")
    return {"gate_passed": True, "completed": [{"task_id": "t1", "evidence_path": str(path)}]}


def test_runtime_wind_is_bound_to_each_context_snapshot(tmp_path):
    result = audit_report(_report(tmp_path), _runtime())
    assert result["runtime_wind_reached_context"] is True
    assert result["configured_wind_mps"] == {"north": 3.0, "east": -1.5, "gust": 2.0}


@pytest.mark.parametrize("field", ["wind_north_mps", "wind_east_mps"])
def test_context_wind_mismatch_fails_closed(tmp_path, field):
    report = _report(tmp_path)
    path = report["completed"][0]["evidence_path"]
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    payload["documents"]["context_snapshot"]["document"][field] = 0.0
    from multidetect.autonomy.serialization import canonical_sha256

    payload["documents"]["context_snapshot"]["sha256"] = canonical_sha256(
        payload["documents"]["context_snapshot"]["document"]
    )
    pathlib.Path(path).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="ContextSnapshot wind mismatch"):
        audit_report(report, _runtime())
