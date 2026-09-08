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

    @property
    def is_relevant(self) -> bool:
        """True if this category's page content should be extracted (CONTENT)."""
        return self.relevancy is Relevancy.CONTENT