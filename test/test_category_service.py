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


def test_categorize_website_returns_none_when_completion_raises(monkeypatch, caplog):
    # e.g. llama_cpp's "Requested tokens (N) exceed context window of M"
    # ValueError for an overlong page - must not propagate and crash the run
    llm = MagicMock()
    llm.create_chat_completion.side_effect = ValueError("Requested tokens (8365) exceed context window of 8192")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    with caplog.at_level("WARNING", logger="model.analyzer.category_service"):
        result = category_service.categorize_website("<html></html>")

    assert result is None
    assert "Categorization failed" in caplog.text


def test_categorize_website_returns_none_when_model_response_matches_no_category(monkeypatch, caplog):
    # defensive: an out-of-grammar/unrecognized response must not crash either
    llm = _make_llm_returning("NOT_A_REAL_CATEGORY")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    with caplog.at_level("WARNING", logger="model.analyzer.category_service"):
        result = category_service.categorize_website("<html></html>")

    assert result is None


# ---------------------------------------------------------------------------
# url_prior (P7) and vote-based abstention (P9)
# ---------------------------------------------------------------------------

def _configure(monkeypatch, **overrides):
    """Apply config overrides to the live session Config for one test."""
    for key, value in overrides.items():
        monkeypatch.setattr(category_service.config, key, value, raising=False)


def test_url_prior_returns_none_when_unconfigured():
    assert category_service.url_prior("http://example.com/datenschutz") is None


def test_url_prior_matches_configured_exclude_token(monkeypatch):
    _configure(monkeypatch, url_tokens_exclude=["datenschutz"], url_prior_category="HUB")
    assert category_service.url_prior("http://example.com/datenschutz.shtml").name == "HUB"


def test_url_prior_ignores_non_matching_url(monkeypatch):
    _configure(monkeypatch, url_tokens_exclude=["datenschutz"], url_prior_category="HUB")
    assert category_service.url_prior("http://example.com/kontakt") is None


def test_url_prior_ignores_token_in_host(monkeypatch):
    _configure(monkeypatch, url_tokens_exclude=["presse"], url_prior_category="HUB")
    assert category_service.url_prior("http://presse.example.com/station") is None


def test_url_prior_needs_no_llm_call(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)
    _configure(monkeypatch, url_tokens_exclude=["spenden"], url_prior_category="HUB")

    result = category_service.categorize_website("<html>x</html>",
                                                 url="http://example.com/spenden")

    assert result.name == "HUB"
    llm.create_chat_completion.assert_not_called()


def test_url_prior_with_unknown_category_falls_through(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)
    _configure(monkeypatch, url_tokens_exclude=["spenden"], url_prior_category="NOPE")

    result = category_service.categorize_website("<html>x</html>",
                                                 url="http://example.com/spenden")

    assert result.name == "STATION"


def test_url_is_included_in_the_prompt(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)

    category_service.categorize_website("<html>body</html>", url="http://example.com/a/b")

    _, kwargs = llm.create_chat_completion.call_args
    assert "http://example.com/a/b" in kwargs["messages"][1]["content"]


def test_categorize_still_works_without_url(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)
    assert category_service.categorize_website("<html></html>").name == "STATION"


def test_voting_takes_the_majority(monkeypatch):
    llm = MagicMock()
    llm.create_chat_completion.side_effect = [
        {"choices": [{"message": {"content": c}}]} for c in ("STATION", "HUB", "STATION")
    ]
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)
    _configure(monkeypatch, category_votes=3)

    assert category_service.categorize_website("<html></html>").name == "STATION"
    assert llm.create_chat_completion.call_count == 3


def test_split_vote_abstains_to_url_prior_category(monkeypatch):
    llm = MagicMock()
    llm.create_chat_completion.side_effect = [
        {"choices": [{"message": {"content": c}}]} for c in ("STATION", "LIST")
    ]
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)
    _configure(monkeypatch, category_votes=2, url_prior_category="HUB")

    assert category_service.categorize_website("<html></html>").name == "HUB"


def test_single_vote_uses_temperature_zero(monkeypatch):
    llm = _make_llm_returning("STATION")
    monkeypatch.setattr(category_service.llm_service, "get_model", lambda model_id: llm)
    _configure(monkeypatch, category_votes=1)

    category_service.categorize_website("<html></html>")

    _, kwargs = llm.create_chat_completion.call_args
    assert kwargs["temperature"] == 0


def test_majority_helper_reports_decisiveness():
    assert category_service._majority(["a", "a", "b"]) == ("a", True)
    assert category_service._majority(["a", "b"])[1] is False
