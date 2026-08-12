from datetime import datetime
from pydantic import BaseModel
from model.objects.category import Category


class Website(BaseModel):
    url:str
    html:str
    content:str=None
    crawl_time:datetime
    category:Category