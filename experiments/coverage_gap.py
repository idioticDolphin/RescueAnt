"""
How many organisations do we know of but have never visited?

Recall cannot be measured without an external reference list, but one gap can
be measured from the database alone: a listing names an organisation's own
website, that website is the best source of its details, and the crawl may
never have gone there. Those records are the ones holding a name and a town
and nothing else.

Usage:
    python experiments/coverage_gap.py [crawl.db] [--show 20]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, url_service  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="crawl.db")
    ap.add_argument("--show", type=int, default=15)
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)

    url_fields = [name for name, sem in (config.field_semantics or {}).items()
                  if sem.get("normalize") == "url"]
    crawled = data_service.get_crawled_sites()
    named, missing = set(), []
    for field in url_fields:
        for value in data_service.get_record_urls(field):
            link = url_service.as_url(value)
            site = url_service.registrable_domain(link) if link else None
            if not site:
                continue
            named.add(site)
            if site not in crawled:
                missing.append(link)

    print(f"{len(named)} distinct website(s) named in records")
    print(f"{len(set(missing))} of them never crawled "
          f"({100 * len(set(missing)) / max(len(named), 1):.0f}%)")
    for link in sorted(set(missing))[: args.show]:
        print("   ", link)


if __name__ == "__main__":
    main()
