"""
Does the text of a link predict what is at the other end of it?

The frontier scores a link by the category of the page that offered it, by
tokens in its path and by depth - never by the words a reader would have
clicked. The focused-crawling literature treats anchor text as a primary
feature (Lu et al. 2016), so its absence here is a gap rather than a
simplification; this measures what closing it would be worth, before any of it
reaches the crawler.

The corpus is what has already been crawled: every stored page body gives its
links and their texts, and every link whose target was also fetched comes with
the answer - the category that target was given. Two signals are scored:

  configured tokens   the identity and exclude tokens the URL scorer already
                      uses, applied to the link text instead of the path
  learned tokens      a naive Bayes model over link-text words, trained and
                      tested on disjoint *sites* - a page-level split flatters
                      badly, because one site's links repeat its own wording

It also prints the words that carry the most weight, which is what a lexicon
entry would be made of.

Usage:
    python experiments/anchor_text_value.py [--db crawl.db] [--rebuild]
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store, url_service  # noqa: E402
from model.analyzer import cleaning_service  # noqa: E402

CACHE = Path(__file__).parent / "data" / "anchor_graph.json"
WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def build_links(db, cache_path, rebuild=False):
    """[(source site, link text, target category)] for every link with a known target."""
    if cache_path.exists() and not rebuild:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    data_service.DATABASE_PATH = Path(db)
    with data_service.get_connection() as connection:
        rows = connection.execute(
            "SELECT source_url, category, content_path FROM crawls "
            "WHERE content_path IS NOT NULL").fetchall()
    category_of = {url_service.canonicalize(row["source_url"]): row["category"]
                   for row in rows}
    links = []
    for index, row in enumerate(rows, 1):
        html = page_store.load(row["content_path"])
        if not html:
            continue
        site = url_service.registrable_domain(row["source_url"])
        for url, text in cleaning_service.extract_links_with_text(html, row["source_url"]):
            target = category_of.get(url_service.canonicalize(url))
            if target and text:
                links.append([site, text, target])
        if index % 2000 == 0:
            print(f"   read {index}/{len(rows)} pages, {len(links)} labelled links",
                  flush=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(links), encoding="utf-8")
    return links


def report(name, hits, wanted_hits, total, wanted_total):
    """One row: how often the signal fires, how right it is, how much it catches."""
    if not hits:
        print(f"  {name:<30} never fires")
        return
    precision = wanted_hits / hits
    base = wanted_total / total
    print(f"  {name:<30} fires on {hits:>6} ({hits / total:>5.1%}), "
          f"{precision:>5.1%} on target, lift {precision / base:>4.2f}x, "
          f"catches {wanted_hits / wanted_total:>5.1%} of them")


def naive_bayes(train, test, wanted):
    """Train on one set of sites, score the other. Returns (threshold, rows)."""
    counts = {True: Counter(), False: Counter()}
    totals = {True: 0, False: 0}
    for _, text, category in train:
        label = category in wanted
        words = set(WORD.findall(text.casefold()))
        counts[label].update(words)
        totals[label] += 1
    vocabulary = set(counts[True]) | set(counts[False])
    weight = {}
    for word in vocabulary:
        if counts[True][word] + counts[False][word] < 5:
            continue  # a word seen four times says nothing it can be trusted on
        good = (counts[True][word] + 1) / (totals[True] + 2)
        bad = (counts[False][word] + 1) / (totals[False] + 2)
        weight[word] = math.log(good / bad)
    scored = []
    for _, text, category in test:
        words = set(WORD.findall(text.casefold()))
        scored.append((sum(weight.get(word, 0.0) for word in words),
                       category in wanted))
    return weight, scored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="crawl.db")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--tokens", default=None,
                        help="comma-separated candidate link-text tokens to score "
                             "as a rule, e.g. the ones a lexicon entry would hold")
    args = parser.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    page_store.configure(config.page_store_path)
    links = build_links(args.db, CACHE, args.rebuild)
    wanted = {c.name for c in config.categories if c.is_relevant}

    total = len(links)
    wanted_total = sum(1 for _, _, category in links if category in wanted)
    print(f"\n{total} links with text and a known target, "
          f"{wanted_total} of them to {sorted(wanted)} ({wanted_total / total:.1%})\n")

    candidates = []
    if args.tokens:
        candidates.append(("candidate tokens", [t.strip() for t in args.tokens.split(",")]))
    print("Tokens matched against the link text:")
    for label, tokens in (("url_tokens[identity]", config.url_tokens_identity),
                          ("url_tokens[exclude]", config.url_tokens_exclude),
                          ("anchor_tokens[identity]", config.anchor_tokens_identity),
                          *candidates):
        tokens = [t.casefold() for t in tokens]
        hits = [(text, category) for _, text, category in links
                if any(token in text.casefold() for token in tokens)]
        report(label, len(hits), sum(1 for _, c in hits if c in wanted), total, wanted_total)

    sites = sorted({site for site, _, _ in links})
    held_out = set(sites[::3])  # every third site, by name - stable across runs
    train = [row for row in links if row[0] not in held_out]
    test = [row for row in links if row[0] in held_out]
    print(f"\nLearned link-text model: {len(train)} links from {len(sites) - len(held_out)} "
          f"sites, tested on {len(test)} links from {len(held_out)} held-out sites")

    weight, scored = naive_bayes(train, test, wanted)
    scored.sort(key=lambda row: -row[0])
    base = sum(1 for _, label in scored if label) / len(scored)
    print(f"  base rate on the held-out sites: {base:.1%}")
    for share in (0.05, 0.1, 0.25, 0.5):
        cut = max(1, int(len(scored) * share))
        top = sum(1 for _, label in scored[:cut] if label)
        print(f"  best {share:>4.0%} of links by text: {top / cut:>5.1%} on target, "
              f"lift {top / cut / base:>4.2f}x")

    seen = Counter(word for _, text, _ in train for word in set(WORD.findall(text.casefold())))
    strong = sorted((w for w in weight if seen[w] >= 40), key=lambda w: -weight[w])
    print("\n  words most in favour:", ", ".join(strong[:25]))
    print("  words most against: ", ", ".join(reversed(strong[-25:])))


if __name__ == "__main__":
    main()
