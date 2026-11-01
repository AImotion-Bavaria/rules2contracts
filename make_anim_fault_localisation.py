#!/usr/bin/env python3
"""Animate the fault-localisation episode of Fig. 6 as a GIF (README illustration).

Reads data/fig_fault_localisation.dat (the recorded US-101 following episode
with an injected perception fault, 10 Hz) and renders, step by step:

  top     bird's-eye view in the ego frame: ego, the true lead vehicle (gap
          d_gap) and the "ghost" lead where Perception reports it (d_lead);
  middle  true gap, perceived gap and the rule threshold tau * v_true;
  bottom  robustness of the Perception guarantee |d_lead - d_gap| <= EPS_D
          and of the system rule; the Perception monitor turns negative
          (step 23) long before the rule is violated (step 40).

Usage: make_anim_fault_localisation.py [--out out/fault_localisation.gif] [--fps 6]
Requires matplotlib (not a runtime dependency of the checker; pip install matplotlib).
"""
import argparse
import csv
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FuncAnimation, PillowWriter  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

HERE = pathlib.Path(__file__).parent
EPS_D, TAU = 2.0, 1.8
CAR_LEN, CAR_W = 4.5, 1.8

BLUE, GREY, ORANGE, RED, GREEN = "#377eb8", "#777777", "#ff7f00", "#e41a1c", "#2b8a3e"


def load(path):
    rows = list(csv.DictReader(path.open(), delimiter=" "))
    return {k: [float(r[k]) for r in rows] for k in rows[0]}


def car(ax, x, y, color, label, alpha=1.0, ls="-", below=False):
    p = FancyBboxPatch((x, y - CAR_W / 2), CAR_LEN, CAR_W,
                       boxstyle="round,pad=0.02,rounding_size=0.4",
                       fc=color, ec="black", lw=0.8, alpha=alpha, ls=ls)
    ax.add_patch(p)
    ty = y - CAR_W / 2 - 0.35 if below else y + CAR_W / 2 + 0.35
    txt = ax.text(x + CAR_LEN / 2, ty, label, ha="center",
                  va="top" if below else "bottom", fontsize=8, color="black")
    return p, txt


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=pathlib.Path,
                    default=HERE / "data" / "fig_fault_localisation.dat")
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path("out/fault_localisation.gif"))
    ap.add_argument("--fps", type=int, default=6)
    args = ap.parse_args()
    d = load(args.data)
    n = len(d["t"])
    culp_onset = next(i for i, r in enumerate(d["rob_percG"]) if r < 0)
    viol_onset = next(i for i, r in enumerate(d["rob_sysG"]) if r < 0)
    fault_lo = next(i for i, f in enumerate(d["fault"]) if f > 0)
    fault_hi = max(i for i, f in enumerate(d["fault"]) if f > 0)

    fig = plt.figure(figsize=(8, 7.2), dpi=100)
    gs = fig.add_gridspec(3, 1, height_ratios=[1.0, 1.5, 1.3], hspace=0.45)
    ax0, ax1, ax2 = (fig.add_subplot(gs[i]) for i in range(3))

    # --- top: lane in the ego frame ------------------------------------
    xmax = max(d["d_lead"]) + 2 * CAR_LEN + 2
    ax0.set_xlim(-6, xmax)
    ax0.set_ylim(-3.4, 3.2)
    ax0.set_yticks([])
    ax0.set_xlabel("distance ahead of ego front bumper (m)", fontsize=8)
    for y in (-1.8, 1.8):
        ax0.axhline(y, color="black", lw=1)
    ax0.axhline(0, color="grey", lw=0.8, ls=(0, (6, 6)))
    ego, _ = car(ax0, -CAR_LEN, 0, BLUE, "ego")
    lead, lead_txt = car(ax0, d["d_gap"][0], 0, GREY, "lead (true)")
    ghost, ghost_txt = car(ax0, d["d_lead"][0], 0, GREY, "lead as perceived",
                           alpha=0.35, ls="--", below=True)
    gap_txt = ax0.text(0.01, 0.97, "", transform=ax0.transAxes, va="top", fontsize=8,
                       family="monospace")
    title = ax0.set_title("", fontsize=10, loc="left")

    # --- middle: gaps and threshold --------------------------------------
    ax1.set_xlim(0, n - 1)
    ax1.set_ylim(0, max(d["d_lead"]) + 1)
    ax1.set_ylabel("distance (m)", fontsize=8)
    ax1.axvspan(fault_lo, fault_hi, color=ORANGE, alpha=0.12, lw=0)
    ax1.text(fault_hi - 0.5, 0.3, "fault window (perception bias +3 EPS_D)",
             fontsize=7, color=ORANGE, va="bottom", ha="right")
    l_gap, = ax1.plot([], [], color=BLUE, lw=1.8, label="d_gap (true gap)")
    l_lead, = ax1.plot([], [], color=GREY, lw=1.6, ls="--", label="d_lead (perceived)")
    l_thr, = ax1.plot([], [], color=ORANGE, lw=1.6, label="tau * v_true (rule threshold)")
    viol_span = ax1.axvspan(viol_onset, viol_onset, color=RED, alpha=0.15, lw=0)
    ax1.legend(loc="upper left", fontsize=7, ncol=1, frameon=False)
    ax1.tick_params(labelsize=8)

    # --- bottom: robustness ----------------------------------------------
    ax2.set_xlim(0, n - 1)
    lo = min(min(d["rob_percG"]), min(d["rob_sysG"])) - 0.5
    hi = max(max(d["rob_percG"]), max(d["rob_sysG"])) + 0.5
    ax2.set_ylim(lo, hi)
    ax2.axhline(0, color="black", lw=0.8)
    ax2.set_ylabel("robustness", fontsize=8)
    ax2.set_xlabel("step t (10 Hz)", fontsize=8)
    r_perc, = ax2.plot([], [], color=GREEN, lw=1.8, label="Perception guarantee |d_lead - d_gap| <= EPS_D")
    r_sys, = ax2.plot([], [], color=RED, lw=1.8, label="system rule G(d_gap <= tau v_true -> F[0,3] hazard)")
    ax2.legend(loc="lower left", fontsize=7, frameon=False)
    ax2.tick_params(labelsize=8)
    verdict = ax2.text(0.99, 0.95, "", transform=ax2.transAxes, ha="right", va="top",
                       fontsize=9, family="monospace")
    marks = []

    def frame(i):
        t = i if i < n else n - 1       # hold the last frame a while
        lead.set_x(d["d_gap"][t])
        lead_txt.set_x(d["d_gap"][t] + CAR_LEN / 2)
        ghost.set_x(d["d_lead"][t])
        ghost_txt.set_x(d["d_lead"][t] + CAR_LEN / 2)
        ghost.set_alpha(0.75 if d["fault"][t] > 0 else 0.35)
        ghost.set_facecolor(ORANGE if d["fault"][t] > 0 else GREY)
        gap_txt.set_text(f"t = {t:2d}   d_gap = {d['d_gap'][t]:5.2f} m   "
                         f"d_lead = {d['d_lead'][t]:5.2f} m   "
                         f"error = {d['d_lead'][t] - d['d_gap'][t]:+5.2f} m (bound {EPS_D:.1f})   "
                         f"hazard = {'ON' if d['hazard'][t] else 'off'}")
        xs = list(range(t + 1))
        l_gap.set_data(xs, d["d_gap"][:t + 1])
        l_lead.set_data(xs, d["d_lead"][:t + 1])
        l_thr.set_data(xs, d["thr_sys"][:t + 1])
        r_perc.set_data(xs, d["rob_percG"][:t + 1])
        r_sys.set_data(xs, d["rob_sysG"][:t + 1])
        if t >= viol_onset:
            viol_span.set_bounds(viol_onset, 0, max(t - viol_onset, 0.01), 1)
        status = []
        if t < fault_lo:
            title.set_text("Nominal: perception error within its contract bound")
        elif t < culp_onset:
            title.set_text("Fault injected: Perception over-reports the gap by 3 EPS_D")
        elif t < viol_onset:
            title.set_text(f"Perception contract VIOLATED at t={culp_onset} -- rule still holds (early warning)")
        else:
            title.set_text(f"System rule VIOLATED at t={viol_onset}: hazard never raised; "
                           f"blame: Perception ({viol_onset - culp_onset} steps = "
                           f"{(viol_onset - culp_onset) / 10:.1f} s earlier)")
        status.append(f"Perception guarantee: {'VIOLATED' if d['rob_percG'][t] < 0 else 'ok'}")
        status.append(f"Perception assumption: {'VIOLATED' if d['rob_percA'][t] < 0 else 'ok'}")
        status.append(f"system rule: {'VIOLATED' if d['rob_sysG'][t] < 0 else 'ok'}")
        verdict.set_text("\n".join(status))
        verdict.set_color(RED if d["rob_sysG"][t] < 0 else (ORANGE if d["rob_percG"][t] < 0 else "black"))
        if t == culp_onset and not any(m[0] == "c" for m in marks):
            marks.append(("c", ax2.axvline(culp_onset, color=GREEN, lw=1, ls=":")))
            ax2.text(culp_onset + 0.5, hi - 0.3, "culpable", color=GREEN, fontsize=7, va="top")
        if t == viol_onset and not any(m[0] == "v" for m in marks):
            marks.append(("v", ax2.axvline(viol_onset, color=RED, lw=1, ls=":")))
            ax2.text(viol_onset + 0.5, hi - 0.3, "rule broken", color=RED, fontsize=7, va="top")
        return []

    frames = n + 3 * args.fps  # hold the final state for ~3 s
    anim = FuncAnimation(fig, frame, frames=frames, blit=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    anim.save(args.out, writer=PillowWriter(fps=args.fps))
    print(f"{args.out} written: {frames} frames at {args.fps} fps "
          f"(culpable at t={culp_onset}, rule violated at t={viol_onset})")


if __name__ == "__main__":
    main()
