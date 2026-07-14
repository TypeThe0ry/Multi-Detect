#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/bootstrap_jetson_orin.sh --date-utc 'YYYY-MM-DD HH:MM:SS UTC'
       [--gateway 192.168.144.100] [--app-dir /home/USER/Multi-Detect]

Installs the bench-only JetPack/Python runtime. It does not enable a systemd
service, transmit MAVLink, configure arming, or create a payload interface.
The gateway and DNS changes are temporary and disappear after reboot.
EOF
}

if [[ ${EUID} -ne 0 ]]; then
  echo "run this script with sudo" >&2
  exit 2
fi

target_user=${SUDO_USER:-}
if [[ -z ${target_user} || ${target_user} == root ]]; then
  echo "SUDO_USER must identify the unprivileged Jetson account" >&2
  exit 2
fi

gateway=192.168.144.100
date_utc=
app_dir="/home/${target_user}/Multi-Detect"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date-utc)
      date_utc=${2:-}
      shift 2
      ;;
    --gateway)
      gateway=${2:-}
      shift 2
      ;;
    --app-dir)
      app_dir=${2:-}
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z ${date_utc} ]]; then
  echo "--date-utc is required because this target booted with a 1970 clock" >&2
  exit 2
fi
if [[ ! -d ${app_dir}/src/multidetect ]]; then
  echo "Multi-Detect source tree not found at ${app_dir}" >&2
  exit 2
fi
if [[ ! -r /proc/device-tree/model ]]; then
  echo "not a supported Jetson target: device-tree model is unavailable" >&2
  exit 2
fi

device_model=$(tr -d '\000' </proc/device-tree/model)
case "${device_model}" in
  *"Jetson Orin"*) ;;
  *)
    echo "unsupported target: ${device_model}" >&2
    exit 2
    ;;
esac

echo "target=${device_model}"
echo "setting the bench clock and temporary Windows NAT route"
date -u -s "${date_utc}"
ip route replace default via "${gateway}" dev eth0 metric 100
resolvectl dns eth0 1.1.1.1 8.8.8.8
resolvectl domain eth0 '~.'

if ! ping -c 1 -W 3 1.1.1.1 >/dev/null; then
  echo "internet check failed through ${gateway}; refusing partial package installation" >&2
  exit 3
fi
if ! getent hosts repo.download.nvidia.com >/dev/null; then
  echo "NVIDIA repository DNS lookup failed; refusing partial package installation" >&2
  exit 3
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  nvidia-jetpack-runtime \
  nvidia-l4t-gstreamer \
  libnvinfer-bin \
  python3-libnvinfer \
  python3-opencv \
  python3-pip \
  python3-venv

usermod -aG dialout,video "${target_user}"

venv=${app_dir}/.venv
runuser -u "${target_user}" -- python3 -m venv --system-site-packages "${venv}"
runuser -u "${target_user}" -- "${venv}/bin/python" -m pip install --upgrade \
  pip setuptools wheel
runuser -u "${target_user}" -- "${venv}/bin/python" -m pip install \
  'numpy>=1.26,<2' \
  'onnxruntime>=1.18,<1.23' \
  'pymavlink>=2.4' \
  'pyserial>=3.5' \
  'PyNaCl>=1.5' \
  'cuda-python==12.2.1'
runuser -u "${target_user}" -- "${venv}/bin/python" -m pip install --no-deps -e "${app_dir}"

runuser -u "${target_user}" -- "${venv}/bin/python" - <<'PY'
import cv2
import nacl
import numpy
import onnxruntime
import pymavlink
import serial
import tensorrt
from cuda import cudart

print("opencv", cv2.__version__)
print("numpy", numpy.__version__)
print("onnxruntime", onnxruntime.__version__, onnxruntime.get_available_providers())
print("pymavlink", getattr(pymavlink, "__version__", "installed"))
print("pyserial", serial.__version__)
print("pynacl", nacl.__version__)
print("tensorrt", tensorrt.__version__)
print("cuda_devices", cudart.cudaGetDeviceCount())
PY

if gst-inspect-1.0 nvv4l2decoder >/dev/null 2>&1; then
  echo "nvv4l2decoder=available"
else
  echo "nvv4l2decoder=unavailable" >&2
  exit 4
fi

timedatectl set-ntp true || true
echo "JETSON_BENCH_RUNTIME_READY"
echo "Log out and back in before opening a Pixhawk serial device through the dialout group."
