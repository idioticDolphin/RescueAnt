"""
Data collection for notebooks/grammar_strictness.ipynb.

Controlled, paired comparison: for the same set of fixed sample texts, run
extraction twice per sample - once with the `accepted_animals` field
constrained to the project's real `animal_type` enum (bot.config's `define
animal_type = mammal|bat|rodent|...`, i.e. what the real pipeline uses for
STATION/LIST categories), once with it as a free-form `list[string]` - and
measure wall-clock time and token counts for each.

Everything else (prompt text, other fields, model, temperature, sample
content) is held constant across both conditions, so any timing/token
difference is attributable to the grammar constraint itself. This mirrors
model.analyzer.extraction_service.extract_information()'s prompt/response
format construction directly (rather than importing it) so token usage
(not exposed by extract_information's return value) can be captured too.

Sample texts are short, fixed, synthetic-but-realistic German animal-rescue
station descriptions (not fetched from the web), so this experiment is
self-contained and reproducible without network access or depending on
live pages remaining unchanged.

Usage: python experiments/collect_grammar_comparison.py [repeats]
"""
import sys

from common import DATA_DIR, configure_logging, timed, write_csv

OUTPUT_PATH = DATA_DIR / "grammar_comparison.csv"
DEFAULT_REPEATS = 2

# Matches bot.config's `define animal_type = ...`
ANIMAL_TYPES = [
    "mammal", "bat", "rodent", "carnivore", "ungulate", "hedgehog", "lagomorph",
    "marine_mammal", "primate", "marsupial", "bird", "bird_of_prey", "owl",
    "songbird", "waterbird", "seabird", "reptile", "turtle", "snake", "lizard",
    "crocodilian", "amphibian", "frog", "salamander", "fish", "invertebrate",
]

ANALYSIS_PROMPT = (
    "Extract the following fields from this animal rescue station website as JSON. "
    "Leave out fields you cannot fill with the provided information."
)

# Fixed, synthetic-but-realistic sample texts. Each names concrete accepted
# animal categories in German, worded differently, so the model has to map
# free text onto animal_type itself rather than finding the enum value verbatim.
SAMPLES = {
    "hedgehog_station": (
        "Igelstation Musterstadt\n"
        "Wir kuemmern uns um verletzte und geschwaechte Igel aus der Region. "
        "Bringen Sie uns bitte auch verletzte Voegel oder junge Feldhasen, die Sie finden - "
        "wir vermitteln sie an die zustaendigen Fachstellen weiter."
    ),
    "wildlife_multi": (
        "Wildtierauffangstation Nordwald\n"
        "Unsere Station nimmt Fuechse, Rehkitze, Eichhoernchen und verletzte Greifvoegel wie "
        "Eulen und Falken auf. Auch Fledermaeuse werden bei uns gepflegt, bis sie wieder "
        "ausgewildert werden koennen."
    ),
    "reptile_center": (
        "Reptilienauffangstation Suedstadt\n"
        "Wir sind spezialisiert auf ausgesetzte oder beschlagnahmte Schlangen, Echsen und "
        "Schildkroeten. Krokodile nehmen wir aus Sicherheitsgruenden nicht auf, vermitteln "
        "aber an eine Partnerstation weiter."
    ),
    "bird_rescue": (
        "Wildvogelhilfe am See\n"
        "Verletzte Wasservoegel wie Enten und Schwaene, aber auch Singvoegel und Seevoegel "
        "finden bei uns ein vorlaeufiges Zuhause, bis sie wieder freigelassen werden koennen."
    ),
    "shelter_generic": (
        "Tierheim Talblick\n"
        "Wir vermitteln herrenlose Hunde und Katzen, betreuen aber auch Kleinsaeuger wie "
        "Kaninchen und Meerschweinchen sowie gelegentlich Papageien und andere Voegel."
    ),
    "amphibian_pond": (
        "Amphibienstation Teichrand\n"
        "Unser Schwerpunkt liegt auf einheimischen Froeschen, Kroeten und Molchen. Fische aus "
        "dem angrenzenden Teich werden ebenfalls versorgt, wenn sie verletzt aufgefunden werden."
    ),
}


def _schema(strict: bool) -> dict:
    """Same shape config_service._build_schema() produces; only the
    accepted_animals field's grammar differs between conditions."""
    animal_field = (
        {"type": "array", "items": {"type": "string", "enum": ANIMAL_TYPES}}
        if strict
        else {"type": "array", "items": {"type": "string"}}
    )
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "accepted_animals": animal_field,
        },
        "required": ["name", "accepted_animals"],
    }


def _run_once(llm, sample_text: str, schema: dict):
    prompt = f"{ANALYSIS_PROMPT} The return schema is {schema}"
    with timed() as t:
        result = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Website content:\n{sample_text}"},
            ],
            response_format={"type": "json_object", "schema": schema},
            temperature=0,
        )
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    return t.seconds, content, usage


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.collect_grammar_comparison")

    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPEATS

    import model.tools.config_service as config_service
    config = config_service.get_config()
    import model.tools.llm_service as llm_service
    # Reuse the real STATION category's model/context so this uses the exact
    # model configuration the production pipeline extracts with.
    station = config.get_category("STATION")
    llm = llm_service.get_model(station.analysis_model_id)

    rows = []
    fieldnames = [
        "sample", "condition", "repeat", "seconds",
        "prompt_tokens", "completion_tokens", "total_tokens", "extracted_json",
    ]
    total_calls = len(SAMPLES) * 2 * repeats
    call_index = 0
    for sample_name, sample_text in SAMPLES.items():
        for condition, strict in [("strict_enum", True), ("lax_string", False)]:
            schema = _schema(strict)
            for repeat in range(repeats):
                call_index += 1
                logger.info("[%d/%d] sample=%s condition=%s repeat=%d",
                            call_index, total_calls, sample_name, condition, repeat)
                seconds, content, usage = _run_once(llm, sample_text, schema)
                logger.info("  -> %.1fs, %s tokens out", seconds, usage.get("completion_tokens"))
                rows.append({
                    "sample": sample_name,
                    "condition": condition,
                    "repeat": repeat,
                    "seconds": round(seconds, 3),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "extracted_json": content,
                })
                write_csv(OUTPUT_PATH, rows, fieldnames)

    logger.info("Wrote %d rows to %s", len(rows), OUTPUT_PATH)


if __name__ == "__main__":
    main()
