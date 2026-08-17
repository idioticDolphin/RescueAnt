import pytest

from model.objects.category import Category
from model.objects.config import Config
from model.exceptions import CategoryNotFoundError


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

def test_category_requires_name_and_is_relevant():
    category = Category(name="STATION", is_relevant=True)
    assert category.name == "STATION"
    assert category.is_relevant is True


def test_category_optional_fields_default_to_none():
    category = Category(name="IRRELEVANT", is_relevant=False)
    assert category.analysis_model_id is None
    assert category.analysis_prompt is None
    assert category.analysis_max_tokens is None
    assert category.fields is None
    assert category.process_links is None


def test_category_missing_required_field_raises():
    with pytest.raises(Exception):
        Category(is_relevant=True)  # missing "name"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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


def test_get_category_returns_matching_category():
    station = Category(name="STATION", is_relevant=True)
    other = Category(name="LIST", is_relevant=True)
    config = _make_config([station, other])

    assert config.get_category("STATION") is station
    assert config.get_category("LIST") is other


def test_get_category_raises_for_unknown_name():
    config = _make_config([Category(name="STATION", is_relevant=True)])
    with pytest.raises(CategoryNotFoundError):
        config.get_category("DOES_NOT_EXIST")


def test_config_simple_getters():
    config = _make_config([Category(name="STATION", is_relevant=True)])
    assert config.get_category_prompt() == "prompt"
    assert config.get_category_model_id() == 0
    assert config.get_politeness() == 2
    assert config.get_skip_tags() == ["script"]
    assert config.get_starting_url_path() == "starting_urls.csv"
    assert config.get_database_path() == "crawl.db"
    assert config.get_categories() == [Category(name="STATION", is_relevant=True)]


def test_config_search_getters_default_to_none():
    config = _make_config([Category(name="STATION", is_relevant=True)])
    assert config.get_search_provider() is None
    assert config.get_search_query_path() is None
