"""
URL normalisation and feature extraction.

Everything here is deliberately domain- and language-neutral: it knows about
URL syntax, never about what a site is *about*. Token lists that carry meaning
(which paths are worth crawling, which identify a contact page) live in
configuration/locale packs, not here - this module only supplies the tokens.
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change which page you get back. Callers can pass
# additional ones from config; these are the universally safe defaults.
DEFAULT_DROP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "sessionid", "phpsessid", "jsessionid", "sid",
})

# Directory-index filenames: /dir/index.html and /dir are the same page.
INDEX_SUFFIX_PATTERN = re.compile(
    r"/(?:index|default|home)\.(?:html?|php|shtml|aspx?|jsp)$", re.IGNORECASE
)

# Split a path into words on any non-alphanumeric character.
_TOKEN_SPLIT = re.compile(r"[^0-9a-zÀ-ɏ]+")

# Multi-label public suffixes common enough to matter. This is a pragmatic
# subset, not the full Public Suffix List; registrable_domain() prefers
# tldextract when it is installed and falls back to this.
_MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.za", "org.za", "net.za", "web.za",
    "com.br", "net.br", "org.br", "gov.br",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "co.in", "net.in", "org.in", "gov.in", "ac.in",
    "com.mx", "com.ar", "com.tr", "com.pl", "com.sg", "com.hk", "com.tw",
})

try:  # pragma: no cover - exercised only when the optional dep is present
    import tldextract as _tldextract
    _EXTRACT = _tldextract.TLDExtract(suffix_list_urls=())  # offline snapshot
except Exception:  # pragma: no cover
    _EXTRACT = None


def canonicalize(url: str, drop_params=None, strip_index: bool = True) -> str:
    """
    Return a normalised form of `url` such that URLs naming the same page
    collapse to one string.

    Applies: scheme/host casefolding, `www.` removal, duplicate-slash collapse,
    trailing-slash removal (except root), directory-index removal, fragment
    removal, tracking-parameter removal and query-parameter sorting.

    Malformed input is returned unchanged rather than raising - a crawler must
    never die on a bad href.

    :param drop_params: extra query parameter names to strip, in addition to
                         DEFAULT_DROP_PARAMS (compared casefolded)
    :param strip_index: remove directory-index filenames such as /index.html
    """
    if not url:
        return url
    try:
        split = urlsplit(url.strip())
        if not split.scheme or not split.netloc:
            return url

        host = split.netloc.casefold()
        if host.startswith("www."):
            host = host[4:]

        path = re.sub(r"/{2,}", "/", split.path) or "/"
        if strip_index:
            path = INDEX_SUFFIX_PATTERN.sub("", path) or "/"
        if path != "/":
            path = path.rstrip("/") or "/"

        drop = set(DEFAULT_DROP_PARAMS)
        if drop_params:
            drop |= {p.casefold() for p in drop_params}
        params = [
            (k, v) for k, v in parse_qsl(split.query, keep_blank_values=True)
            if k.casefold() not in drop
        ]
        query = urlencode(sorted(params))

        return urlunsplit((split.scheme.casefold(), host, path, query, ""))
    except Exception:
        return url


def path_tokens(url: str) -> set[str]:
    """
    Return the set of casefolded word tokens in a URL's *path* (never its host).

    Used to score and filter pages against configured token lists. Uses
    str.casefold() rather than str.lower() so that non-ASCII paths normalise
    correctly (German ß -> ss, Turkish İ, etc.).
    """
    if not url:
        return set()
    try:
        path = urlsplit(url).path
    except Exception:
        return set()
    return {tok for tok in _TOKEN_SPLIT.split(path.casefold()) if tok}


def registrable_domain(url: str) -> str:
    """
    Return the registrable domain (eTLD+1) of `url`, e.g.
    "rlp.nabu.de" -> "nabu.de". Returns "" for malformed input.

    Used as the default site-grouping key, so that every page of one
    organisation's website is recognised as belonging to one site.
    """
    if not url:
        return ""
    try:
        host = urlsplit(url).netloc.casefold()
    except Exception:
        return ""
    if not host:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    if _EXTRACT is not None:  # pragma: no cover - optional dependency
        parts = _EXTRACT(host)
        if parts.domain and parts.suffix:
            return f"{parts.domain}.{parts.suffix}"

    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def url_depth(url: str) -> int:
    """Return the number of non-empty path segments ("/a/b/c" -> 3)."""
    if not url:
        return 0
    try:
        path = urlsplit(url).path
    except Exception:
        return 0
    return len([seg for seg in path.split("/") if seg])


def same_site(a: str, b: str) -> bool:
    """True if two URLs share a registrable domain (and it is non-empty)."""
    da = registrable_domain(a)
    return bool(da) and da == registrable_domain(b)
