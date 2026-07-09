module top(in1, in2, in3, in4, out1, out2, out3, out4);
  input [33:0] in1;
  input [34:0] in2, in3, in4;
  output out1, out2, out3, out4;
  wire [34:0] d = in1 - 1;
  assign out1 = $signed(d) >= $signed(in2);
  assign out2 = d == in3;
  assign out3 = d == in4;
  assign out4 = $signed(d) > $signed(in4);
endmodule