module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, out1);
  input [3:0] in1;
  input [25:0] in2;
  input in3;
  input [25:0] in4;
  input in5;
  input in6;
  input in7;
  input in8;
  input in9;
  input in10;
  input [6:0] in11;
  input in12;
  input [2:0] in13;
  input [2:0] in14;
  input [2:0] in15;
  input [2:0] in16;
  input [2:0] in17;
  output [25:0] out1;

  wire [25:0] w1 = {22'd0, in1};
  wire [25:0] w11 = {19'd0, in11};
  wire [25:0] wt = {23'd0, in13} * 26'd6;
  wire [25:0] wa = {23'd0, in14}, wb = {23'd0, in15}, wc = {23'd0, in16}, wd = {23'd0, in17};

  assign out1 = in3 ?
    (in6 ? (in2 - 26'd8 - wt) :
     in7 ? (in2 - 26'd1) :
     in8 ? (in2 - wa - wb + w1) :
     in9 ? (in2 - wb - wc + w1) :
     in10 ? (in2 - wb - wd + w1) :
     (in2 - w11 + w1)) :
    (in6 ? (in4 + 26'd8 + w1 + wt) :
     in7 ? (in4 + 26'd1 + w1) :
     in8 ? (in4 + wa + wb) :
     in9 ? (in4 + wb + wc) :
     in10 ? (in4 + wb + wd) :
     (in4 + w11));
endmodule
