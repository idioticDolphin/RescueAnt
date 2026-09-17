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
    starting_url_path: str | list[str]
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

    # --- URL handling (P2/P7) -------------------------------------------
    # Extra query parameters to strip when canonicalizing, on top of
    # url_service.DEFAULT_DROP_PARAMS.
    drop_query_params: list[str] = []
    # Path tokens marking a page as not worth extracting from. A page whose
    # path contains one of these is assigned url_prior_category without
    # spending an LLM call. Language-specific - supply via a locale pack.
    url_tokens_exclude: list[str] = []
    # Path tokens marking a page as a likely identity/contact page. Used to
    # pick evidence pages for site-level extraction.
    url_tokens_identity: list[str] = []
    # Category assigned to pages matching url_tokens_exclude. Should normally
    # name a Relevancy.LINKS category so the page's links are still followed.
    url_prior_category: str | None = None

    # --- Extraction control (P10/P11/P13) -------------------------------
    # A record must contain all of these fields to be persisted.
    require_fields: list[str] = []
    # ...and at least one field of each of these roles (see field_roles).
    require_any_role: list[str] = []
    # field name -> semantics dict, e.g.
    #   {"role": "identifier", "fusion": "union", "normalize": "phone",
    #    "weight": 0.9, "similarity": "ngram_dice"}
    field_semantics: dict[str, dict] = {}
    # Abort a single LLM call after this many seconds (0 = no limit).
    llm_call_timeout_seconds: float = 0
    min_generation_tokens_per_second: float = 0
    # llama.cpp repetition penalty applied to extraction calls.
    repeat_penalty: float = 1.0
    # Constrain extraction output with a JSON-schema grammar.
    grammar_constrained_extraction: bool = True
    # Number of classification samples to majority-vote over (1 = disabled).
    category_votes: int = 1

    # --- Durability (P23/P24/P25) ---------------------------------------
    # Directory holding fetched page bodies, so a run can be resumed and
    # pages can be re-processed without refetching.
    page_store_path: str = "store"
    # Maximum pages to queue per registrable domain (0 = unlimited). Keeps one
    # large site from dominating a run, and keeps request volume per host
    # within what site operators tolerate.
    max_pages_per_site: int = 0
    max_extractions_per_site: int = 0
    # Let extraction answer "this page is not what it was classified as".
    mislabel_check: bool = False
    mislabel_instruction: str | None = None
    # Where a page judged mislabeled is re-filed; None keeps its category.
    mislabeled_category: str | None = None
    # Development only: serve pages already in the store instead of fetching
    # them again. A crawl run this way sees no site changes since the page was
    # stored, which is the point for a reproducible dev run and wrong for a
    # production one.
    reuse_stored_pages: bool = False
    reuse_max_age_days: float = 0

    # --- Boilerplate stripping (P8) --------------------------------------
    # Remove lines that recur across a site's pages (headers/footers carrying
    # the operator's identity block) before classifying or extracting.
    strip_site_boilerplate: bool = False
    boilerplate_min_pages: int = 4
    boilerplate_threshold: float = 0.6
    # Maximum URLs fetched-and-processed in one round (0 = unlimited). Keeps
    # analysis in step with fetching, which is orders of magnitude faster.
    max_batch_size: int = 0
    # Registrable domains never worth crawling (search engines, social
    # networks, consent/CDN infrastructure). Deployment-specific, so it lives
    # in configuration rather than in code.
    domain_denylist: list[str] = []
    # Links whose path ends in one of these are never fetched.
    skip_url_extensions: list[str] = []
    # Stop crawling a site after this many low-value pages with nothing
    # extracted from it; 0 disables. A page is low-value when its category
    # carries a referrer weight at or below abandon_site_max_weight.
    abandon_site_after: int = 0
    abandon_site_max_weight: float = 0.5
    # Frontier weight contributed by the category of the page a link was found
    # on: links from productive pages are crawled first. Category names are
    # user-defined, so this mapping is configuration.
    referrer_weights: dict[str, float] = {}
    # Frontier score given to search-discovered URLs.
    discovery_priority: float = 50.0
    # Run discovery once the best queued score falls below this (None = only
    # when the queue is literally empty).
    discovery_when_below: float | None = None

    def get_field_role(self, field_name: str) -> str | None:
        """Return the declared role of a field, or None if undeclared."""
        return (self.field_semantics.get(field_name) or {}).get("role")

    def fields_with_role(self, role: str) -> list[str]:
        """Return every field name declared with the given role."""
        return [name for name, sem in self.field_semantics.items()
                if sem.get("role") == role]

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