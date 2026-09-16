import logging
from collections import Counter

import model.tools.config_service as config_service
import model.tools.llm_service as llm_service
from model.tools import url_service
from llama_cpp import LlamaGrammar

from model.analyzer import boilerplate_service, cleaning_service
from model.objects.category import Category

logger = logging.getLogger(__name__)
config = config_service.get_config()

def url_prior(url):
    """
    Return the Category a URL can be assigned without consulting the model,
    or None if the URL carries no decisive signal.

    A page whose path contains one of the configured `url_tokens[exclude]`
    tokens (e.g. a privacy policy or press archive) is not worth an
    extraction call. Which category such pages get is itself configuration
    (`url_prior_category`), so a deployment can route them to a links-only
    category - keeping their outbound links - or discard them outright.

    Returns None when the feature is unconfigured, keeping this a no-op for
    configs written before it existed.
    """
    if not url or not config.url_tokens_exclude or not config.url_prior_category:
        return None
    tokens = url_service.path_tokens(url)
    if not tokens.intersection(config.url_tokens_exclude):
        return None
    try:
        category = config.get_category(config.url_prior_category)
    except Exception as e:
        logger.warning("url_prior_category %r is not a configured category (%s)",
                       config.url_prior_category, e)
        return None
    logger.debug("URL prior assigned %s to %s", category.name, url)
    return category


def categorize_website(html, url=None):
    """
    Classify a page into one of the configured categories using the category
    LLM, constrained by a grammar built from the category names so the model
    can only return a valid category name.

    If `url` is given it is used twice: once for the cheap `url_prior()`
    short-circuit, and once as additional evidence in the prompt - a page's
    path is a strong classification signal on its own, and the model cannot
    use it unless we pass it.

    With `category_votes` > 1 the classification is sampled repeatedly and
    decided by majority; a tie is treated as abstention and falls back to
    `url_prior_category` when one is configured. Classification costs a small
    fraction of an extraction, so paying for extra votes to avoid a wasted
    extraction is a favourable trade.

    :param html: raw page HTML
    :param url: the page's URL, if known
    :return: the matching Category, or None if categorization failed (e.g.
             the page's cleaned content doesn't fit the category model's
             context window, or the model returned something no configured
             category matches) - logged as a warning either way, so a single
             bad page doesn't take down the whole crawl run.
    """
    prior = url_prior(url)
    if prior is not None:
        return prior

    llm_id = config.get_category_model_id()
    llm = llm_service.get_model(llm_id)
    categories = config.get_categories()
    category_string = '"' + '" | "'.join([category.name for category in categories]) + '"'
    prompt = f"{config.get_category_prompt()} The categories are {category_string}"
    grammar = LlamaGrammar.from_string(f'root ::= {category_string}')
    site_content = cleaning_service.clean(html, deduplicate=True)
    site_content = boilerplate_service.strip_for(site_content, url, config)
    if url:
        site_content = f"URL: {url}\n{site_content}"
    max_tokens = config.category_max_tokens
    # An oversized page is refused outright rather than classified, so a huge
    # page would otherwise end up uncategorised and its links never followed.
    site_content = llm_service.fit_to_context(
        llm, site_content, llm_service.get_context(llm_id), max_tokens,
        overhead=prompt)
    votes = max(1, config.category_votes)

    try:
        results = []
        for vote in range(votes):
            result = llm_service.complete(
                llm,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Website content:\n{site_content}"}
                ],
                timeout_seconds=llm_service.budget_seconds(
                    max_tokens, config.llm_call_timeout_seconds,
                    config.min_generation_tokens_per_second),
                grammar=grammar,
                # A single vote is deterministic; only sample when voting.
                temperature=0 if votes == 1 else 0.7,
                max_tokens=max_tokens,
            )
            results.append(result['choices'][0]['message']['content'])

        found_category, decisive = _majority(results)
        if not decisive and config.url_prior_category:
            logger.debug("Split category vote %s for %s - abstaining", results, url)
            return config.get_category(config.url_prior_category)
        logger.debug("Predicted category: %s", found_category)
        return config.get_category(found_category)
    except Exception as e:
        logger.warning("Categorization failed (%s) - skipping this page", e)
        return None


def _majority(votes):
    """
    Return (winning_value, decisive) for a list of votes.

    `decisive` is False when the top value does not hold a strict majority,
    which callers treat as an abstention signal.
    """
    counts = Counter(votes)
    winner, count = counts.most_common(1)[0]
    return winner, count * 2 > len(votes)