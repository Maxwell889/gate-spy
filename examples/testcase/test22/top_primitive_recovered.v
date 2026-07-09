module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, out1);
  input [31:0] in1, in2, in3, in4, in5, in7, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26;
  input [30:0] in6, in8;
  output [68:0] out1;

  assign out1 = {{37{in1[31]}}, in1} * {{37{in2[31]}}, in2}
              + {{37{in3[31]}}, in3} * {{37{in4[31]}}, in4}
              + in5 * in6
              + in7 * in8
              + {{37{in9[31]}}, in9} * {{37{in10[31]}}, in10}
              + {{37{in11[31]}}, in11} * {{37{in12[31]}}, in12}
              + {{37{in13[31]}}, in13} * {{37{in14[31]}}, in14}
              + {{37{in15[31]}}, in15} * {{37{in16[31]}}, in16}
              + {{37{in17[31]}}, in17} * {{37{in18[31]}}, in18}
              + {{37{in19[31]}}, in19} * {{37{in20[31]}}, in20}
              + {{37{in21[31]}}, in21} * {{37{in22[31]}}, in22}
              + {{37{in23[31]}}, in23} * {{37{in24[31]}}, in24}
              + {{37{in25[31]}}, in25} * {{37{in26[31]}}, in26};
endmodule