#!/bin/bash
# Diagnose a PlutoSky 7020 / PlutoSDR setup. Read-only, safe to run any time.
#
#   ./check_setup.sh            # host side only
#   ./check_setup.sh --serial   # also try the FTDI console (needs the port free)
#
# Every line prints PASS / FAIL / WARN and the command to fix a failure.
set -uo pipefail
PASS="\033[32mPASS\033[0m"; FAIL="\033[31mFAIL\033[0m"; WARN="\033[33mWARN\033[0m"
ok()   { echo -e "  $PASS  $*"; }
bad()  { echo -e "  $FAIL  $*"; }
warn() { echo -e "  $WARN  $*"; }

SERIAL_TEST=0
[[ "${1:-}" == "--serial" ]] && SERIAL_TEST=1

echo "== host packages =="
for b in iio_info iio_attr iio_readdev SoapySDRUtil gqrx; do
  if command -v "$b" >/dev/null; then ok "$b -> $(command -v $b)"; else bad "$b missing (sudo ./setup_host.sh)"; fi
done
/usr/bin/python3 - <<'PY' 2>/dev/null && ok "python numpy + matplotlib" || bad "python numpy/matplotlib missing"
import numpy, matplotlib
PY

echo "== groups =="
for g in plugdev dialout; do
  if id -nG | tr ' ' '\n' | grep -qx "$g"; then ok "member of $g"; else warn "not in $g (sudo usermod -aG $g $USER; re-login)"; fi
done

echo "== udev rules =="
if [[ -f /etc/udev/rules.d/99-plutosdr.rules ]]; then ok "99-plutosdr.rules installed"; else bad "udev rules missing (sudo cp udev/99-plutosdr.rules /etc/udev/rules.d/)"; fi

echo "== USB devices =="
if lsusb -d 0456:b673 >/dev/null 2>&1; then
  ok "PlutoSDR gadget present: $(lsusb -d 0456:b673 | sed 's/.*ID //')"
else
  bad "no 0456:b673 PlutoSDR on USB"
  if lsusb -d 0403:6010 >/dev/null 2>&1; then
    warn "only the DEBUG interface (0403:6010) is connected -> console works, no sample stream"
    warn "plug a data cable into the board's USB (OTG) port as well"
  else
    warn "no board USB interface at all: check power and the cable"
  fi
fi

echo "== IIO contexts =="
if command -v iio_info >/dev/null; then
  ctx=$(iio_info -s 2>/dev/null)
  echo "$ctx" | sed 's/^/      /'
  echo "$ctx" | grep -q "usb" && ok "USB IIO context found" || warn "no USB context (needs the OTG port)"
  echo "$ctx" | grep -q "192.168.2.1" && ok "network context at 192.168.2.1" || true
fi

echo "== SoapySDR =="
if SoapySDRUtil --info 2>/dev/null | grep -q plutosdr; then ok "plutosdr driver registered"; else bad "SoapyPlutoSDR not installed (sudo ./setup_host.sh)"; fi
SoapySDRUtil --probe="driver=plutosdr" >/dev/null 2>&1 && ok "probe found a device" || warn "probe found no device (expected until USB is connected)"

echo "== serial console =="
if ls /dev/ttyUSB1 >/dev/null 2>&1; then
  ok "/dev/ttyUSB1 exists ($(stat -c '%A %U:%G' /dev/ttyUSB1))"
  if [[ $SERIAL_TEST -eq 1 ]]; then
    /usr/bin/python3 "$(dirname "$0")/console.py" "cat /proc/device-tree/model; echo; uname -r" \
      && ok "console login works (root/analog)" || bad "console login failed"
  else
    warn "login not tested (run with --serial)"
  fi
else
  bad "/dev/ttyUSB1 missing: FTDI DEBUG port not plugged in"
fi

echo
echo "Next step: if the USB gadget is missing, move (or add) a cable to the"
echo "board's USB port. Then:  ./scripts/capture.py --find"
