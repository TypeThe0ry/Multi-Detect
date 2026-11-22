from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.polaris_sim.ai.contracts import (
    ContractError,
    validate_spica_query,
    validate_spica_request,
    validate_spica_response,
    validate_spica_result,
)
from multidetect.polaris_sim.ai.events import EventJournal
from multidetect.polaris_sim.ai.inputs import MavlinkTelemetryReader, is_private_address
from multidetect.polaris_sim.ai.service import AIService
from multidetect.polaris_sim.ai.tunnel import SignedTunnelPublisher, _sigma_from_covariance

ROOT = Path(__file__).resolve().parents[2]
SIRIUS_SOURCE = Path("/data/projects/Volatic/Sirius/wt/sirius-runtime/src")
QUERY = {
    "schema_version": "1.0",
    "categories": ["vehicle"],
    "attributes": [{"name": "color", "op": "eq", "value": "red"}],
    "outputs": ["wgs84", "covariance", "confidence"],
}


class FakeCamera:
    fallback_video = None

    def capture(self, slot: int) -> tuple[None, str, None]:
        return None, f"slot-{slot}-fixture", None


def _config(*, enabled: bool = False) -> dict[str, object]:
    components = {
        "altair": enabled,
        "vega": enabled,
        "vlm": enabled,
        "mizar": enabled,
        "tracking": enabled,
        "publish_polaris": enabled,
    }
    return {
        "sirius_source": str(SIRIUS_SOURCE),
        "runtime": {"components": components},
        "adapters": {"vega": None, "mizar": None},
        "inputs": {
            "camera": {"rtsp_template": "rtsp://127.0.0.1:8554/v{index}/cam"},
            "telemetry": {"enabled": False},
        },
        "target_push": {"enabled": False},
    }


def test_spica_snapshots_validate_request_response_and_result() -> None:
    request = {"request_id": "q-1", "query": QUERY}
    result = {
        "schema_version": "1.0",
        "lat": 37.4,
        "lon": -122.1,
        "cov": [[2.0, 0.2], [0.2, 3.0]],
        "confidence": 0.81,
    }
    response = {
        "id": "q-1",
        "name": "query_target",
        "status": "ok",
        "latency_ms": 2.0,
        "data": {"schema_version": "1.0", "results": [result]},
        "warnings": [],
    }
    validate_spica_query(QUERY)
    validate_spica_request(request)
    validate_spica_result(result)
    validate_spica_response(response)
    with pytest.raises(ContractError):
        validate_spica_query({**QUERY, "new_schema_field": True})


def test_spica_schema_snapshots_match_recorded_source_hashes() -> None:
    expected = {
        "target_query.json": "bc49b8d81acd47e1a4596a6b21eb1abe30b94461274cc0e4dad0b8b6ea644ca9",
        "target_result.json": "022287de1ebec0c3d9207fb53809665d97a816a27767d23b047b86181736b483",
        "query_target.json": "60814c6da96356f35eb3cae07fd9013694ce55c28f093a036f1c24aa086250a1",
        "common.json": "e8c65f5de20987201c3ea6535214448f12b6aed66abee3f526b309db9ff2766b",
        "envelope.json": "eef0013ebef0e364a2715753048362a03c583b59d84d195dbfde06cb36077f9d",
    }
    import hashlib

    for name, digest in expected.items():
        path = ROOT / "src/multidetect/polaris_sim/ai/schemas" / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest


def test_disabled_runtime_abstains_without_emitting_results() -> None:
    service = AIService(_config(), start_inputs=False, camera_source=FakeCamera())
    try:
        response = service.query_target({"query": QUERY}, slot=5)
        assert response["result"]["status"] == "unavailable"
        assert response["runtime"]["status"] == "disabled"
        assert response["runtime"]["degradation"] == "abstain"
        assert response["result"]["data"] is None
        assert service.journal.latest_id() == 0
    finally:
        service.close()


def test_enabled_cpu_stub_runtime_emits_ordered_events_and_spica_response() -> None:
    if not (SIRIUS_SOURCE / "multidetect/sirius_runtime.py").is_file():
        pytest.skip("local Sirius runtime checkout is unavailable")
    service = AIService(_config(enabled=True), start_inputs=False, camera_source=FakeCamera())
    try:
        response = service.query_target({"request_id": "q-events", "query": QUERY}, slot=6)
        validate_spica_response(response["result"])
        assert response["runtime"]["status"] == "completed"
        events = []
        cursor = 0
        while event := service.journal.next_after(cursor, 0):
            events.append(event["event"])
            cursor = event["id"]
        assert events == [
            "query.parsed",
            "vega.detected",
            "vlm.reviewed",
            "mizar.located",
            "tracking.updated",
            "published",
        ]
        assert all(
            service.journal.next_after(index, 0)["data"]["vehicle_slot"] == 6
            for index in range(len(events))
        )
    finally:
        service.close()


def test_sirius_component_exception_returns_degraded_abstain() -> None:
    if not (SIRIUS_SOURCE / "multidetect/sirius_runtime.py").is_file():
        pytest.skip("local Sirius runtime checkout is unavailable")
    service = AIService(_config(enabled=True), start_inputs=False, camera_source=FakeCamera())

    class BrokenDetector:
        def detect(self, query: object, frame: object | None = None) -> object:
            raise RuntimeError("synthetic detector fault")

    try:
        service.runtime.runtime.detector = BrokenDetector()
        response = service.query_target({"query": QUERY}, slot=5)
        assert response["runtime"]["status"] == "degraded"
        assert response["runtime"]["degradation"] == "abstain"
        assert response["result"]["status"] == "unavailable"
        names = []
        cursor = 0
        while event := service.journal.next_after(cursor, 0):
            names.append(event["event"])
            cursor = event["id"]
        assert names == ["query.parsed", "runtime.degraded"]
    finally:
        service.close()


def test_private_address_filter_and_mavlink_push_default() -> None:
    assert is_private_address("192.168.1.10")
    assert is_private_address("127.0.0.1")
    assert not is_private_address("8.8.8.8")
    publisher = SignedTunnelPublisher({"enabled": False})
    try:
        assert publisher.active is False
        assert publisher.reason == "disabled"
    finally:
        publisher.close()
    assert _sigma_from_covariance({"matrix_m2": [[4.0, 1.0], [1.0, 9.0]]}) > 3.0
    assert _sigma_from_covariance({}) is None


def test_event_journal_reconnects_from_last_event_id_with_bounded_history() -> None:
    journal = EventJournal(capacity=2)
    first = journal.publish("query.parsed", {"request_id": "q-1"})
    journal.publish("vega.detected", {"request_id": "q-1"})
    journal.publish("published", {"request_id": "q-1"})
    assert first["id"] == 1
    assert journal.next_after(1, 0)["event"] == "vega.detected"
    assert journal.next_after(2, 0)["event"] == "published"
    assert journal.next_after(3, 0) is None


def test_heartbeat_does_not_refresh_position_or_gps_age(monkeypatch: pytest.MonkeyPatch) -> None:
    current_time = 100.0
    monkeypatch.setattr("multidetect.polaris_sim.ai.inputs.time.monotonic", lambda: current_time)
    reader = MavlinkTelemetryReader("tcp:127.0.0.1:5770", enabled=False)

    def message(kind: str, **fields: object) -> SimpleNamespace:
        return SimpleNamespace(
            get_type=lambda: kind,
            get_srcSystem=lambda: 1,
            **fields,
        )

    reader._record(
        message(
            "GLOBAL_POSITION_INT",
            lat=374000000,
            lon=-1220000000,
            alt=100000,
            relative_alt=50000,
            hdg=9000,
        )
    )
    reader._record(
        message("GPS_RAW_INT", fix_type=3, satellites_visible=12, eph=100)
    )
    current_time = 103.0
    reader._record(message("HEARTBEAT"))
    sample = reader.snapshot(5)
    assert sample["fresh"] is False
    assert sample["gps_fresh"] is False
