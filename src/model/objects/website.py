from pydantic import BaseModel
from model.objects.category import Category


class Website(BaseModel):
    url:str
    html:str
    crawl_time:float
    category:Category|None=None