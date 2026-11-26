# expio.xdc - expansion-header pins of the PlutoSky 7020 as PL I/O
#
# Source of truth: vendor schematic "PlutoSky-7020_Schematic_PA.pdf", sheet 8
# (PL banks 34 / 35 / 13). Nets below were extracted from the PDF text layer and
# cross-checked against a render of the sheet - **re-verify before you
# synthesise**, because a wrong PACKAGE_PIN on a bank with a different VCCO can
# damage the part.
#
#   pdftotext -f 8 -l 8 -layout PlutoSky-7020_Schematic_PA.pdf - | less
#
# Two banks are involved and that matters for the IOSTANDARD:
#   bank 13  - the 3V3_IO* nets, VCCO is 3.3 V   -> LVCMOS33
#   bank 35  - the 1V8_IO* nets and PTT_IO, VCCO is 1.8 V -> LVCMOS18
# Never set LVCMOS33 on a 1.8 V bank: the pin driver will be over-driven.
#
# Signal plan used by expio.tcl (3-axis step/dir + limit switches):
#   3V3_IO1 -> step X        1V8_IO1_P -> limit X (input)
#   3V3_IO2 -> step Y        1V8_IO3_P -> limit Y (input)
#   3V3_IO3 -> step R        1V8_IO5_P -> limit R (input)
#   3V3_IO4 -> spare / enable
#   1V8_IO1_N -> dir X       1V8_IO7_P -> spare
#   1V8_IO3_N -> dir Y       1V8_IO7_N -> spare
#   1V8_IO5_N -> dir R       PTT_IO    -> ptt_io  (see ptt.py)
#   1V8_IO7_P -> sync_to_grbl   (pulse out, firmware or hardware triggered)
#   1V8_IO7_N -> sync_from_grbl (edge in, from GRBL M8/probe)
#
# Levels: the step lines are 3.3 V and can drive a driver input directly (or
# through a 74LVC245). The dir lines are 1.8 V and are NOT in spec for a
# DRV8825/TMC2209 input - put a 74LVC1T45 (A=1.8 V, B=3.3 V) on each, or move
# all three dirs onto the 3.3 V pins by giving up the enable/spare.
# Limit switches are 3.3 V or 5 V in the real world: never wire them straight to
# a 1.8 V pin, divide first (10k/20k) and keep the input as LVCMOS18.

# ---- outputs, bank 13 (VCCO 3.3 V) -----------------------------------------
set_property PACKAGE_PIN V10 [get_ports {step_x}]
set_property PACKAGE_PIN U9  [get_ports {step_y}]
set_property PACKAGE_PIN U10 [get_ports {step_r}]
set_property PACKAGE_PIN T9  [get_ports {step_ena_n}]
set_property IOSTANDARD LVCMOS33 [get_ports {step_x step_y step_r step_ena_n}]

# ---- outputs, bank 35 (VCCO 1.8 V) -----------------------------------------
set_property PACKAGE_PIN G20 [get_ports {dir_x}]
set_property PACKAGE_PIN H18 [get_ports {dir_y}]
set_property PACKAGE_PIN H17 [get_ports {dir_r}]
set_property PACKAGE_PIN P14 [get_ports {ptt_io}]
set_property PACKAGE_PIN L14 [get_ports {sync_to_grbl}]
set_property IOSTANDARD LVCMOS18 [get_ports {dir_x dir_y dir_r ptt_io sync_to_grbl}]

# ---- input, the trigger coming back from the controller ---------------------
set_property PACKAGE_PIN L15 [get_ports {sync_from_grbl}]
set_property IOSTANDARD LVCMOS18 [get_ports {sync_from_grbl}]

# ---- inputs, bank 35 (VCCO 1.8 V, divided from the switch) ------------------
set_property PACKAGE_PIN G19 [get_ports {limit_x}]
set_property PACKAGE_PIN J18 [get_ports {limit_y}]
set_property PACKAGE_PIN H16 [get_ports {limit_r}]
set_property IOSTANDARD LVCMOS18 [get_ports {limit_x limit_y limit_r}]

# Step pulses are the one timing-critical thing here: keep them on a real clock
# domain and give the path a relaxed false path only after checking the pulse
# width by hand.
set_false_path -from [get_ports {limit_*}] -to [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz*clk_out1}]]
