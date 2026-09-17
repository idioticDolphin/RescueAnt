"""
Grammar-constrained greedy sampling that checks the grammar last.

llama-cpp-python builds its sampler chain as penalties -> grammar -> greedy,
so for every generated token the grammar is evaluated against the entire
vocabulary on the CPU. With Qwen's vocabulary that is about a quarter of a
million candidates per token, and it measured ten times slower than
generating without a grammar (8 against 82 tokens/s) - most of the cost of
every extraction.

llama.cpp's own sampler avoids this by picking a token first and asking the
grammar about that one token only, filtering the whole vocabulary just when it
is rejected. Under greedy decoding the result is identical: if the best token
overall is allowed, it is also the best allowed token. This module installs
that order on a loaded model.

Only greedy calls (temperature 0, no logits processors) are rerouted; anything
else keeps llama-cpp-python's own chain, whose result could differ.
"""
import ctypes
import logging

import numpy as np
import llama_cpp
from llama_cpp import _internals

logger = logging.getLogger(__name__)


class GrammarLastSampler:
    """Drop-in for llama-cpp-python's sampler chain on greedy grammar calls.

    Exposes the one method Llama uses on its sampler, sample(ctx, idx), with
    the same contract: return the chosen token and accept it into the
    sampler's state.
    """

    def __init__(self, n_vocab, vocab, grammar, penalty_last_n, penalty_repeat,
                 penalty_freq, penalty_present, api=llama_cpp):
        self._api = api
        self._n_vocab = n_vocab
        # Same call, with the same arguments, as LlamaSampler.add_penalties -
        # so penalties behave exactly as they do in llama-cpp-python's chain.
        self._penalties = api.llama_sampler_init_penalties(
            n_vocab, penalty_last_n, penalty_repeat, penalty_freq, penalty_present)
        self._grammar = api.llama_sampler_init_grammar(
            vocab, grammar._grammar.encode("utf-8"), grammar._root.encode("utf-8"))
        self._candidates = _internals.LlamaTokenDataArray(n_vocab=n_vocab)
        self._one = (api.llama_token_data * 1)()
        self._one_array = api.llama_token_data_array(
            data=self._one, size=1, selected=-1, sorted=False)
        self.full_filter_count = 0
        self.token_count = 0

    def sample(self, ctx, idx=-1):
        api = self._api
        logits = np.ctypeslib.as_array(api.llama_get_logits_ith(ctx.ctx, idx),
                                       shape=(self._n_vocab,))
        candidates = self._candidates
        candidates.copy_logits(logits)
        api.llama_sampler_apply(self._penalties, ctypes.byref(candidates.candidates))
        data = candidates.candidates_data
        token = int(np.argmax(data.logit))

        if not self._allowed(token, float(data.logit[token])):
            api.llama_sampler_apply(self._grammar, ctypes.byref(candidates.candidates))
            token = int(np.argmax(data.logit))
            self.full_filter_count += 1

        api.llama_sampler_accept(self._penalties, token)
        api.llama_sampler_accept(self._grammar, token)
        self.token_count += 1
        return token

    def _allowed(self, token, logit):
        self._one[0].id = token
        self._one[0].logit = logit
        self._one[0].p = 0.0
        self._one_array.size = 1
        self._one_array.selected = -1
        self._one_array.sorted = False
        self._api.llama_sampler_apply(self._grammar, ctypes.byref(self._one_array))
        return self._one[0].logit != float("-inf")

    def close(self):
        for sampler in (self._penalties, self._grammar):
            if sampler:
                self._api.llama_sampler_free(sampler)
        self._penalties = self._grammar = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def is_greedy_grammar_call(kwargs):
    return (kwargs.get("grammar") is not None
            and kwargs.get("temp", 0.8) == 0.0
            and kwargs.get("logits_processor") is None)


def install(model):
    """Route a loaded model's greedy grammar calls through GrammarLastSampler."""
    original = getattr(model, "_init_sampler", None)
    if original is None:
        logger.warning("This llama-cpp-python has no sampler hook; grammar-constrained "
                       "calls will run at its own, much slower speed.")
        return model

    def init_sampler(**kwargs):
        if not is_greedy_grammar_call(kwargs):
            return original(**kwargs)
        return GrammarLastSampler(
            n_vocab=model._n_vocab,
            vocab=model._model.vocab,
            grammar=kwargs["grammar"],
            penalty_last_n=model.last_n_tokens_size,
            penalty_repeat=kwargs.get("repeat_penalty", 1.0),
            penalty_freq=kwargs.get("frequency_penalty", 0.0),
            penalty_present=kwargs.get("presence_penalty", 0.0),
        )

    model._init_sampler = init_sampler
    return model
