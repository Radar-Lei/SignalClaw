"""Shared plotting style for SignalClaw paper figures."""

import matplotlib
import matplotlib.pyplot as plt

FONT_SIZE = 10
DPI = 300
FORMAT = "pdf"
FIG_DIR = "figures"

matplotlib.rcParams.update({
    "font.size": FONT_SIZE,
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
    "axes.labelsize": FONT_SIZE,
    "axes.titlesize": FONT_SIZE + 1,
    "xtick.labelsize": FONT_SIZE - 1,
    "ytick.labelsize": FONT_SIZE - 1,
    "legend.fontsize": FONT_SIZE - 1,
    "figure.dpi": DPI,
    "savefig.dpi": DPI,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.05,
    "axes.grid": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "text.usetex": False,
    "mathtext.fontset": "stix",
})

COLORS = plt.cm.tab10.colors


def save_fig(fig, name, fmt=FORMAT):
    """Save figure to FIG_DIR with consistent naming."""
    path = f"{FIG_DIR}/{name}.{fmt}"
    fig.savefig(path)
    plt.close(fig)
    print(f"Saved: {path}")
