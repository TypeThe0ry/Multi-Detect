from copy import deepcopy

import pytest

from multidetect.backends.gazebo_sim import validate_frame_set_clock
from multidetect.clock import HighResolutionSystemClock


@pytest.mark.parametrize(
    "change", [None, "missing", "null", "session", "label", "resolution", "envelope"]
)
def test_qpc_all_frames_bound_to_manifest_clock(change):
    identity = HighResolutionSystemClock(session_id="frames").identity()
    document = {
        "captured_at_clock": "host_perf_counter",
        "host_clock_identity": identity,
        "frames": [
            {
                "provenance": {
                    "captured_at_clock": "host_perf_counter",
                    "host_clock_identity": deepcopy(identity),
                }
            }
            for _ in range(2)
        ],
    }
    last = document["frames"][-1]["provenance"]
    if change == "missing":
        del last["host_clock_identity"]
    elif change == "null":
        last["host_clock_identity"] = None
    elif change == "session":
        last["host_clock_identity"]["session_id"] = "other"
    elif change == "label":
        last["captured_at_clock"] = "host_monotonic"
    elif change == "resolution":
        last["host_clock_identity"]["resolution_ns"] += 1
    elif change == "envelope":
        del document["host_clock_identity"]
    if change is None:
        validate_frame_set_clock(document)
    else:
        with pytest.raises((ValueError, TypeError)):
            validate_frame_set_clock(document)


def test_legacy_explicit_identity_is_not_ignored():
    document = {"frames": [{"provenance": {"captured_at_clock": "host_monotonic"}}]}
    validate_frame_set_clock(document)
    document["frames"][0]["provenance"]["host_clock_identity"] = None
    with pytest.raises(ValueError):
        validate_frame_set_clock(document)
