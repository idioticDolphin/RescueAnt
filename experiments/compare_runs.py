"""
Compare two crawl databases on the quality measures this work targets.

    python experiments/compare_runs.py crawl_second_hub.db crawl.db

Reports, for each database: page-state health (how much work would be lost if
the run stopped now), category mix, extraction yield, duplicate rate after
entity resolution, and the share of records that look like site boilerplate
rather than real entities.
"""
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from evaluate_dedup import SEMANTICS  # noqa: E402
from model.analyzer.entity_service import Resolver  # noqa: E402
from model.tools.url_service import path_tokens, registrable_domain  # noqa: E402

# Paths that cannot be an entity's own page; used to estimate false positives
# without hand-labelling. Mirrors config/lexicon/multilingual.config.
JUNK_TOKENS = {
    "datenschutz", "impressum", "agb", "presse", "spenden", "shop", "warenkorb",
    "jobs", "karriere", "galerie", "bilder", "termine", "newsletter", "suche",
    "privacy", "terms", "press", "donate", "cart", "gallery", "events", "search",
}


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def analyse(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = {"db": db_path}

    crawl_cols = _columns(con, "crawls")
    out["crawls"] = con.execute("SELECT COUNT(*) FROM crawls").fetchone()[0]
    out["fetched_ok"] = con.execute(
        "SELECT COUNT(*) FROM crawls WHERE fetch_success=1").fetchone()[0]
    out["stranded"] = con.execute(
        "SELECT COUNT(*) FROM crawls WHERE fetch_success=1 "
        "AND (category IS NULL OR category='')").fetchone()[0]
    out["has_states"] = "state" in crawl_cols
    if out["has_states"]:
        out["states"] = dict(con.execute("SELECT state, COUNT(*) FROM crawls GROUP BY state"))
    out["categories"] = dict(con.execute(
        "SELECT category, COUNT(*) FROM crawls WHERE category IS NOT NULL GROUP BY category"))

    records = [dict(r) for r in con.execute("""
        SELECT e.*, c.source_url AS _source_url, c.category AS _category
        FROM entries e JOIN crawls c ON e.source_crawl_id = c.crawl_id
    """)]
    out["records"] = len(records)

    # False-positive proxy: a record extracted from a page whose path is
    # structurally incapable of being an entity's own page.
    out["from_junk_paths"] = sum(
        1 for r in records if path_tokens(r.get("_source_url") or "") & JUNK_TOKENS)
    # ...and records with nothing that identifies or locates them.
    out["no_identity"] = sum(
        1 for r in records
        if not any(r.get(f) for f in ("e-mail", "telephone", "mobile", "address", "station_url")))

    if records:
        for r in records:
            own = registrable_domain(r.get("station_url") or "")
            src = registrable_domain(r.get("_source_url") or "")
            r["_trust"] = 1.0 if own and own == src else 0.5
        resolver = Resolver(field_semantics=SEMANTICS)
        clusters = resolver.cluster(records)
        out["entities"] = len(clusters)
        out["duplicate_rate"] = 1 - len(clusters) / len(records)
        worst = Counter(registrable_domain(r.get("_source_url") or "") for r in records)
        site, count = worst.most_common(1)[0] if worst else ("", 0)
        site_entities = sum(1 for c in clusters
                            if registrable_domain(c[0].get("_source_url") or "") == site)
        out["worst_site"] = (site, count, site_entities)
    else:
        out["entities"] = 0
        out["duplicate_rate"] = 0.0
        out["worst_site"] = ("", 0, 0)
    return out


def _pct(part, whole):
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def main(*paths):
    results = [analyse(p) for p in paths if Path(p).exists()]
    if not results:
        print("no databases found")
        return

    rows = [
        ("crawled pages", lambda r: r["crawls"]),
        ("fetched successfully", lambda r: r["fetched_ok"]),
        ("fetched but unprocessed", lambda r: f"{r['stranded']} ({_pct(r['stranded'], r['fetched_ok'])})"),
        ("recoverable on restart", lambda r: "yes" if r["has_states"] else "NO - lost"),
        ("extracted records", lambda r: r["records"]),
        ("records from junk paths", lambda r: f"{r['from_junk_paths']} ({_pct(r['from_junk_paths'], r['records'])})"),
        ("records w/o identity", lambda r: f"{r['no_identity']} ({_pct(r['no_identity'], r['records'])})"),
        ("distinct entities", lambda r: r["entities"]),
        ("duplicate rate", lambda r: f"{100 * r['duplicate_rate']:.1f}%"),
        ("worst site redundancy", lambda r: (
            f"{r['worst_site'][1]}->{r['worst_site'][2]} "
            f"({r['worst_site'][1] / r['worst_site'][2]:.1f}x)" if r["worst_site"][2] else "n/a")),
    ]

    width = max(len(label) for label, _ in rows) + 2
    print(f"{'':<{width}}" + "".join(f"{Path(r['db']).name:>28}" for r in results))
    print("-" * (width + 28 * len(results)))
    for label, getter in rows:
        print(f"{label:<{width}}" + "".join(f"{str(getter(r)):>28}" for r in results))

    print("\ncategory mix:")
    for r in results:
        total = sum(r["categories"].values()) or 1
        mix = ", ".join(f"{k} {v} ({100*v/total:.0f}%)"
                        for k, v in sorted(r["categories"].items(), key=lambda kv: -kv[1]))
        print(f"  {Path(r['db']).name:<26} {mix}")


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["crawl_first.db", "crawl_second_hub.db", "crawl.db"]))
