// tb_expio_io.v - self-checking bench for expio_io + sync_pulse.
//
//   iverilog -g2012 -o /tmp/tb_expio tb_expio_io.v expio_io.v sync_pulse.v && vvp /tmp/tb_expio
//
// Checks the two things that matter for the rig handshake:
//   * a software write to out[10] produces exactly one pulse, and holding the
//     bit high does not produce a train of them;
//   * with out[11] set, an edge arriving from the motion controller produces the
//     pulse in hardware, with no software in the path;
// plus the bit map of the status word and the motion outputs.
`timescale 1ns / 1ps

module tb_expio_io;

    localparam integer PULSE_CLKS = 8;      // short pulses so the bench is quick

    reg         clk = 0;
    reg  [11:0] gpio_out = 12'h000;
    wire [11:0] gpio_in;
    wire        step_x, step_y, step_r, step_ena_n;
    wire        dir_x, dir_y, dir_r, ptt_io;
    wire        sync_to_grbl;
    reg         sync_from_grbl = 0;
    reg         limit_x = 1, limit_y = 0, limit_r = 1;

    integer errors = 0;
    integer width_clks = 0;

    expio_io #(.PULSE_CLKS(PULSE_CLKS)) dut (
        .clk(clk), .gpio_out(gpio_out), .gpio_in(gpio_in),
        .step_x(step_x), .step_y(step_y), .step_r(step_r), .step_ena_n(step_ena_n),
        .dir_x(dir_x), .dir_y(dir_y), .dir_r(dir_r), .ptt_io(ptt_io),
        .sync_to_grbl(sync_to_grbl), .sync_from_grbl(sync_from_grbl),
        .limit_x(limit_x), .limit_y(limit_y), .limit_r(limit_r)
    );

    always #5 clk = ~clk;      // 100 MHz

    // measure the pulse width in clock cycles
    always @(posedge clk) begin
        if (sync_to_grbl) width_clks = width_clks + 1;
    end

    task check(input cond, input [255:0] msg);
        begin
            if (!cond) begin $display("FAIL: %s", msg); errors = errors + 1; end
            else            $display("ok  : %s", msg);
        end
    endtask

    initial begin
        $display("expio_io bench, PULSE_CLKS=%0d", PULSE_CLKS);
        repeat (4) @(posedge clk);

        // ---- 1. bit map of the motion outputs ----------------------------
        gpio_out = 12'h001; #20 check(step_x && !step_y && !step_r, "out[0] drives step_x");
        gpio_out = 12'h002; #20 check(!step_x && step_y, "out[1] drives step_y");
        gpio_out = 12'h004; #20 check(!step_y && step_r, "out[2] drives step_r");
        gpio_out = 12'h008; #20 check(step_ena_n == 1'b1 && !step_r, "out[3] drives enable");
        gpio_out = 12'h010; #20 check(dir_x && !dir_y && !dir_r, "out[4] drives dir_x");
        gpio_out = 12'h020; #20 check(!dir_x && dir_y, "out[5] drives dir_y");
        gpio_out = 12'h040; #20 check(!dir_y && dir_r, "out[6] drives dir_r");
        gpio_out = 12'h080; #20 check(ptt_io, "out[7] drives ptt_io");

        // ---- 2. status word ----------------------------------------------
        gpio_out = 12'h000; #40
        check(gpio_in[2:0] == 3'b101, "in[2:0] report the limit switches 101");

        // ---- 3. software-triggered pulse ----------------------------------
        width_clks = 0;
        gpio_out = 12'h400;                       // out[10] = 1
        repeat (4 * PULSE_CLKS + 8) @(posedge clk);
        check(width_clks > 0, "writing out[10] produces a pulse");
        check(width_clks <= PULSE_CLKS + 6 && width_clks >= PULSE_CLKS - 4,
              "pulse width matches PULSE_CLKS");
        check(sync_to_grbl == 1'b0, "pulse ends by itself while the bit stays high");
        check(gpio_in[4] == 1'b0 && gpio_in[5] == 1'b1, "busy drops, toggle advanced");

        // holding the bit high must not make a train of pulses
        width_clks = 0;
        repeat (10 * PULSE_CLKS) @(posedge clk);
        check(width_clks == 0, "a steady high on out[10] does not retrigger");

        // ---- 4. hardware echo: edge from the motion controller ------------
        gpio_out = 12'h800;                       // out[11] = src_sel
        repeat (10) @(posedge clk);
        width_clks = 0;
        sync_from_grbl = 1'b1;                    // rising edge in
        repeat (4 * PULSE_CLKS + 8) @(posedge clk);
        sync_from_grbl = 1'b0;
        check(width_clks > 0, "an edge from the controller triggers the pulse in hardware");
        repeat (6) @(posedge clk);      // two synchroniser stages plus margin
        check(gpio_in[3] == 1'b0, "ext level follows the input after it falls");

        // ---- 5. no source selected: no pulse -----------------------------
        gpio_out = 12'h000;
        repeat (10) @(posedge clk);
        width_clks = 0;
        sync_from_grbl = 1'b1;
        repeat (4 * PULSE_CLKS) @(posedge clk);
        sync_from_grbl = 1'b0;
        check(width_clks == 0, "with src_sel low an input edge is ignored");

        if (errors == 0) $display("\nPASS: all checks passed");
        else             $display("\nFAIL: %0d check(s) failed", errors);
        $finish;
    end

    initial begin
        #200000; $display("FAIL: timeout"); $finish;
    end
endmodule
