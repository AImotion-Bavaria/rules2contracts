#!/usr/bin/env python3
"""Compare two result CSVs on the given key/value columns (order-insensitive).

usage: diff_csv.py REFERENCE.csv NEW.csv COL [COL ...]
The first columns that exist in both files up to the last *key* column
(rule/model/run or scenario/ego/lead/mode) identify a row; the remaining
named columns are compared. Exits non-zero on any difference.
"""
import csv, sys
KEYS = {"rule", "model", "run", "scenario", "ego", "lead", "mode"}
ref, new, cols = sys.argv[1], sys.argv[2], sys.argv[3:]
def load(p):
    rows = list(csv.DictReader(open(p)))
    keys = [c for c in cols if c in KEYS]
    return {tuple(r[k] for k in keys): r for r in rows}, keys
a, keys = load(ref); b, _ = load(new)
vals = [c for c in cols if c not in KEYS]
missing = [k for k in a if k not in b]; extra = [k for k in b if k not in a]
diff = [(k, c, a[k][c], b[k][c]) for k in a if k in b for c in vals if a[k][c] != b[k][c]]
print(f"{ref} vs {new}: {len(a)} reference rows, {len(b)} new rows, "
      f"{len(missing)} missing, {len(extra)} extra, {len(diff)} value differences on {vals}")
for d in diff[:20]: print("  DIFF", d)
sys.exit(1 if (missing or diff) else 0)
