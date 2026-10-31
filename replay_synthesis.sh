#!/bin/sh
# Replay the recorded synthesis and baseline runs of the open-weight models
# from the response cache -- no API key, no network -- into $OUT/replay_* and
# compare every candidate document and verdict with the recorded run.
# Repair prompts are rebuilt from the recorded verifier reports (--replay), so
# the replay is exact on any platform; the fresh checker's verdict classes are
# compared with the record on the way (see llm_pipeline.run).
set -u
OUT="${OUT:-out}"; PY="${PY:-python3}"
MODELS="qwen3.5-397b-a17b qwen3.6-27b openai-gpt-oss-120b mistral-medium-3.5-128b meta-llama-3.1-8b-instruct"
RULES="R_G1 R_G2 R_G3 R_I1 R_I3"
fail=0; n=0; log="$OUT/replay_synthesis.log"; mkdir -p "$OUT"; : > "$log"
for kind in synthesis baseline; do
  if [ "$kind" = baseline ]; then flag=--baseline; src=runs_baseline; else flag=--replay; src=runs; fi
  for r in $RULES; do for m in $MODELS; do for k in 1 2 3 4 5; do
    echo "=== $kind $r $m run_$k" >> "$log"
    $PY llm_pipeline.py --rule $r --model $m --run $k $flag --out-root "$OUT/replay_$kind" >> "$log" 2>&1
    n=$((n+1))
    if ! diff -rq "$src/$r/$m/run_$k" "$OUT/replay_$kind/$r/$m/run_$k" >/dev/null; then
      echo "DIFF $kind $r $m run_$k"; fail=$((fail+1)); fi
  done; done; done
done
echo "$n runs replayed from the cache, $((n-fail)) identical to the record, $fail differ (details: $log)"
grep -c 'differ from the recorded' "$log" | sed 's/^/fresh-vs-recorded verdict class mismatches: /'
[ "$fail" -eq 0 ]
