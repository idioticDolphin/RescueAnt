from model.objects.category import Category
import model.analyzer.cleaning_service as cleaning_service
import model.tools.llm_service as llm_service
import json


def extract_information(html: str, category:Category, base_url: str):
    """
    Extract structured data from a page's HTML per its category's field
    schema, using that category's LLM constrained to a JSON response
    matching category.fields.

    :param html: raw page HTML
    :param category: the page's Category (as returned by category_service.categorize_website)
    :param base_url: the page's URL, used to resolve any links extracted for further crawling
    :return: None if the category is not relevant; otherwise a
             (extracted_data, links) tuple, where extracted_data is a dict
             (or, for list categories, a list of dicts) and links is the
             list of outbound page URLs found (empty unless category.process_links)
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
    print(f"Extracted: {extracted}")
    return json.loads(extracted), links