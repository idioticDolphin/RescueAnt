import sqlite3

import pytest

import model.tools.config_service as config_service
import model.tools.data_service as data_service
from model.objects.category import Category
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
        is_relevant=True,
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
        name="LIST", is_relevant=True, fields=wrapped, is_list_category=True,
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    monkeypatch.setattr(data_service, "config", _make_config([category]))

    data_service.init_db()

    assert set(data_service.get_db_fields()) == {'"name" TEXT', '"e-mail" TEXT'}


def test_init_db_ignores_irrelevant_categories(monkeypatch):
    relevant = Category(
        name="STATION", is_relevant=True, fields=_object_schema("name"),
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    irrelevant = Category(name="IRRELEVANT", is_relevant=False)
    monkeypatch.setattr(data_service, "config", _make_config([relevant, irrelevant]))

    data_service.init_db()

    assert data_service.get_db_fields() == ['"name" TEXT']


def test_init_db_deduplicates_fields_shared_across_categories(monkeypatch):
    """Two relevant categories reusing the same field names (e.g. because
    they share the global `fields` config block) must not produce duplicate
    column definitions - that used to be a SQL syntax error."""
    station = Category(
        name="STATION", is_relevant=True, fields=_object_schema("name", "address"),
        analysis_prompt="p", analysis_max_tokens=1, analysis_model_id=0, process_links=False,
    )
    listing = Category(
        name="LIST", is_relevant=True, fields=_list_schema("name", "address"), is_list_category=True,
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
