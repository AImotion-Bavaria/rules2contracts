#!/usr/bin/env python3
"""RQ4 sweep: contract monitors on CommonRoad traces with fault injection.

Runs every vehicle-following episode of the given scenarios through all
four injection modes (nominal + one fault per service), evaluates the
per-service contract monitors, and appends one CSV row per (episode, mode):

  scenario, ego, lead, steps, mode, sys_viol, sys_first, culpable,
  localisation

where `localisation` classifies the outcome against the injected ground
truth: for fault modes `exact` (only the faulty service blamed),
`inactive` (the fault was never exercised: nothing violated anywhere,
e.g. a delayed planner whose trigger never fires in the episode), `miss`
(something is violated but the faulty service is not blamed), or
`spurious` (extra services blamed); for nominal runs `clean` (no blame)
or `false_positive`. Fault magnitude is
fixed at the module defaults of commonroad_traces (3x the contract bound)
-- episodes in which the fault breaks the contract without breaking the
system rule are a result, not a failure of the setup.

Usage:
    python3 run_rq4.py scenarios/USA_*.xml --rule contracts/R_G1.yaml \
        --out rq4_results.csv
    python3 run_rq4.py --dataset highd highd/data/01 ... \
        --rule contracts/R_G1.yaml --out rq4_highd_g1.csv
    python3 run_rq4.py --dataset highd highd/data/01 ... \
        --rule contracts/R_G3.yaml --out rq4_highd_g3.csv

With --dataset highd the positional arguments are recording prefixes of
the licensed highD dataset (Krajewski et al., ITSC 2018). For rule R_G3
the episodes are single-vehicle speed tracks (fault modes: nominal and
planning_fault); recordings without a posted speed limit are skipped.
"""

import argparse
import pathlib

import yaml

from commonroad_traces import (find_following_pairs, ground_truth,
                               load_scenario, synthesise_features)
from monitor_backend import RuleMonitor

MODES_G1 = {"nominal": None, "perception_fault": "Perception",
            "localization_fault": "Localization",
            "planning_fault": "Planning"}
MODES_G3 = {"nominal": None, "planning_fault": "Planning"}


def classify(modes: dict, mode: str, culpable: set[str],
             sys_viol: int) -> str:
    injected = modes[mode]
    if injected is None:
        return "clean" if not culpable else "false_positive"
    if culpable == {injected}:
        return "exact"
    if not culpable and sys_viol == 0:
        return "inactive"
    if injected not in culpable:
        return "miss"
    return "spurious"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="+", type=pathlib.Path,
                        help="CommonRoad XMLs, or highD recording "
                             "prefixes with --dataset highd")
    parser.add_argument("--dataset", choices=["commonroad", "highd"],
                        default="commonroad")
    parser.add_argument("--rule", type=pathlib.Path,
                        default=pathlib.Path("contracts/R_G1.yaml"))
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path("rq4_results.csv"))
    parser.add_argument("--max-pairs", type=int, default=0,
                        help="cap episodes per scenario (0 = all)")
    args = parser.parse_args()

    doc = yaml.safe_load(args.rule.read_text())
    constants = {k: float(v) for k, v in doc["constants"].items()}
    rule_id = doc["rule_id"]
    if rule_id == "R_G3" and args.dataset != "highd":
        raise SystemExit("R_G3 monitoring needs the highD speed-limit "
                         "metadata; use --dataset highd")
    modes = MODES_G3 if rule_id == "R_G3" else MODES_G1
    monitor = RuleMonitor(args.rule)

    header = ("scenario,ego,lead,steps,mode,sys_viol,sys_first,"
              "culpable,localisation")
    new_file = not args.out.exists()
    with args.out.open("a") as out:
        if new_file:
            out.write(header + "\n")
        for path in args.scenarios:
            if args.dataset == "highd":
                import highd_traces as hd
                rec = hd.load_recording(path)
                if rule_id == "R_G3":
                    if rec["speed_limit"] is None:
                        print(f"{rec['id']}: no posted speed limit, skipped")
                        continue
                    episodes = hd.single_tracks(rec)
                else:
                    episodes = hd.find_following_pairs(rec)
                name = f"highd_{rec['id']}"
            else:
                scenario = load_scenario(path)
                episodes = find_following_pairs(scenario)
                name = path.name
            if args.max_pairs:
                episodes = episodes[:args.max_pairs]
            print(f"{name}: {len(episodes)} episodes")
            for pair in episodes:
                if args.dataset == "highd" and rule_id == "R_G3":
                    gt = hd.ground_truth_speed(rec, pair)
                elif args.dataset == "highd":
                    gt = hd.ground_truth(rec, pair)
                else:
                    gt = ground_truth(scenario, pair)
                if len(gt["time"]) < 40:
                    continue
                for mode in modes:
                    if rule_id == "R_G3":
                        trace = hd.synthesise_features_g3(gt, constants, mode)
                    else:
                        trace = synthesise_features(gt, constants, mode)
                    report = monitor.localize(trace)
                    sys_viol = report["system_violation_steps"]
                    culpable = {svc for svc, b in report["blame"].items()
                                if b["culpable"]}
                    lead = pair.get("lead", "")
                    out.write(
                        f"{name},{pair['ego']},{lead},"
                        f"{len(gt['time'])},{mode},{len(sys_viol)},"
                        f"{sys_viol[0] if sys_viol else ''},"
                        f"{'+'.join(sorted(culpable))},"
                        f"{classify(modes, mode, culpable, len(sys_viol))}\n")
                    out.flush()

    # summary over the WHOLE file (including earlier batches)
    rows = [line.strip().split(",")
            for line in args.out.read_text().splitlines()[1:]]
    print(f"\n{len(rows)} rows in {args.out}")
    for mode in modes:
        sel = [r for r in rows if r[4] == mode]
        if not sel:
            continue
        viol = sum(1 for r in sel if int(r[5]) > 0)
        outcomes = {}
        for r in sel:
            outcomes[r[8]] = outcomes.get(r[8], 0) + 1
        print(f"  {mode:<20} n={len(sel):>3}  system-violations={viol:>3}  "
              + "  ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))


if __name__ == "__main__":
    main()
