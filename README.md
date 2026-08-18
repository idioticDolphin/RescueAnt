# RescueAnt

This web scraping tool is being developed as part of a bachelor thesis project.
It aims to automatically find rescue station data for the website of WildTanic e.V..

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
python src/main.py [path/to/bot.config]
```

The config path is optional and defaults to `bot.config` at the project
root; pass one to run against a different configuration without touching
your main one (see "Trying it out with the example files" below).

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
download-models.sh         downloads the default LLM model into models/
```
