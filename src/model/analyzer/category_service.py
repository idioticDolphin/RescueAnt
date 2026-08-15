import model.tools.config_service as config_service
import model.tools.llm_service as llm_service
from llama_cpp import LlamaGrammar

from model.analyzer import cleaning_service

config = config_service.get_config()

def categorize_website(html):
    llm_id = config.get_category_model_id()
    llm = llm_service.get_model(llm_id)
    prompt = config.get_category_prompt()
    categories = config.get_categories()
    category_string = '"' + '" | "'.join([category.name for category in categories]) + '"'
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
    print(f"Found category: {found_category}")
    return found_category