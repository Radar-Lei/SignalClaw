"""Generate event-scenario evolution curves for the paper.

Plots fitness vs generation for:
- Normal (T1+T2+T3)
- Emergency dispatcher (E1+E2)
- Incident dispatcher (I1)
- Transit dispatcher (B1+B2)
"""
import csv
import matplotlib.pyplot as plt
from pathlib import Path

# Import shared style
import sys
sys.path.insert(0, str(Path(__file__).parent))
from paper_plot_style import COLORS, save_fig, FONT_SIZE

STORE = Path("store/gpt5_evolve")

CONFIGS = [
    ("Normal (T1–T3)", "normal", COLORS[0]),
    ("Emergency (E1+E2)", "emergency_dispatcher", COLORS[1]),
    ("Transit (B1+B2)", "transit_dispatcher", COLORS[2]),
    ("Incident (I1)", "incident_dispatcher", COLORS[3]),
]


def load_fitness(subdir: str):
    csv_path = STORE / subdir / "phase_selection" / "fitness_history.csv"
    gens, bests, avgs = [], [], []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        seen_gens = set()
        for row in reader:
            g = int(row["generation"])
            if g in seen_gens:
                continue  # skip duplicates from checkpoint resume
            seen_gens.add(g)
            gens.append(g)
            bests.append(float(row["best_fitness"]))
            avgs.append(float(row["avg_fitness"]))
    return gens, bests, avgs


def main():
    fig, axes = plt.subplots(1, 2, figsize=(7, 2.8))

    # Left: Normal evolution (shifted by C=110.3 so values are positive)
    C_NORMAL = 110.3
    ax = axes[0]
    gens, bests, avgs = load_fitness("normal")
    bests = [b + C_NORMAL for b in bests]
    avgs = [a + C_NORMAL for a in avgs]
    ax.plot(gens, bests, color=COLORS[0], linewidth=1.5, label="Best")
    ax.plot(gens, avgs, color=COLORS[0], linewidth=0.8, linestyle="--", alpha=0.5, label="Avg")
    ax.set_xlabel("Generation")
    ax.set_ylabel("Fitness")
    ax.set_title("(a) Normal Traffic (T1–T3)")
    ax.legend(loc="lower right", framealpha=0.8)

    # Right: Event evolutions
    ax = axes[1]
    for label, subdir, color in CONFIGS[1:]:
        gens, bests, avgs = load_fitness(subdir)
        ax.plot(gens, bests, color=color, linewidth=1.5, label=label)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Fitness")
    ax.set_title("(b) Event Dispatcher-Context")
    ax.legend(loc="lower right", framealpha=0.8, fontsize=FONT_SIZE - 2)

    fig.tight_layout()
    save_fig(fig, "evolution_curves_gpt5", fmt="pdf")
    save_fig(fig, "evolution_curves_gpt5", fmt="png")


if __name__ == "__main__":
    main()
