module top(in1, in2, in3, in4, in5, out1, out2);
  input [25:0] in1;
  input [25:0] in5;
  input in2;
  input in3;
  input in4;
  output [25:0] out1;
  output [25:0] out2;

  assign out1 = in1 + in2;
  assign out2 = in5 + (in3 ? 26'd33554431 : out1);
endmodule