"""
What a link is worth, by the category of the page that offered it.

`referrer_weights` is fourteen numbers set by hand - the most consequential
piece of unmeasured configuration in the crawler, since every queued URL
inherits one. Diligenti et al. (2000) make the point that the right quantity
is the observed distance from a page to a target; the crawl's own graph holds
a one-hop version of exactly that, and this reads it off.

For every stored page, its category and the categories of the pages it linked
to are known. So: given a link offered by a page of category C, how often did
it lead to something worth extracting? And - separately, because it is what
drives drift - how often did it lead to a page that was itself a dead end?

What it cannot show: what a category's links would have led to had the crawl
followed more of them. A category whose links were rarely followed is
measured on a small and self-selected sample, so its row carries its count.

Usage:
    python experiments/referrer_value.py [--min-links 200]
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, page_store, url_service  # noqa: E402
from frontier_simulation import CACHE, build_graph, _links_of  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="crawl.db")
    parser.add_argument("--min-links", type=int, default=100)
    args = parser.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    page_store.configure(config.page_store_path)
    graph = build_graph(args.db, CACHE)

    wanted = {c.name for c in config.categories if c.is_relevant}
    dead_end = {c.name for c in config.categories if c.relevancy.value == "IRRELEVANT"}
    dead_end |= {"AUTHORITY", "ADVOCACY", None}

    offered = Counter()
    to_wanted = Counter()
    to_dead = Counter()
    off_site = Counter()
    for url, entry in graph.items():
        source = entry[0]
        site = url_service.registrable_domain(url)
        for link, _ in _links_of(entry):
            target = graph.get(link, [None])[0]
            offered[source] += 1
            if target in wanted:
                to_wanted[source] += 1
            if target in dead_end:
                to_dead[source] += 1
            if url_service.registrable_domain(link) != site:
                off_site[source] += 1

    total = sum(offered.values())
    base = sum(to_wanted.values()) / total
    print(f"\n{total} links from {len(graph)} stored pages; "
          f"{base:.1%} of all links led to {sorted(wanted)}\n")
    print(f"  {'referrer':<12} {'links':>8} {'to a target':>12} {'lift':>6} "
          f"{'to a dead end':>14} {'off-site':>9}   weight now")
    rows = sorted(offered, key=lambda c: -(to_wanted[c] / offered[c]) if offered[c] else 0)
    for category in rows:
        count = offered[category]
        if count < args.min_links:
            continue
        share = to_wanted[category] / count
        print(f"  {str(category):<12} {count:>8} {share:>11.1%} "
              f"{share / base if base else 0:>6.2f} {to_dead[category] / count:>13.0%} "
              f"{off_site[category] / count:>8.0%}   "
              f"{config.referrer_weights.get(category, 0.0):>6.1f}")


if __name__ == "__main__":
    main()
