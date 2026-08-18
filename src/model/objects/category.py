from llama_cpp import Llama
from pydantic import BaseModel

class Category(BaseModel):
    """A page category from bot.config: whether it's relevant to crawl,
    and (if so) the LLM prompt/model/schema used to extract data from it."""
    name: str
    is_relevant: bool
    analysis_model_id: int = None
    analysis_prompt: str = None
    analysis_max_tokens: int = None
    fields: dict = None
    process_links: bool = None
    is_list_category: bool = False