"""
Shared plotting style and helpers for the notebooks in this directory, so
all figures share one visual style and are exported consistently for
inclusion in the thesis document (PDF for LaTeX, PNG for quick preview).
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.ticker import MaxNLocator

FIGURES_DIR = Path(__file__).parent / "figures"

# A colorblind-safe, print-friendly categorical palette (Okabe-Ito).
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#F0E442"]


def set_style():
    """
    Apply a consistent, print-friendly style to all following plots.

    No gridlines and no in-figure titles by convention here: this is thesis
    figure material, not a dashboard - the caption belongs in the LaTeX
    source (each notebook prints a ready-to-use caption right after the
    figure), and gridlines are omitted in favor of plain axis ticks/spines
    (minimal chart-ink, nothing left for a reader to misread as data).
    """
    sns.set_theme(style="ticks", palette=PALETTE, font_scale=1.05)
    plt.rcParams.update({
        "figure.dpi": 110,
        "savefig.dpi": 300,
        "figure.figsize": (7, 4.2),
        "axes.grid": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": True,
        "legend.fancybox": False,
        "legend.edgecolor": "black",
        "legend.facecolor": "white",
        "legend.framealpha": 1.0,
        "svg.fonttype": "none",
    })


def integer_axis(ax, axis: str = "y"):
    """
    Restrict an axis to integer tick labels only.

    Use this for axes that count discrete things (URLs, entries, records) -
    matplotlib's default tick locator can otherwise place fractional ticks
    (e.g. "1.5") on a small-range count axis, which is a nonsensical value
    for a discrete quantity. Do not use this on continuous quantities
    (seconds, percentages, token means) where fractional ticks are real.
    """
    locator = MaxNLocator(integer=True)
    if axis == "y":
        ax.yaxis.set_major_locator(locator)
    else:
        ax.xaxis.set_major_locator(locator)


def savefig(fig, name: str):
    """Save fig as both PDF (for LaTeX \\includegraphics) and PNG (for quick preview)."""
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES_DIR / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(FIGURES_DIR / f"{name}.png", bbox_inches="tight")


def load_csv(relative_path: str) -> pd.DataFrame:
    """Load a CSV from experiments/data/ (path relative to that directory)."""
    return pd.read_csv(Path(__file__).parent.parent / "experiments" / "data" / relative_path)


def wilson_ci(successes: int, n: int, z: float = 1.96):
    """
    Wilson score interval for a binomial proportion, returned as (lower, upper)
    in [0, 1] (z=1.96 -> 95% confidence).

    Used instead of a plain normal-approximation interval (phat +/- z*se) because
    several samples in these notebooks are small (n<20) and/or the observed
    proportion sits near 0 or 1 (e.g. 5/5, 0/13) - exactly where the normal
    approximation is known to misbehave (intervals can extend below 0% or above
    100%). Wilson stays within [0, 1] by construction and is the standard
    recommendation for small-n proportions.
    """
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z ** 2 / n
    center = phat + z ** 2 / (2 * n)
    margin = z * ((phat * (1 - phat) / n + z ** 2 / (4 * n ** 2)) ** 0.5)
    return (max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom))
