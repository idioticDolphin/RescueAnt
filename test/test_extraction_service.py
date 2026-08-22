import json
from unittest.mock import MagicMock

import model.analyzer.extraction_service as extraction_service
from model.objects.category import Category


def _make_llm_returning(payload: dict):
    llm = MagicMock()
    llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": json.dumps(payload)}}]
    }
    return llm


def test_extract_information_returns_none_for_irrelevant_category(monkeypatch):
    category = Category(name="IRRELEVANT", is_relevant=False)
    result = extraction_service.extract_information("<html></html>", category, "http://example.com/")
    assert result is None


def test_extract_information_returns_extracted_text_and_links(monkeypatch):
    category = Category(
        name="STATION",
        is_relevant=True,
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
        is_relevant=True,
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
        is_relevant=True,
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
        is_relevant=True,
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
        name="STATION", is_relevant=True, analysis_model_id=0,
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
        name="STATION", is_relevant=True, analysis_model_id=0,
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
