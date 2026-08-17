from model.analyzer import cleaning_service


# ---------------------------------------------------------------------------
# extract_links
# ---------------------------------------------------------------------------

def test_extract_links_resolves_relative_urls():
    html = '<a href="/about">About</a>'
    links = cleaning_service.extract_links(html, "http://example.com/page")
    assert links == ["http://example.com/about"]


def test_extract_links_keeps_absolute_outbound_urls():
    html = '<a href="http://other.com/x">Other</a>'
    links = cleaning_service.extract_links(html, "http://example.com/page")
    assert links == ["http://other.com/x"]


def test_extract_links_filters_mailto_and_tel():
    html = """
    <a href="mailto:info@example.com">Mail</a>
    <a href="tel:+491234">Call</a>
    <a href="/real-page">Page</a>
    """
    links = cleaning_service.extract_links(html, "http://example.com/")
    assert links == ["http://example.com/real-page"]


def test_extract_links_filters_javascript_and_fragment_links():
    html = """
    <a href="#section2">Jump</a>
    <a href="javascript:void(0)">Click</a>
    <a href="/valid">Valid</a>
    """
    links = cleaning_service.extract_links(html, "http://example.com/")
    assert links == ["http://example.com/valid"]


def test_extract_links_deduplicates_and_sorts():
    html = """
    <a href="/b">B</a>
    <a href="/a">A</a>
    <a href="/a">A again</a>
    """
    links = cleaning_service.extract_links(html, "http://example.com/")
    assert links == ["http://example.com/a", "http://example.com/b"]


def test_extract_links_empty_html_returns_empty_list():
    assert cleaning_service.extract_links("<html><body></body></html>", "http://example.com/") == []


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------

def test_clean_strips_configured_skip_tags():
    html = "<html><body><script>evil()</script><p>Real content</p></body></html>"
    result = cleaning_service.clean(html)
    assert "evil()" not in result
    assert "Real content" in result


def test_clean_inlines_links_with_surrounding_text():
    html = '<p>Contact <a href="mailto:info@example.com">us</a></p>'
    result = cleaning_service.clean(html)
    assert "us (mailto:info@example.com)" in result


def test_clean_inlines_link_with_no_text_using_href_only():
    html = '<a href="/x"></a>'
    result = cleaning_service.clean(html)
    assert "(/x)" in result


def test_clean_marks_list_items():
    html = "<ul><li>Dogs</li><li>Cats</li></ul>"
    result = cleaning_service.clean(html)
    lines = result.splitlines()
    assert lines == ["- Dogs", "- Cats"]


def test_clean_marks_headings_with_hash_prefix():
    # Same root cause as test_clean_marks_list_items above, applied to
    # heading markers - the "#"/"##" prefix lands on its own line rather
    # than prefixing the heading text.
    html = "<h1>Main title</h1><h2>Subtitle</h2>"
    result = cleaning_service.clean(html)
    lines = result.splitlines()
    assert lines == ["# Main title", "## Subtitle"]


def test_clean_renders_table_rows_as_pipe_separated():
    html = """
    <table>
      <tr><th>Species</th><th>Accepted</th></tr>
      <tr><td>Dogs</td><td>Yes</td></tr>
    </table>
    """
    result = cleaning_service.clean(html)
    lines = result.splitlines()
    assert "Species | Accepted" in lines
    assert "Dogs | Yes" in lines


def test_clean_without_deduplication_keeps_repeated_lines():
    html = "<p>Home</p><p>Home</p>"
    result = cleaning_service.clean(html, deduplicate=False)
    assert result.splitlines().count("Home") == 2


def test_clean_with_deduplication_collapses_repeated_lines():
    html = "<p>Home</p><p>Home</p>"
    result = cleaning_service.clean(html, deduplicate=True)
    assert result.splitlines().count("Home") == 1


def test_clean_deduplication_defaults_to_false():
    html = "<p>Home</p><p>Home</p>"
    result = cleaning_service.clean(html)
    assert result.splitlines().count("Home") == 2
