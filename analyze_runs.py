#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aggregate LLM pipeline results from runs/ into an evaluation summary.

For every rule x model run (runs/<rule>/<model>/result.json):
  * final verdict (valid / invalid) and number of attempts
  * error-class histogram per attempt (SYNTAX / YAML / SCHEMA / V0 / V1 /
    V2 / V3a / V3b / V4) — the error-class *progression* over repair rounds
    is a first-class result: it shows WHERE models fail (architecture
    grounding vs. formal composition vs. fragment discipline).

Usage:
    python3 analyze_runs.py            # table to stdout
    python3 analyze_runs.py --json     # machine-readable summary
"""

from __future__ import annotations
import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
RUNS = HERE / "runs"

CLASS_RE = re.compile(r"^\[(SYNTAX|YAML|SCHEMA|CRASH|V0|V1|V2|V3a|V3b|V4)\]")
CLASSES = ["YAML", "SCHEMA", "SYNTAX", "CRASH", "V0", "V1", "V2", "V3a", "V3b", "V4"]


def classify(errors: list[str]) -> list[str]:
    out = []
    for e in errors:
        m = CLASS_RE.match(e.strip())
        if m:
            out.append(m.group(1))
    return out or (["?"] if errors else [])


def collect(runs_dir: pathlib.Path = RUNS) -> list[dict]:
    rows = []
    files = sorted(runs_dir.glob("*/*/run_*/result.json")) or \
        sorted(runs_dir.glob("*/*/result.json"))
    for result_file in files:
        parent = result_file.parent
        if parent.name.startswith("run_"):
            run = int(parent.name.split("_")[1])
            model = parent.parent.name
            rule = parent.parent.parent.name
        else:
            run = 1
            model = parent.name
            rule = parent.parent.name
        if model == "mock":
            continue
        data = json.loads(result_file.read_text())
        progression = []
        for h in data.get("history", []):
            progression.append(sorted(set(classify(h.get("errors", [])))))
        rows.append({
            "rule": rule, "model": model, "run": run,
            "valid": bool(data.get("valid")),
            "attempts": int(data.get("attempts", len(progression))),
            "progression": progression,
        })
    # Run directories without result.json are requests the serving stack
    # declined (finish_reason 'content_filter'/'refusal'); the loop aborted
    # before a verdict. They are reported as REFUSAL (cf. Sect. V-G). Each
    # such directory carries a REFUSAL marker file so that git keeps it.
    for run_dir in sorted(runs_dir.glob("*/*/run_*")):
        if (run_dir / "result.json").exists() or run_dir.parent.name == "mock":
            continue
        rows.append({
            "rule": run_dir.parent.parent.name, "model": run_dir.parent.name,
            "run": int(run_dir.name.split("_")[1]), "valid": False,
            "attempts": 1, "progression": [["REFUSAL"]],
        })
    rows.sort(key=lambda r: (r["rule"], r["model"], r["run"]))
    return rows


def write_csv(rows: list[dict], path: pathlib.Path) -> None:
    """Canonical results table (the format of data/*_results.csv)."""
    import csv
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule", "model", "run", "valid", "attempts", "progression"])
        for r in rows:
            prog = " -> ".join("/".join(p) if p else "VALID"
                               for p in r["progression"])
            w.writerow([r["rule"], r["model"], r["run"], int(r["valid"]),
                        r["attempts"], prog])


def print_table(rows: list[dict]):
    if not rows:
        print("no results under runs/ yet")
        return
    print(f"{'rule':<8}{'model':<30}{'run':<4}{'verdict':<10}{'att.':<6}"
          f"error-class progression (per attempt)")
    print("-" * 100)
    for r in rows:
        prog = " -> ".join("/".join(p) if p else "VALID"
                           for p in r["progression"])
        verdict = "VALID" if r["valid"] else "invalid"
        print(f"{r['rule']:<8}{r['model']:<30}r{r['run']:<3}{verdict:<10}"
              f"{r['attempts']:<6}{prog}")
    # aggregate error-class counts across all attempts
    counts = {c: 0 for c in CLASSES}
    for r in rows:
        for p in r["progression"]:
            for c in p:
                if c in counts:
                    counts[c] += 1
    print("\nError-class totals (attempt-level, across all runs):")
    for c in CLASSES:
        if counts[c]:
            print(f"  {c:<8}{counts[c]}")
    n_valid = sum(1 for r in rows if r["valid"])
    print(f"\nRuns: {len(rows)}, valid: {n_valid} "
          f"({100 * n_valid // max(1, len(rows))}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--csv", type=pathlib.Path, default=None,
                    help="also write the canonical results table to this file")
    ap.add_argument("--runs-dir", type=pathlib.Path, default=RUNS,
                    help="runs directory (e.g. runs_baseline for the "
                         "flat-baseline batch)")
    args = ap.parse_args()
    rows = collect(args.runs_dir)
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(rows, args.csv)
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
