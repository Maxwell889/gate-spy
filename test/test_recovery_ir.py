"""Tests for graph-only recovery reasoning IR and local experience memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.circuit import Circuit
from src.session import CircuitSession


def _session_with_tmp_memory(tmp_path: Path) -> CircuitSession:
    return CircuitSession(
        ir_run_base=str(tmp_path / "runs"),
        recovery_kb_dir=str(tmp_path / "kb"),
    )


def _mux_session(tmp_path: Path) -> CircuitSession:
    circuit = Circuit.from_string(
        "module top(s,a,b,y); input [1:0] a,b; input s; output [1:0] y; "
        "wire [1:0] a,b,y; wire s; "
        "wire ns,t0,t1,t2,t3; "
        "NOT g0 ( .A(s), .Y(ns) ); "
        "AND g1 ( .A(a[0]), .B(ns), .Y(t0) ); "
        "AND g2 ( .A(b[0]), .B(s), .Y(t1) ); "
        "OR g3 ( .A(t0), .B(t1), .Y(y[0]) ); "
        "AND g4 ( .A(a[1]), .B(ns), .Y(t2) ); "
        "AND g5 ( .A(b[1]), .B(s), .Y(t3) ); "
        "OR g6 ( .A(t2), .B(t3), .Y(y[1]) ); "
        "endmodule"
    )
    session = _session_with_tmp_memory(tmp_path)
    session.circuit = circuit
    return session


def _has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_has_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_has_key(item, key) for item in value)
    return False


def test_reasoning_graph_records_nodes_without_raw_tool_logs(tmp_path: Path):
    session = _session_with_tmp_memory(tmp_path)
    load = session.load(
        "examples/iccad22_test01.v",
        record_ir=True,
        case_id="test01",
        out_dir=str(tmp_path / "runs"),
    )
    assert "Recovery reasoning graph run" in load

    session.plan_recovery(format="json")
    session.record_recovery_graph_node(
        "problem",
        target="out1",
        summary="native search would be unnecessary for an obvious sum",
        data={"failure_type": "wrong_tool", "text": "x" * 5000},
    )
    observation = json.loads(session.record_recovery_graph_node(
        "observation",
        target="out1",
        summary="directed rows match a+b+c",
        data={
            "samples": [
                {
                    "word_values": {"in1": 1, "in2": 2, "in3": 3, "out1": 6},
                    "bit_values": {"in1[0]": 1, "in2[1]": 1},
                }
            ],
            "text_preview": "not durable knowledge",
        },
        format="json",
    ))
    good = session.validate_expr(
        "out1", "in1 + in2 + in3", inputs=["in1", "in2", "in3"])
    bad = session.validate_expr("out1", "in1", inputs=["in1"], detail=True)
    assert "C1" in good
    assert "F1" in bad

    exported = json.loads(session.export_recovery_ir(format="json"))
    run_dir = Path(exported["run_dir"])
    assert run_dir.exists()
    assert {p.name for p in run_dir.iterdir()} <= {
        "graph.json",
        "graph.jsonl",
        "summary.json",
        "final_candidate.v",
    }
    assert (run_dir / "graph.json").exists()
    assert (run_dir / "graph.jsonl").exists()
    assert (run_dir / "summary.json").exists()
    assert not (run_dir / "events.jsonl").exists()
    assert not (run_dir / "run.json").exists()
    assert not (run_dir / "candidates").exists()
    assert not (run_dir / "reports").exists()
    assert all(path.stat().st_size < 120_000 for path in run_dir.iterdir())

    nodes = exported["nodes"]
    node_types = [node["node_type"] for node in nodes]
    assert "problem" in node_types
    assert "observation" in node_types
    assert "validation" in node_types
    assert any(node["data"].get("candidate_id") == "C1" for node in nodes)
    assert any(node["data"].get("candidate_id") == "F1" for node in nodes)
    assert not _has_key(observation, "text_preview")
    assert not _has_key(observation, "bit_values")
    assert len(json.dumps(observation)) < 5000


def test_experience_promotion_skips_unverified_relation_and_slice_evidence(tmp_path: Path):
    session = _mux_session(tmp_path)
    session.start_recovery_run(case_id="mux", out_dir=str(tmp_path / "runs"))
    session.record_recovery_graph_node(
        "relation",
        target="y",
        summary="slice branch appears to equal a",
        data={
            "relation": "mux false branch equals a",
            "expression": "a",
            "suggested_experiment": "validate full-support branch under s=0",
        },
    )
    sliced = session.validate_expr(
        "y",
        "a",
        fixed_inputs={"s": 0},
        validate_scope="expr_only",
        exhaustive="never",
        pattern_num=8,
        validation_num=8,
    )
    assert "slice-sample-exact" in sliced

    dry = json.loads(session.promote_recovery_experience(dry_run=True, format="json"))
    assert dry["templates"] == 0
    assert dry["relations"] == 0
    assert dry["experiences"] == 0
    assert dry["records"] == []


def test_experience_promotion_and_query_return_experiment_provenance(tmp_path: Path):
    session = _session_with_tmp_memory(tmp_path)
    session.load(
        "examples/iccad22_test01.v",
        record_ir=True,
        case_id="test01",
        out_dir=str(tmp_path / "runs"),
    )
    session.record_recovery_graph_node(
        "relation",
        target="out1",
        summary="adder relation from directed rows",
        data={
            "relation": "three-input unsigned sum",
            "trigger": "output word has same support as in1,in2,in3",
            "suggested_experiment": "validate in1 + in2 + in3 with full support",
            "expression": "(in1 + in2) + in3",
        },
    )
    session.validate_expr(
        "out1", "in1 + in2 + in3", inputs=["in1", "in2", "in3"])

    write = json.loads(
        session.promote_recovery_experience(dry_run=False, format="json"))
    assert write["written"]["templates"] == 0
    assert write["written"]["relations"] == 1
    assert write["written"]["experiences"] == 1

    hits = json.loads(session.query_recovery_experience(
        target="out1",
        inputs=["in1", "in2", "in3"],
        limit=5,
        format="json",
    ))
    assert hits
    assert all(hit["expression"] == "" for hit in hits)
    assert any("validate" in hit["suggested_experiment"] for hit in hits)
    assert all(hit["provenance"] for hit in hits)


def test_experience_promotion_induces_offset_template_family(tmp_path: Path):
    session = _session_with_tmp_memory(tmp_path)
    session.load(
        "examples/testcase/test17/top_primitive.v",
        record_ir=True,
        case_id="test17",
        out_dir=str(tmp_path / "runs"),
    )
    session.record_recovery_graph_node(
        "relation",
        target="out2",
        summary="offset comparator from boundary rows",
        data={
            "relation": "zero-extended offset comparator",
            "trigger": "directed samples show in2 boundary is in1 plus a constant",
            "suggested_experiment": "scan small offset constants and validate full-support comparator",
            "expression": "{1'd0, in2} >= ({2'd0, in1} + 33'd10)",
        },
    )
    accepted = session.validate_expr(
        "out2",
        "{1'b0, in2} >= ({2'b0, in1} + 33'd10)",
        inputs=["in1", "in2"],
        exhaustive="never",
        pattern_num=64,
        validation_num=128,
    )
    assert "sample-exact" in accepted

    write = json.loads(
        session.promote_recovery_experience(dry_run=False, format="json"))
    assert write["template_families"] == 1
    assert write["written"]["template_families"] == 1

    hits = json.loads(session.query_recovery_experience(
        target="out2",
        inputs=["in1", "in2"],
        limit=5,
        format="json",
    ))
    assert hits
    assert all(hit["expression"] == "" for hit in hits)
    families = [
        hit.get("template_family", {}).get("family")
        for hit in hits
    ]
    assert "offset_comparator" in families
    assert any(
        "scan" in hit.get("suggested_experiment", "")
        for hit in hits
    )


def test_test16_reasoning_graph_successor_experience(tmp_path: Path):
    session = _session_with_tmp_memory(tmp_path)
    session.load(
        "examples/testcase/test16/top_primitive.v",
        record_ir=True,
        case_id="test16",
        out_dir=str(tmp_path / "runs"),
    )
    session.record_recovery_graph_node(
        "problem",
        target="out2",
        summary="greater-than hypothesis fails at in1=2,in3=0",
        data={
            "failure_type": "failed_validation",
            "trigger": "CEX contradicts monotone greater-than",
            "suggested_experiment": "enumerate small in1 around zero and max in3",
        },
        promotable=True,
    )
    failed = session.validate_expr(
        "out2",
        "$unsigned(in1) > $unsigned(in3)",
        inputs=["in1", "in3"],
        fixed_inputs={"in1": 2, "in3": 0},
        exhaustive="never",
        pattern_num=1,
        validation_num=1,
    )
    assert "failed validation" in failed
    session.record_recovery_graph_node(
        "experiment",
        target="out2",
        summary="directed successor table over edge values",
        data={
            "script_path": "/private/tmp/gate-spy-test16-successor/check.py",
            "word_samples": [
                {"in1": 0, "in3": (1 << 35) - 1, "out2": 1},
                {"in1": 1, "in3": 0, "out2": 1},
                {"in1": 2, "in3": 0, "out2": 0},
            ],
        },
    )
    session.record_recovery_graph_node(
        "observation",
        target="out2",
        summary="out2 follows in3 == zero-extended in1 minus one",
        data={"relation_family": "successor equality"},
    )
    session.record_recovery_graph_node(
        "relation",
        target="out2",
        summary="successor equality relation",
        data={
            "relation": "zero-extended predecessor equality",
            "trigger": "greater-than CEX isolates predecessor edge rows",
            "suggested_experiment": "validate ({1'd0, in1} - 35'd1) == in3",
            "expression": "({1'd0, in1} - 35'd1) == in3",
        },
    )
    accepted = session.validate_expr(
        "out2",
        "(({1'b0, in1}) - 35'd1) == in3",
        inputs=["in1", "in3"],
        exhaustive="never",
        pattern_num=32,
        validation_num=64,
    )
    assert "sample-exact" in accepted

    dry = json.loads(session.promote_recovery_experience(dry_run=True, format="json"))
    assert dry["templates"] == 0
    assert dry["relations"] == 1
    assert dry["experiences"] == 2
    assert dry["failures"] == 1
    assert any(record["negative"] for record in dry["records"])
    assert any(
        record["relation"] == "zero-extended predecessor equality"
        for record in dry["records"]
    )
