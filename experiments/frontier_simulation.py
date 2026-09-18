"""
Replay the crawl frontier over the pages already crawled, policy against policy.

Comparing two ordering policies live costs hours per arm and still cannot
reach the part of a crawl where they differ: early on every page is near a
station, so any closeness bonus is uniform and changes no order. The drift
that matters appears twenty or thirty rounds in.

The database already holds what is needed to replay that offline: every page
that was fetched, the category it was given, and its stored body - from which
its links can be read again without touching the network or the model. This
walks that graph from the seed URLs under each policy and reports how many
stations and listings each one reaches per page fetched.

What it cannot show: pages the real crawl never fetched. A policy is measured
on how well it *orders* the known graph, not on what it would have found
beyond it - so treat the numbers as a comparison, not a yield.

Usage:
    python experiments/frontier_simulation.py [--db crawl.db] [--steps 4000]
"""
import argparse
import json
import sys
from heapq import heapify, heappush, heappop
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store, url_service  # noqa: E402
from model.analyzer import cleaning_service  # noqa: E402

CACHE = Path(__file__).parent / "data" / "frontier_graph.json"


def _links_of(entry):
    """A cached page's links, as (url, text) pairs whichever way they were stored."""
    return [(link, "") if isinstance(link, str) else tuple(link) for link in entry[1]]


def build_graph(db, cache_path):
    """url -> (category, [(linked url, link text)]) for every stored page, cached."""
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    data_service.DATABASE_PATH = Path(db)
    graph = {}
    with data_service.get_connection() as connection:
        rows = connection.execute(
            "SELECT source_url, category, content_path FROM crawls "
            "WHERE content_path IS NOT NULL").fetchall()
    known = {url_service.canonicalize(row["source_url"]) for row in rows}
    for index, row in enumerate(rows, 1):
        html = page_store.load(row["content_path"])
        if not html:
            continue
        links = []
        for link, text in cleaning_service.extract_links_with_text(html, row["source_url"]):
            canonical = url_service.canonicalize(link)
            if canonical in known:
                links.append([canonical, text])
        graph[url_service.canonicalize(row["source_url"])] = [row["category"], links]
        if index % 1000 == 0:
            print(f"   read {index}/{len(rows)} pages", flush=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(graph), encoding="utf-8")
    return graph


def simulate(graph, seeds, config, decay, steps, wanted, prune_below=0.0,
             leaving_site_costs=0.0, hub_categories=("LIST", "ADVICE"),
             use_link_text=False, abandon_after=0, abandon_penalty=0.0):
    """
    Walk the graph best-first under one policy.

    :param decay: closeness multiplier per hop; 0 is the policy without it.
    :param prune_below: stop following links once their closeness falls under
                         this - a cut-off rather than a demotion.
    :param abandon_after: low-value pages a site may give before it is
                           abandoned; 0 never abandons one.
    :param abandon_penalty: what an abandoned site's links cost. 0 refuses
                             them outright, which is what the crawler did
                             first; anything else demotes them instead, so a
                             tunnel through a dull site stays passable
                             (Bergmark et al. 2002).
    :return: list of how many wanted pages had been found after each fetch.
    """
    weights = config.referrer_weights
    source_min = config.closeness_source_min
    queue, queued, seen, found, curve = [], {}, set(), 0, []
    wasted, waste_curve = 0, []
    off_target = {"IRRELEVANT", "COMMERCIAL", "AUTHORITY", "ADVOCACY", None}
    counter = 0
    low_value, productive, abandoned = {}, set(), set()

    def offer(url, priority, closeness):
        nonlocal counter
        if url in seen:
            return
        site = url_service.registrable_domain(url)
        if site in abandoned:
            if not abandon_penalty:
                return
            priority -= abandon_penalty
        if url in queued and queued[url] >= (priority, closeness):
            return
        queued[url] = (priority, closeness)
        counter += 1
        heappush(queue, (-priority, counter, url, closeness))

    for seed in seeds:
        offer(seed, 100.0, 0.0)

    while queue and len(curve) < steps:
        negative, _, url, closeness = heappop(queue)
        if url in seen or queued.get(url, (None,))[0] != -negative:
            continue
        seen.add(url)
        entry = graph.get(url, [None, []])
        category, links = entry[0], _links_of(entry)
        this_site = url_service.registrable_domain(url)
        if abandon_after and this_site and this_site not in abandoned:
            if category in wanted:
                productive.add(this_site)
            elif (this_site not in productive
                  and weights.get(category, 0.0) <= config.abandon_site_max_weight):
                low_value[this_site] = low_value.get(this_site, 0) + 1
                if low_value[this_site] >= abandon_after:
                    abandoned.add(this_site)
                    if not abandon_penalty:
                        queue[:] = [row for row in queue
                                    if url_service.registrable_domain(row[2]) != this_site]
                        heapify(queue)
                    else:
                        for index, row in enumerate(queue):
                            if url_service.registrable_domain(row[2]) == this_site:
                                queue[index] = (row[0] + abandon_penalty, *row[1:])
                                # queued[] is what tells a stale heap entry
                                # from a live one, so it has to move too.
                                queued[row[2]] = (-queue[index][0], row[3])
                        heapify(queue)
        if category in wanted:
            found += 1
        if category in off_target:
            wasted += 1
        curve.append(found)
        waste_curve.append(wasted)
        weight = weights.get(category, 0.0)
        source = weight if weight >= source_min else closeness
        child = source * decay
        if prune_below and child < prune_below:
            continue
        site = url_service.registrable_domain(url)
        for link, text in links:
            leaving = (leaving_site_costs and category not in hub_categories
                       and url_service.registrable_domain(link) != site)
            offer(link, (-leaving_site_costs if leaving else 0.0) + url_service.score_url(
                link, referrer_category_weight=weight,
                identity_tokens=config.url_tokens_identity,
                exclude_tokens=config.url_tokens_exclude,
                link_text=text if use_link_text else "",
                anchor_identity_tokens=config.anchor_tokens_identity,
                anchor_exclude_tokens=config.anchor_tokens_exclude,
                anchor_identity_bonus=config.anchor_identity_bonus,
                anchor_exclude_penalty=config.anchor_exclude_penalty) + child, child)
    return curve, waste_curve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--decays", default="0,0.3,0.5,0.7")
    ap.add_argument("--leaving", default="0",
                    help="what it costs a link to leave its site, unless the page is a listing")
    ap.add_argument("--prune", default="0",
                    help="closeness floors to try: below this, links are not followed at all")
    ap.add_argument("--link-text", default="0",
                    help="0/1: whether the words on a link count towards its score")
    ap.add_argument("--abandon-after", type=int, default=0,
                    help="low-value pages a site may give before it is abandoned")
    ap.add_argument("--abandon-penalty", default="0",
                    help="what an abandoned site's links cost; 0 refuses them outright")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    page_store.configure(config.page_store_path)
    if args.rebuild and CACHE.exists():
        CACHE.unlink()
    graph = build_graph(args.db, CACHE)
    print(f"{len(graph)} pages in the graph, "
          f"{sum(len(v[1]) for v in graph.values())} internal links")

    seeds = []
    for path in config.get_starting_url_path() if isinstance(config.get_starting_url_path(), list) \
            else [config.get_starting_url_path()]:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                canonical = url_service.canonicalize(line)
                if canonical in graph:
                    seeds.append(canonical)
    wanted = {c.name for c in config.categories if c.is_relevant}
    print(f"{len(seeds)} seed(s) present in the graph; counting {sorted(wanted)}")

    marks = [m for m in (250, 500, 1000, 2000, 4000) if m <= args.steps]
    print("cells are: targets found / fetches wasted, after that many fetches")
    print("\ndecay prune leav text abdn | "
          + " | ".join(f"{m:>5} fetched" for m in marks) + " |  reached")
    policies = product((float(d) for d in args.decays.split(",")),
                       (float(p) for p in args.prune.split(",")),
                       (float(v) for v in args.leaving.split(",")),
                       (bool(int(t)) for t in args.link_text.split(",")),
                       (float(v) for v in args.abandon_penalty.split(",")))
    for decay, prune, leaving, text, give_up in policies:
        curve, waste = simulate(graph, seeds, config, decay, args.steps, wanted, prune,
                                leaving, use_link_text=text,
                                abandon_after=args.abandon_after,
                                abandon_penalty=give_up)
        cells = [f"{curve[mark - 1]:>5} /{waste[mark - 1]:>6}" if len(curve) >= mark
                 else f"{'-':>13}" for mark in marks]
        print(f"{decay:5.2f} {prune:5.2f} {leaving:4.1f} {int(text):>4} {give_up:5.1f} | "
              + " | ".join(cells) + f" | {len(curve):>7}")


if __name__ == "__main__":
    main()
