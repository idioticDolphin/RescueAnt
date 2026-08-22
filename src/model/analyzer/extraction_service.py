import logging

from model.objects.category import Category
import model.analyzer.cleaning_service as cleaning_service
import model.tools.llm_service as llm_service
import json

logger = logging.getLogger(__name__)


def extract_information(html: str, category:Category, base_url: str):
    """
    Extract structured data from a page's HTML per its category's field
    schema, using that category's LLM constrained to a JSON response
    matching category.fields.

    :param html: raw page HTML
    :param category: the page's Category (as returned by category_service.categorize_website)
    :param base_url: the page's URL, used to resolve any links extracted for further crawling
    :return: None if the category is not relevant, or if extraction failed
             (e.g. content too long for the model's context window, or its
             completion couldn't be parsed as JSON - both logged as a
             warning, so a single bad page doesn't take down the whole
             crawl run); otherwise a (extracted_data, links) tuple, where
             extracted_data is a dict (or, for list categories, a list of
             dicts) and links is the list of outbound page URLs found
             (empty unless category.process_links)
    """
    if not category.is_relevant:
        return None

    links = []
    if category.process_links:
        links = cleaning_service.extract_links(html, base_url)

    site_content = cleaning_service.clean(html)
    llm = llm_service.get_model(category.analysis_model_id)
    schema = category.fields
    prompt = f"{category.analysis_prompt} The return schema is {schema}"

    try:
        result = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Website content:\n{site_content}"}
            ],
            response_format={
                "type": "json_object",
                "schema": schema
            },
            temperature=0,
        )
        extracted = result['choices'][0]['message']['content']
        logger.debug("Extracted from %s: %s", base_url, extracted)
        return json.loads(extracted), links
    except Exception as e:
        # Note: links computed above (a cheap HTML-parse, independent of the
        # LLM call) are discarded here along with the failed extraction -
        # same trade-off experiments/collect_crawl_metrics.py already
        # documented making for this same failure mode.
        logger.warning("Extraction failed for %s (%s) - skipping", base_url, e)
        return None