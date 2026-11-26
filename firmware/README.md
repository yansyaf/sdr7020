# Custom FPGA firmware for the PlutoSky 7020 (AD9361 + Zynq-7020)

This folder builds a **plutosdr-fw** image with a modified FPGA design: an
FFT/IFFT stage inserted into the receive datapath, selectable at runtime from
software. Target toolchain is **Vivado 2024.1 / 2024.2** (free Standard edition
covers the xc7z020).

```
firmware/
├── build.sh                 clone + patch + build, with --check / --topology
├── hdl/
│   ├── system_bd_fft.tcl    block-design edit: xfft + shim + GPIO
│   └── ip/
│       ├── axis_fft_shim.v      AXI-Stream framing / direction / bypass
│       └── tb_axis_fft_shim.v   self-checking testbench (iverilog)
└── README.md                this file
```

## Vivado version matrix

The FPGA design comes from ADI's `hdl` repository, and ADI pins one Vivado
release per branch. Mixing them is the usual source of "it does not build":

| ADI hdl branch | Vivado | notes |
|---|---|---|
| `hdl_2024_r1` | 2024.1 | works with this folder unchanged |
| `hdl_2024_r2` | 2024.2 | **default** in `build.sh` |
| `hdl_2024_r*`+ | 2024.2+ | check `hdl/README.md` of the branch you pick |
| `master` | 2025.1 | plutosdr-fw master tracks this |

```bash
./build.sh --check          # reports Vivado, git, mtools, and the chosen versions
```

## Why not just rebuild the vendor v0.38 firmware?

The board ships a v0.38 port (see the vendor repo: `firmware/source/
plutosky-7020-plutosdr-fw-v0.38.patch`). v0.38 was built with a 2019-era Vivado;
its HDL will not open in 2024.x. But that patch **touches no HDL at all** — it is
device-tree, kernel config, u-boot and one buildroot init script:

```
linux/arch/arm/boot/dts/zynq-pluto-sdr*.dts   2rx-2tx mode, DDS compatible string
linux/arch/arm/configs/zynq_pluto_defconfig
buildroot/board/pluto/S23udc
u-boot-xlnx/... (dts, board.c, defconfig, zynq-common.h)
```

So the migration path is: **current plutosdr-fw + ADI hdl at 2024.x + re-apply
the board patch**. `build.sh` does exactly that (`REAPPLY_VENDOR=1`, the default)
and warns instead of failing when the v0.38 hunks no longer apply, since they
drift as Linux and u-boot move on.

⚠️ The boot layout changed with plutosdr-fw 0.39: v0.38 uses
`BOOT.bin + uEnv.txt + devicetree.dtb + uImage + uramdisk.image.gz`, current
plutosdr-fw produces `BOOT.BIN + image.ub` (FIT). Whatever you flash must match
the generation you built, or the board will not boot from that medium.

## What the FFT stage does

`axis_fft_shim.v` sits between the AD9361 receive core and the DMA:

```
AD9361 core ──AXI4-Stream──► axis_fft_shim ──► xfft_0 ──► axis_fft_shim ──► DMA ──► IIO buffer
                                  │
                                  └── control: enable, FFT/IFFT   (AXI GPIO)
```

It does three things, all of them deliberate design decisions:

1. **Frame alignment.** ADI's DMA only asserts TLAST at the end of a whole
   buffer (megabytes). An FFT needs a fixed transform length, so the shim
   regenerates TLAST every `FRAME_LEN` beats (1024 by default) and passes an
   upstream TLAST through unchanged.
2. **Direction.** It drives the core's config channel with `FWD_INV`, so the
   same bitstream does FFT *and* IFFT; software flips one bit.
3. **Bypass.** `cfg_enable = 0` restores the stock datapath, which makes A/B
   comparison against the original firmware trivial.

### Control / status map (AXI GPIO `axi_gpio_fft`)

| offset | direction | bits |
|---|---|---|
| `+0x00` | out | bit0 `enable`, bit1 `FWD_INV` (1 = inverse) |
| `+0x08` | in  | `[31:0] sample_count`, `[63:32] frame_count` |

Two ways to drive it from the ARM core:

* **`/dev/mem` (no device-tree work):** the `build.sh` log prints the assigned
  base address; `mmap` 8 KiB and read/write 32-bit words. Quick and dirty, fine
  for bring-up.
* **Device tree (proper):** add a node for `axi_gpio_fft` with
  `gpio-controller; #gpio-cells = <2>;` and the address from the block design,
  then use libgpiod. Costs a device-tree rebuild; do this once the datapath
  works.

The register-level view stays intact either way: the DMA still hands you
interleaved 16-bit I/Q in `cf-ad9361-lpc`, only now each 1024-sample frame is a
transformed one. `scripts/plot_iq.py` plots it as usual.

### Resource cost on xc7z020

1024-point, 16-bit fixed point, radix-4 burst I/O: roughly **10–15 DSP48 and
12–18 BRAM36** for the FFT core, plus <1 % LUT for the shim. The stock pluto
design leaves enough of both; confirm with the utilisation report after the
first build — if it does not fit, drop `transform_length` to 512 or switch
`implementation_options` to `radix_2_burst_io`.

## Build

```bash
export VIVADO_SETTINGS=/opt/Xilinx/2024.2/Vivado/settings64.sh
./build.sh --check
./build.sh --topology     # prints the block-design inventory, adds nothing
./build.sh                # clone, patch, build (hours on the first run)
```

`build.sh` never edits the ADI tree beyond: copying the two files into
`hdl/projects/pluto/` and inserting one `source` line before
`validate_bd_design` in `system_bd.tcl`. A diff of that change is printed.

Artifacts land in `work/plutosdr-fw/build/` — `system_top.bit`, `system_top.xsa`,
`pluto.frm`/`pluto.dfu`, `BOOT.BIN`, `image.ub`.

## Verify before you flash

The shim (the part written here) is covered by a self-checking testbench that
needs no Vivado — iverilog is enough:

```bash
cd hdl/ip
iverilog -g2012 -o /tmp/tb_fft tb_axis_fft_shim.v axis_fft_shim.v && vvp /tmp/tb_fft
# -> PASS: all checks passed
```

It checks frame-length TLAST regeneration, upstream TLAST pass-through, that the
FWD_INV bit actually reaches the core (and is resent when the direction
changes), bypass integrity and the counters. `xfft_0` in the testbench is a
deliberately dumb stand-in — it proves the plumbing, **not** the FFT arithmetic;
that is Xilinx's core and is verified by its own model.

What is **not** verified here: the TCL has never been run against a live hdl
checkout on this machine (no Vivado installed). Cell names in ADI's block design
move between releases, which is why `--topology` exists — run it first and read
the inventory if the insert fails.

## Flashing, and how not to brick the board

The board boots from QSPI by default. Keep it that way until the new image is
proven:

1. **SD card first.** FAT32, copy `BOOT.BIN` + `image.ub` (or the `plutosdr-fw`
   zip contents) to the root, insert, power on. SD boot has priority over QSPI on
   this design, so pulling the card returns you to the vendor firmware.
2. **Check it boots** to `pluto login:` on the serial console, then `iio_info -s`
   and a capture with `scripts/capture.py`.
3. **Only then** consider QSPI/DFU (`make qspi`, or the vendor's
   `program_dfu.bat`). A failed QSPI write needs JTAG (the FT2232H on the DEBUG
   port can do it) to recover; SD cannot help you if the boot ROM no longer finds
   a valid image.
4. **Keep a copy** of `PlutoSky-7020_SD-card-firmware_v0.38.zip` and
   `PlutoSky-7020_QSPI-flash-firmware_v0.38.zip` off the board. That is your
   way back.

## Licence

`build.sh`, the TCL and the Verilog in this folder: MIT, same as the rest of the
repository. ADI's hdl, plutosdr-fw, Linux and u-boot keep their own licences
(mostly GPL-2.0 / LGPL-2.1), and the Xilinx FFT core is used under the Vivado
licence you install it with.

## Expansion header as PL I/O (`expio/`)

The vendor's functional block diagram shows a two-pin "3.3V IO / 1.8V IO" header.
**That diagram is simplified and misleading.** Sheet 8 of
`PlutoSky-7020_Schematic_PA.pdf` (PL banks 34 / 35 / 13) shows the header nets
going to ordinary PL pins:

| header net | Zynq ball | level | plan in `expio.xdc` |
|---|---|---|---|
| `3V3_IO1` | V10 | 3.3 V | step X |
| `3V3_IO2` | U9 | 3.3 V | step Y |
| `3V3_IO3` | U10 | 3.3 V | step R |
| `3V3_IO4` | T9 | 3.3 V | enable (active low) |
| `1V8_IO1_P` | G19 | 1.8 V | limit X (input) |
| `1V8_IO1_N` | G20 | 1.8 V | dir X |
| `1V8_IO3_P` | J18 | 1.8 V | limit Y (input) |
| `1V8_IO3_N` | H18 | 1.8 V | dir Y |
| `1V8_IO5_P` | H16 | 1.8 V | limit R (input) |
| `1V8_IO5_N` | H17 | 1.8 V | dir R |
| `1V8_IO7_P` | L14 | 1.8 V | spare |
| `1V8_IO7_N` | L15 | 1.8 V | spare |
| `PTT_IO` | P14 | 1.8 V | PTT output |

Extracted from the PDF text layer and cross-checked against a render of the
sheet. **Re-verify on sheet 8 before you synthesize** — a wrong PACKAGE_PIN on a
bank whose VCCO differs can damage the part.

So yes: custom HDL can drive the header. Twelve general-purpose pins is enough
for 3 axes (6 step/dir + 3 limits) with three to spare. What it is *not* enough
for is driving motor signals directly, for two separate reasons:

* **Levels.** The step lines land on bank 13 (3.3 V, drives a driver input fine);
  the dir lines land on bank 35 (**1.8 V**). A DRV8825/TMC2209 input wants
  3.3–5 V, so put a 74LVC1T45 on each dir line — or move all three dirs onto the
  four 3.3 V pins and give up the enable/spare.
* **Timing.** Linux-toggled step pulses jitter by milliseconds. Fine for jogging
  and for move-stop-measure scans; not fine for coordinated motion. That needs a
  clocked step generator in the PL, behind the same register block.

### Build it

```bash
EXPIO=1 ./build.sh          # adds axi_gpio_exp + expio_io to the block design
```

Then add `expio.xdc` to the project's constraints (it is copied into
`hdl/projects/pluto/`), and in the block design log take the assigned base
address — usually `0x4120_0000`:

```bash
devmem 0x41200000 32 0x001    # step_x high  (bit0)
devmem 0x41200000 32 0x000    # step_x low
devmem 0x41200008 32          # read limit switches (bit0..2)
```

Bit map:

| out bit | signal | pin | | in bit | signal |
|---|---|---|---|---|---|
| 0..2 | step_x / step_y / step_r | 3V3_IO1..3 | | 0..2 | limit_x / limit_y / limit_r |
| 3 | enable (active low) | 3V3_IO4 | | 3 | sync_from_grbl level |
| 4..6 | dir_x / dir_y / dir_r | 1V8_IO1_N, 3_N, 5_N | | 4 | sync pulse busy |
| 7 | ptt_io | PTT_IO (P14) | | 5 | sync pulse toggle (activity) |
| 10 | **sync pulse trigger** (edge) | - | | | |
| 11 | **sync source**: 0 = software, 1 = echo | - | | | |

Verified here: `expio_io.v` compiles under iverilog and its testbench confirms the
bit map; both TCL files parse. The block design itself has not been run through
Vivado (none on this machine), so use `EXPIO_DRY_RUN=1` first, as with the FFT
stage.

## Sync pulse to the motion controller (`sync_pulse.v`)

The handshake in `docs/sync.md` needs one thing from the FPGA: a pulse the motion
controller can trust. `sync_pulse.v` generates it, and it has two sources:

* **software** - a rising edge on `out[10]` of the AXI GPIO. The bit is an event,
  not a level, so it does not need clearing; a steady high produces exactly one
  pulse.
* **hardware echo** - with `out[11]` set, a rising edge arriving on
  `sync_from_grbl` produces the pulse inside the PL. No USB round trip, no
  Linux scheduler, no jitter.

```
GRBL M8 ──[divider 3.3→1.8]──► 1V8_IO7_N (L15) sync_from_grbl
1V8_IO7_P (L14) sync_to_grbl ──[74LVC1T45 1.8→3.3]──► GRBL A5 probe / cycle-start
```

Both pins are on the **1.8 V** bank, so the outgoing line needs a level shifter
(a divider only works downward), and the incoming one needs a divider. Do not
skip either: 3.3 V into a 1.8 V bank pin is out of spec.

Pulse width is the `PULSE_CLKS` parameter, default 1000 cycles (10 us at
100 MHz) - longer than GRBL needs to see it, short enough that it cannot look
like a stuck line. Raise it if your controller's input filter is slow.

Status back to software: `in[3]` follows the incoming level, `in[4]` is busy
while the pulse is high, `in[5]` toggles once per pulse - a cheap way to confirm
from `devmem` that a pulse happened without a scope.

**Verified:** `tb_expio_io.v` runs 17 checks under iverilog - bit map, a pulse of
the right width, no re-trigger while the bit stays high, the hardware echo with
`src_sel` set, and silence when it is not. `PASS: all checks passed`.

## ILA instrumentation (`expio_ila.tcl`)

```bash
EXPIO=1 ILA=1 ./build.sh        # GPIO + RF monitors
ILA_DRY_RUN=1 ./build.sh --topology   # print the AXI-Stream candidates first
```

| core | probes | use |
|---|---|---|
| `ila_expio` | `gpio_out[11:0]`, `gpio_in[11:0]`, `{sync_to_grbl, sync_from_grbl}` | did the step pulse or the sync edge actually reach the pin, and when |
| `system_ila_rf` | the ADC -> DMA AXI4-Stream (`tdata/tvalid/tready/tlast`) | trigger on `tlast` for a frame boundary, or on a data value for a specific sample |
| `system_ila_fft` | `axis_fft_shim_0/m_axis` | only when the FFT stage is built; compare RX against post-FFT |

Depths default to 4096 (GPIO) and 8192 (streams). Each stream monitor costs a
few BRAM36 at those depths - check the utilisation report on the xc7z020 before
raising them.

Use: program the bitstream, open the Vivado hardware manager, pick the core, set
the trigger (`tlast` is the useful one on the stream; any bit on the GPIO word
for the handshake), run, and capture. That is how you answer "did the pulse
leave the pin" without a scope.

**On the JESD question:** this board's RFIC link is **LVDS DDR**, not JESD204B.
The vendor diagram labels the RFIC<->PL arrow "LVDS" for a reason: `projects/pluto`
uses the ADI `axi_ad9361` core with `up_adc_interface`/`up_dac_interface`, there
is no JESD core, no 8b/10b and no lane alignment to probe. To look at the
converter bus itself, probe the internal single-rate `adc_data_*` signals after
the IDDR inside that core - raw DDR package pins cannot be probed by an ILA
directly, and an ILA on them would need an extra IDDR to be readable at all.
