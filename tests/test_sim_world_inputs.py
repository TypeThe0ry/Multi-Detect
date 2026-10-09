from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.sim_world_inputs import qualify_configured_sim_world_wind

SDF = """<sdf version="1.9"><world name="test">
<wind><linear_velocity>1 -0.4 0</linear_velocity></wind>
<spherical_coordinates><surface_model>EARTH_WGS84</surface_model>
<world_frame_orientation>ENU</world_frame_orientation></spherical_coordinates>
</world></sdf>"""


def _source(root, name, raw):
    raw = raw.encode() if isinstance(raw, str) else raw
    (root / name).write_bytes(raw)
    return EvidenceBinding(name, len(raw), hashlib.sha256(raw).hexdigest(), name, None)


def _inputs(root, sdf=SDF, wind=None):
    if wind is None:
        wind = {"wind_north_mps": 1.0, "wind_east_mps": -0.4}
    return {
        "artifact_root": root,
        "world_source": _source(root, "world.sdf", sdf),
        "runtime_source": _source(root, "runtime.json", json.dumps({"world": wind})),
    }


def test_direction_is_derived_from_enu_world_not_parallel_runtime_names(tmp_path):
    result = qualify_configured_sim_world_wind(**_inputs(tmp_path))
    assert result.wind_north_mps == -0.4
    assert result.wind_east_mps == 1.0
    assert result.runtime_direction_matches is False
    assert result.to_mapping()["configured_wind_enu_mps"] == [1.0, -0.4, 0.0]
    assert all(flag is False for flag in result.to_mapping()["claims"].values())


def test_correct_parallel_runtime_direction_is_reported_without_mutation(tmp_path):
    args = _inputs(tmp_path, wind={"wind_north_mps": -0.4, "wind_east_mps": 1.0})
    before = (tmp_path / "runtime.json").read_bytes()
    assert qualify_configured_sim_world_wind(**args).runtime_direction_matches is True
    assert (tmp_path / "runtime.json").read_bytes() == before


@pytest.mark.parametrize(
    "sdf",
    [
        SDF.replace("1.9", "1.8"),
        SDF.replace("ENU", "NED"),
        SDF.replace("EARTH_WGS84", "MOON_SCS"),
        SDF.replace(
            "</spherical_coordinates>", "<heading_deg>90</heading_deg></spherical_coordinates>"
        ),
        SDF.replace(
            "</spherical_coordinates>", "<heading_deg>nan</heading_deg></spherical_coordinates>"
        ),
        SDF.replace(
            "</spherical_coordinates>",
            "<heading_deg>0</heading_deg><heading_deg>0</heading_deg></spherical_coordinates>",
        ),
        SDF.replace("1 -0.4 0", "1 -0.4 1"),
        SDF.replace("1 -0.4 0", "1 -0.4"),
        SDF.replace("1 -0.4 0", "nan -0.4 0"),
        SDF.replace("1 -0.4 0", "101 -0.4 0"),
        SDF.replace("<wind><linear_velocity>1 -0.4 0</linear_velocity></wind>", ""),
        SDF.replace("</world>", "<wind><linear_velocity>1 -0.4 0</linear_velocity></wind></world>"),
        SDF.replace("</sdf>", "<world/></sdf>"),
        "<!DOCTYPE sdf>" + SDF,
        "not XML",
    ],
)
def test_missing_ambiguous_or_unsupported_world_inputs_refused(tmp_path, sdf):
    with pytest.raises(ValueError):
        qualify_configured_sim_world_wind(**_inputs(tmp_path, sdf=sdf))


@pytest.mark.parametrize("wind", [None, True, "1", float("inf"), float("nan")])
def test_runtime_north_is_not_coerced_or_defaulted(tmp_path, wind):
    with pytest.raises(ValueError):
        qualify_configured_sim_world_wind(
            **_inputs(tmp_path, wind={"wind_north_mps": wind, "wind_east_mps": 1})
        )


@pytest.mark.parametrize("key", ["world_source", "runtime_source"])
def test_changed_or_partial_source_cannot_qualify(tmp_path, key):
    args = _inputs(tmp_path)
    (tmp_path / args[key].path).write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity/bytes/SHA"):
        qualify_configured_sim_world_wind(**args)
    args = _inputs(tmp_path)
    args[key] = replace(args[key], json_pointer="")
    with pytest.raises(ValueError, match="whole-file"):
        qualify_configured_sim_world_wind(**args)


def test_explicit_zero_heading_matches_omitted_default(tmp_path):
    result = qualify_configured_sim_world_wind(
        **_inputs(
            tmp_path,
            sdf=SDF.replace(
                "</spherical_coordinates>", "<heading_deg>0</heading_deg></spherical_coordinates>"
            ),
        )
    )
    assert result.wind_north_mps == -0.4


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
def test_wide_encoded_dtd_cannot_bypass_raw_ascii_guard(tmp_path, encoding):
    document = (
        "<?xml version='1.0' encoding='UTF-16'?>\n"
        "<!DOCTYPE sdf [<!ENTITY velocity '1 -0.4 0'>]>\n" + SDF.replace("1 -0.4 0", "&velocity;")
    ).encode(encoding)
    with pytest.raises(ValueError, match="DTD"):
        qualify_configured_sim_world_wind(**_inputs(tmp_path, sdf=document))


def test_wide_encoded_plain_sdf_remains_supported_without_dtd(tmp_path):
    document = ("<?xml version='1.0' encoding='UTF-16'?>\n" + SDF).encode("utf-16")
    result = qualify_configured_sim_world_wind(**_inputs(tmp_path, sdf=document))
    assert (result.wind_east_mps, result.wind_north_mps) == (1.0, -0.4)


@pytest.mark.parametrize(
    "first", ["1_0", "\u0661", "\uff11.0"], ids=["underscore", "arabic", "fullwidth"]
)
def test_python_only_float_spellings_are_not_sdf_numeric_evidence(tmp_path, first):
    with pytest.raises(ValueError, match="numeric"):
        qualify_configured_sim_world_wind(
            **_inputs(tmp_path, sdf=SDF.replace("1 -0.4 0", first + " -0.4 0"))
        )


def test_python_only_zero_heading_is_not_defaulted_or_accepted(tmp_path):
    document = SDF.replace(
        "</spherical_coordinates>", "<heading_deg>0_0</heading_deg></spherical_coordinates>"
    )
    with pytest.raises(ValueError, match="numeric"):
        qualify_configured_sim_world_wind(**_inputs(tmp_path, sdf=document))


def test_ascii_scientific_sdf_numbers_remain_supported(tmp_path):
    result = qualify_configured_sim_world_wind(
        **_inputs(tmp_path, sdf=SDF.replace("1 -0.4 0", "+1.e0 -4E-1 .0"))
    )
    assert (result.wind_east_mps, result.wind_north_mps) == (1.0, -0.4)
