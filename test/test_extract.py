"""Tests for non-destructive XOR / adder extraction."""

from src.circuit import Circuit, extract_xor, extract_adders


def build(body, ports, decls):
    return Circuit.from_string(f"module m({ports}); {decls} {body} endmodule")


# y = (a & !b) | (!a & b)  -- a XOR built from AND/OR/NOT (exercises NOT-as-node)
XOR_AIG = ("""
    NOT u0 ( .A(a), .Y(na) ); NOT u1 ( .A(b), .Y(nb) );
    AND u2 ( .A(a), .B(nb), .Y(t0) ); AND u3 ( .A(na), .B(b), .Y(t1) );
    OR  u4 ( .A(t0), .B(t1), .Y(y) );
""", "a,b,y", "input a,b; output y; wire a,b,y,na,nb,t0,t1;")

HA = ("XOR g0 ( .A(a), .B(b), .Y(s) ); AND g1 ( .A(a), .B(b), .Y(c) );",
      "a,b,s,c", "input a,b; output s,c; wire a,b,s,c;")

FA = ("""
    XOR g0 ( .A(a), .B(b), .Y(axb) ); XOR g1 ( .A(axb), .B(cin), .Y(s) );
    AND g2 ( .A(a), .B(b), .Y(ab) );  AND g3 ( .A(cin), .B(axb), .Y(t) );
    OR  g4 ( .A(ab), .B(t), .Y(co) );
""", "a,b,cin,s,co", "input a,b,cin; output s,co; wire a,b,cin,s,co,axb,ab,t;")


def test_xor_extraction_and_chains():
    assert len(extract_xor(build(*HA))) == 1
    assert len(extract_xor(build(*XOR_AIG))) == 1
    chain = extract_xor(build("XOR g0 ( .A(a), .B(b), .Y(t) ); "
                               "XOR g1 ( .A(t), .B(c), .Y(y) );",
                              "a,b,c,y", "input a,b,c; output y; wire a,b,c,y,t;"))
    assert len(chain.longest_chain()) == 2
    assert "No XOR gates" in extract_xor(build(
        "AND g ( .A(a), .B(b), .Y(y) );",
        "a,b,y", "input a,b; output y; wire a,b,y;")).report()


def test_adder_extraction():
    assert len(extract_adders(build(*HA)).half_adders()) == 1
    assert len(extract_adders(build(*XOR_AIG)).half_adders()) == 0
    aig_ha = "NOT u0 ( .A(a), .Y(na) ); NOT u1 ( .A(b), .Y(nb) ); " \
             "AND u2 ( .A(a), .B(nb), .Y(t0) ); AND u3 ( .A(na), .B(b), .Y(t1) ); " \
             "OR u4 ( .A(t0), .B(t1), .Y(s) ); AND u5 ( .A(a), .B(b), .Y(c) );"
    ha = extract_adders(build(aig_ha, "a,b,s,c",
                              "input a,b; output s,c; wire a,b,s,c,na,nb,t0,t1;"))
    assert len(ha.half_adders()) == 1
    fa = extract_adders(build(*FA))
    assert len(fa.full_adders()) == 1 and len(fa.half_adders()) == 1


def test_adder_trees():
    ha = extract_adders(build(*HA))
    assert ha.trees() == [ha.adders] and "adder trees  : 1" in ha.report()
    assert len(extract_adders(build(*FA)).trees()) == 2


def test_extraction_is_non_destructive():
    c = build(*HA)
    before = len(c.nodes)
    extract_adders(c)
    extract_xor(c)
    assert len(c.nodes) == before
    assert c.simulate({"a": 1, "b": 1}) == {"s": 0, "c": 1}


def test_subgraph_and_verilog_roundtrip(out_dir):
    c = build(*FA)
    from src.circuit.subgraph import extract_subgraph
    from src.circuit.verilog_writer import write_verilog

    sub = extract_subgraph(c, ["axb"], ["a", "b"])
    p = out_dir / "sub.v"
    write_verilog(sub, str(p))
    c2 = Circuit.from_file(str(p))
    assert c2.simulate({"a": 1, "b": 1}) == {"axb": 0}
    assert c2.simulate({"a": 0, "b": 1}) == {"axb": 1}

    sub2 = extract_subgraph(c, ["co"], ["a", "b"])
    assert set(sub2.input_nets) == {"a", "b", "cin"}
    p2 = out_dir / "sub2.v"
    write_verilog(sub2, str(p2))
    c3 = Circuit.from_file(str(p2))
    assert c3.simulate({"a": 1, "b": 1, "cin": 0}) == {"co": 1}
    assert c3.simulate({"a": 1, "b": 0, "cin": 1}) == {"co": 1}
    assert c3.simulate({"a": 0, "b": 0, "cin": 1}) == {"co": 0}

    sub3 = extract_subgraph(c, ["co"])
    assert set(sub3.input_nets) == {"a", "b", "cin"}


def test_to_dot(out_dir):
    c = build(*FA)
    path = out_dir / "fa.dot"
    extract_adders(c).to_dot(str(path))
    content = path.read_text()
    assert content.startswith("digraph") and content.strip().endswith("}")
    assert "FA 0" in content
    assert content.count("[label=") == len(c.nodes)
