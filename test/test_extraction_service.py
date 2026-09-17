import json
from unittest.mock import MagicMock

import model.analyzer.extraction_service as extraction_service
from model.objects.category import Category, Relevancy
from model.objects.config import Config
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


# ---------------------------------------------------------------------------
# scrubbing implausible field values
# ---------------------------------------------------------------------------

def _config_with_phone_semantics(monkeypatch):
    config = Config(
        categories=[], category_prompt="p", category_max_tokens=10,
        category_context=2048, category_model_id=0, politeness=1,
        skip_tags=[], starting_url_path="s.csv", database_path="d.db",
        discover_urls=False, results_per_query=1, query_politeness=1.0,
        redo_all_fetches=False, redo_failed_fetches=False,
        require_fields=["name"],
        require_any_role=["identifier"],
        field_semantics={
            "name": {"role": "label"},
            "telephone": {"role": "identifier", "normalize": "phone"},
            "e-mail": {"role": "identifier", "normalize": "email"},
            "description": {"role": "attribute"},
        },
    )
    monkeypatch.setattr(extraction_service, "config", config)
    return config


def test_a_date_in_a_phone_field_is_dropped(monkeypatch):
    """'08.06.26' was extracted as a telephone in two separate live runs."""
    _config_with_phone_semantics(monkeypatch)
    record = {"name": "Station", "telephone": "08.06.26", "e-mail": "a@b.org"}

    cleaned, dropped = extraction_service.scrub_implausible(record)

    assert cleaned["telephone"] == ""
    assert cleaned["e-mail"] == "a@b.org"      # untouched
    assert "telephone" in dropped


def test_scrubbing_leaves_valid_values_alone(monkeypatch):
    _config_with_phone_semantics(monkeypatch)
    record = {"name": "Station", "telephone": "06131/ 477638", "description": "08.06.26"}

    cleaned, dropped = extraction_service.scrub_implausible(record)

    assert cleaned == record
    assert dropped == []


def test_a_record_left_without_an_identifier_by_scrubbing_is_rejected(monkeypatch):
    """The gate must see the scrubbed record, not the raw one: a record whose
    only identifier was a misparsed date identifies nothing."""
    _config_with_phone_semantics(monkeypatch)
    data = [{"name": "Station", "telephone": "08.06.26"}]

    kept = extraction_service._filter_admissible(data, "https://example.org")

    assert kept == []


def test_a_record_keeps_going_when_another_identifier_survives(monkeypatch):
    _config_with_phone_semantics(monkeypatch)
    data = [{"name": "Station", "telephone": "08.06.26", "e-mail": "a@b.org"}]

    kept = extraction_service._filter_admissible(data, "https://example.org")

    assert len(kept) == 1
    assert kept[0]["e-mail"] == "a@b.org"
    assert not kept[0]["telephone"]


def test_scrubbing_a_single_record_extraction(monkeypatch):
    _config_with_phone_semantics(monkeypatch)
    data = {"name": "Station", "telephone": "https://example.org/notfall",
            "e-mail": "a@b.org"}

    kept = extraction_service._filter_admissible(data, "https://example.org")

    assert kept is not None
    assert not kept["telephone"]


# ---------------------------------------------------------------------------
# the "mislabeled" verdict
#
# The extractor sees far more than the classifier did - the whole page, under
# instructions that describe exactly what a record of this category is. When
# the page plainly is not one, a full extraction still cost about 37 seconds
# and produced a record that was later rejected or, worse, kept. Offering the
# extractor a way to say so turns that into a verdict costing a few tokens.
#
# The option is a grammar alternative, not an extra field: every required
# field of a JSON schema is generated regardless, so a boolean beside the
# record would save nothing. {"mislabeled": true} is about six tokens.
# ---------------------------------------------------------------------------

def test_the_mislabel_option_is_an_alternative_to_the_record():
    record = {"type": "object", "properties": {"name": {"type": "string"}},
              "required": ["name"]}
    wrapped = extraction_service.with_mislabel_option(record)
    assert "anyOf" in wrapped
    assert record in wrapped["anyOf"]
    verdict = [s for s in wrapped["anyOf"] if s is not record][0]
    assert verdict["required"] == ["mislabeled"]


def test_the_mislabel_option_also_wraps_a_listing_schema():
    listing = {"type": "array", "items": {"type": "object", "properties": {}}}
    wrapped = extraction_service.with_mislabel_option(listing)
    assert listing in wrapped["anyOf"]


def test_with_the_check_off_the_schema_is_unchanged(monkeypatch):
    llm = _make_llm_returning({"name": "X"})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "mislabel_check", False, raising=False)
    category = make_fake_category()

    extraction_service.extract_information("<html>x</html>", category, "http://e.com/")

    _, kwargs = llm.create_chat_completion.call_args
    assert "anyOf" not in kwargs["response_format"]["schema"]


def test_with_the_check_on_the_model_is_offered_the_verdict(monkeypatch):
    llm = _make_llm_returning({"name": "X"})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "mislabel_check", True, raising=False)
    monkeypatch.setattr(extraction_service.config, "mislabel_instruction",
                        "If this is not such a page, say so.", raising=False)

    extraction_service.extract_information("<html>x</html>", make_fake_category(), "http://e.com/")

    _, kwargs = llm.create_chat_completion.call_args
    assert "anyOf" in kwargs["response_format"]["schema"]
    system = kwargs["messages"][0]["content"]
    assert "If this is not such a page, say so." in system


def test_a_mislabeled_verdict_is_returned_as_such_with_its_links(monkeypatch):
    llm = _make_llm_returning({"mislabeled": True})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "mislabel_check", True, raising=False)
    monkeypatch.setattr(extraction_service.cleaning_service, "extract_links",
                        lambda html, base: ["http://e.com/next"])

    data, links = extraction_service.extract_information(
        "<html>x</html>", make_fake_category(), "http://e.com/")

    assert data is extraction_service.MISLABELED
    assert links == ["http://e.com/next"]


def test_a_record_is_still_a_record_with_the_check_on(monkeypatch):
    llm = _make_llm_returning({"name": "Station", "telephone": "06131 477638"})
    monkeypatch.setattr(extraction_service.llm_service, "get_model", lambda mid: llm)
    monkeypatch.setattr(extraction_service.config, "mislabel_check", True, raising=False)

    data, _ = extraction_service.extract_information(
        "<html>x</html>", make_fake_category(), "http://e.com/")

    assert data is not extraction_service.MISLABELED
    assert data["name"] == "Station"


def test_a_record_named_with_an_excluded_token_is_rejected(monkeypatch):
    monkeypatch.setattr(extraction_service.config, "exclude_record_name_tokens", ["kitz"], raising=False)
    ok, reason = extraction_service.is_admissible({"name": "Rehkitzrettung Hattingen e.V."})
    assert not ok and "kitz" in reason
    assert extraction_service.is_admissible({"name": "Igelhilfe Luzern"})[0] is True
