import sqlite3
from contextlib import contextmanager
from pathlib import Path
import model.tools.config_service as config_service
import json

config = config_service.get_config()
DATABASE_PATH = Path(config.get_database_path())
db_fields = []

# Page lifecycle states. A page not in a TERMINAL state is unfinished work
# that a later run must pick up (see get_pending_crawls / orchestrator.resume).
STATE_FETCHED = "FETCHED"            # content stored, not yet categorized
STATE_CATEGORIZED = "CATEGORIZED"    # category known, not yet extracted
STATE_EXTRACTED = "EXTRACTED"        # fully processed
STATE_FETCH_FAILED = "FETCH_FAILED"  # retryable depending on redo_failed_fetches
STATE_SKIPPED = "SKIPPED"            # robots.txt / duplicate - deliberately not processed
STATE_FAILED = "FAILED_PERMANENT"    # gave up after repeated failures

TERMINAL_STATES = (STATE_EXTRACTED, STATE_SKIPPED, STATE_FAILED)

# Playwright errors run to hundreds of lines of stack. The column is for
# auditing why fetches failed, not for storing tracebacks.
MAX_ERROR_CHARS = 300
RESUMABLE_STATES = (STATE_FETCHED, STATE_CATEGORIZED)


@contextmanager
def get_connection():
    """Yield a sqlite3 connection to the crawl database, with row access by
    column name and foreign-key enforcement enabled. Rolls back on error and
    always closes the connection when the block exits.

    WAL journalling is enabled so that a commit survives an abrupt process
    kill (or a machine reboot) - the crawler is expected to be interruptible
    at any moment, and per-page commits are only durable if the journal is."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON') # Enforce foreign keys on inserts
    connection.execute('PRAGMA journal_mode = WAL')
    connection.execute('PRAGMA synchronous = FULL')
    try:
        yield connection
    except Exception:
        connection.rollback() # undo changes on failure
        raise
    finally:
        connection.close() # always close connection afterwards

def _quote_identifier(name:str) -> str:
    return '"' + name.replace('"', '""') + '"'

def _schema_field_names(schema: dict) -> list[str]:
    """
    Return the field names of a Category.fields JSON Schema, as built by
    config_service._build_schema()/_wrap_as_list_schema(): a plain
    {"type": "object", "properties": {...}} for a single-entry category, or
    {"type": "array", "items": {"type": "object", "properties": {...}}} for
    a list category.
    """
    if schema.get("type") == "array":
        schema = schema.get("items", {})
    return list(schema.get("properties", {}).keys())

def init_db():
    """
    Create the crawls/entries tables if they don't exist yet, deriving
    the entries columns from every relevant category's fields (deduplicated
    across categories that share the same field names). Safe to call
    repeatedly - existing tables are left untouched.
    """
    categories = config.get_categories()
    global db_fields
    seen_fields = set()
    db_fields = []
    for category in categories:
        if category.is_relevant:
            for field in _schema_field_names(category.fields):
                if field not in seen_fields:
                    seen_fields.add(field)
                    db_fields.append(f"{_quote_identifier(field)} TEXT")

    db_fields_string = ",".join(db_fields) # Careful. Fine for reading from local config, but deadly sql injection risk when exposing configuration to outsiders

    with get_connection() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS crawls (
            crawl_id INTEGER PRIMARY KEY AUTOINCREMENT,
            crawl_time TEXT NOT NULL,
            source_url TEXT NOT NULL,
            category TEXT,
            fetch_success BOOLEAN NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_crawl_id INTEGER NOT NULL,
            """ + db_fields_string + (", " if db_fields_string else "") + """
            FOREIGN KEY (source_crawl_id) REFERENCES crawls(crawl_id) ON DELETE CASCADE
        );
        """)
        _migrate(connection)


# Columns added after the original schema. Applied with ALTER TABLE so that
# databases from earlier runs keep working and keep their data.
_CRAWL_MIGRATIONS = {
    "state": "TEXT",
    "content_path": "TEXT",
    "content_sha256": "TEXT",
    "site": "TEXT",
    "attempt_count": "INTEGER DEFAULT 0",
    "last_error": "TEXT",
    # The category a page was first classified as, when extraction later
    # judged it mislabeled and it was re-filed. Kept rather than overwritten:
    # it is the record of where the classifier goes wrong.
    "reclassified_from": "TEXT",
}


def _migrate(connection):
    """Add any missing crawls columns, then backfill state for legacy rows."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(crawls)")}
    for column, definition in _CRAWL_MIGRATIONS.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE crawls ADD COLUMN {column} {definition}")
    # Rows written before states existed: infer where they got to, so an old
    # database resumes sensibly instead of looking entirely unfinished. Scoped
    # to NULL states, so this is idempotent and safe to run on every startup.
    connection.execute(f"""
            UPDATE crawls SET state = CASE
                WHEN fetch_success = 0 THEN '{STATE_FETCH_FAILED}'
                WHEN category IS NULL OR category = '' THEN '{STATE_FETCH_FAILED}'
                ELSE '{STATE_EXTRACTED}'
            END
            WHERE state IS NULL
        """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_crawls_state ON crawls(state)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_crawls_site ON crawls(site)")
    connection.commit()

def init_entity_tables():
    """
    Create the deduplicated-entity tables, mirroring the entries columns.

    Kept separate from `entries` deliberately: entries stay an immutable log
    of what was observed on which page, while `entities` holds the resolved,
    fused view. Re-running resolution therefore never destroys evidence, and
    conflicting values are recorded rather than silently discarded.
    """
    columns = ",".join(db_fields)
    with get_connection() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            """ + columns + (", " if columns else "") + """
            confidence REAL,
            n_sources INTEGER
        );
        CREATE TABLE IF NOT EXISTS entity_sources (
            entity_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            PRIMARY KEY (entity_id, entry_id),
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS entity_conflicts (
            entity_id INTEGER NOT NULL,
            field TEXT NOT NULL,
            value TEXT,
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        );
        """)
        connection.commit()


def replace_entities(resolved):
    """
    Replace the entity layer with a freshly resolved set.

    :param resolved: iterable of (fused_record, source_entry_ids). Fused
                      records may carry _confidence/_n_sources/_conflicts keys,
                      which are stored in their own columns/tables.
    """
    known = {definition.split()[0].strip('"') for definition in db_fields}
    with get_connection() as connection:
        connection.execute("DELETE FROM entity_conflicts")
        connection.execute("DELETE FROM entity_sources")
        connection.execute("DELETE FROM entities")
        for record, entry_ids in resolved:
            payload = {k: v for k, v in record.items()
                       if not k.startswith("_") and k in known}
            names = list(payload.keys())
            column_sql = ", ".join(_quote_identifier(n) for n in names)
            values = tuple(_to_sql_value(payload[n]) for n in names)
            columns = (column_sql + ", " if column_sql else "") + "confidence, n_sources"
            placeholders = ", ".join(["?"] * (len(values) + 2))
            cursor = connection.execute(
                f"INSERT INTO entities({columns}) VALUES ({placeholders})",
                values + (record.get("_confidence"), record.get("_n_sources")))
            entity_id = cursor.lastrowid
            connection.executemany(
                "INSERT OR IGNORE INTO entity_sources(entity_id, entry_id) VALUES (?, ?)",
                [(entity_id, entry_id) for entry_id in entry_ids])
            connection.executemany(
                "INSERT INTO entity_conflicts(entity_id, field, value) VALUES (?, ?, ?)",
                [(entity_id, c["field"], _to_sql_value(c["value"]))
                 for c in record.get("_conflicts", [])])
        connection.commit()


def get_entries_with_source():
    """Return every entry joined to the URL and category of the page it came from."""
    with get_connection() as connection:
        return [dict(row) for row in connection.execute("""
            SELECT e.*, c.source_url AS _source_url, c.category AS _category
            FROM entries e JOIN crawls c ON e.source_crawl_id = c.crawl_id
        """)]


def get_db_fields():
    """Return the entries-table column definitions computed by the last init_db() call."""
    return db_fields

def save_crawl_instance(url:str, crawl_time:float, fetch_success:bool,
                        state:str=None, content_path:str=None,
                        content_sha256:str=None, site:str=None,
                        last_error:str=None) -> int:
    """
    Insert a new crawls row and return its generated crawl_id.

    :param state: lifecycle state to record (see STATE_* constants). Defaults
                   to FETCHED/FETCH_FAILED based on fetch_success, so a page
                   is never recorded as finished before it has been processed.
    :param content_path: page_store path of the stored body, if any
    :param last_error: why the fetch failed, if it did. Auditing a run's
                        failures otherwise means inferring the reason from
                        hostnames, and the reason is known at the point of
                        failure.
    """
    if state is None:
        state = STATE_FETCHED if fetch_success else STATE_FETCH_FAILED
    if last_error:
        last_error = str(last_error)[:MAX_ERROR_CHARS]
    with get_connection() as connection:
        cursor = connection.execute(
            """INSERT INTO crawls(crawl_time, source_url, fetch_success, state,
                                  content_path, content_sha256, site, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (crawl_time, url, fetch_success, state, content_path, content_sha256,
             site, last_error or None)
        )
        crawl_id = cursor.lastrowid
        connection.commit()
    return crawl_id


def set_crawl_state(crawl_id:int, state:str, last_error:str=None):
    """Move a crawl row to a new lifecycle state, committing immediately.

    Committing per transition (rather than per round) is what makes the crawl
    resumable: the database always reflects exactly how far each page got."""
    with get_connection() as connection:
        if last_error is None:
            connection.execute("UPDATE crawls SET state = ? WHERE crawl_id = ?",
                               (state, crawl_id))
        else:
            connection.execute(
                "UPDATE crawls SET state = ?, last_error = ?, "
                "attempt_count = COALESCE(attempt_count, 0) + 1 WHERE crawl_id = ?",
                (state, last_error, crawl_id))
        connection.commit()


def get_pending_crawls(states=RESUMABLE_STATES):
    """
    Return unfinished crawl rows (as dicts), oldest first.

    This is the whole of the resume mechanism: work that was fetched or
    categorized but never extracted is simply still in a non-terminal state,
    and a later run picks it up from exactly there.
    """
    placeholders = ", ".join("?" for _ in states)
    with get_connection() as connection:
        rows = connection.execute(
            f"""SELECT crawl_id, source_url, category, content_path, state, site
                FROM crawls WHERE state IN ({placeholders})
                ORDER BY crawl_id""", tuple(states)
        ).fetchall()
    return [dict(row) for row in rows]


def delete_entries_for_crawl(crawl_id:int):
    """Remove any entries previously extracted from a crawl.

    Called before re-extracting a page so that reprocessing is idempotent -
    a page processed twice must not produce duplicate rows."""
    with get_connection() as connection:
        connection.execute("DELETE FROM entries WHERE source_crawl_id = ?", (crawl_id,))
        connection.commit()


def find_analysed_twin(crawl_id:int):
    """
    Return (crawl_id, category) of an already-analysed page whose stored
    content is byte-identical to this one's, or None.

    Mirror URLs are common - /links and /index.php/links serve the same bytes -
    and analysing the second copy costs a classification and an extraction to
    reach the answer already on record.
    """
    with get_connection() as connection:
        row = connection.execute(
            """SELECT twin.crawl_id, twin.category FROM crawls AS page
               JOIN crawls AS twin ON twin.content_sha256 = page.content_sha256
               WHERE page.crawl_id = ? AND twin.crawl_id != page.crawl_id
                 AND page.content_sha256 IS NOT NULL
                 AND twin.category IS NOT NULL AND twin.state = ?
               ORDER BY twin.crawl_id LIMIT 1""",
            (crawl_id, STATE_EXTRACTED)).fetchone()
    return (row["crawl_id"], row["category"]) if row else None


def get_finished_crawl_urls():
    """Return the URL of every crawl that needs no further fetching:
    successfully processed pages plus deliberately skipped ones."""
    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    with get_connection() as connection:
        return [row["source_url"] for row in connection.execute(
            f"SELECT source_url FROM crawls WHERE state IN ({placeholders})",
            TERMINAL_STATES
        ).fetchall()]


def count_by_state():
    """Return {state: count} across the crawls table (for progress reporting)."""
    with get_connection() as connection:
        return {row["state"]: row["n"] for row in connection.execute(
            "SELECT state, COUNT(*) AS n FROM crawls GROUP BY state")}

def save_site_category(crawl_id:int, category_name:str):
    """Set the category of an existing crawls row."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE crawls SET category = ? WHERE crawl_id = ?",
            (category_name, crawl_id)
        )
        connection.commit()

def save_extraction(crawl_id:int, extracted_fields):
    """
    Insert one entries row for a crawl, linking it via source_crawl_id.
    List/dict field values are JSON-serialized before being stored.

    :param crawl_id: the crawls row this extraction belongs to
    :param extracted_fields: dict of field name -> extracted value, matching
                              the columns created by init_db() for the site's category
    """
    with get_connection() as connection:
        fields = list(extracted_fields.keys())
        field_string = ", ".join(_quote_identifier(field) for field in fields)
        field_values = tuple(_to_sql_value(extracted_fields[field]) for field in fields)
        parameters = (crawl_id, ) + field_values
        placeholders = ", ".join(["?"] * len(parameters))
        columns = "source_crawl_id" + (f", {field_string}" if field_string else "")
        connection.execute(
            f"INSERT INTO entries({columns}) VALUES ({placeholders})",
            parameters
        )
        connection.commit()

def _to_sql_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value

def get_successful_crawl_urls():
    """Return the source_url of every crawl that fetched successfully."""
    with get_connection() as connection:
        return [row["source_url"] for row in connection.execute(
            "SELECT source_url FROM crawls WHERE fetch_success"
        ).fetchall()]

def get_crawl_urls():
    """Return the source_url of every recorded crawl, regardless of fetch outcome."""
    with get_connection() as connection:
        return [row["source_url"] for row in connection.execute(
            "SELECT source_url FROM crawls"
        ).fetchall()]

def reset_states_for_reprocess(target_state:str, site:str=None, category:str=None):
    """
    Move already-processed pages back to an earlier lifecycle state so they
    are picked up again by resume_pending().

    Only pages whose stored content is still referenced are eligible - there
    is nothing to reprocess without a body. Returns the number of rows reset.

    :param category: limit to pages currently in this category. After a prompt
                      change that moves one boundary, only the pages on the
                      wrong side of it need re-classifying; re-running the whole
                      corpus costs hours and re-answers questions already right.
    """
    clauses = ["state = ?", "content_path IS NOT NULL"]
    params = [STATE_EXTRACTED]
    # Going back to FETCHED means "classify this again", so the stored answer
    # has to go with it. resume_pending() reuses a stored category whenever
    # that category still exists in the config, so resetting only the state
    # left the old answer in place - 818 pages reset, none re-categorized,
    # because every old category name survived into the new taxonomy.
    assignment = "state = ?"
    if target_state == STATE_FETCHED:
        # FETCHED is included deliberately: a page can sit in FETCHED while
        # still carrying a category, which is what an interrupted reprocess
        # leaves behind. Skipping those left them holding a stale label, and
        # resume_pending() then extracted them under it instead of
        # classifying them again - 101 extractions at 37s each, all wasted.
        clauses[0] = "state IN (?, ?, ?)"
        params = [STATE_EXTRACTED, STATE_CATEGORIZED, STATE_FETCHED]
        assignment = "state = ?, category = NULL"
    if site:
        clauses.append("site = ?")
        params.append(site)
    if category:
        clauses.append("category = ?")
        params.append(category)
    with get_connection() as connection:
        cursor = connection.execute(
            f"UPDATE crawls SET {assignment} WHERE {' AND '.join(clauses)}",
            [target_state] + params)
        connection.commit()
        return cursor.rowcount


def reclassify_page(crawl_id:int, category:str, from_category:str=None):
    """Re-file a page under another category, remembering the original.

    Used when extraction judges a page not to be what the classifier said.
    The original classification is kept in reclassified_from rather than lost,
    because which categories get overruled, and how often, is the evidence
    for where the taxonomy or its prompt needs work."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE crawls SET category = ?, reclassified_from = ? WHERE crawl_id = ?",
            (category, from_category, crawl_id))
        connection.commit()


def count_reclassifications() -> dict:
    """How many pages extraction overruled, keyed by original category."""
    with get_connection() as connection:
        return {row["reclassified_from"]: row["n"] for row in connection.execute(
            """SELECT reclassified_from, COUNT(*) AS n FROM crawls
               WHERE reclassified_from IS NOT NULL
               GROUP BY reclassified_from""")}


def count_extracted_pages_for_site(site:str) -> int:
    """How many pages of this site have already yielded at least one record.

    Backs the per-site extraction budget. Counted from the database rather
    than from memory so the budget survives a restart, and counting only
    pages that actually produced something means a page that yielded nothing
    has not spent any of it."""
    if not site:
        return 0
    with get_connection() as connection:
        return connection.execute(
            """SELECT COUNT(DISTINCT c.crawl_id) FROM crawls c
               JOIN entries e ON e.source_crawl_id = c.crawl_id
               WHERE c.site = ?""", (site,)).fetchone()[0]


def get_all_content_paths():
    """Return (source_url, content_path) for every page whose body was stored.

    Lets a prompt or taxonomy change be replayed against the exact pages that
    motivated it, offline and without re-crawling."""
    with get_connection() as connection:
        return [(row["source_url"], row["content_path"]) for row in connection.execute(
            """SELECT source_url, content_path FROM crawls
               WHERE content_path IS NOT NULL ORDER BY crawl_id""").fetchall()]


def get_site_content_paths(site:str, limit:int=8):
    """Return stored-content paths for pages of one site, newest first.

    Used to learn a site's recurring template (see boilerplate_service)."""
    if not site:
        return []
    with get_connection() as connection:
        return [row["content_path"] for row in connection.execute(
            """SELECT content_path FROM crawls
               WHERE site = ? AND content_path IS NOT NULL
               ORDER BY crawl_id DESC LIMIT ?""", (site, limit)).fetchall()]


def skip_pending_over_budget(max_pages_per_site:int):
    """
    Abandon unprocessed pages belonging to sites that have already exceeded
    the per-site page budget.

    A crawl that follows links indiscriminately can sink most of its budget
    into one large site - an observed run spent 424 of 527 fetched pages on a
    single pet-supplies retailer, for one usable record. The budget stops
    *queueing* more such pages; this stops *analysing* the ones already
    fetched, which is where the real cost is.

    Pages are marked SKIPPED (terminal) rather than deleted, so the evidence
    of what was crawled and abandoned is preserved.

    :return: number of pages abandoned.
    """
    if not max_pages_per_site:
        return 0
    placeholders = ", ".join("?" for _ in RESUMABLE_STATES)
    with get_connection() as connection:
        cursor = connection.execute(
            f"""UPDATE crawls SET state = ?, last_error = 'site over page budget'
                WHERE state IN ({placeholders})
                  AND site IN (SELECT site FROM crawls WHERE site IS NOT NULL
                               GROUP BY site HAVING COUNT(*) > ?)""",
            (STATE_SKIPPED, *RESUMABLE_STATES, max_pages_per_site))
        connection.commit()
        return cursor.rowcount
