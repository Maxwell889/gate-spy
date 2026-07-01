module top(in1, in2, in3, in4, out1);
  input [15:0] in1, in2, in3;
  input [1:0] in4;
  output [31:0] out1;
  assign out1 = in4 ? ((in4[1] ? in1 : in3) * (in4[0] ? in2 : in3)) : 0;
endmodule