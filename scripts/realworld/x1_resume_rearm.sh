#!/usr/bin/env bash
# Run on the host after the Iowa staging fix is installed. Preserve the retry evidence.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
keeper=/data/projects/Multi-Detect-env/runs/keepalive
state=$keeper/state
attempts=$state/md-x1-resume/attempts
exec 9<>"$state/lock"
flock -n 9 || { echo 'keepalive tick is active, retry later' >&2; exit 75; }
if [[ ! -f $attempts || $(<"$attempts") != 3 ]]; then
    echo 'Iowa has no exhausted retry counter to rearm'
    exit 0
fi
live=$(systemctl list-units --type=service --state=active,activating,deactivating,reloading,refreshing \
    --no-legend --plain --no-pager md-x1-resume.service)
if [[ -n $live ]]; then
    echo 'Iowa unit is still active, retry later' >&2
    exit 75
fi
exec 8<>/run/lock/md-x1-resume.lock
flock -n 8 || { echo 'X1 work holds the shared lock, retry later' >&2; exit 75; }
/usr/bin/python3 -B "$root/scripts/realworld/x1_resume.py" preflight
archive=$state/md-x1-resume-attempts-before-world-fix-$(date -u +%Y%m%dT%H%M%S%N)
mv -n -- "$attempts" "$archive"
printf 'Rearmed Iowa via keepalive. Previous counter saved at %s\n' "$archive"
