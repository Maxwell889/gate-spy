#!/usr/bin/env python3
"""Run reproducible recovery/candidate evaluation over testcase directories."""

from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.abc_cec import run_cec  # noqa: E402
from scripts.cost import calc_verilog_cost  # noqa: E402
from src.circuit import Circuit  # noqa: E402
from src.session import CircuitSession  # noqa: E402


CASE_MANIFEST: dict[str, dict[str, Any]] = {
    "test13": {
        "target_cost": 17,
        "priority": "hard",
        "note": "WolFEx-style hard predicate/control case",
    },
    "test29": {
        "target_cost": 69,
        "priority": "hard",
        "note": "wide controlled arithmetic; avoid expr-only slice false positives",
    },
    "test24": {"priority": "wolfex-timeout-hard-set"},
    "test26": {"priority": "wolfex-timeout-hard-set"},
    "test27": {"priority": "wolfex-timeout-hard-set"},
    "test28": {"priority": "wolfex-timeout-hard-set"},
}


def _case_path(case_root: Path, case: str) -> Path:
    return case_root / case / "top_primitive.v"


def _candidate_path(args: argparse.Namespace, case: str) -> Path | None:
    if args.candidate_file:
        return Path(args.candidate_file)
    base = Path(args.candidate_dir) if args.candidate_dir else args.case_root
    path = base / case / args.candidate_name
    return path if path.exists() else None


def _safe_cost(path: Path) -> int | None:
    try:
        return calc_verilog_cost(str(path))
    except Exception:
        return None


def _safe_cost_code(code: str) -> int | None:
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".v", prefix="gate_spy_recovery_eval_", delete=False)
    try:
        with tmp:
            tmp.write(code)
        return calc_verilog_cost(tmp.name)
    except Exception:
        return None
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass


def _safe_gate_count(path: Path) -> int | None:
    try:
        return len(Circuit.from_file(str(path)).gate_nodes)
    except Exception:
        return None


def _accepted_candidates(report: str) -> list[dict[str, Any]]:
    data = json.loads(report)
    out: list[dict[str, Any]] = []
    for candidate in data.get("candidates", []):
        verification = candidate.get("verification") or {}
        if verification.get("status") in {
            "exhaustive-exact",
            "exhaustive-masked",
            "sample-exact",
            "sample-masked",
        }:
            out.append(candidate)
    return out


def _choose_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda cand: (
            cand.get("cost", 1_000_000),
            cand.get("expression", ""),
        ),
    )[0]


def _base_ref(ref: str) -> str:
    return ref.split("[", 1)[0]


def _auto_recover_with_session(session: CircuitSession,
                               timeout_s: int) -> dict[str, Any]:
    """Run a bounded, template-first session recovery flow."""
    session.plan_recovery(detail=False, format="json")
    analysis = json.loads(session.analyze_words(format="json"))
    widths = {
        word["name"]: int(word["width"])
        for word in analysis.get("input_words", [])
    }
    support_by_output = {
        _base_ref(cut["target"]): [_base_ref(ref) for ref in cut.get("support_words", [])]
        for cut in analysis.get("support_cuts", [])
    }
    steps: list[dict[str, Any]] = []

    for word in analysis.get("output_words", []):
        target = word["name"]
        support = support_by_output.get(target, [])
        controls = [ref for ref in support if widths.get(ref, 1) == 1]
        data_inputs = [ref for ref in support if ref not in controls]
        if controls and data_inputs and len(controls) <= 4:
            cases: list[dict[str, Any]] = []
            for values in itertools.product((0, 1), repeat=len(controls)):
                fixed = dict(zip(controls, values))
                fit_report = session.fit_template(
                    target,
                    inputs=data_inputs,
                    fixed_inputs=fixed,
                    exhaustive="never",
                    pattern_num=512,
                    validation_num=2048,
                    format="json",
                )
                chosen = _choose_candidate(_accepted_candidates(fit_report))
                steps.append({
                    "tool": "fit_template",
                    "target": target,
                    "fixed_inputs": fixed,
                    "accepted": bool(chosen),
                    "expression": chosen.get("expression") if chosen else "",
                })
                if chosen is None:
                    break
                cases.append({
                    "fixed_inputs": fixed,
                    "expression": chosen["expression"],
                })
            if len(cases) == (1 << len(controls)):
                combined = session.combine_case_expr(
                    target,
                    controls=controls,
                    cases=cases,
                    inputs=support,
                    exhaustive="never",
                    pattern_num=512,
                    validation_num=4096,
                    format="json",
                )
                chosen = _choose_candidate(_accepted_candidates(combined))
                steps.append({
                    "tool": "combine_case_expr",
                    "target": target,
                    "accepted": bool(chosen),
                    "expression": chosen.get("expression") if chosen else "",
                })
        else:
            fit_report = session.fit_template(
                target,
                inputs=support,
                exhaustive="never",
                pattern_num=512,
                validation_num=2048,
                format="json",
            )
            chosen = _choose_candidate(_accepted_candidates(fit_report))
            steps.append({
                "tool": "fit_template",
                "target": target,
                "accepted": bool(chosen),
                "expression": chosen.get("expression") if chosen else "",
            })

    assembled = json.loads(session.assemble_rtl(timeout_s=timeout_s, format="json"))
    verification = assembled.get("verification") or {}
    return {
        "status": verification.get("status", "not-run"),
        "reason": verification.get("reason", ""),
        "candidate_rtl": assembled.get("candidate_rtl", ""),
        "verification": verification,
        "unrecovered": assembled.get("unrecovered", []),
        "steps": steps,
    }


def _evaluate_case(args: argparse.Namespace, case: str) -> dict[str, Any]:
    started = time.time()
    primitive = _case_path(args.case_root, case)
    candidate = _candidate_path(args, case)
    record: dict[str, Any] = {
        "case": case,
        "manifest": CASE_MANIFEST.get(case, {}),
        "primitive": str(primitive),
        "candidate": str(candidate) if candidate else None,
        "timeout_s": args.timeout,
        "started_at": _dt.datetime.now(_dt.UTC).isoformat(),
    }
    if not primitive.exists():
        record.update({
            "status": "missing_primitive",
            "elapsed_s": time.time() - started,
        })
        return record

    record["primitive_cost"] = _safe_cost(primitive)
    record["primitive_gate_count"] = _safe_gate_count(primitive)
    session: CircuitSession | None = None
    if args.record_ir or args.query_kb or args.promote_on_success or candidate is None:
        session = CircuitSession()
        session.load(
            str(primitive),
            record_ir=args.record_ir or args.promote_on_success,
            case_id=case,
            out_dir=str(args.ir_out_dir) if args.ir_out_dir else None,
        )
        if args.record_ir or args.promote_on_success:
            # Plan generation records a structured baseline even when no
            # candidate exists, which turns hard-case failures into reusable IR.
            session.plan_recovery(detail=False, format="json")
            ir = json.loads(session.export_recovery_ir(format="json"))
            record["recovery_ir_run_id"] = ir.get("run_id")
            record["recovery_ir_dir"] = ir.get("run_dir")
        if args.query_kb:
            record["memory_hits"] = json.loads(
                session.query_recovery_memory(limit=args.kb_limit, format="json"))

    if candidate is None or not candidate.exists():
        if session is not None:
            auto = _auto_recover_with_session(session, args.timeout)
            record["auto_recovery"] = {
                key: value for key, value in auto.items()
                if key != "candidate_rtl"
            }
            candidate_rtl = auto.get("candidate_rtl", "")
            verification = auto.get("verification") or {}
            status = str(verification.get("status") or "not-run")
            if candidate_rtl and status in {
                "cec-proved",
                "cec-timeout-assumed",
                "failed",
            }:
                record.update({
                    "status": status,
                    "candidate": "session:auto",
                    "candidate_cost": _safe_cost_code(candidate_rtl),
                    "cec": {
                        "success": status in {"cec-proved", "cec-timeout-assumed"},
                        "reason": verification.get("reason"),
                        "elapsed": verification.get("elapsed"),
                        "counterexample": verification.get("counterexample"),
                    },
                    "elapsed_s": time.time() - started,
                })
                if args.record_ir or args.promote_on_success:
                    ir = json.loads(session.export_recovery_ir(format="json"))
                    record["recovery_ir_run_id"] = ir.get("run_id")
                    record["recovery_ir_dir"] = ir.get("run_dir")
                if args.promote_on_success and status in {
                    "cec-proved",
                    "cec-timeout-assumed",
                }:
                    record["promotion"] = json.loads(
                        session.promote_recovery_memory(dry_run=False, format="json"))
                return record
        record.update({
            "status": "missing_candidate",
            "candidate_cost": None,
            "cec": None,
            "elapsed_s": time.time() - started,
        })
        if session and (args.record_ir or args.promote_on_success):
            session.record_recovery_note(
                "eval",
                target="module",
                summary=(
                    f"{case}: missing candidate; primitive_cost="
                    f"{record.get('primitive_cost')}"
                ),
            )
        return record

    record["candidate_cost"] = _safe_cost(candidate)
    if args.no_cec:
        record.update({
            "status": "cec_skipped",
            "cec": None,
            "elapsed_s": time.time() - started,
        })
        if session and (args.record_ir or args.promote_on_success):
            session.record_recovery_note(
                "eval",
                target="module",
                summary=(
                    f"{case}: CEC skipped; candidate_cost="
                    f"{record.get('candidate_cost')}"
                ),
            )
        return record

    cec = run_cec(str(primitive), str(candidate), timeout=args.timeout)
    reason = cec.get("reason", "unknown")
    if cec.get("success") and reason == "equivalent":
        status = "cec-proved"
    elif cec.get("success") and reason == "timeout_assumed_equivalent":
        status = "cec-timeout-assumed"
    else:
        status = f"cec-{reason}"
    record.update({
        "status": status,
        "cec": {
            "success": cec.get("success"),
            "reason": reason,
            "elapsed": cec.get("elapsed"),
            "counterexample": cec.get("counterexample"),
        },
        "elapsed_s": time.time() - started,
    })
    if session and (args.record_ir or args.promote_on_success):
        session.record_recovery_note(
            "eval",
            target="module",
            summary=(
                f"{case}: {status}; primitive_cost={record.get('primitive_cost')} "
                f"candidate_cost={record.get('candidate_cost')}"
            ),
            refs=[str(candidate)],
        )
        ir = json.loads(session.export_recovery_ir(format="json"))
        record["recovery_ir_run_id"] = ir.get("run_id")
        record["recovery_ir_dir"] = ir.get("run_dir")
    if session and args.promote_on_success and status in {
        "cec-proved",
        "cec-timeout-assumed",
    }:
        record["promotion"] = json.loads(
            session.promote_recovery_memory(dry_run=False, format="json"))
    return record


def _output_path(out_dir: Path) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return out_dir / f"recovery-eval-{stamp}.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate recovered RTL candidates for GateSpy testcases.")
    parser.add_argument("--cases", nargs="+", required=True,
                        help="Case ids such as test01 test13 test29.")
    parser.add_argument("--case-root", type=Path,
                        default=ROOT / "examples" / "testcase",
                        help="Root containing testcase/<case>/top_primitive.v.")
    parser.add_argument("--candidate-dir", type=Path, default=None,
                        help="Root containing <case>/top_recovered.v candidates.")
    parser.add_argument("--candidate-file", type=Path, default=None,
                        help="Evaluate one explicit candidate file.")
    parser.add_argument("--candidate-name", default="top_recovered.v",
                        help="Candidate filename inside each case directory.")
    parser.add_argument("--timeout", type=int, default=300,
                        help="ABC CEC timeout in seconds.")
    parser.add_argument("--no-cec", action="store_true",
                        help="Only compute costs and gate counts.")
    parser.add_argument("--out-dir", type=Path,
                        default=Path("/private/tmp/gate-spy-recovery-runs"),
                        help="Directory for JSONL run records.")
    parser.add_argument("--jsonl", type=Path, default=None,
                        help="Explicit JSONL output path.")
    parser.add_argument("--record-ir", action="store_true",
                        help="Create graph-only recovery reasoning runs for evaluated cases.")
    parser.add_argument("--ir-out-dir", type=Path,
                        default=ROOT / ".gate_spy" / "runs",
                        help="Base directory for graph-only --record-ir run artifacts.")
    parser.add_argument("--query-kb", action="store_true",
                        help="Attach local recovery experience hits to each record.")
    parser.add_argument("--kb-limit", type=int, default=5,
                        help="Number of KB hits to attach with --query-kb.")
    parser.add_argument("--promote-on-success", action="store_true",
                        help="Promote candidate events from successful IR runs into data/recovery_kb.")
    args = parser.parse_args()

    if args.candidate_file and len(args.cases) != 1:
        parser.error("--candidate-file can only be used with exactly one case")

    args.case_root = args.case_root.resolve()
    if args.candidate_dir:
        args.candidate_dir = args.candidate_dir.resolve()
    if args.candidate_file:
        args.candidate_file = args.candidate_file.resolve()
    if args.ir_out_dir:
        args.ir_out_dir = args.ir_out_dir.resolve()
    out_path = args.jsonl or _output_path(args.out_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    records = [_evaluate_case(args, case) for case in args.cases]
    with out_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    print(f"wrote {len(records)} record(s) to {out_path}")
    for record in records:
        print(
            f"{record['case']}: {record['status']} "
            f"primitive_cost={record.get('primitive_cost')} "
            f"candidate_cost={record.get('candidate_cost')}")
    return 0 if all(not str(r["status"]).startswith("cec-counterexample")
                    for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
