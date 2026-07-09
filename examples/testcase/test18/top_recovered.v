module top(in1, in2, in3, in4, in5, in6, out1, out2, out3, out4, out5);
  input [1:0] in1, in2;
  input signed [4:0] in3, in5;
  input [2:0] in4, in6;
  output signed [4:0] out1, out3;
  output [4:0] out5;
  output out2, out4;

  wire [2:0] addend3 = 3'd1 - in1;
  wire [4:0] addend = addend3;

  assign out1 = in2 + addend;
  assign out3 = in4 + addend;
  assign out5 = in6 + addend;
  assign out2 = in3 > out1;
  assign out4 = in5 > out3;
endmodule
