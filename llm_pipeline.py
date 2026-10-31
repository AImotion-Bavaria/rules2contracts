#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM agent stage: natural-language traffic rule -> rule contract document,
with the formal checker as verifier-in-the-loop.

    rule text ──> LLM ──> YAML rule document ──> V0–V4 (z3)
                   ▲                                │
                   └──── repair feedback ◄──────────┘  (up to --rounds)

The verifier replaces the human reviewer twice: it *judges* the output
(valid / invalid with precise errors) and its error messages — including
counterexample traces — are the repair feedback.

Backends:
  * any OpenAI-compatible chat-completions endpoint, configured in llm_config.yaml
    (API key taken from the LLM_API_KEY environment variable).
    NOTE: not reachable from the cloud sandbox — run this on your own
    machine. All requests/responses are cached under runs/ so experiments
    are resumable and the artifact ships the exact transcripts.
  * --mock: canned responses to test the loop offline (no network).

Usage:
    python3 llm_pipeline.py --rule R_G1 --model qwen3-235b-a22b
    python3 llm_pipeline.py --rule R_G1 --mock
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import re
import sys

import yaml

from autoware_services import AutowareArchitecture
from check_contracts import validate

HERE = pathlib.Path(__file__).parent
RUNS = HERE / "runs"

# ---------------------------------------------------------------------------
# Rule tasks: the natural-language input handed to the LLM
# ---------------------------------------------------------------------------

RULES = {
    "R_G1": {
        "text": ("Safe distance to the preceding vehicle (StVO §4(1)): "
                 "'Der Abstand zu einem vorausfahrenden Fahrzeug muss in der "
                 "Regel so groß sein, dass auch dann hinter ihm gehalten "
                 "werden kann, wenn es plötzlich gebremst wird.' Use the "
                 "time-gap interpretation: whenever the true gap to the "
                 "preceding vehicle falls below 1.8 s times the true ego "
                 "velocity, a hazard notification must be raised within "
                 "300 ms (3 steps at 10 Hz)."),
        "hints": ("Perception estimates the gap with a bounded error of "
                  "2.0 m; Localization estimates the ego velocity with a "
                  "bounded error of 0.5 m/s."),
    },
    "R_G2": {
        "text": ("Unnecessary braking (StVO §4(1)): vehicles must not brake "
                 "abruptly without reason. Interpret at feature level: a "
                 "commanded deceleration stronger than 2.0 m/s^2 is only "
                 "permitted when the gap to the preceding vehicle is "
                 "critical (below 20.0 m)."),
        "hints": ("Perception estimates the gap with a bounded error of "
                  "2.0 m; the plant tracks the commanded acceleration "
                  "within 0.5 m/s^2."),
    },
    "R_G3": {
        "text": ("Maximum speed limit (StVO §3(1)/§18): vehicles must not "
                 "drive faster than the speed limit of their current lane."),
        "hints": ("The plant tracks the commanded velocity within 1.0 m/s. "
                  "Treat the lane speed limit as an environment signal."),
    },
    "R_I1": {
        "text": ("Stopping (StVO §12(1)/§18(8)): stopping on the main "
                 "carriageway of an interstate is prohibited unless there "
                 "is congestion or the directly leading vehicle is "
                 "stationary. Interpret at feature level: whenever there is "
                 "no congestion and the true lead-vehicle velocity is "
                 "clearly above the standstill threshold of 0.5 m/s, the "
                 "ego vehicle must keep moving (true ego velocity at least "
                 "0.5 m/s)."),
        "hints": ("Treat congestion as a boolean environment signal and the "
                  "true lead velocity as an environment signal; perception "
                  "estimates the lead velocity with a bounded error of "
                  "0.5 m/s; the plant tracks the commanded velocity within "
                  "1.0 m/s."),
    },
    "R_I3": {
        "text": ("No reversing (StVO §18(7)): reversing manoeuvres are "
                 "prohibited on the interstate. Interpret at feature level: "
                 "the true longitudinal ego velocity must never drop below "
                 "-0.3 m/s."),
        "hints": ("The plant moves backwards only if the reverse gear is "
                  "commanded — express this as a system-level tracking "
                  "assumption linking the gear command feature to the true "
                  "velocity (an environment signal)."),
    },
}

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def architecture_summary(arch: AutowareArchitecture) -> str:
    lines = ["Components and the features they compute:"]
    for fname, f in arch.features.items():
        lines.append(f"  - {fname} [{f.unit}]: computed by {f.component} "
                     f"(topic {arch.port_of_feature(fname).topic}); "
                     f"{f.extract}")
    lines.append("Documented consumption (who may read which feature): "
                 "Planning consumes v_ego, a_ego (Localization) and d_lead, "
                 "v_lead (Perception); Control consumes v_ego and the "
                 "Planning trajectory features (v_cmd, a_plan) and commands "
                 "the gear (gear_rev_cmd); Localization and Perception read "
                 "the abstracted sensing chain (environment signals).")
    return "\n".join(lines)


def system_prompt(arch: AutowareArchitecture, example: str) -> str:
    return f"""You formalize German traffic rules as assume/guarantee \
contracts anchored to the service architecture of an automated vehicle \
(Autoware, component granularity).

OUTPUT FORMAT: exactly one YAML document (no surrounding prose, no code \
fences) with keys: rule_id, description, source_text, sampling_rate_hz, \
horizon, constants, environment (signals with name/sort), system_contract \
(interface with inputs/outputs, assumption, guarantee), decomposition \
(discharge_order, contracts: list of service/interface/assumption/guarantee).

FORMULA LANGUAGE (strict fragment — anything else is rejected):
- outermost operator must be G( ... )
- Boolean: and, or, not, ->
- temporal: F[a,b](...), H[a,b](...), O[a,b](...) with integer step bounds
- atoms: linear comparisons (<=, >=, <, >) over declared signals, declared
  constants, numbers, abs(...); NO signal*signal products, NO nested G,
  NO unbounded operators.
- bare boolean signals are atoms (e.g. hazard_on).

ARCHITECTURE (features are the only signal vocabulary besides the
environment signals you declare):
{architecture_summary(arch)}

RULES OF THE GAME:
- Ground-truth quantities (true gap, true velocity, ...) are environment
  signals; estimated quantities are the architecture features.
- Each service contract: assumption only over its inputs; guarantee only
  over its inputs and outputs; outputs must be features the component
  computes (V0 is checked).
- The decomposition must be PROVABLY correct: the composition of the
  sub-contract guarantees (under the system assumption) must entail the
  system guarantee. Error-bound margins must be compensated in thresholds.
- The system assumption may include a plant/tracking model linking
  commanded and true quantities.

WORKED EXAMPLE (SYNTHETIC rule "runaway-command watchdog" — study the
format, the margin compensation direction, and the discharge order):
{example}
"""


def user_prompt(rule_id: str) -> str:
    r = RULES[rule_id]
    return (f"Formalize the following rule as a rule contract document.\n"
            f"Rule: {r['text']}\nContext: {r['hints']}\n"
            f"Use rule_id: {rule_id}_llm. Output only the YAML document.")


def repair_prompt(errors: list[str]) -> str:
    return ("Your rule document was rejected by the formal checker. "
            "Fix the document and output the complete corrected YAML "
            "document (again: YAML only). Checker errors:\n"
            + "\n".join(f"- {e}" for e in errors))


# ---------------------------------------------------------------------------
# Flat, architecture-agnostic baseline (TR2MTL-style): the model formalises
# the rule as a single world-level formula, without any architecture context.
# Accepted iff the document parses, the formula lies in the fragment, and an
# rtamt monitor can be built from it (syntax + monitorability; there is no
# decomposition, so the V0-V4 validity criterion does not apply by design).
# ---------------------------------------------------------------------------

BASELINE_SYSTEM_PROMPT = """You formalize German traffic rules as temporal-\
logic formulae over the physical state of the world.

OUTPUT FORMAT: exactly one YAML document (no surrounding prose, no code \
fences) with keys: rule_id, description, sampling_rate_hz, horizon, \
constants, environment (signals with name/sort), formula.

FORMULA LANGUAGE (strict fragment — anything else is rejected):
- outermost operator must be G( ... )
- Boolean: and, or, not, ->
- temporal: F[a,b](...), H[a,b](...), O[a,b](...) with integer step bounds
- atoms: linear comparisons (<=, >=, <, >) over declared signals, declared
  constants, numbers, abs(...); NO signal*signal products, NO nested G,
  NO unbounded operators.
- bare boolean signals are atoms.

Declare every quantity you use (true gap, true velocity, hazard-light
state, ...) as an environment signal; name numeric parameters as constants.
The formula ranges over the state of the world, not over any software
architecture.

EXAMPLE (synthetic watchdog rule -- study the exact YAML shape):
rule_id: SYN_watchdog_flat
description: A warning shall follow a sustained runaway torque command.
sampling_rate_hz: 10
horizon: 8
constants:
  T_MAX: 3.0
environment:
  signals:
    - {name: torque_cmd, sort: real}
    - {name: warn, sort: bool}
formula: "G((torque_cmd >= T_MAX) -> F[0,3](warn))"
"""


def baseline_user_prompt(rule_id: str) -> str:
    r = RULES[rule_id]
    return (f"Formalize the following rule as a single formula document.\n"
            f"Rule: {r['text']}\nContext: {r['hints']}\n"
            f"Use rule_id: {rule_id}_flat. Output only the YAML document.")


def _baseline_constants(raw) -> dict[str, float]:
    """Tolerant constants parsing: mapping, or a list of one-key mappings /
    {name, value} entries (LLMs without a worked example produce both)."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict) and {"name", "value"} <= set(item):
                out[str(item["name"])] = float(item["value"])
            elif isinstance(item, dict) and len(item) == 1:
                ((k, v),) = item.items()
                out[str(k)] = float(v)
            else:
                raise ValueError(f"constants entry {item!r}")
        return out
    raise ValueError(f"constants of type {type(raw).__name__}")


def validate_baseline(doc_path: pathlib.Path) -> list[str]:
    """Syntax + monitorability check for flat baseline documents. Malformed
    shapes yield repairable [SCHEMA] feedback instead of crashing."""
    from contract_parser import ContractSyntaxError, parse_contract_formula
    from monitor_backend import build_spec, dsl_to_rtamt
    from stl_ag_checker import Signal

    doc = yaml.safe_load(doc_path.read_text())
    if not isinstance(doc, dict):
        return ["[SCHEMA] document is not a YAML mapping"]
    errors = []
    for key in ("rule_id", "environment", "formula"):
        if key not in doc:
            errors.append(f"[SCHEMA] missing key {key!r}")
    if errors:
        return errors
    if not isinstance(doc["formula"], str):
        return ["[SCHEMA] 'formula' must be a single string"]
    try:
        constants = _baseline_constants(doc.get("constants"))
    except (ValueError, TypeError) as e:
        return [f"[SCHEMA] constants must map names to numbers "
                f"(as in the example): {e}"]
    env = doc.get("environment")
    decls = (env.get("signals") if isinstance(env, dict) else env) or []
    if isinstance(decls, dict):  # tolerate {name: sort} mappings
        decls = [{"name": n, "sort": s} for n, s in decls.items()]
    signals = {}
    for s in decls:
        if isinstance(s, str):
            signals[s] = Signal(s, "real")
        elif isinstance(s, dict) and "name" in s:
            signals[s["name"]] = Signal(s["name"], s.get("sort", "real"))
    if not signals:
        return ["[SCHEMA] no environment signals declared (list of "
                "{name, sort} entries, as in the example)"]
    try:
        parse_contract_formula(doc["formula"], signals, constants)
    except ContractSyntaxError as e:
        return [f"[SYNTAX] {e}"]
    bool_signals = {n for n, s in signals.items() if s.sort == "bool"}
    try:  # monitorability: the formula must compile to an rtamt monitor
        spec_str, used, atoms = dsl_to_rtamt(
            doc["formula"], constants, bool_signals, set(signals))
        build_spec("baseline", spec_str, used | set(atoms))
    except Exception as e:
        return [f"[MONITOR] formula not monitorable: {str(e)[:200]}"]
    return []


def run_baseline(rule_id: str, client, out_dir: pathlib.Path,
                 rounds: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    messages = [{"role": "system", "content": BASELINE_SYSTEM_PROMPT},
                {"role": "user", "content": baseline_user_prompt(rule_id)}]
    history = []
    for attempt in range(1, rounds + 1):
        raw = client.chat(messages)
        doc_text = extract_yaml(raw)
        doc_path = out_dir / f"attempt_{attempt}.yaml"
        doc_path.write_text(doc_text)
        try:
            errors = validate_baseline(doc_path)
        except yaml.YAMLError as e:
            errors = [f"[YAML] not parseable as YAML: {e}"]
        except Exception as e:
            errors = [f"[CRASH] baseline checker raised "
                      f"{type(e).__name__}: {str(e)[:300]}"]
        history.append({"attempt": attempt, "errors": errors})
        print(f"  attempt {attempt}: "
              + ("✓ ACCEPTED" if not errors else f"✗ {len(errors)} error(s)"))
        for e in errors:
            print(f"      {e.splitlines()[0]}")
        if not errors:
            break
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": repair_prompt(errors)})
    result = {"rule": rule_id, "attempts": len(history),
              "valid": not history[-1]["errors"], "history": history,
              "baseline": True}
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible) with on-disk cache
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, config_path: pathlib.Path, model: str,
                 temperature: float, cache_dir: pathlib.Path,
                 max_tokens: int = 16384, cache_salt: int = 1):
        cfg = yaml.safe_load(config_path.read_text())
        self.cache_salt = cache_salt
        self.endpoint = cfg["endpoint"].rstrip("/")
        # The key is read from the environment; a config value of the form
        # ${VAR} is a placeholder, never a key. Cache replays need no key:
        # the check happens only when a request actually goes out.
        key = os.environ.get("LLM_API_KEY") or str(cfg.get("api_key") or "")
        self.api_key = "" if key.startswith("${") else key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def chat(self, messages: list[dict]) -> str:
        key = hashlib.sha256(json.dumps(
            [self.model, self.temperature, self.cache_salt, messages],
            sort_keys=True).encode()).hexdigest()[:24]
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text()).get("content")
            if cached:                      # never trust empty/None cache
                return cached
        return self._request(messages, cache_file, self.max_tokens)

    def _request(self, messages: list[dict], cache_file: pathlib.Path,
                 budget: int) -> str:
        """One completion request at the given token budget; escalates the
        budget (up to 4x the configured maximum) when a reasoning model
        consumes it entirely without emitting content."""
        import time
        import requests  # deferred: not needed in --mock mode
        if not self.api_key:
            raise SystemExit("cache miss and no API key: set LLM_API_KEY to "
                             "query the endpoint (replays need no key)")
        resp = None
        for attempt in range(4):  # retry on timeouts, rate limits, 5xx
            try:
                payload = {"model": self.model, "messages": messages,
                           "temperature": self.temperature,
                           "max_tokens": budget}
                if self.model.startswith("claude-"):
                    # Claude 5 models reject the temperature parameter
                    # ("deprecated"); they sample at their default setting.
                    payload.pop("temperature")
                resp = requests.post(
                    f"{self.endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=600)
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                wait = 30 * (2 ** attempt)
                print(f"    [retry] {type(e).__name__} — waiting {wait}s "
                      f"(attempt {attempt + 1}/4)")
                time.sleep(wait)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 60 * (2 ** attempt)
                print(f"    [retry] HTTP {resp.status_code} — waiting "
                      f"{wait}s (attempt {attempt + 1}/4)")
                time.sleep(wait)
                continue
            break
        if resp is None:
            raise RuntimeError(
                f"model {self.model!r}: request failed after 4 attempts "
                f"(timeouts/connection errors) — re-run the batch later, "
                f"completed calls are cached.")
        if resp.status_code != 200:
            raise RuntimeError(
                f"API error {resp.status_code} for model "
                f"{self.model!r}: {resp.text[:500]}\n"
                f"Hint: run `python3 llm_pipeline.py --list-models` to see "
                f"the model ids actually available to this API key.")
        choice = resp.json()["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        if not content.strip() and choice.get("finish_reason") == "length" \
                and budget < 4 * self.max_tokens:
            # reasoning models may burn the whole budget on thinking:
            # escalate the budget (up to 4x) and re-issue the request
            budget *= 2
            print(f"    [retry] empty content, finish_reason='length' — "
                  f"escalating max_tokens to {budget}")
            return self._request(messages, cache_file, budget)
        if not content.strip():
            finish = choice.get("finish_reason")
            rc = (msg.get("reasoning_content") or "")[:200]
            raise RuntimeError(
                f"model {self.model!r} returned empty content "
                f"(finish_reason={finish!r}) even after budget escalation. "
                f"reasoning_content head: {rc!r}")
        cache_file.write_text(json.dumps(
            {"messages": messages, "content": content}, indent=2))
        return content


class MockClient:
    """Offline test double: first answer contains the classic margin bug,
    the repair answer fixes it — exercises the full loop without network."""

    def __init__(self):
        self.calls = 0
        base = (HERE / "contracts" / "R_G1_faulty.yaml").read_text()
        fixed = (HERE / "contracts" / "R_G1.yaml").read_text()
        self.responses = [
            base.replace("rule_id: R_G1_faulty", "rule_id: R_G1_llm"),
            "```yaml\n" + fixed.replace("rule_id: R_G1",
                                        "rule_id: R_G1_llm") + "\n```",
        ]

    def chat(self, messages):
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return r


# ---------------------------------------------------------------------------
# Extraction + repair loop
# ---------------------------------------------------------------------------

def extract_yaml(text: str) -> str:
    """Tolerate code fences, inline <think> blocks, and surrounding prose
    around the YAML document."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    m = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    start = text.find("rule_id:")
    return text[start:] if start > 0 else text


def run(rule_id: str, client, arch: AutowareArchitecture,
        out_dir: pathlib.Path, rounds: int,
        recorded: list[dict] | None = None) -> dict:
    """One synthesis run. With `recorded` (the history of a recorded run),
    the repair prompts are built from the recorded verifier reports instead
    of the fresh ones: counterexample traces are solver-build dependent, so
    this is what makes a cache replay exact on any platform. The fresh
    checker still runs on every candidate; its error classes are compared
    with the record and a mismatch is reported."""
    out_dir.mkdir(parents=True, exist_ok=True)
    example = (HERE / "contracts" / "SYN_watchdog.yaml").read_text()
    messages = [{"role": "system", "content": system_prompt(arch, example)},
                {"role": "user", "content": user_prompt(rule_id)}]
    history = []
    for attempt in range(1, rounds + 1):
        raw = client.chat(messages)
        doc_text = extract_yaml(raw)
        doc_path = out_dir / f"attempt_{attempt}.yaml"
        doc_path.write_text(doc_text)
        try:
            yaml.safe_load(doc_text)
            errors = validate(doc_path, arch)
        except yaml.YAMLError as e:
            errors = [f"[YAML] not parseable as YAML: {e}"]
        except Exception as e:  # never abort a batch on a checker crash
            errors = [f"[CRASH] checker raised "
                      f"{type(e).__name__}: {str(e)[:300]}"]
        if recorded is not None and attempt <= len(recorded):
            rec_errors = recorded[attempt - 1].get("errors", [])
            from analyze_runs import classify
            if sorted(set(classify(errors))) != sorted(set(classify(rec_errors))):
                print(f"  attempt {attempt}: fresh verdict classes "
                      f"{sorted(set(classify(errors)))} differ from the "
                      f"recorded {sorted(set(classify(rec_errors)))}")
            errors = rec_errors
        history.append({"attempt": attempt, "errors": errors})
        print(f"  attempt {attempt}: "
              + ("✓ VALID" if not errors else f"✗ {len(errors)} error(s)"))
        for e in errors:
            print(f"      {e.splitlines()[0]}")
        if not errors:
            break
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": repair_prompt(errors)})
    result = {"rule": rule_id, "attempts": len(history),
              "valid": not history[-1]["errors"], "history": history}
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def list_models() -> int:
    import requests
    cfg = yaml.safe_load((HERE / "llm_config.yaml").read_text())
    resp = requests.get(
        f"{cfg['endpoint'].rstrip('/')}/models",
        headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', cfg.get('api_key', ''))}",
                 "Accept": "application/json"},
        timeout=60)
    print(f"GET /models -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:1000])
        return 1
    data = resp.json()
    ids = [m.get("id", "?") for m in data.get("data", [])]
    print(f"{len(ids)} models available to this key:")
    for i in sorted(ids):
        print(f"  {i}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=sorted(RULES))
    ap.add_argument("--model", default="qwen3.5-397b-a17b")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--run", type=int, default=1,
                    help="repetition index (N runs per cell); each run "
                         "bypasses the cache of the other runs")
    ap.add_argument("--baseline", action="store_true",
                    help="flat, architecture-agnostic baseline: single "
                         "world-level formula, checked for syntax and "
                         "monitorability only (runs land in runs_baseline/)")
    ap.add_argument("--mock", action="store_true",
                    help="offline test double instead of the real API")
    ap.add_argument("--replay", action="store_true",
                    help="replay a recorded run: answers come from the "
                         "response cache and repair prompts are built from "
                         "the recorded verifier reports (exact on any "
                         "platform); fresh verdicts are compared class-wise")
    ap.add_argument("--out-root", type=pathlib.Path, default=None,
                    help="write run directories under this root instead of "
                         "runs/ (or runs_baseline/); the response cache is "
                         "still read from the recorded runs, so replays "
                         "leave the recorded data untouched")
    ap.add_argument("--list-models", action="store_true",
                    help="query the endpoint for available model ids")
    args = ap.parse_args()

    if args.list_models:
        return list_models()
    if not args.rule:
        ap.error("--rule is required (unless using --list-models)")

    arch = AutowareArchitecture(HERE / "autoware_signatures.yaml")
    tag = "mock" if args.mock else args.model
    runs_root = HERE / "runs_baseline" if args.baseline else RUNS
    out_root = args.out_root if args.out_root is not None else runs_root
    out_dir = out_root / args.rule / tag / f"run_{args.run}"
    client = MockClient() if args.mock else Client(
        HERE / "llm_config.yaml", args.model, args.temperature,
        runs_root / "_cache", args.max_tokens, cache_salt=args.run)

    print(f"Rule {args.rule} -> model {tag}, run {args.run} "
          f"(max {args.rounds} rounds"
          + (", flat baseline)" if args.baseline else ")"))
    recorded = None
    if args.replay and not args.baseline:
        rec_file = runs_root / args.rule / tag / f"run_{args.run}" / "result.json"
        if rec_file.exists():
            recorded = json.loads(rec_file.read_text()).get("history", [])
    result = (run_baseline(args.rule, client, out_dir, args.rounds)
              if args.baseline
              else run(args.rule, client, arch, out_dir, args.rounds, recorded))
    verdict = (f"VALID after {result['attempts']} attempt(s)"
               if result["valid"] else "still invalid")
    print(f"Result: {verdict}")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
