module top(in1, in2, in3, out1, out2, out3, out4, out5, out6);
  input [30:0] in1;
  input [31:0] in2;
  input [31:0] in3;
  output [32:0] out1;
  output out2;
  output out3;
  output [32:0] out4;
  output out5;
  output out6;

  wire [32:0] a = in1;
  wire [32:0] b = in2;
  wire [32:0] c = in3;
  wire [32:0] a2 = a + 33'd2;
  wire signed [32:0] d_ba = b - a2;
  wire signed [32:0] d_ca = c - a2;

  assign out1 = d_ba;
  assign out2 = d_ba > 33'sd7;
  assign out3 = d_ba < -33'sd4;
  assign out4 = d_ca;
  assign out5 = d_ca > 33'sd7;
  assign out6 = d_ca < -33'sd4;
endmodule
