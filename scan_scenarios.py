#!/usr/bin/env python3
"""Scan CommonRoad scenario files for usable vehicle-following episodes.

For every scenario XML given on the command line, loads the recorded
vehicles, detects following episodes (cf. commonroad_traces), and appends
one CSV line per scenario to the output file:

  file, format, dt, vehicles, episodes, best_ego, best_lead, best_steps,
  crossing, v_min, v_max, gap_min, gap_max

where "crossing" is the step at which the safe-distance premise
d_gap <= TAU * v_true first becomes true after being false (the episodes
worth having for fault-injection experiments), and the v/gap ranges refer
to the best (longest) episode. Errors are reported as format=ERROR lines,
so a batch never dies on a single malformed file.

Usage:
    python3 scan_scenarios.py --out scan_results.csv <scenario.xml> ...

Designed to be run in batches (append mode) over a large scenario pool.
"""

import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET

from commonroad_traces import (find_following_pairs, ground_truth,
                               load_scenario, threshold_crossing)

TAU = 1.8  # safe-distance factor of R_G1 (cf. contracts/R_G1.yaml)


def scan(path: pathlib.Path) -> str:
    fmt = ET.parse(path).getroot().attrib.get("commonRoadVersion", "?")
    scenario = load_scenario(path)
    pairs = find_following_pairs(scenario)
    if not pairs:
        return (f"{path.name},{fmt},{scenario['dt']},"
                f"{len(scenario['vehicles'])},0,,,,,,,,")
    best = pairs[0]
    gt = ground_truth(scenario, best)
    cross = threshold_crossing(gt, TAU)
    return (f"{path.name},{fmt},{scenario['dt']},"
            f"{len(scenario['vehicles'])},{len(pairs)},"
            f"{best['ego']},{best['lead']},{len(gt['time'])},"
            f"{'' if cross is None else cross},"
            f"{min(gt['v_true']):.1f},{max(gt['v_true']):.1f},"
            f"{min(gt['d_gap']):.1f},{max(gt['d_gap']):.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path("scan_results.csv"))
    args = parser.parse_args()

    header = ("file,format,dt,vehicles,episodes,best_ego,best_lead,"
              "best_steps,crossing,v_min,v_max,gap_min,gap_max")
    new_file = not args.out.exists()
    with args.out.open("a") as out:
        if new_file:
            out.write(header + "\n")
        for path in args.files:
            try:
                line = scan(path)
            except Exception as exc:  # keep scanning the rest of the batch
                line = f"{path.name},ERROR,,,,,,,,,,,{type(exc).__name__}"
            out.write(line + "\n")
            out.flush()
            print(line, file=sys.stderr)
    print(f"{len(args.files)} files scanned -> {args.out}")


if __name__ == "__main__":
    main()
