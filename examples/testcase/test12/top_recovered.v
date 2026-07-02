module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, out1);
  input signed [10:0] in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24;
  output signed [25:0] out1;
  assign out1 = in1*in2 + in3*in4 + in5*in6 + in7*in8 + in9*in10 + in11*in12 + in13*in14 + in15*in16 + in17*in18 + in19*in20 + in21*in22 + in23*in24;
endmodule