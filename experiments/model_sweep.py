"""
Score every downloaded model on both benchmarks and print one table.

Each model runs in its own subprocess so its VRAM is released before the next
one loads - on an 8 GB card two of these do not fit at once, and a sweep that
kept them in one process would measure thrashing rather than the models.

Classification and extraction are reported separately because they can
disagree. Classification is grammar-constrained to a category name, so a
model that drifts out of the page's language is not penalised for it there;
extraction reproduces text from the page, where that same tendency shows up
immediately. Extraction runs with --force-category so a taxonomy difference
cannot be mistaken for an extraction failure.

Usage:
    python experiments/model_sweep.py                    # every model in models/
    python experiments/model_sweep.py --context 12288    # for models that need it
    python experiments/model_sweep.py --only gemma llama
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
PY = sys.executable


def catalogue():
    """filename -> measured context from models.csv, where one is recorded."""
    by_file = {}
    path = ROOT / "models.csv"
    if not path.exists():
        return by_file
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) >= 5 and parts[4].strip():
            by_file[parts[2].strip()] = int(parts[4])
        elif len(parts) >= 3:
            by_file.setdefault(parts[2].strip(), None)
    return by_file


MIN_TOKENS_PER_SECOND = 4.0


def probe_context(model, candidates=(32768, 16384, 8192, 4096)):
    """Largest context this model can load and generate at a usable rate.

    Two thresholds matter, and only the second one is any good. Loading
    succeeds well past the point where the model is usable, because llama.cpp
    spills to host memory rather than failing. Requiring a token out is not
    enough either: a CPU-offloaded model still emits one, just slowly - a 9B
    that passed a token-only probe at 32k then took over 23 minutes on a
    benchmark the 4B finishes in four.

    So the probe measures the generation rate and rejects anything under
    MIN_TOKENS_PER_SECOND, which is the actual signature of a model that has
    fallen off the GPU.
    """
    for ctx in candidates:
        code = (
            "import time;from llama_cpp import Llama;"
            f"m=Llama(model_path=r'{model}',n_ctx={ctx},n_gpu_layers=-1,verbose=False);"
            "t=time.monotonic();"
            "r=m.create_chat_completion("
            "messages=[{'role':'user','content':'Count from one to twenty.'}],max_tokens=48);"
            "d=time.monotonic()-t;"
            "n=len(r['choices'][0]['message']['content'].split());"
            "print(f'RATE {n/d:.2f}' if d>0 else 'RATE 0')"
        )
        try:
            out = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True,
                                 text=True, timeout=240).stdout
        except subprocess.TimeoutExpired:
            continue
        m = re.search(r"RATE ([\d.]+)", out or "")
        if m and float(m.group(1)) >= MIN_TOKENS_PER_SECOND:
            return ctx
    return None


def run(script, model, context, extra=()):
    cmd = [PY, str(ROOT / "experiments" / script), "--model", str(model)]
    if context:
        cmd += ["--context", str(context)]
    cmd += list(extra)
    started = time.monotonic()
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             timeout=3600).stdout
    except subprocess.TimeoutExpired:
        return {"error": "timed out after 1h"}, time.monotonic() - started
    return out, time.monotonic() - started


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", type=int, default=None,
                    help="context size to load every model with")
    ap.add_argument("--only", nargs="*", default=None,
                    help="substrings; only models whose filename matches are run")
    ap.add_argument("--force-category", default="STATION",
                    help="category to extract as, for the extraction benchmark")
    args = ap.parse_args()

    models = sorted((ROOT / "models").glob("*.gguf"))
    if args.only:
        models = [m for m in models
                  if any(s.lower() in m.name.lower() for s in args.only)]
    if not models:
        print("no models found under models/")
        return

    known = catalogue()
    print(f"sweeping {len(models)} model(s)\n")
    results = []
    for model in models:
        size = model.stat().st_size / 1e9
        context = args.context or known.get(model.name)
        if context is None:
            context = probe_context(str(model))
            if context is None:
                print(f"=== {model.name}  ({size:.2f} GB) === cannot generate at "
                      f"any tried context - skipping", flush=True)
                results.append({"model": model.name, "size": size, "context": None,
                                "cls": None, "cls_rate": None, "matched": None,
                                "fields": {}})
                continue
            print(f"    (probed largest usable context: {context})", flush=True)
        print(f"=== {model.name}  ({size:.2f} GB, ctx {context}) ===", flush=True)

        cls_out, cls_secs = run("categorization_benchmark.py", model, context)
        cls = _grab(cls_out, r"(\d+)/(\d+) correct\s+\((\d+)%\)")
        cls_rate = _grab(cls_out, r"= ([\d.]+)s per page")
        print(f"    classification: {cls or 'FAILED'}  ({cls_secs / 60:.1f} min)", flush=True)

        ext_out, ext_secs = run("extraction_benchmark.py", model, context,
                                ["--force-category", args.force_category])
        matched = _grab(ext_out, r"gold records matched: (\d+)/(\d+)")
        fields = dict(re.findall(r"^\s{3}(\w+)\s+([\d.]+)", ext_out or "", re.M))
        print(f"    extraction:     matched {matched or '-'}  {fields}"
              f"  ({ext_secs / 60:.1f} min)", flush=True)

        results.append({
            "model": model.name, "size": size, "context": context,
            "cls": cls, "cls_rate": cls_rate,
            "matched": matched, "fields": fields,
        })

    _table(results)


def _grab(text, pattern):
    if not isinstance(text, str):
        return None
    m = re.search(pattern, text)
    return m.group(0).split(": ")[-1] if m else None


def _table(results):
    print("\n" + "=" * 78)
    print(f"{'model':<40}{'GB':>6}{'ctx':>7}{'class':>9}{'s/page':>8}{'matched':>8}")
    print("-" * 78)
    for r in results:
        print(f"{r['model'][:40]:<40}{r['size']:>6.2f}"
              f"{(str(r.get('context') or '-')):>7}"
              f"{(r['cls'] or '-'):>9}{(r['cls_rate'] or '-'):>8}"
              f"{(r['matched'] or '-'):>8}")
    print("=" * 78)
    print("\nper-field extraction score (1.0 = matches the hand label):")
    for r in results:
        if r["fields"]:
            pretty = "  ".join(f"{k}={v}" for k, v in r["fields"].items())
            print(f"   {r['model'][:40]:<40} {pretty}")


if __name__ == "__main__":
    main()
