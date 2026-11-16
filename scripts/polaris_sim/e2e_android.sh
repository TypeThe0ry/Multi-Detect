#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST_GATEWAY="${POLARIS_SIM_HOST:-192.168.1.138}"
PACKAGE="com.multidetect.gcs"
AVD=""
APK=""
OUTPUT_ROOT="/data/scratch/polaris-sim/android"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: scripts/polaris_sim/e2e_android.sh [options]
  --apk FILE              Polaris APK; auto-detects one under ground-control/MultiDetectGCS/build
  --avd NAME              Existing x86_64 Android Virtual Device
  --host IP               Gateway host address (default 192.168.1.138)
  --package ID            Android application ID (default com.multidetect.gcs)
  --output-root DIR       Evidence directory (default /data/scratch/polaris-sim/android)
  --dry-run               Print the plan; allowed when the APK is missing
EOF
}

while (($#)); do
  case "$1" in
    --apk) APK="$2"; shift 2 ;;
    --avd) AVD="$2"; shift 2 ;;
    --host) HOST_GATEWAY="$2"; shift 2 ;;
    --package) PACKAGE="$2"; shift 2 ;;
    --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUTPUT_ROOT/$STAMP"
mkdir -p "$OUT"
REPORT="$OUT/report.md"

if [[ -z "$APK" ]]; then
  mapfile -t FOUND_APKS < <(find "$ROOT/ground-control/MultiDetectGCS" -type f -name '*.apk' \
    ! -path '*/androidTest/*' ! -path '*/test/*' 2>/dev/null | sort)
  if ((${#FOUND_APKS[@]} == 1)); then
    APK="${FOUND_APKS[0]}"
  elif ((${#FOUND_APKS[@]} > 1)); then
    echo "Multiple APKs found; specify --apk:" >&2
    printf '  %s\n' "${FOUND_APKS[@]}" >&2
    exit 2
  fi
fi

if [[ -n "$APK" && ! -f "$APK" ]]; then
  echo "APK does not exist: $APK" >&2
  exit 2
fi

if ((DRY_RUN)); then
  {
    echo "# Polaris Android E2E"
    echo
    if [[ -z "$APK" ]]; then
      echo "APK: 未验证，未找到 Polaris APK；构建后通过 --apk 指定。"
    else
      echo "APK: \`$APK\`"
    fi
    echo "AVD: \`${AVD:-first configured x86_64 AVD}\`"
    echo "Gateway: \`127.0.0.1\` via adb reverse; service host \`$HOST_GATEWAY\` (\`10.0.2.2\` is the direct emulator alias)."
    echo "Actions: start owned x86_64 emulator; adb reverse TCP 5770/8780/8554/8790; install APK; open the polaris-sim:// URI; capture screenshots and logcat."
    echo "No emulator, APK install, adb reverse, or gateway request was performed."
  } >"$REPORT"
  echo "DRY RUN: APK ${APK:-missing}; report: $REPORT"
  exit 0
fi

if [[ -z "$APK" ]]; then
  echo "Polaris APK not found. Build the Android APK, then rerun with --apk FILE (or use --dry-run)." >&2
  exit 2
fi

ADB="$(command -v adb || true)"
EMULATOR="$(command -v emulator || true)"
if [[ -z "$ADB" || -z "$EMULATOR" ]]; then
  echo "adb and emulator must be available in PATH (set ANDROID_SDK_ROOT/PATH)." >&2
  exit 2
fi

python - "$HOST_GATEWAY" <<'PY'
import socket
import sys
from urllib.request import urlopen

host = sys.argv[1]
for port, path in ((8780, "/health"), (8790, "/health")):
    with urlopen(f"http://{host}:{port}{path}", timeout=3) as response:
        if response.status != 200:
            raise SystemExit(f"gateway health check failed: {host}:{port}{path} -> {response.status}")
    print(f"PASS HTTP {host}:{port}{path}")
for port in (5770, 8554):
    with socket.create_connection((host, port), timeout=3):
        print(f"PASS TCP {host}:{port}")
PY

AVD_HOME="${ANDROID_AVD_HOME:-}"
if [[ -z "$AVD_HOME" && -n "${ANDROID_SDK_HOME:-}" ]]; then
  AVD_HOME="$ANDROID_SDK_HOME/.android/avd"
fi
if [[ -z "$AVD_HOME" ]]; then
  USER_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
  AVD_HOME="$USER_HOME/.android/avd"
fi
if [[ -z "$AVD" ]]; then
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    config="$AVD_HOME/$candidate.avd/config.ini"
    if [[ -f "$config" ]] && rg -q '^abi\.type=x86_64$' "$config"; then
      AVD="$candidate"
      break
    fi
  done < <("$EMULATOR" -list-avds)
fi
if [[ -z "$AVD" ]]; then
  echo "No x86_64 AVD found under $AVD_HOME. Create one with Android Studio/avdmanager, or use --dry-run." >&2
  exit 2
fi
AVD_CONFIG="$AVD_HOME/$AVD.avd/config.ini"
if [[ ! -f "$AVD_CONFIG" ]] || ! rg -q '^abi\.type=x86_64$' "$AVD_CONFIG"; then
  echo "AVD '$AVD' is not configured as x86_64: $AVD_CONFIG" >&2
  exit 2
fi

SERIAL=""
PORT=5554
while ((PORT <= 5582)); do
  candidate="emulator-$PORT"
  if ! "$ADB" devices | awk 'NR > 1 {print $1}' | rg -qx "$candidate"; then
    SERIAL="$candidate"
    break
  fi
  PORT=$((PORT + 2))
done
if [[ -z "$SERIAL" ]]; then
  echo "No free emulator console port between 5554 and 5582." >&2
  exit 2
fi

EMULATOR_PID=""
REVERSE_ADDED=()
REPORT_WRITTEN=0
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "$SERIAL" ]]; then
    for rule in "${REVERSE_ADDED[@]}"; do
      "$ADB" -s "$SERIAL" reverse --remove "$rule" >/dev/null 2>&1 || true
    done
    "$ADB" -s "$SERIAL" emu kill >/dev/null 2>&1 || true
  fi
  if [[ -n "$EMULATOR_PID" ]] && kill -0 "$EMULATOR_PID" 2>/dev/null; then
    cmdline="$(tr '\0' ' ' <"/proc/$EMULATOR_PID/cmdline" 2>/dev/null || true)"
    if [[ "$cmdline" == *"-avd $AVD"* ]]; then
      kill -TERM "$EMULATOR_PID" 2>/dev/null || true
      wait "$EMULATOR_PID" 2>/dev/null || true
    fi
  fi
  if ((REPORT_WRITTEN == 0)); then
    {
      echo "# Polaris Android E2E"
      echo
      echo "- 结果：**FAIL** (exit $rc)"
      echo "- APK：\`${APK:-未找到}\`"
      echo "- AVD：\`${AVD:-未选择}\`"
      echo "- 日志：\`$OUT\`"
    } >"$REPORT"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

TASKSET=()
if command -v taskset >/dev/null 2>&1 && (( $(nproc --all) > 15 )); then
  TASKSET=(taskset -c 12-15)
fi
"${TASKSET[@]}" "$EMULATOR" -avd "$AVD" -port "$PORT" -no-snapshot-save -no-boot-anim \
  -no-window -gpu swiftshader_indirect -cores 2 -memory 4096 >"$OUT/emulator.log" 2>&1 &
EMULATOR_PID=$!
printf '%s\n' "$EMULATOR_PID" >"$OUT/emulator.pid"
"$ADB" start-server >/dev/null
"$ADB" -s "$SERIAL" wait-for-device

BOOTED=0
for _ in $(seq 1 120); do
  if [[ "$("$ADB" -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == "1" ]]; then
    BOOTED=1
    break
  fi
  sleep 2
done
if ((BOOTED == 0)); then
  echo "Android emulator did not finish booting; log: $OUT/emulator.log" >&2
  exit 1
fi

EXISTING_REVERSE="$("$ADB" -s "$SERIAL" reverse --list 2>/dev/null || true)"
for port in 5770 8780 8554 8790; do
  rule="tcp:$port"
  if ! printf '%s\n' "$EXISTING_REVERSE" | rg -q "\b$rule\b"; then
    "$ADB" -s "$SERIAL" reverse "$rule" "$rule"
    REVERSE_ADDED+=("$rule")
  fi
done

"$ADB" -s "$SERIAL" install -r "$APK" | tee "$OUT/install.log"
"$ADB" -s "$SERIAL" shell monkey -p "$PACKAGE" 1 >"$OUT/launch.log" 2>&1
sleep 8
SIM_URI="polaris-sim://127.0.0.1?mavlink=tcp:127.0.0.1:5770&http=127.0.0.1:8780&rtsp=127.0.0.1:8554&ai=127.0.0.1:8790"
"$ADB" -s "$SERIAL" shell am start -a android.intent.action.VIEW \
  -d "$SIM_URI" -p "$PACKAGE" >"$OUT/connect-intent.log" 2>&1
sleep 5
APP_PID="$("$ADB" -s "$SERIAL" shell pidof -s "$PACKAGE" 2>/dev/null | tr -d '\r')"
if [[ ! "$APP_PID" =~ ^[0-9]+$ ]]; then
  echo "Could not find the Polaris application process for $PACKAGE; log: $OUT/launch.log" >&2
  exit 1
fi
"$ADB" -s "$SERIAL" exec-out screencap -p >"$OUT/screen-1.png"
sleep 3
"$ADB" -s "$SERIAL" exec-out screencap -p >"$OUT/screen-2.png"
"$ADB" -s "$SERIAL" logcat -d -t 20000 --pid "$APP_PID" >"$OUT/logcat.txt"

python - "$OUT/screen-1.png" "$OUT/screen-2.png" <<'PY'
import struct
import sys
from pathlib import Path

for name in sys.argv[1:]:
    data = Path(name).read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"invalid or empty PNG screenshot: {name}")
    width, height = struct.unpack(">II", data[16:24])
    if width < 320 or height < 240:
        raise SystemExit(f"unexpected screenshot size {width}x{height}: {name}")
    print(f"{name}: {width}x{height}, {len(data)} bytes")
PY

VIDEO_REGEX="${POLARIS_SIM_VIDEO_LOG_REGEX:-(video.*(frame|stream)|gst.*(frame|playing)|rtsp.*(frame|connected))}"
TELEMETRY_REGEX="${POLARIS_SIM_TELEMETRY_LOG_REGEX:-(mavlink.*heartbeat|heartbeat.*mavlink|vehicle.*connected)}"
if ! grep -Ei "$VIDEO_REGEX" "$OUT/logcat.txt" >/dev/null; then
  echo "No video-frame/RTSP activity found in logcat; set POLARIS_SIM_VIDEO_LOG_REGEX to the Polaris lane's log marker. Log: $OUT/logcat.txt" >&2
  exit 1
fi
if ! grep -Ei "$TELEMETRY_REGEX" "$OUT/logcat.txt" >/dev/null; then
  echo "No MAVLink heartbeat/vehicle-connected activity found in logcat; set POLARIS_SIM_TELEMETRY_LOG_REGEX to the Polaris lane's log marker. Log: $OUT/logcat.txt" >&2
  exit 1
fi
HASH1="$(sha256sum "$OUT/screen-1.png" | awk '{print $1}')"
HASH2="$(sha256sum "$OUT/screen-2.png" | awk '{print $1}')"
if [[ "$HASH1" == "$HASH2" ]]; then
  echo "The Polaris screen did not change during the 3-second video observation window." >&2
  exit 1
fi

{
  echo "# Polaris Android E2E"
  echo
  echo "- 结果：**PASS**"
  echo "- APK：\`$APK\`"
  echo "- AVD：\`$AVD\` (x86_64), emulator PID \`$EMULATOR_PID\`"
  echo "- Gateway route: \`127.0.0.1\` via adb reverse for TCP 5770/8780/8554/8790"
  echo "- Telemetry and video: logcat markers matched; two changing PNG screenshots captured."
  echo "- Evidence: \`$OUT\`"
} >"$REPORT"
REPORT_WRITTEN=1
echo "PASS: Android emulator screenshot/logcat checks; evidence: $OUT"
