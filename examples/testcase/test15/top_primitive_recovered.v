module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, out1, out2, out3);
  input [12:0] in1;
  input [17:0] in2;
  input [31:0] in3, in7, in9;
  input in4, in5, in6, in8;
  input [4:0] in10;
  input [15:0] in11;
  output [32:0] out1;
  output [31:0] out2, out3;
  wire [32:0] mac;
  assign mac = (in1 * in2) + in3;
  assign out1 = mac;
  assign out2 = mac + 32'd64;
  assign out3 = in11 + (in4 ? out2 : (in5 ? mac : in6 ? in9 + in10 * in1 : in7));
endmodule