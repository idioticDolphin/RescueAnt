"""
Extract the same stored pages with and without the grammar, and compare.

The gold set is too small to tell whether unconstrained extraction loses
anything, but it does not need gold to see where the two modes disagree:
each page is extracted both ways, records are paired by name, and every field
whose value differs is printed for a person to judge. Timing is reported for
both, since speed is the reason to consider dropping the grammar at all.

Usage:
    python experiments/grammar_comparison.py [--single 20] [--lists 5] [--seed 3]
"""
import argparse
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import extraction_service, boilerplate_service  # noqa: E402

SINGLE = ("STATION", "SHELTER", "SANCTUARY", "VET")


def extract(html, category, url, mode):
    extraction_service.config = extraction_service.config.model_copy(
        update={"extraction_grammar": mode})
    started = time.monotonic()
    result = extraction_service.extract_information(html, category, url)
    seconds = time.monotonic() - started
    data = result[0] if isinstance(result, tuple) else None
    if data is None:
        records = []
    else:
        records = data if isinstance(data, list) else [data]
    return records, seconds


def norm(value):
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return " ".join(str(value if value is not None else "").split()).casefold()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--single", type=int, default=20)
    ap.add_argument("--lists", type=int, default=5)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)
    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()
    extraction_service.config = config

    with sqlite3.connect(args.db) as c:
        rows = c.execute("SELECT source_url, content_path, category FROM crawls "
                         "WHERE content_path IS NOT NULL AND category IN (?,?,?,?,?)",
                         SINGLE + ("LIST",)).fetchall()
    rng = random.Random(args.seed)
    rng.shuffle(rows)
    pages = ([r for r in rows if r[2] != "LIST"][:args.single]
             + [r for r in rows if r[2] == "LIST"][:args.lists])

    totals = Counter()
    field_diffs = Counter()
    for url, path, name in pages:
        html = page_store.load(path)
        if not html:
            continue
        category = config.get_category(name)
        slow, slow_s = extract(html, category, url, "always")
        fast, fast_s = extract(html, category, url, "fallback")
        totals["always_s"] += slow_s
        totals["fallback_s"] += fast_s
        totals["always_records"] += len(slow)
        totals["fallback_records"] += len(fast)
        print(f"\n[{name}] {url.split('//', 1)[-1][:70]}\n"
              f"   grammar {len(slow):>2} record(s) {slow_s:6.1f}s   "
              f"free {len(fast):>2} record(s) {fast_s:6.1f}s", flush=True)
        by_name = {norm(r.get("name")): r for r in fast}
        for record in slow:
            other = by_name.pop(norm(record.get("name")), None)
            if other is None:
                print(f"   only with grammar: {str(record.get('name'))[:60]!r}")
                totals["only_always"] += 1
                continue
            for field in sorted(set(record) | set(other)):
                if norm(record.get(field)) != norm(other.get(field)):
                    field_diffs[field] += 1
                    print(f"   {field:<18} {str(record.get(field))[:45]!r}  vs  "
                          f"{str(other.get(field))[:45]!r}")
        for rest in by_name.values():
            print(f"   only without grammar: {str(rest.get('name'))[:60]!r}")
            totals["only_fallback"] += 1

    print("\n--- summary ---")
    print(f"time: grammar {totals['always_s'] / 60:.1f} min, "
          f"free {totals['fallback_s'] / 60:.1f} min")
    print(f"records: grammar {totals['always_records']}, free {totals['fallback_records']} "
          f"(unpaired: {totals['only_always']} grammar-only, {totals['only_fallback']} free-only)")
    print("fields differing in paired records:",
          ", ".join(f"{f} {n}" for f, n in field_diffs.most_common()) or "none")


if __name__ == "__main__":
    main()
