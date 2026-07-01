module top(in1, in2, in3, out2);
  input [31:0] in1, in2, in3;
  output [40:0] out2;
  wire [63:0] mul = in1 * in3;
  wire [63:0] sum = mul + in2;
  assign out2 = sum >> 5;
endmodule