"""
Score the "mislabeled" verdict against labelled pages.

The verdict saves time only if it is right. Rejecting a page that really is a
station deletes a record without trace, which is worse than the extraction
call it saves - so the number that decides whether the option is usable is
the false-rejection rate, not the time saved.

Reuses the labelled cases from categorization_benchmark.py:

  - a page labelled with an extracting category is extracted as that category
    and should produce a record. A verdict here is a false rejection.
  - a page labelled with a non-extracting category (ANIMAL, ADVOCACY,
    COMMERCIAL, ...) is extracted as though the classifier had wrongly put it
    in an extracting one, and should draw the verdict. A record here is a miss.

Usage:
    python experiments/mislabel_verdict_benchmark.py [--instruction "..."]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from categorization_benchmark import load_cases  # noqa: E402
from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import extraction_service, boilerplate_service  # noqa: E402

DEFAULT_DBS = ["crawl.db", "crawl_third_taxonomy_baseline.db", "crawl_fourth_5category.db"]

# What a misclassification would most plausibly have turned each
# non-extracting page into. An adoption gallery is a list of animals, so the
# likely mistake is LIST; a campaigning body's homepage reads like a station.
WRONG_AS = {"ANIMAL": "LIST", "ADVOCACY": "STATION", "AUTHORITY": "STATION",
            "COMMERCIAL": "LIST", "HUB": "STATION", "ADVICE": "STATION",
            "IRRELEVANT": "STATION"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", nargs="+", default=DEFAULT_DBS)
    ap.add_argument("--instruction", default=None,
                    help="mislabel instruction to test, overriding the config")
    ap.add_argument("--cases", choices=("original", "dev", "test", "all"), default="dev",
                    help="which labelled pages to use; pin this when comparing runs, "
                         "since results over different case sets are not comparable")
    ap.add_argument("--instruction-first", action="store_true",
                    help="place the instruction before the category prompt instead of after it")
    args = ap.parse_args()
    extraction_service.MISLABEL_INSTRUCTION_FIRST = args.instruction_first

    config_service.load_config()
    config = config_service.get_config()
    update = {"mislabel_check": True}
    if args.instruction:
        update["mislabel_instruction"] = args.instruction
    config = config.model_copy(update=update)
    extraction_service.config = config
    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()

    stored = {}
    for db in args.db:
        if Path(db).exists():
            data_service.DATABASE_PATH = Path(db)
            try:
                stored.update(dict(data_service.get_all_content_paths()))
            except Exception:
                pass
    data_service.DATABASE_PATH = Path(args.db[0])
    extracting = {c.name for c in config.categories if c.is_relevant}

    false_reject, true_keep, caught, missed = [], [], [], []
    started = time.monotonic()
    if args.cases == "original":
        cases = load_cases(original_only=True)
    else:
        cases = load_cases(split=args.cases)
    print(f"{len(cases)} labelled pages ({args.cases})", flush=True)
    for needle, label in cases:
        match = next(((u, p) for u, p in stored.items() if needle in u and p), None)
        html = page_store.load(match[1]) if match else None
        if not html:
            continue
        url = match[0]
        should_reject = label not in extracting
        as_category = WRONG_AS.get(label, "STATION") if should_reject else label
        result = extraction_service.extract_information(
            html, config.get_category(as_category), url)
        data = result[0] if isinstance(result, tuple) else result
        verdict = data is extraction_service.MISLABELED
        row = (label, as_category, needle)
        if should_reject:
            (caught if verdict else missed).append(row)
        else:
            (false_reject if verdict else true_keep).append(row)
        mark = "REJECT" if verdict else "keep  "
        ok = verdict == should_reject
        print(f"{'ok  ' if ok else 'FAIL'} {mark} label={label:<10} as={as_category:<8} {needle[:52]}",
              flush=True)

    real = len(false_reject) + len(true_keep)
    wrong = len(caught) + len(missed)
    placement = "first" if args.instruction_first else "last"
    print(f"\n--- instruction ({placement}): {config.mislabel_instruction or '(default)'}")
    if real:
        print(f"real pages wrongly rejected: {len(false_reject)}/{real} "
              f"({100 * len(false_reject) / real:.0f}%)   <- must be near zero")
    if wrong:
        print(f"misclassified pages caught:  {len(caught)}/{wrong} "
              f"({100 * len(caught) / wrong:.0f}%)")
    print(f"time: {(time.monotonic() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
