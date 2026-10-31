#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI: load a rule-contract document (YAML, the LLM output format), parse all
formulas, and run the full validity suite:

  V0  architecture conformance of declared service interfaces
      (against autoware_signatures.yaml)
  V1  well-formedness of assumptions/guarantees w.r.t. declared interfaces
  V2  consistency / non-vacuity
  V3  correctness of the decomposition (assumption discharge + entailment)
  V4  tightness indicator

Usage:
    python3 check_contracts.py contracts/R_G1.yaml
"""

from __future__ import annotations
import pathlib
import sys
import yaml

from stl_ag_checker import (Signal, Service, Contract, check_V1, check_V2,
                            check_V3, check_V4, placement_metrics)
from contract_parser import parse_contract_formula, ContractSyntaxError
from autoware_services import AutowareArchitecture


def load_document(path: pathlib.Path, arch: AutowareArchitecture):
    doc = yaml.safe_load(path.read_text())
    constants = {k: float(v) for k, v in (doc.get("constants") or {}).items()}

    # signal table: environment signals + architecture features
    signals: dict[str, Signal] = {}
    env_names: set[str] = set()
    for s in (doc.get("environment", {}).get("signals") or []):
        signals[s["name"]] = Signal(s["name"], s.get("sort", "real"))
        env_names.add(s["name"])
    for fname in arch.features:
        signals[fname] = arch.feature_signal(fname)

    def iface(spec) -> tuple[set[Signal], set[Signal]]:
        for n in (spec.get("inputs") or []) + (spec.get("outputs") or []):
            if n not in signals:
                raise ValueError(
                    f"unknown signal {n!r} in an interface declaration: "
                    f"declare it as an environment signal or use one of the "
                    f"architecture features")
        ins = {signals[n] for n in (spec.get("inputs") or [])}
        outs = {signals[n] for n in (spec.get("outputs") or [])}
        return ins, outs

    # system-level contract
    sc = doc["system_contract"]
    ins, outs = iface(sc["interface"])
    c_sys = Contract(
        Service("System", ins, outs),
        parse_contract_formula(sc["assumption"], signals, constants),
        parse_contract_formula(sc["guarantee"], signals, constants),
        "C_sys")

    # sub-contracts
    contracts, v0_errors = [], []
    for entry in doc["decomposition"]["contracts"]:
        name = entry["service"]
        ins, outs = iface(entry["interface"])
        # V0: declared interface must conform to the architecture
        for sig in outs:
            if sig.name in env_names:
                v0_errors.append(f"[V0] {name}: environment signal "
                                 f"{sig.name!r} declared as output")
            elif sig.name not in arch.features:
                v0_errors.append(f"[V0] {name}: output {sig.name!r} is not "
                                 f"a feature of the architecture")
            elif arch.features[sig.name].component != name:
                v0_errors.append(
                    f"[V0] {name}: output {sig.name!r} is computed by "
                    f"{arch.features[sig.name].component}, not {name}")
        for sig in ins:
            if sig.name in env_names:
                continue  # abstracted sensing chain: env signals permitted
            feat = arch.features.get(sig.name)
            if feat is None:
                v0_errors.append(f"[V0] {name}: unknown input {sig.name!r}")
                continue
            producer_ports = [p for p in arch.ports
                              if p.component == feat.component
                              and p.name == feat.port and p.direction == "out"]
            consumed = any(p.component == name and p.direction == "in"
                           and producer_ports
                           and p.topic == producer_ports[0].topic
                           for p in arch.ports)
            if not consumed:
                v0_errors.append(
                    f"[V0] {name}: input {sig.name!r} — {name} does not "
                    f"consume topic of {feat.component}.{feat.port} "
                    f"per the documented architecture")
        contracts.append(Contract(
            Service(name, ins, outs),
            parse_contract_formula(entry["assumption"], signals, constants),
            parse_contract_formula(entry["guarantee"], signals, constants),
            f"C_{name}"))

    order = doc["decomposition"].get(
        "discharge_order", [c.service.name for c in contracts])
    cnames = [c.service.name for c in contracts]
    if sorted(order) != sorted(cnames):
        v0_errors.append(
            f"[SCHEMA] discharge_order {order} must list exactly the "
            f"services of the decomposition {cnames}")
    # horizon sanity: enough evaluation steps for every bounded-future
    # window (otherwise G would be checked on an empty range and the
    # entailments would hold vacuously), and an upper bound for cost
    horizon = int(doc.get("horizon", 8))
    fd_max = max([c_sys.assumption.future_depth(),
                  c_sys.guarantee.future_depth()]
                 + [f.future_depth() for c in contracts
                    for f in (c.assumption, c.guarantee)], default=0)
    if horizon < fd_max + 4:
        v0_errors.append(
            f"[SCHEMA] horizon {horizon} too small: must be at least "
            f"max future depth ({fd_max}) + 4")
    if horizon > 100:
        v0_errors.append(f"[SCHEMA] horizon {horizon} too large (max 100)")
    # restrict the trace vocabulary to signals actually used in formulas
    used: set[Signal] = set()
    for c in [c_sys] + contracts:
        used |= c.assumption.signals() | c.guarantee.signals()
    return doc, c_sys, contracts, order, used, horizon, v0_errors


def validate(path: pathlib.Path, arch: AutowareArchitecture) -> list[str]:
    """Programmatic entry point (used by the LLM repair loop): run V0–V4 on
    a rule document, return the list of error strings (empty = valid).
    Counterexample traces printed by V3 are captured into the error list."""
    import io
    import contextlib
    try:
        (_, c_sys, contracts, order, signals, horizon,
         errors) = load_document(path, arch)
    except ContractSyntaxError as e:
        return [f"[SYNTAX] {e}"]
    except (KeyError, TypeError, ValueError) as e:
        return [f"[SCHEMA] malformed rule document: {e!r}"]
    if errors:
        return errors
    errors += check_V1(contracts)
    if not errors:
        errors += check_V2(contracts, signals, horizon)
    if not errors:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors += check_V3(c_sys, contracts, order, signals, horizon)
            errors += check_V4(c_sys, contracts, signals, horizon)
        if buf.getvalue():
            errors.append(buf.getvalue().rstrip())
    return errors


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    here = pathlib.Path(__file__).parent
    arch = AutowareArchitecture(here / "autoware_signatures.yaml")
    path = pathlib.Path(argv[1])

    print(f"Rule document: {path}")
    try:
        (doc, c_sys, contracts, order, signals, horizon,
         errors) = load_document(path, arch)
    except ContractSyntaxError as e:
        print(f"  ✗ REJECTED (syntax): {e}")
        return 1

    print(f"  rule: {doc.get('rule_id', '?')} — "
          f"{doc.get('description', '').strip()}")
    print(f"  system contract: A = {c_sys.assumption}")
    print(f"                   G = {c_sys.guarantee}")
    print(f"  decomposition: {', '.join(c.name for c in contracts)} "
          f"(horizon={horizon})")

    if errors:
        print("  ✗ INVALID (V0 architecture conformance):")
        for e in errors:
            print(f"    {e}")
        return 1

    errors += check_V1(contracts)
    if not errors:
        errors += check_V2(contracts, signals, horizon)
    if not errors:
        errors += check_V3(c_sys, contracts, order, signals, horizon)
        errors += check_V4(c_sys, contracts, signals, horizon)

    if errors:
        print("  ✗ INVALID:")
        for e in errors:
            print(f"    {e}")
        return 1

    print("  ✓ VALID (V0–V4 passed)")
    m = placement_metrics(contracts)
    for k, v in m.items():
        if k.startswith("_"):
            print(f"  metric total: cross-service edges = {v}")
        else:
            print(f"  metric {k}: locality={v['locality']:.2f}, "
                  f"observed={v['observed']}, cross={v['cross_edges']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
