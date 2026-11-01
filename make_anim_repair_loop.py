#!/usr/bin/env python3
"""Animate one recorded verifier-in-the-loop run as a GIF slide sequence.

For a run directory runs/<rule>/<model>/run_<k> the GIF shows, per attempt,
(1) the candidate rule document (decomposition part, with the lines changed
since the previous candidate highlighted) and (2) the verifier's report with
the checks in pipeline order and, on a V3 failure, the counterexample trace
that becomes the repair feedback; the last slide shows the accepted document.

Usage: make_anim_repair_loop.py [--run runs/R_G1/qwen3.6-27b/run_2]
                                [--out out/repair_loop.gif] [--seconds 4]
Requires matplotlib (pip install matplotlib).
"""
import argparse
import difflib
import json
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FuncAnimation, PillowWriter  # noqa: E402

HERE = pathlib.Path(__file__).parent
CHECKS = ["YAML", "SCHEMA", "SYNTAX", "V0", "V1", "V2", "V3a", "V3b", "V4"]
GREEN, RED, AMBER, GREY = "#2b8a3e", "#c92a2a", "#e67700", "#868e96"


def doc_excerpt(text: str, max_lines: int = 26) -> list[str]:
    """The system contract and decomposition part of a rule document."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("system_contract"))
    except StopIteration:
        start = 0
    out = [l.rstrip() for l in lines[start:] if not l.startswith("#")]
    return out[:max_lines]


def failed_classes(errors: list[str]) -> set[str]:
    out = set()
    for e in errors:
        tag = e.split("]")[0].strip("[") if e.startswith("[") else ""
        if tag in CHECKS:
            out.add(tag)
    return out


def slides_for_run(run_dir: pathlib.Path):
    rec = json.loads((run_dir / "result.json").read_text())
    slides, prev = [], []
    for h in rec["history"]:
        k = h["attempt"]
        doc = (run_dir / f"attempt_{k}.yaml").read_text()
        cur = doc_excerpt(doc)
        changed = set()
        if prev:
            sm = difflib.SequenceMatcher(a=prev, b=cur)
            for tag, _, _, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    changed.update(range(j1, j2))
        slides.append(("doc", k, cur, changed, bool(h["errors"]) is False))
        slides.append(("report", k, h["errors"], failed_classes(h["errors"]), not h["errors"]))
        prev = cur
    return rec, slides


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=pathlib.Path,
                    default=HERE / "runs" / "R_G1" / "qwen3.6-27b" / "run_2")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("out/repair_loop.gif"))
    ap.add_argument("--seconds", type=float, default=4.0, help="seconds per slide")
    args = ap.parse_args()
    rec, slides = slides_for_run(args.run)
    rule, model = args.run.parents[1].name, args.run.parent.name

    fig = plt.figure(figsize=(9, 6.4), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()

    def draw(i):
        ax.clear()
        ax.set_axis_off()
        kind, k, payload, extra, ok = slides[i]
        n_att = rec["attempts"]
        ax.text(0.03, 0.96, f"{rule}  ·  {model}  ·  verifier-in-the-loop, attempt {k} of {n_att}",
                fontsize=11, weight="bold", va="top", transform=ax.transAxes)
        # pipeline strip
        steps = ["rule text + architecture", f"candidate {k}", "verifier V0–V4",
                 "repair feedback" if not ok else "VALID → monitors"]
        for j, s in enumerate(steps):
            x = 0.03 + j * 0.245
            active = (kind == "doc" and j in (0, 1)) or (kind == "report" and j in (2, 3))
            col = (GREEN if ok and j == 3 else (RED if (not ok and j == 3) else "#1c7ed6")) if active else GREY
            ax.text(x, 0.905, s, fontsize=9, color=col, weight="bold" if active else "normal",
                    va="top", transform=ax.transAxes,
                    bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=col, lw=1.2 if active else 0.6))
            if j < 3:
                ax.text(x + 0.215, 0.895, "→", fontsize=11, color=GREY, va="top", transform=ax.transAxes)
        y = 0.83
        if kind == "doc":
            ax.text(0.03, y, f"candidate document {k} (system contract and decomposition)",
                    fontsize=9.5, color=GREY, va="top", transform=ax.transAxes)
            y -= 0.035
            for ln, line in enumerate(payload):
                col = GREEN if ln in extra else "black"
                if ln in extra:
                    ax.text(0.015, y, "▶", fontsize=8, color=GREEN, va="top", transform=ax.transAxes)
                ax.text(0.03, y, line[:110], fontsize=7.6, family="monospace", color=col,
                        weight="bold" if ln in extra else "normal", va="top", transform=ax.transAxes)
                y -= 0.0285
            if extra:
                ax.text(0.03, 0.04, "▶ lines changed in response to the verifier's feedback",
                        fontsize=8.5, color=GREEN, va="bottom", transform=ax.transAxes)
        else:
            failed = extra
            ax.text(0.03, y, f"verifier report for candidate {k}", fontsize=9.5, color=GREY,
                    va="top", transform=ax.transAxes)
            y -= 0.04
            first_fail = next((c for c in CHECKS if c in failed), None)
            for c in CHECKS:
                if first_fail and CHECKS.index(c) > CHECKS.index(first_fail):
                    mark, col = "–  (not reached)", GREY
                elif c in failed:
                    mark, col = "✗  FAIL", RED
                else:
                    mark, col = "✓  pass", GREEN
                ax.text(0.03, y, f"{c:<6}", fontsize=8.5, family="monospace", weight="bold",
                        color=col, va="top", transform=ax.transAxes)
                ax.text(0.10, y, mark, fontsize=8.5, family="monospace", color=col, va="top",
                        transform=ax.transAxes)
                y -= 0.027
            y -= 0.015
            if ok:
                ax.text(0.03, y, "VALID: the service contracts provably refine the system contract.\n"
                                 "The document is compiled into runtime monitors (one per contract).",
                        fontsize=9.5, color=GREEN, weight="bold", va="top", transform=ax.transAxes)
            else:
                ax.text(0.03, y, "error report sent back to the model as repair feedback:",
                        fontsize=8.5, color=RED, va="top", transform=ax.transAxes)
                y -= 0.03
                shown = 0
                for e in payload:
                    for line in e.splitlines():
                        if shown >= 14:
                            break
                        ax.text(0.03, y, line[:120], fontsize=7.4, family="monospace", color="black",
                                va="top", transform=ax.transAxes)
                        y -= 0.026
                        shown += 1
                if any("Counterexample" in e for e in payload):
                    ax.text(0.03, 0.04, "the trace satisfies every service contract and the system "
                                        "assumption, yet breaks the obligation — the model must repair "
                                        "the compositional argument, not the formula syntax",
                            fontsize=8, color=RED, va="bottom", wrap=True, transform=ax.transAxes)
        return []

    fps = 2
    per = int(args.seconds * fps)
    anim = FuncAnimation(fig, lambda f: draw(min(f // per, len(slides) - 1)),
                         frames=per * len(slides) + fps * 2, blit=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    anim.save(args.out, writer=PillowWriter(fps=fps))
    print(f"{args.out} written: {len(slides)} slides from {args.run} "
          f"({'valid' if rec['valid'] else 'invalid'} after {rec['attempts']} attempt(s))")


if __name__ == "__main__":
    main()
