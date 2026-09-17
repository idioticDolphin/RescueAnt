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


# How long a realistic page may take, prompt included, before the model is
# judged to have fallen off the card. Calibrated on an 8 GB card with an
# 8k-token page: models that fit took 4.1-13.7 s, models whose cache had
# spilled took 64-74 s. Spilling is a cliff, not a slope, and 30 s sits well
# inside the gap. (A first cutoff of 60 s would have passed one spilled case
# by four seconds.)
PROBE_PROMPT_TOKENS = 8000
PROBE_MAX_SECONDS = 30

# A card counts as idle below this. The threshold has to sit between two
# numbers: a normal desktop - browser, launcher, chat app - already holds about
# 1.6 GiB, and a leaked model adds at least another 2.4 GiB (the smallest in
# the catalogue). 3000 MiB tolerates the first and catches the second.
# Override with --idle-mib on a machine with a heavier desktop.
IDLE_VRAM_MIB = 3000


def vram_used_mib():
    """Current VRAM use in MiB, or None if nvidia-smi cannot be queried."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


def wait_for_idle_card(timeout=120, idle_mib=None):
    """Block until the GPU is idle, or return False.

    Without this, a process left holding a model after a killed run makes
    every later measurement quietly wrong: three candidates once "failed to
    fit at 8k" when the card was in fact already 6 GB full of a leaked
    allocation. A model that is refused a clean card is skipped and reported
    as unmeasured, never scored.
    """
    deadline = time.monotonic() + timeout
    while True:
        used = vram_used_mib()
        if used is not None and used < (idle_mib or IDLE_VRAM_MIB):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(5)


def probe_context(model, candidates=(32768, 16384, 12288, 8192)):
    """Largest context this model can serve a realistic page at.

    Every cheap signal has been wrong at least once here. A successful load
    proves nothing, because llama.cpp spills to host memory rather than
    failing. A token coming out proves nothing either. And a *fast* token out
    of a short prompt proves nothing, which cost a second night's run: a
    short prompt barely touches the KV cache, so a model whose cache has
    spilled still answers quickly, then takes 24 minutes over a benchmark the
    default model finishes in four.

    So the probe sends a prompt about as long as a real page and times the
    whole call. The cache is exercised exactly as a crawl would exercise it.
    """
    for ctx in candidates:
        if PROBE_PROMPT_TOKENS >= ctx:
            continue
        code = (
            "import time;from llama_cpp import Llama;"
            f"m=Llama(model_path=r'{model}',n_ctx={ctx},n_gpu_layers=-1,verbose=False);"
            "unit='Die Station nimmt verletzte Wildtiere auf und pflegt sie. ';"
            "n=len(m.tokenize(unit.encode()));"
            f"text=unit*max(1,{PROBE_PROMPT_TOKENS}//n);"
            "t=time.monotonic();"
            "m.create_chat_completion(messages=[{'role':'user','content':text+' Fasse zusammen.'}],max_tokens=16);"
            "print(f'SECONDS {time.monotonic()-t:.1f}')"
        )
        try:
            out = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True,
                                 text=True, timeout=PROBE_MAX_SECONDS + 120).stdout
        except subprocess.TimeoutExpired:
            continue
        m = re.search(r"SECONDS ([\d.]+)", out or "")
        if m and float(m.group(1)) <= PROBE_MAX_SECONDS:
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
    ap.add_argument("--idle-mib", type=int, default=IDLE_VRAM_MIB,
                    help="VRAM in MiB below which the card counts as idle")
    ap.add_argument("--force-category", default="auto",
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
        if not wait_for_idle_card(idle_mib=args.idle_mib):
            print(f"=== {model.name} === card not idle ({vram_used_mib()} MiB in use) "
                  f"- skipped, NOT measured", flush=True)
            results.append({"model": model.name, "size": size, "context": None,
                            "cls": "busy", "cls_rate": None, "matched": None,
                            "fields": {}})
            continue
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
        cls = _grab(cls_out, r"\d+/\d+ correct\s+\((\d+%)\)")
        cls_rate = _grab(cls_out, r"= ([\d.]+)s per page")
        print(f"    classification: {cls or 'FAILED'}  ({cls_secs / 60:.1f} min)", flush=True)

        ext_out, ext_secs = run("extraction_benchmark.py", model, context,
                                ["--force-category", args.force_category])
        matched = _grab(ext_out, r"gold records matched: (\d+/\d+)")
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
    """First capture group of pattern in text, or None."""
    if not isinstance(text, str):
        return None
    m = re.search(pattern, text)
    return m.group(1) if m else None


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
