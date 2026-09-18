"""
The manual is a deliverable, so it is checked like one.

A reference that has drifted is worse than no reference: someone sets a key
that no longer exists, or never finds the one they needed. These checks tie
docs/MANUAL.md to the code it describes - every setting the Config object
carries has to appear there, and every run argument too.
"""
import re
from pathlib import Path

from model.objects.config import Config

MANUAL = Path(__file__).parent.parent / "docs" / "MANUAL.md"
MAIN = Path(__file__).parent.parent / "src" / "main.py"

# Settings named after their config-file spelling rather than the field name,
# because that is what someone writes in bot.config.
SPELLED_DIFFERENTLY = {
    "starting_url_path": "starting_url_file",
    "database_path": "database",
    "search_query_path": "search_query_file",
    "url_tokens_identity": "url_tokens[identity]",
    "url_tokens_exclude": "url_tokens[exclude]",
    "anchor_tokens_identity": "anchor_tokens[identity]",
    "anchor_tokens_exclude": "anchor_tokens[exclude]",
    "category_model_id": "category_model_path",
    "field_semantics": "field[NAME]",
    "categories": "categories",
}
# Read from the taxonomy file per category, and documented there instead.
PER_CATEGORY = {"category_host_tokens", "mislabel_instruction", "mislabeled_category"}


def test_every_setting_is_in_the_manual():
    text = MANUAL.read_text(encoding="utf-8")
    missing = []
    for name in Config.model_fields:
        if name in PER_CATEGORY:
            continue
        spelling = SPELLED_DIFFERENTLY.get(name, name)
        if f"`{spelling}`" not in text:
            missing.append(spelling)
    assert missing == []


def test_every_run_argument_is_in_the_manual():
    text = MANUAL.read_text(encoding="utf-8")
    arguments = set(re.findall(r'"(--[a-z-]+)"', MAIN.read_text(encoding="utf-8")))
    missing = [argument for argument in arguments if f"`{argument}" not in text]
    assert missing == []


def test_the_manual_links_only_to_files_that_exist():
    text = MANUAL.read_text(encoding="utf-8")
    root = MANUAL.parent
    broken = [target for target in re.findall(r"\]\((?!http)([^)#]+)\)", text)
              if not (root / target).exists()]
    assert broken == []
