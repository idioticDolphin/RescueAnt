"""
Measure how much of the resolved database a person would keep.

Precision is the release criterion, and it can only be judged by reading
records. This script makes that reading repeatable:

  sample  draws a fixed random sample of entities from a database and writes a
          labelling sheet - every field of the entity plus where it came from
          (source categories and page URLs), with empty judgement columns.
  score   reads a filled sheet and reports precision overall and broken down by
          source category, so a change can be aimed at where the junk comes
          from.

Judgement columns:
  relevant     yes | no | unsure - is this an organisation (or person acting
               as one) that takes in, treats, shelters or rehomes animals?
  contactable  yes | no - does the record carry enough to reach it: a phone,
               e-mail, postal address or its own website?
  problem      free text naming what is wrong, for records judged "no"

Usage:
    python experiments/entity_precision.py sample snapshot.db sheet.csv [--n 100] [--seed 1]
    python experiments/entity_precision.py score sheet.csv
"""
import argparse
import csv
import random
import sqlite3
from collections import Counter, defaultdict

FIELDS = ["name", "address", "telephone", "e-mail", "station_url", "service_area",
          "accepted_animals", "description"]


def sample(db, out, n, seed):
    c = sqlite3.connect(db)
    ids = [r[0] for r in c.execute("SELECT entity_id FROM entities ORDER BY entity_id")]
    rng = random.Random(seed)
    chosen = sorted(rng.sample(ids, min(n, len(ids))))
    columns = ", ".join(f'"{f}"' for f in FIELDS)
    rows = []
    for entity_id in chosen:
        values = c.execute(f"SELECT {columns}, n_sources FROM entities WHERE entity_id = ?",
                           (entity_id,)).fetchone()
        sources = c.execute(
            """SELECT DISTINCT cr.category, cr.source_url FROM entity_sources s
               JOIN entries e ON e.entry_id = s.entry_id
               JOIN crawls cr ON cr.crawl_id = e.source_crawl_id
               WHERE s.entity_id = ?""", (entity_id,)).fetchall()
        row = dict(zip(FIELDS, values[:-1]))
        row.update({
            "entity_id": entity_id,
            "n_sources": values[-1],
            "source_categories": "|".join(sorted({s[0] or "" for s in sources})),
            "source_urls": " ".join(sorted({s[1] for s in sources}))[:500],
            "relevant": "", "contactable": "", "problem": "",
        })
        rows.append(row)
    header = (["entity_id", "relevant", "contactable", "problem", "source_categories", "n_sources"]
              + FIELDS + ["source_urls"])
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} of {len(ids)} entities to {out}")


def score(sheet):
    with open(sheet, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("relevant")]
    if not rows:
        print("no judged rows")
        return

    def report(label, group):
        relevant = sum(r["relevant"] == "yes" for r in group)
        unsure = sum(r["relevant"] == "unsure" for r in group)
        usable = sum(r["relevant"] == "yes" and r["contactable"] == "yes" for r in group)
        n = len(group)
        print(f"{label:<28} n={n:>3}  relevant {100 * relevant / n:5.1f}%  "
              f"(+{unsure} unsure)  usable {100 * usable / n:5.1f}%")

    report("all", rows)
    by_source = defaultdict(list)
    for r in rows:
        kind = "listing only" if r["source_categories"] == "LIST" else (
            "own page" if "LIST" not in r["source_categories"].split("|") else "listing + own page")
        by_source[kind].append(r)
    for kind, group in sorted(by_source.items()):
        report(f"  {kind}", group)

    problems = Counter(r["problem"].split(":")[0].strip() for r in rows
                       if r["relevant"] != "yes" or r["contactable"] != "yes")
    print("\nproblems:")
    for problem, count in problems.most_common():
        print(f"  {count:>3}  {problem or '(unstated)'}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("db")
    s.add_argument("out")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=1)
    sc = sub.add_parser("score")
    sc.add_argument("sheet")
    args = ap.parse_args()
    if args.command == "sample":
        sample(args.db, args.out, args.n, args.seed)
    else:
        score(args.sheet)


if __name__ == "__main__":
    main()
