module top(in1, in2, in3, in4, in5, out1);
  input [31:0] in1, in2, in3;
  input [5:0] in4, in5;
  output [63:0] out1;
  assign out1 = (in1 << in4) + (in2 << in5) + in3;
endmodule