from model.objects.category import Category
import model.analyzer.cleaning_service as cleaning_service
import model.tools.llm_service as llm_service
import json


def extract_information(html: str, category:Category, base_url: str):
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