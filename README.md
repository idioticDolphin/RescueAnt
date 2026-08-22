# RescueAnt

This web scraping tool is being developed as part of a bachelor thesis project.
It aims to automatically find rescue station data for the website of Wildtanic e.V..

It crawls a list of starting URLs (and, optionally, URLs found via a
search-engine API), categorizes each page with a local LLM, and extracts
structured data (name, address, contact info, ...) from the pages that
turn out to be animal rescue stations, storing everything in a local
sqlite database.

## Requirements

- Python 3.12 or newer
- A C/C++ toolchain (needed to build `llama-cpp-python` if no prebuilt
  wheel is available for your platform)
- ~3 GB free disk space for the default LLM model, plus space for the
  Playwright browser
- A GGUF-format LLM that supports grammar-constrained / JSON-schema-constrained
  chat completions (the default `bot.config` uses
  [Qwen3.5-4B-UD-Q4_K_XL](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF))
- Docker and Docker Compose, to run the self-hosted SearXNG instance used
  for search-based discovery (skip this if you set `discover_urls = False`,
  or point `bot.config` at a different search provider - see "Search
  engines" below)

## Setup

1. **Clone the repository and create a virtual environment**

   ```bash
   git clone <this-repo-url> RescueAnt
   cd RescueAnt
   python3 -m venv .venv
   source .venv/bin/activate  # on Windows: .venv\Scripts\activate
   ```

2. **Install the project and its dependencies**

   ```bash
   pip install -e ".[dev]"
   ```

   The `dev` extra pulls in `pytest`, `pytest-asyncio` and `pytest-cov`,
   needed to run the test suite. Leave it off (`pip install -e .`) for a
   runtime-only install.

3. **Install Playwright's browser binary**

   `fetching_service` renders pages with Playwright's Chromium, which is
   not bundled with the `playwright` package itself:

   ```bash
   playwright install chromium
   ```

4. **Download the LLM model**

   ```bash
   ./download-models.sh
   ```

   This downloads the default model into `models/` (gitignored). If you
   want to use a different GGUF model, download it yourself and point
   `bot.config`'s `category_model_path`/`model_path[...]` entries at it
   instead - relative paths are resolved from the project root, the same
   place `download-models.sh` writes to.

5. **Start the search engine used for discovery**

   ```bash
   docker compose up -d
   ```

   This starts a self-hosted [SearXNG](https://docs.searxng.org/) instance
   on `http://localhost:8080`, which `bot.config` already points at for
   search-based discovery - see "Search engines" below for details, how to
   verify it's working, and how to use a different provider instead. Skip
   this step if you set `discover_urls = False` in `bot.config`.

6. **Review/adjust `bot.config`**

   A working example is committed at the repo root. Fields you're most
   likely to want to change for your own setup:

   | Field | Purpose |
   |---|---|
   | `starting_url_file` | file with one seed URL per line to start crawling from (see `starting_urls.csv`) |
   | `database` | path to the sqlite database file that gets created |
   | `discover_urls` | `True`/`False` - whether to also discover new URLs via a search API once the fetch queue runs dry (see "Search engines" below) |
   | `search_provider`, `search_base_url`, ... | search API configuration, only needed if `discover_urls = True` - see "Search engines" below |
   | `search_query_file` | query-template file for search-based discovery (see `search_queries.csv`) |
   | `discovery_batch_size` | how many queries `orchestrator.run_discovery()` runs each time the fetch queue empties out (queries are consumed gradually, batch by batch, not all at once) |
   | `max_discovery_batches`, `max_rounds`, `max_runtime_seconds` | stop `orchestrator.run()` early - see "Stopping conditions" below |
   | `politeness` | minimum seconds between two requests to the same domain |
   | `categories`, `relevancy[...]`, `prompt[...]`, `fields`, ... | what page categories exist, which are worth extracting data from, and what fields to extract - see the comments in `bot.config` for the full per-category syntax |
   | `category_model_path`, `model_path[...]` | path(s) to the GGUF model(s) used for categorization/extraction |

7. **Provide seed data**

   - `starting_urls.csv`: one URL per line, crawled on the very first run.
   - `search_queries.csv` (only needed if `discover_urls = True`): one
     keyword template or `location: <name>` line per line - see the
     comments at the top of the file, or
     `model.crawler.discovery_service.read_query_templates()`'s docstring,
     for the exact format.

## Search engines

Search-based discovery (`discover_urls = True`) is only reached once the
fetch queue - seeded from `starting_url_file`, and kept alive by links found
on pages already being crawled - runs completely dry (see "Running" below
for the full picture). At that point `orchestrator.run_discovery()` runs
`discovery_batch_size` queries from `search_query_file` and queues whatever
URLs they turn up.

### SearXNG (the default - free, self-hosted, no API key)

Google discontinued unrestricted whole-web results in the free tier of its
Custom Search API, so `bot.config` discovers URLs via a self-hosted
[SearXNG](https://docs.searxng.org/) instance instead - a free, open-source
metasearch engine that queries Google/Bing/DuckDuckGo/... on your behalf
and needs no API key or account of its own.

A ready-to-run setup is included at the project root - `docker-compose.yml`
plus `searxng/settings.yml` (pre-configured to enable the JSON output
format the project needs; SearXNG disables it by default for anything but
`html`). Start it with:

```bash
docker compose up -d
```

Verify it's up and returning JSON:

```bash
curl "http://localhost:8080/search?q=test&format=json"
```

`bot.config` and `examples/bot.config` already point
`ConfigurableJsonSearchProvider` at it:

```
search_provider = "SearXNG";
search_base_url = "http://localhost:8080/search";
query_parameters = "q";
search_result_path = "results";
search_url_field = "url";
search_extra_params = {"format": "json"};
search_headers = {};
search_timeout = 10.0;
```

Stop it with `docker compose down` when you don't need it running. If you
edit `searxng/settings.yml` by hand, do it while the container is stopped -
SearXNG's entrypoint takes ownership of the mounted directory on startup,
which can leave it unwritable by your own user afterwards; if that happens,
`docker run --rm -v ./searxng:/data alpine chown -R $(id -u):$(id -g) /data`
hands it back.

By default SearXNG queries several upstream engines at once (Google, Bing,
DuckDuckGo, Wikipedia, ...); which ones are enabled - and rate limits,
result count, etc. - are all configurable in `searxng/settings.yml`, see
[SearXNG's settings documentation](https://docs.searxng.org/admin/settings/).

### Using a different search API

Any other JSON-returning search API - a different self-hosted SearXNG
instance, Bing Web Search, SerpApi, etc. - can be plugged in the same way
via `ConfigurableJsonSearchProvider` (see
`model.objects.searchprovider.ConfigurableJsonSearchProvider`'s docstring
for the full picture) by pointing these fields at it:

| Field | Meaning |
|---|---|
| `search_base_url` | the API endpoint to send GET requests to |
| `query_parameters` | the query-string parameter name the search text goes in (e.g. `"q"`) |
| `search_result_path` | comma-separated path of keys to walk from the JSON response root down to the list of results (e.g. `results` for `{"results": [...]}`) |
| `search_url_field` | the key inside each result object holding its URL |
| `search_extra_params` | JSON object of extra query-string parameters (API keys, output format, ...) |
| `search_headers` | JSON object of extra HTTP headers to send |
| `search_timeout` | request timeout in seconds |

### Using Google

`GoogleCustomSearchProvider`
(`model.objects.searchprovider.GoogleCustomSearchProvider`) is still
available for cases its current free tier does support (e.g. search
restricted to specific sites you list yourself) - it's just no longer the
default here, since unrestricted whole-web search now requires a paid plan:

```
search_provider = "Google";
search_api_key = "<your API key>";
search_engine_id = "<your search engine ID>";
```

Get an API key at https://developers.google.com/custom-search/v1/introduction
and create a search engine (and its ID/"cx") at
https://programmablesearchengine.google.com/.

## Running

```bash
python src/main.py [path/to/bot.config] [-v]
```

The config path is optional and defaults to `bot.config` at the project
root; pass one to run against a different configuration without touching
your main one (see "Trying it out with the example files" below). By
default only progress/failure messages are logged (batches, fetch/category/
extraction outcomes, discovery yield); pass `-v`/`--verbose` for debug-level
detail (raw LLM outputs, per-field config parsing, ...). Either way, the LLM
library itself (`llama.cpp`) is kept quiet - without that, loading the model
and every single query would print hundreds of lines of its own internal
logging.

This runs `model.orchestrator.run()`, which initializes the database,
seeds the fetch queue from `starting_url_file`, and then works through two
phases:

1. **Crawl what's already known.** Repeatedly fetch/categorize/extract
   batches from the fetch queue - which keeps growing on its own as
   extraction finds links on the pages it visits - until it runs dry.
2. **Discover more, gradually.** Only once that queue is empty does it pull
   in a batch of `discovery_batch_size` search queries and queue their
   results, then goes back to step 1. If a batch doesn't turn up anything
   new, the next batch is tried, and so on.

### Stopping conditions

The run always stops once the fetch queue is empty and there are no more
discovery queries left to try. You can also make it stop earlier via
`bot.config` (`0` means "no limit" for all of these):

| Field | Stops the run once... |
|---|---|
| `max_discovery_batches` | ...this many discovery batches have been run - caps search API usage |
| `max_rounds` | ...this many fetch/process batches have been processed |
| `max_runtime_seconds` | ...this many seconds have elapsed since the run started |

### Session monitoring

Every `python src/main.py` run also writes a `sessions/session_<timestamp>.jsonl`
log via `model.tools.monitor_service` - one JSON line per event, flushed to
disk immediately (not held in memory for the run's duration), so the data
survives the run being interrupted (Ctrl-C, a crash, a killed process). It
records round boundaries (one round = one `process_batch()` call) and
discovery batch outcomes (queries used, URLs discovered, URLs newly queued).

To turn a session log into per-round/per-discovery-batch CSVs - URLs
fetched/failed per round, how many sites landed in which category per
round, how round size changed over the run, discovery yield per batch -
run:

```bash
python experiments/analyze_session.py [session_log.jsonl] [db_path]
```

Both arguments are optional: it defaults to the most recent file under
`sessions/` and the database configured in `bot.config`. It re-derives
fetch/category counts by binning the `crawls` table's timestamps into each
round's time window, rather than duplicating that data in the log - see
`model.tools.monitor_service`'s and `experiments/analyze_session.py`'s
docstrings for why, and for how an interrupted session's still-open last
round is handled. Output goes to `experiments/data/`, alongside the other
experiment CSVs (see "Experiments & notebooks" below).

To plot that data - round size/fetch outcomes, category composition and
link-quality trend per round, round duration vs. quality, time spent on
productive vs. irrelevant pages per round, LIST vs. STATION extraction
time, and discovery yield per batch - run
`notebooks/session_monitoring.ipynb` (needs the `notebooks` extra below).
It defaults to the most recently analyzed session, so analyzing a session
captured on another machine is two commands: run `analyze_session.py`
against the copied-over `session_<timestamp>.jsonl` + `crawl.db`, then
re-run the notebook - headlessly with `jupyter nbconvert --to notebook
--execute --inplace notebooks/session_monitoring.ipynb`, or interactively
to point it at a specific older session instead of the latest one. The
per-page categorize/extract timing (productive-vs-irrelevant, LIST-vs-
STATION) only exists for sessions captured after `model.orchestrator`
started emitting it - older sessions plot everything else, just not that.

`python experiments/compare_gold_to_crawl_db.py [gold_csv_path] [db_path]`
cross-checks `experiments/data/extraction_gold_labels.csv` against what a
*real* crawl actually stored for those same URLs, as opposed to
`collect_extraction_correctness.py`'s isolated re-fetch-and-re-run - the
two can disagree (a real, observed case: the isolated experiment
under-extracted a LIST page 1/9 while the same page's real crawl got 9/9,
see the script's docstring). Gold URLs not yet covered by the given
database are reported, not treated as an error, so it's safe to re-run
against a growing/changing crawl database.

## Trying it out with the example files

`examples/` contains a small, self-contained config (`examples/bot.config`)
plus a couple of real seed URLs and search queries
(`examples/starting_urls.csv`, `examples/search_queries.csv`), so you can
check the whole workflow actually works - fetching, categorizing,
extracting, discovering - without touching your real `bot.config` or
database. It needs the model downloaded (step 4 above) and, for the
discovery part, SearXNG running (step 5 above, `docker compose up -d`) -
without it, each discovery query simply fails (logged, not fatal - see
`discovery_service.discover_urls()`) and is treated as having found nothing.

```bash
python src/main.py examples/bot.config
```

## Running the tests

```bash
python -m pytest test/
```

Add `--cov=src --cov-report=term-missing` (requires the `dev` extra) to
also see coverage.

## Experiments & notebooks

`experiments/` and `notebooks/` hold a small set of measurement scripts and
analysis notebooks used to characterize the crawler's behavior - page
category distribution, per-step (fetch/categorize/extract) timing, the
effect of grammar-constrained vs. free-form extraction on latency, and
search-discovery yield over successive query batches. They're written for
a bachelor thesis audience: each notebook documents its research question,
methodology and limitations alongside the plots, and exports each figure
as both PDF (for LaTeX) and PNG into `notebooks/figures/`. One notebook
differs from the rest: `session_monitoring.ipynb` plots a real, uncontrolled
`src/main.py` run rather than a fixed reproducible sample - see "Session
monitoring" above.

Install the extra dependencies these need (matplotlib, pandas, seaborn,
scipy, jupyter):

```bash
pip install -e ".[notebooks]"
```

The scripts under `experiments/` run the *real* pipeline (real HTTP
fetches, the real configured LLM, and - for discovery - the real search
provider) against fixed, reproducible input samples, and write their
results as CSVs into `experiments/data/` (already committed, so the
notebooks can be read without re-running anything). To regenerate them:

```bash
python experiments/collect_crawl_metrics.py        # fetch/categorize/extract timing + category mix
python experiments/collect_grammar_comparison.py   # strict (enum) vs. lax (string) grammar timing
python experiments/collect_discovery_yield.py       # search-discovery yield per batch
```

Each accepts an optional sample-size/batch-count argument (see the
docstring at the top of the script) and logs progress the same way
`src/main.py` does. `collect_crawl_metrics.py` and
`collect_grammar_comparison.py` need the LLM (and are, on CPU, the slow
ones - see their docstrings for the runtime/accuracy trade-offs made to
keep them tractable); `collect_discovery_yield.py` needs SearXNG running
(`docker compose up -d`) but no LLM.

Then (re-)run the notebooks, e.g. `jupyter lab notebooks/` interactively,
or headlessly:

```bash
jupyter nbconvert --to notebook --execute --inplace notebooks/*.ipynb
```

## Project layout

```
src/
  main.py                 entry point - runs the full crawl workflow
  model/
    orchestrator.py       ties the whole workflow together
    crawler/               fetching (Playwright) and URL discovery
    analyzer/               HTML cleaning, page categorization, data extraction
    tools/                 config loading, sqlite persistence, LLM loading
    objects/                shared data types (Config, Category, Website, SearchProvider)
  view/, controller/       reserved for a future UI
test/                      pytest suite, mirrors the src/ package layout
bot.config                 runtime configuration (see step 6 above)
starting_urls.csv          seed URLs for crawling
search_queries.csv         query templates for search-based discovery
examples/                  small config + seed files to try the whole workflow with
docker-compose.yml         runs the self-hosted SearXNG instance (see "Search engines")
searxng/                   SearXNG configuration (settings.yml), bind-mounted into the container
experiments/                data-collection scripts + their CSV output (see "Experiments & notebooks")
notebooks/                  analysis notebooks + exported figures (see "Experiments & notebooks")
sessions/                   per-run monitoring logs (gitignored, see "Session monitoring")
download-models.sh         downloads the default LLM model into models/
```
