"""Shared range and LOD parameters for realworld scenes."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RangeProfile:
    name: str
    extent_m: float
    near_radius_m: float
    mid_radius_m: float
    spawn_clearance_m: float = 25.0
    near_texture_px: int = 4096
    mid_texture_px: int = 2048
    far_texture_px: int = 1024
    max_prop_triangles: int = 4_000_000

    @property
    def half_extent_m(self) -> float:
        return self.extent_m / 2.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["half_extent_m"] = self.half_extent_m
        return data


def make_profile(extent_m: float, *, name: str = "default") -> RangeProfile:
    extent_m = float(extent_m)
    if extent_m < 500 or extent_m > 20_000 or extent_m % 2:
        raise ValueError("extent_m must be an even value between 500 and 20000 m")
    half = extent_m / 2.0
    near = 850.0 if extent_m <= 3000 else min(1_500.0, 850.0 + (extent_m - 3000.0) * 0.2)
    clearance = 25.0 if extent_m <= 3000 else min(60.0, 25.0 + (extent_m - 3000.0) * 0.005)
    return RangeProfile(name=name, extent_m=extent_m,
                        near_radius_m=near,
                        mid_radius_m=min(2_000.0, half * 0.78),
                        spawn_clearance_m=clearance,
                        far_texture_px=2048 if extent_m <= 3000 else 1024)


def profile_for_site(site: dict, extent_m: float | None = None) -> RangeProfile:
    actual = float(site["size_m"] if extent_m is None else extent_m)
    return make_profile(actual, name=str(site.get("range_profile", "default")))
