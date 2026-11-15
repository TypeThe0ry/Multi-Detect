from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_range_profile_keeps_legacy_near_radius_and_scales_wide_extent() -> None:
    from scripts.realworld.range_profile import make_profile

    legacy = make_profile(2000)
    wide = make_profile(6000)
    assert legacy.half_extent_m == 1000
    assert legacy.near_radius_m == 850
    assert wide.half_extent_m == 3000
    assert wide.near_radius_m > legacy.near_radius_m
    assert wide.mid_radius_m < wide.half_extent_m


def test_range_profile_rejects_odd_or_unbounded_extent() -> None:
    from scripts.realworld.range_profile import make_profile

    for value in (499, 2001, 20001):
        with pytest.raises(ValueError):
            make_profile(value)


def test_wide_area_audit_checks_world_constraints(tmp_path: Path) -> None:
    from scripts.realworld.validate_wide_area import audit

    scene = tmp_path / "scene.json"
    scene.write_text(json.dumps({"size_m": 6000, "stats": {"triangles": 12, "prop_triangles": 8, "prop_instances": 3}}))
    world = tmp_path / "world.sdf"
    world.write_text('<sdf><world><model name="x"/></world></sdf>')
    result = audit(scene, world)
    assert result["extent_m"] == 6000
    assert result["budget_ok"] is True
    assert result["world_static_ok"] is True


def test_wide_area_audit_rejects_plugin_include_and_network(tmp_path: Path) -> None:
    from scripts.realworld.validate_wide_area import audit

    scene = tmp_path / "scene.json"
    scene.write_text(json.dumps({"size_m": 6000, "stats": {}}))
    world = tmp_path / "world.sdf"
    world.write_text('<sdf><world><plugin/><include/><uri>https://example.invalid/a</uri></world></sdf>')
    result = audit(scene, world)
    assert result["world_static_ok"] is False

