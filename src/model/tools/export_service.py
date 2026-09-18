"""
Write the resolved entities out as CSV, with a review column.

The database is the crawler's working store; what a person wants at the end is
a file they can open, sort and hand on. Alongside the fields, each row carries
what the crawl knows about its own uncertainty - how many pages agreed, how
confident resolution was, which values conflicted - so cleanup can start with
the rows that need it instead of the top of the list.

Which fields count as a way to reach an organisation is read from the schema
(`normalize` phone/email and the `locator` role), so this stays as
domain-neutral as the rest of the pipeline.
"""
import csv
import json
import logging

import model.tools.config_service as config_service
import model.tools.data_service as data_service
import model.tools.url_service as url_service

logger = logging.getLogger(__name__)

NO_CONTACT = "no-direct-contact"
CONFLICTS = "conflicting-contact"

# Where a row's evidence came from, worst first. Not a review flag: four rows
# in five of a real export are listing-only, and a flag on four rows in five
# tells nobody anything. It is a column to sort and filter by.
LISTING_ONLY = "listing-only"
LISTING_AND_OWN = "listing+own-page"
OWN_PAGE = "own-page"


def _as_text(value):
    """Flatten a stored value into something a spreadsheet can show."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(("[", "{")):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return "; ".join(_as_text(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_as_text(v)}" for k, v in value.items())
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _contact_fields(config):
    """Field names by which a person could actually get in touch."""
    semantics = config.field_semantics or {}
    return [name for name, sem in semantics.items()
            if sem.get("normalize") in ("phone", "email") or sem.get("role") == "locator"]


def review_flags(row, contact_fields, conflicts):
    """
    Why a row may need a person's eye, as a list of short tags.

    Deliberately narrow. Flagging every single-source row, or every field that
    ever differed between two pages, flagged all 547 rows of one export and
    told nobody anything: how many pages agreed is already a column, and
    descriptions differ between pages as a matter of course. What is worth a
    look is a record nobody could contact, and one whose identifying or
    locating details disagree. Where the row's evidence came from is a column
    of its own - see _evidence().
    """
    flags = []
    if not any(_as_text(row.get(field)).strip() for field in contact_fields):
        flags.append(NO_CONTACT)
    if conflicts & set(contact_fields):
        flags.append(CONFLICTS)
    return flags


def _evidence(connection, config, entities):
    """
    Where each entity's records came from: {entity_id: OWN_SITE | ... }.

    A record read off somebody else's listing and a record read off the
    organisation's own page are not equally trustworthy - measured on this
    project's own database, a listing entry is a wildlife station about two
    thirds of the time and an own-site record nearly always. A person cleaning
    the export wants to know which they are looking at, and the crawler cannot
    tell them by deleting one of the two.
    """
    list_categories = {c.name for c in config.categories if c.is_list_category}
    own_sites, from_listing, from_elsewhere = {}, set(), set()
    rows = connection.execute(
        "SELECT s.entity_id, c.source_url, c.category, e.* FROM entity_sources s "
        "JOIN entries e ON e.entry_id = s.entry_id "
        "JOIN crawls c ON c.crawl_id = e.source_crawl_id").fetchall()
    url_fields = [name for name, sem in (config.field_semantics or {}).items()
                  if sem.get("normalize") == "url"]

    def note_sites(entity_id, record):
        keys = record.keys()
        for field in url_fields:
            if field in keys:
                for value in _as_text(record[field]).split(";"):
                    site = url_service.registrable_domain(value.strip())
                    if site:
                        own_sites.setdefault(entity_id, set()).add(site)

    # The fused entity carries every website its records agreed on; the records
    # themselves carry the one each page gave.
    for entity in entities:
        note_sites(entity["entity_id"], entity)
    for row in rows:
        note_sites(row["entity_id"], row)
    for row in rows:
        site = url_service.registrable_domain(row["source_url"])
        if site and site in own_sites.get(row["entity_id"], set()):
            from_elsewhere.add(row["entity_id"])  # the organisation's own page
        elif row["category"] in list_categories:
            from_listing.add(row["entity_id"])
        else:
            from_elsewhere.add(row["entity_id"])
    evidence = {}
    for entity in entities:
        entity_id = entity["entity_id"]
        if entity_id in from_listing and entity_id in from_elsewhere:
            evidence[entity_id] = LISTING_AND_OWN
        elif entity_id in from_listing:
            evidence[entity_id] = LISTING_ONLY
        elif entity_id in from_elsewhere:
            evidence[entity_id] = OWN_PAGE
    return evidence


def export_entities(path, needing_review=False):
    """
    Write every resolved entity to `path` as CSV.

    :param needing_review: write only the rows carrying a review flag.
    :return: number of rows written.
    """
    config = config_service.get_config()
    contact_fields = _contact_fields(config)
    with data_service.get_connection() as connection:
        rows = [dict(r) for r in connection.execute("SELECT * FROM entities ORDER BY entity_id")]
        conflicts = {}
        for row in connection.execute("SELECT entity_id, field FROM entity_conflicts"):
            conflicts.setdefault(row["entity_id"], set()).add(row["field"])
        evidence = _evidence(connection, config, rows)

    fields = [name for name in (rows[0].keys() if rows else []) if name != "entity_id"]
    header = ["entity_id"] + fields + ["evidence", "review"]
    written = 0
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            flags = review_flags(row, contact_fields, conflicts.get(row["entity_id"], set()))
            if needing_review and not flags:
                continue
            record = {name: _as_text(row.get(name)) for name in fields}
            record["entity_id"] = row["entity_id"]
            record["evidence"] = evidence.get(row["entity_id"], "")
            record["review"] = " ".join(flags)
            writer.writerow(record)
            written += 1
    logger.info("Wrote %d entit%s to %s", written, "y" if written == 1 else "ies", path)
    return written
