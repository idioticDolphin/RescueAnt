from llama_cpp import Llama
from pydantic import BaseModel

class Category(BaseModel):
    name: str
    is_relevant: bool
    analysis_model_id: int = None
    analysis_prompt: str = None
    fields: dict = None
    process_links: bool = None