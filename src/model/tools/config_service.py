from pathlib import Path
import re
from model.exceptions import *
from model.objects.category import Category
from model.objects.config import Config
import model.tools.llm_service as llm_service
import json

_session_config = None

def _read_config(path:Path=Path(__file__).parent.parent.parent.parent / "bot.config"):
    with open(path) as f:
        config_string = f.read()
        config_pattern = r"""(?:^|[\n])\s*(?P<left>.+?)\s*=\s*(?P<right>(?:[^;'"]|(?:(?:".*?")|(?:'.*?')))*?);"""
        configs = re.findall(config_pattern, config_string)
        print(f"read configs: {configs}")
    return {
        config[0]: config[1] for config in configs
    }

def _load_config(configs:dict=None):
    if configs is None:
        configs = _read_config()
    categories = []
    try:
        politeness = int(configs["politeness"])
        skip_tags = configs["skip_tags"].split(",")
        skip_tags = [s.strip() for s in skip_tags]

        category_prompt = configs["category_prompt"]
        category_max_tokens = int(configs["category_max_tokens"])
        category_context = int(configs["category_context"])
        category_model_path = configs["category_model_path"]
        max_chars = 40
        fields = None
        if "fields" in configs.keys():
            fields = json.loads(configs["fields"])
            print(f"General fields: {str(fields)[:max_chars]}{'...' if len(str(fields)) > max_chars else ''}")

        for category in configs["categories"].split("|"):
            print(f"Found category {category}:")
            is_relevant_category = configs[f"relevancy[{category}]"]=="True"
            print(f"relevant: {"yes" if is_relevant_category else "no"}")
            if not is_relevant_category:
                if configs[f"relevancy[{category}]"]=="False":
                    categories.append(Category(name=category, is_relevant=is_relevant_category))
                    continue
                raise

            prompt = configs[f"prompt[{category}]"]
            print(f"prompt: {prompt[:max_chars]}{'...' if len(prompt) > max_chars else ''}")
            model_path = configs[f"model_path[{category}]"]
            print(f"model_path: {model_path}")
            max_tokens = int(configs[f"max_tokens[{category}]"])
            print(f"max_tokens: {max_tokens}")
            context = configs[f"context[{category}]"]
            print(f"context: {context}")
            check_linked_urls = configs[f"check_linked_urls[{category}]"]=="True"
            print(f"check_linked_urls: {"yes" if check_linked_urls else "no"}")
            if f"fields[{category}]" in configs.keys():
                category_fields = json.loads(configs[f"fields[{category}]"])
                print(f"custom fields for category: {str(category_fields)[:max_chars]}{'...' if len(str(category_fields)) > max_chars else ''}")
            else:
                if fields is None:
                    raise
                category_fields = fields
                print(f"reused fields")

            categories.append(
                Category(
                    name=category,
                    is_relevant=is_relevant_category,
                    analysis_model_id=llm_service.get_model_id(model_path, context),
                    process_links=check_linked_urls,
                    fields=category_fields,
                    analysis_max_tokens=max_tokens
                )
            )
        global _session_config
        _session_config = Config(
            categories=categories,
            category_prompt=category_prompt,
            category_model_id=llm_service.get_model_id(category_model_path, category_context),
            politeness=politeness,
            category_max_tokens=category_max_tokens,
            category_context=category_context,
            skip_tags=skip_tags
        )

    except Exception:
        raise ConfigError

def get_config() -> Config: # Singleton-like getter
    if _session_config is None:
        _load_config()
    return _session_config