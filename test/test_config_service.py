import pytest

import model.tools.config_service as config_service
from model.exceptions import ConfigError


# ---------------------------------------------------------------------------
# _read_config - raw ini-like file parsing
# ---------------------------------------------------------------------------

def test_read_config_parses_simple_key_value(tmp_path):
    config_file = tmp_path / "bot.config"
    config_file.write_text('politeness = 5;\n')
    result = config_service._read_config(config_file)
    assert result["politeness"] == "5"


def test_read_config_strips_surrounding_quotes():
    config_file_content = 'category_prompt = "Categorize this website.";'
    import re
    pattern = r"""(?:^|[\n])\s*(?P<left>.+?)\s*=\s*(?P<right>(?:[^;'"]|(?:(?:".*?")|(?:'.*?')))*?);"""
    matches = re.findall(pattern, config_file_content)
    assert matches[0][0] == "category_prompt"
    # the raw captured value still contains the quote characters -
    # _read_config is what strips them off afterwards
    parsed = {m[0]: m[1].strip("'").strip('"') for m in matches}
    assert parsed["category_prompt"] == "Categorize this website."


def test_read_config_handles_multiple_entries(tmp_path):
    config_file = tmp_path / "bot.config"
    config_file.write_text(
        'politeness = 5;\n'
        'database = "crawl.db";\n'
        'categories = STATION|LIST|IRRELEVANT;\n'
    )
    result = config_service._read_config(config_file)
    assert result["politeness"] == "5"
    assert result["database"] == "crawl.db"
    assert result["categories"] == "STATION|LIST|IRRELEVANT"


# ---------------------------------------------------------------------------
# _parse_type_definitions
# ---------------------------------------------------------------------------

def test_parse_type_definitions_splits_on_pipe():
    result = config_service._parse_type_definitions({"animal_type": "MAMMAL|REPTILE|BIRD"})
    assert result == {"animal_type": ["MAMMAL", "REPTILE", "BIRD"]}


def test_parse_type_definitions_single_value_no_pipe():
    result = config_service._parse_type_definitions({"status": "ACTIVE"})
    assert result == {"status": ["ACTIVE"]}


def test_parse_type_definitions_empty_dict():
    assert config_service._parse_type_definitions({}) == {}


# ---------------------------------------------------------------------------
# _resolve_field_schema
# ---------------------------------------------------------------------------

def test_resolve_field_schema_plain_primitive():
    result = config_service._resolve_field_schema("string", {})
    assert result == {"type": "string"}


def test_resolve_field_schema_custom_enum_type():
    custom_types = {"animal_type": ["MAMMAL", "REPTILE", "BIRD"]}
    result = config_service._resolve_field_schema("animal_type", custom_types)
    assert result == {"type": "string", "enum": ["MAMMAL", "REPTILE", "BIRD"]}


def test_resolve_field_schema_list_of_primitive():
    result = config_service._resolve_field_schema("list[string]", {})
    assert result == {"type": "array", "items": {"type": "string"}}


def test_resolve_field_schema_list_of_custom_enum():
    custom_types = {"animal_type": ["MAMMAL", "REPTILE", "BIRD"]}
    result = config_service._resolve_field_schema("list[animal_type]", custom_types)
    assert result == {
        "type": "array",
        "items": {"type": "string", "enum": ["MAMMAL", "REPTILE", "BIRD"]},
    }


def test_resolve_field_schema_unknown_custom_type_falls_back_to_primitive():
    # regression guard: if a type name isn't in custom_types, it should NOT
    # silently produce an invalid schema fragment like {"type": "animal_type"}
    # going undetected - this test documents current behavior explicitly.
    result = config_service._resolve_field_schema("animal_type", {})
    assert result == {"type": "animal_type"}


# ---------------------------------------------------------------------------
# _build_schema
# ---------------------------------------------------------------------------

def test_build_schema_produces_object_with_required_fields():
    fields = {"name": "string", "accepted_animals": "list[animal_type]"}
    custom_types = {"animal_type": ["MAMMAL", "REPTILE", "BIRD"]}
    schema = config_service._build_schema(fields, custom_types)

    assert schema["type"] == "object"
    assert schema["properties"]["name"] == {"type": "string"}
    assert schema["properties"]["accepted_animals"] == {
        "type": "array",
        "items": {"type": "string", "enum": ["MAMMAL", "REPTILE", "BIRD"]},
    }
    assert set(schema["required"]) == {"name", "accepted_animals"}


# ---------------------------------------------------------------------------
# _wrap_as_list_schema
# ---------------------------------------------------------------------------

def test_wrap_as_list_schema():
    item_schema = {"type": "object", "properties": {}, "required": []}
    result = config_service._wrap_as_list_schema(item_schema)
    assert result == {"type": "array", "items": item_schema}


# ---------------------------------------------------------------------------
# _load_config - end to end using the real bot.config format, with
# llm_service.get_model_id mocked out so no real model gets loaded.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_session_config():
    """
    _load_config() mutates the module-level global _session_config as a
    side effect. Other test modules (and conftest) rely on a specific fake
    config being in place, so save/restore it around every test in this
    file to avoid order-dependent test pollution.
    """
    original = config_service._session_config
    yield
    config_service._session_config = original


@pytest.fixture
def sample_config_text():
    return '''
starting_url_file = "starting_urls.csv";
database = "crawl.db";

politeness = 5;
skip_tags = "script", "style";
redo_all_fetches = False;
redo_failed_fetches = True;

categories = STATION|IRRELEVANT;
category_prompt = "Categorize.";
category_max_tokens = 20;
category_model_path = "models/fake.gguf";
category_context = 4096;

relevancy[STATION] = True;
prompt[STATION] = "Extract fields.";
max_tokens[STATION] = 40;
context[STATION] = 4096;
model_path[STATION] = "models/fake.gguf";
check_linked_urls[STATION] = True;
is_list_category[STATION] = False;

relevancy[IRRELEVANT] = False;

fields = {"name": "string", "accepted_animals": "list[animal_type]"};

define animal_type = MAMMAL|REPTILE|BIRD;
'''


def test_load_config_builds_config_with_expected_categories(monkeypatch, tmp_path, sample_config_text):
    config_file = tmp_path / "bot.config"
    config_file.write_text(sample_config_text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    result = config_service.get_config()

    names = [c.name for c in result.get_categories()]
    assert names == ["STATION", "IRRELEVANT"]

    station = result.get_category("STATION")
    assert station.is_relevant is True
    assert station.analysis_max_tokens == 40
    assert station.process_links is True
    assert station.fields["properties"]["accepted_animals"] == {
        "type": "array",
        "items": {"type": "string", "enum": ["MAMMAL", "REPTILE", "BIRD"]},
    }

    irrelevant = result.get_category("IRRELEVANT")
    assert irrelevant.is_relevant is False


def test_load_config_wraps_fields_as_list_when_is_list_category_true(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace(
        "is_list_category[STATION] = False;",
        "is_list_category[STATION] = True;",
    )
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    result = config_service.get_config()

    station = result.get_category("STATION")
    assert station.fields["type"] == "array"
    assert "properties" in station.fields["items"]


def test_load_config_raises_config_error_on_missing_key(monkeypatch, tmp_path):
    # "politeness" is missing entirely -> should surface as ConfigError,
    # not a raw KeyError, since callers rely on catching ConfigError.
    config_file = tmp_path / "bot.config"
    config_file.write_text('database = "crawl.db"; categories = STATION; relevancy[STATION] = False;')

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    with pytest.raises(ConfigError):
        config_service.load_config(configs)


def test_load_config_raises_config_error_on_malformed_relevancy_value(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace("relevancy[IRRELEVANT] = False;", "relevancy[IRRELEVANT] = MAYBE;")
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    with pytest.raises(ConfigError):
        config_service.load_config(configs)


def test_load_config_raises_config_error_when_relevant_category_has_no_fields(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace(
        'fields = {"name": "string", "accepted_animals": "list[animal_type]"};', ""
    )
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    with pytest.raises(ConfigError):
        config_service.load_config(configs)


def test_load_config_uses_per_category_fields_override(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace(
        "check_linked_urls[STATION] = True;",
        'check_linked_urls[STATION] = True;\nfields[STATION] = {"custom_field": "string"};',
    )
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    station = config_service.get_config().get_category("STATION")

    assert "custom_field" in station.fields["properties"]
    assert "accepted_animals" not in station.fields["properties"]


def test_load_config_builds_google_search_provider_when_discovery_enabled(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace(
        "politeness = 5;",
        'politeness = 5;\n'
        'discover_urls = True;\n'
        'search_provider = "Google";\n'
        'search_query_file = "search_queries.csv";\n'
        'search_api_key = "key123";\n'
        'search_engine_id = "cx123";\n'
        'search_timeout = 5.0;\n'
        'results_per_query = 5;\n'
        'query_politeness = 0.5;\n',
    )
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    result = config_service.get_config()

    assert result.discover_urls is True
    assert result.results_per_query == 5
    assert result.query_politeness == 0.5
    from model.objects.searchprovider import GoogleCustomSearchProvider
    assert isinstance(result.search_provider, GoogleCustomSearchProvider)
    assert result.search_provider.api_key == "key123"


def test_load_config_builds_configurable_json_provider_for_non_google_provider(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace(
        "politeness = 5;",
        'politeness = 5;\n'
        'discover_urls = True;\n'
        'search_provider = "SearXNG";\n'
        'search_query_file = "search_queries.csv";\n'
        'search_base_url = "http://localhost:8080/search";\n'
        'query_parameters = "q";\n'
        'search_result_path = "results";\n'
        'search_url_field = "url";\n'
        'search_extra_params = {"format": "json"};\n'
        'search_headers = {};\n'
        'search_timeout = 5.0;\n'
        'results_per_query = 5;\n'
        'query_politeness = 0.5;\n',
    )
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    result = config_service.get_config()

    from model.objects.searchprovider import ConfigurableJsonSearchProvider
    assert isinstance(result.search_provider, ConfigurableJsonSearchProvider)
    assert result.search_provider.base_url == "http://localhost:8080/search"
    assert result.search_provider.result_path == ["results"]


def test_load_config_falls_back_gracefully_when_discovery_config_incomplete(monkeypatch, tmp_path, sample_config_text):
    # discover_urls = True but none of the required search_* keys are present -
    # this must not blow up the whole config load, just disable discovery.
    text = sample_config_text.replace("politeness = 5;", 'politeness = 5;\ndiscover_urls = True;\n')
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    result = config_service.get_config()

    assert result.discover_urls is False
    assert result.search_provider is None


# ---------------------------------------------------------------------------
# get_config - singleton behavior
# ---------------------------------------------------------------------------

def test_load_config_discover_urls_explicitly_false_disables_discovery(monkeypatch, tmp_path, sample_config_text):
    text = sample_config_text.replace("politeness = 5;", 'politeness = 5;\ndiscover_urls = False;\n')
    config_file = tmp_path / "bot.config"
    config_file.write_text(text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )

    configs = config_service._read_config(config_file)
    config_service.load_config(configs)
    result = config_service.get_config()

    assert result.discover_urls is False
    assert result.search_provider is None
    assert result.results_per_query == 0
    assert result.query_politeness == 0


def test_get_config_loads_lazily_when_no_session_config_exists(monkeypatch, tmp_path, sample_config_text):
    config_file = tmp_path / "bot.config"
    config_file.write_text(sample_config_text)

    monkeypatch.setattr(
        "model.tools.llm_service.get_model_id",
        lambda model_path, context: 42,
    )
    original_read_config = config_service._read_config
    monkeypatch.setattr(config_service, "_read_config", lambda: original_read_config(config_file))
    config_service._session_config = None

    result = config_service.get_config()

    assert result is config_service._session_config
    assert [c.name for c in result.get_categories()] == ["STATION", "IRRELEVANT"]
