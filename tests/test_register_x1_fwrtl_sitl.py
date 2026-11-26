from dataclasses import asdict
import hashlib
import json

import pytest

from scripts import register_x1_fwrtl_sitl as setup
from test_x1_fw_native_rtl import bounds


@pytest.fixture
def reviewed_pair(tmp_path):
    # Synthetic fixture: this ID and these bounds are never runtime evidence.
    scope = setup.assumptions.SCOPES["wind_step"]
    raw = asdict(bounds(scope))
    validation = json.dumps({"basis": "ASSUMED_SIM", "status": "reviewed", "scope": scope,
                             "image_id": raw["image_id"], "px4_commit": setup.PX4_RTL_COMMIT,
                             "fw_airspeed_parameters": {"FW_AIRSPD_MIN": 12, "FW_AIRSPD_TRIM": 15, "FW_AIRSPD_MAX": 18}})
    raw["validation_sha256"] = hashlib.sha256(validation.encode()).hexdigest()
    path = tmp_path / "wind_step.json"
    path.write_text(json.dumps(raw))
    path.with_suffix(".validation.json").write_text(validation)
    return path


@pytest.mark.parametrize("mode", ["flight", "hil", "real", "live", "replay", "SIM"])
def test_non_sim_cannot_register_even_a_reviewed_pair(tmp_path, reviewed_pair, monkeypatch, mode):
    monkeypatch.setattr(setup, "verify", lambda *_: pytest.fail("non-SIM attempted Docker inspection"))
    output = tmp_path / "registration"
    with pytest.raises(ValueError, match="explicit sim mode"):
        setup.register(mode, reviewed_pair, output)
    assert not output.exists()


def test_archive_cannot_be_verified_as_candidate(monkeypatch):
    monkeypatch.setattr(setup, "_run", lambda *_: pytest.fail("archive attempted Docker inspection"))
    with pytest.raises(ValueError, match="distinct immutable"):
        setup.verify("sim", setup.assumptions.IMAGE_ID)


def test_sim_register_pair_is_byte_identical_and_never_overwrites(tmp_path, reviewed_pair, monkeypatch):
    verified = []
    monkeypatch.setattr(setup, "verify", lambda *request: verified.append(request))
    output = tmp_path / "registration"
    result = setup.register("sim", reviewed_pair, output)
    assert verified == [("sim", "sha256:" + "b" * 64)]
    assert result.read_bytes() == reviewed_pair.read_bytes()
    assert result.with_suffix(".validation.json").read_bytes() == reviewed_pair.with_suffix(".validation.json").read_bytes()
    with pytest.raises(FileExistsError):
        setup.register("sim", reviewed_pair, output)


@pytest.mark.parametrize("corruption", ["hash", "unproved_phase", "assessment_pending", "legacy_descend",
                                       "manifest_overspeed", "assessment_missing_pins", "assessment_overspeed"])
def test_registration_rejects_incomplete_or_tampered_evidence(tmp_path, reviewed_pair, monkeypatch, corruption):
    monkeypatch.setattr(setup, "verify", lambda *_: pytest.fail("bad proof attempted Docker inspection"))
    field = json.loads(reviewed_pair.read_bytes())
    validation = reviewed_pair.with_suffix(".validation.json")
    if corruption == "hash":
        validation.write_text(validation.read_text() + "\n")
    elif corruption == "unproved_phase":
        field.pop("phase_time_s")
    elif corruption == "legacy_descend":
        field["params"]["RTL_DESCEND_ALT"] = 30
    elif corruption == "manifest_overspeed":
        field["params"]["FW_AIRSPD_MAX"] = 23
    else:
        raw = json.loads(validation.read_bytes())
        if corruption == "assessment_missing_pins":
            del raw["fw_airspeed_parameters"]
        elif corruption == "assessment_overspeed":
            raw["fw_airspeed_parameters"]["FW_AIRSPD_MAX"] = 23
        else:
            raw["status"] = "blocked"
        data = json.dumps(raw).encode()
        validation.write_bytes(data)
        field["validation_sha256"] = hashlib.sha256(data).hexdigest()
    reviewed_pair.write_text(json.dumps(field))
    output = tmp_path / "registration"
    with pytest.raises(ValueError, match="manifest"):
        setup.register("sim", reviewed_pair, output)
    assert not output.exists()
