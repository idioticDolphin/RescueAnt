# Changelog

All notable changes to RescueAnt are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries begin at the first release; earlier development is not itemised.

## [Unreleased]

### Added

- **Interruptible crawling.** Every fetched page body is written to a
  content-addressed store (SHA-256, gzipped) the moment its fetch succeeds,
  and each page carries an explicit lifecycle state. A killed run resumes
  from disk with no refetching.
- **`--reprocess extract|categorize`**, with `--site` and `--category` to
  narrow it. Re-runs analysis over stored pages after a prompt, schema,
  budget or model change — same corpus, no network.
- **`--resolve`**, entity resolution. Records are blocked, scored, clustered
  and fused into `entities`, driven by config-declared field *roles* rather
  than field names. `entries` remains an immutable observation log;
  conflicting values are recorded in `entity_conflicts`, not discarded.
- **Twelve-category page taxonomy** in `config/taxonomy.config`, replacing
  five. Adds `SHELTER`, `SANCTUARY`, `VET`, `ADVICE`, `ANIMAL`, `ADVOCACY`,
  `AUTHORITY` and `COMMERCIAL`. Extracted categories are told apart by the
  category recorded on their crawl row, so a consumer filters for what it
  wants.
- **Multi-file configuration** via `include`, with cycle detection and
  later-file-wins overrides. Retargeting the crawler is now a matter of
  swapping `taxonomy.config` and `schema.config`.
- **Multiple seed files.** `starting_url_file` takes one path or a list;
  blank lines and `#` comments are ignored.
- **Frontier prioritisation.** Links inherit a score from the category of the
  page that offered them, plus URL path-token priors and depth.
- **Per-site budgets** — a page ceiling and a separate extraction ceiling, so
  one site cannot absorb the crawl.
- **Config-driven domain denylist.**
- **Admissibility gate** — a record must carry the configured required fields
  and at least one field with an identifying or locating role, or it is not
  stored.
- **Field-value plausibility checks**, driven by each field's declared
  normaliser. A date in a telephone field or a URL in an e-mail field is
  dropped; only the offending value, never the record.
- **Site boilerplate stripping**, learned per site from stored pages.
- **`experiments/categorization_benchmark.py`** — replays hand-labelled
  stored pages through the classifier, with per-case verdicts and timing, and
  a `--model` override for comparing models on identical input.
- **Session monitoring** — one JSON line per event, flushed immediately, so
  an interrupted run's data survives.
- **Model catalogue** in `models.csv`, read by `download-models.sh`. Download
  one model by name, several, or `--all`; `--list` shows the catalogue. Each
  entry records the largest context measured to fit an 8 GB card. Adding a
  model is a line of CSV.
- **`experiments/extraction_benchmark.py`** — scores extraction against
  hand-labelled records, replaying stored pages. `--force-category` extracts
  every page as a named category, so a classification change cannot register
  as an extraction failure.
- **`experiments/model_sweep.py`** — runs both benchmarks over every
  downloaded model, one subprocess per model so VRAM is released between
  them. Probes for the largest usable context where none is recorded, and
  refuses to score a model unless the card is idle first.
- **Failed fetches record why they failed** in the crawl row's `last_error`.
- **`abandon_site_after`**: a site that yields only low-value pages - by its
  categories' link weights - and no record is dropped after a few pages,
  rather than when its page budget runs out. A site that has given a record
  is never dropped, and pages worth following for their links do not count.
- **`skip_url_extensions`**: links to documents, images and archives are
  dropped before fetching. A browser starts a download for them instead of
  rendering a page, so each one cost a failed navigation.
- **`reuse_stored_pages`**, for development runs: pages already in the page
  store are served from disk rather than refetched, with no request to the
  site. The store keeps a URL index of its own, so this works against a fresh
  crawl database. `--index-store` imports the pages recorded by earlier crawl
  databases.
- **`mislabel_check`**: extraction may answer that a page is not what it was
  classified as, at a cost of a few tokens instead of a full record. The page
  is re-filed and its original category kept in `reclassified_from`. Off by
  default.
- **`--show-misses`** on the extraction benchmark, printing gold against
  extracted for every field that did not match.
- **Labelled page set** in `experiments/data/page_labels.csv`: each label
  carries a confidence and a site-disjoint `dev`/`test` split. The
  categorization benchmark takes `--split` and `--include-unsure`.
- **`experiments/mislabel_verdict_benchmark.py`** — measures how often the
  mislabel verdict rejects a page that should have been extracted, and how
  often it catches one that should not, against the labelled page set.
- **Extraction decision score** in the categorization benchmark: whether each
  page is extracted as a record, as a listing or not at all, with missed and
  spurious extractions counted apart. Per-page results for the test split are
  hidden unless `--show-test-cases` is given.
- **`experiments/extraction_profile.py`** splits extraction calls into prompt
  processing and generation, and **`grammar_sampler_check.py`** verifies that
  the fast sampler produces the same completions as llama-cpp-python's own.

### Changed

- **Grammar-constrained generation is about seven times faster, with
  identical output.** llama-cpp-python checks the grammar against the whole
  vocabulary for every generated token; RescueAnt now picks the most likely
  token first and asks the grammar about that token alone, filtering the
  vocabulary only when it is rejected. Under greedy decoding this chooses the
  same tokens. A typical single-record extraction takes about 5 seconds
  instead of 35, and a 2,000-token listing 36 seconds instead of 4 minutes.
- **Discovery now triggers on an unproductive frontier**, not an empty queue.
  Once link-following reaches the open web the queue never empties, so the
  old condition meant discovery never fired again after the seeds ran out.
- **Generation budgets scale with each category's token allowance** rather
  than being a flat wall-clock cap.
- **Models load lazily.** Configuration is parsed by every entry point,
  including those that run no inference; `--resolve` went from ~4 minutes to
  ~1 second.
- **`<nav>` is stripped structurally** before classification and extraction.
  286 of 300 sampled pages carry one, and removing it drops 46% of all text —
  and so 46% of the tokens through every model call. Link discovery is
  unaffected, since it parses the raw HTML. `header`/`footer` are kept
  deliberately: that is where an organisation's own address usually sits.
- **URL canonicalisation** on queueing — trailing slashes, duplicate slashes,
  fragments, tracking parameters and directory-index filenames collapse to
  one entry.
- **`http://` and `https://` of one address are treated as the same page**
  for deduplication, without rewriting the scheme — sites that serve only
  http must stay fetchable.
- **WAL journalling with per-transaction commits**, so a commit survives an
  abrupt kill.

### Fixed

- **Pages fetched but never processed were recorded as finished** and
  therefore never retried. 4,303 of 5,796 pages in one run — 74% of its
  fetching — were unreachable this way.
- **Extraction ignored its configured token cap**, so a degenerate repetition
  ran to the context limit: 90–120 minutes ending in unparseable output.
- **A flat call budget truncated every listing extraction.** One page
  yielded 5 records where the page held 35. The budget now scales with the
  tokens each category is authorised to produce.
- **Oversized prompts were refused outright**, losing the page. They are now
  trimmed to fit, counting the instructions as well as the page text.
- **A browser-driver crash ended the run.** The session is now retried with a
  fresh browser, skipping pages the failed attempt already fetched.
- **A whole fetch batch was held in memory before anything was written**, so a
  mid-batch stop lost all of it.
- **Resumed pages were also queued for a fresh fetch**, producing a second
  crawl row and duplicate records.
- **`--reprocess categorize` did not re-categorise.** It reset the lifecycle
  state but kept the stored category, which `resume_pending()` then reused.
- **Entity resolution split one organisation across subpages** that prefixed
  its name with the page topic. A contained label now counts as agreement —
  while six genuinely different organisations sharing an umbrella site's URL
  still stay apart, with a test naming them.
- **The crawler's own markup leaked into extracted values** — `tel:` hrefs
  were inlined beside the link text they duplicate, and heading markers came
  back inside fields.
- **38 of 131 failed fetches** went to share widgets and consent
  infrastructure that can never hold a target; these are now denylisted.
- **Gold labels were read with the platform's default encoding**, so on
  Windows every umlaut arrived garbled and `accepted_animals` scored 0.00 for
  every record regardless of what was extracted.
- **Extraction translated values out of the page's language** — a German
  page's "Greifvögel" was stored as "Raptors". Extraction prompts now require
  the page's own wording.
- **A telephone field holding two numbers** joined by a word ("… oder …")
  was either discarded or stored unusable. The first number is kept, found by
  its shape rather than by the joining word, so it works in any language.
- **An interrupted model download was left at the real filename**, where it
  could be loaded as if complete. Downloads now go to `.part` and are renamed
  on success.

### Testing

178 → 513 tests. Each regression test names the failure it exists to
prevent, so the suite doubles as a record of what has gone wrong before.
