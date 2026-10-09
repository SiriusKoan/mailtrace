#!/usr/bin/env python3
"""Plot daemon exact-match rates, marking unfinished parameter sets pending."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Benchmark aggregate.json")
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    revision_path = args.input.parent.parent / "revision.json"
    params = (
        json.loads(revision_path.read_text(encoding="utf-8"))["parameters"]
        if revision_path.exists()
        else {}
    )
    holds = params.get("hold_values", sorted({row["ρ"] for row in rows}))
    lookbacks = params.get(
        "go_back_values", sorted({row["ϕ"] for row in rows})
    )
    runs = params.get("runs", 10)
    sleep = params.get("sleep_seconds", 10)
    size = params.get("size", 10)
    by_params = {(row["ρ"], row["ϕ"]): row for row in rows}
    values = np.full((len(holds), len(lookbacks)), np.nan)
    cells = []
    for y, hold in enumerate(holds):
        for x, lookback in enumerate(lookbacks):
            row = by_params.get((hold, lookback))
            complete = row is not None and row["runs"] == runs
            run_dir = (
                args.input.parent / f"sleep{sleep}_hold{hold}_goback{lookback}"
            )
            count = len(list(run_dir.glob("run-*/result.json")))
            if row is not None:
                count = max(count, row["runs"])
            mean = row["exact_pct_mean"] if complete else None
            std = row["exact_pct_std"] if complete else None
            if complete:
                assert 0 <= mean <= 100 and std >= 0
                values[y, x] = mean
            cells.append(
                {
                    "hold": hold,
                    "go_back": lookback,
                    "runs_completed": count,
                    "mean_pct": mean,
                    "sample_std_pct": std,
                }
            )

    completed_sets = sum(cell["mean_pct"] is not None for cell in cells)
    completed_runs = sum(cell["runs_completed"] for cell in cells)
    total_runs = len(cells) * runs
    partial = completed_sets != len(cells)
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(8.6, 6.8))
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("#f1f3f5")
    image = ax.imshow(
        np.ma.masked_invalid(values),
        cmap=cmap,
        vmin=0,
        vmax=100,
        aspect="auto",
    )
    for index, cell in enumerate(cells):
        y, x = divmod(index, len(lookbacks))
        mean = cell["mean_pct"]
        if mean is None:
            ax.add_patch(
                Rectangle(
                    (x - 0.5, y - 0.5),
                    1,
                    1,
                    fill=False,
                    hatch="///",
                    edgecolor="#cbd0d5",
                    linewidth=0,
                )
            )
            label = f"Pending\n{cell['runs_completed']}/{runs} runs"
            color = "#424b55"
        else:
            label = f"{mean:g}%\n±{cell['sample_std_pct']:.1f}"
            color = "#111827" if mean >= 60 else "white"
        ax.text(
            x, y, label, ha="center", va="center", fontsize=10.5, color=color
        )
    ax.set_xticks(range(len(lookbacks)), lookbacks)
    ax.set_yticks(range(len(holds)), holds)
    ax.set_xlabel("Look-back φ (seconds)", fontsize=12, labelpad=10)
    ax.set_ylabel("Hold rounds ρ", fontsize=12, labelpad=10)
    ax.tick_params(axis="both", length=0, labelsize=11, pad=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.045)
    colorbar.set_label("Exact match (%)", fontsize=11, labelpad=10)
    colorbar.outline.set_visible(False)
    fig.suptitle("Production daemon exact-match rate", fontsize=16, y=0.98)
    fig.text(
        0.5,
        0.935,
        f"Checker fixed · {'Partial rerun' if partial else 'Complete rerun'} · {completed_sets}/{len(cells)} parameter sets · {completed_runs}/{total_runs} runs",
        ha="center",
        fontsize=10.5,
        color="#475569",
    )
    fig.text(
        0.5,
        0.045,
        f"Mean ± sample SD (percentage points) · {runs} runs per cell\n{size} emails per run · sleep = {sleep} s · pending cells excluded",
        ha="center",
        fontsize=10,
        color="#475569",
    )
    fig.subplots_adjust(left=0.14, right=0.88, top=0.88, bottom=0.22)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        output = args.output_prefix.with_suffix(f".{extension}")
        fig.savefig(
            output,
            dpi=220,
            facecolor="white",
        )
        if extension == "svg":
            output.write_text(
                "\n".join(
                    line.rstrip() for line in output.read_text().splitlines()
                )
                + "\n",
                encoding="utf-8",
            )
    plt.close(fig)
    snapshot = {
        "captured_at": datetime.now(UTC).isoformat(),
        "source": str(args.input),
        "completed_parameter_sets": completed_sets,
        "completed_runs": completed_runs,
        "total_runs": total_runs,
        "hold_values": holds,
        "go_back_values": lookbacks,
        "expected_runs_per_cell": runs,
        "sleep_seconds": sleep,
        "emails_per_run": size,
        "cells": cells,
    }
    args.output_prefix.with_suffix(".json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "completed_parameter_sets": completed_sets,
                "completed_runs": completed_runs,
                "png": str(args.output_prefix.with_suffix(".png")),
            }
        )
    )


if __name__ == "__main__":
    main()
