import asyncio
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from playwright.async_api import async_playwright
import model.tools.config_service as config_service
from model.tools import data_service

url_queue = [] # save all urls not yet crawled
processed_urls = {}
_robots_cache = {}
_last_request_time:dict[str, float] = {}
config = config_service.get_config()
politeness_delay = config.get_politeness()


def _get_domain(url:str):
    return urlparse(url).netloc

def _is_allowed(url, user_agent="*"):
    """Check robots.txt for this URL's domain, caching parsers per domain."""
    domain = _get_domain(url)
    if domain not in _robots_cache:
        rp = RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"
        try:
            rp.set_url(robots_url)
            rp.read()
        except Exception:
            # If robots.txt is unreachable/missing, default to allow
            rp = None
        _robots_cache[domain] = rp

    rp = _robots_cache[domain]
    if rp is None:
        return True
    return rp.can_fetch(user_agent, url)

async def _wait_politely(url):
    """Respect politeness_delay on a per-domain basis (not global)."""
    domain = _get_domain(url)
    last = _last_request_time.get(domain)
    if last is not None:
        elapsed = time.monotonic() - last
        remaining = politeness_delay - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
    _last_request_time[domain] = time.monotonic()

async def get_content(url, browser=None):
    """
    Fetch and cache page content for a single URL.
    :param url: page to fetch
    :param browser: an already-launched playwright browser instance to reuse;
                     if None, a temporary one is launched just for this call
    """
    if url in processed_urls.keys():
        return processed_urls[url]

    if not _is_allowed(url):
        processed_urls[url] = ""
        return ""

    await _wait_politely(url)

    own_browser = browser is None
    if own_browser:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch()

    try: #Get this tryception
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            html = await page.content()
        except Exception:
            print(f"Url {url} failed to wait for networkidle. Trying waiting for load.")
            try:
                await page.goto(url, wait_until="load", timeout=30000)
                html = await page.content()
            except Exception:
                print(f"Url {url} failed to wait for load. Aborting.")
                html = ""
        finally:
            await page.close()
    finally:
        if own_browser:
            await browser.close()
            await playwright.stop()

    processed_urls[url] = html
    return html

async def parse_queue(max_concurrency: int = 4):
    """
    Crawl everything currently in the queue, reusing one browser instance.
    Politeness delay is enforced per-domain, so different domains can
    still be fetched concurrently while same-domain requests are spaced out.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        semaphore = asyncio.Semaphore(max_concurrency)

        async def fetch_one(url):
            async with semaphore:
                await get_content(url, browser=browser)

        global url_queue
        await asyncio.gather(*(fetch_one(url) for url in url_queue))
        await browser.close()

    url_queue = []

def queue_url(url:str):
    global url_queue
    if url not in url_queue and url not in processed_urls.keys():
        url_queue.append(url)

def _read_starting_urls(path=config.get_starting_url_path()):
    with open(path) as f:
        urls = f.readlines()
    for url in urls: queue_url(url.strip())

def get_crawl_time(url:str):
    try:
        return _last_request_time[_get_domain(url)]
    except KeyError:
        return 0.0

def init(starting_url_path:str=config.get_starting_url_path(), redo_failed_fetches:bool=True, redo_all_fetches:bool=False):
    if not redo_all_fetches: # Priority. True overrides redo_failed_fetches
        if redo_failed_fetches:
            already_parsed_urls = data_service.get_successful_crawl_urls()
        else:
            already_parsed_urls = data_service.get_crawl_urls()
        for url in already_parsed_urls:
            processed_urls[url] = None
    _read_starting_urls(starting_url_path)