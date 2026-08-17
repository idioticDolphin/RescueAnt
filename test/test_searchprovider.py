from unittest.mock import MagicMock

from model.objects.searchprovider import GoogleCustomSearchProvider, ConfigurableJsonSearchProvider


def _fake_response(json_data):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = json_data
    return response


# ---------------------------------------------------------------------------
# GoogleCustomSearchProvider
# ---------------------------------------------------------------------------

def test_google_search_returns_links_from_single_page(monkeypatch):
    provider = GoogleCustomSearchProvider(api_key="k", search_engine_id="cx")
    response = _fake_response({"items": [{"link": "http://a.com"}, {"link": "http://b.com"}]})
    get = MagicMock(return_value=response)
    monkeypatch.setattr("model.objects.searchprovider.requests.get", get)

    result = provider.search("query", max_results=10)

    assert result == ["http://a.com", "http://b.com"]
    assert get.call_count == 1


def test_google_search_paginates_beyond_ten_results(monkeypatch):
    provider = GoogleCustomSearchProvider(api_key="k", search_engine_id="cx")
    first_page = _fake_response({"items": [{"link": f"http://{i}.com"} for i in range(10)]})
    second_page = _fake_response({"items": [{"link": "http://10.com"}, {"link": "http://11.com"}]})
    get = MagicMock(side_effect=[first_page, second_page])
    monkeypatch.setattr("model.objects.searchprovider.requests.get", get)

    result = provider.search("query", max_results=12)

    assert len(result) == 12
    assert get.call_count == 2
    second_call_params = get.call_args_list[1].kwargs["params"]
    assert second_call_params["start"] == 11


def test_google_search_stops_when_provider_runs_out_of_results(monkeypatch):
    provider = GoogleCustomSearchProvider(api_key="k", search_engine_id="cx")
    short_page = _fake_response({"items": [{"link": "http://a.com"}]})
    get = MagicMock(return_value=short_page)
    monkeypatch.setattr("model.objects.searchprovider.requests.get", get)

    result = provider.search("query", max_results=50)

    assert result == ["http://a.com"]
    assert get.call_count == 1


def test_google_search_skips_items_without_link(monkeypatch):
    provider = GoogleCustomSearchProvider(api_key="k", search_engine_id="cx")
    response = _fake_response({"items": [{"title": "no link here"}, {"link": "http://a.com"}]})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    result = provider.search("query", max_results=10)

    assert result == ["http://a.com"]


def test_google_search_returns_empty_list_when_no_items(monkeypatch):
    provider = GoogleCustomSearchProvider(api_key="k", search_engine_id="cx")
    response = _fake_response({})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    assert provider.search("query", max_results=10) == []


# ---------------------------------------------------------------------------
# ConfigurableJsonSearchProvider
# ---------------------------------------------------------------------------

def test_configurable_provider_extracts_urls_via_result_path_and_url_field(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://searx.local/search",
        query_param="q",
        result_path=["results"],
        url_field="url",
    )
    response = _fake_response({"results": [{"url": "http://a.com"}, {"url": "http://b.com"}]})
    get = MagicMock(return_value=response)
    monkeypatch.setattr("model.objects.searchprovider.requests.get", get)

    result = provider.search("query", max_results=10)

    assert result == ["http://a.com", "http://b.com"]
    assert get.call_args.kwargs["params"]["q"] == "query"


def test_configurable_provider_truncates_to_max_results(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://searx.local/search",
        query_param="q",
        result_path=["results"],
        url_field="url",
    )
    response = _fake_response({"results": [{"url": f"http://{i}.com"} for i in range(5)]})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    result = provider.search("query", max_results=2)

    assert result == ["http://0.com", "http://1.com"]


def test_configurable_provider_navigates_nested_result_path(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://api.local/search",
        query_param="q",
        result_path=["data", "organic_results"],
        url_field="link",
    )
    response = _fake_response({"data": {"organic_results": [{"link": "http://a.com"}]}})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    assert provider.search("query", max_results=10) == ["http://a.com"]


def test_configurable_provider_returns_empty_list_when_result_path_resolves_to_non_list(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://api.local/search",
        query_param="q",
        result_path=["results"],
        url_field="url",
    )
    response = _fake_response({"results": "not a list"})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    assert provider.search("query", max_results=10) == []


def test_configurable_provider_returns_empty_list_when_result_path_missing(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://api.local/search",
        query_param="q",
        result_path=["results"],
        url_field="url",
    )
    response = _fake_response({"unexpected": []})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    assert provider.search("query", max_results=10) == []


def test_configurable_provider_skips_items_missing_url_field(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://api.local/search",
        query_param="q",
        result_path=["results"],
        url_field="url",
    )
    response = _fake_response({"results": [{"title": "no url"}, {"url": "http://a.com"}]})
    monkeypatch.setattr("model.objects.searchprovider.requests.get", MagicMock(return_value=response))

    assert provider.search("query", max_results=10) == ["http://a.com"]


def test_configurable_provider_sends_extra_params_and_headers(monkeypatch):
    provider = ConfigurableJsonSearchProvider(
        base_url="http://api.local/search",
        query_param="q",
        result_path=["results"],
        url_field="url",
        extra_params={"api_key": "secret"},
        headers={"X-Test": "1"},
    )
    response = _fake_response({"results": []})
    get = MagicMock(return_value=response)
    monkeypatch.setattr("model.objects.searchprovider.requests.get", get)

    provider.search("query", max_results=10)

    assert get.call_args.kwargs["params"] == {"q": "query", "api_key": "secret"}
    assert get.call_args.kwargs["headers"] == {"X-Test": "1"}
