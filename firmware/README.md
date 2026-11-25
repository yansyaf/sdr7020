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
