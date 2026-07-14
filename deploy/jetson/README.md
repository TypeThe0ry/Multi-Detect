# Jetson Orin NX/Nano deployment template

This directory packages the current read-only flight integration and perception service. It does
not add a flight-command or physical payload interface. The service intentionally uses the
zero-payload patrol configuration and requires a production-approved model manifest.

The L4T 36.3 / JetPack 6.0 target uses the system Python 3.10 runtime so NVIDIA's multimedia and
TensorRT Python packages remain ABI-aligned. For a first bench install, copy the repository to the
Jetson and run the auditable bootstrap script locally so the sudo password never enters a log:

```bash
cd ~/Multi-Detect
sudo bash scripts/bootstrap_jetson_orin.sh \
  --date-utc '2026-07-13 06:00:00 UTC'
```

Replace the timestamp with the current UTC time. The script configures only a temporary bench
gateway, installs the JetPack runtime, GStreamer hardware decoder, Python/OpenCV, a CPU ONNX
validation baseline and the read-only MAVLink dependency. It deliberately does not enable this
service, configure a flight mode, transmit MAVLink, or expose a payload interface. Validate the
CPU path first; qualify a JetPack-compatible native TensorRT path separately before treating GPU
evidence as available.

The aircraft service itself must use a target-built TensorRT 8.6 `.engine`/`.plan` artifact. The
direct engine adapter avoids silently treating the CPU-only ONNX Runtime wheel commonly available
on Jetson as GPU evidence. The serialized engine and its manifest are a matched pair: copy both to
the generic paths in `runtime.env`, and never reuse an engine built for another Jetson model,
TensorRT release or CUDA stack.

## Device layout

```text
/opt/multi-detect/                    application and target-compatible virtual environment
/etc/multi-detect/fire-patrol.json   deployed patrol mission configuration
/etc/multi-detect/runtime.env        RTSP and device settings, mode 0600
/var/lib/multi-detect/               TensorRT cache and alert outbox
/var/log/multi-detect/               append-only streaming audit logs
```

Create a dedicated unprivileged account and writable directories using the device's normal
administration process. Add the account only to the groups required for the selected camera and
Pixhawk serial device, commonly `video` and `dialout`. Do not run the service as root.

Copy these templates on the Jetson:

```bash
sudo install -d -o multidetect -g multidetect /etc/multi-detect
sudo install -d -o multidetect -g multidetect /var/lib/multi-detect/trt-cache
sudo install -d -o multidetect -g multidetect /var/log/multi-detect
sudo install -m 0600 deploy/jetson/runtime.env.example /etc/multi-detect/runtime.env
sudo install -m 0644 configs/missions/fire_patrol.demo.json /etc/multi-detect/fire-patrol.json
sudo install -m 0644 deploy/jetson/multi-detect.service /etc/systemd/system/multi-detect.service
```

Before enabling the service, edit `/etc/multi-detect/runtime.env`, replace the TEST-NET
`192.0.2.1` address and every credential placeholder, install runtime versions that
match the target JetPack/CUDA/TensorRT stack, and run the gates manually:

Percent-encode reserved characters in RTSP usernames/passwords. Keep the environment file owned by
root with mode `0600`, and never paste its contents into logs or issue reports. Adjust the
supplementary groups if the target baseboard uses different device ownership.

```bash
sudo /opt/multi-detect/.venv/bin/python \
  /opt/multi-detect/scripts/jetson_static_preflight.py \
  /etc/multi-detect/fire-patrol.json /etc/multi-detect/runtime.env \
  --verify-model-files --out /var/log/multi-detect/static-preflight.json

sudo -s
set -a
source /etc/multi-detect/runtime.env
set +a

runuser --preserve-environment -u multidetect -- \
  /opt/multi-detect/.venv/bin/python -m multidetect model-check \
  --onnx-model "${FIRE_MODEL_PATH}" \
  --model-manifest "${FIRE_MODEL_MANIFEST}" \
  --class-names "${FIRE_MODEL_CLASS_NAMES}" \
  --output-coordinates "${FIRE_MODEL_OUTPUT_COORDINATES}" \
  --require-production-approved

runuser --preserve-environment -u multidetect -- \
  /opt/multi-detect/.venv/bin/python -m multidetect camera-check \
  --source-env CAMERA_SOURCE --frames 120

runuser --preserve-environment -u multidetect -- \
  /opt/multi-detect/.venv/bin/python -m multidetect pixhawk-check \
  --endpoint udp:0.0.0.0:14550 --baud 921600 --samples 20 \
  --expected-system-id 1 --expected-autopilot px4 \
  --expected-vehicle-type fixed_wing --require-operational-state \
  --require-fresh-link --require-fresh-position

exit
```

The static preflight does not open RTSP, ONNX Runtime or MAVLink. It validates the patrol-only
mission boundary, redacts both RTSP credentials and the alert HMAC key, checks model class/coordinate
settings against the production-approved manifest, and validates serial/network endpoint syntax.
`FIRE_MODEL_CLASS_NAMES` and `FIRE_MODEL_OUTPUT_COORDINATES` must match the selected artifact; do not
copy values from a different export.
The candidate floor, per-class thresholds and consecutive-frame count are also explicit environment
values. Preflight rejects a class threshold below the detector floor or above the mission confirmation
threshold, and rejects a stability count weaker than `minimum_track_observations`.

An engine whose manifest is `quarantined` or has `production_approved=false` must fail the command
above and must not be used by the systemd service. It may only be exercised in an explicitly
bounded, no-display bench run without `--require-production-approved-models`; retain the audit and
prediction JSONL from that run, and keep `payload_count=0` with all payload/HIL options absent.

The Holybro Pixhawk Jetson Baseboard exposes three board-internal paths between the Jetson and
V6X: Ethernet, `UART1 <-> TELEM2`, and CAN. This aircraft currently uses the verified Ethernet
path: V6X `192.168.0.3` broadcasts MAVLink 2 to `192.168.0.255:14550`. Keep the aircraft-facing
Jetson address `192.168.144.20/24` and add `192.168.0.1/24` as a second address on `eth0`:

```bash
connection="$(nmcli -g GENERAL.CONNECTION device show eth0)"
sudo nmcli connection modify "$connection" +ipv4.addresses 192.168.0.1/24
sudo nmcli device reapply eth0
```

Because PX4 sends a subnet broadcast, the read-only receiver binds `udp:0.0.0.0:14550` rather
than one specific local address. The UART remains a supported fallback at `/dev/ttyTHS1:921600`
after PX4 TELEM2 is configured; it is not the current primary path. Pixhawk and Jetson power rails
remain separate even though all three data paths are board-internal. Compare the output with
QGroundControl before starting the service. Then review the full expanded command and sandbox:

The qualification command distinguishes a live transport from a flight-ready controller. It fails
closed unless SYSID 1 identifies PX4, reports `MAV_TYPE_FIXED_WING`, reaches `STANDBY` or `ACTIVE`,
and supplies fresh global position. `GENERIC`, `UNINIT`, a different SYSID, or a GCS/companion
heartbeat cannot satisfy the gate.

```bash
systemd-analyze verify /etc/systemd/system/multi-detect.service
sudo systemctl daemon-reload
sudo systemctl start multi-detect.service
systemctl status multi-detect.service
journalctl -u multi-detect.service -f
```

Only enable automatic startup after RTSP reconnect, model-provider selection, telemetry freshness,
thermal behavior, disk growth and controlled stop/restart have been measured on the target unit.
The service contains no `--simulate-payload-cycle`, `--auto-simulate-payload-cycle` or
`--inert-payload-hil` option and no actuator transport. Keep the patrol service separate from the
explicit localhost-only, propeller-off HIL command documented in `docs/live-camera-jetson.md`.

The service passes only the environment-variable name in its process arguments; the RTSP URI stays
in the mode-0600 environment file. Application camera errors are redacted, but OpenCV/FFmpeg is an
external logging boundary, so inspect target-device logs for credential leakage before deployment.
The alert HMAC key is handled the same way: `ExecStart` contains only the name `ALERT_HMAC_KEY`,
never its value. The supplied alert transport requires an IP-capable data link; use a separately
framed/tested adapter if the selected radio exposes only a serial byte stream.

The audit file appends across service restarts and gives each run a distinct `session_id`. Rotate or
archive it only between missions using an operations-approved procedure. The service deliberately
does not enable `--prediction-log-out`; enable prediction logging only for bounded validation runs
because it writes one record per processed frame.
