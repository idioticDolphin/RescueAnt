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

    created = []

    class DummyLlama:
        def __init__(self, model_path, n_ctx, verbose=True):
            self.model_path = model_path
            self.n_ctx = n_ctx
            self.verbose = verbose
            created.append(self)

    monkeypatch.setattr(llm_service, "Llama", DummyLlama)
    yield created


def test_get_model_id_creates_new_id_for_new_model(_isolate_llm_service_state):
    model_id = llm_service.get_model_id("models/a.gguf", 2048)
    assert model_id == 0
    assert len(_isolate_llm_service_state) == 1
    assert _isolate_llm_service_state[0].model_path == "models/a.gguf"
    assert _isolate_llm_service_state[0].n_ctx == 2048


def test_init_model_loads_quietly(_isolate_llm_service_state):
    # llama.cpp's own verbose logging floods the terminal (hundreds of lines
    # per call) - it must stay disabled so progress/failure logging remains
    # legible.
    llm_service.get_model_id("models/a.gguf", 2048)
    assert _isolate_llm_service_state[0].verbose is False


def test_get_model_id_reuses_id_for_identical_path_and_context(_isolate_llm_service_state):
    first_id = llm_service.get_model_id("models/a.gguf", 2048)
    second_id = llm_service.get_model_id("models/a.gguf", 2048)
    assert first_id == second_id
    # the underlying model should only have been constructed once
    assert len(_isolate_llm_service_state) == 1


def test_get_model_id_creates_distinct_ids_for_different_context(_isolate_llm_service_state):
    first_id = llm_service.get_model_id("models/a.gguf", 2048)
    second_id = llm_service.get_model_id("models/a.gguf", 4096)
    assert first_id != second_id
    assert len(_isolate_llm_service_state) == 2


def test_get_model_id_creates_distinct_ids_for_different_path(_isolate_llm_service_state):
    first_id = llm_service.get_model_id("models/a.gguf", 2048)
    second_id = llm_service.get_model_id("models/b.gguf", 2048)
    assert first_id != second_id


def test_get_model_returns_the_instance_for_that_id(_isolate_llm_service_state):
    model_id = llm_service.get_model_id("models/a.gguf", 2048)
    instance = llm_service.get_model(model_id)
    assert instance is _isolate_llm_service_state[0]
