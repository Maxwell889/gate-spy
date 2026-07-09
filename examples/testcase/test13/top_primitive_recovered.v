module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, out1, out2, out3, out4, out5);
  input signed [11:0] in1, in2, in7;
  input in3, in4, in5, in6, in8, in9, in10, in11, in12, in13, in14, in15;
  output out1, out2, out3, out4, out5;

  wire signed [12:0] d = in1 - in2;
  wire signed [12:0] r = in2 - in7;
  wire lt = d < r;
  wire gt = d > r;
  wire slt = in1 < in7;
  wire sgt = in1 > in7;

  assign out1 = in5 ? (in3 ? gt : slt) : (in3 ? sgt : lt);
  assign out2 = in8 ? (in10 ? gt : slt) : (in10 ? sgt : lt);
  assign out3 = in12 ? (in14 ? gt : slt) : (in14 ? sgt : lt);
  assign out4 = r < 0;
  assign out5 = d < 0;
endmodule
