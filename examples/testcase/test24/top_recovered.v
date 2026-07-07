module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, in27, in28, in29, in30, in31, in32, in33, in34, in35, in36, in37, in38, in39, in40, in41, in42, in43, in44, in45, in46, in47, in48, in49, in50, in51, in52, in53, in54, in55, in56, in57, in58, in59, in60, in61, in62, out1, out2);
  input [15:0] in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, in27, in28, in29, in30, in31, in32, in33, in34, in35, in36, in37, in38, in39, in40, in41, in42, in43, in44, in45, in46, in47, in48, in49, in50, in51, in52, in53, in54, in55, in56, in57, in58, in59, in60, in61;
  input in62;
  output [28:0] out1;
  output [16:0] out2;

  wire [16:0] p01 = {in1[15], in1} + {in2[15], in2};
  wire [16:0] p02 = {in3[15], in3} + {in4[15], in4};
  wire [16:0] p03 = {in5[15], in5} + {in6[15], in6};
  wire [16:0] p04 = {in7[15], in7} + {in8[15], in8};
  wire [16:0] p05 = {in9[15], in9} + {in10[15], in10};
  wire [16:0] p06 = {in11[15], in11} + {in12[15], in12};
  wire [16:0] p07 = {in13[15], in13} + {in14[15], in14};
  wire [16:0] p08 = {in15[15], in15} + {in16[15], in16};
  wire [16:0] p09 = {in17[15], in17} + {in18[15], in18};
  wire [16:0] p10 = {in19[15], in19} + {in20[15], in20};
  wire [16:0] p11 = {in21[15], in21} + {in22[15], in22};
  wire [16:0] p12 = {in23[15], in23} + {in24[15], in24};
  wire [16:0] p13 = {in25[15], in25} + {in26[15], in26};
  wire [16:0] p14 = {in27[15], in27} + {in28[15], in28};
  wire [16:0] p15 = {in29[15], in29} + {in30[15], in30};
  wire [16:0] p16 = {in31[15], in31} + {in32[15], in32};
  wire [16:0] p17 = {in33[15], in33} + {in34[15], in34};
  wire [16:0] p18 = {in35[15], in35} + {in36[15], in36};
  wire [16:0] p19 = {in37[15], in37} + {in38[15], in38};
  wire [16:0] p20 = {in39[15], in39} + {in40[15], in40};
  wire [16:0] p21 = {in41[15], in41} + {in42[15], in42};
  wire [16:0] p22 = {in43[15], in43} + {in44[15], in44};
  wire [16:0] p23 = {in45[15], in45} + {in46[15], in46};
  wire [16:0] p24 = {in47[15], in47} + {in48[15], in48};
  wire [16:0] p25 = {in49[15], in49} + {in50[15], in50};
  wire [16:0] p26 = {in51[15], in51} + {in52[15], in52};
  wire [16:0] p27 = {in53[15], in53} + {in54[15], in54};

  wire [28:0] u01 = p01;
  wire [28:0] u02 = p02;
  wire [28:0] u03 = p03;
  wire [28:0] u04 = p04;
  wire [28:0] u05 = p05;
  wire [28:0] u06 = p06;
  wire [28:0] u07 = p07;
  wire signed [28:0] s08 = $signed(p08);
  wire signed [28:0] s09 = $signed(p09);
  wire signed [28:0] s10 = $signed(p10);
  wire signed [28:0] s11 = $signed(p11);
  wire signed [28:0] s12 = $signed(p12);
  wire signed [28:0] s13 = $signed(p13);
  wire signed [28:0] s14 = $signed(p14);
  wire signed [28:0] s15 = $signed(p15);
  wire signed [28:0] s16 = $signed(p16);
  wire signed [28:0] s17 = $signed(p17);
  wire [28:0] u18 = p18;
  wire [28:0] u19 = p19;
  wire [28:0] u20 = p20;
  wire [28:0] u21 = p21;
  wire [28:0] u22 = p22;
  wire [28:0] u23 = p23;
  wire [28:0] u24 = p24;
  wire [28:0] u25 = p25;
  wire [28:0] u26 = p26;
  wire [28:0] u27 = p27;
  wire [28:0] u55 = in55;
  wire signed [28:0] s56 = $signed(in56);
  wire signed [28:0] s57 = $signed(in57);
  wire signed [28:0] s58 = $signed(in58);
  wire signed [28:0] s59 = $signed(in59);
  wire signed [28:0] s60 = $signed(in60);
  wire signed [28:0] s61 = $signed(in61);

  assign out1 =
      6 * u01 + 9 * (u02 + u06) + 12 * u03 + 14 * (u04 + u05) + 3 * u07
    - 6 * s08 - 27 * s09 - 38 * s10 - 48 * s11 - 55 * (s12 + s14)
    - 58 * s13 - 46 * s15 - 29 * s16 - 5 * s17
    + 27 * u18 + 65 * u19 + 109 * u20 + 156 * u21 + 203 * u22
    + 250 * u23 + 292 * u24 + 328 * u25 + 355 * u26 + 373 * u27
    + 378 * u55 + 16 * (s58 + s59 + s60 + s61 - s56 - s57);
  assign out2 = out1[28:12] + in62;
endmodule
