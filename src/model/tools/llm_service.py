import logging

from llama_cpp import Llama

logger = logging.getLogger(__name__)

_initialized_model_mapper = dict()
_initialized_models = []

def get_model_id(model_path, max_tokens):
    """
    Return the id of the Llama model loaded from (model_path, max_tokens),
    loading it via init_model() the first time this exact combination is
    requested and reusing the cached instance on subsequent calls.
    """
    global _initialized_model_mapper
    next_id = len(_initialized_model_mapper)
    model_key = f"{model_path};{max_tokens}"
    if model_key not in _initialized_model_mapper.keys():
        _initialized_model_mapper[model_key] = next_id
        init_model(model_path, max_tokens)
        return next_id
    return _initialized_model_mapper[model_key]

def get_model(id: int):
    """Return the loaded Llama instance for the given model id."""
    return _initialized_models[id]

def init_model(model_path, context):
    """Load a Llama model from disk and append it to the loaded-models cache."""
    global _initialized_models
    logger.info("Loading model %s (context=%d)...", model_path, context)
    # verbose=False: llama.cpp's own logging is extremely chatty (hundreds of
    # lines per load and per generation call) - silence it and rely on our
    # own logging for progress/errors instead.
    _initialized_models.append(
        Llama(
            model_path=model_path,
            n_ctx = context,
            verbose = False,
        )
    )
    logger.info("Model %s loaded.", model_path)