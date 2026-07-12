#!/usr/bin/env bash
set -u

section() {
  printf '\n[%s]\n' "$1"
}

safe_command() {
  local label="$1"
  shift
  printf '%s: ' "$label"
  if command -v "$1" >/dev/null 2>&1; then
    "$@" 2>&1 || true
  else
    printf 'NOT_FOUND\n'
  fi
}

section "system"
safe_command "date_utc" date -u +%Y-%m-%dT%H:%M:%SZ
safe_command "uname" uname -a
if [ -r /etc/os-release ]; then
  grep -E '^(NAME|VERSION|ID|VERSION_ID)=' /etc/os-release || true
fi
if [ -r /etc/nv_tegra_release ]; then
  printf 'nv_tegra_release: '
  head -n 1 /etc/nv_tegra_release || true
else
  printf 'nv_tegra_release: NOT_FOUND\n'
fi

section "runtime"
safe_command "python3" python3 --version
safe_command "pip3" pip3 --version
safe_command "trtexec" trtexec --version
safe_command "nvcc" nvcc --version
if command -v dpkg-query >/dev/null 2>&1; then
  dpkg-query -W -f='${Package}\t${Version}\n' \
    'nvidia-jetpack' 'nvidia-l4t-core' 'libnvinfer*' 2>/dev/null || true
fi

section "power_and_storage"
safe_command "nvpmodel" nvpmodel -q
safe_command "memory" free -h
safe_command "root_disk" df -h /

section "serial_devices"
for pattern in /dev/serial/by-id/* /dev/ttyTHS* /dev/ttyUSB* /dev/ttyACM*; do
  if [ -e "$pattern" ]; then
    ls -l "$pattern" 2>/dev/null || true
  fi
done

section "permissions"
safe_command "identity" id

section "multidetect"
if [ -f pyproject.toml ] && [ -d src/multidetect ]; then
  safe_command "camera_help" python3 -m multidetect camera-check --help
  safe_command "pixhawk_help" python3 -m multidetect pixhawk-check --help
else
  printf 'workspace: Multi-Detect repository not detected in current directory\n'
fi

printf '\nNo environment-variable values, RTSP URIs, SSH keys, or credentials were collected.\n'
