# expio_ila.tcl - Integrated Logic Analyzer cores for the GPIO handshake and the
#                 RF datapath, so signals can be checked in hardware with Vivado's
#                 hardware manager after the bitstream is loaded.
#
#   ILA=1 ./build.sh          (sourced after expio.tcl and system_bd_fft.tcl)
#   ILA_DRY_RUN=1 ...         prints the candidates it would probe, adds nothing
#
# Three cores, each one optional and skipped with a notice if its target is not
# in the design:
#
#   ila_gpio       plain ILA on the AXI GPIO word, the status word and the two
#                  handshake lines - the fastest way to see whether a step pulse
#                  or a sync edge actually reached the pin.
#   system_ila_rf  AXI-Stream monitor on the ADC -> DMA link: tdata, tvalid,
#                  tready, tlast, which is the sample stream as the IIO buffer
#                  sees it. Trigger on tlast to catch a frame boundary, or on a
#                  data value to catch a specific sample.
#   system_ila_fft only if the FFT stage is built: same monitor on the shim's
#                  output, so RX and post-FFT streams can be compared side by side.
#
# A note on the RFIC interface, because it is easy to search for the wrong thing:
# the AD9361 on this board talks to the PL over **LVDS DDR** (the vendor diagram
# labels it "LVDS"), not JESD204B. There is no JESD core, no 8b/10b, no lane
# alignment to probe - `projects/pluto` uses the ADI `axi_ad9361` core with
# up_adc_interface / up_dac_interface. To look at the converter bus itself,
# trigger on the internal single-rate signals inside that core (the adc_data_i*
# buses after the IDDR), not on the package pins: raw DDR pins cannot be probed
# by an ILA directly, and any ILA on them would need an extra IDDR to be readable.
# If you ever move to an AD9361 variant with a JESD204B link, this is where the
# JESD monitor core would go instead.

set ILA_GPIO_DEPTH 4096
set ILA_RF_DEPTH   8192

proc ila_dump {} {
    puts "\n---- AXI-Stream interfaces in the design --------------------------"
    foreach p [get_bd_intf_pins] {
        set t [get_property TYPE $p]
        if {[string match "*AXI4_Stream*" $t] || [string match "*axis*" $t]} {
            puts [format "  %-52s %s" $p $t]
        }
    }
    puts "------------------------------------------------------------------\n"
}

if {[info exists ::env(ILA_DRY_RUN)] && $::env(ILA_DRY_RUN) == 1} {
    ila_dump
    puts "ila: dry run, no cores added"
    return
}

set fft_present [expr {[llength [get_bd_cells -quiet axis_fft_shim_0]] > 0}]
set expio_present [expr {[llength [get_bd_cells -quiet expio_io_0]] > 0}]

# ---------------------------------------------------------------------------
# 1. GPIO / handshake ILA
# ---------------------------------------------------------------------------
if {$expio_present} {
    if {[llength [get_bd_cells -quiet ila_expio]] == 0} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:ila ila_expio
        set_property -dict [list \
            CONFIG.C_NUM_OF_PROBES {3} \
            CONFIG.C_DATA_DEPTH    $ILA_GPIO_DEPTH \
            CONFIG.C_TRIGIN_EN     {false} \
            CONFIG.C_PROBE0_WIDTH  {12} \
            CONFIG.C_PROBE1_WIDTH  {12} \
            CONFIG.C_PROBE2_WIDTH  {2} \
        ] [get_bd_cells ila_expio]
    }
    # probe2 needs the two handshake lines as one vector -> xlconcat
    if {[llength [get_bd_cells -quiet xlconcat_sync]] == 0} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat xlconcat_sync
        set_property -dict [list CONFIG.NUM_PORTS {2} CONFIG.IN0_WIDTH {1} CONFIG.IN1_WIDTH {1}] \
            [get_bd_cells xlconcat_sync]
        connect_bd_net [get_bd_pins expio_io_0/sync_to_grbl]   [get_bd_pins xlconcat_sync/In0]
        connect_bd_net [get_bd_pins expio_io_0/sync_from_grbl] [get_bd_pins xlconcat_sync/In1]
    }
    connect_bd_net [get_bd_pins expio_io_0/gpio_out]     [get_bd_pins ila_expio/probe0]
    connect_bd_net [get_bd_pins expio_io_0/gpio_in]      [get_bd_pins ila_expio/probe1]
    connect_bd_net [get_bd_pins xlconcat_sync/dout]      [get_bd_pins ila_expio/probe2]

    set ila_clk [get_bd_nets -quiet -filter {NAME =~ "*clk*"}]
    connect_bd_net [lindex $ila_clk 0] [get_bd_pins ila_expio/clk]
    puts "  ila_expio: gpio_out, gpio_in, {sync_to_grbl, sync_from_grbl}"
} else {
    puts "  ila_expio: skipped (expio_io_0 not in the design - build with EXPIO=1 first)"
}

# ---------------------------------------------------------------------------
# 2/3. AXI-Stream monitors, ADC -> DMA and (optional) post-FFT
# ---------------------------------------------------------------------------
proc ila_add_axis_monitor {name depth} {
    if {[llength [get_bd_cells -quiet $name]] == 0} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:system_ila $name
        set_property -dict [list \
            CONFIG.C_NUM_MONITOR_SLOTS  {1} \
            CONFIG.C_DATA_DEPTH         $depth \
            CONFIG.C_SLOT_0_INTF_TYPE   {xilinx.com:interface:axis_rtl:1.0} \
        ] [get_bd_cells $name]
    }
}

# the ADC stream: look for a stream slave on the DMA cell, as system_bd_fft.tcl does
set dma ""
foreach cand {axi_ad9361_adc_dma axi_ad9361_adc_fifo adc_dma} {
    if {[get_bd_cells -quiet $cand] ne ""} { set dma $cand; break }
}
if {$dma ne ""} {
    set slot ""
    foreach p [get_bd_intf_pins -of_objects [get_bd_cells $dma]] {
        if {[get_property MODE $p] eq "Slave"} { set slot $p; break }
    }
    if {$slot ne ""} {
        ila_add_axis_monitor system_ila_rf $ILA_RF_DEPTH
        connect_bd_intf_net $slot [get_bd_intf_pins system_ila_rf/SLOT_0_AXIS]
        connect_bd_net [lindex $ila_clk 0] [get_bd_pins system_ila_rf/clk]
        puts "  system_ila_rf: monitoring $slot"
    } else {
        puts "  system_ila_rf: skipped (no AXIS slave on $dma)"
    }
} else {
    ila_dump
    puts "  system_ila_rf: skipped (ADC DMA cell not found - update the lookup list)"
}

if {$fft_present} {
    ila_add_axis_monitor system_ila_fft $ILA_RF_DEPTH
    connect_bd_intf_net [get_bd_intf_pins axis_fft_shim_0/m_axis] \
                        [get_bd_intf_pins system_ila_fft/SLOT_0_AXIS]
    connect_bd_net [lindex $ila_clk 0] [get_bd_pins system_ila_fft/clk]
    puts "  system_ila_fft: monitoring axis_fft_shim_0/m_axis"
} else {
    puts "  system_ila_fft: skipped (FFT stage not built)"
}

regenerate_bd_layout
puts "ila: instrumentation added (GPIO depths $ILA_GPIO_DEPTH, stream $ILA_RF_DEPTH)"
