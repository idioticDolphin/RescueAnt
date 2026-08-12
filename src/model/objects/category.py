from llama_cpp import Llama
from pydantic import BaseModel

class Category(BaseModel):
    name: str
    is_relevant: bool
    analysis_model: Llama = None
    analysis_prompt: str = None
    fields: dict = None
    max_tokens: int = None
    process_links: bool = None