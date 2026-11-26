// sync_pulse.v - a firmware-triggerable pulse out to the motion controller,
//                and an edge detector for the trigger coming back in.
//
// This is the piece that makes "move, measure, tell GRBL the window is done"
// deterministic instead of hoping a USB round trip lands in time.
//
// Two sources drive the output pulse:
//   * software: write 1 to the sync bit of the AXI GPIO. The bit is treated as
//     an EVENT (rising edge only), so it does not have to be cleared - writing
//     it out of the way on the next write is enough.
//   * hardware: a rising edge on sync_from_grbl (or any other PL event tied to
//     ext_in), when the source-select bit is set. That is what turns the pulse
//     into an echo/handshake with no software in the path.
//
// Both inputs are synchronised with two flip-flops before use. The pulse width
// is a parameter: PULSE_CLKS cycles of aclk. At 100 MHz the default 1000 is
// 10 microseconds, comfortably longer than GRBL's status poll and short enough
// not to look like a stuck line.
`timescale 1ns / 1ps

module sync_pulse #(
    parameter integer PULSE_CLKS = 1000,
    parameter integer CNT_W      = 16
) (
    input  wire clk,
    input  wire rst,              // synchronous, active high

    input  wire sw_trig,          // GPIO bit: rising edge fires a pulse
    input  wire src_sel,          // GPIO bit: 1 = also echo ext_in edges
    input  wire ext_in,           // from the motion controller

    output reg  pulse_out,        // to the motion controller
    output wire ext_level,        // synchronised level, readable in software
    output wire busy,             // pulse in progress
    output wire pulse_count_lo    // toggles every pulse, a cheap activity flag
);

    // ---- synchronisers ----------------------------------------------------
    reg [2:0] sw_sync;
    reg [2:0] ext_sync;
    always @(posedge clk) begin
        if (rst) begin
            sw_sync  <= 3'b0;
            ext_sync <= 3'b0;
        end else begin
            sw_sync  <= {sw_sync[1:0],  sw_trig};
            ext_sync <= {ext_sync[1:0], ext_in};
        end
    end

    wire sw_rise  = sw_sync[1]  & ~sw_sync[2];
    wire ext_rise = ext_sync[1] & ~ext_sync[2];

    // ---- pulse generator --------------------------------------------------
    reg [CNT_W-1:0] cnt;
    reg             toggle;

    always @(posedge clk) begin
        if (rst) begin
            pulse_out <= 1'b0;
            cnt       <= {CNT_W{1'b0}};
            toggle    <= 1'b0;
        end else if ((sw_rise) || (src_sel && ext_rise)) begin
            pulse_out <= 1'b1;
            cnt       <= PULSE_CLKS[CNT_W-1:0];
            toggle    <= ~toggle;
        end else if (cnt != {CNT_W{1'b0}}) begin
            cnt <= cnt - 1'b1;
            if (cnt == 1)
                pulse_out <= 1'b0;
        end
    end

    assign ext_level      = ext_sync[1];
    assign busy           = (cnt != {CNT_W{1'b0}});
    assign pulse_count_lo = toggle;

endmodule
