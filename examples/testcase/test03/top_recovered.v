module top(in1, in2, in3, out1);
  input [18:0] in1, in2, in3;
  output [19:0] out1;
  wire [18:0] in1, in2, in3;
  wire [19:0] out1;
  assign out1 = in1 + in2 + in3 + 19'd102;
endmodule
