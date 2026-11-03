#!/usr/bin/env python3
"""Export figure data, result macros, and the results table from a results CSV.

Reads the canonical batch results (rule, model, run, valid, attempts,
progression) and writes four files consumed by the paper build, so that
re-running the experiment scripts updates figures AND numbers in the text
automatically on the next LaTeX build:

  * fig_error_classes.dat   -- pgfplots table for Fig. "error classes":
        one row per model (bottom-to-top order in the chart), columns =
        first-failing-check counts on the initial attempt. Combined error
        tokens such as "V3a/V3b" are counted under the earliest stage in
        pipeline order (the first failing check).
  * fig_repair_dynamics.dat -- pgfplots table for Fig. "repair dynamics":
        one row per attempt, one column per model with the cumulative
        number of valid runs after that attempt.
  * results_macros.tex      -- \\newcommand definitions (\\res...) for every
        result number quoted in the section texts, plus \\resTableBody with
        the body rows of the per-rule/per-model results table (a macro
        rather than a separate \\input, which tabularx cannot digest).

Pipeline: run_batch.sh -> analyze_runs.py (writes the results CSV)
          -> export_figure_data.py (this script) -> pdflatex.
"""

import argparse
import collections
import csv
import pathlib

# Pipeline order of the checker stages; earliest first.
STAGE_ORDER = ["YAML", "SCHEMA", "SYNTAX", "V0", "V1", "V2", "V3a", "V3b", "V4", "CRASH"]

# Chart order (bottom-to-top in the stacked-bar chart), display labels,
# short column ids for fig_repair_dynamics.dat, letter-only LaTeX macro
# suffixes for results_macros.tex, and table labels for results_table.tex
# (table order = reversed, strongest model first). Extend this list when the
# model set of the batch changes; fig_error_classes.tex picks up rows and
# labels automatically, fig_repair_dynamics.tex references the column ids,
# and the section texts reference the \res... macros.
MODELS = [
    ("meta-llama-3.1-8b-instruct", "Llama-3.1-8B", "llama8b", "Llama", "Llama-3.1-8B"),
    ("mistral-medium-3.5-128b", "Mistral-Med-3.5", "mistral", "Mistral", "Mistral-Medium-3.5"),
    ("openai-gpt-oss-120b", "GPT-OSS-120B", "gptoss", "GptOss", "GPT-OSS-120B"),
    ("claude-sonnet-5", "Claude-Sonnet-5", "sonnet5", "Sonnet", "Claude Sonnet 5"),
    ("qwen3.6-27b", "Qwen3.6-27B", "qwen36", "QwenDense", "Qwen3.6-27B"),
    ("qwen3.5-397b-a17b", "Qwen3.5-397B", "qwen35", "QwenMoe", "Qwen3.5-397B-A17B"),
    ("claude-opus-5", "Claude-Opus-5", "opus5", "Opus", "Claude Opus 5"),
]

# Rule order of the results table (columns) and the paper's rule labels.
RULES = ["R_G1", "R_G2", "R_G3", "R_I1", "R_I3"]

# Constants from the counterexample re-sampling experiment (not derivable
# from the results CSV): number of re-sampled multi-attempt cells, verdicts
# unchanged, and verdict flips. Update alongside a new re-sampling run.
STABILITY = {"resampled": 52, "stable": 47, "flips": 5}

# Columns of fig_error_classes.dat (order = stacking order in the chart).
ERROR_COLUMNS = ["YAML", "SCHEMA", "SYNTAX", "V0", "V1", "V3a", "V3b", "VALID"]

# Error classes exported as attempt-level total macros (\resErr...).
ERROR_MACROS = {"YAML": "Yaml", "SCHEMA": "Schema", "SYNTAX": "Syntax",
                "V0": "Varch", "V1": "Vwf", "V3a": "VthreeA", "V3b": "VthreeB"}

MAX_ATTEMPTS = 4


def pct(numerator: int, denominator: int) -> int:
    """Percentage rounded to the nearest integer (round half up)."""
    return int(100 * numerator / denominator + 0.5)


def first_failing_check(progression: str) -> str:
    """Return the first failing stage of the initial attempt (or VALID)."""
    first = progression.split(" -> ")[0]
    if first == "VALID":
        return "VALID"
    classes = first.split("/")
    return min(classes, key=lambda c: STAGE_ORDER.index(c) if c in STAGE_ORDER else len(STAGE_ORDER))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--results",
        type=pathlib.Path,
        default=pathlib.Path("data/batch_2026-08-13_results.csv"),
        help="results CSV written by analyze_runs.py (canonical snapshot)",
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=pathlib.Path("data"),
        help="output directory for the generated files",
    )
    parser.add_argument(
        "--rq4",
        type=pathlib.Path,
        default=pathlib.Path("data/rq4_2026-08-12_results.csv"),
        help="RQ4 sweep CSV written by run_rq4.py (canonical snapshot); "
             "the \\resRq... macros are emitted from it",
    )
    parser.add_argument(
        "--baseline",
        type=pathlib.Path,
        default=pathlib.Path("data/baseline_2026-08-13_results.csv"),
        help="flat-baseline results CSV (canonical snapshot); the "
             "\\resBl... macros are emitted from it",
    )
    parser.add_argument(
        "--rq4-highd-g1",
        type=pathlib.Path,
        default=pathlib.Path("data/rq4_highd_g1_2026-08-17_results.csv"),
        help="highD R_G1 sweep CSV (canonical snapshot); \\resHd... macros",
    )
    parser.add_argument(
        "--rq4-highd-g3",
        type=pathlib.Path,
        default=pathlib.Path("data/rq4_highd_g3_2026-08-17_results.csv"),
        help="highD R_G3 sweep CSV (canonical snapshot); \\resHdGthree... macros",
    )
    parser.add_argument(
        "--rq2",
        type=pathlib.Path,
        default=pathlib.Path("data/rq2_2026-08-13_results.csv"),
        help="RQ2 ground-truth comparison CSV written by "
             "compare_system_contracts.py (canonical snapshot); the "
             "\\resRqTwo... macros are emitted from it",
    )
    parser.add_argument(
        "--pt",
        type=pathlib.Path,
        default=pathlib.Path("data/pt_2026-08-13_results.csv"),
        help="placement/tightness CSV written by placement_tightness.py "
             "(canonical snapshot); the \\resPt... macros are emitted from it",
    )
    args = parser.parse_args()

    rows = list(csv.DictReader(args.results.open()))
    known = {m[0] for m in MODELS}
    seen = {r["model"] for r in rows}
    if seen - known:
        raise SystemExit(f"unknown model ids in {args.results}: {sorted(seen - known)}; extend MODELS")
    if {r["rule"] for r in rows} - set(RULES):
        raise SystemExit(f"unknown rule ids in {args.results}; extend RULES")

    # --- per-run derived observations ---------------------------------------
    for r in rows:
        r["is_valid"] = r["valid"] == "1"
        r["first_fail"] = first_failing_check(r["progression"])
        r["first_valid"] = r["first_fail"] == "VALID"

    runs_per_model = len(rows) // len(MODELS)

    # --- fig_error_classes.dat ----------------------------------------------
    profile = {m[0]: collections.Counter() for m in MODELS}
    for r in rows:
        profile[r["model"]][r["first_fail"]] += 1

    out = args.out_dir / "fig_error_classes.dat"
    with out.open("w") as f:
        f.write("model " + " ".join(ERROR_COLUMNS) + "\n")
        for mid, label, *_ in MODELS:
            f.write(f"{label} " + " ".join(str(profile[mid][c]) for c in ERROR_COLUMNS) + "\n")
    unlisted = {c for m in MODELS for c in profile[m[0]]} - set(ERROR_COLUMNS)
    if unlisted:
        raise SystemExit(f"error classes not covered by ERROR_COLUMNS: {sorted(unlisted)}")

    # --- fig_repair_dynamics.dat --------------------------------------------
    out = args.out_dir / "fig_repair_dynamics.dat"
    with out.open("w") as f:
        f.write("attempt " + " ".join(m[2] for m in MODELS) + "\n")
        for k in range(1, MAX_ATTEMPTS + 1):
            cumulative = [
                sum(1 for r in rows if r["model"] == mid and r["is_valid"] and int(r["attempts"]) <= k)
                for mid, *_ in MODELS
            ]
            f.write(f"{k} " + " ".join(str(c) for c in cumulative) + "\n")

    # --- results_macros.tex --------------------------------------------------
    total = len(rows)
    valid = sum(r["is_valid"] for r in rows)
    first_valid = sum(r["first_valid"] for r in rows)
    init_invalid = total - first_valid
    recovered = sum(r["is_valid"] and not r["first_valid"] for r in rows)
    rule_pcts = [pct(sum(r["is_valid"] for r in rows if r["rule"] == rule), total // len(RULES))
                 for rule in RULES]

    # attempt-level error totals: every non-VALID token, combined tokens
    # ("V3a/V3b") counted once per contained class
    err_totals = collections.Counter()
    for r in rows:
        for token in r["progression"].split(" -> "):
            if token != "VALID":
                err_totals.update(token.split("/"))

    macros = [
        "% ------------------------------------------------------------------",
        "% results_macros.tex -- AUTO-GENERATED by export_figure_data.py from",
        f"% {args.results.name}; do not edit by hand. The section texts",
        "% reference these macros so that numbers update with the data.",
        "% ------------------------------------------------------------------",
        f"\\newcommand{{\\resRuns}}{{{total}}}",
        f"\\newcommand{{\\resRunsPerModel}}{{{runs_per_model}}}",
        f"\\newcommand{{\\resValid}}{{{valid}}}",
        f"\\newcommand{{\\resValidPct}}{{{pct(valid, total)}}}",
        f"\\newcommand{{\\resFirstValid}}{{{first_valid}}}",
        f"\\newcommand{{\\resFirstValidPct}}{{{pct(first_valid, total)}}}",
        f"\\newcommand{{\\resInitInvalid}}{{{init_invalid}}}",
        f"\\newcommand{{\\resRecovered}}{{{recovered}}}",
        f"\\newcommand{{\\resRecoveredPct}}{{{pct(recovered, init_invalid)}}}",
        f"\\newcommand{{\\resRuleMinPct}}{{{min(rule_pcts)}}}",
        f"\\newcommand{{\\resRuleMaxPct}}{{{max(rule_pcts)}}}",
        f"\\newcommand{{\\resResampledCells}}{{{STABILITY['resampled']}}}",
        f"\\newcommand{{\\resStableVerdicts}}{{{STABILITY['stable']}}}",
        f"\\newcommand{{\\resStablePct}}{{{pct(STABILITY['stable'], STABILITY['resampled'])}}}",
        f"\\newcommand{{\\resFlips}}{{{STABILITY['flips']}}}",
    ]
    for cls, suffix in ERROR_MACROS.items():
        macros.append(f"\\newcommand{{\\resErr{suffix}}}{{{err_totals[cls]}}}")

    table_lines = []
    for mid, _, _, suffix, table_label in reversed(MODELS):
        mruns = [r for r in rows if r["model"] == mid]
        mvalid = sum(r["is_valid"] for r in mruns)
        mfirst = sum(r["first_valid"] for r in mruns)
        minit_invalid = len(mruns) - mfirst
        mrecovered = sum(r["is_valid"] and not r["first_valid"] for r in mruns)
        att = [int(r["attempts"]) for r in mruns if r["is_valid"]]
        matt = f"{sum(att) / len(att):.2f}" if att else "---"
        macros += [
            f"\\newcommand{{\\resValid{suffix}}}{{{mvalid}}}",
            f"\\newcommand{{\\resValidPct{suffix}}}{{{pct(mvalid, len(mruns))}}}",
            f"\\newcommand{{\\resFirstValid{suffix}}}{{{mfirst}}}",
            f"\\newcommand{{\\resInitInvalid{suffix}}}{{{minit_invalid}}}",
            f"\\newcommand{{\\resRecovered{suffix}}}{{{mrecovered}}}",
            f"\\newcommand{{\\resAtt{suffix}}}{{{matt}}}",
        ]
        per_rule = [sum(1 for r in mruns if r["rule"] == rule and r["is_valid"]) for rule in RULES]
        frac = f"{mvalid}/{len(mruns)}"
        if mvalid == len(mruns):
            frac = f"\\textbf{{{frac}}}"
        table_lines.append(f"{table_label} & " + " & ".join(str(c) for c in per_rule)
                           + f" & {frac} & {matt} \\\\")

    # first-attempt profile of the weakest (format-level) model
    weakest = MODELS[0]
    pre_semantic = sum(profile[weakest[0]][c] for c in ("YAML", "SCHEMA", "SYNTAX"))
    macros += [
        f"\\newcommand{{\\resLlamaFirstYaml}}{{{profile[weakest[0]]['YAML']}}}",
        f"\\newcommand{{\\resLlamaFirstSchema}}{{{profile[weakest[0]]['SCHEMA']}}}",
        f"\\newcommand{{\\resLlamaFirstSyntax}}{{{profile[weakest[0]]['SYNTAX']}}}",
        f"\\newcommand{{\\resLlamaPreSemantic}}{{{pre_semantic}}}",
    ]

    table_lines.append("\\midrule")
    rule_totals = [sum(1 for r in rows if r["rule"] == rule and r["is_valid"]) for rule in RULES]
    table_lines.append("Total & " + " & ".join(str(c) for c in rule_totals)
                       + f" & {valid}/{total} & \\\\")

    # --- RQ4 sweep macros (monitoring + fault localisation) -----------------
    if not args.rq4.exists():
        raise SystemExit(f"RQ4 results not found: {args.rq4} "
                         f"(run run_rq4.py and copy the CSV)")
    rq4 = list(csv.DictReader(args.rq4.open()))
    episodes = {(r["scenario"], r["ego"], r["lead"]) for r in rq4}
    scenarios = {r["scenario"] for r in rq4}

    def rq4_count(mode: str, **conds) -> int:
        sel = [r for r in rq4 if r["mode"] == mode]
        for key, want in conds.items():
            sel = [r for r in sel if r[key] == want]
        return len(sel)

    def rq4_sysviol(mode: str) -> int:
        return sum(1 for r in rq4
                   if r["mode"] == mode and int(r["sys_viol"]) > 0)

    exercised = (rq4_count("perception_fault", localisation="exact")
                 + rq4_count("localization_fault", localisation="exact")
                 + rq4_count("planning_fault", localisation="exact"))
    macros += [
        f"\\newcommand{{\\resRqScenarios}}{{{len(scenarios)}}}",
        f"\\newcommand{{\\resRqEpisodes}}{{{len(episodes)}}}",
        f"\\newcommand{{\\resRqRuns}}{{{len(rq4)}}}",
        f"\\newcommand{{\\resRqNominalClean}}{{{rq4_count('nominal', localisation='clean')}}}",
        f"\\newcommand{{\\resRqPercExact}}{{{rq4_count('perception_fault', localisation='exact')}}}",
        f"\\newcommand{{\\resRqPercSysViol}}{{{rq4_sysviol('perception_fault')}}}",
        f"\\newcommand{{\\resRqPercEarly}}{{{rq4_count('perception_fault', localisation='exact') - rq4_sysviol('perception_fault')}}}",
        f"\\newcommand{{\\resRqLocExact}}{{{rq4_count('localization_fault', localisation='exact')}}}",
        f"\\newcommand{{\\resRqLocSysViol}}{{{rq4_sysviol('localization_fault')}}}",
        f"\\newcommand{{\\resRqPlanExact}}{{{rq4_count('planning_fault', localisation='exact')}}}",
        f"\\newcommand{{\\resRqPlanInactive}}{{{rq4_count('planning_fault', localisation='inactive')}}}",
        f"\\newcommand{{\\resRqPlanSysViol}}{{{rq4_sysviol('planning_fault')}}}",
        f"\\newcommand{{\\resRqExercised}}{{{exercised}}}",
        f"\\newcommand{{\\resRqMisses}}{{{sum(1 for r in rq4 if r['localisation'] in ('miss', 'spurious', 'false_positive'))}}}",
    ]

    # --- highD sweep macros (second dataset, rules R_G1 and R_G3) -----------
    for path, tag in ((args.rq4_highd_g1, "G1"), (args.rq4_highd_g3, "G3")):
        if not path.exists():
            raise SystemExit(f"highD results not found: {path}")
    hd1 = list(csv.DictReader(args.rq4_highd_g1.open()))
    hd3 = list(csv.DictReader(args.rq4_highd_g3.open()))
    bad = sum(1 for r in hd1 + hd3
              if r["localisation"] in ("miss", "spurious", "false_positive"))
    hd1_exact = sum(1 for r in hd1 if r["localisation"] == "exact")
    macros += [
        f"\\newcommand{{\\resHdRecordings}}{{{len({r['scenario'] for r in hd1})}}}",
        f"\\newcommand{{\\resHdEpisodes}}{{{len({(r['scenario'], r['ego'], r['lead']) for r in hd1})}}}",
        f"\\newcommand{{\\resHdRuns}}{{{len(hd1)}}}",
        f"\\newcommand{{\\resHdExercised}}{{{hd1_exact}}}",
        f"\\newcommand{{\\resHdMisses}}{{{bad}}}",
        f"\\newcommand{{\\resHdGthreeRecordings}}{{{len({r['scenario'] for r in hd3})}}}",
        f"\\newcommand{{\\resHdGthreeEpisodes}}{{{len({(r['scenario'], r['ego']) for r in hd3})}}}",
        f"\\newcommand{{\\resHdGthreeExact}}{{{sum(1 for r in hd3 if r['localisation'] == 'exact')}}}",
        f"\\newcommand{{\\resHdGthreeNominalViol}}{{{sum(1 for r in hd3 if r['mode'] == 'nominal' and int(r['sys_viol']) > 0)}}}",
    ]

    # --- flat-baseline macros ------------------------------------------------
    if not args.baseline.exists():
        raise SystemExit(f"baseline results not found: {args.baseline}")
    bl = list(csv.DictReader(args.baseline.open()))
    bl_ok = sum(1 for r in bl if r["valid"] == "1")
    bl_first = sum(1 for r in bl
                   if r["progression"].split(" -> ")[0] == "VALID")
    bl_err = collections.Counter()
    for r in bl:
        for token in r["progression"].split(" -> "):
            if token != "VALID":
                bl_err.update(token.split("/"))
    weakest_id = MODELS[0][0]
    bl_weak = sum(1 for r in bl if r["model"] == weakest_id
                  and r["valid"] == "1")
    bl_opus = sum(1 for r in bl if r["model"] == "claude-opus-5"
                  and r["valid"] == "1")
    macros += [
        f"\\newcommand{{\\resBlRuns}}{{{len(bl)}}}",
        f"\\newcommand{{\\resBlAccepted}}{{{bl_ok}}}",
        f"\\newcommand{{\\resBlAcceptedPct}}{{{pct(bl_ok, len(bl))}}}",
        f"\\newcommand{{\\resBlFirst}}{{{bl_first}}}",
        f"\\newcommand{{\\resBlFirstPct}}{{{pct(bl_first, len(bl))}}}",
        f"\\newcommand{{\\resBlLlamaAccepted}}{{{bl_weak}}}",
        f"\\newcommand{{\\resBlLlamaPct}}{{{pct(bl_weak, runs_per_model)}}}",
        f"\\newcommand{{\\resBlErrYaml}}{{{bl_err['YAML']}}}",
        f"\\newcommand{{\\resBlErrSyntax}}{{{bl_err['SYNTAX']}}}",
        f"\\newcommand{{\\resBlRefusals}}{{{bl_err['REFUSAL']}}}",
        f"\\newcommand{{\\resBlOpusAccepted}}{{{bl_opus}}}",
    ]
    unexpected = set(bl_err) - {"YAML", "SYNTAX", "SCHEMA", "MONITOR", "REFUSAL"}
    if unexpected:
        raise SystemExit(f"unexpected baseline error classes: {unexpected}")

    # --- RQ2 ground-truth comparison macros ----------------------------------
    if not args.rq2.exists():
        raise SystemExit(f"RQ2 results not found: {args.rq2}")
    rq2 = list(csv.DictReader(args.rq2.open()))
    outcome = collections.Counter(r["outcome"].split(":")[0] for r in rq2)
    unexpected = set(outcome) - {"equivalent", "stronger", "weaker",
                                 "incomparable", "vocabulary"}
    if unexpected:
        raise SystemExit(f"unexpected RQ2 outcomes: {unexpected}")
    aligned = sum(1 for r in rq2 if r["aligned"])
    # weaker cases split three ways: the R_G1 boundary artefact (strict vs
    # non-strict trigger inequality), the R_I1 margin-treatment cases, and
    # the remainder (real compliance laxity, e.g. wrong-direction margins)
    weaker_boundary = sum(1 for r in rq2
                          if r["rule"] == "R_G1" and r["outcome"] == "weaker")
    weaker_ione = sum(1 for r in rq2
                      if r["rule"] == "R_I1" and r["outcome"] == "weaker")
    # vocabulary runs that introduce an explicit applicability signal
    # (on_interstate / on_motorway) for the interstate rules
    vocab_scope = sum(1 for r in rq2 if r["outcome"].startswith("vocabulary")
                      and "on_" in r["outcome"])
    macros += [
        f"\\newcommand{{\\resRqTwoRuns}}{{{len(rq2)}}}",
        f"\\newcommand{{\\resRqTwoEq}}{{{outcome['equivalent']}}}",
        f"\\newcommand{{\\resRqTwoStronger}}{{{outcome['stronger']}}}",
        f"\\newcommand{{\\resRqTwoWeaker}}{{{outcome['weaker']}}}",
        f"\\newcommand{{\\resRqTwoIncomp}}{{{outcome['incomparable']}}}",
        f"\\newcommand{{\\resRqTwoVocab}}{{{outcome['vocabulary']}}}",
        f"\\newcommand{{\\resRqTwoVocabPre}}{{{aligned + outcome['vocabulary']}}}",
        f"\\newcommand{{\\resRqTwoAligned}}{{{aligned}}}",
        f"\\newcommand{{\\resRqTwoComparable}}{{{len(rq2) - outcome['vocabulary']}}}",
        f"\\newcommand{{\\resRqTwoAtLeast}}{{{outcome['equivalent'] + outcome['stronger']}}}",
        f"\\newcommand{{\\resRqTwoAtLeastPct}}{{{pct(outcome['equivalent'] + outcome['stronger'], len(rq2) - outcome['vocabulary'])}}}",
        f"\\newcommand{{\\resRqTwoWeakerBoundary}}{{{weaker_boundary}}}",
        f"\\newcommand{{\\resRqTwoWeakerIOne}}{{{weaker_ione}}}",
        f"\\newcommand{{\\resRqTwoWeakerReal}}{{{outcome['weaker'] - weaker_boundary - weaker_ione}}}",
        f"\\newcommand{{\\resRqTwoVocabGtwo}}{{{sum(1 for r in rq2 if r['rule'] == 'R_G2' and r['outcome'].startswith('vocabulary'))}}}",
        f"\\newcommand{{\\resRqTwoVocabScope}}{{{vocab_scope}}}",
    ]

    # --- placement/tightness macros ------------------------------------------
    if not args.pt.exists():
        raise SystemExit(f"placement/tightness results not found: {args.pt}")
    pt = list(csv.DictReader(args.pt.open()))
    if any(r["delta_ref"] != "0.00" for r in pt):
        raise SystemExit("a reference decomposition has delta_ref != 0; "
                         "the 'exactly tight' zero-point claim no longer holds")
    placement = collections.Counter(r["placement"] for r in pt)
    slack = [float(r["delta_star"]) for r in pt]
    # delta* is undefined for purely boolean guarantees (no numeric atom to
    # tighten); the binary search then saturates at its upper cap
    booleans = sum(1 for d in slack if d >= 20.0)
    nonzero = [d for d in slack if 0 < d < 20.0]
    lax = collections.Counter()
    for r in pt:
        for token in r["assumptions"].split():
            lax[token.split(":")[1]] += 1
    macros += [
        f"\\newcommand{{\\resPtRuns}}{{{len(pt)}}}",
        f"\\newcommand{{\\resPtExact}}{{{placement['exact']}}}",
        f"\\newcommand{{\\resPtSuperset}}{{{placement['superset']}}}",
        f"\\newcommand{{\\resPtDifferent}}{{{placement['different']}}}",
        f"\\newcommand{{\\resPtAnchorsMatch}}{{{sum(1 for r in pt if r['anchors'] == 'match')}}}",
        f"\\newcommand{{\\resPtRedundantRuns}}{{{sum(1 for r in pt if r['redundant'])}}}",
        f"\\newcommand{{\\resPtTight}}{{{sum(1 for d in slack if d == 0)}}}",
        f"\\newcommand{{\\resPtSlackRuns}}{{{len(nonzero)}}}",
        f"\\newcommand{{\\resPtSlackMin}}{{{min(nonzero):.1f}}}",
        f"\\newcommand{{\\resPtSlackMax}}{{{max(nonzero):.1f}}}",
        f"\\newcommand{{\\resPtBooleanGuar}}{{{booleans}}}",
        f"\\newcommand{{\\resPtAssumPairs}}{{{sum(lax.values())}}}",
        f"\\newcommand{{\\resPtAssumEq}}{{{lax['eq']}}}",
        f"\\newcommand{{\\resPtAssumWeaker}}{{{lax['weaker']}}}",
        f"\\newcommand{{\\resPtAssumStronger}}{{{lax['stronger']}}}",
        f"\\newcommand{{\\resPtAssumIncomp}}{{{lax['incomp']}}}",
    ]

    macros.append("\\newcommand{\\resTableBody}{%\n" + "\n".join(table_lines) + "}")
    (args.out_dir / "results_macros.tex").write_text("\n".join(macros) + "\n")

    print(f"3 files written to {args.out_dir} from {args.results} ({total} runs, {valid} valid)")


if __name__ == "__main__":
    main()
