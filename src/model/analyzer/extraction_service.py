import logging

from model.objects.category import Category, Relevancy
import model.analyzer.boilerplate_service as boilerplate_service
import model.analyzer.cleaning_service as cleaning_service
import model.analyzer.entity_service as entity_service
import model.tools.config_service as config_service
import model.tools.llm_service as llm_service
import json

logger = logging.getLogger(__name__)
config = config_service.get_config()


class _Mislabeled:
    """Sentinel: the extractor judged the page not to be what it was
    classified as. Distinct from None, which means extraction failed."""

    def __repr__(self):
        return "MISLABELED"


MISLABELED = _Mislabeled()

# Where the mislabel instruction goes in the system prompt. After the category
# prompt: placed before it, the same wording rejected a third of real pages
# (vet clinic and shelter home pages among them) against one in twenty after.
MISLABEL_INSTRUCTION_FIRST = False

# Worded to make rejection the exception. A plain "if the page is not what
# these instructions describe" rejected most real pages with little text on
# them, because a sparse home page does not look like a full record.
DEFAULT_MISLABEL_INSTRUCTION = (
    'Nearly every page given to you has been classified correctly and must be '
    'extracted. Only if you are certain the page is not about what these '
    'instructions describe at all, answer {"mislabeled": true} instead. A home '
    'page, contact page or legal notice of such an organisation must always be '
    'extracted, however little else it contains.'
)


def with_mislabel_option(schema):
    """Offer the model a verdict as an alternative to the record.

    An alternative, not an extra field: every required property of a JSON
    schema is generated whatever its value, so a boolean beside the record
    would cost the full record anyway. {"mislabeled": true} is about six
    tokens against several hundred - which is the entire point, since a
    mislabeled page previously cost a full extraction call.
    """
    verdict = {
        "type": "object",
        "properties": {"mislabeled": {"const": True}},
        "required": ["mislabeled"],
    }
    return {"anyOf": [verdict, schema]}


def _is_mislabel_verdict(data):
    return isinstance(data, dict) and data.get("mislabeled") is True and len(data) == 1


def is_admissible(record):
    """
    Decide whether an extracted record is worth persisting.

    The rule is expressed entirely over *configuration* - `require_fields`
    names fields that must be present, `require_any_role` names field roles
    (see Config.field_semantics) of which at least one member must be
    populated. Nothing here knows what the fields mean, so the same gate
    works for any crawl target.

    Its purpose is to reject records that are really site boilerplate: a page
    that merely repeats the operator's name in a header, with no identifying
    or locating information of its own, cannot be a useful entity record.

    :return: (admissible, reason) - reason is None when admissible.
    """
    if not isinstance(record, dict):
        return False, "record is not an object"

    for field in config.require_fields:
        if not record.get(field):
            return False, f"missing required field {field!r}"

    # Listings mix what the database wants with what it does not, and a name
    # can say which: "Rehkitzrettung ..." is a fawn-rescue group whichever
    # listing it appears on.
    name = str(record.get("name") or "").casefold()
    keepers = getattr(config, "keep_record_name_tokens", None) or ()
    if not any(keeper in name for keeper in keepers):
        for token in getattr(config, "exclude_record_name_tokens", None) or ():
            if token in name:
                return False, f"name contains excluded token {token!r}"

    if config.require_any_role:
        # "any of these roles" - a record qualifies if it carries at least one
        # populated field drawn from the union of the listed roles. Requiring
        # one of *each* role would reject perfectly good records that happen
        # to give, say, a phone number but no postal address.
        candidates = [name for role in config.require_any_role
                      for name in config.fields_with_role(role)]
        if candidates and not any(record.get(name) for name in candidates):
            roles = ", ".join(config.require_any_role)
            return False, f"no populated field with any of the roles: {roles}"

    return True, None


def _salvage_truncated_object(text):
    """
    Recover the complete fields of a record cut off mid-value.

    A single record is one object, so the list salvage above cannot help it and
    the whole page was discarded - a station's name and phone number lost
    because its description ran past the token cap. Closing the object after
    the last field that arrived intact keeps them.
    """
    decoder = json.JSONDecoder()
    depth, in_string, escaped, cuts = 0, False, False, []
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 1:
            cuts.append(index)
    for cut in reversed(cuts):
        try:
            obj, _ = decoder.raw_decode(text[:cut] + "}")
        except ValueError:
            continue
        return obj if obj else None
    return None


def _salvage_truncated_json(raw):
    """
    Recover whatever is parseable from a completion that was cut off.

    Generation stops at max_tokens regardless of where the JSON happens to
    be, so a long list extraction can end mid-object. Rather than discarding
    the whole page's work, keep every complete object that precedes the
    truncation point.

    :return: parsed JSON, or None if nothing can be recovered.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("{"):
        return _salvage_truncated_object(text)
    if not text.startswith("["):
        return None
    decoder = json.JSONDecoder()
    objects, index = [], 1
    while index < len(text):
        while index < len(text) and text[index] in ", \n\r\t":
            index += 1
        if index >= len(text) or text[index] == "]":
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            break  # truncated object - stop, keep what we have
        objects.append(obj)
        index = end
    return objects or None


def extract_information(html: str, category:Category, base_url: str):
    """
    Extract structured data from a page's HTML per its category's field
    schema, using that category's LLM constrained to a JSON response
    matching category.fields.

    :param html: raw page HTML
    :param category: the page's Category (as returned by category_service.categorize_website)
    :param base_url: the page's URL, used to resolve any links extracted for further crawling
    :return: None if the category is IRRELEVANT, or if a CONTENT category's
             extraction failed (e.g. content too long for the model's
             context window, or its completion couldn't be parsed as JSON -
             both logged as a warning, so a single bad page doesn't take
             down the whole crawl run); otherwise a (extracted_data, links)
             tuple, where links is the list of outbound page URLs found
             (empty unless category.process_links) and extracted_data is
             either a dict (or, for list categories, a list of dicts) for a
             CONTENT category, or None for a LINKS category - its own page
             has nothing worth extracting, only the links are useful
    """
    if category.relevancy is Relevancy.IRRELEVANT:
        return None

    links = []
    if category.process_links:
        links = cleaning_service.extract_links(html, base_url)

    if category.relevancy is Relevancy.LINKS:
        return None, links

    site_content = cleaning_service.clean(html)
    # Drop the site's recurring chrome so the model sees this page's own
    # content, not the operator's identity block repeated site-wide.
    site_content = boilerplate_service.strip_for(site_content, base_url, config)
    llm = llm_service.get_model(category.analysis_model_id)
    schema = category.fields
    prompt = f"{category.analysis_prompt} The return schema is {schema}"
    response_schema = schema
    if getattr(config, "mislabel_check", False):
        instruction = getattr(config, 'mislabel_instruction', None) or DEFAULT_MISLABEL_INSTRUCTION
        if MISLABEL_INSTRUCTION_FIRST:
            prompt = f"{instruction} {prompt}"
        else:
            prompt = f"{prompt} {instruction}"
        response_schema = with_mislabel_option(schema)
    # A page too large for prompt + reply is refused outright by llama-cpp,
    # losing it entirely; keep the head, which is where its own content is.
    site_content = llm_service.fit_to_context(
        llm, site_content,
        llm_service.get_context(category.analysis_model_id),
        category.analysis_max_tokens, overhead=prompt)

    try:
        result = llm_service.complete(
            llm,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Website content:\n{site_content}"}
            ],
            # A token cap alone does not bound wall-clock time; without this a
            # single page held the whole crawl for 30 minutes and yielded
            # nothing. The budget scales with the tokens this category is
            # allowed to produce, so a long listing is not cut short by a cap
            # sized for a single record. Partial output is still salvaged.
            timeout_seconds=llm_service.budget_seconds(
                category.analysis_max_tokens,
                config.llm_call_timeout_seconds,
                config.min_generation_tokens_per_second),
            response_format={
                "type": "json_object",
                "schema": response_schema
            },
            temperature=0,
            # Without an explicit cap llama-cpp generates until the context
            # window is exhausted; a degenerate repetition then costs tens of
            # thousands of tokens (observed: 90-120 minutes, ending in
            # unparseable truncated JSON). analysis_max_tokens comes straight
            # from the category's configured max_tokens[...].
            max_tokens=category.analysis_max_tokens,
            repeat_penalty=config.repeat_penalty,
        )
        extracted = result['choices'][0]['message']['content']
        logger.debug("Extracted from %s: %s", base_url, extracted)
        try:
            data = json.loads(extracted)
        except ValueError:
            data = _salvage_truncated_json(extracted)
            if data is None:
                raise
            logger.warning("Extraction for %s was truncated - salvaged %d complete record(s)",
                           base_url, len(data))
        if _is_mislabel_verdict(data):
            logger.info("Extractor judged %s not to be a %s", base_url, category.name)
            return MISLABELED, links
        if isinstance(data, list) and category.record_filter_prompt:
            data = _kept_by_filter(data, category, llm, base_url)
        return _filter_admissible(data, base_url), links
    except Exception as e:
        # Note: links computed above (a cheap HTML-parse, independent of the
        # LLM call) are discarded here along with the failed extraction -
        # same trade-off experiments/collect_crawl_metrics.py already
        # documented making for this same failure mode.
        logger.warning("Extraction failed for %s (%s) - skipping", base_url, e)
        return None


def _filter_key(value):
    """Compare names as the model is likely to echo them back."""
    return " ".join(str(value or "").split()).casefold()


def _kept_by_filter(records, category, llm, base_url):
    """
    Ask once which of a listing's entries belong in the database.

    A listing page is right about being a listing and still mixes what is
    wanted with what is not - a page of wildlife stations that also names
    three vets and a zoo. Judging the page cannot fix that, and judging each
    entry separately would cost a call per entry; the entries are short, so
    they fit in one call as a numbered list.

    A failed or unparseable answer keeps every record: dropping real records
    on a transient error is the worse mistake.
    """
    if not records:
        return records
    listing = "\n".join(f"{i}. {str(r.get('name') or '').strip()[:120]}"
                         for i, r in enumerate(records) if isinstance(r, dict))
    # Either form is allowed: a number is shorter, a name cannot drift out of
    # alignment when the list runs to dozens of entries.
    schema = {"type": "array", "items": {"type": ["integer", "string"]}}
    try:
        result = llm_service.complete(
            llm,
            messages=[{"role": "system", "content": category.record_filter_prompt},
                      {"role": "user", "content": listing}],
            response_format={"type": "json_object", "schema": schema},
            temperature=0,
            max_tokens=8 + 4 * len(records),
        )
        answer = json.loads(result['choices'][0]['message']['content'])
        by_name = {_filter_key(r.get("name")): i for i, r in enumerate(records)
                   if isinstance(r, dict)}
        named = set()
        for item in answer if isinstance(answer, list) else []:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                named.add(item)
            elif isinstance(item, str) and _filter_key(item) in by_name:
                named.add(by_name[_filter_key(item)])
    except Exception as e:
        logger.warning("Filtering the entries of %s failed (%s) - keeping all %d",
                       base_url, e, len(records))
        return records
    removals = getattr(category, "record_filter_names_removals", False)
    kept = [r for i, r in enumerate(records) if (i not in named) == bool(removals)]
    if len(kept) != len(records):
        logger.info("Kept %d of %d entries from %s", len(kept), len(records), base_url)
    return kept


def scrub_implausible(record):
    """
    Blank field values that cannot be what their field claims to be.

    Each field declares its shape through `normalize` in the config's field
    semantics, and that declaration is all this needs - a date extracted into
    a telephone field, or a URL into a phone field, is dropped without any
    field name or domain rule appearing in the code.

    Only the offending value is dropped, never the record: a station with one
    misparsed phone number is still a station. Whether what survives is enough
    remains the admissibility gate's decision.

    :return: (cleaned_record, names of dropped fields)
    """
    if not isinstance(record, dict):
        return record, []

    cleaned, dropped = dict(record), []
    for name, value in record.items():
        semantics = config.field_semantics.get(name) or {}
        if semantics.get("role") == "label":
            cleaned[name] = entity_service.tidy_label(
                value, getattr(config, "label_prefixes_to_strip", None) or ())
        normalizer = semantics.get("normalize")
        if not normalizer:
            continue
        # Tidy before judging: a value whose only fault is our own heading
        # marker is a good value, and should not be thrown away for it.
        value = entity_service.tidy(value, normalizer)
        cleaned[name] = value
        if not entity_service.is_plausible(value, normalizer):
            cleaned[name] = ""
            dropped.append(name)
    return cleaned, dropped


def _filter_admissible(data, base_url):
    """
    Drop records that fail the admissibility gate, logging why.

    Returns a filtered list for list extractions, the record itself for an
    admissible single extraction, or None when a single extraction is
    rejected - callers then persist nothing for that page but still keep its
    links.
    """
    if isinstance(data, list):
        kept = []
        for record in data:
            record = _scrubbed(record, base_url)
            admissible, reason = is_admissible(record)
            if admissible:
                kept.append(record)
            else:
                logger.debug("Rejected record from %s: %s", base_url, reason)
        if len(kept) != len(data):
            logger.info("Rejected %d of %d record(s) from %s as inadmissible",
                        len(data) - len(kept), len(data), base_url)
        return kept

    data = _scrubbed(data, base_url)
    admissible, reason = is_admissible(data)
    if not admissible:
        logger.info("Rejected record from %s as inadmissible: %s", base_url, reason)
        return None
    return data


def _scrubbed(record, base_url):
    """Scrub a record's implausible values, reporting what was dropped."""
    cleaned, dropped = scrub_implausible(record)
    if dropped:
        logger.debug("Dropped implausible value(s) from %s: %s",
                     base_url, ", ".join(dropped))
    return cleaned