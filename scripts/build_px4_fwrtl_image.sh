#!/usr/bin/env bash
# Offline build for the X1 ASSUMED_SIM fixed-wing RTL lane only.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
commit=53afcc82b2a51a90424ff7e2c35583e491587eb5
base=multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4
base_id=sha256:871d3f3ea016a585834ce82e2f55c3116b7bf904940db45cbd7b4155de39c55d
tag=multidetect/px4-sitl-gazebo:fwrtl-53afcc82-r1
source_root=${PX4_SOURCE_ROOT:-/root/localds/px4up/px4-main}
microcdr=${PX4_MICROCDR_ROOT:-/data/scratch/px4-microcdr-pin-20261110.git}
micro_commit=3d1b17703c7cf4f22def2910bc845bdb5152d7b5
jobs=${PX4_BUILD_JOBS:-4}
python=${PX4_VERIFY_PYTHON:-python3}
die() { printf '%s\n' "$*" >&2; exit 2; }
if [[ ${1:-} == --help ]]; then
    printf 'Usage: %s NEW_OUTPUT_UNDER_DOT_TMP sha256:REVIEWED_PX4UP_BUILD_ID\n' "$0"
    exit 0
fi
[[ $# == 2 ]] || die 'Use --help for offline build inputs'
out=$(realpath -m -- "$1")
[[ $out == "$repo/.tmp/"* && ! -e $out ]] || die 'Output must be a fresh directory under the repository .tmp/'
toolchain=$2
[[ $toolchain =~ ^sha256:[a-f0-9]{64}$ ]] || die 'Supply the reviewed full px4up-build:latest ID'
[[ $jobs =~ ^[1-8]$ ]] || die 'PX4_BUILD_JOBS must be 1..8'
[[ $(docker image inspect --format '{{.Id}}' "$base") == "$base_id" ]] || die 'Archived horizon4 ID mismatch'
[[ $(docker image inspect --format '{{.Id}}' px4up-build:latest) == "$toolchain" ]] || die 'Toolchain ID mismatch'
if docker image inspect "$tag" >/dev/null 2>&1; then die "Refusing to overwrite $tag"; fi

patch=simulation/px4/patches/gz-gimbal-horizon-53afcc82.patch
airframes=simulation/px4/airframes-main
(
    cd "$repo"
    sha256sum -c <<'HASHES'
6c0f227b7ff3b8baee39b80c1b51e83d12194c03ba317b4bd8845f266537d0c6  simulation/px4/patches/gz-gimbal-horizon-53afcc82.patch
d3622bcdfd57e65d51c393319d53add6b36afaaa53ff546dd08d039f4f4b186d  simulation/px4/airframes-main/4050_gz_delta_tailsitter
9513c93058361708da3ba156c9501d6bd42536576f76cf4682aa911de87a243f  simulation/px4/airframes-main/4051_gz_delta_tailsitter_gimbal
HASHES
)
[[ $(git -C "$source_root" rev-parse HEAD) == "$commit" && -d $source_root/.git ]] || die 'Wrong PX4 source'
[[ $(git -C "$microcdr" rev-parse 'refs/tags/v2.0.1^{commit}') == "$micro_commit" ]] || die 'Wrong Micro-CDR tag'
[[ $(git -C "$microcdr" archive "$micro_commit" | sha256sum | cut -d' ' -f1) == 7bcc39639741cd85345f76c4d0e2a1b3e96e5862af4da10d478aac3a44a88722 ]] || die 'Wrong Micro-CDR source archive'
mkdir "$out"
mkdir -p "$out/context/overlay"
# Export only tracked files from Git, not the dirty host checkout or existing build.
git clone --no-hardlinks --no-local --no-checkout -- "$source_root" "$out/src"
git -C "$out/src" checkout --detach "$commit"
git -C "$source_root" submodule status --recursive > "$out/submodules.txt"
if [[ ! -s $out/submodules.txt ]] || grep -q '^[-+U]' "$out/submodules.txt"; then
    die 'Source submodules are not initialized at their pinned commits'
fi
while read -r oid path _; do
    # submodule status uses one leading space for an exact gitlink match
    git clone --no-local --no-checkout -- "$source_root/$path" "$out/src/$path"
    git -C "$out/src/$path" checkout --detach "$oid"
done < "$out/submodules.txt"
git -C "$out/src" apply --check "$repo/$patch"
git -C "$out/src" apply "$repo/$patch"
git -C "$out/src" diff --check
git -C "$out/src" diff --binary > "$out/applied.diff"
git clone --bare --no-hardlinks --no-local -- "$microcdr" "$out/microcdr.git"

# The caller's systemd scope caps the client; also cap/nice the actual workers.
docker run --rm --pull=never --network none --cpus="$jobs" --memory=16g --memory-swap=16g \
    --mount "type=bind,src=$out,dst=/work" --workdir /work/src --entrypoint /usr/bin/nice \
    -e GZ_DISTRO=harmonic -e CCACHE_DISABLE=1 -e "JOBS=$jobs" -e HOME=/tmp \
    -e GIT_CONFIG_COUNT=3 \
    -e GIT_CONFIG_KEY_0=url.file:///work/microcdr.git.insteadOf \
    -e GIT_CONFIG_VALUE_0=https://github.com/eProsima/Micro-CDR.git \
    -e GIT_CONFIG_KEY_1=protocol.file.allow -e GIT_CONFIG_VALUE_1=always \
    -e GIT_CONFIG_KEY_2=safe.directory -e GIT_CONFIG_VALUE_2=/work/src \
    "$toolchain" -n 15 bash -ec '
        export PATH=/opt/px4-build-venv/bin:$PATH
        cmake -S . -B /work/build -G Ninja -DCONFIG=px4_sitl_default \
            -DCMAKE_BUILD_TYPE=RelWithDebInfo -DPYTHON_EXECUTABLE=/opt/px4-build-venv/bin/python
        cmake --build /work/build --target px4 -j"$JOBS"
        cmake --install /work/build --prefix /work/install
    ' 2>&1 | tee "$out/build.log"

# Replace the firmware/ROMFS, but inherit the Gazebo plugins/models/wrappers.
cp -a "$out/install/bin" "$out/install/etc" "$out/context/overlay/"
rm -f "$out/context/overlay/bin/px4-gazebo" "$out/context/overlay/bin/px4-alias.sh"
install -m 755 "$repo/$airframes"/405{0,1}_* "$out/context/overlay/etc/init.d-posix/airframes/"
cat > "$out/context/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
LABEL org.multidetect.x1-fwrtl-basis="ASSUMED_SIM" \
      org.multidetect.x1-fwrtl-px4="53afcc82b2a51a90424ff7e2c35583e491587eb5"
COPY overlay/ /opt/px4-gazebo/
DOCKERFILE
[[ $(docker image inspect --format '{{.Id}}' "$base") == "$base_id" ]] || die 'Archived tag moved'
DOCKER_BUILDKIT=0 docker build --pull=false --network=none --memory=8g --memory-swap=8g \
    --build-arg "BASE_IMAGE=$base" --iidfile "$out/image-id.unverified" "$out/context"
image=$(<"$out/image-id.unverified")
[[ $image =~ ^sha256:[a-f0-9]{64}$ && $image != "$base_id" ]] || die 'No distinct immutable image ID'
PYTHONPATH="$repo/src:$repo" "$python" "$repo/scripts/register_x1_fwrtl_sitl.py" verify \
    --mode sim --image "$image" --output "$out/image-check.json"
test "$(python3 -c 'import json, sys; print(json.load(sys.stdin)["px4_binary_sha256"])' < "$out/image-check.json")" = \
    "$(sha256sum "$out/context/overlay/bin/px4" | cut -d' ' -f1)" || die 'Packaged firmware digest differs'
[[ $(docker image inspect --format '{{.Id}}' "$base") == "$base_id" ]] || die 'Archived tag moved'
if docker image inspect "$tag" >/dev/null 2>&1; then die "Refusing to overwrite $tag"; fi
docker image tag "$image" "$tag"
[[ $(docker image inspect --format '{{.Id}}' "$tag") == "$image" ]] || die 'Candidate tag differs from verified ID'
{
    printf 'basis=ASSUMED_SIM\nimage_tag=%s\nimage_id=%s\nbase_id=%s\ntoolchain_id=%s\npx4_commit=%s\nmicrocdr_commit=%s\n' \
        "$tag" "$image" "$base_id" "$toolchain" "$commit" "$micro_commit"
    sha256sum "$out/context/overlay/bin/px4" "$out/context/Dockerfile" "$out/image-check.json" "$out/applied.diff" "$out/submodules.txt"
} | tee "$out/manifest.txt"
