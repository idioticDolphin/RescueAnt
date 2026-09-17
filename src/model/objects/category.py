from enum import Enum

from llama_cpp import Llama
from pydantic import BaseModel


class Relevancy(str, Enum):
    """How a page category should be treated once a page is classified into it.

    - CONTENT: the page itself carries information worth extracting (e.g. a
      rescue station's own site, or a page listing several stations).
    - LINKS: the page's own content is not worth extracting, but it may
      link to CONTENT pages (e.g. an umbrella organization's homepage, or a
      directory site whose current page doesn't list stations itself). Only
      outbound links are followed, no LLM extraction call is made.
    - IRRELEVANT: neither the content nor the links are worth pursuing.
    """
    CONTENT = "CONTENT"
    LINKS = "LINKS"
    IRRELEVANT = "IRRELEVANT"


class Category(BaseModel):
    """A page category from bot.config: how relevant it is (see Relevancy),
    and (for CONTENT categories) the LLM prompt/model/schema used to
    extract data from it."""
    name: str
    relevancy: Relevancy
    analysis_model_id: int = None
    analysis_prompt: str = None
    analysis_max_tokens: int = None
    fields: dict = None
    process_links: bool = None
    is_list_category: bool = False
    # A second, focused question asked of pages put in this category. Unless
    # the model gives confirm_accept (from confirm_answers), the page is filed
    # under confirm_fallback instead.
    # Queue the URLs found in this category's records - a listed organisation's
    # own website - at this frontier priority, so they are fetched soon.
    follow_record_urls: bool = False
    record_url_priority: float = 60.0
    confirm_prompt: str = None
    confirm_answers: list[str] = None
    confirm_accept: str = None
    confirm_fallback: str = None

    @property
    def is_relevant(self) -> bool:
        """True if this category's page content should be extracted (CONTENT)."""
        return self.relevancy is Relevancy.CONTENT