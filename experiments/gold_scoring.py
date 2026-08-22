"""
Shared gold-label reading, matching, and field-scoring logic for
experiments/collect_extraction_correctness.py and
experiments/compare_gold_to_crawl_db.py - factored out so both "run the
isolated pipeline against gold URLs" and "look up what a real crawl already
extracted for those same URLs" score extracted entries against gold the
same way, and stay comparable to each other.

See collect_extraction_correctness.py's module docstring for the scoring
methodology notes (name/address similarity thresholds, phone digit
comparison, accepted_animals Jaccard, animal_pickup exact match) - they
apply here unchanged.
"""
import re
from collections import defaultdict
from difflib import SequenceMatcher

FIELDS = ["name", "address", "telephone", "accepted_animals", "animal_pickup"]
NAME_MATCH_THRESHOLD = 0.3  # below this, treat as "no corresponding extracted entry"

ROW_FIELDNAMES = [
    "source_url", "category",
    "gold_name", "extracted_name", "name_similarity", "name_close",
    "gold_address", "extracted_address", "address_similarity", "address_close",
    "gold_telephone", "extracted_telephone", "telephone_match",
    "gold_accepted_animals", "extracted_accepted_animals", "accepted_animals_jaccard",
    "gold_animal_pickup", "extracted_animal_pickup", "animal_pickup_match",
]


def read_gold_labels(path):
    """Return {source_url: [gold_entry_dict, ...]} from a gold-labels CSV (see extraction_gold_labels.csv)."""
    import csv
    by_url = defaultdict(list)
    with open(path) as f:
        for row in csv.DictReader(f):
            url = row["source_url"].strip()
            if not url:
                continue
            by_url[url].append({field: (row.get(field) or "").strip() for field in FIELDS})
    return by_url


def _similarity(a, b):
    return SequenceMatcher(None, (a or "").strip().lower(), (b or "").strip().lower()).ratio()


def _digits_only(s):
    return re.sub(r"\D", "", s or "")


def _animal_set(value):
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").split("|")
    return {item.strip().lower() for item in items if item.strip()}


def _jaccard(a, b):
    if not a and not b:
        return None  # both empty - not comparable, not a disagreement
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def match_gold_to_extracted(gold_entries, extracted_entries):
    """Greedy, best-score-first name matching. Returns a list of
    (gold_entry, extracted_entry_or_None, name_similarity) triples."""
    pairs = []
    for gi, gold in enumerate(gold_entries):
        for ei, extracted in enumerate(extracted_entries):
            score = _similarity(gold["name"], extracted.get("name", ""))
            pairs.append((score, gi, ei))
    pairs.sort(reverse=True)

    matched_gold = {}
    used_extracted = set()
    for score, gi, ei in pairs:
        if gi in matched_gold or ei in used_extracted:
            continue
        if score < NAME_MATCH_THRESHOLD:
            continue
        matched_gold[gi] = (ei, score)
        used_extracted.add(ei)

    results = []
    for gi, gold in enumerate(gold_entries):
        if gi in matched_gold:
            ei, score = matched_gold[gi]
            results.append((gold, extracted_entries[ei], score))
        else:
            results.append((gold, None, 0.0))
    return results


def score_entry(source_url, category, gold, extracted, name_score):
    """
    Build one ROW_FIELDNAMES-shaped dict comparing a single gold entry
    against its matched extracted entry (or None, if match_gold_to_extracted()
    found nothing above NAME_MATCH_THRESHOLD).
    """
    row = {
        "source_url": source_url, "category": category,
        "gold_name": gold["name"], "extracted_name": "", "name_similarity": round(name_score, 3),
        "name_close": name_score >= 0.8,
        "gold_address": gold["address"], "extracted_address": "",
        "address_similarity": "", "address_close": "",
        "gold_telephone": gold["telephone"], "extracted_telephone": "", "telephone_match": "",
        "gold_accepted_animals": gold["accepted_animals"], "extracted_accepted_animals": "",
        "accepted_animals_jaccard": "",
        "gold_animal_pickup": gold["animal_pickup"], "extracted_animal_pickup": "",
        "animal_pickup_match": "",
    }
    if extracted is None:
        return row

    row["extracted_name"] = extracted.get("name", "")
    row["extracted_address"] = extracted.get("address", "")
    row["extracted_telephone"] = extracted.get("telephone", "")
    row["extracted_accepted_animals"] = "|".join(extracted.get("accepted_animals", []) or [])
    row["extracted_animal_pickup"] = extracted.get("animal_pickup", "")

    # Only score name/address similarity where the gold labeler actually
    # recorded a value - an empty gold field isn't a "wrong" extraction,
    # it's just unlabeled, and SequenceMatcher against "" always scores 0.0.
    if gold["name"]:
        row["name_similarity"] = round(name_score, 3)
        row["name_close"] = name_score >= 0.8
    if gold["address"]:
        addr_sim = _similarity(gold["address"], extracted.get("address", ""))
        row["address_similarity"] = round(addr_sim, 3)
        row["address_close"] = addr_sim >= 0.8

    gold_phone, ext_phone = _digits_only(gold["telephone"]), _digits_only(extracted.get("telephone", ""))
    if gold_phone and ext_phone:
        # compare on the shorter length to tolerate country-code prefixes
        n = min(len(gold_phone), len(ext_phone))
        row["telephone_match"] = gold_phone[-n:] == ext_phone[-n:]

    jaccard = _jaccard(_animal_set(gold["accepted_animals"]), _animal_set(extracted.get("accepted_animals", [])))
    row["accepted_animals_jaccard"] = "" if jaccard is None else round(jaccard, 3)

    gold_pickup = gold["animal_pickup"]
    if gold_pickup:
        ext_pickup = extracted.get("animal_pickup", None)
        row["animal_pickup_match"] = (str(ext_pickup).strip().lower() == gold_pickup.strip().lower())

    return row
