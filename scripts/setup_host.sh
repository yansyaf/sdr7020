#!/bin/bash
# Install everything needed to drive an AD936x / PlutoSDR-compatible board
# (PlutoSky 7020, ADALM-PLUTO, Pluto+) from an Ubuntu 24.04 host.
#
#   sudo ./setup_host.sh
#
# What it does
#   1. libiio + iio tools (iio_info, iio_attr, iio_readdev, iiod)
#   2. libad9361 helpers
#   3. SoapySDR + the SoapyPlutoSDR driver (built from source, it is not in Ubuntu)
#   4. gqrx (GUI spectrum receiver) and the gnuradio IIO bindings
#   5. python: numpy / scipy / matplotlib / pyadi-iio
#   6. udev rules so the board is usable without root
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run me with sudo" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

echo "==> apt packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  libiio-utils libiio0 libiio-dev libad9361-0 libad9361-dev iiod \
  soapysdr-tools libsoapysdr-dev python3-soapysdr \
  gqrx-sdr gnuradio libgnuradio-iio3.10.9t64 \
  python3-numpy python3-scipy python3-matplotlib python3-serial \
  cmake build-essential pkg-config git curl

echo "==> udev rules"
cp "$HERE/../udev/99-plutosdr.rules" /etc/udev/rules.d/99-plutosdr.rules
udevadm control --reload-rules
udevadm trigger

echo "==> groups for $TARGET_USER"
usermod -aG plugdev,dialout "$TARGET_USER"

echo "==> SoapyPlutoSDR"
if [[ ! -d /tmp/SoapyPlutoSDR ]]; then
  git clone --depth 1 https://github.com/pothosware/SoapyPlutoSDR.git /tmp/SoapyPlutoSDR
fi
cmake -S /tmp/SoapyPlutoSDR -B /tmp/SoapyPlutoSDR/build -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/SoapyPlutoSDR/build -j"$(nproc)"
cmake --install /tmp/SoapyPlutoSDR/build
# Ubuntu's SoapySDR looks in /usr/local/lib/<triplet>/SoapySDR/modules0.8
mkdir -p /usr/local/lib/x86_64-linux-gnu/SoapySDR/modules0.8
cp -f /usr/local/lib/SoapySDR/modules0.8/libPlutoSDRSupport.so \
      /usr/local/lib/x86_64-linux-gnu/SoapySDR/modules0.8/ 2>/dev/null || true
ldconfig
SoapySDRUtil --info 2>/dev/null | grep -q plutosdr && echo "   plutosdr driver registered"

echo "==> python packages for $TARGET_USER"
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip
# libiio 0.25 in Ubuntu 24.04 -> pyadi-iio must be <= 0.0.14
sudo -u "$TARGET_USER" python3 -m pip install --user --break-system-packages \
  "pyadi-iio==0.0.14" pyserial 2>/dev/null || \
  echo "   (pyadi-iio install skipped; capture.py falls back to the libiio CLI)"

echo "==> verify"
iio_info --version 2>/dev/null | head -1 || true
SoapySDRUtil --info | grep -A3 "Available factories" || true
echo
echo "Done. Plug the board in, then run:  ./scripts/capture.py --find"
