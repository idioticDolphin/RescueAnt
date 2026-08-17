from unittest.mock import MagicMock

import model.analyzer.category_service as category_service


def _make_llm_returning(category_name):
    llm = MagicMock()
    llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": category_name}}]
    }
    return llm


def test_categorize_website_returns_matching_category(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    result = category_service.categorize_website("<html><body>Some station page</body></html>")

    assert result.name == "STATION"


def test_categorize_website_passes_max_tokens_from_config(monkeypatch):
    llm = _make_llm_returning("IRRELEVANT")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    category_service.categorize_website("<html></html>")

    _, kwargs = llm.create_chat_completion.call_args
    assert kwargs["max_tokens"] == category_service.config.category_max_tokens


def test_categorize_website_builds_grammar_from_all_category_names(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    # Spy on LlamaGrammar.from_string itself rather than inspecting the
    # returned grammar object - a real LlamaGrammar instance isn't
    # subscriptable/introspectable the way a test stub might be, so the
    # only reliable place to check "what source string did we build" is
    # at the call site, before it gets handed to the real constructor.
    captured = {}
    original_from_string = category_service.LlamaGrammar.from_string

    def spy_from_string(grammar_str):
        captured["grammar_str"] = grammar_str
        return original_from_string(grammar_str)

    monkeypatch.setattr(category_service.LlamaGrammar, "from_string", spy_from_string)

    category_service.categorize_website("<html></html>")

    for category in category_service.config.get_categories():
        assert category.name in captured["grammar_str"]


def test_categorize_website_cleans_html_with_deduplication(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    captured = {}
    original_clean = category_service.cleaning_service.clean

    def spy_clean(html, deduplicate=False):
        captured["deduplicate"] = deduplicate
        return original_clean(html, deduplicate=deduplicate)

    monkeypatch.setattr(category_service.cleaning_service, "clean", spy_clean)

    category_service.categorize_website("<html><body><p>Hello</p><p>Hello</p></body></html>")

    assert captured["deduplicate"] is True


def test_categorize_website_uses_zero_temperature(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    category_service.categorize_website("<html></html>")

    _, kwargs = llm.create_chat_completion.call_args
    assert kwargs["temperature"] == 0
