"""
Shared helpers for the data-collection scripts under experiments/.

These scripts are deliberately kept separate from the production src/model
package: they instrument individual pipeline steps (per-URL fetch/categorize/
extract timing, paired grammar-strictness comparisons, ...) purely to
produce CSV data for the analysis notebooks under notebooks/. None of that
instrumentation is needed by the crawler itself, so it doesn't belong in
model.orchestrator - each script instead drives model.crawler/model.analyzer
services directly, the same way model.orchestrator does internally.

Run scripts from the project root, e.g.:
    python experiments/collect_crawl_metrics.py
"""
import csv
import logging
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def configure_logging(verbose: bool = False):
    """Set up the same console logging format used by src/main.py."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


@contextmanager
def timed():
    """
    Context manager measuring wall-clock time of its block.
    Usage: with timed() as t: ...  ->  t.seconds after the block exits.
    """
    class _Result:
        seconds = None
    result = _Result()
    start = time.perf_counter()
    try:
        yield result
    finally:
        result.seconds = time.perf_counter() - start


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    """Write rows to path as CSV, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
