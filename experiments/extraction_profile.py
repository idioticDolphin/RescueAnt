"""
Where does an extraction call spend its time?

Replays stored single-record pages through the extraction prompt and splits
each call into prompt processing (time to first token) and generation (tokens
out per second), once with the response schema enforced and once without it,
so the cost of constrained decoding can be read off directly.

Usage:
    python experiments/extraction_profile.py [--pages 8] [--category STATION]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, llm_service, page_store  # noqa: E402
from model.analyzer import cleaning_service, boilerplate_service  # noqa: E402


def timed_call(llm, messages, max_tokens, response_format):
    started = time.monotonic()
    first = None
    pieces = 0
    kwargs = dict(messages=messages, stream=True, temperature=0, max_tokens=max_tokens)
    if response_format:
        kwargs["response_format"] = response_format
    for chunk in llm.create_chat_completion(**kwargs):
        text = chunk["choices"][0].get("delta", {}).get("content")
        if text:
            if first is None:
                first = time.monotonic()
            pieces += 1
    ended = time.monotonic()
    first = first or ended
    return first - started, ended - first, pieces


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--category", default="STATION")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)
    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()
    category = config.get_category(args.category)

    with sqlite3.connect(args.db) as c:
        rows = c.execute(
            "SELECT source_url, content_path FROM crawls WHERE category = ? "
            "AND content_path IS NOT NULL ORDER BY source_url LIMIT ?",
            (args.category, args.pages)).fetchall()

    llm = llm_service.get_model(category.analysis_model_id)
    prompt = f"{category.analysis_prompt} The return schema is {category.fields}"
    print(f"{'prompt tok':>10} {'ttft s':>7} {'gen s':>7} {'out':>5} {'tok/s':>6} | "
          f"{'no grammar':>9} {'gen s':>7} {'out':>5} {'tok/s':>6}  page", flush=True)
    for url, path in rows:
        html = page_store.load(path)
        if not html:
            continue
        text = boilerplate_service.strip_for(cleaning_service.clean(html), url, config)
        text = llm_service.fit_to_context(
            llm, text, llm_service.get_context(category.analysis_model_id),
            category.analysis_max_tokens, overhead=prompt)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"Website content:\n{text}"}]
        n_prompt = len(llm.tokenize((prompt + text).encode("utf-8")))
        schema = {"type": "json_object", "schema": category.fields}
        a = timed_call(llm, messages, category.analysis_max_tokens, schema)
        b = timed_call(llm, messages, category.analysis_max_tokens, None)
        rate = lambda r: r[2] / r[1] if r[1] else 0
        print(f"{n_prompt:>10} {a[0]:>7.1f} {a[1]:>7.1f} {a[2]:>5} {rate(a):>6.1f} | "
              f"{b[0]:>9.1f} {b[1]:>7.1f} {b[2]:>5} {rate(b):>6.1f}  {url.split('//', 1)[-1][:40]}",
              flush=True)


if __name__ == "__main__":
    main()
