from pydantic import BaseModel

from model.objects.searchprovider import SearchProvider
from model.objects.category import Category
from model.exceptions import *


class Config(BaseModel):
    """Fully parsed, validated runtime configuration for a RescueAnt run.

    Built by model.tools.config_service.load_config() from bot.config; use
    config_service.get_config() to access the current session's instance
    rather than constructing this directly.
    """
    categories: list[Category]
    category_prompt: str
    category_max_tokens: int
    category_context: int
    category_model_id: int
    politeness: int
    skip_tags: list[str]
    starting_url_path: str
    database_path: str
    search_provider: SearchProvider | None = None
    search_query_path: str | None = None
    discover_urls: bool
    results_per_query: int
    query_politeness: float
    redo_all_fetches: bool
    redo_failed_fetches: bool
    discovery_batch_size: int = 5
    max_discovery_batches: int = 0
    max_rounds: int = 0
    max_runtime_seconds: float = 0

    def get_categories(self):
        """Return the list of configured Category objects."""
        return self.categories
    def get_category_prompt(self):
        """Return the LLM prompt used to categorize a website."""
        return self.category_prompt
    def get_category_model_id(self):
        """Return the llm_service model id used for categorization."""
        return self.category_model_id
    def get_politeness(self):
        """Return the minimum delay in seconds between requests to the same domain."""
        return self.politeness
    def get_skip_tags(self):
        """Return the HTML tag names stripped out before cleaning/analysis."""
        return self.skip_tags
    def get_category(self, category_name: str):
        """Return the Category with the given name, or raise CategoryNotFoundError."""
        for category in self.categories:
            if category.name == category_name:
                return category
        else: raise CategoryNotFoundError(category_name)
    def get_starting_url_path(self):
        """Return the path to the file listing the initial URLs to crawl."""
        return self.starting_url_path
    def get_database_path(self):
        """Return the path to the sqlite database file."""
        return self.database_path
    def get_search_provider(self):
        """Return the configured SearchProvider, or None if discovery is disabled."""
        return self.search_provider
    def get_search_query_path(self):
        """Return the path to the query-template file read by discovery_service.read_query_templates()."""
        return self.search_query_path