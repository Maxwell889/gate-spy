module top(in1,in2,out1,out2,out3,out4,out5,out6,out7,out8,out9,out10,out11,out12,out13,out14,out15,out16,out17,out18,out19,out20,out21);
  input [5:0] in1, in2;
  output [6:0] out1;
  output out2, out3, out4, out5, out6, out7, out8, out9, out10, out11, out12, out13, out14, out15, out16, out17, out18, out19, out20, out21;

  wire signed [6:0] diff = in1 - in2;
  assign out1 = diff;
  assign out2 = 1'b1;
  assign out3 = diff <= -60;
  assign out4 = diff >= -59;
  assign out5 = diff <= -48;
  assign out6 = diff >= -47;
  assign out7 = diff <= -36;
  assign out8 = diff >= -35;
  assign out9 = diff <= -24;
  assign out10 = diff >= -23;
  assign out11 = diff <= -12;
  assign out12 = diff >= 12;
  assign out13 = diff <= 23;
  assign out14 = diff >= 24;
  assign out15 = diff <= 35;
  assign out16 = diff >= 36;
  assign out17 = diff <= 47;
  assign out18 = diff >= 48;
  assign out19 = diff <= 59;
  assign out20 = diff >= 60;
  assign out21 = 1'b1;
endmodule