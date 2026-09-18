from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from model.tools import config_service

CRAWLABLE_SCHEMES = {"http", "https"}
# Anchor text long enough to carry a heading is worth keeping; a whole
# paragraph wrapped in a link is not, and it is only ever scanned for tokens.
MAX_LINK_TEXT = 120
config = config_service.get_config()


def _link_text(a):
    """The words a reader sees on a link: its text, else its title, else its
    image's alt text. A logo linking home has none of the three."""
    for candidate in (a.get_text(" ", strip=True), a.get("title", ""),
                      " ".join(img.get("alt", "") for img in a.find_all("img"))):
        text = " ".join((candidate or "").split())
        if text:
            return text[:MAX_LINK_TEXT]
    return ""


def extract_links_with_text(html, base_url):
    """
    Like extract_links(), but each URL comes with the words that led to it.

    Anchor text is a primary signal in the focused-crawling literature (Lu et
    al. 2016) and the cheapest one available: "Wildtierauffangstationen in
    Bayern" and "Datenschutzerklärung" are worth different places in the
    frontier, and the page has already been parsed.

    Where a page links the same URL more than once - a logo and a menu entry -
    the longest text wins, since the uninformative occurrence is usually the
    empty one.

    :return: [(url, text)] sorted by URL; text is "" when the link has none.
    """
    best = {}
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        try:
            full_url = urljoin(base_url, href)
            scheme = urlparse(full_url).scheme
        except ValueError:
            continue
        if scheme not in CRAWLABLE_SCHEMES:
            continue
        text = _link_text(a)
        if full_url not in best or len(text) > len(best[full_url]):
            best[full_url] = text
    return sorted(best.items())


def extract_links(html, base_url):
    """Pull all outbound page links worth crawling — filters out
    mailto:, tel:, javascript:, and other non-page schemes."""
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue

        try:
            full_url = urljoin(base_url, href)
            scheme = urlparse(full_url).scheme
        except ValueError:
            continue  # e.g. "http://[broken" - one bad href once ended a crawl
        if scheme not in CRAWLABLE_SCHEMES:
            continue  # skips mailto:, tel:, javascript:, ftp:, data:, etc.

        links.add(full_url)

    return sorted(links)

def clean(html, deduplicate=False):
    """
    Turn raw HTML into plain, LLM-friendly text: strips configured skip
    tags, inlines links as "text (href)", marks list items/headings/tables
    explicitly, and collapses whitespace into one line per piece of content.

    :param html: raw page HTML
    :param deduplicate: if True, drop repeated lines (useful for
                         categorization; keep False for extraction so
                         repeated list entries aren't lost)
    """
    soup = BeautifulSoup(html, 'html.parser')

    for tag in soup(config.get_skip_tags()):
        tag.decompose()

    # Inline links with context
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", "")).strip()
        link_text = a.get_text(strip=True)
        if not href or href.startswith("#"):
            continue
        # A tel: href only restates its own link text, so inlining it hands
        # the model a second copy to fold into the field it extracts
        # (observed: '0176-55376864 (tel:+4917655376864)' stored as a phone
        # number). mailto: is kept, because visible text is routinely
        # obfuscated while the href is not.
        if href.lower().startswith("tel:") and link_text:
            a.replace_with(link_text)
            continue
        a.replace_with(f"{link_text} ({href})" if link_text else f"({href})")

    # Mark list items explicitly
    for li in soup.find_all("li"):
        text = li.get_text()
        li.clear()
        li.append(f"- {text}")
    # Mark headings explicitly
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            text = h.get_text()
            h.clear()
            h.append(f"{'#'*level} {text}")

    # Render tables as markdown-ish rows
    for table in soup.find_all("table"):
        rows_text = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            rows_text.append(" | ".join(cells))
        table.replace_with("\n".join(rows_text))

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if deduplicate:  # Don't deduplicate for feature extraction
        seen = set()
        deduped = []
        for line in lines:
            if line not in seen:
                deduped.append(line)
                seen.add(line)

        return "\n".join(deduped)
    return "\n".join(lines)