"""
Entity resolution and record fusion.

The crawler necessarily observes the same real-world entity more than once:
on several pages of its own website, and again in any directory that lists
it. This module turns those repeated *observations* into deduplicated
*entities*.

It is deliberately domain-neutral. Nothing here knows what a field means -
behaviour is driven entirely by the roles declared in the configuration
(Config.field_semantics):

    identifier  near-unique; agreement is strong evidence of a match
    locator     places the entity; corroborating evidence, and disagreement
                is evidence *against* a match
    label       a human name; supporting evidence only, never decisive
    attribute   descriptive payload, used only when fusing

The "label is never decisive" rule is not a stylistic choice: generic names
are common in real data (six different raptor-care stations in the observed
corpus all called themselves "Greifvogelpflegestation"), so matching on name
alone would silently destroy distinct records.

Pipeline: blocking -> pairwise scoring -> union-find clustering -> fusion.
"""
import logging
import re
import unicodedata
from collections import defaultdict

logger = logging.getLogger(__name__)

# Score at or above which a pair is considered the same entity.
DEFAULT_ACCEPT = 1.0
# Penalty applied when two records give different values for a locator field.
DEFAULT_CONFLICT_PENALTY = 0.8
# Similarity above which two label values count as "the same name".
DEFAULT_LABEL_SIMILARITY = 0.9

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_POSTCODE = re.compile(r"\b\d{4,6}\b")


def normalize(value, normalizer="casefold"):
    """
    Apply a named normalizer to a single value.

    Normalizers are intentionally language-neutral: `casefold` uses Unicode
    case folding (not str.lower(), which mishandles ß and others), and
    `phone` reduces to digits so formatting conventions stop mattering.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""

    if normalizer == "phone":
        digits = re.sub(r"\D", "", text)
        # Drop an international prefix so +49 6131..., 0049 6131... and
        # 06131... compare equal on their significant tail.
        return digits.lstrip("0") if digits else ""
    if normalizer == "email":
        return unicodedata.normalize("NFKC", text).casefold()
    if normalizer == "digits":
        return re.sub(r"\D", "", text)
    if normalizer == "url":
        return unicodedata.normalize("NFKC", text).casefold().rstrip("/")
    # casefold, and the fallback for anything unrecognised
    return unicodedata.normalize("NFKC", text).casefold()


def _tokens(value):
    """Character 3-grams of a normalised string.

    Character n-grams rather than word tokens, so the comparison works for
    scripts that do not separate words with whitespace.
    """
    text = _NON_ALNUM.sub("", normalize(value))
    if not text:
        return set()
    if len(text) <= 3:
        return {text}
    return {text[i:i + 3] for i in range(len(text) - 2)}


def similarity(a, b):
    """Dice coefficient over character 3-grams; 0.0 when either side is empty."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb)
    return 2 * overlap / (len(ta) + len(tb))


class Resolver:
    """
    Resolves observation records into entities according to field semantics.

    :param field_semantics: {field_name: {"role": ..., "weight": ...,
                              "normalize": ..., "fusion": ...}}
    """

    def __init__(self, field_semantics=None, accept_at=DEFAULT_ACCEPT,
                 conflict_penalty=DEFAULT_CONFLICT_PENALTY):
        self.semantics = field_semantics or {}
        self.accept_at = accept_at
        self.conflict_penalty = conflict_penalty

    # -- role helpers ----------------------------------------------------
    def _fields_with_role(self, role):
        return [name for name, sem in self.semantics.items() if sem.get("role") == role]

    def _normalizer_for(self, field):
        return self.semantics.get(field, {}).get("normalize", "casefold")

    def _weight_for(self, field, default=0.0):
        return float(self.semantics.get(field, {}).get("weight", default))

    # -- blocking --------------------------------------------------------
    def block_keys(self, record):
        """
        Return cheap keys for a record; two records are only ever compared if
        they share one. Identifiers give precise keys, locators give a coarse
        recall-oriented one (a postcode), so that records with no shared
        identifier still get a chance to match.
        """
        keys = set()
        for field in self._fields_with_role("identifier"):
            value = normalize(record.get(field), self._normalizer_for(field))
            if value:
                # Phone numbers are compared on their significant tail, so
                # national-prefix differences don't split the block.
                keys.add((field, value[-9:] if len(value) > 9 else value))
        for field in self._fields_with_role("locator"):
            raw = record.get(field)
            if raw:
                for postcode in _POSTCODE.findall(str(raw)):
                    keys.add((field, postcode))
        return keys

    def candidate_pairs(self, records):
        """Return index pairs sharing at least one block key."""
        buckets = defaultdict(list)
        for index, record in enumerate(records):
            for key in self.block_keys(record):
                buckets[key].append(index)

        pairs = set()
        for indices in buckets.values():
            if len(indices) < 2:
                continue
            for i, left in enumerate(indices):
                for right in indices[i + 1:]:
                    pairs.add((min(left, right), max(left, right)))
        return sorted(pairs)

    # -- scoring ---------------------------------------------------------
    def score(self, a, b):
        """
        Score how strongly two records describe the same entity.

        Agreement on identifiers and locators adds their configured weight;
        a label only contributes its (small) weight and can never on its own
        reach the acceptance threshold. Disagreeing locators subtract, which
        is what keeps same-named but differently-located records apart.
        """
        total = 0.0
        for field, semantics in self.semantics.items():
            role = semantics.get("role")
            if role not in ("identifier", "locator", "label"):
                continue
            left, right = a.get(field), b.get(field)
            if not left or not right:
                continue

            if role == "label":
                if similarity(left, right) >= DEFAULT_LABEL_SIMILARITY:
                    total += self._weight_for(field, 0.3)
                continue

            normalizer = self._normalizer_for(field)
            nl, nr = normalize(left, normalizer), normalize(right, normalizer)
            if role == "identifier":
                if nl and nl == nr:
                    total += self._weight_for(field, 1.0)
                elif len(nl) > 6 and len(nr) > 6 and (nl.endswith(nr) or nr.endswith(nl)):
                    total += self._weight_for(field, 1.0)  # differing dial prefix
            else:  # locator
                if self._locators_agree(left, right):
                    total += self._weight_for(field, 0.7)
                else:
                    total -= self.conflict_penalty
        return total

    def _locators_agree(self, left, right):
        """Locators agree if their postcodes match, else if their text is close."""
        pl, pr = set(_POSTCODE.findall(str(left))), set(_POSTCODE.findall(str(right)))
        if pl and pr:
            return bool(pl & pr)
        return similarity(left, right) >= 0.6

    # -- clustering ------------------------------------------------------
    def cluster(self, records):
        """
        Group records into entities. Returns a list of lists of records.

        Union-find over accepted pairs, so a match chain (A=B, B=C) correctly
        yields one entity - but only strong pairwise evidence creates a link
        in the first place.
        """
        if not records:
            return []

        parent = list(range(len(records)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)

        for left, right in self.candidate_pairs(records):
            if self.score(records[left], records[right]) >= self.accept_at:
                union(left, right)

        groups = defaultdict(list)
        for index, record in enumerate(records):
            groups[find(index)].append(record)
        return [groups[key] for key in sorted(groups)]

    # -- fusion ----------------------------------------------------------
    def fuse(self, records):
        """
        Combine a cluster of records into one, per each field's configured
        fusion strategy, and report what was discarded.

        Records may carry a `_trust` value (higher = more authoritative, e.g.
        an entity's own site over a third-party directory); values from the
        most trusted sources win, and every value not chosen is recorded in
        `_conflicts` rather than being silently dropped.
        """
        if not records:
            return {}
        ordered = sorted(records, key=lambda r: -float(r.get("_trust", 0.5)))
        top_trust = float(ordered[0].get("_trust", 0.5))

        fields = {key for record in records for key in record
                  if not key.startswith("_")}
        fused, conflicts = {}, []

        for field in sorted(fields):
            strategy = self.semantics.get(field, {}).get("fusion", "trust_then_mode")
            values = [(r, r.get(field)) for r in ordered if r.get(field) not in (None, "", [])]
            if not values and strategy != "or_with_evidence":
                continue

            if strategy == "union":
                fused[field] = self._union_values(values)
            elif strategy == "or_with_evidence":
                fused[field] = any(bool(v) for _r, v in values)
            elif strategy == "longest_from_top_trust":
                top = [v for r, v in values if float(r.get("_trust", 0.5)) == top_trust]
                fused[field] = max(top, key=lambda v: len(str(v))) if top else values[0][1]
            elif strategy == "trust_then_valid":
                fused[field] = values[0][1]
            else:  # trust_then_mode
                fused[field] = self._trust_then_mode(values, top_trust)

            for record, value in values:
                if _differs(value, fused.get(field)):
                    conflicts.append({"field": field, "value": value,
                                      "trust": float(record.get("_trust", 0.5))})

        fused["_n_sources"] = len(records)
        fused["_conflicts"] = conflicts
        fused["_confidence"] = self._confidence(records, conflicts, top_trust)
        return fused

    @staticmethod
    def _union_values(values):
        """Flatten list- and scalar-valued fields into one de-duplicated list."""
        collected, seen = [], set()
        for _record, value in values:
            items = value if isinstance(value, list) else [value]
            for item in items:
                key = normalize(item)
                if key and key not in seen:
                    seen.add(key)
                    collected.append(item)
        return collected

    @staticmethod
    def _trust_then_mode(values, top_trust):
        """Most frequent value among the highest-trust sources."""
        top = [v for r, v in values if float(r.get("_trust", 0.5)) == top_trust]
        pool = top or [v for _r, v in values]
        counts = defaultdict(int)
        for value in pool:
            counts[normalize(value)] += 1
        best = max(counts.items(), key=lambda kv: kv[1])[0]
        for value in pool:
            if normalize(value) == best:
                return value
        return pool[0]

    @staticmethod
    def _confidence(records, conflicts, top_trust):
        """Crude but honest: more corroboration raises it, disagreement lowers it."""
        corroboration = min(1.0, len(records) / 3)
        disagreement = min(1.0, len(conflicts) / max(1, len(records) * 2))
        return round(max(0.0, min(1.0, 0.5 * corroboration + 0.5 * top_trust - 0.3 * disagreement)), 3)


def _differs(value, chosen):
    """True if `value` is not represented in the chosen fused value."""
    if chosen is None:
        return True
    if isinstance(chosen, list):
        items = value if isinstance(value, list) else [value]
        return any(normalize(i) not in {normalize(c) for c in chosen} for i in items)
    if isinstance(chosen, bool):
        return False
    return normalize(value) != normalize(chosen)
