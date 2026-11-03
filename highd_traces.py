#!/usr/bin/env python3
"""highD recordings as ground-truth traces for the contract monitors.

Loads a highD recording (the CSV triple <prefix>_recordingMeta.csv,
<prefix>_tracksMeta.csv, <prefix>_tracks.csv of the licensed dataset,
Krajewski et al., ITSC 2018) and provides the same episode/trace
interface as commonroad_traces, so the RQ4 sweep runs unchanged on top:

  * find_following_pairs -- vehicle-following episodes: the recorded
    precedingId is constant and non-zero for at least MIN_OVERLAP_STEPS
    at the 10-Hz feature rate (rule R_G1);
  * ground_truth         -- v_true / d_gap of an episode, resampled from
    the 25-Hz drone recording to 10 Hz by linear interpolation;
  * single_tracks / ground_truth_speed -- per-vehicle speed episodes
    with the recording's posted speed limit (rule R_G3).

The gap is bumper-to-bumper along the road axis (lead rear minus ego
front), like the CommonRoad pipeline; highD's `width` column is the
longitudinal extent of the bounding box. Speeds are absolute values of
xVelocity (direction 1 drives towards decreasing x). The speed limit is
taken from recordingMeta and normalised to m/s (values above 60 are
interpreted as km/h; non-positive values mean no posted limit and the
recording is skipped for R_G3).
"""

import argparse
import csv
import math
import pathlib

TARGET_DT = 0.1          # 10-Hz feature rate of the abstraction layer
MIN_OVERLAP_STEPS = 40   # >= 4 s at 10 Hz, as in the CommonRoad pipeline


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read(path: pathlib.Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def load_recording(prefix: pathlib.Path) -> dict:
    """Load one recording; `prefix` is e.g. highd/data/01 (no suffix)."""
    prefix = pathlib.Path(prefix)
    base, name = prefix.parent, prefix.name
    meta = _read(base / f"{name}_recordingMeta.csv")[0]
    tmeta = _read(base / f"{name}_tracksMeta.csv")
    dt = 1.0 / float(meta["frameRate"])

    limit = float(meta.get("speedLimit", -1))
    if limit > 60:          # plausibility: value given in km/h
        limit /= 3.6
    if limit <= 0:
        limit = None        # no posted limit on this stretch

    direction = {int(r["id"]): int(r["drivingDirection"]) for r in tmeta}
    vclass = {int(r["id"]): r["class"] for r in tmeta}

    vehicles: dict[int, dict] = {}
    for r in _read(base / f"{name}_tracks.csv"):
        vid = int(r["id"])
        v = vehicles.setdefault(
            vid, {"frames": [], "x": [], "speed": [], "preceding": [],
                  "length": float(r["width"]),
                  "direction": direction.get(vid, 2),
                  "class": vclass.get(vid, "Car")})
        v["frames"].append(int(r["frame"]))
        v["x"].append(float(r["x"]))
        v["speed"].append(abs(float(r["xVelocity"])))
        v["preceding"].append(int(float(r.get("precedingId", 0) or 0)))
    for v in vehicles.values():
        v["frame0"] = v["frames"][0]
    return {"id": name, "dt": dt, "speed_limit": limit, "vehicles": vehicles}


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

def _min_frames(rec: dict) -> int:
    return int(MIN_OVERLAP_STEPS * TARGET_DT / rec["dt"])


def find_following_pairs(rec: dict) -> list[dict]:
    """Longest same-leader episodes per vehicle, longest first (R_G1)."""
    pairs = []
    for vid, v in rec["vehicles"].items():
        run_start, run_lead = None, 0
        segs = []
        for i, lead in enumerate(v["preceding"] + [0]):  # sentinel flush
            if lead == run_lead:
                continue
            if run_lead and run_start is not None \
                    and i - run_start >= _min_frames(rec):
                segs.append((run_start, i, run_lead))
            run_start, run_lead = i, lead
        for start, end, lead in segs:
            if lead in rec["vehicles"]:
                pairs.append({"ego": vid, "lead": lead,
                              "start": start, "end": end,
                              "steps": end - start})
    pairs.sort(key=lambda p: -p["steps"])
    return pairs


def single_tracks(rec: dict, min_steps: int = MIN_OVERLAP_STEPS) -> list[dict]:
    """Per-vehicle speed episodes for R_G3 (cars only, full track).

    Sorted by peak speed (descending), not by length: the speed-limit
    rule is only exercised near the limit, so sampling the fastest
    vehicles first makes a bounded episode budget informative."""
    tracks = []
    for vid, v in rec["vehicles"].items():
        n10 = int((len(v["frames"]) - 1) * rec["dt"] / TARGET_DT) + 1
        if v["class"].lower() == "car" and n10 >= min_steps:
            tracks.append({"ego": vid, "steps": len(v["frames"]),
                           "peak": max(v["speed"])})
    tracks.sort(key=lambda t: -t["peak"])
    return tracks


# ---------------------------------------------------------------------------
# Ground truth at 10 Hz
# ---------------------------------------------------------------------------

def _resample(seq: list[float], src_dt: float) -> list[float]:
    """Linear interpolation from src_dt to TARGET_DT (25 Hz -> 10 Hz)."""
    ratio = TARGET_DT / src_dt          # source samples per target step
    n = int((len(seq) - 1) / ratio) + 1
    out = []
    for i in range(n):
        x = i * ratio
        k = min(int(x), len(seq) - 2)
        out.append(seq[k] + (seq[k + 1] - seq[k]) * (x - k))
    return out


def _gap(ego: dict, lead: dict, i_ego: int, i_lead: int) -> float:
    if ego["direction"] == 2:   # driving towards increasing x
        return lead["x"][i_lead] - (ego["x"][i_ego] + ego["length"])
    return ego["x"][i_ego] - (lead["x"][i_lead] + lead["length"])


def ground_truth(rec: dict, pair: dict) -> dict:
    """v_true / d_gap of a following episode at 10 Hz (R_G1)."""
    ego = rec["vehicles"][pair["ego"]]
    lead = rec["vehicles"][pair["lead"]]
    v_true, d_gap = [], []
    for i in range(pair["start"], pair["end"]):
        frame = ego["frames"][i]
        j = frame - lead["frame0"]
        if not 0 <= j < len(lead["frames"]):
            break
        v_true.append(ego["speed"][i])
        d_gap.append(max(_gap(ego, lead, i, j), 0.0))
    v_true = _resample(v_true, rec["dt"])
    d_gap = _resample(d_gap, rec["dt"])
    return {"time": list(range(len(v_true))), "v_true": v_true,
            "d_gap": d_gap}


def ground_truth_speed(rec: dict, track: dict) -> dict:
    """v_true / v_lim of a single-vehicle episode at 10 Hz (R_G3)."""
    v = rec["vehicles"][track["ego"]]
    v_true = _resample(v["speed"], rec["dt"])
    return {"time": list(range(len(v_true))), "v_true": v_true,
            "v_lim": [rec["speed_limit"]] * len(v_true)}


# ---------------------------------------------------------------------------
# Feature synthesis for R_G3: a conformant speed planner
# ---------------------------------------------------------------------------

def synthesise_features_g3(gt: dict, constants: dict,
                           mode: str = "nominal",
                           fault_window: tuple[int, int] | None = None) -> dict:
    """Add v_cmd to a speed ground-truth trace.

    The nominal planner commands the recorded velocity (up to a
    deterministic tracking residual at 80 % of DELTA_V) but never more
    than v_lim - DELTA_V, which is exactly its contract guarantee. A
    planning fault drops the limiter inside fault_window, so v_cmd
    follows the recorded speed unclamped; the default window brackets
    the first step at which the limiter would engage."""
    delta_v = constants["DELTA_V"]
    v_lim = gt["v_lim"][0]
    n = len(gt["time"])

    def resid(t):
        return 0.8 * delta_v * math.sin(2 * math.pi * t / 53 + 0.7)

    def command(t, faulty: bool) -> float:
        raw = gt["v_true"][t] + resid(t)
        return raw if faulty else min(raw, v_lim - delta_v)

    if fault_window is None:
        engaged = next((t for t in range(n)
                        if gt["v_true"][t] + resid(t) > v_lim - delta_v),
                       None)
        anchor = engaged if engaged is not None else n // 3
        fault_window = (max(0, anchor - 5), min(n - 1, anchor + 35))
    lo, hi = fault_window
    v_cmd = [command(t, mode == "planning_fault" and lo <= t <= hi)
             for t in range(n)]
    return {**gt, "v_cmd": v_cmd}


# ---------------------------------------------------------------------------
# CLI: list episodes of a recording
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prefix", type=pathlib.Path,
                        help="recording prefix, e.g. highd/data/01")
    args = parser.parse_args()
    rec = load_recording(args.prefix)
    pairs = find_following_pairs(rec)
    tracks = single_tracks(rec)
    lim = f"{rec['speed_limit']:.1f} m/s" if rec["speed_limit"] else "none"
    print(f"recording {rec['id']}: dt={rec['dt']}s, speed limit {lim}, "
          f"{len(rec['vehicles'])} vehicles, "
          f"{len(pairs)} following episodes, {len(tracks)} speed episodes")
    for p in pairs[:10]:
        print(f"  ego {p['ego']} follows {p['lead']} for {p['steps']} frames")


if __name__ == "__main__":
    main()
