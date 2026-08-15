from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from model.tools import config_service

CRAWLABLE_SCHEMES = {"http", "https"}
config = config_service.get_config()

def extract_links(html, base_url):
    """Pull all outbound page links worth crawling — filters out
    mailto:, tel:, javascript:, and other non-page schemes."""
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue

        full_url = urljoin(base_url, href)
        if urlparse(full_url).scheme not in CRAWLABLE_SCHEMES:
            continue  # skips mailto:, tel:, javascript:, ftp:, data:, etc.

        links.add(full_url)

    return sorted(links)

def clean(html, deduplicate=False):
    soup = BeautifulSoup(html, 'html.parser')

    for tag in soup(config.get_skip_tags()):
        tag.decompose()

    # Inline links with context
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", "")).strip()
        link_text = a.get_text(strip=True)
        if not href or href.startswith("#"):
            continue
        a.replace_with(f"{link_text} ({href})" if link_text else f"({href})")

    # Mark list items explicitly
    for li in soup.find_all("li"):
        li.insert(0, "- ")

    # Mark headings explicitly
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.insert(0, "#" * level + " ")

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