module top(in1, in2, in3, in4, in5, out1, out2, out3);
  input [15:0] in1, in2, in3, in4, in5;
  output [16:0] out1, out2;
  output [33:0] out3;
  wire [15:0] in1, in2, in3, in4, in5;
  wire [16:0] out1, out2;
  wire [33:0] out3;
  assign out1 = in1 + in2;
  assign out2 = in3 - out1;
  assign out3 = in1 * (in2 + in3 + in4) + in5;
endmodule
