from pydantic import BaseModel
from llama_cpp import Llama
from model.objects.category import Category


class Config(BaseModel):
    categories: list[Category]
    category_prompt: str
    category_model_id: int