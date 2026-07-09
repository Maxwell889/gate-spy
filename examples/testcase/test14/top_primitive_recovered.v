module top(in1, in2, in3, out1);
  input [2:0] in1;
  input [3:0] in2;
  input [8:0] in3;
  output out1;

  wire signed [8:0] s1 = in1;
  wire signed [8:0] s2 = in2;
  wire signed [8:0] s3 = in3;
  assign out1 = s3 < s1 - s2;
endmodule