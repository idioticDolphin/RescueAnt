"""
What a frontier score is worth, so `discovery_when_below` can be set from data.

Discovery fires when the best queued score falls below a threshold. That
threshold was a guess. The recorded link graph can price it: every link the
crawl ever followed can be scored exactly as the crawler scores it, and the
category its target turned out to have is known, so a score can be read as a
probability that the fetch is worth making.

Where that probability falls below what the crawl gets from an average link,
the frontier has strayed and a search engine is the better next move - which
is the sentence `discovery_when_below` is meant to encode.

What it cannot show: what a search query would have returned instead. It
prices the links, not the alternative.

Usage:
    python experiments/frontier_threshold.py [--buckets 1.0]
"""
import argparse
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, page_store, url_service  # noqa: E402
from frontier_simulation import CACHE, build_graph, _links_of  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="crawl.db")
    parser.add_argument("--buckets", type=float, default=1.0,
                        help="width of each score bucket")
    args = parser.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    page_store.configure(config.page_store_path)
    graph = build_graph(args.db, CACHE)
    wanted = {c.name for c in config.categories if c.is_relevant}
    weights = config.referrer_weights

    # The best score a URL was ever offered is the one it is fetched at, so
    # score each target by the best offer it received - as the queue does.
    best = {}
    for url, entry in graph.items():
        category = entry[0]
        weight = weights.get(category, 0.0)
        site = url_service.registrable_domain(url)
        exempt = weight >= config.leaving_site_exempt_min_weight
        for link, text in _links_of(entry):
            leaving = (not exempt
                       and url_service.registrable_domain(link) != site)
            score = url_service.score_url(
                link, referrer_category_weight=weight,
                identity_tokens=config.url_tokens_identity,
                exclude_tokens=config.url_tokens_exclude,
                link_text=text,
                anchor_identity_tokens=config.anchor_tokens_identity,
                anchor_exclude_tokens=config.anchor_tokens_exclude,
                anchor_identity_bonus=config.anchor_identity_bonus,
                anchor_exclude_penalty=config.anchor_exclude_penalty)
            if leaving:
                score -= config.leaving_site_penalty
            if score > best.get(link, -math.inf):
                best[link] = score

    offered = Counter()
    hits = Counter()
    for link, score in best.items():
        bucket = math.floor(score / args.buckets) * args.buckets
        offered[bucket] += 1
        if graph.get(link, [None])[0] in wanted:
            hits[bucket] += 1

    total = sum(offered.values())
    total_hits = sum(hits.values())
    base = total_hits / total
    print(f"\n{total} links scored, {total_hits} of them to {sorted(wanted)} "
          f"({base:.1%} - the average link)\n")
    print(f"  {'best score offered':<22} {'links':>8} {'on target':>10} "
          f"{'lift':>6}   {'if the queue stops here':<24}")
    running_hits = running = 0
    for bucket in sorted(offered, reverse=True):
        running += offered[bucket]
        running_hits += hits[bucket]
        share = hits[bucket] / offered[bucket]
        print(f"  {bucket:>6.1f} and up{'':<7} {offered[bucket]:>8} "
              f"{share:>9.1%} {share / base:>6.2f}   "
              f"{running:>7} fetched, {running_hits / running:>5.1%} on target")


if __name__ == "__main__":
    main()
