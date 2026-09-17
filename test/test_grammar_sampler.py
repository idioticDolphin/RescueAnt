import ctypes
from types import SimpleNamespace
from unittest.mock import MagicMock

import llama_cpp

from model.tools import grammar_sampler


class FakeApi:
    """Just enough of llama_cpp for the sampler: logits come from a list, the
    grammar allows a fixed set of tokens, penalties subtract from penalised
    tokens."""

    llama_token_data = llama_cpp.llama_token_data
    llama_token_data_array = llama_cpp.llama_token_data_array

    def __init__(self, logits, allowed, penalised=()):
        self.logits = (ctypes.c_float * len(logits))(*logits)
        self.allowed = set(allowed)
        self.penalised = set(penalised)
        self.grammar_sizes = []
        self.accepted = []
        self.freed = []

    def llama_sampler_init_penalties(self, *args):
        return "penalties"

    def llama_sampler_init_grammar(self, vocab, grammar, root):
        return "grammar"

    def llama_get_logits_ith(self, ctx, idx):
        return ctypes.cast(self.logits, ctypes.POINTER(ctypes.c_float))

    def llama_sampler_apply(self, sampler, ref):
        array = ref._obj
        items = ctypes.cast(array.data, ctypes.POINTER(llama_cpp.llama_token_data))
        if sampler == "grammar":
            self.grammar_sizes.append(array.size)
        for i in range(array.size):
            if sampler == "grammar" and items[i].id not in self.allowed:
                items[i].logit = float("-inf")
            if sampler == "penalties" and items[i].id in self.penalised:
                items[i].logit -= 10.0

    def llama_sampler_accept(self, sampler, token):
        self.accepted.append((sampler, token))

    def llama_sampler_free(self, sampler):
        self.freed.append(sampler)


def _sampler(api):
    grammar = SimpleNamespace(_grammar="root ::= x", _root="root")
    return grammar_sampler.GrammarLastSampler(
        n_vocab=len(api.logits), vocab=None, grammar=grammar, penalty_last_n=64,
        penalty_repeat=1.1, penalty_freq=0.0, penalty_present=0.0, api=api)


CTX = SimpleNamespace(ctx=None)


def test_an_allowed_best_token_is_taken_without_filtering_the_vocabulary():
    api = FakeApi(logits=[0.1, 3.0, 0.5, 2.0], allowed={1, 3})
    sampler = _sampler(api)

    assert sampler.sample(CTX) == 1
    assert api.grammar_sizes == [1]
    assert sampler.full_filter_count == 0


def test_a_rejected_best_token_falls_back_to_the_best_allowed_one():
    api = FakeApi(logits=[0.1, 3.0, 0.5, 2.0], allowed={0, 2})
    sampler = _sampler(api)

    assert sampler.sample(CTX) == 2
    assert api.grammar_sizes == [1, 4]
    assert sampler.full_filter_count == 1


def test_penalties_apply_before_the_choice():
    api = FakeApi(logits=[0.1, 3.0, 0.5, 2.0], allowed={1, 3}, penalised={1})
    assert _sampler(api).sample(CTX) == 3


def test_ties_go_to_the_lowest_token_id_as_in_greedy_sampling():
    api = FakeApi(logits=[1.0, 2.0, 2.0], allowed={1, 2})
    assert _sampler(api).sample(CTX) == 1


def test_the_chosen_token_is_accepted_by_penalties_and_grammar():
    api = FakeApi(logits=[0.1, 3.0, 0.5], allowed={2})
    _sampler(api).sample(CTX)
    assert api.accepted == [("penalties", 2), ("grammar", 2)]


def test_repeated_samples_reuse_fresh_logits():
    api = FakeApi(logits=[0.1, 3.0, 0.5], allowed={0, 1, 2})
    sampler = _sampler(api)
    assert sampler.sample(CTX) == 1
    api.logits[2] = 9.0
    assert sampler.sample(CTX) == 2


def test_close_frees_both_samplers_once():
    api = FakeApi(logits=[0.0], allowed={0})
    sampler = _sampler(api)
    sampler.close()
    sampler.close()
    assert sorted(api.freed) == ["grammar", "penalties"]


def _fake_model():
    model = MagicMock()
    model._n_vocab = 4
    model.last_n_tokens_size = 64
    stock = MagicMock(return_value="stock chain")
    model._init_sampler = stock
    return model, stock


def test_install_routes_greedy_grammar_calls(monkeypatch):
    model, stock = _fake_model()
    built = MagicMock(return_value="grammar-last")
    monkeypatch.setattr(grammar_sampler, "GrammarLastSampler", built)
    grammar_sampler.install(model)

    assert model._init_sampler(temp=0.0, grammar=object(), repeat_penalty=1.1) == "grammar-last"
    assert built.call_args.kwargs["penalty_repeat"] == 1.1
    stock.assert_not_called()


def test_install_leaves_other_calls_to_the_stock_chain(monkeypatch):
    model, stock = _fake_model()
    monkeypatch.setattr(grammar_sampler, "GrammarLastSampler", MagicMock())
    grammar_sampler.install(model)

    assert model._init_sampler(temp=0.0, grammar=None) == "stock chain"
    assert model._init_sampler(temp=0.7, grammar=object()) == "stock chain"
    assert model._init_sampler(temp=0.0, grammar=object(),
                               logits_processor=[lambda ids, logits: logits]) == "stock chain"


def test_install_leaves_a_model_without_the_expected_hook_alone():
    # A llama-cpp-python release that renames the hook must not break loading;
    # the model then simply samples the slow way.
    model = SimpleNamespace()
    assert grammar_sampler.install(model) is model
    assert not hasattr(model, "_init_sampler")
