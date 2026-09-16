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
