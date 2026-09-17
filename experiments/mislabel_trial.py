"""
Measure what the "mislabeled" verdict saves, and what it throws away.

Takes a fixed sample of stored pages the classifier put into extracting
categories and extracts each one twice - verdict off, verdict on - back to back,
so both arms see the same page under the same conditions. No database is
written; this measures the option rather than committing to it.

Two numbers decide whether the option is worth turning on, and only one of
them is a saving:

  - time: a page judged mislabeled costs a few tokens instead of a record.
  - loss: every record the "off" arm produced on a page the "on" arm rejected
    is either junk correctly dropped or a real record wrongly thrown away. The
    script cannot tell which, so it lists each one for a person to judge.

Usage:
    python experiments/mislabel_trial.py [--single 30] [--lists 10] [--seed 7]
"""
import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import extraction_service, boilerplate_service  # noqa: E402

EXTRACTING = ("STATION", "SHELTER", "SANCTUARY", "VET")


def sample(db, single, lists, seed):
    with sqlite3.connect(db) as c:
        rows = c.execute(
            f"""SELECT source_url, content_path, category FROM crawls
                WHERE content_path IS NOT NULL
                  AND category IN ({','.join('?' * (len(EXTRACTING) + 1))})""",
            EXTRACTING + ("LIST",)).fetchall()
    rng = random.Random(seed)
    one = [r for r in rows if r[2] != "LIST"]
    many = [r for r in rows if r[2] == "LIST"]
    rng.shuffle(one)
    rng.shuffle(many)
    return one[:single] + many[:lists]


def run(html, category, url, check):
    extraction_service.config = extraction_service.config.model_copy(
        update={"mislabel_check": check})
    started = time.monotonic()
    result = extraction_service.extract_information(html, category, url)
    seconds = time.monotonic() - started
    data = result[0] if isinstance(result, tuple) else result
    if data is extraction_service.MISLABELED:
        return "MISLABELED", [], seconds
    if not data:
        return "empty", [], seconds
    records = data if isinstance(data, list) else [data]
    return f"{len(records)} rec", records, seconds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--single", type=int, default=30)
    ap.add_argument("--lists", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)
    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()

    pages = sample(args.db, args.single, args.lists, args.seed)
    print(f"{len(pages)} pages ({args.single} single-record, {args.lists} listing)\n", flush=True)
    print(f"{'category':<10} {'off':>10} {'s':>6}   {'on':>11} {'s':>6}   page", flush=True)

    total_off = total_on = 0.0
    rejected = []
    for url, path, name in pages:
        html = page_store.load(path)
        if not html:
            continue
        category = config.get_category(name)
        off, off_records, off_s = run(html, category, url, False)
        on, _, on_s = run(html, category, url, True)
        total_off += off_s
        total_on += on_s
        print(f"{name:<10} {off:>10} {off_s:>6.1f}   {on:>11} {on_s:>6.1f}   "
              f"{url.split('//', 1)[-1][:60]}", flush=True)
        if on == "MISLABELED":
            rejected.append((name, url, off_records))

    print(f"\ntotal extraction time: off {total_off / 60:.1f} min, on {total_on / 60:.1f} min "
          f"({100 * (total_off - total_on) / total_off:+.0f}% saved)" if total_off else "")
    print(f"pages judged mislabeled: {len(rejected)} of {len(pages)}")
    lost = sum(len(r) for _, _, r in rejected)
    print(f"records the verdict discarded: {lost} - each needs a person's judgement:\n")
    for name, url, records in rejected:
        print(f"  [{name}] {url}")
        for r in records[:6]:
            print(f"      name={str(r.get('name'))[:50]!r}  address={str(r.get('address'))[:40]!r}")
        if len(records) > 6:
            print(f"      ... and {len(records) - 6} more")


if __name__ == "__main__":
    main()
