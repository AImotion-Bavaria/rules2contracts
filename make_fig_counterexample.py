#!/usr/bin/env python3
"""Export the counterexample-trace data for the paper's Fig. "counterexample".

Loads the faulty R_G1 decomposition (Planning threshold without the
velocity-error compensation), builds the V3b proof obligation exactly as the
checker does (A_sys and all sub-guarantees must entail G_sys), and pins the
trace to a readable braking-approach scenario: the true gap shrinks to just
below the legal threshold TAU * v_true and stays inside the band that the
uncompensated Planning threshold does not cover, so the hazard obligation of
the system rule is never triggered by the Planning contract.

The pinned scenario is handed to the SMT solver TOGETHER with the V3b
obligation's negation, so the exported trace is a machine-verified
counterexample of the same obligation the checker rejects -- merely a
readable representative instead of an arbitrary solver model.

Output: data/fig_counterexample.dat (whitespace-separated, read by
figures/fig_counterexample.tex via \\pgfplotstableread).

Pipeline: contracts/R_G1_faulty.yaml -> make_fig_counterexample.py -> pdflatex.
"""

import pathlib
import sys

import z3

from autoware_services import AutowareArchitecture
from check_contracts import load_document
from stl_ag_checker import make_trace

DOC = pathlib.Path("contracts/R_G1_faulty.yaml")
OUT = pathlib.Path("data/fig_counterexample.dat")
if "--out-dir" in sys.argv:  # optional: write elsewhere (reproduction runs)
    OUT = pathlib.Path(sys.argv[sys.argv.index("--out-dir") + 1]) / OUT.name
    OUT.parent.mkdir(parents=True, exist_ok=True)

# Readable braking-approach scenario (metres, metres/second, 10 Hz steps).
# The true gap crosses TAU * v_true (= 36 m) at t = 4 and stays inside the
# uncovered band (TAU * v_true - TAU * EPS_V, TAU * v_true] afterwards.
V_TRUE = 20.0
D_GAP = [40.0, 39.0, 38.0, 37.0, 36.0, 35.9, 35.8, 35.7, 35.6]


def main() -> None:
    arch = AutowareArchitecture("autoware_signatures.yaml")
    doc, c_sys, contracts, _order, used, horizon, errors = load_document(DOC, arch)
    if errors:
        raise SystemExit(f"document unexpectedly ill-formed: {errors}")
    if len(D_GAP) != horizon + 1:
        raise SystemExit(f"scenario length {len(D_GAP)} != horizon+1 ({horizon + 1})")
    const = {k: float(v) for k, v in doc["constants"].items()}
    tau, eps_v, eps_d = const["TAU"], const["EPS_V"], const["EPS_D"]

    # V3b obligation exactly as in check_V3: A_sys ∧ ⋀ G_i ⊨ G_sys.
    trace = make_trace(used, horizon)
    solver = z3.Solver()
    for premise in [c_sys.assumption] + [c.guarantee for c in contracts]:
        solver.add(premise.encode_over(trace, horizon))
    solver.add(z3.Not(c_sys.guarantee.encode_over(trace, horizon)))

    # Pin the scenario: worst-case (but contract-conformant) feature errors,
    # no hazard. If this were not a genuine counterexample of the obligation
    # above, the conjunction would be UNSAT and we would fail loudly here.
    for t in range(horizon + 1):
        solver.add(trace[("v_true", t)] == V_TRUE)
        solver.add(trace[("d_gap", t)] == D_GAP[t])
        solver.add(trace[("v_ego", t)] == V_TRUE - eps_v)
        solver.add(trace[("d_lead", t)] == D_GAP[t] + eps_d)
        solver.add(trace[("hazard_on", t)] == False)  # noqa: E712 (z3 expr)
    if solver.check() != z3.sat:
        raise SystemExit("pinned scenario is NOT a V3b counterexample -- "
                         "did the faulty document or the checker change?")

    model = solver.model()

    def val(name: str, t: int) -> float:
        v = model.eval(trace[(name, t)], model_completion=True)
        if z3.is_bool(v):
            return 1.0 if z3.is_true(v) else 0.0
        return float(v.as_fraction())

    with OUT.open("w") as f:
        f.write("t d_gap d_lead v_true v_ego thr_sys thr_plan hazard\n")
        for t in range(horizon + 1):
            thr_sys = tau * val("v_true", t)
            thr_plan = tau * val("v_ego", t) + eps_d
            f.write(f"{t} {val('d_gap', t):.2f} {val('d_lead', t):.2f} "
                    f"{val('v_true', t):.2f} {val('v_ego', t):.2f} "
                    f"{thr_sys:.2f} {thr_plan:.2f} {val('hazard_on', t):.0f}\n")
    print(f"{OUT} written (verified V3b counterexample, horizon {horizon})")


if __name__ == "__main__":
    main()
