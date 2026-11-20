#!/usr/bin/env bash
# Exercise the X1 registry row with the keeper and fake host tools.
set -euo pipefail
keeper=${1:-/data/projects/Multi-Detect-env/runs/keepalive}
registry=${2:-$keeper/registry.tsv}
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/x1-keeper.XXXXXX")
trap 'rm -r -- "$test_dir"' EXIT
mkdir "$test_dir/state" "$test_dir/bin"
cp "$keeper/keepalive.sh" "$keeper/checks.sh" "$test_dir/"
awk -F '\t' '$1 == "md-x1-resume" { print }' "$registry" | \
    sed -E 's@/usr/bin/python3 -B [^[:space:]]+/x1_resume\.py@mock-x1@g' > "$test_dir/registry.tsv"
[[ $(wc -l < "$test_dir/registry.tsv") == 1 ]]
[[ $(awk -F '\t' '{print NF}' "$test_dir/registry.tsv") == 8 ]]
cat > "$test_dir/bin/mock-x1" <<'MOCK'
#!/usr/bin/env bash
case $1 in
    preflight) [[ ${TEST_IMAGE:-0} == 1 && ${TEST_LOAD:-100} -lt 60 ]] ;;
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
expect '.status=="WAIT_RESOURCES" and .reason=="disk_below_60GiB" and .attempts==0'
TEST_IMAGE=1 TEST_LOAD=59 TEST_GPU_MEM=1024 run
expect '.status=="WAIT_RESOURCES" and .reason=="gpu3_busy_or_unknown" and .attempts==0'
TEST_IMAGE=1 TEST_LOAD=59 TEST_LAUNCH_RC=75 run
expect '.status=="WAIT_PREREQ" and .reason=="prerequisite_changed" and .attempts==0'
TEST_IMAGE=1 TEST_LOAD=59 TEST_LAUNCH_RC=2 run
expect '.status=="DOWN" and .action=="start_failed" and .attempts==1'
TEST_IMAGE=1 TEST_LOAD=59 run
expect '.status=="START_REQUESTED" and .attempts==2'
TEST_COMPLETE=1 run
expect '.status=="IDLE" and .attempts==0'
[[ $(wc -l < "$test_dir/started") == 3 ]]
printf '%s\n' 'PASS: X1 prerequisite waits, completion, live status, resource gates, race deferral and attempt counts.'
