#!/usr/bin/env python3
"""Verify an offline X1 candidate or register the demo's host-manifest pair."""
from __future__ import annotations

import argparse
import hashlib
import base64
import json
import lzma
from pathlib import Path
import re
import subprocess

from scripts.realworld.x1_resume import image_check
from scripts.run_simulator_docker import SIM_STARTUP_PATCHES, CONTAINER_WORLD, PX4_GZSIM_RC
from scripts.run_x1_autonomy_demo import PROFILE, assumptions
from multidetect.autonomy.serialization import strict_json_loads
from multidetect.sim_teacher.x1_fw_native_rtl import FwRtlBounds, PX4_RTL_COMMIT, _REQUIRED, _POSITIVE
from multidetect.sim_teacher.x1_native_handoff import IOWA_FW_AIRSPEED_PARAMETERS
from multidetect.autonomy.airframe_profile import load_airframe_profile

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"org.multidetect.x1-fwrtl-basis": "ASSUMED_SIM", "org.multidetect.x1-fwrtl-px4": PX4_RTL_COMMIT}
BASE_COMMIT = "83c4f4e580558816f0e398918f66358988004696"
MARKERS = ("MULTIDETECT_GIMBAL_BRIDGE_STABILIZE", "C11 bridge stabilization enabled (20 ms pose interval)",
           "C11 vehicle attitude unavailable; holding joints", "unsupported C11 pose command; holding previous target")
STAGE = "/tmp/x1-fwrtl-check"


def _run(*command):
    return subprocess.run(command, text=True, capture_output=True, check=True, timeout=120).stdout


def _scope_mode(mode):
    if mode != "sim":
        raise ValueError("FW RTL registration requires explicit sim mode (ASSUMED_SIM)")


def _read_only(image, shell, *args):
    return _run("docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m", "--entrypoint", "/bin/sh",
                image, "-ec", shell, "x1-fwrtl-check", *args)


def verify(mode, image):
    _scope_mode(mode)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image) or image == assumptions.IMAGE_ID:
        raise ValueError("expected a distinct immutable candidate image ID")
    base, candidate = json.loads(_run("docker", "image", "inspect", assumptions.IMAGE_ID, image))
    if base["Id"] != assumptions.IMAGE_ID or candidate["Id"] != image or image_check(image) != image:
        raise ValueError("immutable image identity or launcher bridge label mismatch")
    expected = dict(base["Config"])
    expected["Labels"] = {**(expected.get("Labels") or {}), **LABELS}
    if (candidate["Config"] != expected or candidate["RootFS"]["Layers"][:-1] != base["RootFS"]["Layers"]
            or any(candidate.get(k) != base.get(k) for k in ("Os", "Architecture", "Variant"))):
        raise ValueError("candidate does not preserve the archived runtime and config")

    common = r'''
r=/opt/px4-gazebo
deps=$(ldd "$r/bin/px4")
if printf '%s\n' "$deps" | grep -q 'not found'; then printf '%s\n' "$deps" >&2; exit 1; fi
for marker do tr -c '[:print:]' '\n' < "$r/bin/px4" | grep -Fx -- "$marker"; done
dpkg-query -W -f='${Version}\n' libgz-sim8 | grep -E '^8\.14\.0($|[-+~])'
dpkg-query -W -f='${Version}\n' libgz-rendering8 | grep -E '^8\.3\.0($|[-+~])'
for system in wind-effects air-speed; do
    find /usr/lib -name "libgz-sim*-${system}-system.so*" -type f > "/tmp/$system"
    test -s "/tmp/$system"
done
test -x "$r/bin/px4-entrypoint.sh"
test -f "$r/bin/px4-alias.sh"
test -f "$r/share/gz/server.config"
find "$r/lib/gz/plugins" -type f -name '*.so*' > /tmp/plugins
test -s /tmp/plugins
find /opt -maxdepth 1 -name 'multidetect*' -print > /tmp/hooks
{
    printf '%s\n' "$r/bin/px4-entrypoint.sh" "$r/bin/px4-alias.sh" "$r/bin/px4-gazebo" "$r/share/gz/server.config"
    cat /tmp/plugins /tmp/wind-effects /tmp/air-speed
    while IFS= read -r hook; do find "$hook" -type f; done < /tmp/hooks
} | LC_ALL=C sort -u > /tmp/files
while IFS= read -r file; do sha256sum "$file"; done < /tmp/files
'''
    runtime = _read_only(image, common, *MARKERS)
    if runtime != _read_only(assumptions.IMAGE_ID, common, *MARKERS):
        raise ValueError("Gazebo stack, gimbal markers or runtime hook files differ from horizon4")
    for ref, commit in ((assumptions.IMAGE_ID, BASE_COMMIT), (image, PX4_RTL_COMMIT)):
        _read_only(ref, "tr -c '[:print:]' '\\n' < /opt/px4-gazebo/bin/px4 | grep -Fx -- \"$1\"", commit)
    # Exercise the actual launcher shell prefix; only its sed -i input is copied to tmpfs.
    patches = SIM_STARTUP_PATCHES.replace(PX4_GZSIM_RC, STAGE + "/rc").replace(CONTAINER_WORLD, STAGE + "/world")
    _read_only(image, f'mkdir {STAGE}; cp {PX4_GZSIM_RC} {STAGE}/rc; '
               f'echo "<!-- multidetect-wind-gust amplitude_percent=0 period=10 -->" > {STAGE}/world; '
               + patches)
    paths = "etc/init.d-posix/airframes"
    airframe = _read_only(image, f'cd /opt/px4-gazebo/{paths}; '
                         'sha256sum 4050_gz_delta_tailsitter 4051_gz_delta_tailsitter_gimbal')
    airframe_digests = {name: digest for digest, name in map(str.split, airframe.splitlines())}
    if airframe_digests.keys() != {"4050_gz_delta_tailsitter", "4051_gz_delta_tailsitter_gimbal"}:
        raise ValueError("missing packaged 4050/4051 airframe")
    for name, digest in airframe_digests.items():
        if hashlib.sha256((ROOT / "simulation/px4/airframes-main" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"candidate airframe mismatch: {name}")
    params = json.loads(lzma.decompress(base64.b64decode(_read_only(image, "base64 /opt/px4-gazebo/etc/extras/parameters.json.xz"))))
    defaults = {row["name"]: row["default"] for row in params["parameters"] if "default" in row}
    keys = {*_REQUIRED, *_POSITIVE, "SIM_BAT_DRAIN", "SIM_BAT_MIN_PCT", "SENS_GNSS0_DELAY"}
    if not keys <= defaults.keys():
        raise ValueError(f"candidate metadata lacks parameters {sorted(keys - defaults.keys())}")
    return {"basis": "ASSUMED_SIM", "status": "static_verified", "image_id": image,
            "base_id": assumptions.IMAGE_ID, "px4_commit": PX4_RTL_COMMIT,
            "px4_binary_sha256": _read_only(image, "sha256sum /opt/px4-gazebo/bin/px4").split()[0],
            "image_inspect": candidate, "inherited_runtime": runtime.splitlines(),
            "airframe_sha256": airframe.splitlines(), "compiled_defaults": {k: defaults[k] for k in sorted(keys)},
            "fw_airspeed_parameters": dict(IOWA_FW_AIRSPEED_PARAMETERS),
            "startup_prefix_checked": True, "prearm_readback": False}


def register(mode, manifest, output_dir):
    _scope_mode(mode)
    raw = manifest.read_bytes()
    bounds = FwRtlBounds.from_host_manifest(strict_json_loads(raw.decode(), name=str(manifest)))
    validation = manifest.with_suffix(".validation.json").read_bytes()
    assessment = strict_json_loads(validation.decode(), name="FW RTL validation")
    pins = assumptions.generate(mode=mode, world=assumptions.WORLD, image=assumptions.IMAGE,
                                profile_path=PROFILE)["px4_native_parameters"]["value"]
    if (hashlib.sha256(validation).hexdigest() != bounds.validation_sha256
            or bounds.scope not in {*assumptions.scopes_for("wind_step"), *assumptions.scopes_for("target_found")}
            or not bounds.valid_for(bounds.scope, load_airframe_profile(PROFILE))
            or not bounds.matches_validation(assessment)
            or any(bounds.params[k] != value for k, value in pins.items())):
        raise ValueError("FW RTL needs a separately reviewed ASSUMED_SIM manifest and matching validation")
    verify(mode, bounds.image_id)
    # A fresh directory cannot replace the archived allowlist or another review.
    output_dir.mkdir()
    (output_dir / manifest.with_suffix(".validation.json").name).write_bytes(validation)
    target = output_dir / manifest.name
    target.write_bytes(raw)
    return target.resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--image", required=True)
    check.add_argument("--output", type=Path, required=True)
    setup = sub.add_parser("register")
    setup.add_argument("--manifest", type=Path, required=True)
    setup.add_argument("--output-dir", type=Path, required=True)
    for command in (check, setup):
        command.add_argument("--mode", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "verify":
            receipt = verify(args.mode, args.image)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, indent=2, sort_keys=True)
                stream.write("\n")
        else:
            print(register(args.mode, args.manifest, args.output_dir))
    except subprocess.CalledProcessError as exc:
        parser.error(f"offline image check exit {exc.returncode}: {exc.stderr.strip()}")
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
