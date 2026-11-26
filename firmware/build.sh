#!/bin/bash
# build.sh - build a PlutoSky 7020 (ADALM-PLUTO compatible) firmware image with a
# custom FPGA design, targeting Vivado 2024.1 / 2024.2.
#
#   ./build.sh --check                 # what is installed, which versions
#   ./build.sh --topology              # print the block-design inventory only
#   ./build.sh                         # full build (long: bitstream + kernel + rootfs)
#   ./build.sh --no-fpga               # firmware only, keep the stock bitstream
#   ./build.sh --clean                 # throw the work tree away first
#
# Environment (all optional)
#   VIVADO_SETTINGS   path to Vivado settings64.sh   (default /opt/Xilinx/2024.2/Vivado/settings64.sh)
#   VIVADO_VERSION    expected Vivado version        (default 2024.2)
#   HDL_BRANCH        ADI hdl branch                 (default hdl_2024_r2 -> Vivado 2024.2)
#   FW_BRANCH         plutosdr-fw branch/tag         (default master)
#   WORKDIR           where everything is cloned     (default ./work)
#   REAPPLY_VENDOR   1 = apply the OpenSourceSDRLab board patch (default 1)
#
# Nothing here touches the board until you flash the result. See docs in README.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${WORKDIR:-$HERE/work}"
FW_BRANCH="${FW_BRANCH:-master}"
HDL_BRANCH="${HDL_BRANCH:-hdl_2024_r2}"
VIVADO_VERSION="${VIVADO_VERSION:-2024.2}"
VIVADO_SETTINGS="${VIVADO_SETTINGS:-/opt/Xilinx/${VIVADO_VERSION}/Vivado/settings64.sh}"
REAPPLY_VENDOR="${REAPPLY_VENDOR:-1}"
VENDOR_PATCH_URL="https://raw.githubusercontent.com/OpenSourceSDRLab/PlutoSky_7020_AD936X_SDR/main/firmware/source/plutosky-7020-plutosdr-fw-v0.38.patch"

FW_DIR="$WORKDIR/plutosdr-fw"
PROJ_DIR="$FW_DIR/hdl/projects/pluto"

CHECK_ONLY=0; TOPO=0; NO_FPGA=0; CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --check)    CHECK_ONLY=1 ;;
    --topology) TOPO=1 ;;
    --no-fpga)  NO_FPGA=1 ;;
    --clean)    CLEAN=1 ;;
    -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. toolchain
# ---------------------------------------------------------------------------
check_vivado() {
  if [[ ! -f "$VIVADO_SETTINGS" ]]; then
    echo "ERROR: $VIVADO_SETTINGS not found." >&2
    echo "       install Vivado $VIVADO_VERSION (free WebPACK covers xc7z020) and either" >&2
    echo "       export VIVADO_SETTINGS=/path/to/Vivado/settings64.sh or pass VIVADO_VERSION." >&2
    return 1
  fi
  # shellcheck disable=SC1090
  source "$VIVADO_SETTINGS" >/dev/null 2>&1 || true
  local v
  v="$(vivado -version 2>/dev/null | head -1 || true)"
  echo "    vivado : $v"
  if [[ "$v" != *"$VIVADO_VERSION"* ]]; then
    echo "    WARNING: expected $VIVADO_VERSION, found ${v:-nothing}" >&2
    echo "             ADI's hdl branch and the Vivado version must agree ($HDL_BRANCH -> $VIVADO_VERSION)" >&2
  fi
  local part
  part="$(ls /opt/Xilinx/${VIVADO_VERSION}/Vivado/data/parts/xilinx/zynq/installed 2>/dev/null | head -1 || true)"
  [[ -n "$part" ]] && echo "    parts  : zynq families present"
}

if [[ $CHECK_ONLY -eq 1 ]]; then
  say "toolchain"
  check_vivado || true
  for t in git make gcc python3 mtools; do
    printf '    %-8s %s\n' "$t" "$(command -v "$t" || echo MISSING)"
  done
  echo
  echo "    FW_BRANCH=$FW_BRANCH  HDL_BRANCH=$HDL_BRANCH  VIVADO_VERSION=$VIVADO_VERSION"
  echo "    work dir : $WORKDIR"
  exit 0
fi

if [[ $CLEAN -eq 1 && -d "$WORKDIR" ]]; then
  say "cleaning $WORKDIR"
  rm -rf "$WORKDIR"
fi

# ---------------------------------------------------------------------------
# 2. sources
# ---------------------------------------------------------------------------
mkdir -p "$WORKDIR"
if [[ ! -d "$FW_DIR/.git" ]]; then
  say "cloning plutosdr-fw ($FW_BRANCH)"
  git clone --recursive --branch "$FW_BRANCH" \
    https://github.com/analogdevicesinc/plutosdr-fw.git "$FW_DIR"
else
  say "plutosdr-fw already present, leaving it alone"
fi

say "pinning hdl to $HDL_BRANCH"
git -C "$FW_DIR/hdl" fetch --depth 1 origin "$HDL_BRANCH"
git -C "$FW_DIR/hdl" checkout -q FETCH_HEAD
git -C "$FW_DIR/hdl" log -1 --oneline

if [[ "$REAPPLY_VENDOR" == "1" ]]; then
  say "applying the PlutoSky 7020 board patch (linux dtb / u-boot / buildroot)"
  if [[ ! -f "$WORKDIR/vendor.patch" ]]; then
    curl -fsSL "$VENDOR_PATCH_URL" -o "$WORKDIR/vendor.patch"
  fi
  if git -C "$FW_DIR" apply --check "$WORKDIR/vendor.patch" 2>/dev/null; then
    git -C "$FW_DIR" apply "$WORKDIR/vendor.patch"
    echo "    applied cleanly"
  else
    echo "    WARNING: the v0.38 board patch does not apply to $FW_BRANCH." >&2
    echo "             That is expected when moving far forward: port the device-tree and" >&2
    echo "             u-boot hunks by hand (the patch touches no HDL). The FPGA build below" >&2
    echo "             still works; the resulting image is a stock Pluto image." >&2
  fi
fi

# ---------------------------------------------------------------------------
# 3. our HDL
# ---------------------------------------------------------------------------
say "installing the FFT stage into $PROJ_DIR"
cp "$HERE/hdl/ip/axis_fft_shim.v" "$PROJ_DIR/axis_fft_shim.v"
cp "$HERE/hdl/system_bd_fft.tcl"  "$PROJ_DIR/system_bd_fft.tcl"
cp "$HERE/hdl/expio/expio_io.v"   "$PROJ_DIR/expio_io.v"
cp "$HERE/hdl/expio/expio.tcl"    "$PROJ_DIR/expio.tcl"
cp "$HERE/hdl/expio/expio.xdc"    "$PROJ_DIR/expio.xdc"

BD="$PROJ_DIR/system_bd.tcl"
if ! grep -q "system_bd_fft.tcl" "$BD"; then
  cp "$BD" "$WORKDIR/system_bd.tcl.orig"
  awk '
    BEGIN { done = 0 }
    /validate_bd_design/ && !done {
      print "# --- sdr7020: FFT/IFFT stage -------------------------------------"
      print "source [file join $ad_project_dir system_bd_fft.tcl]"
      print "# -----------------------------------------------------------------"
      done = 1
    }
    { print }
  ' "$WORKDIR/system_bd.tcl.orig" > "$BD"
  echo "    inserted the source line before validate_bd_design"
else
  echo "    already patched"
fi
diff -u "$WORKDIR/system_bd.tcl.orig" "$BD" | sed 's/^/    /' || true

# expansion header as PL I/O (opt-in: EXPIO=1 ./build.sh)
if [[ "${EXPIO:-0}" == "1" ]]; then
  if ! grep -q "expio.tcl" "$BD"; then
    sed -i '/system_bd_fft.tcl/a source [file join $ad_project_dir expio.tcl]' "$BD"
    echo "    inserted expio.tcl after the FFT stage"
  fi
  echo "    NOTE: add expio.xdc to the project constraints (see firmware/README.md)"
fi

if [[ $TOPO -eq 1 ]]; then
  say "block-design inventory (dry run, no build)"
  # shellcheck disable=SC1090
  source "$VIVADO_SETTINGS" >/dev/null 2>&1 || true
  export FFT_DRY_RUN=1
  ( cd "$PROJ_DIR" && vivado -mode batch -notrace -source system_bd.tcl 2>&1 | tail -80 )
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. build
# ---------------------------------------------------------------------------
say "building firmware (this is hours on the first run)"
export VIVADO_SETTINGS
if [[ $NO_FPGA -eq 1 ]]; then
  ( cd "$FW_DIR" && make -j"$(nproc)" )
else
  ( cd "$FW_DIR" && make -j"$(nproc)" )
fi

say "artifacts"
ls -lh "$FW_DIR/build/" 2>/dev/null | grep -E 'pluto\.(frm|dfu|itb)|boot\.bin|system_top\.(bit|xsa)' || true
cat <<'EOF'

Next steps (read firmware/README.md before flashing anything):
  * SD card  : copy BOOT.BIN / image.ub (or the zip contents) onto a FAT32 card.
               Keep the QSPI image untouched -> pull the card out and the board is
               back on its old firmware.
  * QSPI/DFU : permanent unless you have a JTAG cable and the vendor's original
               dfu files. Not recommended for a first test.
EOF
