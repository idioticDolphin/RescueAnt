"""
Shared plotting style and helpers for the notebooks in this directory, so
all figures share one visual style and are exported consistently for
inclusion in the thesis document (PDF for LaTeX, PNG for quick preview).
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

FIGURES_DIR = Path(__file__).parent / "figures"

# A colorblind-safe, print-friendly categorical palette (Okabe-Ito).
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#F0E442"]


def set_style():
    """Apply a consistent, print-friendly style to all following plots."""
    sns.set_theme(style="whitegrid", palette=PALETTE, font_scale=1.05)
    plt.rcParams.update({
        "figure.dpi": 110,
        "savefig.dpi": 300,
        "figure.figsize": (7, 4.2),
        "axes.titleweight": "bold",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
        "svg.fonttype": "none",
    })


def savefig(fig, name: str):
    """Save fig as both PDF (for LaTeX \\includegraphics) and PNG (for quick preview)."""
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES_DIR / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(FIGURES_DIR / f"{name}.png", bbox_inches="tight")


def load_csv(relative_path: str) -> pd.DataFrame:
    """Load a CSV from experiments/data/ (path relative to that directory)."""
    return pd.read_csv(Path(__file__).parent.parent / "experiments" / "data" / relative_path)
