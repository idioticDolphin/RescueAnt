"""
Measure entity resolution against a real crawl database.

Reports how many raw observation records collapse into how many entities,
per site and overall, and prints the largest clusters so merges can be
eyeballed. Run it before and after a change to see the effect:

    python experiments/evaluate_dedup.py crawl_first.db
"""
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.analyzer.entity_service import Resolver  # noqa: E402
from model.tools.url_service import registrable_domain  # noqa: E402

# Field semantics for the wildlife-rescue schema. These live in bot.config for
# a real run; they are repeated here so the script is runnable standalone.
SEMANTICS = {
    "name":                {"role": "label", "weight": 0.3},
    "e-mail":              {"role": "identifier", "weight": 1.0, "normalize": "email", "fusion": "union"},
    "telephone":           {"role": "identifier", "weight": 0.9, "normalize": "phone", "fusion": "union"},
    "mobile":              {"role": "identifier", "weight": 0.9, "normalize": "phone", "fusion": "union"},
    "emergency_telephone": {"role": "attribute", "fusion": "first_by_trust"},
    "station_url":         {"role": "identifier", "weight": 0.8, "normalize": "url", "fusion": "union"},
    "address":             {"role": "locator", "weight": 0.7, "fusion": "trust_then_valid"},
    "country":             {"role": "attribute", "fusion": "trust_then_mode"},
    "service_area":        {"role": "attribute", "fusion": "trust_then_mode"},
    "accepted_animals":    {"role": "attribute", "fusion": "union"},
    "animal_pickup":       {"role": "attribute", "fusion": "or_with_evidence"},
    "opening_hours":       {"role": "attribute", "fusion": "longest_from_top_trust"},
    "description":         {"role": "attribute", "fusion": "longest_from_top_trust"},
    "social_media":        {"role": "attribute", "fusion": "union"},
}


def load(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("""
        SELECT e.*, c.source_url AS _source_url, c.category AS _category
        FROM entries e JOIN crawls c ON e.source_crawl_id = c.crawl_id
    """)]
    for row in rows:
        # A record from an entity's own site outranks one from a directory.
        own_site = registrable_domain(row.get("station_url") or "") == \
                   registrable_domain(row.get("_source_url") or "")
        row["_trust"] = 1.0 if own_site else 0.5
    return rows


def main(db_path="crawl_first.db"):
    records = load(db_path)
    resolver = Resolver(field_semantics=SEMANTICS)
    clusters = resolver.cluster(records)

    print(f"database              : {db_path}")
    print(f"raw observation records: {len(records)}")
    print(f"resolved entities      : {len(clusters)}")
    reduction = 100 * (1 - len(clusters) / len(records)) if records else 0
    print(f"duplicate rate removed : {reduction:.1f}%")

    print("\nlargest merges:")
    for cluster in sorted(clusters, key=len, reverse=True)[:8]:
        if len(cluster) < 2:
            continue
        fused = resolver.fuse(cluster)
        sites = Counter(registrable_domain(r.get("_source_url") or "") for r in cluster)
        print(f"  {len(cluster):3d} records -> {str(fused.get('name'))[:46]!r}")
        print(f"      from: {', '.join(f'{s} x{n}' for s, n in sites.most_common(3))}")
        print(f"      confidence={fused['_confidence']} conflicts={len(fused['_conflicts'])}")

    # Per-site redundancy: the headline defect was one organisation becoming
    # many rows, so measure it the same way before and after.
    print("\nper-site redundancy (records -> entities):")
    by_site_records = defaultdict(int)
    for record in records:
        by_site_records[registrable_domain(record.get("_source_url") or "")] += 1
    by_site_entities = defaultdict(int)
    for cluster in clusters:
        by_site_entities[registrable_domain(cluster[0].get("_source_url") or "")] += 1
    for site, count in sorted(by_site_records.items(), key=lambda kv: -kv[1])[:8]:
        entities = by_site_entities.get(site, 0)
        ratio = count / entities if entities else float("inf")
        print(f"  {site:32s} {count:4d} -> {entities:4d}  ({ratio:.1f}x)")

    # Guard against over-merging: distinct records that share a generic name
    # must stay apart.
    print("\nover-merge guard (clusters containing conflicting addresses):")
    bad = 0
    for cluster in clusters:
        addresses = {(r.get("address") or "").strip() for r in cluster if r.get("address")}
        if len(addresses) > 1:
            bad += 1
            if bad <= 3:
                print(f"  {len(cluster)} records, {len(addresses)} distinct addresses: "
                      f"{list(addresses)[:2]}")
    print(f"  clusters with >1 distinct address: {bad} of {len(clusters)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "crawl_first.db")
