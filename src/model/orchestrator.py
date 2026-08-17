import model.tools.data_service as data_service
import model.tools.config_service as config_service
import model.crawler.fetching_service as fetching_service
import asyncio
import model.analyzer.category_service as category_service
import model.analyzer.extraction_service as extraction_service
from model.objects.website import Website


def init():
    config = config_service.get_config()
    data_service.init_db()
    fetching_service.init(
        starting_url_path = config.get_starting_url_path(),
        redo_failed_fetches=config.redo_failed_fetches,
        redo_all_fetches=config.redo_all_fetches
    )

def process_batch(urls:list[str]|None=None):

    ### Fetching
    if urls is not None:
        for url in urls:
            fetching_service.queue_url(url)
    batch = fetching_service.url_queue
    asyncio.run(fetching_service.parse_queue())
    websites = []
    crawl_ids = {}
    for url in batch:
        html = asyncio.run(fetching_service.get_content(url))
        crawl_time = fetching_service.get_crawl_time(url)
        fetch_success = bool(html)
        crawl_id = data_service.save_crawl_instance(url, crawl_time, fetch_success)
        crawl_ids[url] = crawl_id
        if fetch_success:
            websites.append(
                Website(
                    url = url,
                    crawl_time= crawl_time,
                    html = html,
                )
            )

    ### Categorization
    for website in websites:
        category = category_service.categorize_website(website.html)
        website.category = category
        url = website.url
        category_name = category.name
        crawl_id = crawl_ids[url]
        data_service.save_site_category(crawl_id, category_name)

    ### Extraction
    for website in websites:
        url = website.url
        category = website.category
        crawl_id = crawl_ids[url]
        extracted = extraction_service.extract_information(website.html, website.category, website.url)
        if extracted:
            extracted, links = extracted
            if category.is_list_category:
                for entry in extracted:
                    data_service.save_extraction(crawl_id, entry)
            else:
                data_service.save_extraction(crawl_id, extracted)
            for link in links:
                fetching_service.queue_url(link)

# TODO: add function to call another discovery search
# TODO: add function to keep repeating the workflow until stopping condition (time, times discovered, rounds, empty queue...)