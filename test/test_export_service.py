import csv

import pytest

import model.tools.config_service as config_service
import model.tools.data_service as data_service
from model.tools import export_service


@pytest.fixture(autouse=True)
def _database(tmp_path, monkeypatch):
    from model.objects.category import Category, Relevancy
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "crawl.sqlite3")
    station = Category(
        name="STATION", relevancy=Relevancy.CONTENT, analysis_model_id=0,
        analysis_prompt="p", analysis_max_tokens=10, process_links=False,
        fields={"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "telephone": {"type": "string"},
            "address": {"type": "string"}, "station_url": {"type": "string"}}})
    config = config_service.get_config().model_copy(update={"categories": [station], "field_semantics": {
        "name": {"role": "label"},
        "telephone": {"role": "identifier", "normalize": "phone"},
        "address": {"role": "locator"},
        "station_url": {"role": "identifier", "normalize": "url"}}})
    monkeypatch.setattr(config_service, "_session_config", config)
    monkeypatch.setattr(data_service, "config", config)
    data_service.init_db()
    data_service.init_entity_tables()
    yield


def _store(records):
    data_service.replace_entities([(record, []) for record in records])


def _exported(tmp_path, **kwargs):
    out = tmp_path / "stations.csv"
    export_service.export_entities(out, **kwargs)
    with open(out, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_export_writes_one_row_per_entity(tmp_path):
    _store([{"name": "Igelhilfe A", "telephone": "0123", "_n_sources": 2, "_confidence": 0.9},
            {"name": "Wildvogelhilfe B", "address": "Hauptstr. 1", "_n_sources": 1, "_confidence": 0.4}])

    rows = _exported(tmp_path)

    assert [r["name"] for r in rows] == ["Igelhilfe A", "Wildvogelhilfe B"]
    assert rows[0]["n_sources"] == "2"


def test_a_record_with_no_way_to_reach_it_is_flagged(tmp_path):
    _store([{"name": "Igelhilfe A", "telephone": "0123", "_n_sources": 2},
            {"name": "Only A Name", "station_url": "https://x.de/", "_n_sources": 2}])

    rows = _exported(tmp_path)

    assert rows[0]["review"] == ""
    assert "no-direct-contact" in rows[1]["review"]


def test_being_seen_once_is_reported_but_not_flagged(tmp_path):
    # Flagging every single-source row flagged all 547 rows of a real export.
    _store([{"name": "Igelhilfe A", "telephone": "0123", "_n_sources": 1}])
    row = _exported(tmp_path)[0]
    assert row["review"] == ""
    assert row["n_sources"] == "1"


def test_conflicting_contact_details_are_flagged(tmp_path):
    _store([{"name": "Igelhilfe A", "telephone": "0123", "_n_sources": 3,
             "_conflicts": [{"field": "telephone", "value": "0999"}]}])
    assert "conflicting-contact" in _exported(tmp_path)[0]["review"]


def test_a_differing_description_is_not_worth_flagging(tmp_path):
    _store([{"name": "Igelhilfe A", "telephone": "0123", "_n_sources": 3,
             "_conflicts": [{"field": "description", "value": "another blurb"}]}])
    assert _exported(tmp_path)[0]["review"] == ""


def test_only_records_needing_review_can_be_exported(tmp_path):
    _store([{"name": "Clean", "telephone": "0123", "_n_sources": 2},
            {"name": "Thin", "_n_sources": 1, "station_url": "https://thin.example/"}])

    rows = _exported(tmp_path, needing_review=True)

    assert [r["name"] for r in rows] == ["Thin"]


def test_lists_are_written_as_plain_text(tmp_path):
    _store([{"name": "Igelhilfe A", "telephone": ["0123", "0456"], "_n_sources": 2}])
    assert _exported(tmp_path)[0]["telephone"] == "0123; 0456"
