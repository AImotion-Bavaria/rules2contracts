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
    args = ap.parse_args()
    arch = AutowareArchitecture(HERE / "autoware_signatures.yaml")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rows, n, mismatches = [], 0, 0
    for res in sorted(args.runs_dir.glob("*/*/run_*/result.json")):
        rec = json.loads(res.read_text())
        for h in rec.get("history", []):
            att = res.parent / f"attempt_{h['attempt']}.yaml"
            if not att.exists():
                continue
            n += 1
            recorded = "/".join(sorted(set(classify(h.get("errors", []))))) or "VALID"
            fresh = "/".join(sorted(set(classify(fresh_errors(att, arch))))) or "VALID"
            agree = int(recorded == fresh)
            mismatches += 1 - agree
            rows.append({"rule": res.parents[2].name, "model": res.parents[1].name,
                         "run": res.parent.name, "attempt": h["attempt"],
                         "recorded": recorded, "fresh": fresh, "agree": agree})
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{n} candidate documents re-checked: {n - mismatches} agree with the "
          f"recorded error classes, {mismatches} differ -> {args.out}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
