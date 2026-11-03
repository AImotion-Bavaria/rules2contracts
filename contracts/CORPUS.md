# Rule Corpus Status

Ground truth: Maierhofer et al., *Formalization of Interstate Traffic Rules
in Temporal Logic* (IV 2021) — 4 general + 5 interstate rules. Encoding
status against the fixed STL fragment and the Autoware feature vocabulary
(2026-08-07):

| Rule | Name | Status | Notes |
|------|------|--------|-------|
| SYN-1 | Runaway-command watchdog (synthetic) | **encoded** (`SYN_watchdog.yaml`) | NOT a corpus rule — serves as the few-shot example in the LLM prompt; must never be evaluated |
| R_G1 | Safe distance to preceding vehicle | **encoded** (`R_G1.yaml` + faulty/illegal variants) | time-gap instance; back in the evaluation since 2026-08-07 (few-shot example replaced by SYN-1); cut-in exception (`O[0,tc]` past clause) not yet encoded — fits the fragment, planned |
| R_G2 | Unnecessary braking | **encoded** (`R_G2.yaml`) | feature-level instance (critical-gap justification); congestion/obstacle exceptions out of feature vocabulary |
| R_G3 | Maximum speed limit | **encoded** (`R_G3.yaml`) | lane speed limit only; fov/type/braking limits out of feature vocabulary |
| R_G4 | Traffic flow | excluded (for now) | needs `preserves_flow` over road-type suggestions; candidate once a `v_min` feature is added |
| R_I1 | Stopping | **encoded** (`R_I1.yaml`) | congestion as environment bool (detection out of scope); lead standstill via perceived lead velocity |
| R_I2 | No passing on the right | excluded | lane-topology predicates (left_of, markings) far outside feature vocabulary |
| R_I3 | No reversing / U-turns | **encoded** (`R_I3.yaml`) | reversing half only (gear command anchor, new feature `gear_rev_cmd`); U-turn half excluded (lane topology) |
| R_I4 | Emergency lane | excluded | complex lane-topology predicates |
| R_I5 | Consider entering vehicles | excluded | unbounded `F` (outside the bounded fragment) + ramp topology |

Exclusions are a *documented scope decision* (threat to validity in the
paper): the feature vocabulary anchors contracts at the component
interfaces of Autoware; rules whose predicates require lane-topology
reasoning belong to a richer feature layer (future work).

UN R157 (ALKS) clauses: to be added as a second corpus part — no existing
formalization exists, so those are evaluated by V0–V4 alone (no ground
truth comparison).
