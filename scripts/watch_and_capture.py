#!/usr/bin/env python3
"""Wait for the board to appear, then survey the bands and capture the best one.

Useful when the board is plugged in by somebody else, or after a reboot:

    ./watch_and_capture.py                 # poll forever
    ./watch_and_capture.py --once          # single check, exit 0 if captured

Detection is either the USB gadget (0456:b673) or an IIO context that libiio can
reach (USB or network). The best band is chosen by mean power, then a long
capture is stored together with its plot.

Runs as a systemd user service out of the box: see docs/service.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAPS = os.path.join(ROOT, "captures")

BANDS = [
    (88.0e6, "FM broadcast"),
    (100.0e6, "FM broadcast"),
    (121.5e6, "airband"),
    (433.92e6, "ISM 433"),
    (570.0e6, "DVB-T"),
    (650.0e6, "DVB-T"),
    (806.0e6, "LTE 800 downlink"),
    (935.0e6, "GSM 900 downlink"),
    (947.0e6, "GSM 900 downlink"),
    (1842.5e6, "LTE 1800 downlink"),
    (2140.0e6, "UMTS 2100"),
    (2437.0e6, "WiFi 2.4 GHz"),
    (5180.0e6, "WiFi 5 GHz"),
]


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def context():
    rc, out = sh("iio_info -s 2>&1", 30)
    m = re.search(r"\[usb:([^\]]*)\]", out)
    if m:
        return "usb:" + m.group(1)
    if "192.168.2.1" in out:
        return "ip:192.168.2.1"
    return None


def power_at(uri, freq):
    for c in ("voltage0 gain_control_mode manual",
              "voltage0 hardwaregain 71",
              "voltage0 sampling_frequency 2083333",
              "voltage0 rf_bandwidth 2000000",
              "altvoltage0 frequency %d" % int(freq)):
        sh("iio_attr -u %s -c ad9361-phy %s" % (uri, c), 20)
    rc, out = sh("iio_readdev -u %s -b 32768 -s 131072 cf-ad9361-lpc voltage0 voltage1 "
                 "2>/dev/null | od -An -td2 -v | awk '{for(i=1;i<=NF;i++){s+=$i*$i;n++}} "
                 "END{printf \"%%.0f %%d\", s, n}'" % uri, 90)
    try:
        s, n = out.split()[:2]
        return float(s) / float(n)
    except Exception:
        return -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--samples", type=int, default=1048576)
    a = ap.parse_args()
    os.makedirs(CAPS, exist_ok=True)

    while True:
        uri = context()
        if uri:
            print("board found at %s" % uri, flush=True)
            res = [(power_at(uri, f), f, name) for f, name in BANDS]
            for p, f, name in res:
                print("  %-20s %8.2f MHz  %12.0f" % (name, f / 1e6, p), flush=True)
            best_p, best_f, best_name = max(res)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out = os.path.join(CAPS, "auto_%s_%.1fMHz" % (ts, best_f / 1e6))
            rc, o = sh("%s %s --uri %s --freq %d --samples %d --out %s"
                       % (sys.executable, os.path.join(HERE, "capture.py"),
                          uri, int(best_f), a.samples, out), 300)
            print(o, flush=True)
            json.dump({"uri": uri, "when": ts, "best": {"freq": best_f, "name": best_name},
                       "survey": [{"freq": f, "name": n, "meansq": p} for p, f, n in res]},
                      open(out + "_survey.json", "w"), indent=2)
            if a.once:
                return 0
            time.sleep(30)
        else:
            if a.once:
                print("board not found")
                return 1
            time.sleep(4)


if __name__ == "__main__":
    sys.exit(main())
