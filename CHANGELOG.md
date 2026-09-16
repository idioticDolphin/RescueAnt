# Changelog

All notable changes to RescueAnt are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Work up to and including the submitted bachelor thesis is treated as the
starting point and is not itemised; everything below is what changed after
submission, while developing toward a first beta.

## [Unreleased] — working toward 0.1.0-beta

### Release criterion

The beta ships when extraction accuracy is good enough that the output is
usable without significant manual cleanup. Concretely, the bar is on two
numbers, measured on a full crawl rather than a sample:

| Measure | At submission | Now | Beta bar |
|---|---|---|---|
| Entity precision — share a person would keep | 64% | ~94% | ≥95% |
| Classification accuracy — 50 labelled pages, 12 categories | n/a (5 categories) | 90–92% | ≥90% sustained |
| Records recoverable after an interrupted run | 26% | 100% | 100% |

Precision is the binding constraint, and the remaining gap is concentrated in
one place: a political party's regional-branch listing page is a genuine list
of organisations, just not of rescues.

Still open before the beta:

- The "mislabeled" return from extraction, so a page the extractor recognises
  as misclassified feeds that back rather than silently costing a 37-second
  call.
- A site-disjoint labelled evaluation set. Hand labels are currently the
  limiting factor on measurement — twice during development the model's
  answer turned out to be right and the label wrong.
- Optional reuse of stored pages across separate crawls, for development runs
  that should not re-hit live sites.

---

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

### Changed

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
- **A flat 120-second call budget truncated every listing extraction.** One
  page fell from 35 records to 5. Introduced and caught in the same session;
  the budget now scales with the work authorised.
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

### Testing

178 → 450 tests. New modules were written test-first, and several tests
encode a finding rather than a wish — the comment on each says which failure
it exists to prevent.
