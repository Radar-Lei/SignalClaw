"""Generate a publication-style summary figure for event-aware evaluation."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt

from paper_plot_style import COLORS

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "figures" / "data" / "event_aware_metrics.csv"
FIG_DIR = ROOT / "figures"
IMG_DIR = ROOT / "images"

METHODS = ["FixedTime", "MaxPressure", "PI-Light", "DQN", "SignalClaw"]
COLOR_MAP = {
    "FixedTime": COLORS[7],
    "MaxPressure": COLORS[2],
    "PI-Light": COLORS[0],
    "DQN": COLORS[3],
    "SignalClaw": COLORS[1],
}


def _maybe_float(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    return float(value)


def load_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with DATA_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "scenario": row["scenario"],
                    "method": row["method"],
                    "avg_delay_mean": float(row["avg_delay_mean"]),
                    "avg_delay_std": float(row["avg_delay_std"]),
                    "emg_delay_mean": _maybe_float(row["emg_delay_mean"]),
                    "emg_delay_std": _maybe_float(row["emg_delay_std"]),
                    "person_delay_mean": _maybe_float(row["person_delay_mean"]),
                    "person_delay_std": _maybe_float(row["person_delay_std"]),
                }
            )
    return rows


def values_for(rows: list[dict[str, object]], scenarios: list[str], mean_key: str, std_key: str):
    means: dict[str, list[float]] = {m: [] for m in METHODS}
    stds: dict[str, list[float]] = {m: [] for m in METHODS}
    for scenario in scenarios:
        for method in METHODS:
            row = next(
                r for r in rows if r["scenario"] == scenario and r["method"] == method
            )
            mean = row[mean_key]
            std = row[std_key]
            if mean is None:
                means[method].append(float("nan"))
                stds[method].append(float("nan"))
            else:
                means[method].append(float(mean))
                stds[method].append(float(std))
    return means, stds


def grouped_bars(ax, scenarios, means, stds, ylabel, log_scale=False):
    x = list(range(len(scenarios)))
    width = 0.15
    offsets = [-2, -1, 0, 1, 2]

    for idx, method in enumerate(METHODS):
        xs = [v + offsets[idx] * width for v in x]
        ax.bar(
            xs,
            means[method],
            width=width,
            yerr=stds[method],
            capsize=2,
            color=COLOR_MAP[method],
            edgecolor="black",
            linewidth=0.35,
            label=method,
        )

    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.set_ylabel(ylabel)
    if log_scale:
        ax.set_yscale("log")
    ax.spines["left"].set_linewidth(0.8)
    ax.spines["bottom"].set_linewidth(0.8)


def main():
    rows = load_rows()

    fig, axes = plt.subplots(1, 3, figsize=(12.2, 3.6))

    emergency_scenarios = ["E1", "E2", "M1"]
    transit_scenarios = ["B1", "B2"]
    avg_scenarios = ["E1", "E2", "B1", "B2", "I1", "M1"]

    emg_means, emg_stds = values_for(rows, emergency_scenarios, "emg_delay_mean", "emg_delay_std")
    grouped_bars(
        axes[0],
        emergency_scenarios,
        emg_means,
        emg_stds,
        "Emergency Delay (s, log)",
        log_scale=True,
    )

    person_means, person_stds = values_for(rows, transit_scenarios, "person_delay_mean", "person_delay_std")
    grouped_bars(
        axes[1],
        transit_scenarios,
        person_means,
        person_stds,
        "Person-Delay (s, log)",
        log_scale=True,
    )

    avg_means, avg_stds = values_for(rows, avg_scenarios, "avg_delay_mean", "avg_delay_std")
    grouped_bars(
        axes[2],
        avg_scenarios,
        avg_means,
        avg_stds,
        "Average Delay (s)",
        log_scale=False,
    )

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        ncol=5,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.04),
        frameon=False,
        columnspacing=1.0,
        handletextpad=0.4,
    )

    fig.tight_layout(rect=[0.0, 0.0, 1.0, 0.93])
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_DIR / "fig_event_aware_summary.pdf", bbox_inches="tight", pad_inches=0.05)
    fig.savefig(IMG_DIR / "fig_event_aware_summary.png", dpi=300, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"Saved: {FIG_DIR / 'fig_event_aware_summary.pdf'}")
    print(f"Saved: {IMG_DIR / 'fig_event_aware_summary.png'}")


if __name__ == "__main__":
    main()
