from pydantic import BaseModel
from model.objects.category import Category
from model.exceptions import *


class Config(BaseModel):
    categories: list[Category]
    category_prompt: str
    category_max_tokens: int
    category_context: int
    category_model_id: int
    politeness: int
    skip_tags: list[str]
    starting_url_path: str
    database_path: str

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
    def get_category(self, category_name: str):
        for category in self.categories:
            if category.name == category_name:
                return category
        else: raise CategoryNotFoundError(category_name)
    def get_starting_url_path(self):
        return self.starting_url_path
    def get_database_path(self):
        return self.database_path