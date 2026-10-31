#!/usr/bin/env python3
"""Placement and tightness metrics for valid contract decompositions.

For every valid run in a runs directory, compares the decomposition
against the reference decomposition of the same rule and computes:

  placement    -- do the participating services match the reference
                  (exact / superset / different), and are the guarantees
                  anchored at the same features per service (anchors
                  match / differ)?
  redundancy   -- which sub-contracts contribute nothing to correctness:
                  a contract is redundant iff V3 still holds after
                  removing it (one re-check per contract).
  delta_star   -- guarantee slack: the largest delta by which every
                  numeric threshold of the system guarantee can be
                  tightened (polarity-aware) such that the composition
                  still proves it (binary search over SMT entailment).
                  delta_star = 0 means the decomposition is exactly
                  tight; large values indicate conservatism. The
                  reference decomposition's own slack is reported per
                  rule as the natural zero point.
  assumptions  -- per service shared with the reference, entailment of
                  the synthesised assumption against the reference
                  assumption (equivalent / stronger / weaker /
                  incomparable): laxity or over-restriction.

Appends one CSV row per valid run. Usage:
    python3 placement_tightness.py --runs-dir runs --out pt_results.csv
"""

import argparse
import csv
import json
import pathlib
import re
import tempfile

import z3

from autoware_services import AutowareArchitecture
from check_contracts import load_document
from contract_parser import ABS_INNER
from stl_ag_checker import (And, Atom, F, G, Hist, Implies, Not, Once, Or,
                            check_V3, check_entailment)

HERE = pathlib.Path(__file__).parent
DELTA_MAX = 20.0
DELTA_TOL = 0.01


# ---------------------------------------------------------------------------
# Polarity-aware delta-strengthening of a formula
# ---------------------------------------------------------------------------

def _lin_to_z3(lin: dict, v: dict):
    """z3 expression for a linear form under the atom's signal binding."""
    total = 0.0
    expr = None
    for base, coeff in lin.items():
        if base is None:
            total += coeff
            continue
        if isinstance(base, tuple):  # ("abs", canonical inner string)
            inner = _lin_to_z3(ABS_INNER[base[1]], v)
            term = coeff * z3.If(inner >= 0, inner, -inner)
        else:
            term = coeff * v[base]
        expr = term if expr is None else expr + term
    if expr is None:
        return z3.RealVal(total)
    return expr + total if total else expr


def _shift_atom(atom: Atom, delta: float, positive: bool) -> Atom:
    """Tighten (positive polarity) or loosen (negative) a linear atom by
    delta; boolean signal atoms are returned unchanged."""
    if not hasattr(atom, "linear"):
        return atom
    lhs, op, rhs = atom.linear
    d = delta if positive else -delta

    def fn(v, lhs=lhs, op=op, rhs=rhs, d=d):
        left, right = _lin_to_z3(lhs, v), _lin_to_z3(rhs, v)
        if op == "<=":
            return left <= right - d
        if op == "<":
            return left < right - d
        if op == ">=":
            return left >= right + d
        return left > right + d

    return Atom(fn, atom.sigs, f"{atom.label}[±{delta}]")


def strengthen(phi, delta: float, positive: bool = True):
    """Rebuild phi with every linear atom shifted by delta, tracking
    polarity through negation and implication antecedents."""
    if isinstance(phi, Atom):
        return _shift_atom(phi, delta, positive)
    if isinstance(phi, Not):
        return Not(strengthen(phi.phi, delta, not positive))
    if isinstance(phi, And):
        return And(*[strengthen(p, delta, positive) for p in phi.phis])
    if isinstance(phi, Or):
        return Or(*[strengthen(p, delta, positive) for p in phi.phis])
    if isinstance(phi, Implies):
        return Implies(strengthen(phi.lhs, delta, not positive),
                       strengthen(phi.rhs, delta, positive))
    if isinstance(phi, (F, Hist, Once)):
        return type(phi)(phi.a, phi.b, strengthen(phi.phi, delta, positive))
    if isinstance(phi, G):
        return G(strengthen(phi.phi, delta, positive))
    raise TypeError(f"unsupported node {type(phi).__name__}")


def guarantee_slack(c_sys, contracts, sigs, horizon) -> float:
    """Largest delta such that A_sys ∧ ⋀ G_i still entails the
    delta-strengthened system guarantee (binary search)."""
    premises = [c_sys.assumption] + [c.guarantee for c in contracts]

    def holds(delta: float) -> bool:
        ok, _ = check_entailment(
            premises, strengthen(c_sys.guarantee, delta), sigs, horizon)
        return ok

    lo, hi = 0.0, DELTA_MAX
    if not holds(lo):
        return -1.0  # should not happen for a valid decomposition
    if holds(hi):
        return DELTA_MAX
    while hi - lo > DELTA_TOL:
        mid = (lo + hi) / 2
        if holds(mid):
            lo = mid
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# Per-run metrics
# ---------------------------------------------------------------------------

def anchors(contract) -> set[str]:
    """Feature names the guarantee of a contract is anchored at."""
    return {s.name for s in contract.guarantee.signals()
            if s in contract.service.outputs}


def analyse(ref_bundle, llm_bundle) -> dict:
    ref_doc, ref_sys, ref_cts, ref_order, ref_sigs, ref_h, _ = ref_bundle
    llm_doc, llm_sys, llm_cts, llm_order, llm_sigs, llm_h, _ = llm_bundle
    sigs = ref_sigs | llm_sigs
    horizon = max(ref_h, llm_h)

    ref_by = {c.service.name: c for c in ref_cts}
    llm_by = {c.service.name: c for c in llm_cts}
    ref_names, llm_names = set(ref_by), set(llm_by)
    if llm_names == ref_names:
        placement = "exact"
    elif llm_names > ref_names:
        placement = "superset"
    else:
        placement = "different"
    shared = ref_names & llm_names
    anchor_ok = all(anchors(llm_by[s]) == anchors(ref_by[s]) for s in shared)

    redundant = []
    for c in llm_cts:
        rest = [x for x in llm_cts if x is not c]
        order = [n for n in llm_order if n != c.service.name]
        if not check_V3(llm_sys, rest, order, sigs, horizon, verbose=False):
            redundant.append(c.service.name)

    delta = guarantee_slack(llm_sys, llm_cts, sigs, horizon)

    lax = []
    for s in sorted(shared):
        fwd, _ = check_entailment([llm_by[s].assumption],
                                  ref_by[s].assumption, sigs, horizon)
        bwd, _ = check_entailment([ref_by[s].assumption],
                                  llm_by[s].assumption, sigs, horizon)
        cls = {(True, True): "eq", (True, False): "stronger",
               (False, True): "weaker", (False, False): "incomp"}[(fwd, bwd)]
        lax.append(f"{s}:{cls}")

    return {"placement": placement,
            "anchors": "match" if anchor_ok else "differ",
            "redundant": "+".join(redundant),
            "delta_star": f"{delta:.2f}",
            "assumptions": " ".join(lax)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", type=pathlib.Path, default=HERE / "runs")
    parser.add_argument("--contracts", type=pathlib.Path,
                        default=HERE / "contracts")
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path("pt_results.csv"))
    parser.add_argument("--rq2", type=pathlib.Path,
                        default=pathlib.Path("rq2_results.csv"),
                        help="RQ2 results CSV: its per-run environment-"
                             "signal alignments are reused here, so both "
                             "analyses compare under the same renaming")
    args = parser.parse_args()

    alignments = {}
    if args.rq2.exists():
        for r in csv.DictReader(args.rq2.open()):
            if r.get("aligned"):
                alignments[(r["rule"], r["model"], r["run"])] = dict(
                    pair.split("->") for pair in r["aligned"].split("+"))

    arch = AutowareArchitecture(HERE / "autoware_signatures.yaml")
    references = {}
    for ref_path in sorted(args.contracts.glob("R_*.yaml")):
        if "faulty" in ref_path.stem or "illegal" in ref_path.stem:
            continue
        references[ref_path.stem] = load_document(ref_path, arch)

    print("reference guarantee slack per rule (natural zero point):")
    ref_slack = {}
    for rule, bundle in references.items():
        _, c_sys, cts, _, sigs, h, _ = bundle
        ref_slack[rule] = guarantee_slack(c_sys, cts, sigs, h)
        print(f"  {rule}: delta* = {ref_slack[rule]:.2f}")

    rows = []
    for result_file in sorted(args.runs_dir.glob("*/*/run_*/result.json")):
        result = json.loads(result_file.read_text())
        if not result.get("valid") or result["rule"] not in references:
            continue
        run_dir = result_file.parent
        doc_path = run_dir / f"attempt_{result['attempts']}.yaml"
        mapping = alignments.get((result["rule"], run_dir.parent.name,
                                  run_dir.name.split("_")[1]))
        if mapping:  # apply the RQ2-chosen environment-signal renaming
            text = doc_path.read_text()
            for old, new in mapping.items():
                text = re.sub(rf"\b{re.escape(old)}\b", new, text)
            doc_path = pathlib.Path(tempfile.mkstemp(suffix=".yaml")[1])
            doc_path.write_text(text)
        try:
            llm_bundle = load_document(doc_path, arch)
            metrics = analyse(references[result["rule"]], llm_bundle)
        except Exception as exc:
            metrics = {"placement": f"error:{type(exc).__name__}",
                       "anchors": "", "redundant": "", "delta_star": "",
                       "assumptions": ""}
        row = {"rule": result["rule"], "model": run_dir.parent.name,
               "run": run_dir.name.split("_")[1],
               "delta_ref": f"{ref_slack[result['rule']]:.2f}", **metrics}
        rows.append(row)
        print(f"{row['rule']} {row['model']} run {row['run']}: "
              f"{row['placement']}/{row['anchors']} "
              f"redundant=[{row['redundant']}] delta*={row['delta_star']} "
              f"(ref {row['delta_ref']})  {row['assumptions']}")

    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else
                                ["rule", "model", "run"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n{len(rows)} valid runs analysed -> {args.out}")


if __name__ == "__main__":
    main()
