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
        config[0]: config[1].strip("'").strip('"') for config in configs
    }

def _load_config(configs:dict=None):
    if configs is None:
        configs = _read_config()
    categories = []
    try:
        starting_url_path = configs["starting_url_file"]
        database_path = configs["database"]

        politeness = int(configs["politeness"])
        skip_tags = configs["skip_tags"].split(",")
        skip_tags = [s.strip() for s in skip_tags]

        category_prompt = configs["category_prompt"]
        category_max_tokens = int(configs["category_max_tokens"])
        category_context = int(configs["category_context"])
        category_model_path = configs["category_model_path"]
        max_chars = 40

        fields = None
        type_definitions = dict()
        for key in configs.keys():
            if key.strip().startswith("define"):
                type_definitions[key.strip()[len("define"):].strip()] = configs[key]
        type_definitions = _parse_type_definitions(type_definitions)
        if "fields" in configs.keys():
            fields = json.loads(configs["fields"])
            fields = _build_schema(fields, type_definitions)
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
            context = int(configs[f"context[{category}]"])
            print(f"context: {context}")
            check_linked_urls = configs[f"check_linked_urls[{category}]"]=="True"
            print(f"check_linked_urls: {"yes" if check_linked_urls else "no"}")
            if f"fields[{category}]" in configs.keys():
                category_fields = json.loads(configs[f"fields[{category}]"])
                category_fields = _build_schema(category_fields, type_definitions)
                print(f"custom fields for category: {str(category_fields)[:max_chars]}{'...' if len(str(category_fields)) > max_chars else ''}")
            else:
                if fields is None:
                    raise
                category_fields = fields
                print(f"reused fields")
            try:
                if configs[f"is_list_category[{category}]"] == "True":
                    category_fields = _wrap_as_list_schema(category_fields)
            finally:
                pass

            categories.append(
                Category(
                    name=category,
                    is_relevant=is_relevant_category,
                    analysis_model_id=llm_service.get_model_id(model_path, context),
                    analysis_prompt=prompt,
                    process_links=check_linked_urls,
                    fields=category_fields,
                    analysis_max_tokens=max_tokens,
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
            skip_tags=skip_tags,
            starting_url_path=starting_url_path,
            database_path=database_path,
        )

    except Exception as e:
        raise ConfigError from e


def _parse_type_definitions(definitions: dict[str, str]) -> dict[str, list[str]]:
    """
    definitions: e.g. {"animal_type": "MAMMAL|REPTILE|BIRD"}
    returns: {"animal_type": ["MAMMAL", "REPTILE", "BIRD"]}
    """
    return {name: value.split("|") for name, value in definitions.items()}

def _resolve_field_schema(type_str: str, custom_types: dict[str, list[str]]) -> dict:
    """
    Resolves a shorthand type string into a JSON Schema fragment.
    Supports: "string", "list[string]", "list[<custom_type>]", "<custom_type>"
    """
    list_match = re.fullmatch(r"list\[(\w+)]", type_str.strip())
    if list_match:
        inner = list_match.group(1)
        return {
            "type": "array",
            "items": _resolve_field_schema(inner, custom_types)
        }

    if type_str in custom_types:
        return {"type": "string", "enum": custom_types[type_str]}

    # fallback: plain JSON schema primitive (string, boolean, number, etc.)
    return {"type": type_str}

def _build_schema(analyzed_fields: dict[str, str], custom_types: dict[str, list[str]]) -> dict:
    return {
        "type": "object",
        "properties": {
            field: _resolve_field_schema(type_str, custom_types)
            for field, type_str in analyzed_fields.items()
        },
        "required": list(analyzed_fields.keys())
    }

def get_config() -> Config: # Singleton-like getter
    if _session_config is None:
        _load_config()
    return _session_config

def _wrap_as_list_schema(item_schema: dict) -> dict:
    return {
        "type": "array",
        "items": item_schema
    }