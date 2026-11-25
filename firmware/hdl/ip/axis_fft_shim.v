// axis_fft_shim.v
//
// FFT / IFFT insert for the PlutoSDR (ADI hdl, projects/pluto) receive path.
//
// It sits between the AD9361 receive core and the DMA that feeds the IIO buffer
// and does three small, reviewable jobs:
//
//   1. frame alignment - ADI's DMA only produces TLAST at the end of a full
//      buffer, which can be megabytes. An FFT needs a transform frame (e.g.
//      1024 complex samples), so this block regenerates TLAST every FRAME_LEN
//      beats and passes it straight through when the upstream already frames.
//   2. direction control - drives the Xilinx FFT core's config channel so
//      software can switch between FFT (FWD_INV=0) and IFFT (FWD_INV=1) without
//      rebuilding the bitstream.
//   3. bypass - with cfg_enable low the samples go straight through, so the
//      same bitstream can be compared against the stock datapath.
//
// Data format: one complex sample per beat, {Q[15:0], I[15:0]}, AXI4-Stream,
// matching the AD9361 core's native 16-bit I/Q layout.
//
// The Xilinx FFT core is instantiated by name (xfft_0) and is created by
// hdl/system_bd_fft.tcl through create_ip. Simulation uses the behavioural
// xfft_0 from tb_axis_fft_shim.v.

`timescale 1ns / 1ps

module axis_fft_shim #(
    parameter integer DATA_WIDTH = 32,
    parameter integer FRAME_LEN  = 1024,
    parameter integer CNT_WIDTH  = 11          // must hold FRAME_LEN
) (
    input  wire                    aclk,
    input  wire                    aresetn,

    // ---- control (drive from an AXI GPIO, see system_bd_fft.tcl) ----------
    input  wire                    cfg_enable,   // 0 = bypass, 1 = FFT/IFFT
    input  wire                    cfg_fwd_inv,  // 0 = forward FFT, 1 = inverse
    output reg  [31:0]             frame_count,
    output reg  [31:0]             sample_count,

    // ---- slave: from the ADC core ----------------------------------------
    input  wire [DATA_WIDTH-1:0]   s_axis_tdata,
    input  wire                    s_axis_tvalid,
    output wire                    s_axis_tready,
    input  wire                    s_axis_tlast,

    // ---- master: to the DMA ----------------------------------------------
    output wire [DATA_WIDTH-1:0]   m_axis_tdata,
    output wire                    m_axis_tvalid,
    input  wire                    m_axis_tready,
    output wire                    m_axis_tlast
);

    localparam [CNT_WIDTH-1:0] LAST_BEAT = FRAME_LEN - 1;

    // ---- xfft-facing nets ------------------------------------------------
    wire                  fft_s_tready;
    wire [DATA_WIDTH-1:0] fft_m_tdata;
    wire                  fft_m_tvalid;
    wire                  fft_m_tlast;

    // ---------------------------------------------------------------------
    // config channel: hold FWD_INV until the core takes it, resend on change
    // ---------------------------------------------------------------------
    reg        cfg_dirty;
    reg        cfg_fwd_q;
    reg [15:0] cfg_tdata_r;
    reg        cfg_tvalid_r;
    wire       cfg_tready;

    always @(posedge aclk) begin
        if (!aresetn) begin
            cfg_dirty    <= 1'b1;
            cfg_fwd_q    <= 1'b0;
            cfg_tdata_r  <= 16'h0;
            cfg_tvalid_r <= 1'b0;
        end else begin
            if (cfg_fwd_q != cfg_fwd_inv)
                cfg_dirty <= 1'b1;

            if (cfg_tvalid_r && cfg_tready) begin
                cfg_tvalid_r <= 1'b0;
            end else if (cfg_dirty && !cfg_tvalid_r) begin
                cfg_tdata_r  <= {15'b0, cfg_fwd_inv};   // bit0 = FWD_INV
                cfg_tvalid_r <= 1'b1;
                cfg_fwd_q    <= cfg_fwd_inv;
                cfg_dirty    <= 1'b0;
            end
        end
    end

    // ---------------------------------------------------------------------
    // frame alignment: regenerate TLAST every FRAME_LEN accepted beats
    // ---------------------------------------------------------------------
    reg  [CNT_WIDTH-1:0] beat_cnt;

    wire take_in    = cfg_enable ? (s_axis_tvalid && fft_s_tready)
                                 : (s_axis_tvalid && m_axis_tready);
    wire frame_end  = (beat_cnt == LAST_BEAT);
    wire fft_s_tlast = frame_end | s_axis_tlast;

    always @(posedge aclk) begin
        if (!aresetn)
            beat_cnt <= {CNT_WIDTH{1'b0}};
        else if (take_in)
            beat_cnt <= frame_end ? {CNT_WIDTH{1'b0}} : beat_cnt + 1'b1;
    end

    // ---------------------------------------------------------------------
    // datapath
    // ---------------------------------------------------------------------
    assign s_axis_tready = cfg_enable ? fft_s_tready : m_axis_tready;

    assign m_axis_tdata  = cfg_enable ? fft_m_tdata      : s_axis_tdata;
    assign m_axis_tvalid = cfg_enable ? fft_m_tvalid     : s_axis_tvalid;
    assign m_axis_tlast  = cfg_enable ? fft_m_tlast      : (s_axis_tlast | frame_end);

    // output counters (32-bit, read by software through the GPIO block)
    wire out_fire = m_axis_tvalid && m_axis_tready;

    always @(posedge aclk) begin
        if (!aresetn) begin
            frame_count  <= 32'd0;
            sample_count <= 32'd0;
        end else if (out_fire) begin
            sample_count <= sample_count + 32'd1;
            if (m_axis_tlast) begin
                frame_count  <= frame_count + 32'd1;
                sample_count <= 32'd0;
            end
        end
    end

    // ---------------------------------------------------------------------
    // Xilinx FFT core (created by create_ip in system_bd_fft.tcl)
    // ---------------------------------------------------------------------
    xfft_0 u_fft (
        .aclk                        (aclk),
        .aresetn                     (aresetn),

        .s_axis_config_tdata         (cfg_tdata_r),
        .s_axis_config_tvalid        (cfg_tvalid_r),
        .s_axis_config_tready        (cfg_tready),

        .s_axis_data_tdata           (s_axis_tdata),
        .s_axis_data_tvalid          (s_axis_tvalid),
        .s_axis_data_tready          (fft_s_tready),
        .s_axis_data_tlast           (fft_s_tlast),

        .m_axis_data_tdata           (fft_m_tdata),
        .m_axis_data_tvalid          (fft_m_tvalid),
        .m_axis_data_tready          (m_axis_tready),
        .m_axis_data_tlast           (fft_m_tlast),

        .event_frame_started         (),
        .event_tlast_unexpected      (),
        .event_tlast_missing         (),
        .event_status_channel_halt   (),
        .event_data_in_channel_halt  (),
        .event_data_out_channel_halt ()
    );

endmodule
