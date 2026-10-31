# Reproducing the results

Everything below runs offline: no API key, no network, no model calls. The
recorded runs are never modified; all outputs go to `out/`.

Measured on a Linux x86-64 container (2 vCPU, Python 3.12.3, z3 4.13.4,
rtamt 0.3.5); a laptop is comparable.

## 0. Environment

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # ~30 s
```
or `docker build -t rules2contracts . && docker run --rm rules2contracts`
(the image runs `make replay`, step 7).

Python 3.12 is required: rtamt 0.3.5 does not import on 3.13. z3 is pinned
to 4.13.4; other versions give the same verdicts but may choose different
counterexample traces (see step 6).

## 1. Worked examples — `make examples` (≈2 s)

* `check_contracts.py contracts/R_G1.yaml` — the reference R_G1
  decomposition: V0–V4 pass, placement metrics printed.
* `contracts/R_G1_faulty.yaml` — the ε_v compensation omitted: V3(b) fails
  and the counterexample trace of Fig. 3 is printed.
* `contracts/R_G1_illegal.yaml` — `d_lead` declared as a Planning output:
  V0 (architecture conformance) rejects.
* `monitor_demo.py` — monitors compiled from R_G1 on a compliant and a
  violating trace; on violation Perception is blamed, the others exonerated.
* `autoware_example.py` — the same rule through the Python API.

## 2. Every verifier verdict — `make check-all` (≈30 s)

Re-runs V0–V4 on all 460 candidate documents of the 175 synthesis runs and
compares the error classes with the recorded repair trajectories.
Expected: `460 candidate documents re-checked: 460 agree …, 0 differ`.

## 3. Result tables and all paper numbers — `make diff-data` (≈1 s)

Re-aggregates `runs/` and `runs_baseline/` into the two result tables,
compares them with `data/batch_2026-08-13_results.csv` and
`data/baseline_2026-08-13_results.csv` (expected: 175 rows each, 0
differences), then regenerates `results_macros.tex` (every `\res…` number in
the paper), `fig_error_classes.dat` (Fig. 5a) and `fig_repair_dynamics.dat`
(Fig. 5b). Expected: `OK … identical` three times.

Run directories without `result.json` (19 under `runs_baseline/*/claude-opus-5/`)
are requests the serving stack declined; they are tabulated as `REFUSAL`
(Sect. V-G).

## 4. Fidelity (RQ3) and placement/tightness (RQ2) — `make pt` (≈1 min)

`compare_system_contracts.py` decides both entailment directions between
every valid synthesised system contract and its reference, including the
injective renaming of environment signals (`aligned` column);
`placement_tightness.py` computes placement class, anchor match, redundant
contracts, guarantee slack δ* and per-contract assumption relations.
Expected: 119 rows each, 0 differences against `data/rq2_*.csv` and
`data/pt_*.csv`.

## 5. Runtime monitoring on NGSIM (RQ4) — `make rq4` (≈3 s)

95 following episodes × 4 modes = 380 monitored runs on the six US-101
scenarios in `scenarios/`. Expected: 380 rows, 0 differences in
`steps, sys_viol, sys_first, culpable, localisation` against
`data/rq4_2026-08-12_results.csv`. `make figs` (≈1 s) regenerates the Fig. 3
counterexample data and the Fig. 6 episode (`fig_fault_localisation.dat`,
`_macros.tex`, scene `.tex`), expected identical.

The highD rows (Sect. V-H, second paragraph) need the licensed dataset; see
`highd/README.md`.

## 6. The synthesis runs themselves — `make replay-synthesis` (≈1.5 min)

Replays all 250 recorded runs of the five open-weight models (125 synthesis,
125 baseline) through the full loop — prompt → cached response → fresh
verifier → repair prompt → cached response … — into `out/replay_*` and
diffs each run directory against the record. Expected:
`250 runs replayed from the cache, 250 identical to the record, 0 differ`
and `fresh-vs-recorded verdict class mismatches: 0`.

How this is exact across platforms: the repair prompt embeds the
counterexample trace, and the trace an SMT solver returns depends on the z3
build (the verdict does not). In `--replay` mode `llm_pipeline.py` therefore
builds each repair prompt from the *recorded* verifier report — which is what
the model actually saw — while the fresh checker still runs on every
candidate and its error classes are compared with the record. Without
`--replay`, a replay on the original machine is byte-identical too; on a
different z3 build it diverges at the first counterexample-bearing repair
prompt (cache miss), which is the point at which a new query would be sent.

The two proprietary models (Claude Opus 5, Claude Sonnet 5) were queried
through the vendor API; `runs/` holds their candidate documents and
verifier reports (steps 2–4 cover them), not their raw responses.

## 7. Everything — `make replay` (≈3 min)

Steps 2–6 in sequence; `make clean` removes `out/`.

## 8. A fresh batch (not reproducible, needs a key)

```bash
export LLM_API_KEY=…                     # set endpoint in llm_config.yaml
python3 llm_pipeline.py --rule R_G1 --model qwen3.6-27b --run 6 --out-root out/fresh
python3 llm_pipeline.py --rule R_G1 --model qwen3.6-27b --run 6 --baseline --out-root out/fresh
python3 analyze_runs.py --runs-dir out/fresh --csv out/fresh/results.csv
```
Run indices 1–5 of the recorded models hit the cache; use new indices or a
new model id for fresh samples. Sampling at temperature 0.2 is not
reproducible across endpoints or checkpoints (Sect. VI).
