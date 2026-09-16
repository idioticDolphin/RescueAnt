import pytest

import model.tools.llm_service as llm_service


@pytest.fixture(autouse=True)
def _isolate_llm_service_state(monkeypatch):
    """
    llm_service keeps module-level global caches (_initialized_model_mapper,
    _initialized_models). Reset them around each test so tests don't leak
    state into each other, and stub out the real Llama() constructor so we
    never try to load an actual model file from disk.
    """
    monkeypatch.setattr(llm_service, "_initialized_model_mapper", {})
    monkeypatch.setattr(llm_service, "_initialized_models", [])
    monkeypatch.setattr(llm_service, "_model_specs", {})

    created = []

    class DummyLlama:
        def __init__(self, model_path, n_ctx, n_gpu_layers=0, verbose=True):
            self.model_path = model_path
            self.n_ctx = n_ctx
            self.n_gpu_layers = n_gpu_layers
            self.verbose = verbose
            created.append(self)

    monkeypatch.setattr(llm_service, "Llama", DummyLlama)
    yield created


def test_get_model_id_creates_new_id_for_new_model(_isolate_llm_service_state):
    model_id = llm_service.get_model_id("models/a.gguf", 2048)
    assert model_id == 0
    # registration is lazy - nothing is constructed until the model is used
    assert len(_isolate_llm_service_state) == 0
    llm_service.get_model(model_id)
    assert len(_isolate_llm_service_state) == 1
    assert _isolate_llm_service_state[0].model_path == "models/a.gguf"
    assert _isolate_llm_service_state[0].n_ctx == 2048


def test_init_model_loads_quietly(_isolate_llm_service_state):
    # llama.cpp's own verbose logging floods the terminal (hundreds of lines
    # per call) - it must stay disabled so progress/failure logging remains
    # legible.
    llm_service.get_model(llm_service.get_model_id("models/a.gguf", 2048))
    assert _isolate_llm_service_state[0].verbose is False


def test_get_model_id_reuses_id_for_identical_path_and_context(_isolate_llm_service_state):
    first_id = llm_service.get_model_id("models/a.gguf", 2048)
    second_id = llm_service.get_model_id("models/a.gguf", 2048)
    assert first_id == second_id
    llm_service.get_model(first_id)
    llm_service.get_model(second_id)
    # the underlying model should only have been constructed once
    assert len(_isolate_llm_service_state) == 1


def test_get_model_id_creates_distinct_ids_for_different_context(_isolate_llm_service_state):
    first_id = llm_service.get_model_id("models/a.gguf", 2048)
    second_id = llm_service.get_model_id("models/a.gguf", 4096)
    assert first_id != second_id
    llm_service.get_model(first_id)
    llm_service.get_model(second_id)
    assert len(_isolate_llm_service_state) == 2


def test_get_model_id_creates_distinct_ids_for_different_path(_isolate_llm_service_state):
    first_id = llm_service.get_model_id("models/a.gguf", 2048)
    second_id = llm_service.get_model_id("models/b.gguf", 2048)
    assert first_id != second_id


def test_get_model_returns_the_instance_for_that_id(_isolate_llm_service_state):
    model_id = llm_service.get_model_id("models/a.gguf", 2048)
    instance = llm_service.get_model(model_id)
    assert instance is _isolate_llm_service_state[0]


# ---------------------------------------------------------------------------
# lazy loading
# ---------------------------------------------------------------------------

def test_registering_a_model_does_not_load_it(_isolate_llm_service_state):
    """Config parsing happens in every entry point, including ones that never
    run inference; loading a multi-gigabyte model there wastes minutes."""
    loaded = []

    import model.tools.llm_service as svc
    original = svc._load
    svc._load = lambda path, ctx: loaded.append((path, ctx)) or object()
    try:
        model_id = svc.get_model_id("models/x.gguf", 2048)
        assert loaded == []            # registered, not loaded
        svc.get_model(model_id)
        assert loaded == [("models/x.gguf", 2048)]
    finally:
        svc._load = original


def test_model_is_loaded_only_once(_isolate_llm_service_state):
    import model.tools.llm_service as svc
    loaded = []
    original = svc._load
    svc._load = lambda path, ctx: loaded.append(path) or object()
    try:
        model_id = svc.get_model_id("models/x.gguf", 2048)
        svc.get_model(model_id)
        svc.get_model(model_id)
        assert len(loaded) == 1
    finally:
        svc._load = original


def test_distinct_specs_get_distinct_ids(_isolate_llm_service_state):
    import model.tools.llm_service as svc
    a = svc.get_model_id("models/a.gguf", 2048)
    b = svc.get_model_id("models/b.gguf", 2048)
    same = svc.get_model_id("models/a.gguf", 2048)
    assert a != b and a == same


# ---------------------------------------------------------------------------
# call deadlines
#
# A single extraction call was observed running for 30 minutes on a large,
# irrelevant page (dogorama.app/de-de/ernaehrungsberater: 1852s) and returning
# nothing usable. max_tokens caps the token count but not wall-clock time, and
# on a loaded GPU a capped call can still take tens of minutes. complete()
# enforces an actual time budget by streaming and abandoning generation once
# the deadline passes.
# ---------------------------------------------------------------------------

class _FakeLlama:
    """Records how it was called and replays a scripted stream."""

    def __init__(self, chunks=(), blocking_result=None):
        self._chunks = list(chunks)
        self._blocking_result = blocking_result or {
            "choices": [{"message": {"content": "blocking"}}]
        }
        self.calls = []
        self.stream_closed = False

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("stream"):
            return self._blocking_result
        return self._make_stream()

    def _make_stream(self):
        outer = self

        def gen():
            try:
                for chunk in outer._chunks:
                    yield {"choices": [{"delta": {"content": chunk}}]}
            finally:
                outer.stream_closed = True

        return gen()


def test_complete_without_a_timeout_uses_the_blocking_call():
    """Zero/absent budget must keep the previous behaviour exactly - streaming
    has overhead and the blocking path is well understood."""
    llm = _FakeLlama()
    out = llm_service.complete(llm, messages=[{"role": "user", "content": "x"}],
                               timeout_seconds=0, temperature=0)
    assert out["choices"][0]["message"]["content"] == "blocking"
    assert llm.calls[0].get("stream") is not True
    # caller kwargs are passed straight through
    assert llm.calls[0]["temperature"] == 0


def test_complete_with_a_timeout_streams_and_returns_the_blocking_shape():
    """Callers index result['choices'][0]['message']['content']; the streaming
    path must produce that same shape so call sites stay unchanged."""
    llm = _FakeLlama(chunks=["Hel", "lo", " world"])
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4]).__next__
    out = llm_service.complete(llm, messages=[], timeout_seconds=30, clock=clock)
    assert out["choices"][0]["message"]["content"] == "Hello world"
    assert out["truncated"] is False
    assert llm.calls[0]["stream"] is True


def test_complete_abandons_generation_once_the_deadline_passes():
    llm = _FakeLlama(chunks=[f"tok{i}" for i in range(100)])
    # start at 0, then jump past a 10s budget on the third token
    clock = iter([0.0, 1.0, 2.0] + [999.0] * 200).__next__
    out = llm_service.complete(llm, messages=[], timeout_seconds=10, clock=clock)
    text = out["choices"][0]["message"]["content"]
    assert out["truncated"] is True
    # it kept what it had rather than throwing the partial answer away
    assert text.startswith("tok0")
    # and it stopped early instead of draining all 100 chunks
    assert len(text) < len("".join(f"tok{i}" for i in range(100)))


def test_complete_closes_the_stream_when_it_gives_up():
    """An abandoned generator would otherwise keep the model busy; the GPU is
    the crawl's scarcest resource."""
    llm = _FakeLlama(chunks=[f"tok{i}" for i in range(100)])
    clock = iter([0.0] + [999.0] * 200).__next__
    llm_service.complete(llm, messages=[], timeout_seconds=5, clock=clock)
    assert llm.stream_closed is True


def test_complete_that_finishes_in_time_is_not_marked_truncated():
    llm = _FakeLlama(chunks=["a", "b"])
    clock = iter([0.0, 0.1, 0.2, 0.3]).__next__
    out = llm_service.complete(llm, messages=[], timeout_seconds=60, clock=clock)
    assert out["truncated"] is False
    assert out["choices"][0]["message"]["content"] == "ab"


def test_complete_tolerates_chunks_without_content():
    """llama-cpp emits role-only and finish-reason deltas with no 'content'."""
    llm = _FakeLlama()
    llm._chunks = []

    def gen():
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        yield {"choices": [{"delta": {"content": "hi"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    llm.create_chat_completion = lambda **kw: gen() if kw.get("stream") else None
    clock = iter([0.0] * 10).__next__
    out = llm_service.complete(llm, messages=[], timeout_seconds=60, clock=clock)
    assert out["choices"][0]["message"]["content"] == "hi"


# ---------------------------------------------------------------------------
# fitting input to the context window
#
# "Requested tokens (35519) exceed context window of 32768" aborted extraction
# outright: the page was lost even though its first few thousand tokens held
# everything worth having. Input has to be trimmed to leave room for the
# reply instead.
# ---------------------------------------------------------------------------

class _TokenizingLlama:
    """Tokenizes on whitespace - enough to exercise the budget arithmetic."""

    def __init__(self):
        self.tokenize_calls = 0

    def tokenize(self, raw, add_bos=True, special=False):
        self.tokenize_calls += 1
        return raw.decode("utf-8", "replace").split()


def test_fit_to_context_leaves_short_text_alone():
    llm = _TokenizingLlama()
    text = "a b c"
    assert llm_service.fit_to_context(llm, text, context=100, reserve=10) == text


def test_fit_to_context_trims_text_that_would_overflow():
    llm = _TokenizingLlama()
    text = " ".join(str(i) for i in range(500))
    out = llm_service.fit_to_context(llm, text, context=100, reserve=40)
    assert len(out) < len(text)
    # what survives is a prefix: the head of a page holds its own content,
    # the tail is usually navigation and legal chrome
    assert text.startswith(out.rstrip())


def test_fit_to_context_keeps_the_result_within_budget():
    llm = _TokenizingLlama()
    text = " ".join(str(i) for i in range(500))
    out = llm_service.fit_to_context(llm, text, context=100, reserve=40)
    assert len(llm.tokenize(out.encode("utf-8"))) <= 60


def test_fit_to_context_is_a_noop_without_a_context_size():
    """Categories that configure no context must behave exactly as before."""
    llm = _TokenizingLlama()
    text = " ".join(str(i) for i in range(500))
    assert llm_service.fit_to_context(llm, text, context=0, reserve=40) == text
    assert llm.tokenize_calls == 0


def test_fit_to_context_survives_a_model_without_a_tokenizer():
    """Never let a trimming helper be the thing that breaks a call."""
    text = "some text"
    assert llm_service.fit_to_context(object(), text, context=10, reserve=5) == text


def test_fit_to_context_handles_a_reserve_larger_than_the_context():
    """Misconfiguration must degrade to a small prompt, not a negative slice."""
    llm = _TokenizingLlama()
    out = llm_service.fit_to_context(llm, "a b c d e", context=10, reserve=99)
    assert out == "" or len(out) < len("a b c d e")


def test_get_context_returns_the_registered_context_size(_isolate_llm_service_state):
    """Callers need the context size to budget their prompt, and must be able
    to ask for it without loading the model."""
    model_id = llm_service.get_model_id("models/a.gguf", 4096)
    assert llm_service.get_context(model_id) == 4096
    assert len(_isolate_llm_service_state) == 0  # still not loaded


def test_get_context_of_an_unknown_id_is_zero(_isolate_llm_service_state):
    """Zero disables trimming, which is the safe default for a caller that
    cannot find out the budget."""
    assert llm_service.get_context(99) == 0


# ---------------------------------------------------------------------------
# sizing the budget to the work
#
# A flat 120s cap was a regression: it is generous for a 400-token station
# extraction and brutal for an 8000-token listing. In the first run with it
# enabled, *every* LIST extraction hit the cap - vbu-ffm.de/pflegestationen
# fell from 19 records to 5. The guard exists to catch generation that has
# stalled or gone degenerate, not generation that is legitimately long, so
# the budget has to scale with the tokens the call was authorised to produce.
# ---------------------------------------------------------------------------

def test_budget_scales_with_the_authorised_token_count():
    short = llm_service.budget_seconds(400, floor=120, tokens_per_second=10)
    long = llm_service.budget_seconds(8000, floor=120, tokens_per_second=10)
    assert long > short
    assert long == 800


def test_budget_never_falls_below_the_floor():
    """A small call still deserves a grace period - model load, prompt
    ingestion and a cold cache all happen before the first token."""
    assert llm_service.budget_seconds(50, floor=120, tokens_per_second=10) == 120


def test_budget_is_disabled_when_the_floor_is_disabled():
    """Zero must keep meaning "no budget", so the feature stays opt-in."""
    assert llm_service.budget_seconds(8000, floor=0, tokens_per_second=10) == 0


def test_budget_without_a_token_cap_falls_back_to_the_floor():
    """An uncapped call has no authorised size to scale from."""
    assert llm_service.budget_seconds(None, floor=120, tokens_per_second=10) == 120
    assert llm_service.budget_seconds(0, floor=120, tokens_per_second=10) == 120


def test_budget_tolerates_a_nonsensical_throughput():
    assert llm_service.budget_seconds(8000, floor=120, tokens_per_second=0) == 120
