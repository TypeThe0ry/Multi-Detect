#!/usr/bin/env bash
# Rebuild and package PX4 without fetching dependencies or launching SITL.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
base=multidetect/px4-sitl-gazebo:gz-rendering-830-r1
source_root=${PX4_SOURCE_ROOT:-/data/scratch/px4-gimbal-lock-20261110}
microcdr=${PX4_MICROCDR_ROOT:-/data/scratch/px4-microcdr-pin-20261110.git}
toolchain=sha256:d244695ce8a359550c11d4ec422d7ab6fc33adae05112f92c4dfba89235acf7e
commit=83c4f4e580558816f0e398918f66358988004696
micro_commit=3d1b17703c7cf4f22def2910bc845bdb5152d7b5
micro_sha=7bcc39639741cd85345f76c4d0e2a1b3e96e5862af4da10d478aac3a44a88722
jobs=${PX4_BUILD_JOBS:-8}
out=
tag=
dry_run=0
compile_check=0

die() { printf '%s\n' "$*" >&2; exit 2; }
usage() {
    printf '%s\n' "Usage: $0 [--from LOCAL_BASE_TAG] [--tag NEW_TAG] [--source PINNED_CHECKOUT]" \
        '       [--microcdr LOCAL_GIT_REPO] [--out NEW_BUILD_DIR] [--dry-run]' \
        "       $0 --compile-check --out EXISTING_BUILD_DIR [--dry-run]" \
        'Refresh the recorded --out/src bridge files; compile only GZGimbal.cpp.o and prerequisites.' \
        'Writes --out/compile-check.log and compile-check.diff; never packages or tags.' \
        'Native caches use local CMake; /work caches use the pinned offline toolchain container.' \
        'Default base: multidetect/px4-sitl-gazebo:gz-rendering-830-r1' \
        'Default output tag: <base-tag>-gimbal-horizon2. No existing tag is replaced.'
}
while (($#)); do
    case "$1" in
        --compile-check) compile_check=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        --help|-h) usage; exit 0 ;;
        --from|--tag|--source|--microcdr|--out)
            [[ $# -ge 2 && -n $2 && $2 != --* ]] || die "Missing argument for $1"
            case "$1" in
                --from) base=$2 ;;
                --tag) tag=$2 ;;
                --source) source_root=$2 ;;
                --microcdr) microcdr=$2 ;;
                --out) out=$2 ;;
            esac
            shift 2 ;;
        *) die "Unknown option: $1" ;;
    esac
done
[[ $jobs =~ ^([1-9]|1[0-2])$ ]] || die 'PX4_BUILD_JOBS must be 1..12'
[[ $base != sha256:* && $base != *@* && $base != -* && ${base##*/} == *:* ]] || die '--from requires a local repository:tag'
tag=${tag:-$base-gimbal-horizon2}
[[ $tag != "$base" && $tag != -* && $tag != sha256:* && $tag != *@* && ${tag##*/} == *:* ]] ||
    die '--tag must be a different repository:tag'
(( ! compile_check )) || [[ -n $out ]] || die '--compile-check requires --out EXISTING_BUILD_DIR'
out=${out:-/data/scratch/px4-gimbal-horizon-$(date -u +%Y%m%dT%H%M%S)-$$}
lockstep="$repo/simulation/px4/patches/lockstep-lifetime-r1.patch"
horizon="$repo/simulation/px4/patches/gz-gimbal-horizon-stabilization.patch"
dockerfile="$repo/simulation/px4/clock-build/Dockerfile.gimbal-horizon"

if ((compile_check)); then
    bridge=src/modules/simulation/gz_bridge
    target=$bridge/CMakeFiles/modules__simulation__gz_bridge.dir/GZGimbal.cpp.o
    printf 'compile-check: output=%s target=%s\n' "$out" "$target"
    sha256sum "$horizon"
    if ((dry_run)); then
        echo 'Dry run: refresh the recorded bridge sources, build only the object and prerequisites. No Docker calls or writes.'
        exit 0
    fi

    out=$(realpath -e "$out")
    src=$out/src
    build=$out/build
    cache=$build/CMakeCache.txt
    [[ -d $src/.git && -f $out/applied.diff && -f $build/build.ninja && -f $cache ]] ||
        die 'Expected an existing --out with src, applied.diff and a configured Ninja build'
    [[ $(git -C "$src" rev-parse HEAD) == "$commit" ]] || die 'Wrong PX4 build tree revision'
    grep -Fxq 'CMAKE_GENERATOR:INTERNAL=Ninja' "$cache" || die 'Expected Ninja cache'
    if grep -Fxq "CMAKE_HOME_DIRECTORY:INTERNAL=$src" "$cache" && grep -Fxq "CMAKE_CACHEFILE_DIR:INTERNAL=$build" "$cache"; then
        command=(cmake --build "$build")
    elif grep -Fxq 'CMAKE_HOME_DIRECTORY:INTERNAL=/work/src' "$cache" && grep -Fxq 'CMAKE_CACHEFILE_DIR:INTERNAL=/work/build' "$cache"; then
        command=(docker run --rm --pull=never --network none --cpus="$jobs" --memory=64g --memory-swap=64g
                 --mount "type=bind,src=$out,dst=/work" --workdir /work/src --env GZ_DISTRO=harmonic
                 --entrypoint cmake "$toolchain" --build /work/build)
    else
        die 'Cache paths match neither this host tree nor the original /work mount'
    fi

    # The failed build still contains the previous patch. Refresh only the two
    # bridge files, and never overwrite unrecorded edits or the original build receipt.
    stage=$(mktemp -d)
    trap 'rm -rf -- "$stage"' EXIT
    git -C "$src" diff --binary HEAD > "$stage/before.diff"
    cmp -s "$stage/before.diff" "$out/applied.diff" || cmp -s "$stage/before.diff" "$out/compile-check.diff" ||
        die 'Unrecorded tracked edits in --out/src; refusing to replace bridge sources'
    files=("$bridge/GZGimbal.cpp" "$bridge/GZGimbal.hpp")
    git -C "$src" archive "$commit" -- "${files[@]}" | tar -x -C "$stage"
    (cd "$stage" && git init -q && git apply "$horizon")
    for file in "${files[@]}"; do
        cmp -s "$stage/$file" "$src/$file" || cp -- "$stage/$file" "$src/$file"
    done
    git -C "$src" diff --binary HEAD > "$out/compile-check.diff"
    "${command[@]}" --target "$target" -j"$jobs" 2>&1 | tee "$out/compile-check.log"
    exit 0
fi

printf 'base=%s\ntag=%s\npx4_commit=%s\nsource=%s\ntoolchain=%s\nmicrocdr=%s\noutput=%s\n' \
    "$base" "$tag" "$commit" "$source_root" "$toolchain" "$microcdr" "$out"
sha256sum "$lockstep" "$horizon" "$dockerfile"
if ((dry_run)); then
    printf '%s\n' 'Dry run: no Docker calls or file writes.' \
        'Plan: verify local inputs, copy PX4 sources, apply both patches, compile px4_sitl_default/bin/px4.' \
        'Package with local classic Docker builder; verify image, binary and runtime linkage, then tag.' \
        'Build-time container network: none. Runtime bridge stabilization defaults to off.'
    exit 0
fi

source_root=$(realpath -e "$source_root")
microcdr=$(realpath -e "$microcdr")
out=$(realpath -m "$out")
[[ $out == /data/* && $out != "$source_root" && $out != "$source_root/"* && ! -e $out ]] ||
    die 'Use a fresh --out under /data outside the PX4 source tree'
[[ -d $source_root/.git ]] || die 'A standalone PX4 checkout with its Git metadata is required'
[[ $(git -C "$source_root" rev-parse HEAD) == "$commit" ]] || die 'Wrong PX4 source revision'
[[ -z $(git --no-optional-locks -C "$source_root" status --porcelain --untracked-files=no --ignore-submodules=none) ]] ||
    die 'Use a pristine PX4 checkout with clean submodules'
submodules=$(git -C "$source_root" submodule status --recursive)
[[ -n $submodules && ! $submodules =~ (^|$'\n')[-+U] ]] || die 'Initialize all pinned PX4 submodules offline first'
[[ $(git -C "$microcdr" rev-parse 'refs/tags/v2.0.1^{commit}') == "$micro_commit" ]] ||
    die 'Wrong Micro-CDR v2.0.1 revision'
[[ $(git -C "$microcdr" archive "$micro_commit" | sha256sum | cut -d' ' -f1) == "$micro_sha" ]] ||
    die 'Wrong Micro-CDR source hash'
(
    cd "$source_root/src/modules/simulation/gz_bridge"
    sha256sum -c - <<'HASHES'
1b5ff1dfc8c5575ed9ebaf70f781dbd93571dfcabc23934714bbfa05d6534a7a  GZGimbal.cpp
5ac1e9bf6463d5c8fc221df6f93480e2b47e826e898ef727a688513eb6264e7a  GZGimbal.hpp
HASHES
)
git -C "$source_root" apply --check "$lockstep" "$horizon"
base_id=$(docker image inspect "$base" --format '{{.Id}}')
case "$base" in
    multidetect/px4-sitl-gazebo:gz-rendering-830-r1)
        [[ $base_id == sha256:f35793a1bd4ce32d04cf2ad505234cc566893e48980f98b984af5aee93a81288 ]] ||
            die 'Default base tag does not match the recorded image ID' ;;
esac
[[ $(docker image inspect "$toolchain" --format '{{.Id}}') == "$toolchain" ]] ||
    die 'Pinned offline PX4 toolchain is not available'
if docker image inspect "$tag" >/dev/null 2>&1; then die "Refusing to overwrite $tag"; fi

mkdir "$out"
mkdir "$out/context"
cp -a --reflink=auto "$source_root" "$out/src"
cp -a --reflink=auto "$microcdr" "$out/microcdr.git"
cp "$dockerfile" "$out/context/Dockerfile"
printf '%s\n' "$submodules" > "$out/submodules.txt"
git -C "$out/src" apply "$lockstep" "$horizon"
git -C "$out/src" diff --check
git -C "$out/src" diff --binary > "$out/applied.diff"

docker run --rm --pull=never --network none --cpus="$jobs" --memory=64g --memory-swap=64g \
    --mount "type=bind,src=$out,dst=/work" --workdir /work/src \
    --env GZ_DISTRO=harmonic --env "JOBS=$jobs" \
    --env GIT_CONFIG_COUNT=4 \
    --env GIT_CONFIG_KEY_0=url.file:///work/microcdr.git.insteadOf \
    --env GIT_CONFIG_VALUE_0=https://github.com/eProsima/Micro-CDR.git \
    --env GIT_CONFIG_KEY_1=protocol.file.allow --env GIT_CONFIG_VALUE_1=always \
    --env GIT_CONFIG_KEY_2=safe.directory --env GIT_CONFIG_VALUE_2=/work/src \
    --env GIT_CONFIG_KEY_3=protocol.http.allow --env GIT_CONFIG_VALUE_3=never \
    --entrypoint bash "$toolchain" -ec '
        cmake -S . -B /work/build -G Ninja -DCONFIG=px4_sitl_default -DCMAKE_BUILD_TYPE=RelWithDebInfo -DPYTHON_EXECUTABLE=/opt/px4-build-venv/bin/python
        cmake --build /work/build --target px4 -j"$JOBS"
        install -m 755 /work/build/bin/px4 /work/context/px4
    ' 2>&1 | tee "$out/build.log"

# Classic builder resolves FROM in the daemon's local image store.
[[ $(docker image inspect "$base" --format '{{.Id}}') == "$base_id" ]] || die 'Base tag changed before packaging'
DOCKER_BUILDKIT=0 docker build --pull=false --network=none --iidfile "$out/image-id.txt" \
    --build-arg "BASE_IMAGE=$base" "$out/context"
image_id=$(<"$out/image-id.txt")
[[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Invalid Docker image ID'
docker image inspect "$base_id" "$image_id" > "$out/images.json"
python3 - "$out/images.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as file:
    base, result = json.load(file)
layers = base["RootFS"]["Layers"]
if result["RootFS"]["Layers"][:-1] != layers:
    raise SystemExit("Output rootfs does not extend the selected base by one layer")
expected = base["Config"]
expected["Labels"] = {**(expected.get("Labels") or {}), "org.multidetect.gimbal-bridge-stabilize": "1"}
if any(result["Config"].get(key) != expected.get(key) for key in
       ("Entrypoint", "Cmd", "User", "WorkingDir", "Env", "Labels")):
    raise SystemExit("Output runtime configuration drifted")
PY
docker run --rm --pull=never --network none --entrypoint sh "$image_id" -ec '
    ldd /opt/px4-gazebo/bin/px4
    sha256sum /opt/px4-gazebo/bin/px4
' > "$out/runtime-check.txt"
if grep -q 'not found' "$out/runtime-check.txt"; then die 'Missing runtime library; see runtime-check.txt'; fi
binary_sha=$(sha256sum "$out/context/px4" | cut -d' ' -f1)
grep -Fx "$binary_sha  /opt/px4-gazebo/bin/px4" "$out/runtime-check.txt" >/dev/null ||
    die 'Packaged binary is different from the rebuilt binary'
[[ $(docker image inspect "$base" --format '{{.Id}}') == "$base_id" ]] || die 'Base tag changed during packaging'
if docker image inspect "$tag" >/dev/null 2>&1; then die "Refusing to overwrite $tag"; fi
docker image tag "$image_id" "$tag"
{
    printf 'image_tag=%s\nimage_id=%s\nbase_id=%s\ntoolchain_id=%s\npx4_commit=%s\npx4_binary_sha256=%s\n' \
        "$tag" "$image_id" "$base_id" "$toolchain" "$commit" "$binary_sha"
    sha256sum "$lockstep" "$horizon" "$out/applied.diff"
} | tee "$out/manifest.txt"
