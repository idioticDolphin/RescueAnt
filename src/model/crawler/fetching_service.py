import os
import asyncio
import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from playwright.async_api import async_playwright
import model.tools.config_service as config_service
from model.tools import data_service, page_store, url_service

logger = logging.getLogger(__name__)

url_queue = [] # save all urls not yet crawled
processed_urls = {}
_robots_cache = {}
_last_request_time:dict[str, float] = {}
_site_counts:dict[str, int] = {}  # pages queued per registrable domain
_site_low_value:dict[str, int] = {}  # low-value pages seen per domain
_productive_sites:set[str] = set()  # domains something was extracted from
abandoned_sites:set[str] = set()  # domains no longer worth a fetch
fetched_content:dict[str, tuple] = {}  # url -> (sha256, page_store path)
# url -> why its fetch failed. Recorded here because the reason is only known
# at the point of failure; auditing a run's failures without it means guessing
# from hostnames.
fetch_errors:dict[str, str] = {}
url_priorities:dict[str, float] = {}   # url -> frontier score (higher first)
# url -> how close this URL is to a page worth extracting, decayed once per
# hop. A page reached from a station is close; a page five links further into
# the open web is not, however plausible its path looks.
url_closeness:dict[str, float] = {}
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

def _serve_from_store(url):
    """Serve a URL from the page store when reuse_stored_pages is on.

    Development runs replay stored bodies rather than refetching them. Returns
    the HTML, or None when reuse is off or no usable stored body exists."""
    if not getattr(config, "reuse_stored_pages", False):
        return None
    max_age_days = getattr(config, "reuse_max_age_days", 0) or 0
    hit = page_store.lookup(url, max_age_seconds=max_age_days * 86400 or None)
    if not hit:
        return None
    html = page_store.load(hit[1])
    if not html:
        return None
    logger.debug("Served %s from the page store (reuse_stored_pages)", url)
    fetched_content[url] = hit
    processed_urls[url] = html
    return html


async def get_content(url, browser=None):
    """
    Fetch and cache page content for a single URL.
    :param url: page to fetch
    :param browser: an already-launched playwright browser instance to reuse;
                     if None, a temporary one is launched just for this call
    """
    if url in processed_urls.keys():
        return processed_urls[url]

    # Checked before robots.txt and politeness on purpose: a stored page costs
    # the site nothing, and asking for its robots.txt again would.
    stored = _serve_from_store(url)
    if stored is not None:
        return stored

    if not _is_allowed(url):
        logger.info("Skipping %s (disallowed by robots.txt)", url)
        fetch_errors[url] = "disallowed by robots.txt"
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
            logger.debug("%s didn't reach networkidle, falling back to 'load'", url)
            try:
                await page.goto(url, wait_until="load", timeout=30000)
                html = await page.content()
            except Exception as e:
                logger.warning("Failed to fetch %s (%s)", url, e)
                # First line only: Playwright appends pages of context that
                # would bury the reason rather than explain it.
                fetch_errors[url] = str(e).strip().splitlines()[0][:300]
                html = ""
        finally:
            await page.close()
    finally:
        if own_browser:
            await browser.close()
            await playwright.stop()

    # Persist immediately, not after the whole batch: a batch can be thousands
    # of pages, and anything fetched but not yet written to disk is lost if the
    # process stops. Storing here makes every completed fetch durable the
    # moment it happens.
    if html:
        digest, path = page_store.store(html)
        if path:
            fetched_content[url] = (digest, path)
            # Index by URL too, so a later crawl - even against a fresh
            # database - can find this body instead of refetching it.
            page_store.remember(url, digest, path)

    processed_urls[url] = html
    return html

# The browser driver is a separate process that can die mid-batch, taking the
# whole crawl with it ("Connection closed while reading from the driver" ended
# one 3.3-hour run). Restarting it costs seconds; losing the run costs hours.
_FETCH_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 5


async def parse_queue(max_concurrency: int = 4, urls=None):
    """
    Crawl everything currently in the queue, reusing one browser instance.

    With `urls`, that explicit batch is fetched instead and the shared queue is
    left alone - which is what lets one batch be fetched while another is being
    analysed, without the two treading on each other's queue.
    Politeness delay is enforced per-domain, so different domains can
    still be fetched concurrently while same-domain requests are spaced out.

    A failing browser session is retried with a fresh one, skipping pages the
    failed attempt already fetched. If every attempt fails the queue is left
    intact for a later round rather than raising: the caller still has pages
    to persist and process, and a transient browser fault must not end the run.
    """
    global url_queue
    batch = list(url_queue) if urls is None else list(urls)
    # Serve what the store already holds first, so a round replayed entirely
    # from disk never starts a browser at all.
    for url in batch:
        if url not in processed_urls:
            _serve_from_store(url)
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        pending = [url for url in batch if url not in processed_urls]
        if not pending:
            break
        try:
            await _fetch_all(pending, max_concurrency)
            break
        except Exception as e:
            if attempt == _FETCH_ATTEMPTS:
                logger.error(
                    "Browser session failed %d time(s) (%s) - leaving %d URL(s) "
                    "queued for a later round.", attempt, e, len(pending))
                return
            logger.warning(
                "Browser session failed (%s) - restarting it, attempt %d of %d.",
                e, attempt + 1, _FETCH_ATTEMPTS)
            if _RETRY_BACKOFF_SECONDS:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

    if urls is None:
        url_queue = []


async def _fetch_all(urls, max_concurrency, browser=None):
    """
    Fetch the given URLs through one browser instance.

    Every page is given a deadline of its own. Playwright's navigation timeout
    bounds page.goto, not the whole fetch: one wedged page - its browser
    process spinning on 10,000 seconds of CPU - left an overnight run blocked
    for eleven hours with nothing in the log. A page that overruns is recorded
    as a failed fetch, like any other, and the rest of the batch goes on.
    """
    timeout = getattr(config, "fetch_timeout_seconds", 0) or None
    semaphore = asyncio.Semaphore(max_concurrency)

    async def fetch_one(url):
        async with semaphore:
            try:
                await asyncio.wait_for(get_content(url, browser=browser), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Fetching %s timed out after %ss - abandoning it", url, timeout)
                fetch_errors[url] = f"fetch timed out after {timeout}s"
                processed_urls[url] = ""

    if browser is not None:
        await asyncio.gather(*(fetch_one(url) for url in urls))
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            await asyncio.gather(*(fetch_one(url) for url in urls))
        finally:
            await browser.close()

def _scheme_twin(url:str):
    """The same URL under the other web scheme, or None if it has neither."""
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return None


def mark_processed(url:str):
    """
    Record a URL as already handled this run, so it is never queued again.

    Used for pages resumed from the page store: their content is already on
    disk and being processed, but link discovery elsewhere in the run would
    otherwise queue the same URL for a fresh fetch. That produced a second
    crawl row and a second set of records for pages that were already done.

    Canonicalizes first, because queue_url() canonicalizes before checking -
    registering a raw URL here would leave the guard silently ineffective.
    """
    if not url:
        return
    processed_urls.setdefault(
        url_service.canonicalize(url, drop_params=config.drop_query_params), None)


def record_page_value(url:str, category):
    """
    Note what a classified page turned out to be, and give up on its site once
    it has produced enough pages of no value and nothing worth extracting.

    The per-site page budget stops one site absorbing a crawl, but only after
    it has taken its whole allowance: one run spent 39 pages on nih.gov, all
    IRRELEVANT, and 34 on an agriculture news site. Five low-value pages in,
    neither was going to become a rescue organisation - while a site that has
    given one record is kept whatever its other pages are, and pages worth
    following for their links (advice, hubs) never count against a site.
    """
    site = url_service.registrable_domain(url)
    limit = getattr(config, "abandon_site_after", 0)
    if not site or not limit or category is None:
        return
    if category.is_relevant:
        _productive_sites.add(site)
        return
    if site in _productive_sites or site in abandoned_sites:
        return
    weight = config.referrer_weights.get(category.name, 0.0)
    if weight > config.abandon_site_max_weight:
        return
    _site_low_value[site] = _site_low_value.get(site, 0) + 1
    if _site_low_value[site] >= limit:
        abandon_site(site)


def abandon_site(site:str):
    """Drop a site's queued URLs and refuse any it is offered later."""
    global url_queue
    abandoned_sites.add(site)
    kept = [u for u in url_queue if url_service.registrable_domain(u) != site]
    dropped = len(url_queue) - len(kept)
    url_queue = kept
    logger.info("Abandoning %s after %d low-value page(s) - dropped %d queued URL(s)",
                site, _site_low_value.get(site, 0), dropped)


def closeness_of(url:str) -> float:
    """How close a URL is known to be to a page worth extracting."""
    if not url:
        return 0.0
    canonical = url_service.canonicalize(url, drop_params=config.drop_query_params)
    return url_closeness.get(canonical, 0.0)


def queue_url(url:str, priority:float=0.0, closeness:float=0.0):
    """
    Add a URL to the fetch queue, skipping it if already queued or fetched.

    The URL is canonicalized first (url_service.canonicalize), so that
    variants naming the same page - trailing slashes, duplicate slashes,
    tracking parameters, directory-index filenames, fragments - collapse to a
    single queue entry and are fetched (and extracted from) only once.

    :param priority: frontier score; higher is crawled sooner. A URL already
                      queued keeps the best score it has been offered.
    :param closeness: how close this URL is to a page worth extracting. Like
                      the score, the best value offered wins: finding a shorter
                      way to a page raises it, a longer way never lowers it.
    """
    global url_queue
    if not url:
        return
    canonical = url_service.canonicalize(url, drop_params=config.drop_query_params)
    # http:// and https:// of the same address are one page. Left unmerged,
    # a site is crawled twice and can even be classified differently each
    # time (observed: the same homepage came back LIST over http and STATION
    # over https). The scheme is not rewritten - sites that only serve http
    # must stay fetchable - only this "already done?" test ignores it.
    if closeness:
        url_closeness[canonical] = max(url_closeness.get(canonical, 0.0), closeness)
    twin = _scheme_twin(canonical)
    for known in (canonical, twin):
        if known and known in url_queue:
            url_priorities[known] = max(url_priorities.get(known, priority), priority)
            return
        if known and known in processed_urls:
            return

    site = url_service.registrable_domain(canonical)
    if site and site in abandoned_sites:
        logger.debug("Skipping %s - site abandoned as unproductive", canonical)
        return
    if site and site in config.domain_denylist:
        logger.debug("Skipping %s - domain is denylisted", canonical)
        return

    # A browser cannot render a PDF or an office document; it starts a
    # download and the fetch fails, after spending a navigation and the
    # politeness delay on a site that may not even be the one being crawled.
    extensions = tuple(getattr(config, "skip_url_extensions", None) or ())
    if extensions and urlparse(canonical).path.lower().endswith(extensions):
        logger.debug("Skipping %s - links to a file, not a page", canonical)
        return

    # Per-site budget: without one, a single large site can dominate a run -
    # the previous run took 80+ pages from one host and ended up blocked by
    # its firewall. Capping pages per site is both politer and spreads the
    # crawl over more distinct sources.
    if config.max_pages_per_site:
        if site and _site_counts.get(site, 0) >= config.max_pages_per_site:
            logger.debug("Skipping %s - per-site budget of %d reached",
                         canonical, config.max_pages_per_site)
            return
        if site:
            _site_counts[site] = _site_counts.get(site, 0) + 1

    url_queue.append(canonical)
    url_priorities[canonical] = priority

def _read_starting_urls(path=config.get_starting_url_path()):
    """Queue the seed URLs from one file or several.

    Seeds arrive in themed sets - a regional directory here, a species network
    there - so a deployment can keep them in separate files and mix them
    without editing anyone else's list. A file that is named but not shipped is
    logged and skipped rather than stopping the run.

    Blank lines and lines starting with '#' are ignored, so the lists can carry
    section comments.
    """
    paths = [path] if isinstance(path, (str, bytes, os.PathLike)) else list(path)
    for one in paths:
        try:
            with open(one, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning("Could not read seed file %s (%s) - skipping it.", one, e)
            continue
        queued = 0
        for line in lines:
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            queue_url(url, priority=100.0)
            queued += 1
        logger.info("Seeded %d URL(s) from %s", queued, one)

def get_crawl_time(url:str):
    """Return the monotonic timestamp of the most recent request to url's domain, or 0.0 if none was made."""
    try:
        return _last_request_time[_get_domain(url)]
    except KeyError:
        return 0.0

def init(starting_url_path:str=config.get_starting_url_path(), redo_failed_fetches:bool=True, redo_all_fetches:bool=False):
    """
    Prepare the module state for a crawl run: mark URLs from prior crawls as
    already processed (so they're skipped) and queue the starting URLs.

    :param starting_url_path: file with one URL per line to seed the queue with
    :param redo_failed_fetches: if True, only URLs from prior *successful*
                                 crawls are skipped - failed ones are retried
    :param redo_all_fetches: if True, no prior crawls are skipped at all;
                              takes priority over redo_failed_fetches
    """
    if not redo_all_fetches: # Priority. True overrides redo_failed_fetches
        if redo_failed_fetches:
            # Only pages that actually reached a terminal state count as done.
            # Using "fetched successfully" here instead would permanently skip
            # pages that were fetched but never categorized or extracted -
            # they would be neither retried nor processed, just lost.
            already_parsed_urls = data_service.get_finished_crawl_urls()
        else:
            already_parsed_urls = data_service.get_crawl_urls()
        for url in already_parsed_urls:
            processed_urls[url] = None
    _read_starting_urls(starting_url_path)