"""
Shared pytest fixtures for the RescueAnt test suite.

IMPORTANT: several project modules (cleaning_service, category_service,
fetching_service) call `config_service.get_config()` at *module import time*
(`config = config_service.get_config()` at the top of the file). That means
a valid Config object must exist in config_service._session_config BEFORE
those modules are imported anywhere - including before pytest collects any
test module that imports them.

To make that work deterministically (without depending on a real bot.config
file on disk, and without caring which order pytest collects test files in),
we inject a fake Config directly into config_service._session_config right
here in conftest.py, at import time of conftest itself. Pytest always
imports conftest.py before collecting sibling test modules, so this runs
first.
"""
import sys
from pathlib import Path

SRC_PATH = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_PATH))

import model.tools.config_service as config_service
from model.objects.category import Category
from model.objects.config import Config

FAKE_SKIP_TAGS = ["script", "style", "noscript", "svg", "iframe", "form"]


def make_fake_category(name="STATION", is_relevant=True, model_id=0, fields=None):
    return Category(
        name=name,
        is_relevant=is_relevant,
        analysis_model_id=model_id,
        analysis_prompt=f"Extract fields for {name}.",
        analysis_max_tokens=40,
        fields=fields if fields is not None else {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        process_links=True,
    )


def make_fake_config():
    return Config(
        categories=[
            make_fake_category("STATION", True, model_id=0),
            make_fake_category("LIST", True, model_id=1, fields={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"station_url": {"type": "string"}},
                    "required": ["station_url"],
                },
            }),
            Category(name="IRRELEVANT", is_relevant=False),
        ],
        category_prompt="Categorize the website.",
        category_max_tokens=20,
        category_context=4096,
        category_model_id=99,
        politeness=1,
        skip_tags=FAKE_SKIP_TAGS,
        starting_url_path="starting_urls.csv",
        database_path="crawl.db",
    )


# Inject immediately, at conftest import time - this is what makes the
# module-level `config = config_service.get_config()` calls in other
# modules safe to import anywhere in the test suite.
config_service._session_config = make_fake_config()
