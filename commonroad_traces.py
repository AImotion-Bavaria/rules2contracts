#!/usr/bin/env python3
"""Turn CommonRoad scenarios into monitorable traces for the rule monitors.

Reads a CommonRoad scenario XML (formats 2018b and 2020a, stdlib parser --
no commonroad-io dependency), finds vehicle-following pairs among the
recorded dynamic obstacles, and extracts the ground-truth signals of the
safe-distance rule at 10 Hz:

  v_true  -- velocity of the following vehicle (the "ego" of the pair)
  d_gap   -- bumper-to-bumper longitudinal gap to the lead vehicle

On top of the ground truth, the module synthesises the architecture-side
signals so that the per-service contract monitors have something to judge:
feature estimates with injected estimation errors (v_ego, d_lead) and a
simple conformant planner model for hazard_on. Error injection is
deterministic (sinusoidal error curves, no RNG) and comes in four modes:

  nominal            -- all errors within the contract bounds
  perception_fault   -- d_lead biased beyond EPS_D inside a fault window
  localization_fault -- v_ego biased beyond EPS_V inside a fault window
  planning_fault     -- planner reaction delayed beyond the F[0,3] bound

Usage (also see the joint-experiment driver at the bottom):
    python3 commonroad_traces.py scenarios/USA_US101-4_1_T-1.xml \
        --rule contracts/R_G1.yaml --mode perception_fault
"""

import argparse
import math
import pathlib
import xml.etree.ElementTree as ET

import yaml

# Pair-finding parameters: a lead vehicle must stay within this lateral
# corridor and heading difference of the follower, for at least this many
# consecutive 10-Hz steps.
LATERAL_TOL_M = 1.8
HEADING_TOL_RAD = 0.35
MIN_OVERLAP_STEPS = 40

TARGET_DT = 0.1  # the feature layer samples at 10 Hz


# ---------------------------------------------------------------------------
# Scenario parsing (formats 2018b and 2020a)
# ---------------------------------------------------------------------------

def _exact(elem):
    return float(elem.find("exact").text)


def _parse_states(traj_elem):
    states = {}
    for st in traj_elem.findall("state"):
        p = st.find("position/point")
        if p is None:  # 2018b allows lanelet-referenced positions; skip those
            continue
        t = int(_exact(st.find("time")))
        states[t] = (float(p.find("x").text), float(p.find("y").text),
                     _exact(st.find("orientation")),
                     _exact(st.find("velocity")))
    return states


def load_scenario(path: pathlib.Path) -> dict:
    """Return {"dt": float, "vehicles": {id: {"length": float,
    "states": {step: (x, y, orientation, velocity)}}}}."""
    root = ET.parse(path).getroot()
    dt = float(root.attrib["timeStepSize"])
    vehicles = {}

    def add(obst_id, type_text, shape_elem, init_elem, traj_elem):
        if type_text not in ("car", "truck", "bus"):
            return
        if traj_elem is None:
            return
        states = _parse_states(traj_elem)
        if init_elem is not None:
            p = init_elem.find("position/point")
            if p is not None:
                t0 = int(_exact(init_elem.find("time")))
                states[t0] = (float(p.find("x").text), float(p.find("y").text),
                              _exact(init_elem.find("orientation")),
                              _exact(init_elem.find("velocity")))
        length = float(shape_elem.find("rectangle/length").text)
        width_elem = shape_elem.find("rectangle/width")
        width = float(width_elem.text) if width_elem is not None else 1.8
        vehicles[obst_id] = {"length": length, "width": width,
                             "states": states}

    for o in root.findall("dynamicObstacle"):  # 2020a
        add(o.attrib["id"], o.find("type").text, o.find("shape"),
            o.find("initialState"), o.find("trajectory"))
    for o in root.findall("obstacle"):  # 2018b
        if o.find("role") is not None and o.find("role").text == "dynamic":
            add(o.attrib["id"], o.find("type").text, o.find("shape"),
                o.find("initialState"), o.find("trajectory"))
    return {"dt": dt, "vehicles": vehicles}


# ---------------------------------------------------------------------------
# Vehicle-following pairs and ground-truth signals
# ---------------------------------------------------------------------------

def _gap(ego_state, lead_state, ego_len, lead_len):
    """Longitudinal bumper gap of lead w.r.t. ego, or None if the lead is
    not ahead of ego in ego's driving direction and lateral corridor."""
    ex, ey, eo, _ = ego_state
    lx, ly, lo, _ = lead_state
    dx, dy = lx - ex, ly - ey
    s = dx * math.cos(eo) + dy * math.sin(eo)       # longitudinal offset
    d = -dx * math.sin(eo) + dy * math.cos(eo)      # lateral offset
    dh = abs((lo - eo + math.pi) % (2 * math.pi) - math.pi)
    if s <= 0 or abs(d) > LATERAL_TOL_M or dh > HEADING_TOL_RAD:
        return None
    return s - (ego_len + lead_len) / 2.0


def find_following_pairs(scenario: dict) -> list[dict]:
    """All (ego, lead) pairs with an unbroken following episode of at least
    MIN_OVERLAP_STEPS (after resampling to 10 Hz), longest episodes first."""
    step = max(1, round(TARGET_DT / scenario["dt"]))  # subsample if dt < target
    repeat = max(1, round(scenario["dt"] / TARGET_DT))  # interpolate if coarser
    pairs = []
    vehicles = scenario["vehicles"]
    for ego_id, ego in vehicles.items():
        for lead_id, lead in vehicles.items():
            if lead_id == ego_id:
                continue
            common = sorted(set(ego["states"]) & set(lead["states"]))
            episode = []
            best = None
            for t in common:
                g = _gap(ego["states"][t], lead["states"][t],
                         ego["length"], lead["length"])
                nearest = g is not None and all(
                    (g2 := _gap(ego["states"][t], other["states"][t],
                                ego["length"], other["length"])) is None
                    or g2 >= g
                    for oid, other in vehicles.items()
                    if oid not in (ego_id, lead_id) and t in other["states"])
                if nearest and (not episode or t == episode[-1] + 1):
                    episode.append(t)
                else:
                    if len(episode) * repeat // step >= MIN_OVERLAP_STEPS \
                            and (best is None or len(episode) > len(best)):
                        best = episode
                    episode = [t] if nearest else []
            if len(episode) * repeat // step >= MIN_OVERLAP_STEPS \
                    and (best is None or len(episode) > len(best)):
                best = episode
            if best:
                pairs.append({"ego": ego_id, "lead": lead_id, "steps": best})
    return sorted(pairs, key=lambda p: -len(p["steps"]))


def ground_truth(scenario: dict, pair: dict) -> dict:
    """10-Hz ground-truth signals v_true and d_gap for a following episode."""
    ego = scenario["vehicles"][pair["ego"]]
    lead = scenario["vehicles"][pair["lead"]]
    v_true, d_gap = [], []
    for t in pair["steps"]:
        g = _gap(ego["states"][t], lead["states"][t],
                 ego["length"], lead["length"])
        v_true.append(ego["states"][t][3])
        d_gap.append(g)
    # resample the scenario's dt to the 10-Hz feature rate
    ratio = scenario["dt"] / TARGET_DT
    if ratio > 1:  # coarser recording: linear interpolation
        n = int((len(v_true) - 1) * ratio) + 1
        def lerp(seq, i):
            x = i / ratio
            k = min(int(x), len(seq) - 2)
            return seq[k] + (seq[k + 1] - seq[k]) * (x - k)
        v_true = [lerp(v_true, i) for i in range(n)]
        d_gap = [lerp(d_gap, i) for i in range(n)]
    elif ratio < 1:  # finer recording: subsample
        step = round(1 / ratio)
        v_true, d_gap = v_true[::step], d_gap[::step]
    return {"time": list(range(len(v_true))), "v_true": v_true, "d_gap": d_gap}


def threshold_crossing(gt: dict, tau: float) -> int | None:
    """First step at which the safe-distance premise d_gap <= TAU * v_true
    becomes true after having been false (None if never or always true)."""
    above = False
    for t in gt["time"]:
        if gt["d_gap"][t] > tau * gt["v_true"][t]:
            above = True
        elif above:
            return t
    return None


# ---------------------------------------------------------------------------
# Feature synthesis: bounded estimation errors and a conformant planner
# ---------------------------------------------------------------------------

def synthesise_features(gt: dict, constants: dict, mode: str = "nominal",
                        fault_window: tuple[int, int] | None = None) -> dict:
    """Add v_ego, d_lead, hazard_on to a ground-truth trace.

    Estimation errors are deterministic sinusoids at 80 % of the contract
    bound; a fault mode biases one signal beyond its bound (or delays the
    planner beyond its deadline) inside fault_window. The default window
    brackets the step at which the (nominal) planner would first raise the
    hazard -- the interesting moment: a fault there can suppress the hazard
    the system rule demands."""
    tau = constants["TAU"]
    eps_v, eps_d = constants["EPS_V"], constants["EPS_D"]
    n = len(gt["time"])

    def err(amp, period, phase, t):
        return amp * math.sin(2 * math.pi * t / period + phase)

    def signals(lo: int, hi: int):
        v_ego, d_lead = [], []
        for t in range(n):
            ev = err(0.8 * eps_v, 47, 0.3, t)
            ed = err(0.8 * eps_d, 61, 1.1, t)
            if mode == "localization_fault" and lo <= t <= hi:
                ev = 2.5 * eps_v
            if mode == "perception_fault" and lo <= t <= hi:
                ed = 3.0 * eps_d
            v_ego.append(gt["v_true"][t] + ev)
            d_lead.append(max(gt["d_gap"][t] + ed, 0.0))
        return v_ego, d_lead

    if fault_window is None:
        # nominal first-fire step of the planner determines where a fault
        # can actually suppress the hazard obligation
        v_nom, d_nom = signals(n, n)  # empty fault window -> nominal
        t_fire = next((t for t in range(n)
                       if d_nom[t] <= tau * (v_nom[t] + eps_v) + eps_d), None)
        anchor = t_fire if t_fire is not None else n // 3
        fault_window = (max(0, anchor - 5), min(n - 1, anchor + 35))
    lo, hi = fault_window
    v_ego, d_lead = signals(lo, hi)

    # conformant planner: once its (correctly compensated) trigger condition
    # first holds, hazard_on is raised within the F[0,3] deadline and stays
    # on for the rest of the episode (hazard lights are latching)
    delay = 5 if mode == "planning_fault" else 1
    hazard = [0.0] * n
    fired = next((t for t in range(n)
                  if d_lead[t] <= tau * (v_ego[t] + eps_v) + eps_d), None)
    if fired is not None:
        for t in range(min(fired + delay, n - 1), n):
            hazard[t] = 1.0
    return {**gt, "v_ego": v_ego, "d_lead": d_lead, "hazard_on": hazard}


# ---------------------------------------------------------------------------
# CLI: list pairs, build a trace, run the contract monitors
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", type=pathlib.Path,
                        help="CommonRoad scenario XML (2018b or 2020a)")
    parser.add_argument("--rule", type=pathlib.Path,
                        default=pathlib.Path("contracts/R_G1.yaml"))
    parser.add_argument("--mode", default="nominal",
                        choices=["nominal", "perception_fault",
                                 "localization_fault", "planning_fault"])
    parser.add_argument("--pair", type=int, default=0,
                        help="index into the detected following pairs")
    parser.add_argument("--dump", type=pathlib.Path,
                        help="optionally write the trace as CSV")
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    pairs = find_following_pairs(scenario)
    if not pairs:
        raise SystemExit("no vehicle-following episode of sufficient length")
    doc = yaml.safe_load(args.rule.read_text())
    constants = {k: float(v) for k, v in doc["constants"].items()}
    print(f"{args.scenario.name}: {len(scenario['vehicles'])} vehicles, "
          f"dt={scenario['dt']}s, {len(pairs)} following episodes")
    for i, p in enumerate(pairs[:8]):
        cross = threshold_crossing(ground_truth(scenario, p),
                                   constants["TAU"])
        print(f"  [{i}] ego {p['ego']} follows {p['lead']} for "
              f"{len(p['steps'])} recorded steps"
              + (f", threshold crossing at t={cross}" if cross is not None
                 else ""))
    pair = pairs[args.pair]
    trace = synthesise_features(ground_truth(scenario, pair),
                                constants, args.mode)
    print(f"episode: {len(trace['time'])} steps at 10 Hz, "
          f"v_true {min(trace['v_true']):.1f}..{max(trace['v_true']):.1f} m/s, "
          f"d_gap {min(trace['d_gap']):.1f}..{max(trace['d_gap']):.1f} m, "
          f"mode {args.mode}")

    if args.dump:
        cols = ["time", "v_true", "d_gap", "v_ego", "d_lead", "hazard_on"]
        with args.dump.open("w") as f:
            f.write(",".join(cols) + "\n")
            for t in trace["time"]:
                f.write(",".join(f"{trace[c][t]:.3f}" for c in cols) + "\n")
        print(f"trace written to {args.dump}")

    from monitor_backend import RuleMonitor
    report = RuleMonitor(args.rule).localize(trace)
    sys_viol = report["system_violation_steps"]
    print(f"system rule violated at steps: {sys_viol or 'never'}")
    for svc, blame in report["blame"].items():
        print(f"  {svc}: {blame}")


if __name__ == "__main__":
    main()
