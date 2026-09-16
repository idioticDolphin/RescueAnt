"""
Tests for domain-neutral entity resolution (P4) and record fusion (§5).

Every rule under test is driven by config-declared field *roles*, never by
field names, so the same code deduplicates any crawl target.
"""
import pytest

from model.analyzer import entity_service

SEMANTICS = {
    "name":      {"role": "label", "weight": 0.3},
    "e-mail":    {"role": "identifier", "weight": 1.0, "normalize": "email", "fusion": "union"},
    "telephone": {"role": "identifier", "weight": 0.9, "normalize": "phone", "fusion": "union"},
    "address":   {"role": "locator", "weight": 0.7, "fusion": "trust_then_valid"},
    "tags":      {"role": "attribute", "fusion": "union"},
    "pickup":    {"role": "attribute", "fusion": "or_with_evidence"},
    "blurb":     {"role": "attribute", "fusion": "longest_from_top_trust"},
}


@pytest.fixture
def resolver():
    return entity_service.Resolver(field_semantics=SEMANTICS)


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def test_phone_normalizes_to_digits():
    assert entity_service.normalize("06131/ 477638", "phone") == "6131477638"


def test_phone_normalization_ignores_formatting_differences():
    a = entity_service.normalize("+49 (0)6131 477638", "phone")
    b = entity_service.normalize("0049-6131-477638", "phone")
    assert a[-9:] == b[-9:]


def test_email_normalization_is_casefolded_and_trimmed():
    assert entity_service.normalize("  Info@Example.DE ", "email") == "info@example.de"


def test_casefold_handles_non_ascii():
    # .lower() would leave "ß" alone; casefold maps it to "ss"
    assert entity_service.normalize("STRASSE", "casefold") == \
           entity_service.normalize("straße", "casefold")


def test_unknown_normalizer_falls_back_to_casefold():
    assert entity_service.normalize(" Abc ", "no-such-normalizer") == "abc"


def test_normalize_handles_none():
    assert entity_service.normalize(None, "phone") is None


# ---------------------------------------------------------------------------
# blocking
# ---------------------------------------------------------------------------

def test_block_keys_come_from_identifier_roles(resolver):
    keys = resolver.block_keys({"e-mail": "a@b.de", "telephone": "0611 1234567",
                                "name": "X", "blurb": "irrelevant"})
    kinds = {k[0] for k in keys}
    assert "e-mail" in kinds and "telephone" in kinds
    assert "blurb" not in kinds


def test_block_keys_include_locator_for_recall(resolver):
    keys = resolver.block_keys({"address": "Kirchstraße 1, 76879 Bornheim"})
    assert any(k[0] == "address" for k in keys)


def test_records_sharing_no_key_are_never_compared(resolver):
    a = {"name": "A", "e-mail": "a@x.de"}
    b = {"name": "B", "e-mail": "b@y.de"}
    assert resolver.candidate_pairs([a, b]) == []


def test_records_sharing_a_key_become_a_candidate_pair(resolver):
    a = {"name": "A", "e-mail": "shared@x.de"}
    b = {"name": "B", "e-mail": "SHARED@x.de"}
    assert len(resolver.candidate_pairs([a, b])) == 1


# ---------------------------------------------------------------------------
# pair scoring
# ---------------------------------------------------------------------------

def test_identical_identifier_is_decisive(resolver):
    a = {"name": "Station A", "e-mail": "same@x.de"}
    b = {"name": "Totally Different", "e-mail": "same@x.de"}
    assert resolver.score(a, b) >= 1.0


def test_matching_name_alone_is_not_decisive(resolver):
    """F4: generic names are common; a shared name must never merge records."""
    a = {"name": "Greifvogelpflegestation", "address": "Kreisstraße 30"}
    b = {"name": "Greifvogelpflegestation", "address": "Michelsgrund 6"}
    assert resolver.score(a, b) < 1.0


def test_conflicting_locators_are_penalised(resolver):
    a = {"name": "Same Name", "address": "Kreisstraße 30, 61118 Bad Vilbel"}
    b = {"name": "Same Name", "address": "Michelsgrund 6, 64689 Grasellenbach"}
    same_name_only = resolver.score({"name": "Same Name"}, {"name": "Same Name"})
    assert resolver.score(a, b) < same_name_only


def test_matching_phone_and_name_is_decisive(resolver):
    a = {"name": "Station A", "telephone": "06131/477638"}
    b = {"name": "Station A", "telephone": "+49 6131 477638"}
    assert resolver.score(a, b) >= 1.0


def test_missing_fields_do_not_count_as_agreement(resolver):
    assert resolver.score({"name": "A"}, {"name": "A", "e-mail": ""}) < 1.0


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------

def test_clustering_merges_duplicate_records(resolver):
    records = [
        {"name": "MARS Wildlife Rescue", "e-mail": "info@mars.ca"},
        {"name": "MARS Wildlife Rescue", "e-mail": "INFO@mars.ca"},
        {"name": "MARS Wildlife Rescue", "e-mail": "info@mars.ca"},
    ]
    assert len(resolver.cluster(records)) == 1


def test_clustering_keeps_distinct_records_apart(resolver):
    """The six generically-named stations from one listing page must survive."""
    records = [
        {"name": "Greifvogelpflegestation", "address": "Kreisstraße 30", "telephone": "01512-7705235"},
        {"name": "Greifvogelpflegestation", "address": "Michelsgrund 6", "telephone": "06207-5687"},
        {"name": "Greifvogelpflegestation", "address": "Heide 47", "telephone": "06206-912030"},
    ]
    assert len(resolver.cluster(records)) == 3


def test_clustering_is_transitive_through_strong_links(resolver):
    """A=B and B=C should yield one entity, even though A and C share nothing."""
    records = [
        {"name": "A", "e-mail": "first@y.de"},
        {"name": "A", "e-mail": "first@y.de", "telephone": "0611 123456"},
        {"name": "C", "e-mail": "first@y.de"},
    ]
    assert len(resolver.cluster(records)) == 1


def test_shared_phone_alone_does_not_merge_differently_named_records(resolver):
    """Deliberate: several independent entities can publish one shared
    hotline number, so a phone match needs corroboration before merging."""
    records = [
        {"name": "Station One", "telephone": "0611 123456"},
        {"name": "Station Two", "telephone": "0611 123456"},
    ]
    assert len(resolver.cluster(records)) == 2


def test_shared_phone_plus_matching_name_does_merge(resolver):
    records = [
        {"name": "Wildtierhilfe Sauerland", "telephone": "0611 123456"},
        {"name": "Wildtierhilfe Sauerland", "telephone": "+49 611 123456"},
    ]
    assert len(resolver.cluster(records)) == 1


def test_empty_input_clusters_to_nothing(resolver):
    assert resolver.cluster([]) == []


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------

def test_union_fusion_collects_distinct_values(resolver):
    fused = resolver.fuse([
        {"tags": ["birds"], "name": "A"},
        {"tags": ["mammals", "birds"], "name": "A"},
    ])
    assert sorted(fused["tags"]) == ["birds", "mammals"]


def test_union_fusion_of_scalar_identifiers(resolver):
    fused = resolver.fuse([
        {"name": "A", "e-mail": "one@x.de"},
        {"name": "A", "e-mail": "two@x.de"},
    ])
    assert "one@x.de" in fused["e-mail"] and "two@x.de" in fused["e-mail"]


def test_trust_ordering_prefers_higher_trust_source(resolver):
    fused = resolver.fuse([
        {"name": "Directory Version", "address": "old", "_trust": 0.5},
        {"name": "Own Site Version", "address": "new", "_trust": 1.0},
    ])
    assert fused["name"] == "Own Site Version"
    assert fused["address"] == "new"


def test_or_with_evidence_is_true_if_any_source_asserts_it(resolver):
    fused = resolver.fuse([{"name": "A", "pickup": False},
                           {"name": "A", "pickup": True}])
    assert fused["pickup"] is True


def test_or_with_evidence_stays_false_without_assertion(resolver):
    fused = resolver.fuse([{"name": "A", "pickup": False},
                           {"name": "A", "pickup": False}])
    assert fused["pickup"] is False


def test_longest_from_top_trust_picks_most_specific(resolver):
    fused = resolver.fuse([
        {"name": "A", "blurb": "short", "_trust": 1.0},
        {"name": "A", "blurb": "a much longer and more specific description", "_trust": 1.0},
    ])
    assert fused["blurb"].startswith("a much longer")


def test_fusion_records_conflicts(resolver):
    fused = resolver.fuse([
        {"name": "Version One", "_trust": 1.0},
        {"name": "Version Two", "_trust": 0.5},
    ])
    assert any(c["field"] == "name" and c["value"] == "Version Two"
               for c in fused["_conflicts"])


def test_fusion_reports_source_count(resolver):
    fused = resolver.fuse([{"name": "A"}, {"name": "A"}, {"name": "A"}])
    assert fused["_n_sources"] == 3


def test_fusion_of_single_record_is_that_record(resolver):
    fused = resolver.fuse([{"name": "Solo", "e-mail": "s@x.de"}])
    assert fused["name"] == "Solo"
    assert fused["_n_sources"] == 1


# ---------------------------------------------------------------------------
# value plausibility
#
# Observed in live output: '08.06.26' extracted as a telephone (twice, across
# two runs - the model reads a date near the phone block), an emergency
# telephone holding a URL, and 'reginaweber@377@yahoo.de' as an address.
# Fields already declare what they are via "normalize"; that declaration is
# enough to reject a value that cannot be what the field claims, without any
# field names or domain knowledge in the code.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "06131/ 477638", "+49 6334 98 47 377", "0170 911 543 50", "06004-2749",
])
def test_real_phone_numbers_are_plausible(value):
    assert entity_service.is_plausible(value, "phone") is True


@pytest.mark.parametrize("value", [
    "08.06.26",           # a date, not a phone - seen in two separate runs
    "1.2.2026",
    "2026-06-08",
    "12345",              # too few digits to be a dialable number
    "https://example.org/notfall",   # a URL in a phone field
    "Mo-Fr",
])
def test_implausible_phone_values_are_rejected(value):
    assert entity_service.is_plausible(value, "phone") is False


@pytest.mark.parametrize("value", [
    "info@example.org", "Schierstein@t-online.de", "a.b-c@sub.example.co.uk",
])
def test_real_addresses_are_plausible(value):
    assert entity_service.is_plausible(value, "email") is True


@pytest.mark.parametrize("value", [
    "reginaweber@377@yahoo.de",   # two @ - seen in live output
    "not an email", "info@example", "@example.org", "info@.org",
])
def test_implausible_email_values_are_rejected(value):
    assert entity_service.is_plausible(value, "email") is False


def test_values_without_a_declared_normalizer_are_always_plausible():
    """Free-text fields (descriptions, opening hours) have no shape to check;
    the gate must not invent one."""
    assert entity_service.is_plausible("anything at all", None) is True
    assert entity_service.is_plausible("anything at all", "casefold") is True


def test_empty_values_are_not_rejected():
    """An absent value is the gate's business, not this check's."""
    assert entity_service.is_plausible("", "phone") is True
    assert entity_service.is_plausible(None, "phone") is True


def test_url_plausibility():
    assert entity_service.is_plausible("https://example.org", "url") is True
    assert entity_service.is_plausible("example.org/path", "url") is True
    assert entity_service.is_plausible("not a url at all", "url") is False
