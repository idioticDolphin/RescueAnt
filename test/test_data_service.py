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


def test_get_all_content_paths_returns_url_and_path_pairs(monkeypatch):
    """Offline replay (re-categorising or re-extracting stored pages without
    re-crawling) needs the whole store addressed by URL, not one site at a
    time."""
    _init_with_fields(monkeypatch, "name")
    data_service.save_crawl_instance(
        "https://a.example/one", 0.0, True, content_path="aa/bb/one.html.gz",
        content_sha256="aa", site="a.example")
    data_service.save_crawl_instance(
        "https://b.example/two", 0.0, True, content_path="cc/dd/two.html.gz",
        content_sha256="cc", site="b.example")
    # a page that was never stored (e.g. the fetch failed) must not appear
    data_service.save_crawl_instance("https://c.example/none", 0.0, False)

    pairs = data_service.get_all_content_paths()
    assert ("https://a.example/one", "aa/bb/one.html.gz") in pairs
    assert ("https://b.example/two", "cc/dd/two.html.gz") in pairs
    assert all(p for _, p in pairs)
    assert len(pairs) == 2


def test_count_extracted_records_for_site_counts_pages_that_yielded_records(monkeypatch):
    """The per-site extraction budget needs to know how many times a site has
    already been mined, and that has to survive a restart - so it is counted
    from the database, not from memory."""
    _init_with_fields(monkeypatch, "name")
    a1 = data_service.save_crawl_instance("https://a.example/1", 0.0, True, site="a.example")
    a2 = data_service.save_crawl_instance("https://a.example/2", 0.0, True, site="a.example")
    b1 = data_service.save_crawl_instance("https://b.example/1", 0.0, True, site="b.example")
    data_service.save_extraction(a1, {"name": "A"})
    data_service.save_extraction(a2, {"name": "A"})
    data_service.save_extraction(b1, {"name": "B"})

    assert data_service.count_extracted_pages_for_site("a.example") == 2
    assert data_service.count_extracted_pages_for_site("b.example") == 1
    assert data_service.count_extracted_pages_for_site("c.example") == 0


def test_count_extracted_pages_ignores_pages_that_yielded_nothing(monkeypatch):
    """A page that produced no record has not used up any of the budget."""
    _init_with_fields(monkeypatch, "name")
    data_service.save_crawl_instance("https://a.example/empty", 0.0, True, site="a.example")

    assert data_service.count_extracted_pages_for_site("a.example") == 0


def test_count_extracted_pages_for_a_missing_site_is_zero(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    assert data_service.count_extracted_pages_for_site(None) == 0
    assert data_service.count_extracted_pages_for_site("") == 0


def test_resetting_to_fetched_also_clears_the_stored_category(monkeypatch):
    """`--reprocess categorize` has to actually re-categorize.

    resume_pending() reuses a stored category whenever that category still
    exists in the config, so resetting only the lifecycle state left the old
    answer in place: 818 pages were reset and none was re-categorized, because
    every old category name survived into the new taxonomy.
    """
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance(
        "https://a.example/1", 0.0, True, content_path="a/b/c.gz",
        content_sha256="x", site="a.example")
    data_service.save_site_category(crawl_id, "STATION")
    data_service.set_crawl_state(crawl_id, data_service.STATE_EXTRACTED)

    data_service.reset_states_for_reprocess(data_service.STATE_FETCHED)

    with data_service.get_connection() as c:
        row = c.execute("SELECT state, category FROM crawls WHERE crawl_id = ?",
                        (crawl_id,)).fetchone()
    assert row["state"] == data_service.STATE_FETCHED
    assert row["category"] is None


def test_resetting_to_categorized_keeps_the_category(monkeypatch):
    """Re-extracting must not throw away a classification that is still good -
    that is the whole point of the cheaper stage."""
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance(
        "https://a.example/1", 0.0, True, content_path="a/b/c.gz",
        content_sha256="x", site="a.example")
    data_service.save_site_category(crawl_id, "STATION")
    data_service.set_crawl_state(crawl_id, data_service.STATE_EXTRACTED)

    data_service.reset_states_for_reprocess(data_service.STATE_CATEGORIZED)

    with data_service.get_connection() as c:
        row = c.execute("SELECT state, category FROM crawls WHERE crawl_id = ?",
                        (crawl_id,)).fetchone()
    assert row["state"] == data_service.STATE_CATEGORIZED
    assert row["category"] == "STATION"


def test_resetting_to_fetched_also_reclaims_pages_already_in_fetched(monkeypatch):
    """A page can sit in FETCHED *carrying* a category - that is what an
    interrupted reprocess leaves behind. Resetting only EXTRACTED and
    CATEGORIZED rows left those holding a stale label, and resume_pending()
    then extracted them under it rather than classifying them again."""
    _init_with_fields(monkeypatch, "name")
    stale = data_service.save_crawl_instance(
        "https://a.example/stale", 0.0, True, content_path="a/b/c.gz",
        content_sha256="x", site="a.example")
    data_service.save_site_category(stale, "STATION")
    data_service.set_crawl_state(stale, data_service.STATE_FETCHED)

    data_service.reset_states_for_reprocess(data_service.STATE_FETCHED)

    with data_service.get_connection() as c:
        row = c.execute("SELECT state, category FROM crawls WHERE crawl_id = ?",
                        (stale,)).fetchone()
    assert row["category"] is None
    assert row["state"] == data_service.STATE_FETCHED


def test_resetting_leaves_pages_without_stored_content_alone(monkeypatch):
    """There is nothing to reprocess without a body; a failed fetch must stay
    a failed fetch so it is retried rather than silently marked ready."""
    _init_with_fields(monkeypatch, "name")
    failed = data_service.save_crawl_instance("https://a.example/gone", 0.0, False)

    data_service.reset_states_for_reprocess(data_service.STATE_FETCHED)

    with data_service.get_connection() as c:
        row = c.execute("SELECT state FROM crawls WHERE crawl_id = ?",
                        (failed,)).fetchone()
    assert row["state"] == data_service.STATE_FETCH_FAILED


def test_reprocess_can_be_limited_to_one_category(monkeypatch):
    """After a prompt change that moves one boundary, only the pages sitting
    on the wrong side of it need re-classifying. Re-running the whole corpus
    costs hours and re-answers questions that were already right."""
    _init_with_fields(monkeypatch, "name")
    advice = data_service.save_crawl_instance(
        "https://a.example/1", 0.0, True, content_path="a/1.gz",
        content_sha256="x", site="a.example")
    station = data_service.save_crawl_instance(
        "https://b.example/1", 0.0, True, content_path="b/1.gz",
        content_sha256="y", site="b.example")
    for crawl_id, name in ((advice, "ADVICE"), (station, "STATION")):
        data_service.save_site_category(crawl_id, name)
        data_service.set_crawl_state(crawl_id, data_service.STATE_EXTRACTED)

    reset = data_service.reset_states_for_reprocess(
        data_service.STATE_FETCHED, category="ADVICE")

    assert reset == 1
    with data_service.get_connection() as c:
        rows = {r["crawl_id"]: dict(r) for r in
                c.execute("SELECT crawl_id, state, category FROM crawls")}
    assert rows[advice]["category"] is None
    assert rows[advice]["state"] == data_service.STATE_FETCHED
    # the category that was not named is untouched
    assert rows[station]["category"] == "STATION"
    assert rows[station]["state"] == data_service.STATE_EXTRACTED


def test_reprocess_without_a_category_filter_still_takes_everything(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    for i, name in enumerate(("ADVICE", "STATION")):
        crawl_id = data_service.save_crawl_instance(
            f"https://x{i}.example/", 0.0, True, content_path=f"x{i}.gz",
            content_sha256=str(i), site=f"x{i}.example")
        data_service.save_site_category(crawl_id, name)
        data_service.set_crawl_state(crawl_id, data_service.STATE_EXTRACTED)

    assert data_service.reset_states_for_reprocess(data_service.STATE_FETCHED) == 2


def test_a_failed_fetch_records_why_it_failed(monkeypatch):
    """Auditing 131 failed fetches meant inferring the reason from hostnames,
    because last_error was NULL on every one of them. The reason is known at
    the point of failure and costs nothing to keep."""
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance(
        "https://a.example/blocked", 0.0, False,
        last_error="disallowed by robots.txt")

    with data_service.get_connection() as c:
        row = c.execute("SELECT state, last_error FROM crawls WHERE crawl_id = ?",
                        (crawl_id,)).fetchone()
    assert row["state"] == data_service.STATE_FETCH_FAILED
    assert row["last_error"] == "disallowed by robots.txt"


def test_a_successful_fetch_records_no_error(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance("https://a.example/ok", 0.0, True)

    with data_service.get_connection() as c:
        row = c.execute("SELECT last_error FROM crawls WHERE crawl_id = ?",
                        (crawl_id,)).fetchone()
    assert row["last_error"] is None


def test_a_long_failure_reason_is_truncated(monkeypatch):
    """Playwright errors run to hundreds of lines of stack; the database is
    for auditing, not for storing tracebacks."""
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance(
        "https://a.example/x", 0.0, False, last_error="e" * 5000)

    with data_service.get_connection() as c:
        row = c.execute("SELECT last_error FROM crawls WHERE crawl_id = ?",
                        (crawl_id,)).fetchone()
    assert len(row["last_error"]) <= data_service.MAX_ERROR_CHARS


def test_a_page_judged_mislabeled_keeps_what_it_was_classified_as(monkeypatch):
    """Re-filing a page must not destroy the evidence of the mistake: which
    category the classifier chose, against the extractor's verdict, is exactly
    the confusion data needed to decide where the taxonomy needs work."""
    _init_with_fields(monkeypatch, "name")
    crawl_id = data_service.save_crawl_instance("https://a.example/", 0.0, True, site="a.example")
    data_service.save_site_category(crawl_id, "STATION")

    data_service.reclassify_page(crawl_id, "HUB", from_category="STATION")

    with data_service.get_connection() as c:
        row = c.execute("SELECT category, reclassified_from FROM crawls WHERE crawl_id = ?",
                        (crawl_id,)).fetchone()
    assert row["category"] == "HUB"
    assert row["reclassified_from"] == "STATION"


def test_reclassification_counts_by_original_category(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    for i, original in enumerate(("STATION", "STATION", "LIST")):
        cid = data_service.save_crawl_instance(f"https://x{i}.example/", 0.0, True)
        data_service.save_site_category(cid, original)
        data_service.reclassify_page(cid, "HUB", from_category=original)

    assert data_service.count_reclassifications() == {"STATION": 2, "LIST": 1}


def test_get_record_urls_returns_every_stored_website(monkeypatch):
    _init_with_fields(monkeypatch, "name", "station_url")

    def crawl(url):
        return data_service.save_crawl_instance(url, 1.0, True, site="x.de")

    data_service.save_extraction(crawl("http://x.de/1"), {"name": "A", "station_url": "https://a.de/"})
    data_service.save_extraction(crawl("http://x.de/2"),
                                 {"name": "B", "station_url": ["https://b.de/", "https://b.de/kontakt"]})
    data_service.save_extraction(crawl("http://x.de/3"), {"name": "C"})

    assert sorted(data_service.get_record_urls("station_url")) == [
        "https://a.de/", "https://b.de/", "https://b.de/kontakt"]


def test_get_crawled_sites_lists_each_domain_once(monkeypatch):
    _init_with_fields(monkeypatch, "name")
    data_service.save_crawl_instance("https://a.de/one", 1.0, True, site="a.de")
    data_service.save_crawl_instance("https://a.de/two", 1.0, True, site="a.de")
    data_service.save_crawl_instance("https://b.de/", 1.0, False, site="b.de")

    assert data_service.get_crawled_sites() == {"a.de", "b.de"}


# ---------------------------------------------------------------------------
# the saved frontier
# ---------------------------------------------------------------------------

def test_a_saved_frontier_comes_back_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "crawl.sqlite3")
    data_service.init_db()

    data_service.save_frontier([("https://a.example/", 5.0, 1.0),
                                ("https://b.example/", -2.0, 0.0)])

    assert data_service.load_frontier() == [("https://a.example/", 5.0, 1.0),
                                            ("https://b.example/", -2.0, 0.0)]


def test_saving_the_frontier_replaces_the_last_one(tmp_path, monkeypatch):
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "crawl.sqlite3")
    data_service.init_db()

    data_service.save_frontier([("https://a.example/", 5.0, 0.0)])
    data_service.save_frontier([("https://b.example/", 1.0, 0.0)])

    assert [url for url, _, _ in data_service.load_frontier()] == ["https://b.example/"]


def test_a_dropped_frontier_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "crawl.sqlite3")
    data_service.init_db()
    data_service.save_frontier([("https://a.example/", 5.0, 0.0)])

    data_service.clear_frontier()

    assert data_service.load_frontier() == []


def test_loading_a_frontier_from_a_database_that_never_saved_one(tmp_path, monkeypatch):
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "crawl.sqlite3")
    data_service.init_db()

    assert data_service.load_frontier() == []
