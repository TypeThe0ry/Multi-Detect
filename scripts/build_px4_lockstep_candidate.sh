#!/usr/bin/env bash
# Rebuild the pinned SITL candidate into fresh tags; never replace an existing image/tag.
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_root=${1:-/data/scratch/px4-clock-build-20261108}
base_commit=83c4f4e580558816f0e398918f66358988004696
upstream_commit=27a21488b21f6334ec6eb9a89ddfe44016814500
expected_base_image=sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2
base_image=multidetect/px4-sitl-gazebo@sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2
expected_patch_sha256=43bfe32d5d7572b866b3d411c2394d941b6bcc8e19ace5bdd6e230845cdf0559
expected_px4_sha256=9c2280e005ad426f01cdffddf2114e1e4b13e26aa9f5996c4b52cdfa9e722d99
build_stamp=$(date -u +%Y%m%dT%H%M%S)-$$
candidate_image=${2:-multidetect/px4-sitl-gazebo:lockstep-lifetime-r1-rebuild-$build_stamp}
toolchain_image=multidetect/px4-clock-toolchain:lockstep-lifetime-r1-rebuild-$build_stamp
patch="$repo_root/simulation/px4/patches/lockstep-lifetime-r1.patch"
jobs=${PX4_BUILD_JOBS:-8}
[[ $jobs =~ ^[1-9][0-9]*$ && $jobs -le 12 ]] || { echo 'PX4_BUILD_JOBS must be 1..12' >&2; exit 2; }
case "$source_root" in /data/*) ;; *) echo 'PX4 sources and outputs must stay on /data' >&2; exit 2 ;; esac
[[ $(sha256sum "$patch" | cut -d' ' -f1) == "$expected_patch_sha256" ]] || { echo 'Unexpected lifetime-r1 patch hash' >&2; exit 2; }
case "$candidate_image" in
    multidetect/px4-sitl-gazebo:lockstep-lifetime-r1-rebuild-?*) ;;
    *) echo 'Output must be a fresh lockstep-lifetime-r1-rebuild-* tag' >&2; exit 2 ;;
esac
for output_image in "$candidate_image" "$toolchain_image"; do
    if docker image inspect "$output_image" >/dev/null 2>&1; then echo "Refusing to overwrite existing image $output_image" >&2; exit 2; fi
done
actual_base=$(docker image inspect "$base_image" --format '{{.Id}}')
[[ $actual_base == "$expected_base_image" ]] || { echo "Unexpected base runtime image: $actual_base" >&2; exit 2; }
mkdir -p "$source_root"
if [[ ! -d $source_root/.git ]]; then
    [[ -z $(ls -A "$source_root") ]] || { echo 'Source directory must be empty or the pinned PX4 checkout' >&2; exit 2; }
    git init -q "$source_root"
    git -C "$source_root" remote add origin https://github.com/PX4/PX4-Autopilot.git
    git -C "$source_root" fetch --depth=1 origin "$base_commit"
    git -C "$source_root" checkout --detach FETCH_HEAD
fi
[[ $(git -C "$source_root" rev-parse HEAD) == "$base_commit" ]] || { echo 'Unexpected PX4 source commit' >&2; exit 2; }
git -C "$source_root" fetch --depth=1 origin tag v1.18.0-beta2
git -C "$source_root" fetch --depth=1 origin "$upstream_commit"
[[ $(git -C "$source_root" rev-parse 'v1.18.0-beta2^{}') == "$base_commit" ]]
if git -C "$source_root" apply --check "$patch"; then
    git -C "$source_root" apply "$patch"
else
    git -C "$source_root" apply --reverse --check "$patch"
fi
# Refuse to silently combine the clock patch with other tracked PX4 modifications.
while IFS= read -r path; do
    case "$path" in
        platforms/posix/src/px4/common/lockstep_scheduler/include/lockstep_scheduler/lockstep_scheduler.h|platforms/posix/src/px4/common/lockstep_scheduler/src/lockstep_scheduler.cpp) ;;
        *) echo "Unexpected modified PX4 source: $path" >&2; exit 2 ;;
    esac
done < <(git -C "$source_root" diff --name-only HEAD)
git -C "$source_root" diff --check
git -C "$source_root" submodule update --init --recursive --depth 1 --jobs 4 \
    src/modules/mavlink/mavlink src/drivers/gps/devices src/lib/events/libevents \
    src/modules/uxrce_dds_client/Micro-XRCE-DDS-Client src/lib/heatshrink/heatshrink \
    src/lib/cdrstream/cyclonedds src/lib/cdrstream/rosidl Tools/simulation/gz \
    src/modules/simulation/gz_plugins/optical_flow/PX4-OpticalFlow
out_root="$source_root/clock-candidate-rebuilds/$build_stamp"
mkdir -p "$out_root/runtime-context"
docker build --pull=false --build-arg "BASE_IMAGE=$base_image" -f "$repo_root/simulation/px4/clock-build/Dockerfile.toolchain" -t "$toolchain_image" "$repo_root/simulation/px4/clock-build"
docker run --rm --cpus="$jobs" --memory=64g --memory-swap=64g \
    --mount "type=bind,src=$source_root,dst=/src" --mount "type=bind,src=$repo_root,dst=/repo,readonly" \
    --workdir /src --env "PX4_CANDIDATE_OUT=/src/clock-candidate-rebuilds/$build_stamp" --env GZ_DISTRO=harmonic --env "PX4_BUILD_JOBS=$jobs" --entrypoint bash "$toolchain_image" -c '
        set -euo pipefail
        git config --global --add safe.directory /src
        /repo/scripts/validate_px4_lockstep_backport.sh /src "$PX4_CANDIDATE_OUT/validation" | tee "$PX4_CANDIDATE_OUT/red-green.log"
        cmake -S . -B build/px4_sitl_default -G Ninja -DCONFIG=px4_sitl_default -DCMAKE_BUILD_TYPE=RelWithDebInfo -DPYTHON_EXECUTABLE=/opt/px4-build-venv/bin/python
        cmake --build build/px4_sitl_default --target px4 -j"$PX4_BUILD_JOBS"
        g++ --version > "$PX4_CANDIDATE_OUT/compiler.txt"
        python -m pip freeze > "$PX4_CANDIDATE_OUT/python-packages.txt"
        dpkg-query -W > "$PX4_CANDIDATE_OUT/system-packages.txt"
    '
cp "$source_root/build/px4_sitl_default/bin/px4" "$out_root/runtime-context/px4"
actual_px4=$(sha256sum "$out_root/runtime-context/px4" | cut -d' ' -f1)
[[ $actual_px4 == "$expected_px4_sha256" ]] || {
    echo "Rebuild differs from accepted binary: $actual_px4 (expected $expected_px4_sha256). Artifacts preserved; review compiler/package/build-path drift and repeat acceptance before changing runtime identity." >&2
    exit 2
}
cp "$repo_root/simulation/px4/clock-build/Dockerfile.runtime" "$out_root/runtime-context/Dockerfile"
docker build --pull=false --build-arg "BASE_IMAGE=$base_image" -t "$candidate_image" "$out_root/runtime-context"
docker run --rm --network none --entrypoint sh "$candidate_image" -c 'ldd /opt/px4-gazebo/bin/px4' | tee "$out_root/ldd.txt"
if rg -q 'not found' "$out_root/ldd.txt"; then echo 'Candidate has missing runtime dependencies' >&2; exit 1; fi
[[ $(docker image inspect "$base_image" --format '{{.Id}}') == "$actual_base" ]]
image_id=$(docker image inspect "$candidate_image" --format '{{.Id}}')
python3 - "$out_root" "$actual_base" "$image_id" "$base_commit" "$upstream_commit" "$patch" "$candidate_image" <<'PY'
import hashlib,json,sys
from pathlib import Path
out,base,image,commit,upstream,patch,tag=sys.argv[1:]
root=Path(out)
x={"base_image_id":base,"candidate_image_id":image,"candidate_tag":tag,"px4_base_commit":commit,"upstream_fix_commit":upstream,
   "patch_sha256":hashlib.sha256(Path(patch).read_bytes()).hexdigest(),"px4_binary_sha256":hashlib.sha256((root/'runtime-context/px4').read_bytes()).hexdigest(),
   "runtime_change":"replace only /opt/px4-gazebo/bin/px4","runtime_gpu_access":False,"flight_validation":"pending; standalone RED/GREEN is not flight acceptance"}
(root/'manifest.json').write_text(json.dumps(x,indent=2)+'\n')
print(json.dumps(x,indent=2))
PY
