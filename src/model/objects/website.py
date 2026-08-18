from pydantic import BaseModel
from model.objects.category import Category


class Website(BaseModel):
    """A single fetched page, carrying its content and (once known) its category."""
    url:str
    html:str
    crawl_time:float
    category:Category|None=None