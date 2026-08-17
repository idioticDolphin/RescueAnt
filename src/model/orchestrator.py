import model.tools.data_service as data_service
import model.tools.config_service as config_service
import model.crawler.fetching_service as fetching_service
from model.objects.website import *
import asyncio
import model.analyzer.category_service as category_service
import model.analyzer.extraction_service as extraction_service


def init():
    config_service.load_config()
    data_service.init_db()
    # Todo: read already finished websites from db
    fetching_service.read_starting_urls()

def process_batch(urls:list[str]|None=None):

    ### Fetching
    if urls is not None:
        for url in urls:
            fetching_service.queue_url(url)
    batch = fetching_service.url_queue
    asyncio.run(fetching_service.parse_queue())
    websites = []
    for url in batch:
        html = asyncio.run(fetching_service.get_content(url))
        crawl_time = fetching_service.get_crawl_time(url)
        if html:
            websites.append(
                Website(
                    url = url,
                    crawl_time= crawl_time,
                    html = html,
                )
            )
        else:
            pass # TODO: Put failed website into db

    ### Categorizing
    for website in websites:
        category = category_service.categorize_website(website.html)
        website.category = category

    for website in websites:
        extracted = extraction_service.extract_information(website.html, website.category, website.url)
        if extracted:
            extracted, links = extracted
            # TODO: write successful extraction data to db
            # TODO: add extracted links to queue

        else:
            pass # TODO: write unsuccessful/irrelevant extraction data into db

# TODO: add function to call another discovery search
# TODO: add function to keep repeating the workflow until stopping condition (time, times discovered, rounds, empty queue...)