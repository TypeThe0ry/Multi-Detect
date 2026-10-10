import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "docs/evidence/sim-teacher-first-real-episode-20261010-r1.json"
INDEX_KEY = "sim_teacher_first_real_episode_20261010_r1"


def read(path):
    return json.loads(path.read_bytes())


def check_binding(binding):
    path = (ROOT / binding["path"]).resolve(strict=True)
    assert path.is_relative_to(ROOT)
    raw = path.read_bytes()
    assert len(raw) == binding["bytes"]
    assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    return path


def test_episode_sources_artifacts_and_index_match_actual_bytes():
    stage = read(STAGE)
    paths = set()
    for binding in (*stage["sources"], *stage["retained_artifact_bindings"], *stage["protected"]):
        path = check_binding(binding)
        assert path not in paths
        paths.add(path)
    entry = read(ROOT / "docs/current-progress.json")[INDEX_KEY]
    assert check_binding(entry["evidence"]) == STAGE
    assert entry["claims"] == stage["claims"]
    assert entry["validation"] == stage["validation"]


def test_complete_sim_episode_counts_and_audit_gate():
    stage = read(STAGE)
    assert stage["status"] == "external_sim_passed"
    dataset = stage["dataset"]
    assert dataset["steps"] == 5
    assert dataset["rgb_png"] == 10
    assert dataset["depth_npy"] == 10
    assert dataset["mavlink_lines"] == 17789
    assert dataset["depth_dtype"] == "float32"
    validation = stage["validation"]
    assert validation["native_exit_code"] == 0
    assert validation["success_rate"] == 1.0
    assert validation["position_error_mean_m"] == 0.11426998697525384
    assert validation["position_error_max_m"] == 0.21768332024481699
    assert validation["yaw_error_mean_deg"] == 2.497953953825487
    assert validation["yaw_error_max_deg"] == 5.485698914876579


def test_claim_boundaries_remain_sim_only():
    claims = read(STAGE)["claims"]
    assert claims["sim_only"] is True
    assert all(claims[key] is False for key in ("hil", "real_hardware", "outdoor", "model_trained_on_data", "promotion", "final_release_allowed"))
