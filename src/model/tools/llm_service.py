import logging
import time

from llama_cpp import Llama

from model.tools import grammar_sampler

logger = logging.getLogger(__name__)

_initialized_model_mapper = dict()
_initialized_models = []
# id -> (model_path, context) for models registered but not yet loaded
_model_specs: dict[int, tuple] = {}


def get_model_id(model_path, max_tokens):
    """
    Return the id of the Llama model described by (model_path, max_tokens),
    registering it on first request and reusing the id on subsequent calls.

    Registration does *not* load the model. Loading a multi-gigabyte model
    takes minutes, and configuration is parsed by every entry point - including
    ones that never run inference, such as entity resolution or reprocessing
    with a different stage. The model is loaded on first actual use instead,
    by get_model().
    """
    global _initialized_model_mapper
    next_id = len(_initialized_model_mapper)
    model_key = f"{model_path};{max_tokens}"
    if model_key not in _initialized_model_mapper.keys():
        _initialized_model_mapper[model_key] = next_id
        _model_specs[next_id] = (model_path, max_tokens)
        return next_id
    return _initialized_model_mapper[model_key]


def get_model(id: int):
    """
    Return the Llama instance for the given model id, loading it on first use.
    """
    while len(_initialized_models) <= id:
        _initialized_models.append(None)
    if _initialized_models[id] is None:
        model_path, context = _model_specs[id]
        _initialized_models[id] = _load(model_path, context)
    return _initialized_models[id]


def budget_seconds(max_tokens, floor, tokens_per_second):
    """
    How long a call producing `max_tokens` tokens may reasonably take.

    A flat wall-clock cap cannot serve both a 400-token record extraction and
    an 8000-token listing: the cap that bounds the first truncates the second.
    Measured: a flat 120s cut every LIST extraction in one run, taking one
    listing page from 19 records to 5.

    The guard is meant to catch generation that has stalled or gone
    degenerate, so it is expressed as a minimum acceptable throughput, with a
    floor covering the fixed costs before the first token appears.

    :param floor: minimum budget; 0 disables budgeting entirely.
    """
    if not floor or floor <= 0:
        return 0
    if not max_tokens or not tokens_per_second or tokens_per_second <= 0:
        return floor
    return max(floor, max_tokens / tokens_per_second)


def get_context(id: int) -> int:
    """Return the context size a model id was registered with, without loading
    it. Callers budget their prompt against this; 0 means "unknown", which
    disables trimming."""
    spec = _model_specs.get(id)
    return spec[1] if spec else 0


def fit_to_context(llm, text, context, reserve, overhead=None):
    """
    Trim text so the prompt plus the reserved reply still fits the context.

    llama-cpp refuses the whole call when they don't ("Requested tokens
    (35519) exceed context window of 32768"), losing the page entirely - even
    though its first few thousand tokens usually held everything worth having.
    Trimming keeps a prefix: a page's own content comes first, navigation and
    legal chrome come last.

    :param context: the model's context size; 0 disables trimming.
    :param reserve: tokens to keep free for the reply.
    :param overhead: other prompt text that shares the same window - the
                      system message carrying the category definitions runs to
                      several hundred tokens, and budgeting only the page let
                      the total overflow anyway.
    """
    if not context or not text:
        return text
    tokenize = getattr(llm, "tokenize", None)
    if tokenize is None:
        return text

    # Leave a small margin for the chat template's own wrapper tokens. Scaled
    # down for small contexts so the margin can't swallow the whole budget.
    margin = min(_CONTEXT_MARGIN_TOKENS, context // 10)
    budget = max(0, context - reserve - margin)
    try:
        if overhead:
            budget = max(0, budget - len(tokenize(str(overhead).encode("utf-8"))))
        tokens = tokenize(text.encode("utf-8"))
        if len(tokens) <= budget:
            return text
        # Cut by character ratio rather than detokenising: it needs no
        # detokenise API, and overshooting slightly is harmless.
        keep = int(len(text) * budget / len(tokens))
        trimmed = text[:keep]
        while trimmed and len(tokenize(trimmed.encode("utf-8"))) > budget:
            trimmed = trimmed[: int(len(trimmed) * 0.9)]
    except Exception as e:
        logger.warning("Could not measure prompt length (%s) - passing it through.", e)
        return text

    logger.info("Trimmed prompt from %d to %d characters to fit the context window.",
                len(text), len(trimmed))
    return trimmed


# Chat templates add role markers and control tokens around the prompt.
_CONTEXT_MARGIN_TOKENS = 256


def complete(llm, messages, timeout_seconds=0, clock=None, **kwargs):
    """
    Run a chat completion under an optional wall-clock budget.

    max_tokens caps how much a call generates but not how long that takes: on
    a contended GPU a capped call can still run for tens of minutes. One
    observed extraction spent 1852 seconds on a single large page and returned
    nothing usable, while the rest of the crawl waited.

    With no budget (timeout_seconds <= 0) this is the plain blocking call.
    With a budget it streams instead, so generation can actually be abandoned
    mid-flight - stopping the model rather than merely ignoring its answer -
    and returns whatever was produced so far. The return value has the same
    shape as the blocking call, plus a "truncated" flag.
    """
    if timeout_seconds and timeout_seconds > 0:
        return _complete_streaming(llm, messages, timeout_seconds,
                                   clock or time.monotonic, **kwargs)
    result = llm.create_chat_completion(messages=messages, **kwargs)
    if isinstance(result, dict):
        result.setdefault("truncated", False)
    return result


def _complete_streaming(llm, messages, timeout_seconds, clock, **kwargs):
    started = clock()
    parts = []
    truncated = False
    stream = llm.create_chat_completion(messages=messages, stream=True, **kwargs)
    try:
        for chunk in stream:
            piece = _chunk_text(chunk)
            if piece:
                parts.append(piece)
            if clock() - started > timeout_seconds:
                truncated = True
                break
    finally:
        # Abandoning the generator without closing it leaves the model
        # generating into a buffer nobody reads.
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    if truncated:
        logger.warning(
            "Abandoned generation after %.0fs budget (kept %d characters).",
            timeout_seconds, sum(len(p) for p in parts),
        )
    return {
        "choices": [{"message": {"content": "".join(parts)}}],
        "truncated": truncated,
    }


def _chunk_text(chunk):
    """Pull the text out of a streaming delta, tolerating role-only and
    finish-reason chunks that carry no content."""
    try:
        return chunk["choices"][0].get("delta", {}).get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def init_model(model_path, context):
    """Load a Llama model from disk and append it to the loaded-models cache.

    Retained for callers that want a model materialised eagerly; the normal
    path is registration via get_model_id() plus lazy loading in get_model()."""
    global _initialized_models
    _initialized_models.append(_load(model_path, context))


def _load(model_path, context):
    logger.info("Loading model %s (context=%d)...", model_path, context)
    # verbose=False: llama.cpp's own logging is extremely chatty (hundreds of
    # lines per load and per generation call) - silence it and rely on our
    # own logging for progress/errors instead.
    model = Llama(
        model_path=model_path,
        n_ctx = context,
        n_gpu_layers = -1,
        verbose = False,
    )
    # Grammar-constrained extraction and classification otherwise check every
    # vocabulary entry for every token, several times slower than need be.
    grammar_sampler.install(model)
    logger.info("Model %s loaded.", model_path)
    return model
