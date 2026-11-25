# system_bd_fft.tcl
#
# Adds an FFT/IFFT stage to the ADI "pluto" reference design.
#
# Where it comes from: plutosdr-fw builds its bitstream from
#   hdl/projects/pluto/system_bd.tcl
# build.sh inserts a single line
#   source $ad_project_dir/system_bd_fft.tcl
# just before validate_bd_design, and copies this file plus hdl/ip/ into
# hdl/projects/pluto/. Nothing else in the ADI tree is edited, so switching the
# feature off is the same as removing that line.
#
# What gets added:
#   axis_fft_shim   our AXI4-Stream framing / direction / bypass block
#   xfft_0          Xilinx FFT core, 1024 points, 16-bit fixed point, scaled
#   axis_dwidth     only if the ADI stream is wider than 32 bits (see WIDTH)
#   axi_gpio_fft    2 channels: control out, counters in
#
# IMPORTANT: ADI renames block-design cells between hdl releases. Run this
# script with FFT_DRY_RUN 1 first (build.sh --topology does exactly that) and
# read the printed inventory; the lookup lists below cover the names used by
# hdl_2024_r1 / hdl_2024_r2 / master, but verify before trusting them.

set FFT_FRAME_LEN   1024
set FFT_DATA_WIDTH  32        ;# one complex sample {Q,I}; set 64 for both channels
set FFT_IP_DIR      "$ad_project_dir/../../ip"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
proc fft_first {patterns} {
    foreach p $patterns {
        set o [get_bd_cells -quiet $p]
        if {$o ne ""} { return $o }
    }
    return ""
}

proc fft_dump_topology {} {
    puts "\n---- block design inventory -------------------------------------"
    foreach c [get_bd_cells] {
        puts [format "  cell %-28s %s" [get_property NAME $c] [get_property VLNV $c]]
    }
    puts "\n---- interface pins --------------------------------------------"
    foreach p [get_bd_intf_pins] { puts "  intf $p" }
    puts "----------------------------------------------------------------\n"
}

proc fft_create_xfft {} {
    global FFT_FRAME_LEN FFT_DATA_WIDTH FFT_IP_DIR
    set ipdir [file join $FFT_IP_DIR xfft]
    file mkdir $ipdir
    if {[llength [get_ips -quiet xfft_0]] == 0} {
        create_ip -name xfft -vendor xilinx.com -library ip -module_name xfft_0 -dir $ipdir
    }
    set_property -dict [list \
        CONFIG.transform_length          $FFT_FRAME_LEN \
        CONFIG.implementation_options    {radix_4_burst_io} \
        CONFIG.data_format               {fixed_point} \
        CONFIG.input_width               {16} \
        CONFIG.phase_factor_width        {16} \
        CONFIG.scaling_options           {scaled} \
        CONFIG.rounding_modes            {convergent_rounding} \
        CONFIG.aclken                    {false} \
        CONFIG.aresetn                   {true} \
        CONFIG.ovflo                     {false} \
        CONFIG.output_ordering           {natural_order} \
    ] [get_ips xfft_0]
    puts "  xfft_0: $FFT_FRAME_LEN point, 16 bit fixed point, scaled"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if {[info exists ::env(FFT_DRY_RUN)] && $::env(FFT_DRY_RUN) == 1} {
    fft_dump_topology
    puts "dry run: no cells added"
    return
}

set adc_dma [fft_first {axi_ad9361_adc_dma axi_ad9361_adc_fifo adc_dma}]
if {$adc_dma eq ""} {
    fft_dump_topology
    error "cannot find the ADC DMA cell; check the inventory above and update the lookup list"
}
puts "  ADC path target: $adc_dma"

set dma_slave ""
foreach p [get_bd_intf_pins -of_objects $adc_dma] {
    if {[get_property MODE $p] eq "Slave" && [get_property TYPE $p] eq "AXI4_Stream"} {
        set dma_slave $p
        break
    }
}
if {$dma_slave eq ""} {
    fft_dump_topology
    error "cannot find the AXI4-Stream slave pin of $adc_dma"
}
set src_pin ""
set src_net [get_bd_intf_nets -quiet -of_objects $dma_slave]
if {$src_net ne ""} {
    foreach p [get_bd_intf_pins -of_objects $src_net] {
        if {$p ne $dma_slave} { set src_pin $p }
    }
}
if {$src_pin eq ""} {
    error "the DMA slave pin is not connected - is this the right design?"
}
puts "  inserting between $src_pin and $dma_slave"

# ---- cells ----------------------------------------------------------------
if {[llength [get_bd_cells -quiet axis_fft_shim_0]] == 0} {
    set shim_file [file normalize [file join $ad_project_dir axis_fft_shim.v]]
    if {[llength [get_files -quiet $shim_file]] == 0} {
        add_files -norecurse -fileset sources_1 $shim_file
    }
    create_bd_cell -type module -reference axis_fft_shim axis_fft_shim_0
    set_property -dict [list \
        CONFIG.DATA_WIDTH $FFT_DATA_WIDTH \
        CONFIG.FRAME_LEN  $FFT_FRAME_LEN \
    ] [get_bd_cells axis_fft_shim_0]
}

fft_create_xfft

# ---- clock / reset --------------------------------------------------------
set aclk_net [get_bd_nets -quiet -of_objects [get_bd_pins $adc_dma/aclk]]
if {$aclk_net eq ""} {
    set aclk_net [get_bd_nets -quiet -filter {NAME =~ "*clk*"}]
    set aclk_net [lindex $aclk_net 0]
}
set rst_net [get_bd_nets -quiet -filter {NAME =~ "*rst*"}]
set rst_net [lindex $rst_net 0]

connect_bd_net $aclk_net [get_bd_pins axis_fft_shim_0/aclk]
connect_bd_net $aclk_net [get_bd_pins xfft_0/aclk]
if {$rst_net ne ""} {
    connect_bd_net $rst_net [get_bd_pins axis_fft_shim_0/aresetn]
    connect_bd_net $rst_net [get_bd_pins xfft_0/aresetn]
} else {
    puts "  WARNING: no reset net matched \"*rst*\"; connect aresetn manually"
}

# ---- datapath: source -> shim -> dma --------------------------------------
delete_bd_objs [get_bd_intf_nets -of_objects $dma_slave]
connect_bd_intf_net $src_pin [get_bd_intf_pins axis_fft_shim_0/s_axis]
connect_bd_intf_net [get_bd_intf_pins axis_fft_shim_0/m_axis] $dma_slave

# ---- control: AXI GPIO ----------------------------------------------------
# A module-reference cell exposes each scalar port as its own pin, so the 8-bit
# GPIO output is sliced per bit and the two 32-bit counters are concatenated
# into the 64-bit input channel.
if {[llength [get_bd_cells -quiet axi_gpio_fft]] == 0} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio axi_gpio_fft
    set_property -dict [list \
        CONFIG.C_GPIO_WIDTH   {8} \
        CONFIG.C_ALL_OUTPUTS  {1} \
        CONFIG.C_IS_DUAL      {1} \
        CONFIG.C_GPIO2_WIDTH  {64} \
        CONFIG.C_ALL_INPUTS_2 {1} \
    ] [get_bd_cells axi_gpio_fft]
}

connect_bd_net $aclk_net [get_bd_pins axi_gpio_fft/s_axi_aclk]
if {$rst_net ne ""} {
    connect_bd_net $rst_net [get_bd_pins axi_gpio_fft/s_axi_aresetn]
}
set fft_ic [fft_first {axi_interconnect_0 axi_interconnect ps_axi_ic axi_interconnect_1}]
if {$fft_ic eq ""} {
    fft_dump_topology
    error "no AXI interconnect found for the GPIO; connect axi_gpio_fft/S_AXI by hand"
}
set fft_free_port ""
foreach m [get_bd_intf_pins -of_objects $fft_ic] {
    if {[get_property MODE $m] eq "Master" &&
        [llength [get_bd_intf_nets -quiet -of_objects $m]] == 0} {
        set fft_free_port $m
        break
    }
}
if {$fft_free_port eq ""} {
    error "no free master port on $fft_ic - raise its NUM_MI or move the GPIO elsewhere"
}
connect_bd_intf_net $fft_free_port [get_bd_intf_pins axi_gpio_fft/S_AXI]
puts "  axi_gpio_fft S_AXI -> $fft_free_port"

foreach {slice_name bit target} {xlslice_en 0 cfg_enable xlslice_fwd 1 cfg_fwd_inv} {
    if {[llength [get_bd_cells -quiet $slice_name]] == 0} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice $slice_name
        set_property -dict [list CONFIG.DIN_WIDTH {8} \
                                 CONFIG.DIN_FROM $bit CONFIG.DIN_TO $bit \
                                 CONFIG.DOUT_WIDTH {1}] [get_bd_cells $slice_name]
        connect_bd_net [get_bd_pins axi_gpio_fft/gpio_io_o] [get_bd_pins $slice_name/Din]
        connect_bd_net [get_bd_pins $slice_name/Dout] [get_bd_pins axis_fft_shim_0/$target]
    }
}

if {[llength [get_bd_cells -quiet xlconcat_fft_cnt]] == 0} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat xlconcat_fft_cnt
    set_property -dict [list CONFIG.NUM_PORTS {2} \
                             CONFIG.IN0_WIDTH {32} CONFIG.IN1_WIDTH {32}] \
        [get_bd_cells xlconcat_fft_cnt]
    connect_bd_net [get_bd_pins axis_fft_shim_0/sample_count] [get_bd_pins xlconcat_fft_cnt/In0]
    connect_bd_net [get_bd_pins axis_fft_shim_0/frame_count]  [get_bd_pins xlconcat_fft_cnt/In1]
    connect_bd_net [get_bd_pins xlconcat_fft_cnt/dout] [get_bd_pins axi_gpio_fft/gpio2_io_i]
}
puts "  axi_gpio_fft: bit0 = enable, bit1 = FWD_INV; channel 2 = {sample_count, frame_count}"

# ---- address map ----------------------------------------------------------
assign_bd_address [get_bd_addr_segs axi_gpio_fft/S_AXI/Reg]

regenerate_bd_layout
puts "FFT insert done: $FFT_FRAME_LEN point FFT/IFFT between $src_pin and $dma_slave"
