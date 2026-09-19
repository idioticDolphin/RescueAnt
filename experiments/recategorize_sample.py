"""
Re-classify a sample of already-crawled pages under the current taxonomy.

Between the labelled benchmark (which says whether a change breaks what
worked) and a live crawl (which costs a night), there is a cheaper question:
what does this change do to the pages the crawl actually spent itself on? A
sample of stored pages from one category, run through the classifier as it
stands now, answers it in minutes.

Usage:
    python experiments/recategorize_sample.py --from ADVICE --n 40
    python experiments/recategorize_sample.py --from ADVICE --n 40 --since 20000
"""
import argparse
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import category_service  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="crawl.db")
    parser.add_argument("--from", dest="category", default="ADVICE",
                        help="the category these pages were filed under")
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--since", type=int, default=0,
                        help="only pages from the last N crawl rows")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    page_store.configure(config.page_store_path)
    data_service.DATABASE_PATH = Path(args.db)

    where = "category = ? AND content_path IS NOT NULL"
    params = [args.category]
    if args.since:
        where += " AND crawl_id > (SELECT MAX(crawl_id) - ? FROM crawls)"
        params.append(args.since)
    with data_service.get_connection() as connection:
        rows = connection.execute(
            f"SELECT source_url, content_path FROM crawls WHERE {where}", params).fetchall()

    random.seed(args.seed)
    sample = random.sample(list(rows), min(args.n, len(rows)))
    print(f"{len(rows)} stored {args.category} page(s); re-classifying {len(sample)}\n")

    moved = Counter()
    for row in sample:
        html = page_store.load(row["content_path"])
        if not html:
            continue
        category = category_service.categorize_website(html, row["source_url"])
        name = category.name if category else "(failed)"
        moved[name] += 1
        mark = " " if name == args.category else ">"
        print(f" {mark} {name:<11} {row['source_url'][:96]}", flush=True)

    total = sum(moved.values())
    print(f"\n{args.category} pages now:")
    for name, count in moved.most_common():
        print(f"  {name:<12} {count:>4}  {count / total:>5.0%}")


if __name__ == "__main__":
    main()
