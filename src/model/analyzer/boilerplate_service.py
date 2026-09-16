"""
Site-level boilerplate (template) detection.

An organisation's identity block - its name, phone number and address -
typically sits in the header or footer of every page on its site. After
cleaning, a privacy policy therefore still looks like a plausible entity
page, which is the mechanism behind two distinct defects: unrelated pages
being classified as entities, and the operator's own details being extracted
again from every subpage.

No per-page content extractor can detect this, because the evidence that a
block is chrome is that it *recurs across the site*. That comparison only
became possible once fetched pages were stored durably, so this module reads
several of a site's pages and treats lines common to most of them as
template.

Everything here is text-statistical: no language-specific rules, no
domain vocabulary.
"""
import logging
from collections import Counter

logger = logging.getLogger(__name__)

_cache: dict[str, set[str]] = {}


def forget_all():
    """Drop cached templates (used by tests and between runs)."""
    _cache.clear()


def learn(page_texts, min_pages=4, threshold=0.6):
    """
    Return the set of lines that recur across a site's pages.

    :param page_texts: cleaned text of several pages from one site
    :param min_pages: below this many pages, return nothing - with one or two
                       samples there is no way to distinguish chrome from
                       content, and guessing would delete real data
    :param threshold: fraction of pages a line must appear on to count as
                       template
    """
    pages = [text for text in page_texts if text]
    if len(pages) < min_pages:
        return set()

    counts = Counter()
    for text in pages:
        # count each distinct line once per page, so a line repeated within
        # one page doesn't look site-wide
        for line in {ln.strip() for ln in text.splitlines() if ln.strip()}:
            counts[line] += 1

    cutoff = threshold * len(pages)
    return {line for line, count in counts.items() if count >= cutoff}


def strip(text, template, min_keep_ratio=0.15):
    """
    Remove template lines from a page's text.

    :param min_keep_ratio: if stripping would leave less than this fraction of
                            the page's lines, return the text unchanged. A page
                            that is almost entirely template is usually just a
                            short page, and gutting it would lose real content.
    """
    if not text or not template:
        return text

    lines = text.splitlines()
    kept = [line for line in lines if line.strip() not in template]
    if not lines:
        return text
    if len(kept) / len(lines) < min_keep_ratio:
        logger.debug("Skipping boilerplate strip: would remove %d of %d lines",
                     len(lines) - len(kept), len(lines))
        return text
    return "\n".join(kept)


def for_site(site, loader, min_pages=4, threshold=0.6):
    """
    Return (and cache) the template for one site.

    :param loader: callable taking a site key and returning that site's cleaned
                    page texts. Called at most once per site per run.
    """
    if not site:
        return set()
    if site in _cache:
        return _cache[site]

    try:
        pages = loader(site)
    except Exception as e:
        logger.warning("Could not load pages for %s to learn its template (%s)", site, e)
        pages = []

    template = learn(pages, min_pages=min_pages, threshold=threshold)
    if template:
        logger.debug("Learned %d template line(s) for %s from %d page(s)",
                     len(template), site, len(pages))
    _cache[site] = template
    return template


def _load_site_pages(site, sample=8):
    """Load and clean a sample of a site's stored pages.

    Imports are deferred to keep this module free of import cycles with the
    services that use it.
    """
    from model.analyzer import cleaning_service
    from model.tools import data_service, page_store

    texts = []
    for path in data_service.get_site_content_paths(site, limit=sample):
        html = page_store.load(path)
        if html:
            texts.append(cleaning_service.clean(html, deduplicate=True))
    return texts


def site_template(site, min_pages=4, threshold=0.6):
    """Template for a site, learned from its stored pages and cached."""
    return for_site(site, _load_site_pages, min_pages=min_pages, threshold=threshold)


def strip_for(text, url, config):
    """
    Convenience wrapper: strip `url`'s site template from `text`, honouring
    the configuration. A no-op when disabled, when the site has too few
    stored pages, or when stripping would gut the page.
    """
    if not getattr(config, "strip_site_boilerplate", False) or not text:
        return text
    from model.tools import url_service
    site = url_service.registrable_domain(url or "")
    if not site:
        return text
    template = site_template(site,
                             min_pages=getattr(config, "boilerplate_min_pages", 4),
                             threshold=getattr(config, "boilerplate_threshold", 0.6))
    return strip(text, template)
