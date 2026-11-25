// tb_axis_fft_shim.v - self-checking testbench for axis_fft_shim.v
//
//   iverilog -g2005 -o /tmp/tb_fft tb_axis_fft_shim.v ../ip/axis_fft_shim.v && vvp /tmp/tb_fft
//
// What is verified here is *our* logic, not the FFT arithmetic: frame-length
// TLAST regeneration, pass-through of an upstream TLAST, the FWD_INV config
// beat (and its resend when the direction changes), bypass mode and the
// counters. xfft_0 below is a deliberately dumb behavioural stand-in: forward
// passes the sample, inverse negates the Q half, so the bench can see which
// direction actually reached the core. The real Xilinx core is exercised by
// its own VIP after synthesis, not here.

`timescale 1ns / 1ps

// ---------------------------------------------------------------------------
// behavioural stand-in for the Xilinx FFT core (module name must match)
// ---------------------------------------------------------------------------
module xfft_0 #(
    parameter integer DATA_WIDTH = 32
) (
    input  wire                  aclk,
    input  wire                  aresetn,
    input  wire [15:0]           s_axis_config_tdata,
    input  wire                  s_axis_config_tvalid,
    output wire                  s_axis_config_tready,
    input  wire [DATA_WIDTH-1:0] s_axis_data_tdata,
    input  wire                  s_axis_data_tvalid,
    output wire                  s_axis_data_tready,
    input  wire                  s_axis_data_tlast,
    output reg  [DATA_WIDTH-1:0] m_axis_data_tdata,
    output reg                   m_axis_data_tvalid,
    input  wire                  m_axis_data_tready,
    output reg                   m_axis_data_tlast,
    output wire                  event_frame_started,
    output wire                  event_tlast_unexpected,
    output wire                  event_tlast_missing,
    output wire                  event_status_channel_halt,
    output wire                  event_data_in_channel_halt,
    output wire                  event_data_out_channel_halt
);
    reg fwd_inv = 1'b0;
    assign s_axis_config_tready = 1'b1;
    assign s_axis_data_tready   = m_axis_data_tready | ~m_axis_data_tvalid;

    always @(posedge aclk) begin
        if (!aresetn) begin
            fwd_inv            <= 1'b0;
            m_axis_data_tvalid <= 1'b0;
            m_axis_data_tlast  <= 1'b0;
            m_axis_data_tdata  <= 0;
        end else begin
            if (s_axis_config_tvalid && s_axis_config_tready)
                fwd_inv <= s_axis_config_tdata[0];

            if (s_axis_data_tvalid && s_axis_data_tready) begin
                // "IFFT" = negate Q, "FFT" = pass through. Not a real DFT.
                m_axis_data_tdata  <= fwd_inv ? {~s_axis_data_tdata[31:16] + 1'b1,
                                                 s_axis_data_tdata[15:0]}
                                              : s_axis_data_tdata;
                m_axis_data_tvalid <= 1'b1;
                m_axis_data_tlast  <= s_axis_data_tlast;
            end else if (m_axis_data_tvalid && m_axis_data_tready) begin
                m_axis_data_tvalid <= 1'b0;
                m_axis_data_tlast  <= 1'b0;
            end
        end
    end

    assign event_frame_started        = 1'b0;
    assign event_tlast_unexpected     = 1'b0;
    assign event_tlast_missing        = 1'b0;
    assign event_status_channel_halt  = 1'b0;
    assign event_data_in_channel_halt = 1'b0;
    assign event_data_out_channel_halt= 1'b0;
endmodule

// ---------------------------------------------------------------------------
module tb_axis_fft_shim;
    localparam FRAME = 8;

    reg         aclk = 0;
    reg         aresetn = 0;
    reg         cfg_enable = 0;
    reg         cfg_fwd_inv = 0;
    wire [31:0] frame_count, sample_count;

    reg  [31:0] s_tdata = 0;
    reg         s_tvalid = 0;
    wire        s_tready;
    reg         s_tlast = 0;

    wire [31:0] m_tdata;
    wire        m_tvalid;
    reg         m_tready = 1;
    wire        m_tlast;

    integer errors = 0;
    integer i;
    integer frames_seen = 0;
    integer beats_seen  = 0;
    reg [31:0] last_m = 0;
    integer last_tlast_at = -1;

    axis_fft_shim #(.DATA_WIDTH(32), .FRAME_LEN(FRAME), .CNT_WIDTH(4)) dut (
        .aclk(aclk), .aresetn(aresetn),
        .cfg_enable(cfg_enable), .cfg_fwd_inv(cfg_fwd_inv),
        .frame_count(frame_count), .sample_count(sample_count),
        .s_axis_tdata(s_tdata), .s_axis_tvalid(s_tvalid),
        .s_axis_tready(s_tready), .s_axis_tlast(s_tlast),
        .m_axis_tdata(m_tdata), .m_axis_tvalid(m_tvalid),
        .m_axis_tready(m_tready), .m_axis_tlast(m_tlast)
    );

    always #5 aclk = ~aclk;

    // watch the output stream
    always @(posedge aclk) begin
        if (aresetn && m_tvalid && m_tready) begin
            beats_seen = beats_seen + 1;
            last_m = m_tdata;
            if (m_tlast) begin
                frames_seen   = frames_seen + 1;
                last_tlast_at = beats_seen;
            end
        end
    end

    task push(input [15:0] ii, input [15:0] qq, input last);
        begin
            @(negedge aclk);
            s_tdata  = {qq, ii};
            s_tvalid = 1'b1;
            s_tlast  = last;
            @(posedge aclk);
            while (!s_tready) @(posedge aclk);
            @(negedge aclk);
            s_tvalid = 1'b0;
            s_tlast  = 1'b0;
        end
    endtask

    task check(input cond, input [255:0] msg);
        begin
            if (!cond) begin
                $display("FAIL: %s", msg);
                errors = errors + 1;
            end else
                $display("ok  : %s", msg);
        end
    endtask

    initial begin
        $display("axis_fft_shim testbench, FRAME_LEN=%0d", FRAME);
        repeat (4) @(posedge aclk);
        aresetn = 1'b1;
        repeat (4) @(posedge aclk);

        // ---- 1. bypass mode: samples must arrive unchanged -----------------
        cfg_enable  = 1'b0;
        cfg_fwd_inv = 1'b0;
        beats_seen  = 0;
        frames_seen = 0;
        for (i = 0; i < FRAME; i = i + 1)
            push(i[15:0], (i + 100), 1'b0);
        repeat (4) @(posedge aclk);
        check(beats_seen == FRAME, "bypass passes every beat");
        check(frames_seen == 1, "bypass frames once per FRAME_LEN beats (TLAST synthesised)");
        check(last_m == 32'h006B_0007, "bypass does not alter the samples");

        // ---- 2. forward FFT: data goes through the core --------------------
        cfg_enable  = 1'b1;
        cfg_fwd_inv = 1'b0;
        repeat (4) @(posedge aclk);
        beats_seen  = 0;
        frames_seen = 0;
        for (i = 0; i < FRAME; i = i + 1)
            push(i[15:0], 16'h0100, 1'b0);
        repeat (8) @(posedge aclk);
        check(beats_seen == FRAME, "FFT path returns every beat");
        check(frames_seen == 1, "FFT path frames once per FRAME_LEN beats");
        check(last_m[31:16] == 16'h0100, "forward FFT leaves Q untouched (stand-in model)");

        // ---- 3. inverse: config change must reach the core ------------------
        cfg_fwd_inv = 1'b1;
        repeat (4) @(posedge aclk);
        beats_seen  = 0;
        for (i = 0; i < FRAME; i = i + 1)
            push(i[15:0], 16'h0100, 1'b0);
        repeat (8) @(posedge aclk);
        check(beats_seen == FRAME, "IFFT path returns every beat");
        check(last_m[31:16] == 16'hFF00, "IFFT direction reached the core (Q negated by the model)");

        // ---- 4. upstream TLAST is passed through ---------------------------
        cfg_enable = 1'b0;
        beats_seen = 0; frames_seen = 0;
        push(16'h0001, 16'h0002, 1'b1);
        repeat (4) @(posedge aclk);
        check(frames_seen >= 1, "upstream TLAST still terminates the frame");

        // ---- 5. counters ---------------------------------------------------
        check(frame_count > 0, "frame counter advances");

        if (errors == 0) $display("\nPASS: all checks passed");
        else             $display("\nFAIL: %0d check(s) failed", errors);
        $finish;
    end

    initial begin
        #200000;
        $display("FAIL: timeout");
        $finish;
    end
endmodule
