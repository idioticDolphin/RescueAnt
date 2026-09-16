import sqlite3

import pytest

import model.tools.config_service as config_service
import model.tools.data_service as data_service
from model.objects.category import Category, Relevancy
from model.objects.config import Config


def _make_config(categories):
    return Config(
        categories=categories,
        category_prompt="prompt",
        category_max_tokens=10,
        category_context=2048,
        category_model_id=0,
        politeness=2,
        skip_tags=["script"],
        starting_url_path="starting_urls.csv",
        database_path="crawl.db",
        discover_urls=False,
        results_per_query=10,
        query_politeness=1.0,
        redo_all_fetches=False,
        redo_failed_fetches=True,
    )


def _object_schema(*field_names):
    """Build the same {"type": "object", "properties": {...}} shape
    config_service._build_schema() produces for a non-list category."""
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in field_names},
        "required": list(field_names),
    }


def _list_schema(*field_names):
    """Build the same {"type": "array", "items": {...}} shape
    config_service._wrap_as_list_schema() produces for a list category."""
    return {"type": "array", "items": _object_schema(*field_names)}


@pytest.fixture(autouse=True)
def _isolate_data_service_state(tmp_path, monkeypatch):
    """Point data_service at a throwaway sqlite file and reset db_fields
    around every test so tests can't leak state into one another."""
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(data_service, "db_fields", [])


def _init_with_fields(monkeypatch, *field_names, is_list_category=False):
    category = Category(
        name="STATION",
        relevancy=Relevancy.CONTENT,
        fields=_list_schema(*field_names) if is_list_category else _object_schema(*field_names),
        analysis_prompt="p",
        analysis_max_tokens=1,
        analysis_model_id=0,
        process_links=False,
        is_list_category=is_list_category,
    )
    monkeypatch.setattr(data_service, "config", _make_config([category]))
    data_service.init_db()


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_crawls_and_entries_tables(monkeypatch):
    _init_with_fields(monkeypatch, "name")

    with data_service.get_connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"crawls", "entries"} <= tables


def test_init_db_reads_field_names_from_the_properties_object(monkeypatch):
    """
    Regression guard: Category.fields is a JSON Schema object
    ({"type": "object", "properties": {...}}), not a flat field-name dict -
    init_db() must pull column names from schema["properties"], not from
    the schema's own top-level keys ("type"/"properties"/"required").
    """
    _init_with_fields(monkeypatch, "station_url", "name", "e-mail")

    assert data_service.get_db_fields() == [
        '"station_url" TEXT', '"name" TEXT', '"e-mail" TEXT',
    ]


def test_init_db_reads_field_names_from_list_category_items_properties(monkeypatch):
    """Same regression guard as above, for a list category's
    {"type": "array", "items": {"type": "object", "properties": {...}}} schema."""
    _init_with_fields(monkeypatch, "station_url", "name", is_list_category=True)

    assert data_service.get_db_fields() == ['"station_url" TEXT', '"name" TEXT']


def test_init_db_field_names_match_a_real_config_service_built_schema(monkeypatch):
    """
    End-to-end guard against the schema shape drifting apart from what
    config_service actually produces: build fields via the real
    _build_schema()/_wrap_as_list_schema() helpers instead of hand-rolling
    the expected shape.
    """
    built = config_service._build_schema({"name": "string", "e-mail": "string"}, {})
    wrapped = config_service._wrap_as_list_schema(built)
    category = Category(
        name="LIST", relevancy=Relevancy.CONTENT, fields=wrapped, is_list_category=True,
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    monkeypatch.setattr(data_service, "config", _make_config([category]))

    data_service.init_db()

    assert set(data_service.get_db_fields()) == {'"name" TEXT', '"e-mail" TEXT'}


def test_init_db_ignores_irrelevant_categories(monkeypatch):
    relevant = Category(
        name="STATION", relevancy=Relevancy.CONTENT, fields=_object_schema("name"),
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    irrelevant = Category(name="IRRELEVANT", relevancy=Relevancy.IRRELEVANT)
    monkeypatch.setattr(data_service, "config", _make_config([relevant, irrelevant]))

    data_service.init_db()

    assert data_service.get_db_fields() == ['"name" TEXT']


def test_init_db_ignores_links_only_categories(monkeypatch):
    relevant = Category(
        name="STATION", relevancy=Relevancy.CONTENT, fields=_object_schema("name"),
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    links_only = Category(name="HUB", relevancy=Relevancy.LINKS, process_links=True)
    monkeypatch.setattr(data_service, "config", _make_config([relevant, links_only]))

    data_service.init_db()

    assert data_service.get_db_fields() == ['"name" TEXT']


def test_init_db_deduplicates_fields_shared_across_categories(monkeypatch):
    """Two relevant categories reusing the same field names (e.g. because
    they share the global `fields` config block) must not produce duplicate
    column definitions - that used to be a SQL syntax error."""
    station = Category(
        name="STATION", relevancy=Relevancy.CONTENT, fields=_object_schema("name", "address"),
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    listing = Category(
        name="LIST", relevancy=Relevancy.CONTENT, fields=_list_schema("name", "address"), is_list_category=True,
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    monkeypatch.setattr(data_service, "config", _make_config([station, listing]))

    data_service.init_db()  # must not raise "duplicate column name"

    fields = data_service.get_db_fields()
    assert fields.count('"name" TEXT') == 1
    assert fields.count('"address" TEXT') == 1


def test_init_db_handles_field_names_with_special_characters(monkeypatch):
    # "e-mail" is not a valid bare SQL identifier - unquoted, the hyphen
    # would be parsed as a minus operator and break the CREATE TABLE.
    _init_with_fields(monkeypatch, "e-mail")

    with data_service.get_connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(entries)").fetchall()}
    assert "e-mail" in columns


def test_init_db_is_idempotent(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    data_service.init_db()  # second call should not raise ("IF NOT EXISTS")


def test_init_db_entries_foreign_key_references_crawls(monkeypatch):
    _init_with_fields(monkeypatch, "name")

    with data_service.get_connection() as connection:
        fk_rows = connection.execute("PRAGMA foreign_key_list(entries)").fetchall()
    assert len(fk_rows) == 1
    assert fk_rows[0]["table"] == "crawls"
    assert fk_rows[0]["from"] == "source_crawl_id"
    assert fk_rows[0]["to"] == "crawl_id"


# ---------------------------------------------------------------------------
# save_crawl_instance / save_site_category
# ---------------------------------------------------------------------------

def test_save_crawl_instance_returns_new_crawl_id(monkeypatch):
    _init_with_fields(monkeypatch, "name")

    first_id = data_service.save_crawl_instance("http://example.com/a", 1.0, True)
    second_id = data_service.save_crawl_instance("http://example.com/b", 2.0, False)

    assert second_id == first_id + 1


def test_save_crawl_instance_persists_fields(monkeypatch):
    _init_with_fields(monkeypatch, "name")

    crawl_id = data_service.save_crawl_instance("http://example.com/a", 42.5, True)

    with data_service.get_connection() as connection:
        row = connection.execute("SELECT * FROM crawls WHERE crawl_id = ?", (crawl_id,)).fetchone()
    assert row["source_url"] == "http://example.com/a"
    # crawl_time column has TEXT affinity, so sqlite stores/returns it as a string
    assert row["crawl_time"] == "42.5"
    assert row["fetch_success"] == 1


def test_save_site_category_updates_category(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance("http://example.com/a", 1.0, True)

    data_service.save_site_category(crawl_id, "STATION")

    with data_service.get_connection() as connection:
        row = connection.execute("SELECT category FROM crawls WHERE crawl_id = ?", (crawl_id,)).fetchone()
    assert row["category"] == "STATION"


# ---------------------------------------------------------------------------
# save_extraction
# ---------------------------------------------------------------------------

def test_save_extraction_persists_simple_fields(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance("http://example.com/a", 1.0, True)

    data_service.save_extraction(crawl_id, {"name": "Igel Station Berlin"})

    with data_service.get_connection() as connection:
        row = connection.execute("SELECT * FROM entries WHERE source_crawl_id = ?", (crawl_id,)).fetchone()
    assert row["name"] == "Igel Station Berlin"


def test_save_extraction_handles_field_names_with_special_characters(monkeypatch):
    _init_with_fields(monkeypatch, "e-mail")
    crawl_id = data_service.save_crawl_instance("http://example.com/a", 1.0, True)

    data_service.save_extraction(crawl_id, {"e-mail": "info@example.com"})

    with data_service.get_connection() as connection:
        row = connection.execute("SELECT * FROM entries WHERE source_crawl_id = ?", (crawl_id,)).fetchone()
    assert row["e-mail"] == "info@example.com"


def test_save_extraction_serializes_list_values_as_json(monkeypatch):
    _init_with_fields(monkeypatch, "accepted_animals")
    crawl_id = data_service.save_crawl_instance("http://example.com/a", 1.0, True)

    data_service.save_extraction(crawl_id, {"accepted_animals": ["dog", "cat"]})

    with data_service.get_connection() as connection:
        row = connection.execute("SELECT * FROM entries WHERE source_crawl_id = ?", (crawl_id,)).fetchone()
    assert row["accepted_animals"] == '["dog", "cat"]'


def test_save_extraction_rejects_unknown_source_crawl_id(monkeypatch):
    _init_with_fields(monkeypatch, "name")

    with pytest.raises(sqlite3.IntegrityError):
        data_service.save_extraction(999, {"name": "orphaned entry"})


def test_deleting_crawl_cascades_to_entries(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance("http://example.com/a", 1.0, True)
    data_service.save_extraction(crawl_id, {"name": "Igel Station Berlin"})

    with data_service.get_connection() as connection:
        connection.execute("DELETE FROM crawls WHERE crawl_id = ?", (crawl_id,))
        connection.commit()
        remaining = connection.execute("SELECT * FROM entries").fetchall()
    assert remaining == []


# ---------------------------------------------------------------------------
# get_successful_crawl_urls / get_crawl_urls
# ---------------------------------------------------------------------------

def test_get_crawl_urls_returns_all_urls(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    data_service.save_crawl_instance("http://example.com/a", 1.0, True)
    data_service.save_crawl_instance("http://example.com/b", 2.0, False)

    assert set(data_service.get_crawl_urls()) == {"http://example.com/a", "http://example.com/b"}


def test_get_successful_crawl_urls_filters_failed_fetches(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    data_service.save_crawl_instance("http://example.com/a", 1.0, True)
    data_service.save_crawl_instance("http://example.com/b", 2.0, False)

    assert len(data_service.get_crawl_urls()) == 2
    assert data_service.get_successful_crawl_urls() == ["http://example.com/a"]


@pytest.fixture
def temp_db(monkeypatch):
    """An initialized throwaway database with a single 'name' column, for the
    lifecycle-state tests below."""
    _init_with_fields(monkeypatch, "name")
    return data_service.DATABASE_PATH


# ---------------------------------------------------------------------------
# page lifecycle states and resumption (P23/P24/P25)
# ---------------------------------------------------------------------------

def test_new_crawl_defaults_to_fetched_state(temp_db):
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    rows = data_service.get_pending_crawls()
    assert [r["crawl_id"] for r in rows] == [crawl_id]
    assert rows[0]["state"] == data_service.STATE_FETCHED


def test_failed_fetch_is_not_pending_work(temp_db):
    data_service.save_crawl_instance("http://a.com/", 1.0, False)
    assert data_service.get_pending_crawls() == []


def test_crawl_stores_content_reference(temp_db):
    data_service.save_crawl_instance("http://a.com/", 1.0, True,
                                     content_path="ab/cd/x.html.gz",
                                     content_sha256="abc", site="a.com")
    row = data_service.get_pending_crawls()[0]
    assert row["content_path"] == "ab/cd/x.html.gz"
    assert row["site"] == "a.com"


def test_set_crawl_state_advances_lifecycle(temp_db):
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    data_service.set_crawl_state(crawl_id, data_service.STATE_CATEGORIZED)
    assert data_service.get_pending_crawls()[0]["state"] == data_service.STATE_CATEGORIZED


def test_extracted_pages_are_no_longer_pending(temp_db):
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    data_service.set_crawl_state(crawl_id, data_service.STATE_EXTRACTED)
    assert data_service.get_pending_crawls() == []


def test_set_crawl_state_with_error_increments_attempts(temp_db):
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    data_service.set_crawl_state(crawl_id, data_service.STATE_CATEGORIZED, last_error="boom")
    with data_service.get_connection() as c:
        row = c.execute("SELECT attempt_count, last_error FROM crawls WHERE crawl_id=?",
                        (crawl_id,)).fetchone()
    assert row["attempt_count"] == 1 and row["last_error"] == "boom"


def test_finished_urls_exclude_unprocessed_pages(temp_db):
    """The F7 bug: a fetched-but-unprocessed page must NOT count as done, or
    it is skipped forever on the next run."""
    data_service.save_crawl_instance("http://pending.com/", 1.0, True)
    done = data_service.save_crawl_instance("http://done.com/", 1.0, True)
    data_service.set_crawl_state(done, data_service.STATE_EXTRACTED)

    finished = data_service.get_finished_crawl_urls()

    assert "http://done.com/" in finished
    assert "http://pending.com/" not in finished


def test_delete_entries_makes_reextraction_idempotent(temp_db):
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    data_service.save_extraction(crawl_id, {"name": "first"})
    data_service.delete_entries_for_crawl(crawl_id)
    data_service.save_extraction(crawl_id, {"name": "second"})

    with data_service.get_connection() as c:
        rows = c.execute("SELECT name FROM entries WHERE source_crawl_id=?", (crawl_id,)).fetchall()
    assert [r["name"] for r in rows] == ["second"]


def test_count_by_state_reports_progress(temp_db):
    a = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    data_service.save_crawl_instance("http://b.com/", 1.0, True)
    data_service.set_crawl_state(a, data_service.STATE_EXTRACTED)

    counts = data_service.count_by_state()

    assert counts[data_service.STATE_EXTRACTED] == 1
    assert counts[data_service.STATE_FETCHED] == 1


def test_migration_backfills_state_for_legacy_rows(temp_db):
    """A database written before states existed must still resume sensibly."""
    with data_service.get_connection() as c:
        c.execute("UPDATE crawls SET state = NULL")
        c.commit()
    # simulate a legacy row: fetched successfully, never categorized
    crawl_id = data_service.save_crawl_instance("http://legacy.com/", 1.0, True)
    with data_service.get_connection() as c:
        c.execute("UPDATE crawls SET state = NULL, category = NULL WHERE crawl_id=?", (crawl_id,))
        c.commit()

    data_service.init_db()   # runs the migration

    with data_service.get_connection() as c:
        state = c.execute("SELECT state FROM crawls WHERE crawl_id=?", (crawl_id,)).fetchone()["state"]
    assert state == data_service.STATE_FETCH_FAILED   # retryable, not silently dropped


def test_skip_pending_over_budget_abandons_only_oversized_sites(temp_db):
    for i in range(5):
        cid = data_service.save_crawl_instance(f"http://big.com/{i}", 1.0, True, site="big.com")
    small = data_service.save_crawl_instance("http://small.com/1", 1.0, True, site="small.com")

    abandoned = data_service.skip_pending_over_budget(3)

    assert abandoned == 5
    remaining = {r["source_url"] for r in data_service.get_pending_crawls()}
    assert remaining == {"http://small.com/1"}


def test_skip_pending_over_budget_is_a_noop_when_unset(temp_db):
    data_service.save_crawl_instance("http://big.com/1", 1.0, True, site="big.com")
    assert data_service.skip_pending_over_budget(0) == 0
    assert len(data_service.get_pending_crawls()) == 1


def test_abandoned_pages_are_terminal_not_deleted(temp_db):
    for i in range(3):
        data_service.save_crawl_instance(f"http://big.com/{i}", 1.0, True, site="big.com")
    data_service.skip_pending_over_budget(1)

    counts = data_service.count_by_state()
    assert counts[data_service.STATE_SKIPPED] == 3
    assert "http://big.com/0" in data_service.get_finished_crawl_urls()
