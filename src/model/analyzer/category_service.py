import logging

import model.tools.config_service as config_service
import model.tools.llm_service as llm_service
from llama_cpp import LlamaGrammar

from model.analyzer import cleaning_service
from model.objects.category import Category

logger = logging.getLogger(__name__)
config = config_service.get_config()

def categorize_website(html):
    """
    Classify a page's HTML into one of the configured categories using the
    category LLM, constrained by a grammar built from the category names so
    the model can only return a valid category name.

    :param html: raw page HTML
    :return: the matching Category
    """
    llm_id = config.get_category_model_id()
    llm = llm_service.get_model(llm_id)
    categories = config.get_categories()
    category_string = '"' + '" | "'.join([category.name for category in categories]) + '"'
    prompt = f"{config.get_category_prompt()} The categories are {category_string}"
    grammar = LlamaGrammar.from_string(f'root ::= {category_string}')
    site_content = cleaning_service.clean(html, deduplicate=True)
    max_tokens = config.category_max_tokens

    result = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Website content:\n{site_content}"}
        ],
        grammar=grammar,
        temperature=0,
        max_tokens=max_tokens,
    )
    found_category = result['choices'][0]['message']['content']
    logger.debug("Predicted category: %s", found_category)
    return config.get_category(found_category)