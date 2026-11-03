#!/usr/bin/env python3
"""RQ2: compare synthesised system contracts against the reference corpus.

For every valid run in a runs directory, loads the final (valid) rule
document, extracts its system contract (A_llm, G_llm), and compares it
against the hand-transcribed reference contract (A_ref, G_ref) of the same
rule from contracts/<rule>.yaml. The comparison is semantic, by SMT
entailment over the unrolled horizon (the same decision procedure as the
validity checker), and yields four entailment bits

  aLR: A_llm |= A_ref        aRL: A_ref |= A_llm
  gLR: A_ref ∧ A_llm ∧ G_llm |= G_ref
  gRL: A_ref ∧ A_llm ∧ G_llm ... symmetric for G_ref |= G_llm

(guarantees are compared under the conjoined assumptions: the common
domain on which both contracts claim anything), from which one outcome is
classified per run:

  equivalent    -- both guarantee directions hold (and assumption bits
                   reported alongside)
  stronger      -- G_llm |= G_ref only: the synthesised rule implies the
                   reference (over-approximates the obligation)
  weaker        -- G_ref |= G_llm only: the synthesised rule is implied by
                   the reference (misses behaviours)
  incomparable  -- neither direction holds
  vocabulary    -- the synthesised contract speaks about environment
                   signals unknown to the reference (no alignment possible)

Appends one CSV row per valid run. Usage:
    python3 compare_system_contracts.py --runs-dir runs --out rq2_results.csv
"""

import argparse
import csv
import itertools
import json
import pathlib
import re
import tempfile

from autoware_services import AutowareArchitecture
from check_contracts import load_document
from stl_ag_checker import check_entailment

HERE = pathlib.Path(__file__).parent

RANK = {"equivalent": 0, "stronger": 1, "weaker": 2, "incomparable": 3}


def env_alignments(doc_path: pathlib.Path, unknown: set[str],
                   ref_env: set[str], doc_used: set[str]):
    """Candidate rewrites of a document whose environment-signal names do
    not match the reference vocabulary: every injective renaming of the
    unknown names onto unused reference environment names, realised as a
    word-boundary textual substitution (a pure renaming -- constants and
    structure are untouched). Yields (mapping, temp_path)."""
    targets = sorted(ref_env - doc_used)
    if len(unknown) > len(targets) or len(unknown) > 4:
        return
    text = doc_path.read_text()
    for assignment in itertools.permutations(targets, len(unknown)):
        mapping = dict(zip(sorted(unknown), assignment))
        renamed = text
        for old, new in mapping.items():
            renamed = re.sub(rf"\b{re.escape(old)}\b", new, renamed)
        tmp = pathlib.Path(tempfile.mkstemp(suffix=".yaml")[1])
        tmp.write_text(renamed)
        yield mapping, tmp


def load_system_contract(doc_path: pathlib.Path, arch):
    doc, c_sys, _contracts, _order, used, horizon, errors = load_document(
        doc_path, arch)
    return c_sys, used, horizon, errors


def compare(ref, ref_sigs, ref_h, llm, llm_sigs, llm_h):
    """Return (bits, outcome) for one contract pair."""
    ref_names = {s.name for s in ref_sigs}
    unknown = {s.name for s in llm.assumption.signals()
               | llm.guarantee.signals()} - ref_names
    if unknown:
        return None, f"vocabulary:{'+'.join(sorted(unknown))}"
    sigs = ref_sigs | llm_sigs
    horizon = max(ref_h, llm_h)

    def ent(premises, conclusion):
        ok, _cex = check_entailment(premises, conclusion, sigs, horizon)
        return ok

    bits = {
        "aLR": ent([llm.assumption], ref.assumption),
        "aRL": ent([ref.assumption], llm.assumption),
        "gLR": ent([ref.assumption, llm.assumption, llm.guarantee],
                   ref.guarantee),
        "gRL": ent([ref.assumption, llm.assumption, ref.guarantee],
                   llm.guarantee),
    }
    outcome = {(True, True): "equivalent", (True, False): "stronger",
               (False, True): "weaker", (False, False): "incomparable"}[
        (bits["gLR"], bits["gRL"])]
    return bits, outcome


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", type=pathlib.Path,
                        default=HERE / "runs")
    parser.add_argument("--contracts", type=pathlib.Path,
                        default=HERE / "contracts")
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path("rq2_results.csv"))
    args = parser.parse_args()

    arch = AutowareArchitecture(HERE / "autoware_signatures.yaml")
    references = {}
    for ref_path in sorted(args.contracts.glob("R_*.yaml")):
        if "faulty" in ref_path.stem or "illegal" in ref_path.stem:
            continue
        references[ref_path.stem] = load_system_contract(ref_path, arch)

    rows = []
    for result_file in sorted(args.runs_dir.glob("*/*/run_*/result.json")):
        result = json.loads(result_file.read_text())
        if not result.get("valid"):
            continue
        rule = result["rule"]
        if rule not in references:
            continue
        run_dir = result_file.parent
        doc_path = run_dir / f"attempt_{result['attempts']}.yaml"
        model = run_dir.parent.name
        run = run_dir.name.split("_")[1]
        aligned = ""
        try:
            llm, llm_sigs, llm_h, errors = load_system_contract(doc_path, arch)
            if errors:
                raise ValueError("; ".join(errors)[:120])
            ref, ref_sigs, ref_h, _ = references[rule]
            bits, outcome = compare(ref, ref_sigs, ref_h, llm, llm_sigs, llm_h)
            if outcome.startswith("vocabulary"):
                # retry under injective renamings of the unknown environment
                # signals onto the reference vocabulary; keep the best
                unknown = set(outcome.split(":")[1].split("+"))
                ref_env = ({s.name for s in ref_sigs}
                           - set(arch.features))
                doc_used = {s.name for s in llm_sigs}
                best = (RANK["incomparable"] + 1, None, None)
                for mapping, tmp in env_alignments(doc_path, unknown,
                                                   ref_env, doc_used):
                    try:
                        l2, s2, h2, e2 = load_system_contract(tmp, arch)
                        if e2:
                            continue
                        b2, o2 = compare(ref, ref_sigs, ref_h, l2, s2, h2)
                    except Exception:
                        continue
                    if b2 is not None and RANK[o2] < best[0]:
                        best = (RANK[o2], (b2, o2), mapping)
                if best[1] is not None:
                    bits, outcome = best[1]
                    aligned = "+".join(f"{a}->{b}"
                                       for a, b in best[2].items())
        except Exception as exc:
            bits, outcome = None, f"error:{type(exc).__name__}"
        rows.append({
            "rule": rule, "model": model, "run": run, "outcome": outcome,
            "aligned": aligned,
            **{k: int(v) for k, v in (bits or {}).items()}})
        print(f"{rule} {model} run {run}: {outcome}"
              + (f" [{aligned}]" if aligned else "")
              + (f"  A: llm|=ref={bits['aLR']} ref|=llm={bits['aRL']}"
                 if bits else ""))

    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["rule", "model", "run", "outcome", "aligned",
                           "aLR", "aRL", "gLR", "gRL"])
        writer.writeheader()
        writer.writerows(rows)

    from collections import Counter
    outcomes = Counter(r["outcome"].split(":")[0] for r in rows)
    print(f"\n{len(rows)} valid runs compared -> {args.out}")
    for k, v in outcomes.most_common():
        print(f"  {k:<14} {v}")


if __name__ == "__main__":
    main()
