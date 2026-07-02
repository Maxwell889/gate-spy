module top(in1, in2, in3, out1, out2);
  input [15:0] in1, in2, in3;
  output [31:0] out1, out2;
  wire [31:0] prod = in1 * in2;
  wire [31:0] base = prod + in3;
  assign out1 = base + 1;
  assign out2 = base - 1;
endmodule