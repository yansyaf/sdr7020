#!/usr/bin/env python3
"""Transmit from a PlutoSDR-compatible board (PlutoSky 7020 / ADALM-PLUTO).

Two independent ways to get RF out of the TX port:

  cw      on-chip DDS tone - no sample streaming, so it also works with only the
          DEBUG cable (--serial). Good for checking the TX chain and for
          measuring TX leakage / LO accuracy.
  tone    one complex tone streamed through the TX DMA (cyclic buffer)
  chirp   linear sweep, useful as a test signal for the RX path or a cable loop
  fm      a single-tone FM carrier

Then, to prove the whole RF chain end to end, feed TX back into RX:

    ./tx_example.py --mode chirp --loopback

--loopback needs TX1 and RX1 joined by a cable (attenuator recommended: the
board has a PGA-102+ PA on both TX outputs).

Typical transmit levels
-----------------------
  tx_hardwaregain_chan0   0 dB ... -89.75 dB (0 dB = maximum)
  DDS scale               0.0 ... 1.0   (0.1 is a safe start)

**RF safety and law.** Never transmit into an antenna without the appropriate
licence, and never connect a PA output to a receiver without attenuation. For
bench work use a shielded cable + 30 dB attenuator, or a 50 ohm dummy load.

Examples
--------
    ./tx_example.py --serial --mode cw --freq 915e6 --dds-scale 0.1
    ./tx_example.py --mode cw --freq 2400e6 --tx-gain -40
    ./tx_example.py --mode chirp --freq 915e6 --seconds 0.2 --save /tmp/chirp.npy
    ./tx_example.py --mode chirp --loopback --freq 915e6
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# --------------------------------------------------------------------------
# waveform generators, amplitude in [-1, 1] (2**14 is full scale for the DAC)
# --------------------------------------------------------------------------
def wf_tone(rate, seconds, offset=100e3, amp=0.5):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    return (amp * np.exp(2j * np.pi * offset * t)).astype(np.complex64)


def wf_chirp(rate, seconds, bw=1e6, amp=0.5):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    k = bw / seconds
    ph = np.pi * k * t ** 2
    return (amp * np.exp(1j * ph)).astype(np.complex64)


def wf_fm(rate, seconds, deviation=75e3, tone=1e3, amp=0.5):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    inst = deviation / tone * np.sin(2 * np.pi * tone * t)
    return (amp * np.exp(1j * 2 * np.pi * inst)).astype(np.complex64)


# --------------------------------------------------------------------------
# USB path (pyadi-iio)
# --------------------------------------------------------------------------
def tx_usb(a):
    import adi
    sdr = adi.Pluto(uri=a.uri) if a.uri and a.uri not in ("usb:", "local:") else adi.Pluto()
    sdr.sample_rate = int(a.rate)
    sdr.tx_rf_bandwidth = int(a.rate)
    sdr.tx_lo = sdr.rx_lo = int(a.freq)
    sdr.tx_hardwaregain_chan0 = float(a.tx_gain)
    try:
        sdr.tx_enabled_channels = [0]
    except Exception:
        pass

    if a.mode == "cw":
        # on-chip DDS: continuous, no DMA, no buffer size limits
        sdr.dds_single_tone(int(a.dds_offset), float(a.dds_scale), 0)
        print("CW: TX LO %.3f MHz, DDS %.1f kHz, scale %.2f, TX gain %.1f dB"
              % (a.freq / 1e6, a.dds_offset / 1e3, a.dds_scale, a.tx_gain))
    else:
        gen = {"tone": wf_tone, "chirp": wf_chirp, "fm": wf_fm}[a.mode]
        iq = gen(a.rate, a.seconds,)
        iq = iq * (a.amp / max(1e-9, float(np.max(np.abs(iq)))))
        if a.save:
            np.save(a.save, iq)
            json.dump({"freq": a.freq, "rate": a.rate, "mode": a.mode, "samples": len(iq),
                       "when": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      open(os.path.splitext(a.save)[0] + ".json", "w"), indent=2)
            print("waveform saved:", a.save)
        # pluto TX wants a multiple of 8 samples in the cyclic buffer
        n = (len(iq) // 8) * 8
        iq = iq[:n]
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        sdr.tx_cyclic_buffer = True
        sdr.tx(iq * 2 ** 14)
        print("%s: %.3f ms of IQ, cyclic, TX LO %.3f MHz, gain %.1f dB"
              % (a.mode, n / a.rate * 1e3, a.freq / 1e6, a.tx_gain))

    if not a.loopback:
        if a.hold:
            print("transmitting; Ctrl-C to stop")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        print("stopping")
        try:
            if a.mode != "cw":
                sdr.tx_destroy_buffer()
            else:
                sdr.dds_single_tone(0, 0.0, 0)
        except Exception:
            pass
        return 0

    # ---------------------------------------------------------------- loopback
    sdr.rx_rf_bandwidth = int(a.rate)
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = 10     # low: the cable is a strong signal
    sdr.rx_buffer_size = int(min(a.rate * 0.02, 1 << 20))
    for _ in range(2):
        sdr.rx()
    iq_rx = sdr.rx()
    iq_rx = np.asarray(iq_rx)
    if iq_rx.ndim == 2:
        iq_rx = iq_rx[0] + 1j * iq_rx[1]
    iq_rx = iq_rx / 2 ** 11

    out = a.out or "/tmp/tx_loopback"
    np.save(out + ".npy", iq_rx.astype(np.complex64))
    json.dump({"freq": a.freq, "rate": a.rate, "gain": 10, "samples": len(iq_rx),
               "mode": "tx-loopback-" + a.mode, "when": time.strftime("%Y-%m-%dT%H:%M:%S")},
              open(out + ".json", "w"), indent=2)

    # measure the tone at the expected offset
    n = len(iq_rx)
    w = np.hanning(n)
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq_rx * w))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / a.rate))
    exp = -a.dds_offset if a.mode == "cw" else 0.0
    band = np.abs(f - exp) < 50e3
    peer = spec[band].max() if band.any() else spec.max()
    floor = np.median(spec)
    snr = 10 * np.log10(peer / floor) if floor > 0 else 99
    print("RX heard the transmitter: peak %.1f dB above the noise floor at %.3f MHz"
          % (snr, (f[np.argmax(spec)] + a.freq) / 1e6))
    print("capture:", out + ".npy")
    if float(snr) < 10:
        print("no tone - check the TX1->RX1 cable, the attenuator, and the TX gain")
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass
    return 0


# --------------------------------------------------------------------------
# serial path: on-chip DDS only, works with nothing but the DEBUG cable
# --------------------------------------------------------------------------
def tx_serial(a):
    from capture_serial import PlutoSerial
    p = PlutoSerial(a.tty, a.baud)
    try:
        p.run("echo 1 > /proc/sys/kernel/printk", 20)
        p.run("iio_attr -c ad9361-phy altvoltage1 frequency %d" % int(a.freq), 30, )
        p.run("iio_attr -c ad9361-phy voltage0 hardwaregain %.2f" % a.tx_gain, 30)
        p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage0 frequency %d" % int(a.dds_offset), 30)
        p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage0 scale %.4f" % a.dds_scale, 30)
        p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage0 raw 1", 30)
        p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage1 frequency %d" % int(a.dds_offset), 30)
        p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage1 scale %.4f" % a.dds_scale, 30)
        p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage1 raw 1", 30)
        lo = p.run("iio_attr -c ad9361-phy altvoltage1 frequency", 30)
        print("board answers:\n" + lo)
        print("\nCW running: TX_LO %.3f MHz, DDS %.1f kHz, scale %.2f, gain %.1f dB"
              % (a.freq / 1e6, a.dds_offset / 1e3, a.dds_scale, a.tx_gain))
        print("Listen on the TX1 connector (spectrum analyser, or loop TX1 -> RX1 with "
              "an attenuator) then check it with:\n"
              "  ./capture_serial.py --freq %d --samples 16384 --out /tmp/cw\n"
              "  ./plot_iq.py /tmp/cw.npy" % int(a.freq))
        if a.hold:
            input("press Enter to stop\n")
            p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage0 raw 0", 30)
            p.run("iio_attr -c cf-ad9361-dds-core-lpc altvoltage1 raw 0", 30)
            print("DDS stopped")
    finally:
        p.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["cw", "tone", "chirp", "fm"], default="cw")
    ap.add_argument("--freq", type=float, default=915e6, help="TX LO in Hz")
    ap.add_argument("--rate", type=float, default=2.0833333e6)
    ap.add_argument("--tx-gain", type=float, default=-30.0, help="0 dB (max) .. -89.75 dB")
    ap.add_argument("--amp", type=float, default=0.5, help="waveform amplitude, 0..1")
    ap.add_argument("--seconds", type=float, default=0.05, help="buffer length for streaming modes")
    ap.add_argument("--dds-offset", type=float, default=100e3, help="CW/DDS tone offset from the LO")
    ap.add_argument("--dds-scale", type=float, default=0.10)
    ap.add_argument("--uri", default="usb:")
    ap.add_argument("--serial", action="store_true", help="use the DDS over the serial console")
    ap.add_argument("--tty", default="/dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--loopback", action="store_true", help="capture on RX while transmitting")
    ap.add_argument("--hold", action="store_true", help="keep transmitting until Ctrl-C / Enter")
    ap.add_argument("--save", default=None, help="save the transmitted waveform (.npy)")
    ap.add_argument("--out", default=None, help="basename for the loopback capture")
    a = ap.parse_args()

    if a.serial:
        if a.mode != "cw":
            print("--serial only supports --mode cw (the DDS lives on the chip)")
            return 3
        return tx_serial(a)
    try:
        return tx_usb(a)
    except Exception as e:
        print("transmit failed: %s" % e)
        print("no USB data link? the CW tone still works through the console:\n"
              "  ./tx_example.py --serial --mode cw --freq %d" % a.freq)
        return 2


if __name__ == "__main__":
    sys.exit(main())
