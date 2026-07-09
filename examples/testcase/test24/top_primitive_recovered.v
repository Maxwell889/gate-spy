module top(in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, in27, in28, in29, in30, in31, in32, in33, in34, in35, in36, in37, in38, in39, in40, in41, in42, in43, in44, in45, in46, in47, in48, in49, in50, in51, in52, in53, in54, in55, in56, in57, in58, in59, in60, in61, in62, out1, out2);
  input [15:0] in1, in2, in3, in4, in5, in6, in7, in8, in9, in10, in11, in12, in13, in14, in15, in16, in17, in18, in19, in20, in21, in22, in23, in24, in25, in26, in27, in28, in29, in30, in31, in32, in33, in34, in35, in36, in37, in38, in39, in40, in41, in42, in43, in44, in45, in46, in47, in48, in49, in50, in51, in52, in53, in54, in55, in56, in57, in58, in59, in60, in61;
  input in62;
  output [28:0] out1;
  output [16:0] out2;

  wire [16:0] p01 = $signed(in1) + $signed(in2);
  wire [16:0] p02 = $signed(in3) + $signed(in4);
  wire [16:0] p03 = $signed(in5) + $signed(in6);
  wire [16:0] p04 = $signed(in7) + $signed(in8);
  wire [16:0] p05 = $signed(in9) + $signed(in10);
  wire [16:0] p06 = $signed(in11) + $signed(in12);
  wire [16:0] p07 = $signed(in13) + $signed(in14);
  wire [16:0] p08 = $signed(in15) + $signed(in16);
  wire [16:0] p09 = $signed(in17) + $signed(in18);
  wire [16:0] p10 = $signed(in19) + $signed(in20);
  wire [16:0] p11 = $signed(in21) + $signed(in22);
  wire [16:0] p12 = $signed(in23) + $signed(in24);
  wire [16:0] p13 = $signed(in25) + $signed(in26);
  wire [16:0] p14 = $signed(in27) + $signed(in28);
  wire [16:0] p15 = $signed(in29) + $signed(in30);
  wire [16:0] p16 = $signed(in31) + $signed(in32);
  wire [16:0] p17 = $signed(in33) + $signed(in34);
  wire [16:0] p18 = $signed(in35) + $signed(in36);
  wire [16:0] p19 = $signed(in37) + $signed(in38);
  wire [16:0] p20 = $signed(in39) + $signed(in40);
  wire [16:0] p21 = $signed(in41) + $signed(in42);
  wire [16:0] p22 = $signed(in43) + $signed(in44);
  wire [16:0] p23 = $signed(in45) + $signed(in46);
  wire [16:0] p24 = $signed(in47) + $signed(in48);
  wire [16:0] p25 = $signed(in49) + $signed(in50);
  wire [16:0] p26 = $signed(in51) + $signed(in52);
  wire [16:0] p27 = $signed(in53) + $signed(in54);

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
