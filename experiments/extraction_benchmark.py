"""
Score extraction against hand-labelled gold records, replaying stored pages.

The sibling of categorization_benchmark.py, and the other half of judging a
model. Classification is grammar-constrained to a category name, so a model's
tendency to drift out of the page's language costs nothing there; extraction
reproduces text *from* the page, so it shows up immediately. A model can win
one benchmark and lose the other, and picking on classification alone has no
way of noticing.

Pages come from the page store rather than the network, so a comparison
between models sees byte-identical input and does not re-hit the sites. Only
URLs that are not in any store are fetched, and only if --allow-fetch says so.

Scoring is delegated to experiments/gold_scoring.py, unchanged, so numbers
here stay comparable with collect_extraction_correctness.py: names and
addresses by sequence similarity, telephones digits-only, accepted_animals as
Jaccard overlap, animal_pickup as an exact boolean where gold states one.

Recall on listing pages is deliberately not claimed - gold rows for a LIST
page are a human's sample of it, not necessarily every entry, so an unmatched
gold row means this record was missed, not that the page was under-counted.

Usage:
    python experiments/extraction_benchmark.py
    python experiments/extraction_benchmark.py --model models/other.gguf
    python experiments/extraction_benchmark.py --context 12288 --gold path.csv
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import gold_scoring  # noqa: E402
from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import category_service, extraction_service, boilerplate_service  # noqa: E402

DEFAULT_GOLD = Path(__file__).parent / "data" / "extraction_gold_labels.csv"
DEFAULT_DBS = ["crawl.db", "crawl_third_taxonomy_baseline.db", "crawl_fourth_5category.db"]


def _stored_pages(db_paths):
    """Map url -> stored content path, newest run winning, across databases."""
    found = {}
    for db in db_paths:
        if not Path(db).exists():
            continue
        data_service.DATABASE_PATH = Path(db)
        try:
            for url, path in data_service.get_all_content_paths():
                if path:
                    found[url] = path
        except Exception as e:
            print(f"(skipping {db}: {e})")
    return found


def _lookup(stored, url):
    """Find a stored page for this URL, tolerating www/scheme/slash variants."""
    if url in stored:
        return stored[url]
    def key(u):
        u = u.split("//", 1)[-1].rstrip("/")
        return u[4:] if u.startswith("www.") else u
    target = key(url)
    for candidate, path in stored.items():
        if key(candidate) == target:
            return path
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--db", nargs="+", default=DEFAULT_DBS)
    ap.add_argument("--model", help="GGUF to extract with, overriding the config")
    ap.add_argument("--context", type=int, help="context size for --model")
    ap.add_argument("--allow-fetch", action="store_true",
                    help="fetch gold URLs missing from every page store")
    ap.add_argument("--force-category", metavar="NAME",
                    help="extract every page as this category instead of "
                         "classifying it - measures extraction alone, so a "
                         "taxonomy change cannot look like an extraction failure. "
                         "'auto' picks a listing category for pages with several "
                         "gold records and a single-record one otherwise")
    ap.add_argument("--show-misses", action="store_true",
                    help="print gold against extracted for every field scoring below 1")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()

    if args.model:
        # Repoint every extracting category at the candidate, so the whole
        # extraction path is measured rather than one category's.
        from model.tools import llm_service
        context = args.context or 32768
        model_id = llm_service.get_model_id(args.model, context)
        categories = [c.model_copy(update={"analysis_model_id": model_id})
                      if c.analysis_model_id is not None else c
                      for c in config.categories]
        config = config.model_copy(update={"categories": categories,
                                           "category_model_id": model_id})
        config_service._session_config = config
        category_service.config = config
        extraction_service.config = config

    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()

    gold_by_url = gold_scoring.read_gold_labels(args.gold)
    stored = _stored_pages(args.db)
    data_service.DATABASE_PATH = Path(args.db[0])

    rows, missing, routed, elapsed = [], [], [], []
    for url, gold_entries in gold_by_url.items():
        path = _lookup(stored, url)
        html = page_store.load(path) if path else None
        if not html and args.allow_fetch:
            import asyncio
            import model.crawler.fetching_service as fetching_service
            html = asyncio.run(fetching_service.get_content(url))
        if not html:
            missing.append(url)
            continue

        started = time.monotonic()
        if args.force_category:
            category = _forced_category(config, args.force_category, len(gold_entries))
        else:
            category = category_service.categorize_website(html, url=url)
        if category is None or not category.is_relevant:
            # Routed away from extraction: nothing is produced, by design.
            # Counted separately rather than scored as an extraction failure -
            # these two are different problems with different fixes, and the
            # gold field values stay valid however the taxonomy is redrawn.
            routed.append((url, getattr(category, "name", "None"), len(gold_entries)))
            elapsed.append(time.monotonic() - started)
            continue
        result = extraction_service.extract_information(html, category, url)
        elapsed.append(time.monotonic() - started)
        data = (result[0] if isinstance(result, tuple) else result) or []
        extracted = data if isinstance(data, list) else [data]
        rows.append((url, category.name, gold_entries, extracted))

    _report(rows, missing, routed, elapsed,
            args.model or "configured model",
            forced=args.force_category, show_misses=args.show_misses)


def _forced_category(config, name, gold_count):
    """The category to extract a page as when classification is bypassed.

    'auto' chooses by the *shape* of the gold data rather than by any
    taxonomy: a page with several gold records is a listing and must be
    extracted as one, or a nine-record page comes back as a single record and
    scores 1 of 9 - which is what forcing every page to one single-record
    category did, and it made two models look identical at 2/10.
    """
    if name != "auto":
        return config.get_category(name)
    extracting = [c for c in config.categories if c.is_relevant]
    want_list = gold_count > 1
    for c in extracting:
        if bool(c.is_list_category) == want_list:
            return c
    return extracting[0] if extracting else None


def _report(rows, missing, routed, elapsed, label, forced=None, show_misses=False):
    totals = {f: [0.0, 0] for f in gold_scoring.FIELDS}
    matched = unmatched = 0
    misses = []

    print(f"\n{'page':<58} {'cat':<10} {'gold':>5} {'got':>4} {'matched':>8}")
    for url, category, gold_entries, extracted in rows:
        pairs = gold_scoring.match_gold_to_extracted(gold_entries, extracted)
        hit = 0
        for gold, got, name_score in pairs:
            if got is None:
                unmatched += 1
                continue
            matched += 1
            hit += 1
            scored = gold_scoring.score_entry(url, category, gold, got, name_score)
            for field in gold_scoring.FIELDS:
                value = _field_score(scored, field)
                if value is not None:
                    totals[field][0] += value
                    totals[field][1] += 1
                    if show_misses and value < 1.0:
                        misses.append((gold.get("name", "")[:30], field,
                                       scored.get(f"gold_{field}"),
                                       scored.get(f"extracted_{field}"), value))
        short = url.split("//", 1)[-1]
        print(f"{short[:58]:<58} {category[:10]:<10} {len(gold_entries):>5} "
              f"{len(extracted):>4} {hit:>8}")

    if routed:
        print(f"\n{len(routed)} gold page(s) routed away from extraction by the "
              f"classifier - not scored below:")
        for url, cat, n in routed:
            print(f"   {cat:<11} {n:>2} gold record(s)  {url.split('//', 1)[-1][:52]}")
        print("   (re-run with --force-category to measure extraction alone)")

    suffix = f", forced to {forced}" if forced else ""
    print(f"\n--- {label}{suffix} ---")
    total_gold = matched + unmatched
    if total_gold:
        print(f"gold records matched: {matched}/{total_gold} "
              f"({100 * matched / total_gold:.0f}%)")
    print("\nper-field score, over matched records:")
    for field in gold_scoring.FIELDS:
        acc, n = totals[field]
        if n:
            print(f"   {field:<18} {acc / n:.2f}   (n={n})")
        else:
            print(f"   {field:<18}    -   (no gold values)")
    if misses:
        print("\nfields scoring below 1 (gold -> extracted):")
        for who, field, want, got, value in misses:
            print(f"   {value:.2f}  {who:<30} {field:<16} {str(want)[:34]!r} -> {str(got)[:40]!r}")
    if elapsed:
        print(f"\nclassify+extract: {sum(elapsed):.0f}s for {len(elapsed)} pages "
              f"= {sum(elapsed) / len(elapsed):.1f}s per page")
    if missing:
        print(f"\n{len(missing)} gold URL(s) in no page store "
              f"(use --allow-fetch to fetch them):")
        for url in missing[:5]:
            print(f"   {url[:70]}")


def _field_score(scored, field):
    """Pull a 0-1 score for one field out of a scored row, or None if the gold
    value was blank - blank means the labeller did not state it, which is not
    the same as the model getting it wrong."""
    if field in ("name", "address"):
        if not scored.get(f"gold_{field}"):
            return None
        return float(scored.get(f"{field}_similarity") or 0.0)
    if field == "telephone":
        if not scored.get("gold_telephone"):
            return None
        return 1.0 if scored.get("telephone_match") else 0.0
    if field == "accepted_animals":
        if not scored.get("gold_accepted_animals"):
            return None
        return float(scored.get("accepted_animals_jaccard") or 0.0)
    if field == "animal_pickup":
        if not scored.get("gold_animal_pickup"):
            return None
        return 1.0 if scored.get("animal_pickup_match") else 0.0
    return None


if __name__ == "__main__":
    main()
