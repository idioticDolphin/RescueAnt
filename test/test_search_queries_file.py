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
    """[(header comment, [locations], [templates])] as read_query_templates sees them."""
    blocks, locations, templates, label = [], [], [], "(first block)"
    pending_label = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if set(line) == {"-"} and len(line) >= 3:
            blocks.append((label, locations, templates))
            locations, templates, label = [], [], pending_label or "(unnamed)"
            pending_label = None
        elif line.startswith("#"):
            if line.startswith("# ---") and not locations and not templates:
                pending_label = line.strip("# -")
        elif not line:
            continue
        elif line.lower().startswith("location:"):
            locations.append(line.split(":", 1)[1].strip())
        else:
            templates.append(line)
    blocks.append((label, locations, templates))
    return blocks


def test_every_block_has_both_locations_and_templates():
    empty = [label for label, locations, templates in _blocks(QUERY_FILE)
             if not locations or not templates]
    assert empty == []


def test_every_template_carries_the_location_placeholder():
    # A template without it still works - the location is appended - but in a
    # file this size a missing placeholder is far more likely to be a typo.
    missing = [template for _, _, templates in _blocks(QUERY_FILE)
               for template in templates if "{location}" not in template]
    assert missing == []


def test_the_file_covers_the_world_in_many_languages():
    blocks = _blocks(QUERY_FILE)
    assert len(blocks) >= 30
    assert sum(len(locations) for _, locations, _ in blocks) >= 250


def test_interleaved_order_reaches_most_languages_within_one_pass():
    # discovery_batch_size is 5, so the first fifty queries are ten discovery
    # turns. Those ten turns should have asked in ten different languages.
    queries = discovery_service.read_query_templates(str(QUERY_FILE), order="interleave")
    first_of_each_block = {block[2][0].replace("{location}", "").strip()
                           for block in _blocks(QUERY_FILE)}
    asked = {q for q in queries[:50]
             for template in first_of_each_block if template and q.startswith(template)}
    assert len(asked) >= 30
