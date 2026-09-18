# RescueAnt manual

Everything the crawler can be told to do, and every setting that tells it.
The [README](../README.md) covers installing it and getting a first run out;
this is the reference you come back to.

- [Running](#running)
  - [Command-line arguments](#command-line-arguments)
  - [What a run does](#what-a-run-does)
  - [Workflows](#workflows)
- [Configuration files](#configuration-files)
  - [File syntax](#file-syntax)
  - [What lives where](#what-lives-where)
- [Settings reference](#settings-reference)
  - [Core](#core)
  - [Fetching and politeness](#fetching-and-politeness)
  - [Frontier order](#frontier-order)
  - [Budgets](#budgets)
  - [Analysis and the model](#analysis-and-the-model)
  - [Extraction control](#extraction-control)
  - [Durability and reuse](#durability-and-reuse)
  - [Discovery](#discovery)
  - [Stopping conditions](#stopping-conditions)
- [The taxonomy file](#the-taxonomy-file)
- [The schema file](#the-schema-file)
- [The lexicon file](#the-lexicon-file)
- [search_queries.csv](#search_queriescsv)
- [starting_urls.csv](#starting_urlscsv)
- [Recipes](#recipes)

---

## Running

```bash
python src/main.py [CONFIG] [OPTIONS]
```

`CONFIG` is a `bot.config`-style file and defaults to `bot.config` at the
project root. It is read before any model module is imported, so a run always
sees exactly the configuration named on the command line.

### Command-line arguments

| Argument | Effect |
| --- | --- |
| `CONFIG` | Path to the configuration file. Optional; defaults to `bot.config`. |
| *(no option)* | Run the full crawl workflow: seed, fetch, classify, extract, discover, repeat until a stopping condition. |
| `--resolve` | Deduplicate the records already extracted into entities, then exit. No crawling, no network, no model. Safe to re-run after changing field semantics. |
| `--export CSV` | Write the resolved entities to `CSV` and exit. Each row carries an `evidence` column (`own-page`, `listing+own-page`, `listing-only`) and a `review` column (`no-direct-contact`, `conflicting-contact`). |
| `--needing-review` | With `--export`, write only the rows carrying a review flag. |
| `--reprocess extract` | Re-run extraction over pages already in the page store, without refetching. |
| `--reprocess categorize` | Re-run classification (and the extraction that follows it) over stored pages. Use after a taxonomy or prompt change. |
| `--site DOMAIN` | Limit `--reprocess` to one registrable domain, e.g. `--site wildtierhilfe.example`. |
| `--category NAME` | Limit `--reprocess` to pages currently filed under that category. |
| `--index-store DB [DB ...]` | Record the pages stored by these crawl databases in the page store's URL index, then exit, so `reuse_stored_pages` can serve them to later runs. |
| `-v`, `--verbose` | Debug-level logging: raw model output, per-field config parsing, per-URL frontier detail. `llama.cpp`'s own logging stays suppressed either way. |

Only one mode runs per invocation. If several are given, the order of
precedence is `--index-store`, `--export`, `--resolve`, `--reprocess`, then
the full crawl.

Every mode writes into the database named by the config's `database` key, so
point a different config at a different database to keep experiments apart.

### What a run does

`orchestrator.run()` initialises the database, finishes any work left over
from a previous run, seeds the fetch queue, and then alternates:

1. **Crawl what looks promising.** Fetch a batch of at most `max_batch_size`
   URLs, classify each page, extract records from the categories worth
   extracting, and queue the links found. The queue is a priority queue - see
   [Frontier order](#frontier-order).
2. **Discover more.** Once the best queued score falls below
   `discovery_when_below`, run `discovery_batch_size` search queries and queue
   what they return at `discovery_priority`, then go back to 1.

It stops when the queue is empty and no discovery queries are left, or when
one of the [stopping conditions](#stopping-conditions) is hit. An interrupted
run loses nothing: page bodies are written to the page store as they arrive
and each page carries a lifecycle state, so the next run resumes from disk.

### Workflows

```bash
# A full crawl, logging to a file you can tail
python src/main.py bot.config > crawl.log 2>&1

# Re-extract everything already crawled after a prompt or schema change
python src/main.py bot.config --reprocess extract

# Re-classify one site after a taxonomy change
python src/main.py bot.config --reprocess categorize --site example.org

# Deduplicate and export
python src/main.py bot.config --resolve
python src/main.py bot.config --export stations.csv
python src/main.py bot.config --export needs-a-look.csv --needing-review

# Let a later crawl reuse pages stored by earlier ones (development)
python src/main.py bot.config --index-store crawl.db older-crawl.db
```

---

## Configuration files

### File syntax

A configuration file is a list of statements, each ending in a semicolon.

```
key = value;                       # a scalar
key = "a", "b", "c";               # a list
key = {"a": 1.0, "b": -2.0};       # JSON, for anything structured
key[NAME] = value;                 # scoped to a category or a field
include "config/other.config";     # pull in another file
define animal = bird|mammal|fish;  # a named type, usable in a schema
```

- Quotes around a value are optional and stripped; use them when the value
  contains a comma or a semicolon.
- `#` starts a comment.
- A value may span several lines; only the semicolon ends it.
- `include` is resolved relative to the including file. Includes are read
  first, so a key set after an `include` overrides the included one - which is
  how a deployment pulls in a shared taxonomy and then changes two values.
  Include cycles are detected and ignored.
- An unknown key is ignored. A missing *required* key raises `ConfigError`
  naming it; an unparseable optional key falls back to its default.

### What lives where

The shipped configuration is split so that each file can be swapped as a
unit. Nothing but convention requires this - it is all one namespace after the
includes are resolved.

| File | Holds |
| --- | --- |
| `bot.config` | The run: what to crawl, how fast, how much, where to put it. |
| `config/taxonomy.config` | The page categories, their prompts, and what each one is worth. |
| `config/schema.config` | The fields to extract and what each field *means*. |
| `config/lexicon/multilingual.config` | Everything language-bound: URL tokens, name tokens, the domain denylist. |
| `search_queries.csv` | Search-discovery query templates. |
| `starting_urls.csv` | Seed URLs. |

Retargeting the crawler at a different kind of organisation is a matter of
replacing `taxonomy.config` and `schema.config`, and rewriting the lexicon and
the queries. No code changes.

---

## Settings reference

Defaults are what you get when the key is absent. "Required" means the run
refuses to start without it.

### Core

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `starting_url_file` | path or list of paths | required | Seed URL files. Several may be given, so a deployment can add its own list without editing anyone else's. |
| `database` | path | required | The sqlite database. Created if missing. |
| `page_store_path` | path | `store` | Directory holding fetched page bodies, content-addressed and gzipped. |
| `categories` | `A\|B\|C` | required | The page taxonomy. See [the taxonomy file](#the-taxonomy-file). |
| `fields` | JSON object | required | The extraction schema. See [the schema file](#the-schema-file). |

### Fetching and politeness

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `politeness` | seconds | required | Minimum delay between two requests to the same domain. |
| `user_agent` | string | *(RescueAntBot)* | How the crawler names itself when asking a site for its `robots.txt`, and the name those rules are matched against. Pages themselves are fetched by a real browser, which sends its own. |
| `fetch_timeout_seconds` | seconds | `90` | Deadline for one page's whole fetch, not just its navigation. `0` disables it. A wedged browser page once blocked an overnight run for eleven hours. |
| `max_batch_size` | int | `0` | Pages fetched and analysed per round; `0` is unlimited. Fetching is orders of magnitude faster than analysis, so an uncapped round fetches thousands of pages and then grinds through them one at a time. |
| `prefetch_next_batch` | bool | `False` | Fetch the next batch while this one is analysed. A quarter faster in measurement; the cost is that the next batch is chosen before this batch's links are known. |
| `skip_tags` | list | required | HTML tags stripped before analysis. `nav` is worth including; `header` and `footer` are not, since that is where an operator's address usually sits. |
| `skip_url_extensions` | list | *(empty)* | Links whose path ends in one of these are never fetched. A browser downloads rather than renders them, so each one costs a failed navigation. |
| `domain_denylist` | list | *(empty)* | Registrable domains never worth crawling: search engines, social networks, consent and CDN infrastructure. |
| `drop_query_params` | list | *(empty)* | Query parameters stripped when canonicalising a URL, on top of the built-in list. |
| `redo_all_fetches` | bool | required | Refetch pages already in the database. |
| `redo_failed_fetches` | bool | required | Retry pages whose fetch failed. |

### Frontier order

A queued URL's score decides when it is fetched. It is the sum of: the
referrer weight of the category of the page that offered it, plus `2.0` per
identity token in its path, minus `1.5` per exclude token, minus `0.25` per
path segment of depth, minus `leaving_site_penalty` if it leaves its site,
plus any inherited closeness.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `referrer_weights` | JSON object | `{}` | Category name to weight, e.g. `{"LIST": 6.0, "COMMERCIAL": -10.0}`. Links from productive pages are crawled first. |
| `url_tokens[identity]` | list | *(empty)* | Path tokens marking a page likely to carry the operator's own contact details. |
| `url_tokens[exclude]` | list | *(empty)* | Path tokens marking a page not worth extracting from. Such a page is filed as `url_prior_category` without a model call. |
| `anchor_tokens[identity]` | list | *(empty)* | Words in a link's own text that mark its target as worth fetching. The strongest of the cheap signals: over 221,930 links whose target had been crawled, this list fires on 2.1% of them and is right 40.2% of the time, against a 6.0% base rate. Matched as substrings of the casefolded text, once per token. |
| `anchor_tokens[exclude]` | list | *(empty)* | ...and the words that mark it as not worth fetching. |
| `anchor_identity_bonus` | float | `2.0` | Added per identity token found in the link text. |
| `anchor_exclude_penalty` | float | `1.5` | Subtracted per exclude token found in the link text. |
| `url_prior_category` | category | *(none)* | Where those pages go. Should name a links-only category, so their outbound links are still followed. |
| `leaving_site_penalty` | float | `0.0` | Subtracted from a link that leaves its site. Measured on a 14,705-page replay: 497 targets found in the first 2,000 fetches instead of 437, with 689 wasted fetches instead of 765. |
| `leaving_site_exempt_min_weight` | float | `5.0` | A page whose category's referrer weight reaches this is exempt: a listing exists to send you elsewhere. |
| `link_closeness_decay` | float | `0.0` | Multiplies a link's inherited closeness at each hop from an interesting page; `0` turns the idea off. Measured and left off - it changed the order barely at all. |
| `closeness_source_min` | float | `3.0` | A category whose referrer weight reaches this restarts the closeness count from that weight. |

### Budgets

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_pages_per_site` | int | `0` | Page ceiling per registrable domain; `0` is unlimited. An unbudgeted run once spent 424 of 527 fetches on a single pet-supplies retailer. |
| `max_extractions_per_site` | int | `0` | Extraction calls per site. Three still covers a homepage, a contact page and a legal notice; list categories are exempt, since each listing yields different entities. |
| `abandon_site_after` | int | `0` | Stop crawling a site after this many low-value pages with nothing extracted. `0` disables it. |
| `abandon_site_max_weight` | float | `0.5` | What counts as low-value: a page whose category's referrer weight is at or below this. |
| `abandoned_site_penalty` | float | `0.0` | What an abandoned site's links cost from then on. `0` refuses them outright; anything else demotes them instead, so they drain from the queue last but a strong enough referrer can still pull one through. Bergmark et al. (2002) found pages on one topic separated by 1 to 12 irrelevant ones, so refusing a failed host is how a crawler misses whole clusters - a wasted fetch costs seconds, a severed tunnel costs a site. |

### Analysis and the model

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `category_prompt` | string | required | The classification instruction. |
| `category_model_path` | path | required | GGUF model used for classification. |
| `category_context` | int | required | Context window for it. Larger than the card can hold is not an error - llama.cpp spills into system memory and everything becomes eight times slower. |
| `category_max_tokens` | int | required | Output cap for a classification; one category name needs very few. |
| `category_votes` | int | `1` | Classification samples to majority-vote over. |
| `repeat_penalty` | float | `1.0` | llama.cpp repetition penalty for extraction. `1.1` discourages the degenerate repetition that used to run generation to the context limit. |
| `llm_call_timeout_seconds` | seconds | `0` | Floor for a single model call's deadline. A flat cap is a trap: 120 s cut *every* listing extraction, taking one page from 19 records to 5. |
| `min_generation_tokens_per_second` | float | `0` | The deadline scales with the tokens a category may produce, at this rate - a backstop against stalled generation, not against long generation. |
| `grammar_constrained_extraction` | bool | `True` | Constrain extraction output with a JSON-schema grammar. Off, a listing page loses postcode-only addresses and merges array items into one string. |
| `strip_site_boilerplate` | bool | `False` | Remove lines recurring across a site's pages before analysis. This is what stops a header's identity block making every subpage look like its own entity. |
| `boilerplate_min_pages` | int | `4` | Stored pages needed before a site's boilerplate can be told from its content. |
| `boilerplate_threshold` | float | `0.6` | Share of a site's pages a line must appear on to count as boilerplate. |

### Extraction control

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `require_fields` | list | *(empty)* | A record must carry all of these to be stored. |
| `require_any_role` | list | *(empty)* | ...and at least one field of each named role. Together these are what separates an entity from site boilerplate. |
| `field[NAME]` | JSON object | *(none)* | What a field *means*: role, weight, normaliser, fusion. See [the schema file](#the-schema-file). |
| `exclude_record_name_tokens` | list | *(empty)* | A record whose name contains one of these is not admissible. |
| `keep_record_name_tokens` | list | *(empty)* | ...unless the name also carries one of these: a care word outranks a kind word, as in "Igelpflegestation Walter Zoo". |
| `label_prefixes_to_strip` | list | *(empty)* | Dropped from the front of a label: `Contact: DHORNE` becomes `DHORNE`. |
| `category_host_tokens[NAME]` | list | *(empty)* | Substrings of a page's host that decide its category without asking the model. |
| `mislabel_check` | bool | `False` | Let extraction answer "this page is not what it was classified as". Off by default: on 50 labelled pages it caught 24 of 31 misclassified pages and rejected 1 of 19 real ones, and a wrongly rejected page is a lost record. |
| `mislabel_instruction` | string | *(built-in)* | Overrides the wording of that verdict. Wording matters more than the idea - see the README's evaluation section. |
| `mislabeled_category` | category | *(keep)* | Where a page judged mislabeled is re-filed. |

### Durability and reuse

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `skip_identical_content` | bool | `True` | Take the category of an already-analysed page with byte-identical content instead of analysing this one again. |
| `queue_record_urls_at_start` | bool | `True` | Begin a run by queueing the websites named in records that no crawl has visited - 26% of them, in this project's own database. |
| `reuse_stored_pages` | bool | `False` | **Development only.** Serve pages from the store instead of fetching them; no request reaches the site, not even for robots.txt. Reproducible, and wrong for building a real directory. |
| `reuse_max_age_days` | float | `0` | How old a stored page may be and still be served. `0` means any age. |

### Discovery

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `discover_urls` | bool | required | Whether search-engine discovery runs at all. |
| `search_provider` | `"Google"` or other | required if discovering | `"Google"` selects the Custom Search provider; anything else selects the configurable JSON provider (SearXNG, SerpApi, Bing, ...). |
| `search_query_file` | path | required if discovering | See [search_queries.csv](#search_queriescsv). |
| `results_per_query` | int | required if discovering | URLs taken from each query. |
| `query_politeness` | seconds | required if discovering | Delay between queries, rate-limiting the search API rather than the sites. |
| `search_timeout` | seconds | required if discovering | Per-request timeout. |
| `search_base_url` | URL | required (JSON provider) | Where to send the query. |
| `query_parameters` | string | required (JSON provider) | Name of the query parameter, e.g. `q`. |
| `search_result_path` | comma list | required (JSON provider) | Path into the JSON response, e.g. `results`. |
| `search_url_field` | string | required (JSON provider) | Field holding each result's URL. |
| `search_extra_params` | JSON object | required (JSON provider) | Constant parameters, e.g. `{"format": "json"}`. |
| `search_headers` | JSON object | required (JSON provider) | Constant headers. |
| `search_api_key`, `search_engine_id` | string | required (Google) | Credentials for Google Custom Search. |
| `discovery_when_below` | float | *(only when empty)* | Run discovery once the best queued score falls below this. With link-following into the open web the queue never empties, so waiting for an empty queue means discovery never runs again. Set it from your own data with `experiments/frontier_threshold.py`, which scores every link the crawl followed and reports what each score band's targets turned out to be; the shipped 3.0 is where this project's yield falls from 4-14% to under 1%. |
| `discovery_priority` | float | `50.0` | Frontier score given to search hits - deliberately above any link-derived score, because a search hit answers the configured query directly. |
| `discovery_batch_size` | int | `5` | Queries per discovery turn. |
| `discovery_query_order` | `file` or `interleave` | `file` | `interleave` takes one query from each block of the query file per turn (or `weight` of them), so a run that fires discovery ten times has asked in ten languages. In file order a world-wide query file is, in practice, single-language. |

If a required discovery key is missing or malformed, discovery is disabled
with a warning and the rest of the run proceeds.

### Stopping conditions

The run stops at the first of these that is met. `0` means "no limit".

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_rounds` | int | `0` | Crawl rounds. |
| `max_discovery_batches` | int | `0` | Discovery turns. |
| `max_runtime_seconds` | float | `0` | Wall-clock seconds. |

A run also stops on its own once the fetch queue is empty and no discovery
queries are left.

---

## The taxonomy file

`config/taxonomy.config` declares what kinds of page exist. Every key here is
scoped to a category name.

```
categories = STATION|LIST|ADVICE|HUB|IRRELEVANT;
```

| Key | Applies to | Effect |
| --- | --- | --- |
| `relevancy[NAME]` | every category | `CONTENT` (extract records from it), `LINKS` (follow its links, extract nothing), or `IRRELEVANT` (a dead end). Required. |
| `check_linked_urls[NAME]` | `CONTENT`, `LINKS` | Whether this category's outbound links are queued at all. |
| `prompt[NAME]` | `CONTENT` | The extraction instruction. |
| `model_path[NAME]` | `CONTENT` | GGUF model for this category's extraction. |
| `context[NAME]` | `CONTENT` | Context window for it. |
| `max_tokens[NAME]` | `CONTENT` | Output cap. A single record needs a few hundred; a listing page needs thousands. |
| `fields[NAME]` | `CONTENT` | A category-specific schema, overriding the global `fields`. |
| `is_list_category[NAME]` | `CONTENT` | The page holds many entities, so the schema is wrapped as an array and the per-site extraction budget does not apply. |
| `follow_record_urls[NAME]` | `CONTENT` | Queue the websites named in this category's records - a listed organisation's own site - instead of waiting for link-following to reach them. |
| `record_url_priority[NAME]` | `CONTENT` | At what frontier score. Above `discovery_priority` means "before anything else". |
| `record_filter_prompt[NAME]` | `CONTENT` | One question asked about a listing's extracted entries, naming which belong in the database. Costs one call per page. |
| `record_filter_names_removals[NAME]` | `CONTENT` | `True` when that answer names the entries to *remove* rather than the ones to keep. |
| `confirm_prompt[NAME]` | any | A second, focused question asked of pages put in this category. |
| `confirm_answers[NAME]` | with a prompt | The permitted answers, `a\|b\|c`. Grammar-constrained, so nothing else can come back. |
| `confirm_accept[NAME]` | with a prompt | The answer that keeps the category. Must be one of the answers. |
| `confirm_fallback[NAME]` | with a prompt | Where the page goes otherwise. Must name a category. |

Two findings are worth carrying to a new taxonomy, because both cost a day to
learn:

- **A page goes in the bucket that fits it.** The way to stop pages landing in
  the wrong category is to offer a right one, not to write a longer
  prohibition. Pet-supply catalogues stopped being classified as listings the
  moment a commercial category existed; nothing in the listing wording changed.
- **In a confirmation question, put the accepted answer last.** Offered first,
  it was chosen for 18 of 31 pages that did not deserve it; offered last, for
  2. Naming the local words for what you are excluding matters as much.

## The schema file

`config/schema.config` declares what to extract, and what each field means.
The meaning is what drives deduplication, so it deserves more care than the
field list.

```
fields = {"name": "string", "e-mail": "string", "accepted_animals": "list[string]"};
field[e-mail] = {"role": "identifier", "weight": 1.0, "normalize": "email", "fusion": "union"};
require_fields   = "name";
require_any_role = "identifier", "locator";
```

Field types are `string`, `boolean`, `list[string]`, or any type named by a
`define` statement:

```
define animal_type = mammal|bird|reptile|amphibian|fish|invertebrate;
fields = {"accepted_animals": "list[animal_type]"};
```

| Semantic | Values | Meaning |
| --- | --- | --- |
| `role` | `identifier` | Near-unique. Agreement strongly implies two records are the same thing. |
| | `locator` | Places the entity. Corroborates, and disagreement counts against a match. |
| | `label` | A human name. Supporting evidence only - generic names are common. |
| | `attribute` | Descriptive payload, used only when fusing. |
| `weight` | float | This field's contribution to the match score. |
| `normalize` | `email`, `phone`, `url`, `digits`, `casefold` | How values are compared, and which values are implausible enough to drop. A date in a telephone field goes; the record stays. |
| `fusion` | `union` | Keep every distinct value across the cluster. |
| | `trust_then_mode` | The most common value, own-site records first. |
| | `trust_then_valid` | The first valid value, own-site records first. |
| | `first_by_trust` | The most trusted record's value. |
| | `longest_from_top_trust` | The longest value among the most trusted records. |
| | `or_with_evidence` | True if any record says so, recording which page said it. |
| `similarity` | e.g. `ngram_dice` | How near-matches are scored. |

Resolution blocks records on normalised identifier values, scores the pairs,
clusters them with union-find, and fuses each field according to its `fusion`.
Conflicting values are recorded in `entity_conflicts` rather than discarded,
which is where the `conflicting-contact` review flag comes from.

## The lexicon file

`config/lexicon/multilingual.config` holds everything language-bound, so a
deployment in another language swaps one file. It sets `url_tokens[...]`,
`drop_query_params`, `domain_denylist`, `category_host_tokens[...]`,
`exclude_record_name_tokens`, `keep_record_name_tokens` and
`label_prefixes_to_strip` - all described in
[the settings reference](#settings-reference).

The rule of thumb behind it: **this model judges kinds well and instances
badly.** Asking it to certify one short entry - is this listing entry a
station? - was unreliable in every wording tried, and a deterministic token
rule over the same text beat all of them. Give the model a category to sort
into; give the lexicon the individual calls.

## search_queries.csv

Read by `discovery_service.read_query_templates()`. A line of three or more
dashes starts a new block, and everything a block declares applies to that
block alone - which is what keeps a Spanish template away from a Japanese
location.

| Line | Meaning |
| --- | --- |
| `location: Bayern` | A place to search in. |
| `language: de` | Sent to the search provider with every query of this block, so a German query is answered with German pages. The JSON provider sends it as given (SearXNG's own spelling); the Google provider translates it to `lr=lang_de`. |
| `params: safesearch=0, time_range=year` | Any further request parameters, comma-separated. |
| `weight: 2` | Take this many of the block's queries per interleaved turn. |
| `set animal = Igel, Dachs` | A placeholder the block's templates may use; a template naming it is written out once per value. |
| `Wildtierhilfe {location}` | A query template. `{location}` is substituted; a template without it gets the location appended. |
| `# ...` | Comment. |

```
language: de
set tier = Igel, Fledermaus
location: Bayern
location: Hessen

{tier} Auffangstation {location}

---
language: fr
location: Bretagne

centre de soins faune sauvage {location}
```

That file produces four German queries and one French one, and no German
template ever meets Bretagne. With `discovery_query_order = "interleave"` the
first turn takes one query from each block.

Queries are consumed from the front of the list and not repeated within a run,
so a long file costs nothing until discovery needs it.

## starting_urls.csv

One URL per line; blank lines and `#` comments are ignored. Several files may
be named in `starting_url_file`, which lets a deployment add its own seeds
without editing anyone else's. A seed on the domain denylist is skipped - that
is the denylist working, not a missing seed.

## Recipes

**Retarget the crawler at a different kind of organisation.** Rewrite
`config/taxonomy.config` (categories, prompts, referrer weights) and
`config/schema.config` (fields and their semantics), replace the lexicon and
the query file, and point `starting_url_file` at new seeds. Nothing in `src/`
changes.

**Make a run finish sooner.** Lower `max_pages_per_site`, raise
`discovery_when_below` so the crawler reaches for search rather than working
through marginal links, and set `max_runtime_seconds` or `max_rounds`.

**Make the output cleaner rather than larger.** Add name tokens to the
lexicon, give confusable kinds of page their own categories, and add a
confirmation question to any category that collects two different things.
Measure with `experiments/entity_precision.py` before and after: at small
sample sizes a single prompt clause is indistinguishable from noise.

**Try a change without recrawling.** Everything fetched is in the page store.
`--reprocess categorize` replays classification and extraction over it;
`--reprocess extract` replays extraction alone. Both write to the same
database, so copy it first if you want to compare.

**Run two configurations side by side.** Copy `bot.config`, point its
`database` at a second file, and keep `page_store_path` the same so both share
the fetched pages.
