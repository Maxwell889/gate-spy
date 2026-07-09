module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, out1, out2, out3, out4, out5);
  input signed [11:0] in1, in2, in7;
  input in3, in4, in5, in6, in8, in9, in10, in11, in12, in13, in14, in15;
  output out1, out2, out3, out4, out5;

  wire signed [12:0] a = in1;
  wire signed [12:0] b = in2;
  wire signed [12:0] c = in7;
  wire signed [12:0] d = a - b;
  wire signed [12:0] r = b - c;
  wire lt = d < r;
  wire gt = d > r;
  wire slt = a < c;
  wire sgt = a > c;

  assign out1 = in3 ? (in5 ? gt : sgt) : (in5 ? slt : lt);
  assign out2 = in10 ? (in8 ? gt : sgt) : (in8 ? slt : lt);
  assign out3 = in14 ? (in12 ? gt : sgt) : (in12 ? slt : lt);
  assign out4 = b < c;
  assign out5 = a < b;
endmodule
