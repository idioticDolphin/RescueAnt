from llama_cpp import Llama

_initialized_model_mapper = dict()
_initialized_models = []

def get_model_id(model_path, max_tokens):
    global initialized_model_mapper
    next_id = len(initialized_model_mapper)
    model_key = f"{model_path};{max_tokens}"
    if model_key not in initialized_model_mapper.keys():
        initialized_model_mapper[model_key] = next_id
        init_model(model_path, max_tokens)
        return next_id
    return initialized_model_mapper[model_key]

def get_model(id: int):
    return _initialized_models[id]

def init_model(model_path, max_tokens):
    global _initialized_models
    _initialized_models.append(Llama(model_path))