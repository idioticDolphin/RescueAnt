# RescueAnt

RescueAnt finds animal rescue organisations on the open web and turns them
into a structured database. It runs entirely on your own machine - the only
network traffic is to the sites being crawled, and to your own search
instance if you enable discovery.

It crawls a list of seed URLs (and, optionally, URLs found via a search
engine), classifies each page with a local LLM, extracts structured data
(name, address, contact details, accepted animals, ...) from the pages worth
mining, and resolves the resulting records into deduplicated entities in a
local sqlite database.

Three properties are worth knowing up front:

- **It is interruptible.** Every fetched page is stored on disk, so a run can
  be stopped at any moment and resumed exactly where it left off, without
  refetching anything. Analysis can also be re-run over stored pages after a
  prompt or schema change - see "Reprocessing" below.
- **It is configured, not coded.** The page taxonomy, the extraction schema,
  the field semantics that drive deduplication, and the URL/domain lexicons
  all live in config files. Retargeting it at a different kind of entity
  means swapping those files, not editing `src/`.
- **It is domain- and language-neutral in its mechanics.** Matching and
  fusion are driven by declared field *roles*, never field names; comparisons
  use Unicode case folding and character n-grams rather than English-specific
  tokenisation.

> **Scope note.** The crawler collects contact details of organisations from
> their own public websites. That is personal data in the GDPR sense, so
> `experiments/data/` is gitignored and crawl databases are not committed.
> Respect `robots.txt` (enforced), keep `politeness` sane, and check your own
> legal basis before publishing anything it collects.

## Requirements

- Python 3.12 or newer
- A C/C++ toolchain (needed to build `llama-cpp-python` if no prebuilt
  wheel is available for your platform)
- ~3 GB free disk space for the default LLM model, plus space for the
  Playwright browser and the page store (roughly 1 MB per 20 pages crawled,
  gzipped)
- A GGUF-format LLM that supports grammar-constrained / JSON-schema-constrained
  chat completions (the default `bot.config` uses
  [Qwen3.5-4B-UD-Q4_K_XL](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF))
- Docker and Docker Compose, to run the self-hosted SearXNG instance used
  for search-based discovery (skip this if you set `discover_urls = False`,
  or point `bot.config` at a different search provider - see "Search
  engines" below)

### On model size

A larger or newer model is not automatically a better choice, because on a
consumer card VRAM is the binding constraint. Measured on an 8 GB RTX 2070
SUPER, against 50 labelled pages across an earlier 12-category version of the
taxonomy:

| Model | On disk | Usable context | Accuracy | Long page (8k tokens) |
|---|---|---|---|---|
| **Qwen3.5-4B UD-Q4_K_XL** (default) | 2.71 GB | 32k | **92%** | **4.1 s** |
| Qwen3.5-9B Q4_K_M | 5.29 GB | 12k | 80% | - |
| Gemma 3 4B UD-Q4_K_XL | 2.54 GB | 16k | 40% | - |
| Llama 3.1 8B UD-Q4_K_XL | 4.99 GB | 16k | - | 13.6 s |
| Ministral 3 8B UD-Q4_K_XL | 5.29 GB | 12k | - | 13.7 s |
| Qwen3.5-2B UD-Q4_K_XL | 1.25 GB | 32k | 42% | - |

Two things this table shows that download sizes do not:

- **Disk size does not predict VRAM.** Gemma 3 4B is smaller on disk than the
  default yet cannot hold a 32k context on the same card.
- **A model that no longer fits does not fail - it slows down.** llama.cpp
  moves what does not fit into system memory, and throughput falls by roughly
  ten times. The 8B models were not scored for accuracy: at three times the
  default's per-page cost they cannot come out ahead however accurate they are,
  since the default already classifies 92% of pages correctly.

The accuracy column comes from pages the category prompt was developed
against. On 75 pages from sites it was never tuned on, the default model picks
the exact category for 76% of pages and makes the right extraction decision -
extract a record, extract a listing, or only follow links - for 95%.

With more VRAM these trade-offs change. Measure candidates on your own
hardware before switching:

```bash
./download-models.sh --list
./download-models.sh <name>
python experiments/model_sweep.py --only <name>
```

## Setup

1. **Clone the repository and create a virtual environment**

   ```bash
   git clone https://github.com/idioticDolphin/RescueAnt.git
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
   ./download-models.sh              # the default model
   ./download-models.sh --list       # the whole catalogue
   ./download-models.sh gemma-3-4b   # a named alternative
   ```

   Models are listed in `models.csv` and downloaded into `models/`
   (gitignored); downloads resume if interrupted and already-present files
   are skipped. Add a model by adding a line to `models.csv` - the script
   needs no change.

   To use one, point `category_model_path` / `model_path[...]` at it.
   Relative paths resolve from the project root, where the script writes.

5. **Start the search engine used for discovery**

   ```bash
   docker compose up -d
   ```

   This starts a self-hosted [SearXNG](https://docs.searxng.org/) instance
   on `http://localhost:8080`, which `bot.config` already points at for
   search-based discovery - see "Search engines" below for details, how to
   verify it's working, and how to use a different provider instead. Skip
   this step if you set `discover_urls = False` in `bot.config`.

6. **Review/adjust the configuration**

   Configuration is split across several files, pulled together by
   `include` directives at the top of `bot.config`. Later files override
   keys from the files they include, so a deployment can keep its own
   overrides in one place:

   ```
   bot.config                         run settings + includes the rest
   config/taxonomy.config             page categories, prompts, referrer weights
   config/schema.config               extraction schema + field semantics
   config/lexicon/multilingual.config URL tokens, domain denylist
   ```

   **Retargeting the crawler at a different kind of entity** means editing
   `taxonomy.config` (what page types exist and how to recognise them) and
   `schema.config` (what to extract and what each field *means*). No code
   changes are involved.

   Run settings you're most likely to change, in `bot.config`:

   | Field | Purpose |
   |---|---|
   | `starting_url_file` | one seed file, or several - `"a.csv", "b.csv"`. Blank lines and `#` comments are ignored |
   | `database` | path to the sqlite database that gets created |
   | `page_store_path` | where fetched page bodies are kept, for resume and reprocessing |
   | `politeness` | minimum seconds between two requests to the same domain |
   | `link_closeness_decay`, `closeness_source_min` | bot | how fast a link's priority decays with each hop away from a station or listing |
   | `fetch_timeout_seconds` | bot | deadline for one page's whole fetch, not just its navigation |
   | `queue_record_urls_at_start` | bot | begin a run by queueing websites named in earlier records that were never visited |
   | `skip_identical_content` | bot | reuse the analysis of an already-analysed page with byte-identical content |
   | `max_pages_per_site` | hard ceiling on pages fetched from one registrable domain |
   | `abandon_site_after`, `abandon_site_max_weight` | stop crawling a site once it has given this many low-value pages and no record |
   | `skip_url_extensions` | links to files (`.pdf`, `.docx`, images, archives, ...) that are never fetched |
   | `max_extractions_per_site` | how many times one site may be mined for records before further pages are classified but not extracted (listing pages are exempt) |
   | `max_batch_size` | pages fetched per round, so analysis keeps pace with fetching |
   | `discovery_when_below` | reach for search discovery once the best queued link scores below this - **not** only when the queue is empty |
   | `discover_urls`, `search_*` | search configuration - see "Search engines" below |
   | `llm_call_timeout_seconds`, `min_generation_tokens_per_second` | backstop against stalled generation; the budget scales with each category's token allowance |
   | `strip_site_boilerplate`, `boilerplate_*` | remove a site's recurring chrome before classifying/extracting |
   | `category_model_path`, `model_path[...]` | GGUF model(s) for classification/extraction |

   And in the included files:

   | Field | File | Purpose |
   |---|---|---|
   | `categories`, `category_prompt` | taxonomy | the page taxonomy and how the model is asked to apply it |
   | `relevancy[X]`, `prompt[X]`, `max_tokens[X]` | taxonomy | per-category: extract, follow links only, or ignore |
   | `referrer_weights` | taxonomy | how much a link inherits from the category of the page offering it |
   | `follow_record_urls[...]`, `record_url_priority[...]` | taxonomy | queue the websites named in a category's records - a listed organisation's own site - at this frontier priority, so they are fetched soon |
   | `confirm_prompt[...]`, `confirm_answers[...]`, `confirm_accept[...]`, `confirm_fallback[...]` | taxonomy | a second, focused question for pages put in a category; unless the accepted answer comes back, the page is filed under the fallback |
   | `mislabel_check`, `mislabeled_category` | taxonomy | let extraction answer that a page is not what it was classified as; it is re-filed under `mislabeled_category` and its original category kept in `reclassified_from` |
   | `fields`, `field[name]` | schema | what to extract, and each field's role/weight/normaliser/fusion strategy |
   | `require_fields`, `require_any_role` | schema | the admissibility gate a record must pass to be stored |
   | `url_tokens[...]`, `domain_denylist` | lexicon | path tokens and domains to prefer or refuse |
   | `label_prefixes_to_strip` | lexicon | prefixes dropped from a name field, e.g. "Contact:" |
   | `category_host_tokens[...]`, `exclude_record_name_tokens`, `keep_record_name_tokens` | lexicon | host substrings that decide a site's category without asking the model; name substrings that make a record inadmissible, and care words that override them |
   | `record_filter_prompt[...]` | taxonomy | one call per listing page naming which extracted entries to keep (or, with `record_filter_names_removals`, to drop) |

7. **Provide seed data**

   - `starting_urls.csv` and `starting_urls_other.csv`: one URL per line.
     Keeping seeds in themed sets (regional directories in one file,
     species-specific networks in another) lets you add your own without
     editing anyone else's list.
   - `search_queries.csv` (only needed if `discover_urls = True`): one
     keyword template or `location: <name>` line per line - see the
     comments at the top of the file, or
     `model.crawler.discovery_service.read_query_templates()`'s docstring,
     for the exact format.

## GPU acceleration (optional)

`fetching_service`/`analyzer` inference goes through `llama-cpp-python`
(`model.tools.llm_service`), which loads every model with `n_gpu_layers=-1` -
i.e. it offloads as many layers as fit onto a CUDA GPU automatically,
falling back to CPU-only layers if none is found. Whether that actually uses
a GPU depends on which `llama-cpp-python` build is installed:

1. **Try the prebuilt CUDA wheel first** - no compiler needed:

   ```bash
   pip uninstall -y llama-cpp-python
   pip install llama-cpp-python --prefer-binary --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
   ```

   (swap `cu124` for whichever CUDA branch matches your driver - see
   https://abetlen.github.io/llama-cpp-python/whl/ for the available ones).
   Sanity-check it loads without crashing:

   ```bash
   python -c "from llama_cpp import Llama; Llama(model_path='models/<your model>.gguf', n_ctx=512, n_gpu_layers=-1, verbose=True)"
   ```

   Look for `offloaded N/N layers to GPU` in the output. If it instead
   crashes with `OSError: [WinError -1073741795] Windows Error 0xc000001d`
   (or a SIGILL on Linux) **even with `n_gpu_layers=0`**, the prebuilt
   wheel's CPU code was compiled for AVX-512, which older CPUs (e.g. AMD
   Zen 2/Ryzen 3000-series) don't support - move on to building from source.

2. **Build from source** if the prebuilt wheel crashes or none matches your
   setup. On Windows this needs, on top of the base setup:

   - [Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio)
     with the "Desktop development with C++" workload
   - the [NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-toolkit-archive)
     (pick a version your driver supports - check with `nvidia-smi`; you only
     need the compiler/libraries, the driver components can be deselected in
     a custom install if a newer driver is already installed)
   - [CMake](https://cmake.org/download/)

   Building via a single chained shell command silently breaks on Windows:
   `cmd.exe` expands every `%VAR%` in a `&&`-joined line up front, so
   `set PATH=...;%PATH%` ends up using the PATH from *before*
   `vcvars64.bat` ran, wiping out the compiler/SDK paths it just added for
   every command after it. Use a real (multi-line) batch script instead, so
   each line's variables resolve after the previous line has run - e.g.:

   ```bat
   @echo off
   call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
   set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4
   set PATH=%CUDA_PATH%\bin;%PATH%
   set CMAKE_ARGS=-GNinja -DGGML_CUDA=on -DGGML_AVX512=OFF -DGGML_NATIVE=OFF -DGGML_AVX2=ON -DCMAKE_CUDA_ARCHITECTURES=75
   pip install llama-cpp-python --no-cache-dir --no-binary llama-cpp-python
   ```

   Run it with `cmd.exe /c build_gpu.bat` from an activated venv shell.
   Notes on the flags:
   - `-GNinja`: CMake's Visual Studio generator needs the CUDA Toolkit's
     MSBuild integration files, which a component-selective CUDA install
     (skipping the driver) may not register; Ninja invokes `nvcc`/`cl.exe`
     directly and sidesteps that entirely. VS Build Tools ships its own
     `ninja.exe`, already on `PATH` after `vcvars64.bat`.
   - `-DGGML_AVX512=OFF -DGGML_NATIVE=OFF -DGGML_AVX2=ON`: pins the CPU
     fallback code to AVX2, avoiding the AVX-512 crash described above.
   - `-DCMAKE_CUDA_ARCHITECTURES=75`: the CUDA compute capability to
     compile for - 75 is Turing (RTX 20-series); check yours at
     https://developer.nvidia.com/cuda-gpus.

   Re-run the same load/offload check as step 1 to confirm it worked, then
   `python -m pytest test/` (a `DummyLlama` test double in
   `test/test_llm_service.py` needs to accept whatever `Llama(...)` kwargs
   `llm_service.py` passes).

## Search engines

Search-based discovery (`discover_urls = True`) fires once nothing promising
is left in the fetch queue - when the best queued link's score falls below
`discovery_when_below` (see "Running" below for why that is a threshold
rather than an empty queue). At that point `orchestrator.run_discovery()`
runs `discovery_batch_size` queries from `search_query_file` and queues
whatever URLs they turn up, at `discovery_priority` - far above any
link-derived score, because a search hit answers the configured query
directly.

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
finishes any work left over from a previous run, seeds the fetch queue, and
then alternates between two phases:

1. **Crawl what looks promising.** Repeatedly fetch/classify/extract batches
   from the fetch queue - which keeps growing on its own as pages yield
   links. The queue is ordered by a score each link inherits from the
   category of the page that offered it, so a link found on a directory of
   stations outranks one found on a privacy policy.
2. **Discover more, gradually.** Once nothing *promising* is queued - the
   best queued score has fallen below `discovery_when_below` - it runs
   `discovery_batch_size` search queries and queues their results at high
   priority, then returns to step 1.

> **Tuning `discovery_when_below`.** The trigger is a score threshold, not an
> empty queue - once link-following reaches the open web the queue never
> empties, so a crawler waiting for that would never search again. Raise the
> threshold to reach for search sooner and spend less time on marginal links;
> lower it to exhaust the link graph more thoroughly first. Set it above your
> highest link score and the crawler searches almost exclusively.

### Exporting the result

The crawl database is the working store; the deliverable is a file:

```bash
python src/main.py bot.config --export stations.csv
python src/main.py bot.config --export needs-a-look.csv --needing-review
```

Each row is one resolved organisation, with `n_sources` (how many pages
agreed), `confidence`, and a `review` column naming what deserves a person's
eye - `no-direct-contact` for a record with no phone, e-mail or address, and
`conflicting-contact` where pages disagreed about one. `--needing-review`
keeps only the flagged rows.

### Resuming an interrupted run

Just start it again. Every page body is written to the page store the moment
its fetch succeeds, and each page carries an explicit lifecycle state
(`FETCHED` &rarr; `CATEGORIZED` &rarr; `EXTRACTED`), so a run that is killed
mid-batch loses nothing:

```
Resuming 853 unprocessed page(s) from a previous run
```

Pages fetched but not yet classified are picked up from disk with no network
traffic at all. Only a page whose stored body has gone missing is requeued
for a refetch.

### Reprocessing without recrawling

Because the bodies are kept, analysis can be re-run against the exact same
corpus after changing a prompt, a schema, a token budget or a model - which
is what makes such comparisons controlled rather than confounded by a
re-crawl:

```bash
python src/main.py bot.config --reprocess extract      # re-extract, keep classifications
python src/main.py bot.config --reprocess categorize   # re-classify and re-extract
python src/main.py bot.config --reprocess categorize --category ADVICE
python src/main.py bot.config --reprocess extract --site example.org
```

`--category` and `--site` narrow it. That matters: a prompt change usually
moves *one* boundary, and re-running the whole corpus can take hours to
re-answer questions that were already right.

### Replaying stored pages during development

Set `reuse_stored_pages = True` and a crawl serves any page already in the
page store instead of fetching it: no request reaches the site, not even for
`robots.txt`, and a round served entirely from disk never starts a browser.
That makes a development run fast, repeatable and free for the sites being
crawled. It also means the crawl cannot see anything that changed since a page
was stored - so leave it off when building a directory you intend to use.

Pages stored by earlier crawls can be made available with:

```bash
python src/main.py bot.config --index-store crawl.db older_crawl.db
```

`reuse_max_age_days` limits reuse to pages stored within that many days; `0`
accepts any age.

### Deduplicating into entities

```bash
python src/main.py bot.config --resolve
```

`entries` stays an immutable log of what was observed on which page;
`entities` holds the resolved, fused view, with conflicting values recorded
in `entity_conflicts` rather than silently dropped. Re-running resolution
therefore never destroys evidence, and is safe after tuning `field[...]`
semantics in `config/schema.config`.

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
`collect_extraction_correctness.py`'s isolated re-fetch-and-re-run. The two
can disagree, sometimes substantially - an isolated re-run sees a page
without the site context a real crawl has built up, so prefer this script
when you want to know what the crawler actually produced. Gold URLs not yet
covered by the given
database are reported, not treated as an error, so it's safe to re-run
against a growing/changing crawl database.
`experiments/compare_categorization_gold_to_crawl_db.py [gold_csv_path]
[db_path]` does the same comparison for `categorization_gold_labels.csv`,
scoring a real session's stored category per URL instead of re-running
`categorize_website()` in isolation.

> **`categorization_gold_labels.csv` is labelled against an older, smaller
> taxonomy** and its labels do not map onto the categories the crawler uses
> now. The current classification ground truth is the case list inside
> `experiments/categorization_benchmark.py`. The CSV is kept because its URLs
> remain a useful sample to relabel from.

For a real session captured *before* `model.orchestrator` started emitting
per-page timing (see "Session monitoring" above), `experiments/parse_run_log_timing.py
<log_file> <label>` reconstructs per-page `categorize_seconds`/
`extract_seconds` from the plain console log instead - `process_batch()`'s
categorize/extract loops process pages strictly one after another, so the
gap between two consecutive same-phase log lines *is* that page's call
duration. Needs the run to have been logged at `-v`/`--verbose` (see
"Running" above). Output matches `analyze_session.py`'s
`_page_timing.csv` shape, so it's a drop-in for the same notebook cells.

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
search-discovery yield over successive query batches. Each notebook states
the question it answers, its method and its limitations alongside the plots,
and exports every figure as both PDF and PNG into `notebooks/figures/`. One
notebook
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
results as CSVs into `experiments/data/`. The hand-labeled ground-truth
files (`categorization_gold_labels.csv`, `extraction_gold_labels.csv`) are
committed; the rest of that directory is gitignored, since it holds real
extracted personal data (station operators' names, addresses, phone
numbers) scraped from real websites - re-run the scripts below to
regenerate it locally before reading the notebooks that depend on it:

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
  main.py                 entry point - crawl, --resolve, or --reprocess
  model/
    orchestrator.py       ties the whole workflow together
    crawler/              fetching (Playwright), frontier, URL discovery
    analyzer/             cleaning, boilerplate stripping, classification,
                          extraction, entity resolution
    tools/                config loading, sqlite persistence, page store,
                          LLM loading, URL canonicalisation, monitoring
    objects/              shared data types (Config, Category, SearchProvider)
  view/, controller/      reserved for a future UI
test/                     pytest suite, mirrors the src/ package layout
bot.config                run settings; includes the files below
config/
  taxonomy.config         page categories, prompts, referrer weights
  schema.config           extraction schema + field semantics
  lexicon/                URL tokens and domain denylist, per language/locale
starting_urls.csv         seed URLs
starting_urls_other.csv   further seeds (regional directories, species networks)
search_queries.csv        query templates for search-based discovery
store/                    fetched page bodies, content-addressed (gitignored)
models/                   GGUF models (gitignored)
examples/                 small config + seed files to try the workflow with
docker-compose.yml        runs the self-hosted SearXNG instance
searxng/                  SearXNG configuration, bind-mounted into the container
experiments/              measurement scripts + CSV output
notebooks/                analysis notebooks + exported figures
sessions/                 per-run monitoring logs (gitignored)
models.csv                model catalogue for download-models.sh
download-models.sh        downloads models listed in models.csv
```

## Evaluating a change

`experiments/categorization_benchmark.py` replays hand-labelled pages from
the page store through the classifier and scores them - offline, with no
network and no re-crawl, so a prompt or model change is measured against the
exact pages that motivated it:

```bash
python experiments/categorization_benchmark.py
python experiments/categorization_benchmark.py --split test
python experiments/categorization_benchmark.py --model models/other.gguf --context 12288
```

Labels live in `experiments/data/page_labels.csv`, one row per page with a
`confidence` (`sure` or `unsure`) and a `split` (`dev` or `test`). Unsure
labels are left out unless `--include-unsure` is given. Tune prompts against
`dev` only; `test` measures whether a change generalises to sites it was not
tuned on, and stops doing so once it has been looked at while tuning - which
is why per-page results for `test` are only printed with `--show-test-cases`.
Besides the exact category, the benchmark scores the extraction decision:
whether a page is extracted as a record, as a listing, or not at all. Most
confusions between follow-only categories change nothing in the database.

Two conventions to keep if you extend the case list for your own domain:

- **Record what the classifier previously returned** alongside the expected
  label, so a change that fixes one case and breaks another shows both.
- **Split cases by site, never by page.** Pages from one site share
  boilerplate and layout, so holding out individual pages while their
  siblings remain in the set makes a change look far more effective than it
  is.

`experiments/extraction_benchmark.py` does the same for extraction, scoring
records against `experiments/data/extraction_gold_labels.csv`. Score both:
classification is constrained to a category name, so a model that drifts out
of the page's language is not penalised there, while extraction reproduces
text from the page and shows it at once.

```bash
python experiments/extraction_benchmark.py --force-category auto --show-misses
```

`--force-category auto` extracts each page as a listing or a single record
according to its gold data, so a classification change cannot show up as an
extraction failure. `--show-misses` prints gold against extracted for every
field that did not match - usually the fastest way from a score to a cause.

`experiments/mislabel_verdict_benchmark.py` measures `mislabel_check`
before you enable it. Pages labelled with an extracting category should be
extracted; pages labelled otherwise are extracted as though misclassified and
should draw the verdict. The number that matters is the first - a wrongly
rejected station is a lost record, which costs more than the call it saves:

```bash
python experiments/mislabel_verdict_benchmark.py --cases dev
python experiments/mislabel_verdict_benchmark.py --cases dev --instruction "..."
```

`experiments/model_sweep.py` runs both benchmarks over every model in
`models/`, one at a time, and prints a comparison table. It waits for the GPU
to be idle before each model and reports a model as unmeasured rather than
scoring it on a card something else is still holding.

`experiments/evaluate_dedup.py <db>` scores entity resolution on a database,
and `experiments/compare_runs.py <db> <db>` puts two runs side by side on
durability, deduplication and output quality.

### Judging what the database holds

Scores of classification and extraction do not say whether the result is
worth having. That takes reading records:

```bash
python experiments/entity_precision.py sample crawl.db sheet.csv --n 100
python experiments/entity_precision.py score sheet.csv
```

`sample` draws a fixed random sample into a labelling sheet - every field,
plus where the record came from - with empty judgement columns. Fill in
`relevant`, `contactable` and (for anything doubtful) `problem`, and `score`
reports the share kept, split by whether a record came from a listing or from
an organisation's own page. Keep the sheets: a later change is measured
against the same records.

Two scripts report where the gaps are:

- `experiments/coverage_gap.py` counts the websites named in records that no
  crawl has visited - records the crawler knows of but has never seen
  first-hand.
- `experiments/record_filter_trial.py` and `list_confirmation_trial.py` score
  a candidate filtering prompt against judged records and labelled listing
  pages, before it is wired into the pipeline.
