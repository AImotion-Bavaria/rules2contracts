#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rtamt monitoring backend — the second backend over the same contract DSL.

The same rule documents (contracts/*.yaml) drive two engines:

    z3 (check_contracts.py)   -> proof:      is the decomposition valid?
    rtamt (this module)       -> monitoring: is a concrete trace compliant,
                                             and if not, which service's
                                             contract is to blame?

The DSL is translated token-wise into rtamt's discrete-time STL syntax:
    ->            =>  implies
    F[a,b](...)   =>  eventually[a:b](...)
    H[a,b](...)   =>  historically[a:b](...)
    O[a,b](...)   =>  once[a:b](...)
    bool signal s =>  (s >= 0.5)          (bool encoded as 0/1 float)
    constants     =>  numeric literals
The outermost G is dropped: rtamt evaluates the body pointwise, so
violations localize in time (robustness < 0 at step t).
"""

from __future__ import annotations
import pathlib
import yaml

import rtamt

from stl_ag_checker import Signal, Atom, Not, And, Or, Implies, F, Hist, Once
from contract_parser import parse_contract_formula, ABS_INNER


# ---------------------------------------------------------------------------
# DSL -> rtamt translation (via the parsed AST, fully parenthesized at the
# Boolean level). Arithmetic atoms are NOT handed to rtamt as expressions:
# rtamt's expression grammar has proven version-fragile (no parentheses
# inside real expressions; some releases apply inverted operator precedence,
# silently mis-evaluating `a * x + c` as `a * (x + c)`). Instead, every
# linear atom `lhs op rhs` becomes a fresh auxiliary signal
# atomK = lhs - rhs computed in Python, and the spec only contains
# `(atomK op 0)` -- which preserves the quantitative robustness margins
# exactly and keeps rtamt to Boolean/temporal structure it parses reliably.
# ---------------------------------------------------------------------------

def _lin_value(lin: dict, trace: dict, t: int) -> float:
    """Numeric value of a linear form at step t (abs terms via ABS_INNER)."""
    total = 0.0
    for base, coeff in lin.items():
        if base is None:
            total += coeff
        elif isinstance(base, tuple):  # ("abs", canonical inner string)
            total += coeff * abs(_lin_value(ABS_INNER[base[1]], trace, t))
        else:
            total += coeff * trace[base][t]
    return total


class _Translation:
    """Collects auxiliary atom signals while rendering the Boolean/temporal
    structure of a formula for rtamt."""

    def __init__(self):
        self.atoms: dict[str, tuple[dict, dict]] = {}  # name -> (lhs, rhs)

    def render(self, phi) -> str:
        if isinstance(phi, Atom):
            if not hasattr(phi, "linear"):   # bare boolean signal atom
                return f"({phi.label} >= 0.5)"
            lhs, op, rhs = phi.linear
            name = f"atom{len(self.atoms)}"
            self.atoms[name] = (lhs, rhs)
            return f"({name} {op} 0)"
        if isinstance(phi, Not):
            return f"(not {self.render(phi.phi)})"
        if isinstance(phi, And):
            return "(" + " and ".join(self.render(p) for p in phi.phis) + ")"
        if isinstance(phi, Or):
            return "(" + " or ".join(self.render(p) for p in phi.phis) + ")"
        if isinstance(phi, Implies):
            return f"({self.render(phi.lhs)} implies {self.render(phi.rhs)})"
        if isinstance(phi, (F, Hist, Once)):
            name = {F: "eventually", Hist: "historically",
                    Once: "once"}[type(phi)]
            return f"({name}[{phi.a}:{phi.b}]{self.render(phi.phi)})"
        raise TypeError(f"unsupported node {type(phi).__name__}")


def dsl_to_rtamt(text: str, constants: dict[str, float],
                 bool_signals: set[str],
                 all_signals: set[str]) -> tuple[str, set[str], dict]:
    """Translate a contract formula (outermost G mandatory) into an rtamt
    spec string evaluated pointwise (outer G dropped, so violations
    localize in time). Returns (spec, used_signal_names, atom_defs), where
    atom_defs maps auxiliary signal names to (lhs, rhs) linear forms that
    the evaluator must compute per step as lhs - rhs."""
    signal_table = {n: Signal(n, "bool" if n in bool_signals else "real")
                    for n in all_signals}
    g = parse_contract_formula(text, signal_table, constants)
    tr = _Translation()
    spec = tr.render(g.phi)
    used = {s.name for s in g.signals()}
    return spec, used, tr.atoms


def build_spec(name: str, spec_str: str, signal_names: set[str]):
    spec = rtamt.StlDiscreteTimeSpecification()
    spec.name = name
    for s in signal_names:
        spec.declare_var(s, "float")
    spec.spec = spec_str
    spec.parse()
    return spec


# ---------------------------------------------------------------------------
# Rule monitor: one rule document -> a family of monitors
# ---------------------------------------------------------------------------

class RuleMonitor:
    """Builds rtamt monitors for the system contract and every sub-contract
    of a rule document, and evaluates/localizes on concrete traces."""

    def __init__(self, doc_path: str | pathlib.Path):
        doc = yaml.safe_load(pathlib.Path(doc_path).read_text())
        self.rule_id = doc.get("rule_id", "?")
        constants = {k: float(v)
                     for k, v in (doc.get("constants") or {}).items()}
        env = {s["name"]: s.get("sort", "real")
               for s in (doc.get("environment", {}).get("signals") or [])}
        # bool signals: from env decls and known feature sorts
        self.bool_signals = {n for n, sort in env.items() if sort == "bool"}
        self.bool_signals |= {"hazard_on"}  # feature layer bool signals
        self.all_signals = set(env)
        for entry in doc["decomposition"]["contracts"]:
            self.all_signals |= set(entry["interface"].get("inputs") or [])
            self.all_signals |= set(entry["interface"].get("outputs") or [])
        sc = doc["system_contract"]
        self.all_signals |= set(sc["interface"].get("inputs") or [])
        self.all_signals |= set(sc["interface"].get("outputs") or [])

        def make(name, formula):
            spec_str, used, atoms = dsl_to_rtamt(
                formula, constants, self.bool_signals, self.all_signals)
            return build_spec(name, spec_str, used | set(atoms)), used, atoms

        self.system = {
            "assumption": make("sys_A", sc["assumption"]),
            "guarantee": make("sys_G", sc["guarantee"]),
        }
        self.services: dict[str, dict] = {}
        for entry in doc["decomposition"]["contracts"]:
            svc = entry["service"]
            self.services[svc] = {
                "assumption": make(f"{svc}_A", entry["assumption"]),
                "guarantee": make(f"{svc}_G", entry["guarantee"]),
            }

    # -- evaluation ---------------------------------------------------------

    @staticmethod
    def _evaluate(spec_used, trace: dict[str, list[float]]):
        spec, used, atoms = spec_used
        n = len(trace["time"])
        data = {"time": trace["time"]}
        for s in used:
            data[s] = trace[s]
        for name, (lhs, rhs) in atoms.items():  # auxiliary atom signals
            data[name] = [_lin_value(lhs, trace, t) - _lin_value(rhs, trace, t)
                          for t in range(n)]
        rob = spec.evaluate(data)
        return [(int(t), float(r)) for t, r in rob[:n]]

    def evaluate(self, trace) -> dict:
        """Robustness per step for the system guarantee and each service
        guarantee/assumption."""
        res = {"system": {k: self._evaluate(v, trace)
                          for k, v in self.system.items()}}
        for svc, specs in self.services.items():
            res[svc] = {k: self._evaluate(v, trace)
                        for k, v in specs.items()}
        return res

    @staticmethod
    def violations(rob: list[tuple[int, float]]) -> list[int]:
        return [t for t, r in rob if r < 0]

    def localize(self, trace) -> dict:
        """Fault localization on a trace: if the system guarantee is violated,
        report which services broke their own guarantee (while their
        assumption held) — the architectural value of contract monitors."""
        res = self.evaluate(trace)
        sys_viol = self.violations(res["system"]["guarantee"])
        report = {"rule": self.rule_id,
                  "system_violation_steps": sys_viol,
                  "blame": {}}
        for svc in self.services:
            a_viol = set(self.violations(res[svc]["assumption"]))
            g_viol = self.violations(res[svc]["guarantee"])
            blamed = [t for t in g_viol if t not in a_viol]
            report["blame"][svc] = {
                "guarantee_violation_steps": g_viol,
                "assumption_violation_steps": sorted(a_viol),
                "culpable": bool(blamed),
            }
        return report
