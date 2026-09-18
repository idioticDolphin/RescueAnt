"""
What does visiting a listed organisation's own website actually add?

`follow_record_urls[LIST]` queues the websites named in a listing's records at
a priority above everything else, on the argument that a listing gives a name
and a town while the organisation's own site gives the address, the phone
number and the e-mail. That argument was never measured; this measures it.

Every resolved entity is sorted by where its records came from:

  listing only    every source record was extracted from a listing page
  own site only   every source record came from a page on the entity's own
                  site (its website field, or a page whose host matches)
  both            the enrichment the setting is supposed to produce

and for each group it reports how many carry a way to reach the
organisation - the thing a directory is for. The interesting number is the
gap between "listing only" and "both": that is what one extra fetch per
listed organisation buys.

Usage:
    python experiments/enrichment_value.py [--db crawl.db]
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, url_service  # noqa: E402

CONTACT_FIELDS = ("telephone", "mobile", "e-mail", "address", "emergency_telephone")


def load(db):
    data_service.DATABASE_PATH = Path(db)
    with data_service.get_connection() as connection:
        entities = connection.execute("SELECT * FROM entities").fetchall()
        sources = connection.execute(
            "SELECT s.entity_id, e.*, c.source_url, c.category "
            "FROM entity_sources s "
            "JOIN entries e ON e.entry_id = s.entry_id "
            "JOIN crawls c ON c.crawl_id = e.source_crawl_id").fetchall()
    return entities, sources


def sites_of(value):
    """Registrable domains in a field that may hold one URL or a fused list."""
    if not value:
        return set()
    try:
        values = json.loads(value)
    except (TypeError, ValueError):
        values = [value]
    if not isinstance(values, list):
        values = [values]
    return {url_service.registrable_domain(str(v)) for v in values} - {""}


def origin(row, entity_sites):
    """Where one source record came from, as seen from its entity."""
    page_site = url_service.registrable_domain(row["source_url"])
    if page_site and page_site in entity_sites:
        return "own site"
    return "listing" if row["category"] == "LIST" else "other page"


def contactable(record, fields):
    return any((record[field] or "").strip() for field in fields
               if field in record.keys())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="crawl.db")
    args = parser.parse_args()
    config_service.load_config()
    config = config_service.get_config()
    fields = [f for f in CONTACT_FIELDS if f in config.field_semantics]

    entities, sources = load(args.db)
    by_entity = {}
    for row in sources:
        by_entity.setdefault(row["entity_id"], []).append(row)

    groups = Counter()
    reachable = Counter()
    gained = 0
    for entity in entities:
        rows = by_entity.get(entity["entity_id"], [])
        sites = sites_of(entity["station_url"])
        for row in rows:
            sites |= sites_of(row["station_url"])
        origins = {origin(row, sites) for row in rows}
        if origins == {"listing"}:
            group = "listing only"
        elif "own site" in origins and "listing" in origins:
            group = "listing + own site"
        elif "own site" in origins:
            group = "own site only"
        else:
            group = "other pages"
        groups[group] += 1
        if contactable(entity, fields):
            reachable[group] += 1
        if group == "listing + own site":
            listing_rows = [r for r in rows if origin(r, sites) == "listing"]
            if not any(contactable(r, fields) for r in listing_rows) \
                    and contactable(entity, fields):
                gained += 1

    print(f"\n{len(entities)} entities, {len(sources)} source records, "
          f"contact fields: {', '.join(fields)}\n")
    print(f"  {'where the records came from':<22} {'entities':>9} "
          f"{'reachable':>10} {'share':>7}")
    for group in ("listing only", "listing + own site", "own site only", "other pages"):
        count = groups[group]
        if not count:
            continue
        print(f"  {group:<22} {count:>9} {reachable[group]:>10} "
              f"{reachable[group] / count:>6.0%}")
    if groups["listing + own site"]:
        print(f"\n  {gained} of the {groups['listing + own site']} enriched entities "
              f"had no contact detail from their listing alone,\n"
              f"  and are reachable only because the organisation's own site was visited.")
    unvisited = groups["listing only"]
    print(f"\n  {unvisited} entities are still listing-only: their own site is either "
          f"unknown or unvisited.")


if __name__ == "__main__":
    main()
