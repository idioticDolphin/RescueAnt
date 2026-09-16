import json
from unittest.mock import MagicMock

import model.analyzer.extraction_service as extraction_service
from model.objects.category import Category, Relevancy
from conftest import make_fake_category


def _make_llm_returning(payload: dict):
    llm = MagicMock()
    llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": json.dumps(payload)}}]
    }
    return llm


def _make_llm_returning_raw(raw: str):
    """LLM stub returning `raw` verbatim (not JSON-encoded), so tests can
    simulate malformed or truncated completions."""
    llm = MagicMock()
    llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": raw}}]
    }
    return llm


def test_extract_information_returns_none_for_irrelevant_category(monkeypatch):
    category = Category(name="IRRELEVANT", relevancy=Relevancy.IRRELEVANT)
    result = extraction_service.extract_information("<html></html>", category, "http://example.com/")
    assert result is None


def test_extract_information_returns_no_content_but_links_for_links_only_category(monkeypatch):
    category = Category(name="HUB", relevancy=Relevancy.LINKS, process_links=True)
    monkeypatch.setattr(
        extraction_service.cleaning_service, "extract_links",
        lambda html, base_url: ["http://example.com/station-a", "http://example.com/station-b"],
    )

    extracted_data, links = extraction_service.extract_information(
        "<html><a href='/station-a'>A</a></html>", category, "http://example.com/"
    )

    assert extracted_data is None
    assert links == ["http://example.com/station-a", "http://example.com/station-b"]


def test_extract_information_links_only_category_skips_link_extraction_when_process_links_false(monkeypatch):
    category = Category(name="HUB", relevancy=Relevancy.LINKS, process_links=False)
    link_extractor = MagicMock(return_value=["should-not-be-called"])
    monkeypatch.setattr(extraction_service.cleaning_service, "extract_links", link_extractor)

    extracted_data, links = extraction_service.extract_information("<html></html>", category, "http://example.com/")

    link_extractor.assert_not_called()
    assert extracted_data is None
    assert links == []


def test_extract_information_returns_extracted_text_and_links(monkeypatch):
    category = Category(
        name="STATION",
        relevancy=Relevancy.CONTENT,
        analysis_model_id=0,
        analysis_prompt="Extract fields.",
        analysis_max_tokens=40,
        fields={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        process_links=True,
    )
    llm = _make_llm_returning({"name": "Igel Station Berlin"})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda model_id: llm)
    monkeypatch.setattr(
        extraction_service.cleaning_service, "extract_links",
        lambda html, base_url: ["http://example.com/imprint"],
    )

    extracted, links = extraction_service.extract_information(
        "<html><a href='/imprint'>Imprint</a></html>", category, "http://example.com/"
    )

    assert extracted == {"name": "Igel Station Berlin"}
    assert links == ["http://example.com/imprint"]


def test_extract_information_skips_link_extraction_when_process_links_false(monkeypatch):
    category = Category(
        name="STATION",
        relevancy=Relevancy.CONTENT,
        analysis_model_id=0,
        analysis_prompt="Extract fields.",
        analysis_max_tokens=40,
        fields={"type": "object", "properties": {}, "required": []},
        process_links=False,
    )
    llm = _make_llm_returning({})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda model_id: llm)

    link_extractor = MagicMock(return_value=["should-not-be-called"])
    monkeypatch.setattr(extraction_service.cleaning_service, "extract_links", link_extractor)

    extracted, links = extraction_service.extract_information("<html></html>", category, "http://example.com/")

    link_extractor.assert_not_called()
    assert links == []


def test_extract_information_passes_category_schema_as_response_format(monkeypatch):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    category = Category(
        name="STATION",
        relevancy=Relevancy.CONTENT,
        analysis_model_id=0,
        analysis_prompt="Extract fields.",
        analysis_max_tokens=40,
        fields=schema,
        process_links=False,
    )
    llm = _make_llm_returning({"name": "x"})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda model_id: llm)

    extraction_service.extract_information("<html></html>", category, "http://example.com/")

    _, kwargs = llm.create_chat_completion.call_args
    assert kwargs["response_format"]["type"] == "json_object"
    assert kwargs["response_format"]["schema"] == schema


def test_extract_information_prompt_mentions_the_schema(monkeypatch):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    category = Category(
        name="STATION",
        relevancy=Relevancy.CONTENT,
        analysis_model_id=0,
        analysis_prompt="Extract fields.",
        analysis_max_tokens=40,
        fields=schema,
        process_links=False,
    )
    llm = _make_llm_returning({"name": "x"})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda model_id: llm)

    extraction_service.extract_information("<html></html>", category, "http://example.com/")

    _, kwargs = llm.create_chat_completion.call_args
    system_message = kwargs["messages"][0]["content"]
    assert "name" in system_message  # schema was embedded into the prompt


def test_extract_information_returns_none_when_completion_raises(monkeypatch, caplog):
    # e.g. llama_cpp's "Requested tokens (N) exceed context window of M"
    # ValueError for an overlong page - must not propagate and crash the run
    category = Category(
        name="STATION", relevancy=Relevancy.CONTENT, analysis_model_id=0,
        analysis_prompt="Extract fields.", analysis_max_tokens=40,
        fields={"type": "object", "properties": {}, "required": []},
        process_links=False,
    )
    llm = MagicMock()
    llm.create_chat_completion.side_effect = ValueError("Requested tokens (40000) exceed context window of 32768")
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda model_id: llm)

    with caplog.at_level("WARNING", logger="model.analyzer.extraction_service"):
        result = extraction_service.extract_information("<html></html>", category, "http://example.com/")

    assert result is None
    assert "Extraction failed" in caplog.text


def test_extract_information_returns_none_when_completion_is_unparsable_json(monkeypatch, caplog):
    # a completion cut off by max_tokens before the JSON closes must not crash either
    category = Category(
        name="STATION", relevancy=Relevancy.CONTENT, analysis_model_id=0,
        analysis_prompt="Extract fields.", analysis_max_tokens=40,
        fields={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        process_links=False,
    )
    llm = MagicMock()
    llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": '{"name": "truncated...'}}]
    }
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda model_id: llm)

    with caplog.at_level("WARNING", logger="model.analyzer.extraction_service"):
        result = extraction_service.extract_information("<html></html>", category, "http://example.com/")

    assert result is None
    assert "Extraction failed" in caplog.text


# ---------------------------------------------------------------------------
# generation bounds (P13) - the runaway-generation root cause
# ---------------------------------------------------------------------------

def test_extraction_passes_category_max_tokens(monkeypatch):
    """Without max_tokens, llama-cpp generates until the context window is
    exhausted - the cause of the observed 90-120 minute runaway calls."""
    llm = _make_llm_returning_raw('{"name": "X"}')
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    category = make_fake_category()

    extraction_service.extract_information("<html>x</html>", category, "http://e.com/")

    _, kwargs = llm.create_chat_completion.call_args
    assert kwargs["max_tokens"] == category.analysis_max_tokens
    assert kwargs["max_tokens"] is not None


def test_extraction_passes_repeat_penalty(monkeypatch):
    llm = _make_llm_returning_raw('{"name": "X"}')
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "repeat_penalty", 1.15, raising=False)

    extraction_service.extract_information("<html>x</html>", make_fake_category(), "http://e.com/")

    _, kwargs = llm.create_chat_completion.call_args
    assert kwargs["repeat_penalty"] == 1.15


# ---------------------------------------------------------------------------
# truncation salvage
# ---------------------------------------------------------------------------

def test_truncated_list_output_salvages_complete_objects(monkeypatch):
    # A list extraction that hit max_tokens mid-way: two complete objects,
    # then a truncated third. The complete ones should survive.
    truncated = '[{"station_url": "a"}, {"station_url": "b"}, {"station_url": "c'
    llm = _make_llm_returning_raw(truncated)
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    category = make_fake_category("LIST", fields={
        "type": "array",
        "items": {"type": "object", "properties": {"station_url": {"type": "string"}}},
    })

    result = extraction_service.extract_information("<html>x</html>", category, "http://e.com/")

    assert result is not None
    data, _links = result
    assert [d["station_url"] for d in data] == ["a", "b"]


def test_unsalvageable_output_still_returns_none(monkeypatch):
    llm = _make_llm_returning_raw("total garbage not json")
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    result = extraction_service.extract_information("<html>x</html>", make_fake_category(), "http://e.com/")
    assert result is None


# ---------------------------------------------------------------------------
# admissibility gate (P10)
# ---------------------------------------------------------------------------

def test_is_admissible_accepts_when_unconfigured():
    ok, reason = extraction_service.is_admissible({"name": "X"})
    assert ok and reason is None


def test_is_admissible_requires_configured_fields(monkeypatch):
    monkeypatch.setattr(extraction_service.config, "require_fields", ["name"], raising=False)
    assert extraction_service.is_admissible({"name": "X"})[0] is True
    assert extraction_service.is_admissible({"name": ""})[0] is False
    assert extraction_service.is_admissible({})[0] is False


def test_is_admissible_requires_a_field_of_each_required_role(monkeypatch):
    monkeypatch.setattr(extraction_service.config, "require_any_role", ["identifier"], raising=False)
    monkeypatch.setattr(extraction_service.config, "field_semantics", {
        "e-mail": {"role": "identifier"},
        "telephone": {"role": "identifier"},
        "blurb": {"role": "attribute"},
    }, raising=False)

    assert extraction_service.is_admissible({"e-mail": "a@b.c"})[0] is True
    assert extraction_service.is_admissible({"telephone": "123"})[0] is True
    assert extraction_service.is_admissible({"blurb": "words"})[0] is False


def test_is_admissible_reports_a_reason(monkeypatch):
    monkeypatch.setattr(extraction_service.config, "require_fields", ["name"], raising=False)
    ok, reason = extraction_service.is_admissible({})
    assert not ok and "name" in reason


def test_inadmissible_records_are_filtered_from_list_results(monkeypatch):
    payload = '[{"station_url": "a", "e-mail": "x@y.z"}, {"station_url": "b"}]'
    llm = _make_llm_returning_raw(payload)
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "require_any_role", ["identifier"], raising=False)
    monkeypatch.setattr(extraction_service.config, "field_semantics",
                        {"e-mail": {"role": "identifier"}}, raising=False)
    category = make_fake_category("LIST", fields={
        "type": "array",
        "items": {"type": "object", "properties": {"station_url": {"type": "string"}}},
    })

    data, _links = extraction_service.extract_information("<html>x</html>", category, "http://e.com/")

    assert [d["station_url"] for d in data] == ["a"]


def test_inadmissible_single_record_yields_no_data_but_keeps_links(monkeypatch):
    llm = _make_llm_returning_raw('{"name": "Boilerplate Org"}')
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "require_any_role", ["identifier"], raising=False)
    monkeypatch.setattr(extraction_service.config, "field_semantics",
                        {"e-mail": {"role": "identifier"}}, raising=False)

    html = '<html><a href="/x">x</a></html>'
    data, links = extraction_service.extract_information(html, make_fake_category(), "http://e.com/")

    assert data is None
    assert links == ["http://e.com/x"]


def test_require_any_role_is_a_union_not_an_intersection(monkeypatch):
    """A record with a locator but no identifier still qualifies when both
    roles are listed - listing two roles must not demand one of each."""
    monkeypatch.setattr(extraction_service.config, "require_any_role",
                        ["identifier", "locator"], raising=False)
    monkeypatch.setattr(extraction_service.config, "field_semantics", {
        "e-mail": {"role": "identifier"},
        "address": {"role": "locator"},
    }, raising=False)

    assert extraction_service.is_admissible({"address": "Kirchstr. 1"})[0] is True
    assert extraction_service.is_admissible({"e-mail": "a@b.c"})[0] is True
    assert extraction_service.is_admissible({"name": "only a name"})[0] is False
