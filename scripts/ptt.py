#!/usr/bin/env python3
"""Assert PTT / TX-enable on a PlutoSky 7020, from Python.

"PTT" means three different things on this board, so there are three backends
-----------------------------------------------------------------------------
`ensm`   - the transceiver's TX enable, over USB. Works today, no firmware
           change. The header's PTT OUT (5 V) pin follows TX enable on stock
           firmware, so asserting this is what actually keys the PA.
`gpio`   - a Linux GPIO line on the *board*, exported through sysfs and poked
           over the FTDI console. Needs a custom device tree that frees a line.
`devmem` - a bit of the AXI GPIO in front of the FFT stage (firmware/), written
           with busybox `devmem` over the console. Quickest route to a pin you
           can toggle without touching the transceiver.

The board-side backends run their commands **on the board** through
`/dev/ttyUSB1`, because `/dev/mem` and the Zynq gpiochip are there, not on your
PC. The stock firmware has no Python, so this script drives sysfs and `devmem`
instead of running code remotely.

    ./ptt.py --diag                         # what does this board expose?
    ./ptt.py on                             # TX path on -> PTT asserted
    ./ptt.py off
    ./ptt.py pulse --ms 250                 # 250 ms marker, then release
    ./ptt.py on --keep                      # leave it keyed
    ./ptt.py on --backend gpio --gpio 1013   # custom DT line, via the console
    ./ptt.py pulse --backend devmem --axi-base 0x41200000 --axi-bit 2

Safety
------
PTT keys the transmitter through a PGA-102+ PA. Put a load, or a well attenuated
path, on TX1 before pulsing, and leave --tx-gain at -30 dB or lower. `off` uses
the transceiver's `alert` state, which powers down RX as well - what a
half-duplex rig wants, but it will stop an RX capture that is in progress.
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# --------------------------------------------------------------------------
class EnsmBackend:
    """AD9361 TX enable via IIO, host side over USB."""

    name = "ensm"
    ON, OFF = "fdd", "alert"

    def __init__(self, uri, tx_gain=-30.0):
        import iio
        self.ctx = iio.Context(uri)
        self.phy = self.ctx.find_device("ad9361-phy")
        if self.phy is None:
            raise RuntimeError("no ad9361-phy in context %r" % uri)
        self.attr = self.phy.attrs["ensm_mode"]
        try:
            tx = self.phy.find_channel("voltage0", True)     # TX channel
            if tx is not None and "hardwaregain" in tx.attrs:
                tx.attrs["hardwaregain"].value = "%.2f" % tx_gain
        except Exception:
            pass

    def get(self):
        return self.attr.value

    def set(self, s):
        self.attr.value = s
        time.sleep(0.03)
        return self.get()

    def on(self):
        return self.set(self.ON)

    def off(self):
        return self.set(self.OFF)


# --------------------------------------------------------------------------
class ConsoleBackend:
    """Base for backends that must run on the board itself."""

    def __init__(self, tty, baud):
        from capture_serial import PlutoSerial
        self.p = PlutoSerial(tty, baud)
        self.p.run("echo 1 > /proc/sys/kernel/printk", 20)

    def close(self):
        self.p.close()


class ConsoleGpio(ConsoleBackend):
    """sysfs GPIO on the board."""

    name = "gpio"

    def __init__(self, number, tty, baud, active_low=False):
        super().__init__(tty, baud)
        self.n = number
        self.p.run("test -d /sys/class/gpio/gpio%d || echo %d > /sys/class/gpio/export"
                   % (number, number), 20)
        self.p.run("echo out > /sys/class/gpio/gpio%d/direction" % number, 20)
        if active_low:
            self.p.run("echo 1 > /sys/class/gpio/gpio%d/active_low" % number, 20)

    def get(self):
        out = self.p.run("cat /sys/class/gpio/gpio%d/value" % self.n, 20)
        m = re.findall(r"[01]", out)
        return m[-1] if m else "?"

    def set(self, v):
        self.p.run("echo %d > /sys/class/gpio/gpio%d/value" % (v, self.n), 20)
        return self.get()

    def on(self):
        return self.set(1)

    def off(self):
        return self.set(0)


class ConsoleDevmem(ConsoleBackend):
    """A bit of an AXI GPIO register, via busybox devmem on the board."""

    name = "devmem"

    def __init__(self, base, bit, tty, baud):
        super().__init__(tty, baud)
        self.base = base
        self.bit = bit

    def _read(self):
        out = self.p.run("devmem 0x%X 32" % self.base, 20)
        m = re.findall(r"0x([0-9a-fA-F]{4,8})", out)
        return int(m[-1], 16) if m else 0

    def get(self):
        return "1" if (self._read() >> self.bit) & 1 else "0"

    def set(self, v):
        cur = self._read()
        new = (cur | (1 << self.bit)) if v else (cur & ~(1 << self.bit))
        self.p.run("devmem 0x%X 32 0x%X" % (self.base, new), 20)
        return self.get()

    def on(self):
        return self.set(1)

    def off(self):
        return self.set(0)


# --------------------------------------------------------------------------
def diag(a):
    print("== PTT capability of this board ==")
    print("[host side, over USB]")
    try:
        import iio
        ctx = iio.Context(a.uri)
        phy = ctx.find_device("ad9361-phy")
        print("  IIO context    : %s" % a.uri)
        print("  ad9361-phy     : %s" % ("present" if phy else "MISSING"))
        if phy:
            print("  ensm_mode      : %-6s  <- the PTT lever on stock firmware"
                  % phy.attrs["ensm_mode"].value)
            gpo = [k for k in phy.attrs if "gpo" in k.lower()]
            print("  AD9361 GPOs    : %s" % (", ".join(gpo) if gpo else "none exported"))
    except Exception as e:
        print("  IIO            : %s" % e)

    print("[board side, over the FTDI console]")
    try:
        from capture_serial import PlutoSerial
        p = PlutoSerial(a.tty, a.baud)
        try:
            gp = p.run("cat /sys/kernel/debug/gpio 2>/dev/null", 40)
            lines = [l for l in gp.splitlines() if "gpio-" in l]
            print("  Zynq GPIO lines in use: %d" % len(lines))
            for l in lines[:6]:
                print("     " + l.strip())
            chips = p.run("ls /sys/class/gpio/ 2>/dev/null", 20)
            print("  gpiochips      : %s" % " ".join(chips.split()))
            dv = p.run("devmem 2>&1 | head -3", 25)
            print("  devmem         : %s" % ("available" if re.search(r"usage|busybox",
                                                                    dv, re.I) else "check manually"))
        finally:
            p.close()
    except Exception as e:
        print("  console        : %s (needs /dev/ttyUSB1)" % e)

    print("\nverdict")
    print("  today       : --backend ensm (PTT follows TX enable)")
    print("  board GPIO  : none free on stock firmware -> needs a custom device tree")
    print("  custom FPGA : --backend devmem against the AXI GPIO from firmware/")
    return 0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=["on", "off", "pulse", "diag"], default=None)
    ap.add_argument("--diag", action="store_true", help="report what this board exposes")
    ap.add_argument("--backend", choices=["auto", "ensm", "gpio", "devmem"], default="auto")
    ap.add_argument("--uri", default="usb:")
    ap.add_argument("--tty", default="/dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ms", type=int, default=100, help="pulse length")
    ap.add_argument("--keep", action="store_true", help="leave PTT asserted on exit")
    ap.add_argument("--gpio", type=int, default=None, help="board GPIO number")
    ap.add_argument("--active-low", action="store_true")
    ap.add_argument("--axi-base", type=lambda x: int(x, 0), default=None,
                    help="AXI GPIO base address, e.g. 0x41200000")
    ap.add_argument("--axi-bit", type=int, default=2)
    ap.add_argument("--tx-gain", type=float, default=-30.0)
    a = ap.parse_args()

    if a.diag or a.action in (None, "diag"):
        return diag(a)

    be = a.backend
    if be == "auto":
        be = "devmem" if a.axi_base is not None else ("gpio" if a.gpio is not None else "ensm")

    if be == "ensm":
        dev = EnsmBackend(a.uri, a.tx_gain)
    elif be == "gpio":
        if a.gpio is None:
            sys.exit("--backend gpio needs --gpio N")
        dev = ConsoleGpio(a.gpio, a.tty, a.baud, a.active_low)
    else:
        if a.axi_base is None:
            sys.exit("--backend devmem needs --axi-base 0x41200000")
        dev = ConsoleDevmem(a.axi_base, a.axi_bit, a.tty, a.baud)

    prev = dev.get()
    print("backend %s, state before: %s" % (dev.name, prev))

    def restore():
        if dev.name == "ensm":
            return dev.set(prev)
        return dev.set(1 if str(prev).strip() in ("1", "hi", "fdd") else 0)

    try:
        if a.action == "on":
            print("asserted -> %s" % dev.on())
        elif a.action == "off":
            print("released -> %s" % dev.off())
        else:
            print("asserted -> %s for %d ms" % (dev.on(), a.ms))
            time.sleep(a.ms / 1000.0)
            print("released -> %s" % dev.off())
    finally:
        if a.action in ("on", "pulse"):
            if a.keep:
                print("PTT left asserted - run 'ptt.py off' when you are done")
            else:
                print("restored to %s -> %s" % (prev, restore()))
        if hasattr(dev, "close"):
            dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
