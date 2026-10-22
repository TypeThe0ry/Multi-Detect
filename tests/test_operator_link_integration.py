"""Loopback coverage for prompt package admission and mission telemetry."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_operator import MissionOperatorAdapter
from multidetect.autonomy.mission_operator_protocol import CHUNK, document_chunks
from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import OnboardMissionLog
from multidetect.operator_link import SelectionCommandGuard, VideoGeometry
from multidetect.operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
from multidetect.operator_protocol import OperatorTunnelCodec
from multidetect.operator_udp import UdpOperatorSelectionServer


def _package() -> dict:
    value = json.loads(
        Path("configs/onboard/mission-package-example.json").read_text(encoding="utf-8")
    )
    plan = value.pop("plan")
    value["task_text"] = "Count the traffic cones and return home."
    return value, plan


def test_signed_task_only_udp_loopback_plan_approval_and_status(tmp_path: Path) -> None:
    pytest.importorskip("pymavlink")
    raw, compiled = _package()
    payload = json.dumps(raw, separators=(",", ":")).encode()
    compiled_plan = load_mission_package({**raw, "plan": compiled}).plan
    key = b"L" * 32
    geometry = VideoGeometry("camera-main", 1280, 720)

    def make_supervisor(package):
        executor = MissionExecutor(
            compiled_plan, loop_factory=lambda *a, **k: None, refs=package.refs
        )
        return MissionSupervisor(
            executor,
            package,
            log=OnboardMissionLog(tmp_path, package.mission_id),
            requires_approval=True,
        )

    onboard_codec = OperatorTunnelCodec(hmac_key=key, geometries=(geometry,))
    ground_codec = OperatorTunnelCodec(hmac_key=key, geometries=(geometry,))
    onboard = OperatorMavlinkTunnelAdapter(
        onboard_codec,
        OperatorMavlinkEndpoint(1, 191, 255, 190),
        signing_key=b"S" * 32,
        signing_link_id=1,
        initial_signing_timestamp=10,
        receive_signing_link_id=2,
    )
    ground = OperatorMavlinkTunnelAdapter(
        ground_codec,
        OperatorMavlinkEndpoint(255, 190, 1, 191),
        signing_key=b"S" * 32,
        signing_link_id=2,
        initial_signing_timestamp=20,
        receive_signing_link_id=1,
    )
    adapter = MissionOperatorAdapter(
        onboard_codec, package_loader=make_supervisor, preflight_allowed=lambda: True
    )
    with (
        UdpOperatorSelectionServer(
            bind_host="127.0.0.1",
            port=0,
            mavlink=onboard,
            guard=SelectionCommandGuard(geometry),
            mission_adapter=adapter,
            receive_timeout_s=0.2,
        ) as server,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
    ):
        sock.settimeout(1)
        for sequence, chunk in enumerate(document_chunks(payload, 11), 1):
            wire = ground_codec.encode_mission(28, chunk.body, sequence=sequence, sent_at_s=1)
            sock.sendto(ground.wrap_authenticated_operator_payload(wire), server.bound_address)
            server.serve_once()
            sock.recvfrom(4096)
        assert adapter.supervisor is not None
        status = adapter.status_document()
        assert status["v"] == 2 and status["state"] == "awaiting_approval"
        assert len(json.dumps(status, ensure_ascii=False, separators=(",", ":")).encode()) <= 1200
        digest = adapter.supervisor.plan_sha256
        command = json.dumps(
            {
                "mission_id": adapter.supervisor.package.mission_id,
                "type": "approve_plan",
                "plan_sha256": digest,
            },
            separators=(",", ":"),
        ).encode()
        for sequence, chunk in enumerate(document_chunks(command, 12, message_type=37), 100):
            wire = ground_codec.encode_mission(37, chunk.body, sequence=sequence, sent_at_s=1)
            sock.sendto(ground.wrap_authenticated_operator_payload(wire), server.bound_address)
            server.serve_once()
            sock.recvfrom(4096)
        assert adapter.supervisor.plan_approved
        assert not adapter.supervisor.approve_plan("0" * 64)


def test_final_summary_pages_require_all_pages_and_reassemble(tmp_path: Path) -> None:
    from multidetect.autonomy.mission_operator_protocol import MissionWireMessage

    package = load_mission_package(
        json.loads(Path("configs/onboard/mission-package-example.json").read_text(encoding="utf-8"))
    )
    executor = MissionExecutor(package.plan, loop_factory=lambda *a, **k: None, refs=package.refs)
    supervisor = MissionSupervisor(
        executor, package, log=OnboardMissionLog(tmp_path, package.mission_id)
    )
    adapter = MissionOperatorAdapter(
        OperatorTunnelCodec(
            hmac_key=b"x" * 32,
            geometries=(VideoGeometry("camera-main", 1280, 720),),
        ),
        supervisor=supervisor,
    )
    pending = adapter._new_telemetry(
        {
            "kind": "final_summary",
            "mission_id": package.mission_id,
            "summary": {"findings": ["x" * 3000]},
        }
    )
    pages = []
    for offset in range(0, len(pending.messages), 1):
        first = pending.messages[offset]
        if not isinstance(first, MissionWireMessage):
            continue
        _token, _index, count, _total, _digest = CHUNK.unpack_from(first.body)
        if _index != 0:
            continue
        body = b"".join(
            item.body[CHUNK.size :] for item in pending.messages[offset : offset + count]
        )
        pages.append(json.loads(body))
    assert pages and all(page["kind"] == "final_summary_page" for page in pages)
    assert len({page["pages"] for page in pages}) == 1
    received_pages = {page["page"] for page in pages[:-1]}
    assert len(received_pages) != pages[0]["pages"]
    reassembled = json.loads(
        "".join(page["body"] for page in sorted(pages, key=lambda p: p["page"]))
    )
    assert reassembled["findings"] == ["x" * 3000]
    assert len(pages) > 1
    assert len({page["page"] for page in pages[:-1]}) == len(pages) - 1


def test_reject_and_comms_free_approval_policy(tmp_path: Path) -> None:
    package = load_mission_package(
        json.loads(Path("configs/onboard/mission-package-example.json").read_text(encoding="utf-8"))
    )
    executor = MissionExecutor(package.plan, loop_factory=lambda *a, **k: None, refs=package.refs)
    rejected = MissionSupervisor(
        executor,
        package,
        log=OnboardMissionLog(tmp_path / "reject", package.mission_id),
        requires_approval=True,
    )
    assert not rejected.approve_plan("0" * 64)
    assert rejected.reject_plan(rejected.plan_sha256)
    assert rejected.plan_rejected and not rejected.plan_approved
    free = MissionSupervisor(
        MissionExecutor(package.plan, loop_factory=lambda *a, **k: None, refs=package.refs),
        package,
        log=OnboardMissionLog(tmp_path / "free", package.mission_id),
    )
    assert free.plan_approved
