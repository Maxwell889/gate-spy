module top(in1, in2, in3, in4, out1);
  input [15:0] in1, in2, in3, in4;
  output [33:0] out1;
  wire [15:0] in1, in2, in3, in4;
  wire [33:0] out1;
  assign out1 = 123 * in1 + 456 * in2 + in3 + 789 * in4;
endmodule
