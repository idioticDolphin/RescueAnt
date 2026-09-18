"""
Guards on the shipped search_queries.csv itself.

The query file is data, but it fails silently: a block whose locations are
missing produces no queries at all, and nothing in a run says so - discovery
just never asks in that language. These checks are cheap and catch the three
ways an edit to the file goes wrong.
"""
from pathlib import Path

import model.crawler.discovery_service as discovery_service

QUERY_FILE = Path(__file__).parent.parent / "search_queries.csv"


def _blocks(path):
    """[(header comment, [locations], [templates], {directives})] as the parser sees them."""
    blocks, locations, templates, label = [], [], [], "(first block)"
    directives, pending_label = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if set(line) == {"-"} and len(line) >= 3:
            blocks.append((label, locations, templates, directives))
            locations, templates, directives = [], [], {}
            label, pending_label = pending_label or "(unnamed)", None
        elif line.startswith("#"):
            if line.startswith("# ---") and not locations and not templates:
                pending_label = line.strip("# -")
        elif not line:
            continue
        elif line.lower().startswith("location:"):
            locations.append(line.split(":", 1)[1].strip())
        elif line.lower().startswith("set "):
            continue
        elif discovery_service._directive(line):
            key, value = discovery_service._directive(line)
            directives[key] = value
        else:
            templates.append(line)
    blocks.append((label, locations, templates, directives))
    return blocks


def test_every_block_has_both_locations_and_templates():
    empty = [label for label, locations, templates, _ in _blocks(QUERY_FILE)
             if not locations or not templates]
    assert empty == []


def test_every_template_carries_the_location_placeholder():
    # A template without it still works - the location is appended - but in a
    # file this size a missing placeholder is far more likely to be a typo.
    missing = [template for _, _, templates, _ in _blocks(QUERY_FILE)
               for template in templates if "{location}" not in template]
    assert missing == []


def test_every_block_declares_which_language_it_asks_in():
    # A block pairs its templates with its own locations, so the language is
    # the thing that has to be stated once and stated right: it goes to the
    # search engine, and it is what makes a Spanish template on a Japanese
    # location impossible rather than merely unlikely.
    undeclared = [label for label, _, _, directives in _blocks(QUERY_FILE)
                  if not directives.get("language")]
    assert undeclared == []


def test_each_language_is_declared_by_exactly_one_kind_of_block():
    # Two blocks may share a language - English has five, one per part of the
    # world - but a block naming two languages cannot pair either of them with
    # the right places, which is how "Lithuanian, Latvian, Estonian" started.
    for label, _, _, directives in _blocks(QUERY_FILE):
        assert len(directives["language"].split()) == 1, label


def test_the_file_covers_the_world_in_many_languages():
    blocks = _blocks(QUERY_FILE)
    languages = {directives["language"] for _, _, _, directives in blocks}
    assert len(blocks) >= 40
    assert len(languages) >= 35
    assert sum(len(locations) for _, locations, _, _ in blocks) >= 250


def test_interleaved_order_reaches_most_languages_within_one_pass():
    # discovery_batch_size is 5, so the first fifty queries are ten discovery
    # turns. Those ten turns should have asked in ten different languages.
    queries = discovery_service.read_query_templates(str(QUERY_FILE), order="interleave")
    assert len({query.params.get("language") for query in queries[:50]}) >= 10
    assert len({query.params.get("language") for query in queries[:200]}) >= 30
