// expio_io.v - maps an AXI GPIO onto the expansion-header pins as named signals.
//
// The block design sees one 12-bit output word and one 12-bit input word; this
// wrapper gives those bits the names used in expio.xdc, so the constraints stay
// readable and a step generator can later replace the direct assigns without
// touching the pinout.
//
// Bit map (see expio.tcl / expio.xdc):
//   out[0] step_x   out[4] dir_x      in[0] limit_x
//   out[1] step_y   out[5] dir_y      in[1] limit_y
//   out[2] step_r   out[6] dir_r      in[2] limit_r
//   out[3] ena_n    out[7] spare_out0
//                   out[8] spare_out1
//                   out[9] ptt_io
//   out[11:10] unused
`timescale 1ns / 1ps

module expio_io (
    input  wire [11:0] gpio_out,
    output wire [11:0] gpio_in,

    // bank 13, 3.3 V
    output wire        step_x,
    output wire        step_y,
    output wire        step_r,
    output wire        step_ena_n,

    // bank 35, 1.8 V
    output wire        dir_x,
    output wire        dir_y,
    output wire        dir_r,
    output wire        spare_out0,
    output wire        spare_out1,
    output wire        ptt_io,

    // bank 35, 1.8 V, divided from the switches
    input  wire        limit_x,
    input  wire        limit_y,
    input  wire        limit_r
);

    assign step_x     = gpio_out[0];
    assign step_y     = gpio_out[1];
    assign step_r     = gpio_out[2];
    assign step_ena_n = gpio_out[3];

    assign dir_x      = gpio_out[4];
    assign dir_y      = gpio_out[5];
    assign dir_r      = gpio_out[6];
    assign spare_out0 = gpio_out[7];
    assign spare_out1 = gpio_out[8];
    assign ptt_io     = gpio_out[9];

    assign gpio_in    = {9'b0, limit_r, limit_y, limit_x};

endmodule
