# rules2contracts — reproduction targets (all offline; no API key needed).
# See REPRODUCE.md for what each target reproduces and the expected output.
PY ?= python3
OUT ?= out

.PHONY: all replay check-all regen-data diff-data rq2 pt rq4 figs examples docker clean

all: replay

## 1. Re-check every recorded candidate document with the verifier (V0-V4)
check-all:
	$(PY) replay_checks.py --runs-dir runs --out $(OUT)/check_replay.csv

## 2. Re-aggregate the run records into the result tables, then regenerate all paper numbers / figure data
tables:
	mkdir -p $(OUT)
	$(PY) analyze_runs.py --runs-dir runs --csv $(OUT)/batch_results.csv > /dev/null
	$(PY) diff_csv.py data/batch_2026-08-13_results.csv $(OUT)/batch_results.csv rule model run valid attempts progression
	$(PY) analyze_runs.py --runs-dir runs_baseline --csv $(OUT)/baseline_results.csv > /dev/null
	$(PY) diff_csv.py data/baseline_2026-08-13_results.csv $(OUT)/baseline_results.csv rule model run valid attempts progression

regen-data: tables
	mkdir -p $(OUT)/data && cp data/*.csv $(OUT)/data/
	cp $(OUT)/batch_results.csv $(OUT)/data/batch_2026-08-13_results.csv
	cp $(OUT)/baseline_results.csv $(OUT)/data/baseline_2026-08-13_results.csv
	$(PY) export_figure_data.py --results $(OUT)/data/batch_2026-08-13_results.csv --baseline $(OUT)/data/baseline_2026-08-13_results.csv --out-dir $(OUT)/data

diff-data: regen-data
	@for f in results_macros.tex fig_error_classes.dat fig_repair_dynamics.dat; do \
	  if diff -q data/$$f $(OUT)/data/$$f >/dev/null; then echo "OK   $$f identical"; else echo "DIFF $$f"; diff data/$$f $(OUT)/data/$$f | head -20; fi; done

## 3. Fidelity (RQ3) and placement/tightness (RQ2) over the recorded valid runs
rq2:
	$(PY) compare_system_contracts.py --runs-dir runs --out $(OUT)/rq2_results.csv
	$(PY) diff_csv.py data/rq2_2026-08-13_results.csv $(OUT)/rq2_results.csv rule model run outcome aligned

pt: rq2
	$(PY) placement_tightness.py --runs-dir runs --out $(OUT)/pt_results.csv --rq2 $(OUT)/rq2_results.csv
	$(PY) diff_csv.py data/pt_2026-08-13_results.csv $(OUT)/pt_results.csv rule model run delta_ref placement anchors redundant delta_star assumptions

## 4. Runtime monitoring and fault localisation (RQ4) on the NGSIM/CommonRoad scenarios
rq4:
	$(PY) run_rq4.py scenarios/USA_US101-*.xml --rule contracts/R_G1.yaml --out $(OUT)/rq4_results.csv
	$(PY) diff_csv.py data/rq4_2026-08-12_results.csv $(OUT)/rq4_results.csv scenario ego lead mode localisation

## 5. Figure data for Fig. 3 and Fig. 6
figs:
	mkdir -p $(OUT)/data
	$(PY) make_fig_counterexample.py --out-dir $(OUT)/data
	$(PY) make_fig_fault_localisation.py --out-dir $(OUT)/data
	@for f in fig_counterexample.dat fig_fault_localisation.dat fig_fault_localisation_macros.tex; do \
	  diff -q data/$$f $(OUT)/data/$$f >/dev/null && echo "OK   $$f identical" || echo "DIFF $$f"; done

## 6. Replay the 250 recorded open-weight runs (synthesis + baseline) from the response cache
replay-synthesis:
	OUT=$(OUT) PY=$(PY) sh replay_synthesis.sh

## Everything that can be checked offline
replay: check-all diff-data pt rq4 figs replay-synthesis
	@echo; echo "Replay complete; outputs under $(OUT)/."

## Worked examples (no run records needed)
examples:
	$(PY) check_contracts.py contracts/R_G1.yaml
	$(PY) check_contracts.py contracts/R_G1_faulty.yaml || true
	$(PY) check_contracts.py contracts/R_G1_illegal.yaml || true
	$(PY) monitor_demo.py
	$(PY) autoware_example.py

## README animations (needs matplotlib: pip install matplotlib)
anim:
	mkdir -p docs
	$(PY) make_anim_repair_loop.py --out docs/repair_loop.gif
	$(PY) make_anim_fault_localisation.py --out docs/fault_localisation.gif

docker:
	docker build -t rules2contracts .

clean:
	rm -rf $(OUT)
