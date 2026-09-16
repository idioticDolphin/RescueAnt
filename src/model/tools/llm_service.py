import logging

from llama_cpp import Llama

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
    logger.info("Model %s loaded.", model_path)
    return model
