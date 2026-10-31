#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Demo: the same R_G1 rule document, now on the monitoring backend (rtamt).

Two synthetic traces (10 Hz, 40 steps):

  Trace A (compliant): the gap closes below the time-gap threshold, the
  perception estimate stays within its error bound, planning raises the
  hazard notification within the 300 ms budget.

  Trace B (violating): identical scenario, but the perception estimate has
  an error spike (+12 m >> EPS_D = 2 m) around the threshold crossing, so
  planning — although honoring ITS contract on ITS inputs — raises the
  notification too late (the spike delays the trigger beyond the 300 ms
  budget). The system rule is violated, and fault localization blames
  Perception, not Planning.

This is RQ4 in miniature: contract monitors do not only detect rule
violations, they localize the responsible service in the architecture.
"""

from __future__ import annotations
from monitor_backend import RuleMonitor

TAU, EPS_V, EPS_D, K = 1.8, 0.5, 2.0, 3
N = 40


def make_trace(perception_fault: bool):
    time = list(range(N))
    v_true = [25.0] * N                       # 90 km/h, constant
    v_ego = [25.0 + 0.3 for _ in range(N)]    # within EPS_V
    # gap: 100 m shrinking by 2 m/step until 30 m, then constant
    d_gap = [max(100.0 - 2.0 * t, 30.0) for t in range(N)]
    # perception estimate: small error, or a +6 m spike around the
    # threshold crossing (d_gap <= TAU*v_true = 45 m  <=>  t >= 27.5)
    d_lead = []
    for t in range(N):
        err = 0.8
        if perception_fault and 26 <= t <= 36:
            err = 12.0
        d_lead.append(d_gap[t] + err)
    # planning: raises hazard 2 steps after ITS trigger condition
    # (d_lead <= TAU*(v_ego+EPS_V)+EPS_D) first holds
    thresh = [TAU * (v_ego[t] + EPS_V) + EPS_D for t in range(N)]
    hazard = [0.0] * N
    fired = None
    for t in range(N):
        if d_lead[t] <= thresh[t]:
            fired = t if fired is None else fired
    if fired is not None:
        for t in range(min(fired + 2, N - 1), N):
            hazard[t] = 1.0
    return {"time": time, "v_true": v_true, "v_ego": v_ego,
            "d_gap": d_gap, "d_lead": d_lead, "hazard_on": hazard}


def report(mon: RuleMonitor, trace, label: str):
    print(f"\n=== Trace: {label} ===")
    loc = mon.localize(trace)
    sys_viol = loc["system_violation_steps"]
    if not sys_viol:
        print("  system rule: ✓ satisfied on the whole trace")
    else:
        print(f"  system rule: ✗ violated at steps {sys_viol}")
    for svc, b in loc["blame"].items():
        flag = "✗ CULPABLE" if b["culpable"] else "✓ contract kept"
        extra = ""
        if b["guarantee_violation_steps"]:
            extra = f" (guarantee violated at {b['guarantee_violation_steps']})"
        print(f"    {svc:<13} {flag}{extra}")


if __name__ == "__main__":
    mon = RuleMonitor("contracts/R_G1.yaml")
    print(f"Rule {mon.rule_id}: monitors built from the SAME document that "
          f"z3 proved valid (two backends, one spec).")

    report(mon, make_trace(perception_fault=False), "A — compliant")
    report(mon, make_trace(perception_fault=True),
           "B — perception error spike (+12 m >> EPS_D)")
