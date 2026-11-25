#!/usr/bin/python3
"""Auto-detect the PlutoSky 7020 and immediately verify + capture a signal.

Runs as a systemd user service (pluto-watch.service). Every few seconds it
looks for the PlutoSDR USB gadget or an IIO context on the network; the first
time the board shows up it runs a full verification:

  * device/context inventory
  * fast band survey (RSSI-style power per band)
  * wide IQ capture at the best band
  * spectrum + waterfall + time-domain PNG
  * Telegram message with the picture

Results land in ~/sdr/captures/ and a state file keeps it from repeating.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
SDR = os.path.join(HOME, "sdr")
CAPS = os.path.join(SDR, "captures")
STATE = os.path.join(SDR, ".watch_state.json")
LOG = os.path.join(SDR, "watch.log")
os.makedirs(CAPS, exist_ok=True)

BANDS = [
    (88.0e6, "FM broadcast"),
    (100.0e6, "FM broadcast"),
    (121.5e6, "airband"),
    (147.0e6, "VHF"),
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


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def find_context():
    rc, out = sh("/usr/bin/iio_info -s 2>&1", 30)
    if "usb" in out.lower():
        m = re.search(r"\[usb:([^\]]+)\]", out)
        return "usb:" + (m.group(1) if m else "")
    if "192.168.2.1" in out:
        return "ip:192.168.2.1"
    return None


def usb_present():
    rc, out = sh("lsusb -d 0456:b673", 15)
    return rc == 0 and "0456:b673" in out


def survey(uri):
    """Power in each band, quick and text-only."""
    res = []
    for f, name in BANDS:
        sh("iio_attr -u %s -c ad9361-phy voltage0 gain_control_mode manual" % uri, 20)
        sh("iio_attr -u %s -c ad9361-phy voltage0 hardwaregain 71" % uri, 20)
        sh("iio_attr -u %s -c ad9361-phy voltage0 sampling_frequency 2083333" % uri, 20)
        sh("iio_attr -u %s -c ad9361-phy voltage0 rf_bandwidth 2000000" % uri, 20)
        sh("iio_attr -u %s -c ad9361-phy altvoltage0 frequency %d" % (uri, int(f)), 20)
        rc, out = sh(
            "iio_readdev -u %s -b 32768 -s 262144 cf-ad9361-lpc voltage0 voltage1 2>/dev/null "
            "| od -An -td2 -v | awk '{for(i=1;i<=NF;i++){s+=$i*$i;n++}} END{printf \"%%.0f %%d\", s, n}'"
            % uri, 90)
        try:
            sumsq, n = out.split()[:2]
            p = float(sumsq) / float(n)
        except Exception:
            p = float("nan")
        db = 10 * (p ** 0.5) / 32768.0
        log("  %-22s %7.3f MHz  meansq=%10.0f" % (name, f / 1e6, p))
        res.append({"freq": f, "name": name, "meansq": p})
    return res


def capture(uri, freq, nsamples, path):
    sh("iio_attr -u %s -c ad9361-phy altvoltage0 frequency %d" % (uri, int(freq)), 20)
    rc, out = sh("iio_readdev -u %s -b 262144 -s %d cf-ad9361-lpc voltage0 voltage1 > %s"
                 % (uri, nsamples, path), 180)
    return os.path.exists(path) and os.path.getsize(path) == nsamples * 4, out


def notify(text, image=None):
    try:
        tok = None
        for p in ("~/.hermes/config.yaml",):
            t = open(os.path.expanduser(p)).read()
            m = re.search(r"telegram:.*?token:\s*['\"]?([0-9]+:[A-Za-z0-9_-]+)", t, re.S)
            if m:
                tok = m.group(1)
                break
        if not tok:
            log("no telegram token found, skipping notify")
            return
        subprocess.run("curl -s -F chat_id=802534305 -F text='%s' "
                       "https://api.telegram.org/bot%s/sendMessage" % (text.replace("'", ""), tok),
                       shell=True, timeout=30, capture_output=True)
        if image:
            subprocess.run("curl -s -F chat_id=802534305 -F photo=@%s "
                           "https://api.telegram.org/bot%s/sendPhoto" % (image, tok),
                           shell=True, timeout=60, capture_output=True)
    except Exception as e:
        log("notify failed: %s" % e)


def verify(uri):
    log("=== device found at %s ===" % uri)
    rc, info = sh("/usr/bin/iio_info -u %s 2>&1" % uri, 60)
    open(os.path.join(CAPS, "iio_info.txt"), "w").write(info)
    log(info[:600])

    s = survey(uri)
    best = max(s, key=lambda r: (r["meansq"] if r["meansq"] == r["meansq"] else 0))
    log("best band: %s (%.3f MHz)" % (best["name"], best["freq"] / 1e6))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = os.path.join(CAPS, "cap_%s_%.1fMHz.raw" % (ts, best["freq"] / 1e6))
    ok, out = capture(uri, best["freq"], 2 * 1024 * 1024, raw)
    log("capture %s -> %s (%s)" % (ok, raw, out[-200:]))

    png = os.path.join(CAPS, "spectrum_%s.png" % ts)
    rc, out = sh("/usr/bin/python3 %s/analyze_iq.py %s %d 2083333 %s 'PlutoSky 7020 - %s'"
                 % (SDR, raw, int(best["freq"]), png, best["name"]), 180)
    log(out)

    summary = os.path.join(CAPS, "summary_%s.json" % ts)
    json.dump({"time": ts, "uri": uri, "best": best, "survey": s, "raw": raw,
               "png": png if os.path.exists(png) else None}, open(summary, "w"), indent=2)

    msg = ("PlutoSky 7020 online. Best band %s at %.1f MHz (meansq %.0f). "
           "2M-sample IQ capture saved." % (best["name"], best["freq"] / 1e6, best["meansq"]))
    notify(msg, png if os.path.exists(png) else None)
    return {"best": best, "png": png, "raw": raw}


def main():
    if os.path.exists(STATE):
        st = json.load(open(STATE))
        if st.get("done"):
            log("already verified (%s); watching for re-plug" % st.get("when"))
            if not usb_present():
                st["done"] = False
                json.dump(st, open(STATE, "w"))
    log("watch started")
    while True:
        if not os.path.exists(STATE) or not json.load(open(STATE)).get("done"):
            uri = find_context()
            if uri:
                try:
                    r = verify(uri)
                    json.dump({"done": True, "when": datetime.now().isoformat(), **r},
                              open(STATE, "w"), indent=2)
                    log("verification complete")
                except Exception as e:
                    log("verify error: %r" % e)
                    time.sleep(15)
        time.sleep(4)


if __name__ == "__main__":
    main()
