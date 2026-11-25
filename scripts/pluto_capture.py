#!/usr/bin/python3
"""Capture IQ samples on the PlutoSky board and transfer them to this host
over the FTDI serial console (no USB data link required).

The board dumps `od -An -tx1` hex between BEGIN/END markers; the host
decodes, verifies with md5 and saves <out>.c64 (complex float32 pairs).

Usage:
  pluto_capture.py --freq 947e6 --rate 2083333 --bw 2000000 --gain 71 --samples 8192 --out /home/yansyaf/sdr/cap_947M
"""
import argparse, hashlib, os, random, re, serial, sys, time

TTY = os.environ.get("PLUTO_TTY", "/dev/ttyUSB1")
BAUD = int(os.environ.get("PLUTO_BAUD", "115200"))
PASS = os.environ.get("PLUTO_PASS", "analog")

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
HEXTOK = re.compile(rb"^[0-9a-fA-F]{2}$")


def read_until(s, needles, timeout, verbose=False):
    buf = b""
    t0 = time.time()
    last = t0
    while time.time() - t0 < timeout:
        d = s.read(65536)
        if d:
            buf += d
            last = time.time()
            for n in needles:
                if n in buf:
                    return buf, n
        elif time.time() - last > timeout:
            break
    return buf, None


def login(s):
    s.write(b"\r\n")
    buf, m = read_until(s, [b"login:", b"Password:", b"#", b"$"], 20)
    if m == b"Password:":
        s.write(PASS.encode() + b"\r\n")
        buf2, m2 = read_until(s, [b"#", b"$", b"Login incorrect"], 20)
        return buf + buf2, m2
    if m in (b"#", b"$"):
        return buf, m
    s.write(b"root\r\n")
    buf2, m2 = read_until(s, [b"Password:", b"#", b"$"], 20)
    if m2 == b"Password:":
        s.write(PASS.encode() + b"\r\n")
        buf3, m3 = read_until(s, [b"#", b"$", b"Login incorrect"], 20)
        return buf + buf2 + buf3, m3
    return buf + buf2, m2


def run(s, cmd, timeout=60, quiet=False):
    code = "%05d" % random.randint(10000, 99999)
    mark = ("Zn" + code).encode()
    s.write((cmd + " ; Z=" + code + " ; echo Zn$Z$Z").encode() + b"\r\n")
    buf, m = read_until(s, [mark], timeout)
    txt = ANSI.sub(b"", buf).decode("utf-8", "replace")
    lines = [l for l in txt.replace("\r", "\n").split("\n")]
    # drop echo lines of our own command
    out = []
    for l in lines:
        if "Zn$Z$Z" in l or ("Z=" + code) in l:
            continue
        out.append(l)
    res = "\n".join(out).replace("Zn" + code, "").strip()
    if not quiet:
        print("   $ " + cmd[:90] + ("..." if len(cmd) > 90 else ""))
        if res:
            print("     " + res.replace("\n", "\n     ")[:600])
    if m is None:
        raise RuntimeError("timeout waiting for marker on: " + cmd[:60])
    return res


def configure(s, freq, rate, bw, gain, antenna="A_BALANCED"):
    run(s, "iio_attr -c ad9361-phy voltage0 gain_control_mode manual", quiet=True)
    run(s, "iio_attr -c ad9361-phy voltage0 hardwaregain %d" % gain, quiet=True)
    run(s, "iio_attr -c ad9361-phy voltage0 sampling_frequency %d" % rate, quiet=True)
    run(s, "iio_attr -c ad9361-phy voltage0 rf_bandwidth %d" % bw, quiet=True)
    run(s, "iio_attr -c ad9361-phy voltage0 rf_port_select %s" % antenna, quiet=True)
    run(s, "iio_attr -c ad9361-phy altvoltage0 frequency %d" % int(freq), quiet=True)
    time.sleep(0.4)
    got = run(s, "iio_attr -c ad9361-phy altvoltage0 frequency", quiet=True)
    print("   LO readback:", got.strip().splitlines()[-1] if got.strip() else "?")


def capture(s, nsamples, out, timeout_factor=1.9):
    run(s, "iio_readdev -b %d -s %d cf-ad9361-lpc voltage0 voltage1 > /tmp/rx.bin"
        % (min(262144, max(4096, nsamples // 4)), nsamples), quiet=True)
    size = run(s, "ls -l /tmp/rx.bin", quiet=True)
    print("   board file:", size.strip().splitlines()[-1] if size.strip() else "?")
    md5 = run(s, "md5sum /tmp/rx.bin", quiet=True)
    md5 = md5.strip().split()[0] if md5.strip() else ""
    print("   board md5:", md5)

    code = "%05d" % random.randint(10000, 99999)
    beg, end = ("BEGn%d" % int(code)).encode(), ("ENDn%d" % int(code)).encode()
    s.reset_input_buffer()
    time.sleep(0.2)
    s.reset_input_buffer()
    s.write(("Z=%s; echo BEGn$Z$Z; od -An -tx1 -v /tmp/rx.bin; echo ENDn$Z$Z\r\n"
             % code).encode())
    buf, m = read_until(s, [beg], 90)
    if m is None:
        raise RuntimeError("no BEGIN marker")
    payload = buf.split(beg, 1)[1]
    expected_hex = nsamples * 4 * 2
    need_secs = expected_hex / 9500.0
    t_end = time.time() + 60 + need_secs * 2.0
    while end not in payload and time.time() < t_end:
        d = s.read(65536)
        if d:
            payload += d
    if end not in payload:
        raise RuntimeError("transfer truncated (%d bytes so far)" % len(payload))
    payload = payload.split(end)[0]
    if os.environ.get("PLUTO_DBG"):
        with open("/tmp/pluto_payload.txt", "wb") as f:
            f.write(payload)
        print("   debug payload saved (%d bytes text)" % len(payload))
    toks = [t for t in re.split(rb"[^0-9a-fA-F]+", payload) if HEXTOK.match(t)]
    raw = bytes.fromhex(b"".join(toks).decode())
    print("   got %d bytes, expected %d" % (len(raw), nsamples * 4))
    if len(raw) != nsamples * 4:
        raise RuntimeError("size mismatch")
    if md5 and hashlib.md5(raw).hexdigest() != md5:
        raise RuntimeError("md5 mismatch")
    with open(out + ".raw", "wb") as f:
        f.write(raw)
    print("   saved", out + ".raw")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, required=True)
    ap.add_argument("--rate", type=float, default=2083333)
    ap.add_argument("--bw", type=float, default=2000000)
    ap.add_argument("--gain", type=int, default=71)
    ap.add_argument("--samples", type=int, default=8192)
    ap.add_argument("--antenna", default="A_BALANCED")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    s = serial.Serial(TTY, BAUD, timeout=0.5)
    buf, m = login(s)
    if m not in (b"#", b"$"):
        print("login failed")
        return 2
    run(s, "echo 1 > /proc/sys/kernel/printk", quiet=True)  # silence kernel console
    configure(s, a.freq, a.rate, a.bw, a.gain, a.antenna)
    t0 = time.time()
    capture(s, a.samples, a.out)
    print("   elapsed %.1f s" % (time.time() - t0))
    s.close()
    return 0


sys.exit(main())
