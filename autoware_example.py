#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end blueprint: a velocity-dependent safe-distance rule (R_G1 style,
"halber Tacho" / time-gap rule) anchored to the *real* Autoware component
architecture, decomposed into A/G sub-contracts, and checked with V1-V4.

Rule (informal, StVO §4 / interstate rule R_G1 flavour):
    "If the gap to the preceding vehicle falls below the safe distance
     tau * v_ego, a hazard notification must be raised within 300 ms."

System-level contract (service-anchored, over ground-truth env signals):
    A_sys: G( 0 <= d_gap <= 200  ∧  0 <= v_true <= 70 )
    G_sys: G( d_gap <= TAU * v_true  ->  F[0,3] hazard_on )

Decomposition across three Autoware components (discharge order:
Localization -> Perception -> Planning):

    C_Localization: G( |v_ego - v_true| <= EPS_V )
        (v_ego published on /localization/kinematic_state)
    C_Perception:   G( |d_lead - d_gap| <= EPS_D )
        (d_lead derived from /perception/object_recognition/objects)
    C_Planning:     G( d_lead <= TAU*(v_ego + EPS_V) + EPS_D
                        ->  F[0,3] hazard_on )
        (hazard_on published on /planning/hazard_lights_cmd)

The correct planning threshold compensates BOTH estimation error bounds.
The faulty variant forgets the velocity error compensation (TAU * v_ego
instead of TAU * (v_ego + EPS_V)) — V3b finds it and z3 produces the
counterexample trace.
"""

from __future__ import annotations
import pathlib
import z3

from stl_ag_checker import (Signal, Service, Atom, Implies, F, G, Contract,
                            run_suite)
from autoware_services import AutowareArchitecture

TAU = 1.8      # time-gap threshold [s]
EPS_V = 0.5    # localization velocity error bound [m/s]
EPS_D = 2.0    # perception gap error bound [m]
K = 3          # reaction budget: 3 steps @ 10 Hz = 300 ms
HORIZON = 8


def atom(label, sigs, fn):
    return Atom(fn, set(sigs), label)


def build(arch: AutowareArchitecture):
    # Ground-truth environment signals (plant model, not part of Autoware)
    d_gap = Signal("d_gap")     # true gap to lead vehicle [m]
    v_true = Signal("v_true")   # true ego velocity [m/s]

    # Feature signals derived from real Autoware ports
    v_ego = arch.feature_signal("v_ego")        # Localization output
    d_lead = arch.feature_signal("d_lead")      # Perception output
    hazard = arch.feature_signal("hazard_on")   # Planning output

    # aSOA services: real Autoware port ownership, env signals as abstract
    # sensing inputs (the sensing chain is abstracted into the plant)
    loc = Service("Localization", {v_true}, {v_ego})
    perc = Service("Perception", {d_gap}, {d_lead})
    plan = Service("Planning", {d_lead, v_ego}, {hazard})

    # ---- system-level contract -------------------------------------------
    a_sys = G(atom("env-bounds", {d_gap, v_true},
                   lambda v: z3.And(v["d_gap"] >= 0, v["d_gap"] <= 200,
                                    v["v_true"] >= 0, v["v_true"] <= 70)))
    g_sys = G(Implies(
        atom("d_gap<=TAU*v_true", {d_gap, v_true},
             lambda v: v["d_gap"] <= TAU * v["v_true"]),
        F(0, K, atom("hazard_on", {hazard}, lambda v: v["hazard_on"]))))
    c_sys = Contract(Service("System", {d_gap, v_true}, {hazard}),
                     a_sys, g_sys, "C_sys")

    # ---- sub-contracts ----------------------------------------------------
    c_loc = Contract(
        loc,
        G(atom("0<=v_true<=70", {v_true},
               lambda v: z3.And(v["v_true"] >= 0, v["v_true"] <= 70))),
        G(atom("|v_ego-v_true|<=EPS_V", {v_ego, v_true},
               lambda v: z3.And(v["v_ego"] - v["v_true"] <= EPS_V,
                                v["v_true"] - v["v_ego"] <= EPS_V))))

    c_perc = Contract(
        perc,
        G(atom("0<=d_gap<=200", {d_gap},
               lambda v: z3.And(v["d_gap"] >= 0, v["d_gap"] <= 200))),
        G(atom("|d_lead-d_gap|<=EPS_D", {d_lead, d_gap},
               lambda v: z3.And(v["d_lead"] - v["d_gap"] <= EPS_D,
                                v["d_gap"] - v["d_lead"] <= EPS_D))))

    a_plan = G(atom("plan-input-bounds", {d_lead, v_ego},
                    lambda v: z3.And(v["d_lead"] >= -EPS_D,
                                     v["v_ego"] >= -EPS_V)))

    # correct: threshold compensates both error bounds
    c_plan_ok = Contract(
        plan, a_plan,
        G(Implies(
            atom("d_lead<=TAU*(v_ego+EPS_V)+EPS_D", {d_lead, v_ego},
                 lambda v: v["d_lead"] <= TAU * (v["v_ego"] + EPS_V) + EPS_D),
            F(0, K, atom("hazard_on", {hazard}, lambda v: v["hazard_on"])))),
        "C_Planning")

    # faulty: velocity error compensation forgotten
    c_plan_bad = Contract(
        plan, a_plan,
        G(Implies(
            atom("d_lead<=TAU*v_ego+EPS_D", {d_lead, v_ego},
                 lambda v: v["d_lead"] <= TAU * v["v_ego"] + EPS_D),
            F(0, K, atom("hazard_on", {hazard}, lambda v: v["hazard_on"])))),
        "C_Planning(bug)")

    signals = {d_gap, v_true, v_ego, d_lead, hazard}
    return c_sys, c_loc, c_perc, c_plan_ok, c_plan_bad, signals


if __name__ == "__main__":
    arch = AutowareArchitecture(
        pathlib.Path(__file__).parent / "autoware_signatures.yaml")
    print(arch.summary())

    c_sys, c_loc, c_perc, c_plan_ok, c_plan_bad, signals = build(arch)

    print("\nRule anchoring (feature -> real Autoware port):")
    for fname in ("v_ego", "d_lead", "hazard_on"):
        p = arch.port_of_feature(fname)
        print(f"  {fname:<10} <- {p.component}: {p.topic}  [{p.msg}]")

    print("\nSystem-level rule (R_G1 style, time-gap TAU=1.8 s):")
    print(f"  A_sys = {c_sys.assumption}")
    print(f"  G_sys = {c_sys.guarantee}")

    e1 = run_suite(c_sys, [c_loc, c_perc, c_plan_ok], signals, HORIZON,
                   "correct (threshold TAU*(v_ego+EPS_V)+EPS_D)")
    e2 = run_suite(c_sys, [c_loc, c_perc, c_plan_bad], signals, HORIZON,
                   "faulty (velocity error compensation forgotten)")

    print("\n=== Summary ===")
    assert not e1, "correct decomposition must be valid"
    assert any("V3b" in e for e in e2), "bug must be caught by V3b"
    print("Blueprint complete: a real traffic rule, anchored to documented "
          "Autoware ports, decomposed across Localization/Perception/"
          "Planning, with the correct decomposition provably valid and the "
          "faulty one refuted by a counterexample trace.")
