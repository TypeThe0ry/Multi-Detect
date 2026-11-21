#!/usr/bin/env bash
# Exercise an X1 registry row with the keeper and fake host tools.
set -euo pipefail
keeper=${1:-/data/projects/Multi-Detect-env/runs/keepalive}
registry=${2:-$keeper/registry.tsv}
name=${3:-md-x1-resume}
case $name in
    md-x1-resume|md-x1-collect-next) gpu=3,1,2,0 ;;
    md-x1-worlds) gpu=- ;;
    *) exit 2 ;;
esac
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/x1-keeper.XXXXXX")
trap 'rm -r -- "$test_dir"' EXIT
mkdir "$test_dir/state" "$test_dir/bin"
cp "$keeper/keepalive.sh" "$keeper/checks.sh" "$test_dir/"
awk -F '\t' -v name="$name" -v gpu="$gpu" '$1 == name && $8 == gpu { print }' "$registry" | \
    sed -E 's@(/usr/bin/python3 -B )?[^[:space:]]+/x1_(resume\.py|collect_next\.py|world_pipeline\.sh)@mock-x1@g' > "$test_dir/registry.tsv"
[[ $(wc -l < "$test_dir/registry.tsv") == 1 ]]
[[ $(awk -F '\t' '{print NF}' "$test_dir/registry.tsv") == 8 ]]
cat > "$test_dir/bin/mock-x1" <<'MOCK'
#!/usr/bin/env bash
case $1 in
    keepalive-preflight)
        [[ $2 == --keepalive-state && -d $3 ]] || exit 9
        if [[ -f $3/world-yield ]]; then exec "$TEST_WORLD_PIPELINE" "$@"; fi
        ;&
    preflight) [[ ${TEST_IMAGE:-0} == 1 && ${TEST_LOAD:-100} -lt 60 && ${TEST_GPU_READY:-1} == 1 ]] ;;
    complete) [[ ${TEST_COMPLETE:-0} == 1 ]] ;;
    launch)
        printf 'request\n' >> started
        exit "${TEST_LAUNCH_RC:-0}"
        ;;
    *) exit 9 ;;
esac
MOCK
cat > "$test_dir/bin/df" <<'MOCK'
#!/usr/bin/env bash
printf 'Avail\n%s\n' "$(( ${TEST_GIB:-200} * 1073741824 ))"
MOCK
cat > "$test_dir/bin/nvidia-smi" <<'MOCK'
#!/usr/bin/env bash
case $1 in
    --query-gpu=*) printf '3, D, %s, 20480, 0\n' "${TEST_GPU_MEM:-2}" ;;
    --query-compute-apps=*) ;;
    *) exit 9 ;;
esac
MOCK
cat > "$test_dir/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case $1 in
    list-units) ;;
    show) printf '%s\n' "${TEST_UNIT:-inactive}" ;;
    *) exit 9 ;;
esac
MOCK
chmod 0700 "$test_dir/bin/"*
export PATH="$test_dir/bin:$PATH"
export TEST_WORLD_PIPELINE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/scripts/realworld/x1_world_pipeline.sh"
run() { bash "$test_dir/keepalive.sh"; }
expect() { jq -e ".entries[0] | $1" "$test_dir/heartbeat.json" >/dev/null; }
touch "$test_dir/state/armed"
for _ in 1 2 3 4; do run; done
expect '.status=="WAIT_PREREQ" and .attempts==0 and .action=="none"'
TEST_IMAGE=1 TEST_LOAD=60 run
expect '.status=="WAIT_PREREQ" and .attempts==0'
[[ ! -e "$test_dir/started" ]]
TEST_COMPLETE=1 run
expect '.status=="IDLE" and .attempts==0'
TEST_UNIT=active run
expect '.status=="ALIVE" and .attempts==0'
TEST_IMAGE=1 TEST_LOAD=59 TEST_GIB=59 run
floor=60
[[ $name != md-x1-worlds ]] || floor=70
expect ".status==\"WAIT_RESOURCES\" and .reason==\"disk_below_${floor}GiB\" and .attempts==0"
if [[ $gpu != - ]]; then
    TEST_IMAGE=1 TEST_LOAD=59 TEST_GPU_READY=0 run
    expect '.status=="WAIT_PREREQ" and .attempts==0'
    # A busy GPU3 does not block the launch when the X1 selector found a fallback.
    export TEST_GPU_MEM=1024
fi
TEST_IMAGE=1 TEST_LOAD=59 TEST_LAUNCH_RC=75 run
expect '.status=="WAIT_PREREQ" and .reason=="prerequisite_changed" and .attempts==0'
TEST_IMAGE=1 TEST_LOAD=59 TEST_LAUNCH_RC=2 run
expect '.status=="DOWN" and .action=="start_failed" and .attempts==1'
TEST_IMAGE=1 TEST_LOAD=59 run
expect '.status=="START_REQUESTED" and .attempts==2'
TEST_COMPLETE=1 run
expect '.status=="IDLE" and .attempts==0'
[[ $(wc -l < "$test_dir/started") == 3 ]]
printf 'PASS: %s prerequisite waits, completion, live status, resource gates, race deferral and attempt counts.\n' "$name"

if [[ $name == md-x1-worlds ]]; then
    state=$test_dir/state/md-x1-worlds
    TEST_IMAGE=1 TEST_LOAD=59 run
    printf '3\n' > "$state/attempts"
    printf '{"state":"yielded","attempt":3}\n' > "$state/world-yield"
    TEST_UNIT=active run
    expect '.status=="ALIVE" and .attempts==3'
    [[ -f $state/world-yield ]]
    TEST_IMAGE=1 TEST_LOAD=59 run
    expect '.status=="WAIT_PREREQ" and .action=="none"'
    [[ ! -e $state/world-yield && $(<"$state/attempts") == 2 ]]
    run
    expect '.status=="WAIT_PREREQ" and .attempts==2'
    TEST_IMAGE=1 TEST_LOAD=59 run
    expect '.status=="START_REQUESTED" and .attempts==3'
    TEST_COMPLETE=1 run
    expect '.status=="IDLE" and .attempts==0'
    printf 'PASS: a yielded world dispatch refunds exactly one attempt and can restart.\n'
fi
