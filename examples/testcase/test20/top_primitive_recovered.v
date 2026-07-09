module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, out1);
  input [8:0] in1, in8, in12, in16, in20, in24;
  input [7:0] in2, in9, in13, in17, in21, in25;
  input in3, in4, in6, in7, in10, in11, in14, in15, in18, in19, in22, in23;
  input [6:0] in5;
  output [16:0] out1;
  assign out1 = in5 + (in3 ? 0 : in1*in2) + (in6 ? 0 : in8*in9) + (in10 ? 0 : in12*in13) + (in14 ? 0 : in16*in17) + (in18 ? 0 : in20*in21) + (in22 ? 0 : in24*in25);
endmodule
