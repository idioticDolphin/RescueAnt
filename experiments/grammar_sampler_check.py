"""
Check that the grammar-last sampler changes speed and nothing else.

Extracts stored pages twice with the same model instance - once through
llama-cpp-python's own sampler chain, once through GrammarLastSampler - and
compares the completions character for character. Any difference is a bug:
under greedy decoding the two orders must choose the same tokens.

Usage:
    python experiments/grammar_sampler_check.py [--single 6] [--lists 2]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, grammar_sampler, llm_service, page_store  # noqa: E402
from model.analyzer import cleaning_service, boilerplate_service  # noqa: E402


def run(llm, messages, schema, max_tokens, repeat_penalty):
    started = time.monotonic()
    result = llm.create_chat_completion(
        messages=messages, temperature=0, max_tokens=max_tokens,
        repeat_penalty=repeat_penalty,
        response_format={"type": "json_object", "schema": schema})
    return result["choices"][0]["message"]["content"], time.monotonic() - started, \
        result["usage"]["completion_tokens"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--single", type=int, default=6)
    ap.add_argument("--lists", type=int, default=2)
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)
    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()

    with sqlite3.connect(args.db) as c:
        single = c.execute("SELECT source_url, content_path, category FROM crawls WHERE "
                           "content_path IS NOT NULL AND category IN ('STATION','SHELTER','VET') "
                           "ORDER BY source_url LIMIT ?", (args.single,)).fetchall()
        lists = c.execute("SELECT source_url, content_path, category FROM crawls WHERE "
                          "content_path IS NOT NULL AND category = 'LIST' "
                          "ORDER BY source_url LIMIT ?", (args.lists,)).fetchall()

    llm = None
    stock = None
    same = differ = 0
    total_stock = total_fast = 0.0
    for url, path, name in single + lists:
        html = page_store.load(path)
        if not html:
            continue
        category = config.get_category(name)
        if llm is None:
            llm = llm_service.get_model(category.analysis_model_id)
            stock = llm._init_sampler
        prompt = f"{category.analysis_prompt} The return schema is {category.fields}"
        text = boilerplate_service.strip_for(cleaning_service.clean(html), url, config)
        text = llm_service.fit_to_context(
            llm, text, llm_service.get_context(category.analysis_model_id),
            category.analysis_max_tokens, overhead=prompt)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"Website content:\n{text}"}]

        llm._init_sampler = stock
        a, a_s, a_n = run(llm, messages, category.fields, category.analysis_max_tokens,
                          config.repeat_penalty)
        grammar_sampler.install(llm)
        b, b_s, b_n = run(llm, messages, category.fields, category.analysis_max_tokens,
                          config.repeat_penalty)
        llm._init_sampler = stock

        total_stock += a_s
        total_fast += b_s
        verdict = "identical" if a == b else "DIFFERENT"
        same += a == b
        differ += a != b
        print(f"{name:<8} {verdict:<9} stock {a_s:6.1f}s ({a_n} tok)   "
              f"grammar-last {b_s:6.1f}s ({b_n} tok)   {url.split('//', 1)[-1][:45]}", flush=True)
        if a != b:
            cut = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            print(f"   first difference at character {cut}:\n"
                  f"   stock:        {a[max(0, cut - 40):cut + 40]!r}\n"
                  f"   grammar-last: {b[max(0, cut - 40):cut + 40]!r}")

    print(f"\n{same} identical, {differ} different")
    if total_fast:
        print(f"time: stock {total_stock:.0f}s, grammar-last {total_fast:.0f}s "
              f"({total_stock / total_fast:.1f}x)")


if __name__ == "__main__":
    main()
