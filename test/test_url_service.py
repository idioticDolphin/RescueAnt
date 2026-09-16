from model.tools import url_service


# ---------------------------------------------------------------------------
# canonicalize
# ---------------------------------------------------------------------------

def test_canonicalize_strips_trailing_slash():
    assert url_service.canonicalize("http://example.com/presse/") == \
           url_service.canonicalize("http://example.com/presse")


def test_canonicalize_keeps_root_slash():
    assert url_service.canonicalize("http://example.com/") == "http://example.com/"


def test_canonicalize_drops_www():
    assert url_service.canonicalize("http://www.example.com/a") == \
           url_service.canonicalize("http://example.com/a")


def test_canonicalize_collapses_duplicate_slashes():
    assert url_service.canonicalize("http://example.com//greifvogelhilfe") == \
           url_service.canonicalize("http://example.com/greifvogelhilfe")


def test_canonicalize_drops_tracking_params():
    url = "http://example.com/a?utm_source=x&fbclid=y"
    assert url_service.canonicalize(url) == "http://example.com/a"


def test_canonicalize_drops_configured_params():
    url = "http://example.com/greifvogelhilfe?rCH=2"
    assert url_service.canonicalize(url, drop_params={"rch"}) == \
           "http://example.com/greifvogelhilfe"


def test_canonicalize_keeps_meaningful_params():
    url = "http://example.com/list?page=2"
    assert "page=2" in url_service.canonicalize(url)


def test_canonicalize_sorts_query_params():
    a = url_service.canonicalize("http://example.com/x?b=2&a=1")
    b = url_service.canonicalize("http://example.com/x?a=1&b=2")
    assert a == b


def test_canonicalize_strips_index_suffix():
    assert url_service.canonicalize("http://example.com/dir/index.html") == \
           url_service.canonicalize("http://example.com/dir")


def test_canonicalize_drops_fragment():
    url = "http://example.com/a#cmplz-cookies-overview"
    assert url_service.canonicalize(url) == "http://example.com/a"


def test_canonicalize_lowercases_host_only_not_path():
    out = url_service.canonicalize("http://EXAMPLE.com/CaseSensitive")
    assert out == "http://example.com/CaseSensitive"


def test_canonicalize_is_idempotent():
    once = url_service.canonicalize("http://www.example.com//a/index.html?utm_source=x#frag")
    assert url_service.canonicalize(once) == once


def test_canonicalize_handles_garbage_gracefully():
    assert url_service.canonicalize("not a url") == "not a url"


# ---------------------------------------------------------------------------
# path_tokens
# ---------------------------------------------------------------------------

def test_path_tokens_splits_on_separators():
    toks = url_service.path_tokens("http://example.com/wir-ueber-uns/kontakt.html")
    assert "wir" in toks and "ueber" in toks and "uns" in toks and "kontakt" in toks


def test_path_tokens_is_casefolded():
    assert "presse" in url_service.path_tokens("http://example.com/Presse")


def test_path_tokens_excludes_host():
    toks = url_service.path_tokens("http://presse.example.com/a")
    assert "presse" not in toks


def test_path_tokens_handles_unicode_casefold():
    # casefold() maps ß -> ss, which .lower() does not
    toks = url_service.path_tokens("http://example.com/STRASSE")
    assert "strasse" in toks


def test_path_tokens_empty_for_root():
    assert url_service.path_tokens("http://example.com/") == set()


# ---------------------------------------------------------------------------
# registrable_domain
# ---------------------------------------------------------------------------

def test_registrable_domain_simple():
    assert url_service.registrable_domain("http://www.example.com/a") == "example.com"


def test_registrable_domain_subdomain():
    assert url_service.registrable_domain("https://rlp.nabu.de/x") == "nabu.de"


def test_registrable_domain_multi_label_suffix():
    assert url_service.registrable_domain("http://shop.example.co.uk/x") == "example.co.uk"


def test_registrable_domain_of_garbage_is_empty():
    assert url_service.registrable_domain("not a url") == ""


# ---------------------------------------------------------------------------
# url_depth
# ---------------------------------------------------------------------------

def test_url_depth_root_is_zero():
    assert url_service.url_depth("http://example.com/") == 0


def test_url_depth_counts_segments():
    assert url_service.url_depth("http://example.com/a/b/c") == 3


# ---------------------------------------------------------------------------
# score_url (frontier ordering)
# ---------------------------------------------------------------------------

def test_identity_tokens_raise_the_score():
    plain = url_service.score_url("http://e.com/x", identity_tokens=["kontakt"])
    contact = url_service.score_url("http://e.com/kontakt", identity_tokens=["kontakt"])
    assert contact > plain


def test_exclude_tokens_lower_the_score():
    plain = url_service.score_url("http://e.com/x", exclude_tokens=["datenschutz"])
    junk = url_service.score_url("http://e.com/datenschutz", exclude_tokens=["datenschutz"])
    assert junk < plain


def test_referrer_weight_is_carried_through():
    low = url_service.score_url("http://e.com/x", referrer_category_weight=0.0)
    high = url_service.score_url("http://e.com/x", referrer_category_weight=5.0)
    assert high - low == 5.0


def test_deeper_urls_score_slightly_lower():
    shallow = url_service.score_url("http://e.com/a")
    deep = url_service.score_url("http://e.com/a/b/c/d")
    assert deep < shallow


def test_productive_referrer_outranks_junk_path_on_shallow_page():
    from_list = url_service.score_url("http://e.com/station", referrer_category_weight=4.0,
                                      exclude_tokens=["presse"])
    junk = url_service.score_url("http://e.com/presse", referrer_category_weight=4.0,
                                 exclude_tokens=["presse"])
    assert from_list > junk
