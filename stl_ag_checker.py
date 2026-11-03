#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prototype: Formal validity checking (V1–V4) of A/G contract decompositions
for automotive service-oriented architectures (αSOA + STL fragment).

Paper prototype — "From Traffic Rules to Service Contracts"

STL fragment (fixed by design decision, 2026-08-07):
  * discrete time, finite horizon H
  * atoms: linear arithmetic over real/bool signals at the current step
  * boolean connectives
  * temporal: G (over the whole horizon), H[a,b] (historically),
    O[a,b] (once / past), F[a,b] (bounded future), U excluded
  * bounded-future depth must fit into the horizon (truncated semantics:
    G φ is evaluated for all t where the future window of φ fits)

Within this fragment, refinement/entailment over the bounded horizon is
decidable: validity of  premise → conclusion  is checked as UNSAT of
premise ∧ ¬conclusion over the trace variables (QF_LRA after unrolling).

Checks implemented:
  V1  Well-formedness  (syntactic; assumptions over inputs, guarantees over
      inputs ∪ outputs, all signals within the service signature)
  V2  Consistency / non-vacuity  (A satisfiable, A ∧ G satisfiable)
  V3  Correctness of the decomposition (acyclic assumption discharge +
      guarantee entailment:  A_sys ∧ ⋀ G_i  ⊨  G_sys)
  V4  Tightness indicator  (no system-conformant behaviour forces a
      sub-contract violation)

Plus: placement metrics (locality / induced coupling) for monitor placement.

Only dependency: z3-solver.  Runs offline on a laptop.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import itertools
import z3

z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)


# ---------------------------------------------------------------------------
# Signals and services (αSOA signatures)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """A typed port/channel. sort ∈ {'real', 'bool'}."""
    name: str
    sort: str = "real"

    def at(self, trace: dict, t: int):
        return trace[(self.name, t)]


@dataclass
class Service:
    """αSOA service signature: typed input and output ports."""
    name: str
    inputs: set[Signal] = field(default_factory=set)
    outputs: set[Signal] = field(default_factory=set)

    @property
    def signature(self) -> set[Signal]:
        return self.inputs | self.outputs


# ---------------------------------------------------------------------------
# Formula AST (the fixed STL fragment)
# ---------------------------------------------------------------------------

class Formula:
    def signals(self) -> set[Signal]:
        raise NotImplementedError

    def future_depth(self) -> int:
        """Maximal look-ahead in steps (for truncated semantics)."""
        raise NotImplementedError

    def past_depth(self) -> int:
        raise NotImplementedError

    def encode(self, trace: dict, t: int):
        """z3 encoding of the formula evaluated at step t."""
        raise NotImplementedError


class Atom(Formula):
    """Linear-arithmetic predicate over signals at the current step.
    `fn` maps a dict {signal_name: z3var} to a z3 Bool expression."""

    def __init__(self, fn, sigs: set[Signal], label: str = "atom"):
        self.fn, self.sigs, self.label = fn, sigs, label

    def signals(self): return set(self.sigs)
    def future_depth(self): return 0
    def past_depth(self): return 0

    def encode(self, trace, t):
        return self.fn({s.name: s.at(trace, t) for s in self.sigs})

    def __repr__(self): return self.label


class Not(Formula):
    def __init__(self, phi): self.phi = phi
    def signals(self): return self.phi.signals()
    def future_depth(self): return self.phi.future_depth()
    def past_depth(self): return self.phi.past_depth()
    def encode(self, trace, t): return z3.Not(self.phi.encode(trace, t))
    def __repr__(self): return f"¬({self.phi})"


class And(Formula):
    def __init__(self, *phis): self.phis = phis
    def signals(self): return set().union(*[p.signals() for p in self.phis])
    def future_depth(self): return max(p.future_depth() for p in self.phis)
    def past_depth(self): return max(p.past_depth() for p in self.phis)
    def encode(self, trace, t):
        return z3.And(*[p.encode(trace, t) for p in self.phis])
    def __repr__(self): return "(" + " ∧ ".join(map(str, self.phis)) + ")"


class Or(Formula):
    def __init__(self, *phis): self.phis = phis
    def signals(self): return set().union(*[p.signals() for p in self.phis])
    def future_depth(self): return max(p.future_depth() for p in self.phis)
    def past_depth(self): return max(p.past_depth() for p in self.phis)
    def encode(self, trace, t):
        return z3.Or(*[p.encode(trace, t) for p in self.phis])
    def __repr__(self): return "(" + " ∨ ".join(map(str, self.phis)) + ")"


class Implies(Formula):
    def __init__(self, lhs, rhs): self.lhs, self.rhs = lhs, rhs
    def signals(self): return self.lhs.signals() | self.rhs.signals()
    def future_depth(self):
        return max(self.lhs.future_depth(), self.rhs.future_depth())
    def past_depth(self):
        return max(self.lhs.past_depth(), self.rhs.past_depth())
    def encode(self, trace, t):
        return z3.Implies(self.lhs.encode(trace, t), self.rhs.encode(trace, t))
    def __repr__(self): return f"({self.lhs} → {self.rhs})"


class F(Formula):
    """Bounded future: F[a,b] φ  — φ holds at some step in [t+a, t+b]."""

    def __init__(self, a: int, b: int, phi: Formula):
        assert 0 <= a <= b
        self.a, self.b, self.phi = a, b, phi

    def signals(self): return self.phi.signals()
    def future_depth(self): return self.b + self.phi.future_depth()
    def past_depth(self): return self.phi.past_depth()

    def encode(self, trace, t):
        return z3.Or(*[self.phi.encode(trace, t + k)
                       for k in range(self.a, self.b + 1)])

    def __repr__(self): return f"F[{self.a},{self.b}]({self.phi})"


class Hist(Formula):
    """Bounded past: H[a,b] φ — φ held at every step in [t−b, t−a]
    (truncated at trace start)."""

    def __init__(self, a: int, b: int, phi: Formula):
        assert 0 <= a <= b
        self.a, self.b, self.phi = a, b, phi

    def signals(self): return self.phi.signals()
    def future_depth(self): return self.phi.future_depth()
    def past_depth(self): return self.b + self.phi.past_depth()

    def encode(self, trace, t):
        return z3.And(*[self.phi.encode(trace, t - k)
                        for k in range(self.a, self.b + 1) if t - k >= 0])

    def __repr__(self): return f"H[{self.a},{self.b}]({self.phi})"


class Once(Formula):
    """Bounded past: O[a,b] φ — φ held at some step in [t−b, t−a]."""

    def __init__(self, a: int, b: int, phi: Formula):
        assert 0 <= a <= b
        self.a, self.b, self.phi = a, b, phi

    def signals(self): return self.phi.signals()
    def future_depth(self): return self.phi.future_depth()
    def past_depth(self): return self.b + self.phi.past_depth()

    def encode(self, trace, t):
        opts = [self.phi.encode(trace, t - k)
                for k in range(self.a, self.b + 1) if t - k >= 0]
        return z3.Or(*opts) if opts else z3.BoolVal(False)

    def __repr__(self): return f"O[{self.a},{self.b}]({self.phi})"


class G(Formula):
    """Outer 'globally' over the horizon (safety-fragment shape: G φ with
    φ from the past/bounded-future fragment)."""

    def __init__(self, phi: Formula): self.phi = phi
    def signals(self): return self.phi.signals()
    def future_depth(self): return self.phi.future_depth()
    def past_depth(self): return self.phi.past_depth()

    def encode_over(self, trace, horizon: int):
        fd = self.phi.future_depth()
        return z3.And(*[self.phi.encode(trace, t)
                        for t in range(0, horizon - fd + 1)])

    def encode(self, trace, t):  # nested G not used in the fragment
        raise RuntimeError("G is only allowed as the outermost operator")

    def __repr__(self): return f"G({self.phi})"


# ---------------------------------------------------------------------------
# A/G contracts
# ---------------------------------------------------------------------------

@dataclass
class Contract:
    """Assume/Guarantee contract of a service.  A and G are outer-G
    formulas (safety fragment)."""
    service: Service
    assumption: G
    guarantee: G
    name: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = f"C_{self.service.name}"


# ---------------------------------------------------------------------------
# Trace encoding and entailment engine
# ---------------------------------------------------------------------------

def make_trace(signals: set[Signal], horizon: int) -> dict:
    # Deterministic variable-creation order (sorted by name): together with
    # the fixed z3 seeds below this makes counterexample models reproducible
    # across processes, which the repair-loop response cache relies on.
    trace = {}
    for s in sorted(signals, key=lambda x: x.name):
        for t in range(horizon + 1):
            v = (z3.Real if s.sort == "real" else z3.Bool)(f"{s.name}@{t}")
            trace[(s.name, t)] = v
    return trace


def check_entailment(premises: list[G], conclusion: G, signals: set[Signal],
                     horizon: int):
    """Return (valid: bool, counterexample: Optional[dict]).
    Valid iff  ⋀ premises → conclusion  holds over all traces of the
    given horizon (UNSAT of premises ∧ ¬conclusion)."""
    trace = make_trace(signals, horizon)
    s = z3.Solver()
    for p in premises:
        s.add(p.encode_over(trace, horizon))
    s.add(z3.Not(conclusion.encode_over(trace, horizon)))
    res = s.check()
    if res == z3.unsat:
        return True, None
    model = s.model()
    cex = {}
    for (name, t), var in sorted(trace.items()):
        val = model.eval(var, model_completion=True)
        cex.setdefault(name, {})[t] = val
    return False, cex


def check_sat(formulas: list[G], signals: set[Signal], horizon: int) -> bool:
    trace = make_trace(signals, horizon)
    s = z3.Solver()
    for f in formulas:
        s.add(f.encode_over(trace, horizon))
    return s.check() == z3.sat


def print_cex(cex: dict, horizon: int, title: str):
    print(f"    Counterexample trace ({title}):")
    header = "      t     " + " ".join(f"{t:>8}" for t in range(horizon + 1))
    print(header)
    for name in sorted(cex):
        row = " ".join(f"{str(cex[name][t]):>8}" for t in range(horizon + 1))
        print(f"      {name:<6}{row}")


# ---------------------------------------------------------------------------
# Validity checks V1–V4
# ---------------------------------------------------------------------------

def check_V1(contracts: list[Contract]) -> list[str]:
    """Well-formedness: A over inputs only; G over inputs ∪ outputs;
    everything within the service signature."""
    errors = []
    for c in contracts:
        bad_a = c.assumption.signals() - c.service.inputs
        if bad_a:
            errors.append(
                f"[V1] {c.name}: assumption references non-input ports: "
                f"{sorted(s.name for s in bad_a)}")
        bad_g = c.guarantee.signals() - c.service.signature
        if bad_g:
            errors.append(
                f"[V1] {c.name}: guarantee references ports outside the "
                f"service signature: {sorted(s.name for s in bad_g)}")
    return errors


def check_V2(contracts: list[Contract], signals: set[Signal],
             horizon: int) -> list[str]:
    """Consistency / non-vacuity."""
    errors = []
    for c in contracts:
        if not check_sat([c.assumption], signals, horizon):
            errors.append(f"[V2] {c.name}: assumption unsatisfiable "
                          f"(contract vacuously true)")
        elif not check_sat([c.assumption, c.guarantee], signals, horizon):
            errors.append(f"[V2] {c.name}: A ∧ G unsatisfiable "
                          f"(contract inconsistent)")
    return errors


def check_V3(sys_contract: Contract, contracts: list[Contract],
             order: list[str], signals: set[Signal], horizon: int,
             verbose: bool = True) -> list[str]:
    """Correctness of the decomposition (acyclic discharge order):
      (a) for each service i (in topological order):
            A_sys ∧ ⋀_{j earlier} G_j  ⊨  A_i
      (b) A_sys ∧ ⋀_i G_i  ⊨  G_sys
    """
    errors = []
    by_name = {c.service.name: c for c in contracts}
    discharged: list[G] = []
    for name in order:
        c = by_name[name]
        ok, cex = check_entailment(
            [sys_contract.assumption] + discharged, c.assumption,
            signals, horizon)
        if not ok:
            errors.append(f"[V3a] assumption of {c.name} not discharged")
            if verbose and cex:
                print_cex(cex, horizon, f"A_{name} not discharged")
        discharged.append(c.guarantee)
    ok, cex = check_entailment(
        [sys_contract.assumption] + [c.guarantee for c in contracts],
        sys_contract.guarantee, signals, horizon)
    if not ok:
        errors.append("[V3b] composition of guarantees does NOT entail "
                      "the system-level guarantee")
        if verbose and cex:
            print_cex(cex, horizon,
                      "system rule violated despite all sub-contracts")
    return errors


def check_V4(sys_contract: Contract, contracts: list[Contract],
             signals: set[Signal], horizon: int) -> list[str]:
    """Tightness indicator: no system-conformant behaviour *forces* a
    sub-contract violation, i.e. A_sys ∧ G_sys ∧ ⋀ A_i ∧ ⋀ G_i is SAT.
    (Heuristic indicator, not a completeness proof.)"""
    errors = []
    all_f = ([sys_contract.assumption, sys_contract.guarantee]
             + [c.assumption for c in contracts]
             + [c.guarantee for c in contracts])
    if not check_sat(all_f, signals, horizon):
        errors.append("[V4] no system-conformant behaviour satisfies all "
                      "sub-contracts — decomposition over-constrains")
    return errors


# ---------------------------------------------------------------------------
# Placement metrics (continuation of the IV-2020 metric line)
# ---------------------------------------------------------------------------

def placement_metrics(contracts: list[Contract]) -> dict:
    """Locality: fraction of monitored signals that are local to the service.
    Coupling: number of cross-service observation edges."""
    out = {}
    total_cross = 0
    for c in contracts:
        obs = c.guarantee.signals() | c.assumption.signals()
        local = obs & c.service.signature
        cross = obs - c.service.signature
        total_cross += len(cross)
        out[c.name] = {
            "observed": len(obs),
            "locality": len(local) / len(obs) if obs else 1.0,
            "cross_edges": len(cross),
        }
    out["_total_cross_edges"] = total_cross
    return out


# ---------------------------------------------------------------------------
# Toy example: safe-distance warning rule (R_G1-style), 10 Hz, horizon 8
# ---------------------------------------------------------------------------

def build_example():
    # Signals
    d_true = Signal("d_true")        # actual gap to lead vehicle [m] (env)
    d_est = Signal("d_est")          # estimated gap [m] (perception output)
    warn = Signal("warn", "bool")    # takeover/brake warning (planning output)

    # Services (αSOA signatures, Autoware-style component granularity)
    env = Service("Env", set(), {d_true})
    perception = Service("Perception", {d_true}, {d_est})
    planning = Service("Planning", {d_est}, {warn})

    D_SAFE = 50.0   # safe distance threshold [m]
    EPS = 2.0       # perception error bound [m]
    K = 3           # reaction budget: 3 steps @ 10 Hz = 300 ms

    def atom(label, sigs, fn):
        return Atom(fn, sigs, label)

    # --- System-level contract (the traffic rule, service-anchored) -------
    # A_sys: the world delivers plausible distances
    a_sys = G(atom("0≤d_true≤200", {d_true},
                   lambda v: z3.And(v["d_true"] >= 0, v["d_true"] <= 200)))
    # G_sys: if the gap is unsafe, a warning follows within K steps
    g_sys = G(Implies(atom("d_true≤D_SAFE", {d_true},
                           lambda v: v["d_true"] <= D_SAFE),
                      F(0, K, atom("warn", {warn}, lambda v: v["warn"]))))
    c_sys = Contract(Service("System", {d_true}, {warn}), a_sys, g_sys,
                     "C_sys")

    # --- Correct decomposition --------------------------------------------
    a_perc = G(atom("0≤d_true≤200", {d_true},
                    lambda v: z3.And(v["d_true"] >= 0, v["d_true"] <= 200)))
    g_perc = G(atom("|d_est−d_true|≤ε", {d_true, d_est},
                    lambda v: z3.And(v["d_est"] - v["d_true"] <= EPS,
                                     v["d_true"] - v["d_est"] <= EPS)))
    c_perc = Contract(perception, a_perc, g_perc)

    a_plan = G(atom("d_est≥−ε", {d_est}, lambda v: v["d_est"] >= -EPS))
    # threshold D_SAFE + ε compensates the perception error bound
    g_plan_ok = G(Implies(atom("d_est≤D_SAFE+ε", {d_est},
                               lambda v: v["d_est"] <= D_SAFE + EPS),
                          F(0, K, atom("warn", {warn}, lambda v: v["warn"]))))
    c_plan_ok = Contract(planning, a_plan, g_plan_ok, "C_Planning")

    # --- Faulty decomposition (forgotten ε margin) ------------------------
    g_plan_bad = G(Implies(atom("d_est≤D_SAFE", {d_est},
                                lambda v: v["d_est"] <= D_SAFE),
                           F(0, K, atom("warn", {warn}, lambda v: v["warn"]))))
    c_plan_bad = Contract(planning, a_plan, g_plan_bad, "C_Planning(bug)")

    # --- V1-violating contract (guarantee peeks at ground truth) ----------
    g_plan_illegal = G(Implies(atom("d_true≤D_SAFE", {d_true},
                                    lambda v: v["d_true"] <= D_SAFE),
                               F(0, K, atom("warn", {warn},
                                            lambda v: v["warn"]))))
    c_plan_illegal = Contract(planning, a_plan, g_plan_illegal,
                              "C_Planning(illegal)")

    signals = {d_true, d_est, warn}
    return (c_sys, c_perc, c_plan_ok, c_plan_bad, c_plan_illegal, signals)


def run_suite(c_sys, contracts, signals, horizon, label):
    print(f"\n=== Decomposition: {label} ===")
    errors = []
    errors += check_V1(contracts)
    if not errors:
        errors += check_V2(contracts, signals, horizon)
    if not errors:
        errors += check_V3(c_sys, contracts,
                           [c.service.name for c in contracts],
                           signals, horizon)
        errors += check_V4(c_sys, contracts, signals, horizon)
    if errors:
        print("  ✗ INVALID:")
        for e in errors:
            print(f"    {e}")
    else:
        print("  ✓ VALID (V1–V4 passed)")
        m = placement_metrics(contracts)
        for k, v in m.items():
            if k.startswith("_"):
                print(f"  metric total: cross-service edges = {v}")
            else:
                print(f"  metric {k}: locality={v['locality']:.2f}, "
                      f"observed signals={v['observed']}, "
                      f"cross={v['cross_edges']}")
    return errors


if __name__ == "__main__":
    HORIZON = 8  # 8 steps @ 10 Hz; future depth K=3 fits
    (c_sys, c_perc, c_plan_ok, c_plan_bad, c_plan_illegal,
     signals) = build_example()

    print("System-level rule (service-anchored):")
    print(f"  A_sys = {c_sys.assumption}")
    print(f"  G_sys = {c_sys.guarantee}")

    e1 = run_suite(c_sys, [c_perc, c_plan_ok], signals, HORIZON,
                   "correct (threshold D_SAFE + eps)")
    e2 = run_suite(c_sys, [c_perc, c_plan_bad], signals, HORIZON,
                   "faulty (eps margin forgotten)")
    e3 = run_suite(c_sys, [c_perc, c_plan_illegal], signals, HORIZON,
                   "illegal (Planning guarantee reads d_true)")

    print("\n=== Summary ===")
    assert not e1, "correct decomposition must be valid"
    assert any("V3b" in e for e in e2), "bug must be caught by V3b"
    assert any("V1" in e for e in e3), "signature violation must trigger V1"
    print("All expectations met: V1 catches signature violations, V3b finds "
          "the forgotten eps margin incl. counterexample trace, and the "
          "correct decomposition is provably valid.")
