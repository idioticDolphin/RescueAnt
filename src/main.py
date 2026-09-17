import argparse
import logging
from pathlib import Path


def _parse_args():
    parser = argparse.ArgumentParser(description="Run the RescueAnt crawl workflow.")
    parser.add_argument(
        "config_path", nargs="?", default=None,
        help="Path to a bot.config-style file (default: bot.config at the project root)",
    )
    parser.add_argument(
        "--resolve", action="store_true",
        help="Deduplicate already-extracted records into entities and exit "
             "(no crawling); safe to re-run after tuning field semantics",
    )
    parser.add_argument(
        "--export", metavar="CSV", default=None,
        help="Write the resolved entities to a CSV file and exit",
    )
    parser.add_argument(
        "--needing-review", action="store_true",
        help="With --export, write only the rows carrying a review flag",
    )
    parser.add_argument(
        "--reprocess", choices=("extract", "categorize"), default=None,
        help="Re-run analysis over already-stored pages without refetching",
    )
    parser.add_argument(
        "--site", default=None, help="Limit --reprocess to one registrable domain",
    )
    parser.add_argument(
        "--category", default=None,
        help="Limit --reprocess to pages currently in this category",
    )
    parser.add_argument(
        "--index-store", nargs="+", metavar="DB", default=None,
        help="Record the pages stored by these crawl databases in the page "
             "store's URL index and exit, so reuse_stored_pages can serve them "
             "to later crawls",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show debug-level logging (raw LLM outputs, per-field config details, ...)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # config_service.get_config() is called at *import time* by most other
    # model modules (fetching_service, category_service, ...), so a custom
    # config has to be loaded before model.orchestrator (or anything it
    # imports) gets imported below.
    import model.tools.config_service as config_service
    if args.config_path is not None:
        config_service.load_config(config_service._read_config(Path(args.config_path)))

    if args.index_store:
        from model.tools import page_store
        page_store.configure(config_service.get_config().page_store_path)
        for db in args.index_store:
            count = page_store.index_database(db)
            logging.getLogger("main").info("Read %d stored page(s) from %s", count, db)
        # Distinct URLs, not the sum above: the same URL is usually recorded
        # by more than one database.
        logging.getLogger("main").info("Page store can now serve %d distinct URL(s)",
                                       page_store.indexed_count())
        raise SystemExit(0)

    import model.orchestrator as orchestrator
    if args.export:
        from model.tools import export_service
        export_service.export_entities(Path(args.export), needing_review=args.needing_review)
    elif args.resolve:
        orchestrator.resolve_entities()
    elif args.reprocess:
        orchestrator.reprocess(stage=args.reprocess, where_site=args.site,
                               where_category=args.category)
    else:
        orchestrator.run()
