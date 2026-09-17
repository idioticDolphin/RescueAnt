import logging
from pathlib import Path
import re

from model.objects.searchprovider import *
from model.exceptions import *
from model.objects.category import Category, Relevancy
from model.objects.config import Config
import model.tools.llm_service as llm_service
import json

logger = logging.getLogger(__name__)

_session_config = None

CONFIG_PATTERN = r"""(?:^|[\n])\s*(?P<left>.+?)\s*=\s*(?P<right>(?:[^;'"]|(?:(?:".*?")|(?:'.*?')))*?);"""
INCLUDE_PATTERN = re.compile(r'(?:^|\n)\s*include\s+["\'](?P<path>[^"\']+)["\']\s*;')


def _read_config(path:Path=Path(__file__).parent.parent.parent.parent / "bot.config",
                 _seen:set|None=None):
    """
    Read a config file into a flat key/value dict.

    Supports `include "other.config";` directives, resolved relative to the
    including file. Includes are read first, so keys in the including file
    override keys from the files it includes; this lets a deployment pull in
    shared schema/lexicon files and then tweak individual values. Cycles are
    detected and ignored.
    """
    path = Path(path)
    _seen = set() if _seen is None else _seen
    resolved = path.resolve()
    if resolved in _seen:
        logger.warning("Ignoring circular config include of %s", path)
        return {}
    _seen.add(resolved)

    with open(path, encoding="utf-8") as f:
        config_string = f.read()

    merged: dict[str, str] = {}
    for include in INCLUDE_PATTERN.finditer(config_string):
        include_path = (path.parent / include.group("path")).resolve()
        try:
            merged.update(_read_config(include_path, _seen))
        except FileNotFoundError:
            logger.warning("Config include not found, skipping: %s", include_path)

    configs = re.findall(CONFIG_PATTERN, config_string)
    own = {c[0]: c[1].strip("'").strip('"') for c in configs if c[0] != "include"}
    merged.update(own)
    logger.debug("Read %d config entries from %s (%d total after includes)",
                 len(own), path, len(merged))
    return merged


def _csv_list(raw:str) -> list[str]:
    """Parse a comma-separated config value into a list of trimmed strings."""
    if not raw:
        return []
    return [item.strip().strip('"').strip("'") for item in raw.split(",") if item.strip()]


def _opt_int(configs:dict, key:str, default:int) -> int:
    try:
        return int(configs[key])
    except Exception:
        return default


def _opt_float(configs:dict, key:str, default:float) -> float:
    try:
        return float(configs[key])
    except Exception:
        return default


def _parse_field_semantics(configs:dict) -> dict[str, dict]:
    """
    Collect `field[<name>] = {...json...};` entries into {field_name: semantics}.

    Semantics are free-form JSON so new keys (role, fusion, normalize, weight,
    similarity, alias_map, ...) can be added without touching the parser. Bad
    JSON is logged and skipped rather than failing the whole config.
    """
    semantics:dict[str, dict] = {}
    for key, value in configs.items():
        key = key.strip()
        if not (key.startswith("field[") and key.endswith("]")):
            continue
        name = key[len("field["):-1].strip()
        try:
            parsed = json.loads(value)
        except Exception as e:
            logger.warning("Ignoring malformed field semantics for %r (%s)", name, e)
            continue
        if isinstance(parsed, dict):
            semantics[name] = parsed
        else:
            logger.warning("Field semantics for %r must be a JSON object", name)
    return semantics

def load_config(configs:dict=None):
    """
    Parse a bot.config-style dict into a Config object and store it as the
    current session config (see get_config()). Raises ConfigError, wrapping
    the original exception, if any required key is missing or malformed.

    :param configs: pre-parsed key/value pairs (as returned by _read_config());
                     if None, bot.config is read and parsed from disk.
    """
    if configs is None:
        configs = _read_config()
    categories = []
    try:
        # One file or several: seeds arrive in themed sets and a deployment
        # should be able to mix them without editing anyone else's list.
        starting_url_path = _csv_list(configs["starting_url_file"])
        if len(starting_url_path) == 1:
            starting_url_path = starting_url_path[0]
        database_path = configs["database"]

        try:
            discover_urls = configs["discover_urls"]
            if discover_urls == "True":
                discover_urls = True
                search_provider = configs["search_provider"]
                search_query_file = configs["search_query_file"]
                results_per_query = int(configs["results_per_query"])
                query_politeness = float(configs["query_politeness"])
                if search_provider == "Google":
                    search_provider = GoogleCustomSearchProvider(
                        api_key=configs["search_api_key"],
                        search_engine_id=configs["search_engine_id"],
                        timeout=float(configs["search_timeout"])
                    )
                else:
                    search_provider = ConfigurableJsonSearchProvider(
                        base_url=configs["search_base_url"],
                        query_param=configs["query_parameters"],
                        result_path=configs["search_result_path"].split(","),
                        url_field=configs["search_url_field"],
                        extra_params=json.loads(configs["search_extra_params"]),
                        headers=json.loads(configs["search_headers"]),
                        timeout=float(configs["search_timeout"])
                    )
            else:
                discover_urls = False
                search_provider = None
                search_query_file = ""
                results_per_query = 0
                query_politeness = 0

        except Exception as e:
            discover_urls = False
            search_provider = None
            search_query_file = ""
            results_per_query = 0
            query_politeness = 0
            logger.warning("Failed to load search provider from config (%s). Search discovery disabled.", e)


        politeness = int(configs["politeness"])
        skip_tags = configs["skip_tags"].split(",")
        skip_tags = [s.strip() for s in skip_tags]
        redo_all_fetches = configs["redo_all_fetches"] == "True"
        redo_failed_fetches = configs["redo_failed_fetches"] == "True"

        # Workflow batching/stopping parameters - optional, default to sensible
        # "unlimited"/"small batch" values so existing bot.config files without
        # them keep working.
        try:
            discovery_batch_size = int(configs["discovery_batch_size"])
        except Exception:
            discovery_batch_size = 5
        try:
            max_discovery_batches = int(configs["max_discovery_batches"])
        except Exception:
            max_discovery_batches = 0
        try:
            max_rounds = int(configs["max_rounds"])
        except Exception:
            max_rounds = 0
        try:
            max_runtime_seconds = float(configs["max_runtime_seconds"])
        except Exception:
            max_runtime_seconds = 0

        # --- optional, domain-neutral tuning keys ---------------------------
        # All default to "feature off", so configs written before these
        # existed keep working unchanged.
        drop_query_params = _csv_list(configs.get("drop_query_params", ""))
        url_tokens_exclude = _csv_list(configs.get("url_tokens[exclude]", ""))
        url_tokens_identity = _csv_list(configs.get("url_tokens[identity]", ""))
        url_prior_category = configs.get("url_prior_category") or None
        require_fields = _csv_list(configs.get("require_fields", ""))
        require_any_role = _csv_list(configs.get("require_any_role", ""))
        field_semantics = _parse_field_semantics(configs)
        llm_call_timeout_seconds = _opt_float(configs, "llm_call_timeout_seconds", 0)
        min_generation_tokens_per_second = _opt_float(configs, "min_generation_tokens_per_second", 0)
        repeat_penalty = _opt_float(configs, "repeat_penalty", 1.0)
        grammar_constrained_extraction = configs.get(
            "grammar_constrained_extraction", "True") == "True"
        category_votes = max(1, _opt_int(configs, "category_votes", 1))
        page_store_path = configs.get("page_store_path", "store")
        max_pages_per_site = _opt_int(configs, "max_pages_per_site", 0)
        max_extractions_per_site = _opt_int(configs, "max_extractions_per_site", 0)
        mislabel_check = configs.get("mislabel_check", "False") == "True"
        reuse_stored_pages = configs.get("reuse_stored_pages", "False") == "True"
        reuse_max_age_days = _opt_float(configs, "reuse_max_age_days", 0)
        mislabeled_category = (configs.get("mislabeled_category") or "").strip().strip('"') or None
        mislabel_instruction = (configs.get("mislabel_instruction") or "").strip().strip('"') or None
        strip_site_boilerplate = configs.get("strip_site_boilerplate", "False") == "True"
        boilerplate_min_pages = _opt_int(configs, "boilerplate_min_pages", 4)
        boilerplate_threshold = _opt_float(configs, "boilerplate_threshold", 0.6)
        max_batch_size = _opt_int(configs, "max_batch_size", 0)
        domain_denylist = _csv_list(configs.get("domain_denylist", ""))
        skip_url_extensions = [e.lower() for e in _csv_list(configs.get("skip_url_extensions", ""))]
        try:
            referrer_weights = {k: float(v) for k, v in
                                json.loads(configs.get("referrer_weights", "{}")).items()}
        except Exception:
            referrer_weights = {}

        category_prompt = configs["category_prompt"]
        category_max_tokens = int(configs["category_max_tokens"])
        category_context = int(configs["category_context"])
        category_model_path = configs["category_model_path"]

        fields = None
        type_definitions = dict()
        for key in configs.keys():
            if key.strip().startswith("define"):
                type_definitions[key.strip()[len("define"):].strip()] = configs[key]
        type_definitions = _parse_type_definitions(type_definitions)
        if "fields" in configs.keys():
            fields = json.loads(configs["fields"])
            fields = _build_schema(fields, type_definitions)

        for category in configs["categories"].split("|"):
            relevancy = Relevancy(configs[f"relevancy[{category}]"])

            if relevancy is Relevancy.IRRELEVANT:
                categories.append(Category(name=category, relevancy=relevancy))
                logger.debug("Loaded category %r (irrelevant, skipped)", category)
                continue

            check_linked_urls = configs[f"check_linked_urls[{category}]"]=="True"

            if relevancy is Relevancy.LINKS:
                # No content extraction happens for this category - only its
                # outbound links matter, so none of the extraction-specific
                # config keys (prompt/model_path/fields/...) are needed.
                categories.append(
                    Category(name=category, relevancy=relevancy, process_links=check_linked_urls)
                )
                logger.debug("Loaded category %r (links-only, process_links=%s)", category, check_linked_urls)
                continue

            prompt = configs[f"prompt[{category}]"]
            model_path = configs[f"model_path[{category}]"]
            max_tokens = int(configs[f"max_tokens[{category}]"])
            context = int(configs[f"context[{category}]"])
            if f"fields[{category}]" in configs.keys():
                category_fields = json.loads(configs[f"fields[{category}]"])
                category_fields = _build_schema(category_fields, type_definitions)
            else:
                if fields is None:
                    raise
                category_fields = fields
            is_list_category = False
            try:
                is_list_category = configs[f"is_list_category[{category}]"] == "True"
                if is_list_category:
                    category_fields = _wrap_as_list_schema(category_fields)
            finally:
                pass

            categories.append(
                Category(
                    name=category,
                    relevancy=relevancy,
                    analysis_model_id=llm_service.get_model_id(model_path, context),
                    analysis_prompt=prompt,
                    process_links=check_linked_urls,
                    fields=category_fields,
                    analysis_max_tokens=max_tokens,
                    is_list_category=is_list_category
                )
            )
            logger.debug(
                "Loaded category %r (list=%s, process_links=%s, model=%s)",
                category, is_list_category, check_linked_urls, model_path,
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
            search_provider=search_provider,
            search_query_path=search_query_file,
            discover_urls=discover_urls,
            results_per_query=results_per_query,
            query_politeness=query_politeness,
            redo_all_fetches = redo_all_fetches,
            redo_failed_fetches = redo_failed_fetches,
            discovery_batch_size = discovery_batch_size,
            max_discovery_batches = max_discovery_batches,
            max_rounds = max_rounds,
            max_runtime_seconds = max_runtime_seconds,
            drop_query_params = drop_query_params,
            url_tokens_exclude = url_tokens_exclude,
            url_tokens_identity = url_tokens_identity,
            url_prior_category = url_prior_category,
            require_fields = require_fields,
            require_any_role = require_any_role,
            field_semantics = field_semantics,
            llm_call_timeout_seconds = llm_call_timeout_seconds,
            min_generation_tokens_per_second = min_generation_tokens_per_second,
            repeat_penalty = repeat_penalty,
            grammar_constrained_extraction = grammar_constrained_extraction,
            category_votes = category_votes,
            page_store_path = page_store_path,
            max_pages_per_site = max_pages_per_site,
            max_extractions_per_site = max_extractions_per_site,
            mislabel_check = mislabel_check,
            reuse_stored_pages = reuse_stored_pages,
            reuse_max_age_days = reuse_max_age_days,
            mislabeled_category = mislabeled_category,
            mislabel_instruction = mislabel_instruction,
            strip_site_boilerplate = strip_site_boilerplate,
            boilerplate_min_pages = boilerplate_min_pages,
            boilerplate_threshold = boilerplate_threshold,
            max_batch_size = max_batch_size,
            domain_denylist = domain_denylist,
            skip_url_extensions = skip_url_extensions,
            referrer_weights = referrer_weights,
            discovery_priority = _opt_float(configs, "discovery_priority", 50.0),
            discovery_when_below = (_opt_float(configs, "discovery_when_below", 0.0)
                                    if "discovery_when_below" in configs else None),
        )
        logger.info(
            "Config loaded: %d categories (%d relevant), discovery=%s",
            len(categories), sum(c.is_relevant for c in categories), discover_urls,
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
    """Return the current session's Config, loading it from bot.config on first access."""
    if _session_config is None:
        load_config()
    return _session_config

def _wrap_as_list_schema(item_schema: dict) -> dict:
    return {
        "type": "array",
        "items": item_schema
    }