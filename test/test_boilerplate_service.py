"""
Tests for site-level boilerplate detection.

The defect this addresses: an organisation's name, phone and address sit in
the header/footer of every page of its site, so after cleaning even a privacy
policy still carries a plausible-looking identity block - which is what makes
unrelated pages classify as entities and extract the operator's own details
over and over.

A per-page content extractor cannot detect this by construction; it needs to
compare pages *across* a site, which is only possible now that fetched pages
are stored.
"""
import pytest

from model.analyzer import boilerplate_service


@pytest.fixture(autouse=True)
def clear_cache():
    boilerplate_service.forget_all()
    yield
    boilerplate_service.forget_all()


HEADER = "Wildtierhilfe Musterstadt e.V.\nTelefon: 0611 123456\nStartseite\nKontakt\nSpenden"
FOOTER = "Impressum | Datenschutz\n(c) 2026 Wildtierhilfe Musterstadt e.V."


def _page(unique_body):
    return f"{HEADER}\n{unique_body}\n{FOOTER}"


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------

def test_lines_on_every_page_are_boilerplate():
    pages = [_page(f"Unique body text number {i}") for i in range(5)]
    template = boilerplate_service.learn(pages, min_pages=3, threshold=0.6)
    assert "Telefon: 0611 123456" in template
    assert "Impressum | Datenschutz" in template


def test_unique_body_lines_are_not_boilerplate():
    pages = [_page(f"Unique body text number {i}") for i in range(5)]
    template = boilerplate_service.learn(pages, min_pages=3, threshold=0.6)
    assert "Unique body text number 2" not in template


def test_nothing_is_learned_from_too_few_pages():
    """With one or two pages there is no way to tell chrome from content."""
    pages = [_page("body a"), _page("body b")]
    assert boilerplate_service.learn(pages, min_pages=3, threshold=0.6) == set()


def test_threshold_controls_strictness():
    pages = [_page(f"body {i}") for i in range(4)] + ["A wholly different page"]
    lenient = boilerplate_service.learn(pages, min_pages=3, threshold=0.5)
    strict = boilerplate_service.learn(pages, min_pages=3, threshold=0.95)
    assert "Kontakt" in lenient
    assert "Kontakt" not in strict


def test_learning_ignores_blank_lines():
    pages = ["a\n\n\nb", "a\n\n\nc", "a\n\n\nd"]
    assert "" not in boilerplate_service.learn(pages, min_pages=3, threshold=0.6)


# ---------------------------------------------------------------------------
# stripping
# ---------------------------------------------------------------------------

def test_strip_removes_boilerplate_lines():
    template = {"Telefon: 0611 123456", "Impressum | Datenschutz"}
    out = boilerplate_service.strip("Telefon: 0611 123456\nReal content\nImpressum | Datenschutz",
                                    template)
    assert out == "Real content"


def test_strip_keeps_text_when_template_is_empty():
    text = "line one\nline two"
    assert boilerplate_service.strip(text, set()) == text


def test_strip_refuses_to_gut_a_page():
    """A page that is almost entirely template is most likely a short page,
    not a page with no content - removing everything would lose real data."""
    template = {"a", "b", "c"}
    text = "a\nb\nc\nd"
    assert boilerplate_service.strip(text, template, min_keep_ratio=0.5) == text


def test_strip_handles_empty_text():
    assert boilerplate_service.strip("", {"a"}) == ""


# ---------------------------------------------------------------------------
# per-site caching
# ---------------------------------------------------------------------------

def test_site_template_is_computed_once_and_cached():
    calls = []

    def loader(site):
        calls.append(site)
        return [_page(f"body {i}") for i in range(4)]

    first = boilerplate_service.for_site("example.com", loader)
    second = boilerplate_service.for_site("example.com", loader)

    assert first == second
    assert calls == ["example.com"]


def test_different_sites_get_different_templates():
    def loader(site):
        return [f"{site} header\nbody {i}" for i in range(4)]

    a = boilerplate_service.for_site("a.com", loader)
    b = boilerplate_service.for_site("b.com", loader)
    assert "a.com header" in a
    assert "a.com header" not in b


def test_missing_site_yields_empty_template():
    assert boilerplate_service.for_site("", lambda s: []) == set()
