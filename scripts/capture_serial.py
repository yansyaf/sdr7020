#!/usr/bin/env python3
"""Capture IQ samples over the board's serial console (no USB data link needed).

The PlutoSky 7020 always exposes a serial console through the on-board FTDI
FT2232H (``/dev/ttyUSB1``, 115200 8N1, login ``root`` / ``analog``).  This
script drives that console with the libiio tools that live on the board,
dumps the samples as hex and reconstructs them here.

It is slow (~4 kB/s) but it works when only the DEBUG port is plugged in, when
the USB gadget does not enumerate, or when the board is on a remote site.

Examples
--------
    ./capture_serial.py --freq 935e6 --samples 65536 --out captures/serial_935M
    ./capture_serial.py --freq 100e6 --rate 2083333 --gain 71 --samples 131072
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time

import numpy as np
import serial

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
HEXTOK = re.compile(rb"^[0-9a-fA-F]{2}$")


class PlutoSerial:
    def __init__(self, tty, baud=115200, password="analog", verbose=True):
        self.verbose = verbose
        self.s = serial.Serial(tty, baud, timeout=0.5)
        self.s.reset_input_buffer()
        if not self.login(password):
            raise RuntimeError("login failed on %s" % tty)

    # -- low level -------------------------------------------------------
    def _read_until(self, needles, timeout):
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            d = self.s.read(65536)
            if d:
                buf += d
                for n in needles:
                    if n in buf:
                        return buf, n
        return buf, None

    def login(self, password):
        self.s.write(b"\r\n")
        buf, m = self._read_until([b"login:", b"Password:", b"#", b"$"], 20)
        if m == b"Password:":
            self.s.write(password.encode() + b"\r\n")
            buf2, m2 = self._read_until([b"#", b"$", b"Login incorrect"], 20)
            return m2 in (b"#", b"$")
        if m in (b"#", b"$"):
            return True
        self.s.write(b"root\r\n")
        buf2, m2 = self._read_until([b"Password:", b"#", b"$"], 20)
        if m2 == b"Password:":
            self.s.write(password.encode() + b"\r\n")
            _, m3 = self._read_until([b"#", b"$", b"Login incorrect"], 20)
            return m3 in (b"#", b"$")
        return m2 in (b"#", b"$")

    def run(self, cmd, timeout=60):
        """Run a command, return its output (echo and marker stripped)."""
        code = "%05d" % random.randint(10000, 99999)
        mark = ("Zn" + code).encode()
        self.s.write((cmd + " ; Z=" + code + " ; echo Zn$Z$Z").encode() + b"\r\n")
        buf, m = self._read_until([mark], timeout)
        if m is None:
            raise RuntimeError("timeout on: %s" % cmd[:70])
        txt = ANSI.sub(b"", buf).decode("utf-8", "replace")
        keep = [l for l in txt.replace("\r", "\n").split("\n")
                if "Zn$Z$Z" not in l and ("Z=" + code) not in l]
        return "\n".join(keep).replace("Zn" + code, "").strip()

    # -- board operations ------------------------------------------------
    def configure(self, freq, rate, bw, gain, antenna):
        for c in ("iio_attr -c ad9361-phy voltage0 gain_control_mode manual",
                  "iio_attr -c ad9361-phy voltage0 hardwaregain %d" % gain,
                  "iio_attr -c ad9361-phy voltage0 sampling_frequency %d" % int(rate),
                  "iio_attr -c ad9361-phy voltage0 rf_bandwidth %d" % int(bw),
                  "iio_attr -c ad9361-phy voltage0 rf_port_select %s" % antenna,
                  "iio_attr -c ad9361-phy altvoltage0 frequency %d" % int(freq)):
            self.run(c, 30)
        time.sleep(0.4)
        out = self.run("iio_attr -c ad9361-phy altvoltage0 frequency", 30)
        vals = re.findall(r"value\s*[:']?\s*'?(-?\d+)", out)
        if not vals:
            vals = re.findall(r"\b(\d{6,})\b", out)
        if vals:
            got = int(vals[-1])
            if self.verbose:
                print("   LO readback: %d Hz" % got)
            if abs(got - int(freq)) > 1000:
                raise RuntimeError("tuning failed: asked %d Hz, LO is at %d Hz "
                                   "(board may need the -no-tuning firmware)" % (freq, got))

    def capture(self, nsamples):
        block = min(262144, max(4096, nsamples // 4))
        self.run("iio_readdev -b %d -s %d cf-ad9361-lpc voltage0 voltage1 > /tmp/rx.bin"
                 % (block, nsamples), 120)
        md5 = self.run("md5sum /tmp/rx.bin", 30).split()[0]

        code = "%05d" % random.randint(10000, 99999)
        beg = ("BEGn%s" % code).encode()
        end = ("ENDn%s" % code).encode()
        self.s.reset_input_buffer()
        time.sleep(0.2)
        self.s.reset_input_buffer()
        self.s.write(("Z=%s; echo BEGn$Z$Z; od -An -tx1 -v /tmp/rx.bin; echo ENDn$Z$Z\r\n"
                      % code).encode())
        buf, m = self._read_until([beg], 90)
        if m is None:
            raise RuntimeError("no BEGIN marker")
        payload = buf.split(beg, 1)[1]
        est = nsamples * 8 / 9500.0
        deadline = time.time() + 60 + est * 2
        while end not in payload and time.time() < deadline:
            d = self.s.read(65536)
            if d:
                payload += d
        if end not in payload:
            raise RuntimeError("truncated transfer (%d bytes)" % len(payload))
        payload = payload.split(end)[0]
        toks = [t for t in re.split(rb"[^0-9a-fA-F]+", payload) if HEXTOK.match(t)]
        raw = bytes.fromhex(b"".join(toks).decode())
        if len(raw) != nsamples * 4:
            raise RuntimeError("size mismatch: %d != %d" % (len(raw), nsamples * 4))
        if md5 and hashlib.md5(raw).hexdigest() != md5:
            raise RuntimeError("md5 mismatch (line noise?)")
        return np.frombuffer(raw, dtype=np.int16)

    def close(self):
        self.s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tty", default="/dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--password", default="analog")
    ap.add_argument("--freq", type=float, required=True)
    ap.add_argument("--rate", type=float, default=2083333)
    ap.add_argument("--bw", type=float, default=2e6)
    ap.add_argument("--gain", type=int, default=71)
    ap.add_argument("--samples", type=int, default=65536)
    ap.add_argument("--antenna", default="A_BALANCED")
    ap.add_argument("--out", required=True)
    ap.add_argument("--retries", type=int, default=2)
    a = ap.parse_args()

    p = PlutoSerial(a.tty, a.baud, a.password)
    p.run("echo 1 > /proc/sys/kernel/printk", 20)      # silence kernel console
    p.configure(a.freq, a.rate, a.bw, a.gain, a.antenna)

    d = None
    for attempt in range(a.retries + 1):
        try:
            d = p.capture(a.samples)
            break
        except Exception as e:
            print("   attempt %d failed: %s" % (attempt + 1, e))
            time.sleep(2)
    p.close()
    if d is None:
        return 1

    iq = (d[0::2].astype(np.float32) + 1j * d[1::2].astype(np.float32)) / 2 ** 11
    out = a.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    np.save(out + ".npy", iq.astype(np.complex64))
    d.tofile(out + ".raw")
    json.dump({"freq": a.freq, "rate": a.rate, "bw": a.bw, "gain": a.gain,
               "samples": int(len(iq)), "backend": "serial-console",
               "tty": a.tty, "when": time.strftime("%Y-%m-%dT%H:%M:%S")},
              open(out + ".json", "w"), indent=2)
    print("saved %s.npy (%d samples)" % (out, len(iq)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
