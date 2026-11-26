# expio.tcl - expose the expansion header as a Linux-controlled AXI GPIO.
#
# Sourced from hdl/projects/pluto/system_bd.tcl the same way as
# system_bd_fft.tcl, and independent of it: you can build with the FFT stage,
# the expansion I/O, both or neither.
#
#   source [file join $ad_project_dir expio.tcl]
#
# What it adds
#   axi_gpio_exp   dual channel AXI GPIO, 12 bits out / 12 bits in
#   expio_io       the wrapper from expio_io.v, giving the header pins their names
#
# Address map: the block design log prints it; typically 0x4120_0000.
#   +0x0000  ch1 data   (write the output word: bit0 step_x ... bit9 ptt_io)
#   +0x0004  ch1 tri-state (1 = input; we use all outputs, so write 0)
#   +0x0008  ch2 data   (read: bit0..2 = limit_x/y/r)
#
# Software, without any device-tree work:
#   devmem 0x41200000 32 0x001     # step_x high
#   devmem 0x41200000 32 0x000     # step_x low
#   devmem 0x41200008 32           # read the limit switches
#
# IMPORTANT: software-toggled step pulses from Linux are jittery (milliseconds,
# not microseconds). That is fine for jogging and for move-stop-measure scans
# where you wait afterwards anyway; it is NOT a substitute for GRBL for real
# coordinated motion. The next increment is a clocked step generator behind this
# same register block - the wrapper is the place it plugs in.

set EXP_OUT_W 12
set EXP_IN_W  12

if {[info exists ::env(EXPIO_DRY_RUN)] && $::env(EXPIO_DRY_RUN) == 1} {
    puts "expio: dry run, no cells added"
    return
}

# ---- the source file has to be in the project before it can be referenced ----
if {[llength [get_bd_cells -quiet expio_io_0]] == 0} {
    set expio_v [file normalize [file join $ad_project_dir expio_io.v]]
    if {[llength [get_files -quiet $expio_v]] == 0} {
        add_files -norecurse -fileset sources_1 $expio_v
    }
}

# ---- GPIO ------------------------------------------------------------------
if {[llength [get_bd_cells -quiet axi_gpio_exp]] == 0} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio axi_gpio_exp
    set_property -dict [list \
        CONFIG.C_GPIO_WIDTH   $EXP_OUT_W \
        CONFIG.C_ALL_OUTPUTS  {1} \
        CONFIG.C_IS_DUAL      {1} \
        CONFIG.C_GPIO2_WIDTH  $EXP_IN_W \
        CONFIG.C_ALL_INPUTS_2 {1} \
    ] [get_bd_cells axi_gpio_exp]
}

# ---- wrapper ---------------------------------------------------------------
if {[llength [get_bd_cells -quiet expio_io_0]] == 0} {
    create_bd_cell -type module -reference expio_io expio_io_0
}
connect_bd_net [get_bd_pins axi_gpio_exp/gpio_io_o]  [get_bd_pins expio_io_0/gpio_out]
connect_bd_net [get_bd_pins expio_io_0/gpio_in]      [get_bd_pins axi_gpio_exp/gpio2_io_i]

# ---- clock / reset ---------------------------------------------------------
set aclk_net [get_bd_nets -quiet -filter {NAME =~ "*clk*"}]
set aclk_net [lindex $aclk_net 0]
set rst_net  [get_bd_nets -quiet -filter {NAME =~ "*rst*"}]
set rst_net  [lindex $rst_net 0]
connect_bd_net $aclk_net [get_bd_pins axi_gpio_exp/s_axi_aclk]
if {$rst_net ne ""} {
    connect_bd_net $rst_net [get_bd_pins axi_gpio_exp/s_axi_aresetn]
} else {
    puts "  WARNING: no reset net matched \"*rst*\" - connect s_axi_aresetn by hand"
}

# ---- AXI slave, on a free master port of the existing interconnect ----------
set exp_ic ""
foreach cand {axi_interconnect_0 axi_interconnect ps_axi_ic axi_interconnect_1} {
    if {[get_bd_cells -quiet $cand] ne ""} { set exp_ic $cand; break }
}
if {$exp_ic eq ""} {
    error "expio: no AXI interconnect found - connect axi_gpio_exp/S_AXI by hand"
}
set free_port ""
foreach m [get_bd_intf_pins -of_objects [get_bd_cells $exp_ic]] {
    if {[get_property MODE $m] eq "Master" &&
        [llength [get_bd_intf_nets -quiet -of_objects $m]] == 0} {
        set free_port $m
        break
    }
}
if {$free_port eq ""} {
    error "expio: no free master port on $exp_ic - raise NUM_MI"
}
connect_bd_intf_net $free_port [get_bd_intf_pins axi_gpio_exp/S_AXI]
puts "  axi_gpio_exp S_AXI -> $free_port"

# ---- header pins to the top level -----------------------------------------
foreach p {step_x step_y step_r step_ena_n dir_x dir_y dir_r \
           spare_out0 spare_out1 ptt_io limit_x limit_y limit_r} {
    set pin [get_bd_pins expio_io_0/$p]
    if {[llength [get_bd_intf_nets -quiet -of_objects $pin]] == 0 &&
        [llength [get_bd_nets -quiet -of_objects $pin]] == 0} {
        make_bd_pins_external $pin
        puts "  external: $p"
    }
}

assign_bd_address [get_bd_addr_segs axi_gpio_exp/S_AXI/Reg]
regenerate_bd_layout
puts "expio: expansion header exposed as axi_gpio_exp ($EXP_OUT_W out / $EXP_IN_W in)"
