#!/usr/bin/python3
"""Run shell commands on the PlutoSky board over its FTDI serial console.

Usage: pluto_console.py "cmd1" "cmd2" ...
Env: PLUTO_PASS (default analog), PLUTO_TTY (default /dev/ttyUSB1), PLUTO_BAUD,
     CMD_TIMEOUT (default 30)

Each command is followed by a runtime-built marker so that the shell's own echo
of the command line cannot match the terminator.
"""
import os, random, re, serial, sys, time

TTY = os.environ.get("PLUTO_TTY", "/dev/ttyUSB1")
BAUD = int(os.environ.get("PLUTO_BAUD", "115200"))
PASS = os.environ.get("PLUTO_PASS", "analog")
TMO = float(os.environ.get("CMD_TIMEOUT", "30"))

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def read_until(s, needles, timeout=10.0):
    buf = b""
    t0 = time.time()
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
    buf, m = read_until(s, [b"login:", b"Password:", b"#", b"$"], timeout=15)
    if m in (b"Password:",):
        s.write(PASS.encode() + b"\r\n")
        buf2, m2 = read_until(s, [b"#", b"$", b"Login incorrect"], timeout=15)
        return buf + buf2, m2
    if m in (b"#", b"$"):
        return buf, m
    s.write(b"root\r\n")
    buf2, m2 = read_until(s, [b"Password:", b"#", b"$"], timeout=15)
    buf += buf2
    if m2 == b"Password:":
        s.write(PASS.encode() + b"\r\n")
        buf3, m3 = read_until(s, [b"#", b"$", b"Login incorrect"], timeout=15)
        return buf + buf3, m3
    return buf, m2


def clean(b):
    b = ANSI.sub(b"", b.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    return b.decode("utf-8", "replace")


def main():
    cmds = sys.argv[1:]
    s = serial.Serial(TTY, BAUD, timeout=0.3)
    s.reset_input_buffer()
    out, m = login(s)
    if m not in (b"#", b"$"):
        print("!!! login failed:", clean(out)[-300:])
        return 1
    print("### logged in as root")
    for c in cmds:
        code = "%04d" % random.randint(1000, 9999)
        mark = "Zn" + code
        full = c + " ; Z=" + code + " ; echo RC=$? ; echo Zn$Z$Z"
        s.write(full.encode() + b"\r\n")
        buf, _ = read_until(s, [mark.encode()], timeout=TMO)
        txt = clean(buf)
        # drop the echoed command line(s) up to the first newline after the echo
        lines = txt.split("\n")
        keep = []
        skipped = 0
        for ln in lines:
            if skipped < 2 and ("Zn$Z$Z" in ln or ln.strip() == c.strip()[:40] or "Z=" + code in ln):
                skipped += 1
                continue
            keep.append(ln)
        txt = "\n".join(keep)
        txt = txt.replace(mark, "").strip()
        print("$ " + c + "\n" + txt)
    s.close()
    return 0


sys.exit(main())
