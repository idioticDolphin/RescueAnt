"""
Replay stored pages through the categorizer and score them against labels.

The crawl keeps every fetched body in the page store, so a prompt or taxonomy
change can be evaluated against the exact pages that motivated it - offline,
with no network and no re-crawl. Each labelled page comes from a real run and
records what the classifier said about it before, so a change that fixes one
case and breaks another shows both.

Labels live in experiments/data/page_labels.csv.

Usage:
    python experiments/categorization_benchmark.py                 # all confident labels
    python experiments/categorization_benchmark.py --split test    # held-out sites only
    python experiments/categorization_benchmark.py --model models/x.gguf
"""
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import category_service, boilerplate_service  # noqa: E402

LABELS = Path(__file__).parent / "data" / "page_labels.csv"


ORIGINAL_NOTE = "original 50-case benchmark"


def load_cases(split="all", include_unsure=False, path=LABELS, original_only=False):
    """(url, label) pairs from the labelled page set.

    Labels describe a page's role, not its site: a shelter's job-ad page is
    HUB even though the shelter is a SHELTER. Each carries a confidence -
    genuinely ambiguous pages are marked "unsure" and left out by default, so
    a score is not moved by cases a careful reader could label either way.

    The split is by site, never by page. "dev" holds the sites the category
    prompt was tuned against plus a hashed half of the rest; "test" holds the
    other half and must not be looked at while tuning, or it stops measuring
    anything.
    """
    import csv
    cases = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if split != "all" and row.get("split") != split:
                continue
            if row.get("confidence") != "sure" and not include_unsure:
                continue
            if original_only and row.get("note") != ORIGINAL_NOTE:
                continue
            cases.append((row["url"], row["label"]))
    return cases


# Kept for callers that import the case list directly.
CASES = load_cases()


def main():
    ap = argparse.ArgumentParser()
    # Several databases, because the labelled pages were collected across
    # more than one run and the page store on disk is shared between them.
    ap.add_argument("--db", nargs="+",
                    default=["crawl.db", "crawl_third_taxonomy_baseline.db",
                             "crawl_fourth_5category.db"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--include-unsure", action="store_true")
    ap.add_argument("--show-test-cases", action="store_true",
                    help="print per-page results for the test split too; once a "
                         "test page has been looked at while tuning, it no longer "
                         "measures generalisation")
    ap.add_argument("--model", help="GGUF to classify with, overriding the config")
    ap.add_argument("--context", type=int, help="context size for --model")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    if args.model:
        # Swap the classifier without editing config, so several models can be
        # compared against the same labelled pages in one sitting.
        from model.tools import llm_service
        context = args.context or config.category_context
        config = config.model_copy(update={
            "category_model_id": llm_service.get_model_id(args.model, context)})
        config_service._session_config = config
        category_service.config = config
    page_store.configure(config.page_store_path)
    # Templates are learned from stored pages, exactly as in a live run.
    boilerplate_service.forget_all()

    stored = []
    for db in args.db:
        if not Path(db).exists():
            continue
        data_service.DATABASE_PATH = Path(db)
        try:
            stored.extend(data_service.get_all_content_paths())
        except Exception as e:
            # Databases from before the page store exists have no such column.
            print(f"(skipping {db}: {e})")
    # Keep the newest copy of each URL: later runs replace earlier ones.
    seen = {}
    for url, path in stored:
        seen[url] = path
    stored = list(seen.items())
    # Point back at the primary database: boilerplate learning reads through
    # data_service, and leaving it on an old-schema database silently disabled
    # template stripping for the whole benchmark.
    data_service.DATABASE_PATH = Path(args.db[0])
    cases = load_cases(args.split, args.include_unsure)
    cases = cases[: args.limit] if args.limit else cases

    rows, confusion, elapsed = [], Counter(), []
    for needle, expected in cases:
        match = (next(((u, p) for u, p in stored if u == needle and p), None)
                 or next(((u, p) for u, p in stored if needle in u and p), None))
        if match is None:
            rows.append((needle, expected, "MISSING", False))
            continue
        url, path = match
        html = page_store.load(path)
        if not html:
            rows.append((needle, expected, "NOCONTENT", False))
            continue
        started = time.monotonic()
        got = category_service.categorize_website(html, url=url)
        elapsed.append(time.monotonic() - started)
        got = getattr(got, "name", got)
        ok = got == expected
        confusion[(expected, got)] += 1
        rows.append((needle, expected, got, ok))

    hide = args.split == "test" and not args.show_test_cases
    width = max(len(r[0]) for r in rows)
    for needle, expected, got, ok in ([] if hide else rows):
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {needle:<{width}}  expected={expected:<11} got={got}")
    if hide:
        print("(per-page results hidden for the test split; --show-test-cases prints them)")

    scored = [r for r in rows if r[2] not in ("MISSING", "NOCONTENT")]
    correct = sum(1 for r in scored if r[3])
    print(f"\n{correct}/{len(scored)} correct"
          f"  ({100 * correct / len(scored):.0f}%)" if scored else "\nnothing scored")
    skipped = len(rows) - len(scored)
    if skipped:
        print(f"{skipped} case(s) skipped - page not in this store")

    # Most confusions between follow-only categories change a link weight at
    # most. What decides the database is whether a page is extracted, and as a
    # record or a listing: a missed extraction loses records, a spurious one
    # costs a call and risks junk.
    def action(name):
        category = next((c for c in config.categories if c.name == name), None)
        if category is None or not category.is_relevant:
            return "follow"
        return "list" if category.is_list_category else "record"
    lost = sum(1 for r in scored if action(r[1]) != "follow" and action(r[2]) == "follow")
    wasted = sum(1 for r in scored if action(r[1]) == "follow" and action(r[2]) != "follow")
    same = sum(1 for r in scored if action(r[1]) == action(r[2]))
    if scored:
        print(f"extraction decision correct: {same}/{len(scored)}"
              f"  ({100 * same / len(scored):.0f}%)"
              f"   pages not extracted that should be: {lost}"
              f"   extracted that should not be: {wasted}")

    if elapsed:
        total = sum(elapsed)
        print(f"classification: {total:.0f}s for {len(elapsed)} pages "
              f"= {total / len(elapsed):.1f}s per page")

    if hide:
        return
    print("\nmistakes (expected -> got):")
    for (exp, got), n in sorted(confusion.items(), key=lambda kv: -kv[1]):
        if exp != got:
            print(f"  {n:3}  {exp} -> {got}")


if __name__ == "__main__":
    main()
