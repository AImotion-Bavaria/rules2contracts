#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loader for Autoware component-level service signatures (aSOA view).

Reads `autoware_signatures.yaml` (extracted from the official Autoware
interface documentation) and materializes:

  * `Service` objects (aSOA signatures) — one per Autoware component,
    with topic-level ports,
  * a feature table — the abstraction layer that maps STL-atom vocabulary
    (v_ego, d_lead, ...) onto concrete component ports.

This is the architecture side of the pipeline: LLM-generated contracts must
be expressed over these features, and V1 well-formedness is checked against
the port ownership derived here.
"""

from __future__ import annotations
from dataclasses import dataclass
import pathlib
import yaml

from stl_ag_checker import Service, Signal


@dataclass(frozen=True)
class Port:
    component: str
    name: str
    topic: str
    msg: str
    direction: str  # 'in' | 'out'


@dataclass(frozen=True)
class Feature:
    """An observable quantity derived from one port's message payload.
    STL atoms range over features, not raw messages."""
    name: str
    unit: str
    component: str
    port: str
    extract: str
    rate_hz: float

    @property
    def sort(self) -> str:
        return "bool" if self.unit == "bool" else "real"


class AutowareArchitecture:
    def __init__(self, yaml_path: str | pathlib.Path):
        raw = yaml.safe_load(pathlib.Path(yaml_path).read_text())
        self.meta = raw.get("meta", {})
        self.ports: list[Port] = []
        self.components: dict[str, dict] = raw["components"]
        for comp, io in self.components.items():
            for direction, key in (("in", "inputs"), ("out", "outputs")):
                for p in io.get(key) or []:
                    self.ports.append(Port(comp, p["name"], p["topic"],
                                           p["msg"], direction))
        self.features: dict[str, Feature] = {}
        for f in raw.get("features", []):
            comp, port = f["port"].split(".", 1)
            self.features[f["name"]] = Feature(
                f["name"], f["unit"], comp, port, f["extract"], f["rate_hz"])

    # -- aSOA materialization ------------------------------------------------

    def feature_signal(self, name: str) -> Signal:
        f = self.features[name]
        return Signal(f.name, f.sort)

    def service(self, component: str,
                feature_subset: list[str] | None = None) -> Service:
        """Build an aSOA `Service` for a component. Ports are represented at
        feature granularity: a feature derived from one of the component's
        output ports becomes an output signal; features derived from ports
        this component consumes become input signals.

        `feature_subset` restricts the signature to the features relevant
        for a given rule (projection, aSOA Def. 4)."""
        inputs, outputs = set(), set()
        comp_ports = {(p.name, p.direction) for p in self.ports
                      if p.component == component}
        for fname, f in self.features.items():
            if feature_subset is not None and fname not in feature_subset:
                continue
            sig = self.feature_signal(fname)
            if f.component == component:
                # feature is computed by this component -> output
                if (f.port, "out") in comp_ports:
                    outputs.add(sig)
            else:
                # feature produced elsewhere; input iff this component
                # consumes the underlying topic
                producing = [p for p in self.ports
                             if p.component == f.component
                             and p.name == f.port and p.direction == "out"]
                if producing:
                    topic = producing[0].topic
                    consumes = any(p.component == component
                                   and p.direction == "in"
                                   and p.topic == topic
                                   for p in self.ports)
                    if consumes:
                        inputs.add(sig)
        return Service(component, inputs, outputs)

    def port_of_feature(self, name: str) -> Port:
        f = self.features[name]
        for p in self.ports:
            if p.component == f.component and p.name == f.port \
                    and p.direction == "out":
                return p
        raise KeyError(f"no producing port for feature {name}")

    def summary(self) -> str:
        lines = [f"Autoware architecture "
                 f"(accessed {self.meta.get('accessed', '?')}, "
                 f"granularity: {self.meta.get('granularity', '?')})"]
        for comp, io in self.components.items():
            n_in = len(io.get("inputs") or [])
            n_out = len(io.get("outputs") or [])
            lines.append(f"  {comp:<17} {n_in:>2} in / {n_out:>2} out ports")
        lines.append(f"  features: {', '.join(self.features)}")
        return "\n".join(lines)


if __name__ == "__main__":
    arch = AutowareArchitecture(
        pathlib.Path(__file__).parent / "autoware_signatures.yaml")
    print(arch.summary())
    print()
    for comp in ("Localization", "Perception", "Planning"):
        svc = arch.service(comp)
        print(f"aSOA service {svc.name}: "
              f"inputs={sorted(s.name for s in svc.inputs)}, "
              f"outputs={sorted(s.name for s in svc.outputs)}")
