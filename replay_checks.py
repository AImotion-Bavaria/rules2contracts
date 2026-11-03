#!/usr/bin/env python3
"""Re-check every recorded candidate document with the verifier.

For each runs/<rule>/<model>/run_<k>/attempt_<n>.yaml the checker (V0-V4) is
executed afresh (exactly as the synthesis loop ran it: check_contracts.validate)
and the resulting error classes are compared with the classes recorded in the
run's result.json. Writes one CSV row per candidate document and exits
non-zero if any class set differs. No API key, no network, no model calls.
"""
import argparse
import csv
import json
import pathlib
import sys

import yaml

from analyze_runs import classify
from autoware_services import AutowareArchitecture
from check_contracts import validate

HERE = pathlib.Path(__file__).parent


def fresh_errors(path: pathlib.Path, arch) -> list[str]:
    try:
        yaml.safe_load(path.read_text())
        return validate(path, arch)
    except yaml.YAMLError as e:
        return [f"[YAML] not parseable as YAML: {e}"]
    except Exception as e:
        return [f"[CRASH] checker raised {type(e).__name__}: {str(e)[:300]}"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", type=pathlib.Path, default=HERE / "runs")
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path("out/check_replay.csv"))
    ap.add_argument("--timeout-ms", type=int, default=0,
                    help="per-query z3 timeout (0 = none); a timed-out query "
                         "shows up as a verdict mismatch instead of a hang")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="no per-run progress line")
    args = ap.parse_args()
    if args.timeout_ms:
        import z3
        z3.set_param("timeout", args.timeout_ms)
    arch = AutowareArchitecture(HERE / "autoware_signatures.yaml")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    import time
    rows, n, mismatches = [], 0, 0
    runs = sorted(args.runs_dir.glob("*/*/run_*/result.json"))
    t0 = time.perf_counter()
    for i, res in enumerate(runs, 1):
        rec = json.loads(res.read_text())
        if not args.quiet:
            print(f"[{i:3d}/{len(runs)}] {res.parents[2].name} {res.parents[1].name} "
                  f"{res.parent.name} ({rec.get('attempts', '?')} attempt(s))",
                  end=" ", flush=True)
        for h in rec.get("history", []):
            att = res.parent / f"attempt_{h['attempt']}.yaml"
            if not att.exists():
                continue
            n += 1
            t1 = time.perf_counter()
            recorded = "/".join(sorted(set(classify(h.get("errors", []))))) or "VALID"
            fresh = "/".join(sorted(set(classify(fresh_errors(att, arch))))) or "VALID"
            if not args.quiet:
                print(f"{fresh}:{time.perf_counter() - t1:.1f}s", end=" ", flush=True)
            agree = int(recorded == fresh)
            mismatches += 1 - agree
            rows.append({"rule": res.parents[2].name, "model": res.parents[1].name,
                         "run": res.parent.name, "attempt": h["attempt"],
                         "recorded": recorded, "fresh": fresh, "agree": agree})
        if not args.quiet:
            print(flush=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{n} candidate documents re-checked in {time.perf_counter() - t0:.0f}s: "
          f"{n - mismatches} agree with the recorded error classes, "
          f"{mismatches} differ -> {args.out}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
