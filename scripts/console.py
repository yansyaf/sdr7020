#!/usr/bin/env python3
"""Run shell commands on the board through its FTDI serial console.

The on-board FT2232H exposes two ports:
    /dev/ttyUSB1  -> Zynq PS UART (115200 8N1) <- the console
    /dev/ttyUSB0  -> JTAG (MPSSE, not a UART)

Login is ``root`` / ``analog`` on the stock PlutoSDR v0.38 firmware.

Examples
--------
    ./console.py "uname -a" "iio_info -s" "cat /proc/device-tree/model"
    ./console.py "iio_attr -c ad9361-phy voltage0 rssi"
    PLUTO_TTY=/dev/ttyUSB1 ./console.py "ip -br a"
"""
import os
import random
import re
import sys
import time

import serial

TTY = os.environ.get("PLUTO_TTY", "/dev/ttyUSB1")
BAUD = int(os.environ.get("PLUTO_BAUD", "115200"))
PASSWORD = os.environ.get("PLUTO_PASS", "analog")
TIMEOUT = float(os.environ.get("CMD_TIMEOUT", "30"))

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


def read_until(s, needles, timeout):
    buf, t0 = b"", time.time()
    while time.time() - t0 < timeout:
        d = s.read(4096)
        if d:
            buf += d
            for n in needles:
                if n in buf:
                    return buf, n
    return buf, None


def login(s):
    s.write(b"\r\n")
    buf, m = read_until(s, [b"login:", b"Password:", b"#", b"$"], 20)
    if m == b"Password:":
        s.write(PASSWORD.encode() + b"\r\n")
        buf2, m2 = read_until(s, [b"#", b"$", b"Login incorrect"], 20)
        return buf + buf2, m2
    if m in (b"#", b"$"):
        return buf, m
    s.write(b"root\r\n")
    buf2, m2 = read_until(s, [b"Password:", b"#", b"$"], 20)
    buf += buf2
    if m2 == b"Password:":
        s.write(PASSWORD.encode() + b"\r\n")
        buf3, m3 = read_until(s, [b"#", b"$", b"Login incorrect"], 20)
        return buf + buf3, m3
    return buf, m2


def main():
    cmds = sys.argv[1:]
    if not cmds:
        print(__doc__)
        return 1
    s = serial.Serial(TTY, BAUD, timeout=0.3)
    s.reset_input_buffer()
    out, m = login(s)
    if m not in (b"#", b"$"):
        print("login failed:", ANSI.sub(b"", out).decode("utf-8", "replace")[-300:])
        return 1
    print("### logged in to %s as root" % TTY)
    for c in cmds:
        code = "%04d" % random.randint(1000, 9999)
        mark = ("Zn" + code).encode()
        s.write((c + " ; Z=" + code + " ; echo RC=$? ; echo Zn$Z$Z").encode() + b"\r\n")
        buf, _ = read_until(s, [mark], TIMEOUT)
        txt = ANSI.sub(b"", buf).decode("utf-8", "replace").replace("\r", "\n")
        keep = [l for l in txt.split("\n")
                if "Zn$Z$Z" not in l and ("Z=" + code) not in l]
        print("$ " + c)
        print("\n".join(keep).replace("Zn" + code, "").strip())
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
