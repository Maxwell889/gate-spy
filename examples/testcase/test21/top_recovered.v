module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, in27, in28, in29, in30, in31, out1);
  input [6:0] in1, in2, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, in27, in28, in29, in30, in31;
  input in3, in4;
  output [12:0] out1;
  assign out1 = 32 + (in3 ? in1+in2+in19+in20 : in5+in6+in21+in22) + 2*(in7+in8+in9+in10+in11+in12+in13+in14+in15+in16+in17+in18) + 4*(in23+in24+in25+in26+in27+in28+in29+in30+in31);
endmodule
