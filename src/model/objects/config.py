from pydantic import BaseModel
from llama_cpp import Llama
from model.objects.category import Category


class Config(BaseModel):
    categories: list[Category]
    category_prompt: str
    category_max_tokens: int
    category_context: int
    category_model_id: int
    politeness: int
    skip_tags: list[str]

    def get_categories(self):
        return self.categories
    def get_category_prompt(self):
        return self.category_prompt
    def get_category_model_id(self):
        return self.category_model_id
    def get_politeness(self):
        return self.politeness
    def get_skip_tags(self):
        return self.skip_tags