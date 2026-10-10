"""AI Understanding / Normalization (`documents/normalization.py`) — pure
functions, no app, no DB. This stage runs AFTER structuring
(`extractors.parse_prescription_entities` decides which lines belong to
which medicine) and never re-decides that; it only cleans a medicine name
of stray leading OCR noise and scores each field's certainty."""

from __future__ import annotations

import pytest
from app.documents import normalization as norm


def test_a_leading_noise_symbol_is_stripped_from_the_medicine_name():
    entities = [{"medicine": "& Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Metformin"
    assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


def test_a_symbol_that_is_part_of_a_real_combination_drug_name_is_never_touched():
    """ "&" INSIDE a name (not leading) must survive untouched — a real
    combination-drug name, not noise. Proves the cleanup is contextual, not
    a blind global strip."""
    entities = [
        {"medicine": "Tylenol & Codeine", "dosage": "500 mg", "frequency": "", "duration": ""}
    ]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Tylenol & Codeine"
    assert certainty[0]["medicine"] == norm.CERTAINTY_HIGH


def test_every_recognised_noise_symbol_is_stripped_when_leading_and_isolated():
    for symbol in ("&", "|", "#", "@", "~"):
        entities = [
            {"medicine": f"{symbol} Amoxicillin", "dosage": "", "frequency": "", "duration": ""}
        ]
        rows, _ = norm.normalize_prescription_rows(entities)
        assert rows[0]["medicine"] == "Amoxicillin", symbol


def test_a_clean_name_with_no_noise_is_marked_high_certainty_unchanged():
    entities = [{"medicine": "Losartan", "dosage": "50 mg", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Losartan"
    assert certainty[0]["medicine"] == norm.CERTAINTY_HIGH


def test_a_name_that_still_opens_with_noise_after_cleanup_is_ambiguous_not_silently_kept():
    """A leading symbol NOT in the recognised, isolated-marker set (here,
    fused directly onto the name with no separating space) is left alone by
    the cleanup — and flagged as ambiguous rather than silently trusted."""
    entities = [{"medicine": "*Furosemide", "dosage": "", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "*Furosemide"  # never invented or guessed at
    assert certainty[0]["medicine"] == norm.CERTAINTY_AMBIGUOUS


def test_an_empty_medicine_name_is_missing_not_ambiguous():
    entities = [{"medicine": "", "dosage": "500 mg", "frequency": "", "duration": ""}]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0]["medicine"] == norm.CERTAINTY_MISSING


def test_missing_fields_are_flagged_missing_and_present_fields_high():
    entities = [{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0] == {
        "medicine": norm.CERTAINTY_HIGH,
        "dosage": norm.CERTAINTY_HIGH,
        "frequency": norm.CERTAINTY_MISSING,
        "duration": norm.CERTAINTY_MISSING,
    }


def test_rows_and_certainty_are_parallel_lists_of_the_same_length():
    entities = [
        {"medicine": "Metformin", "dosage": "500 mg", "frequency": "daily", "duration": "30 days"},
        {"medicine": "Atorvastatin", "dosage": "10 mg", "frequency": "", "duration": ""},
    ]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert len(rows) == len(certainty) == 2


def test_aggregate_confidence_ignores_missing_fields_never_punishing_genuinely_absent_data():
    """A medicine with an honestly-empty duration (e.g. a PRN medicine) must
    not drag the document's overall score down — only fields that were
    actually found and scored contribute."""
    entities = [{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}]
    _, certainty = norm.normalize_prescription_rows(entities)
    # medicine=high(1.0), dosage=high(1.0); frequency/duration=missing (excluded)
    assert norm.aggregate_confidence(certainty) == 1.0


def test_aggregate_confidence_reflects_a_mix_of_certainty_levels():
    entities = [
        {"medicine": "& Metformin", "dosage": "500 mg", "frequency": "daily", "duration": "30 days"}
    ]
    _, certainty = norm.normalize_prescription_rows(entities)
    # medicine=uncertain(0.6), the rest high(1.0) -> mean = (0.6+1+1+1)/4
    assert norm.aggregate_confidence(certainty) == round((0.6 + 1 + 1 + 1) / 4, 3)


def test_aggregate_confidence_is_none_for_no_rows_at_all():
    assert norm.aggregate_confidence([]) is None


def test_an_embedded_junk_character_downgrades_any_field_not_just_medicine():
    """A stray '(', '_' etc. mid-value is a corrupted-marker artifact
    wherever it lands — not only ever in front of a medicine name. Must
    never be reported as HIGH confidence just because the field is
    non-empty (the actual bug: a wrong-but-non-empty reading looked exactly
    as trustworthy as a correct one)."""
    entities = [
        {"medicine": "Metformin", "dosage": "50(0 mg", "frequency": "da_ly", "duration": "30 da#ys"}
    ]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0]["dosage"] == norm.CERTAINTY_AMBIGUOUS
    assert certainty[0]["frequency"] == norm.CERTAINTY_AMBIGUOUS
    assert certainty[0]["duration"] == norm.CERTAINTY_AMBIGUOUS


def test_a_clean_non_empty_field_still_earns_high_confidence():
    """The honesty check must not become over-strict: a genuinely clean
    reading is still HIGH, not downgraded for no reason."""
    entities = [
        {
            "medicine": "Amoxicillin",
            "dosage": "500 mg",
            "frequency": "three times daily",
            "duration": "5 days",
        }
    ]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0] == {
        "medicine": norm.CERTAINTY_HIGH,
        "dosage": norm.CERTAINTY_HIGH,
        "frequency": norm.CERTAINTY_HIGH,
        "duration": norm.CERTAINTY_HIGH,
    }


def test_an_empty_dose_with_a_garbled_number_in_its_source_is_flagged_not_hidden():
    """The real-failure case: "D3,000. IN." (garbled beyond any pattern
    here recovering "1000 IU") must be told apart from a dose that was
    simply never written — flagged AMBIGUOUS, so the patient is prompted to
    read it off the original, never silently reported as if nothing was
    there at all."""
    entities = [{"medicine": "Vitamin D3", "dosage": "", "frequency": "", "duration": ""}]
    sources = ["Vitamin D3,000. IN. Take once daily"]
    _, certainty = norm.normalize_prescription_rows(entities, sources)
    assert certainty[0]["dosage"] == norm.CERTAINTY_AMBIGUOUS


def test_an_empty_dose_with_nothing_number_shaped_in_its_source_stays_missing():
    """The other half of the same distinction: when the source genuinely
    has nothing resembling a garbled strength, "missing" is still the
    honest answer — this must not become a blanket downgrade."""
    entities = [{"medicine": "Paracetamol", "dosage": "", "frequency": "", "duration": ""}]
    sources = ["Paracetamol Take 1 tablet if needed"]
    _, certainty = norm.normalize_prescription_rows(entities, sources)
    assert certainty[0]["dosage"] == norm.CERTAINTY_MISSING


def test_an_empty_dose_with_no_source_at_all_stays_missing_not_a_crash():
    """`sources=None` is the doctor-generated path's own contract (no OCR
    block ever exists for already-typed input) — must degrade gracefully,
    never raise, and never claim a garbled source it doesn't have."""
    entities = [{"medicine": "Metformin", "dosage": "", "frequency": "", "duration": ""}]
    _, certainty = norm.normalize_prescription_rows(entities)  # no sources arg at all
    assert certainty[0]["dosage"] == norm.CERTAINTY_MISSING


def test_the_garbled_number_check_is_scoped_to_dosage_only_for_now():
    """Frequency/duration being empty must not be reinterpreted as
    "ambiguous" just because the source happens to contain a garbled
    number elsewhere (e.g. the garbled dose itself) — the check is
    deliberately narrow, not a blanket "any digit noise nearby" rule."""
    entities = [{"medicine": "Vitamin D3", "dosage": "", "frequency": "", "duration": ""}]
    sources = ["Vitamin D3,000. IN."]
    _, certainty = norm.normalize_prescription_rows(entities, sources)
    assert certainty[0]["frequency"] == norm.CERTAINTY_MISSING
    assert certainty[0]["duration"] == norm.CERTAINTY_MISSING


def test_the_normalization_generalises_to_a_medicine_name_never_seen_elsewhere():
    """A name that appears nowhere else in this test file, to prove the
    cleanup logic is generic (position/shape-based), not keyed to any
    specific drug."""
    entities = [{"medicine": "| Rosuvastatin", "dosage": "20 mg", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Rosuvastatin"
    assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


# ═══ List-marker stripping: the "(" shape is safe, the bare letter+period
# shape is NOT (it collides with real product names) ═══════════════════════
def test_a_corrupted_paren_marker_with_a_single_letter_is_stripped():
    entities = [{"medicine": "( D Paracetamol", "dosage": "", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Paracetamol"
    assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


def test_a_corrupted_paren_marker_fused_with_an_underscore_is_stripped():
    entities = [{"medicine": "( A_Cetirizine", "dosage": "", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Cetirizine"
    assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


def test_b_complex_text_is_never_touched_by_the_paren_marker_strip():
    """The exact case the careful rule exists for: "B. Complex" is a real
    product name, not a corrupted list marker — its TEXT must survive
    completely unchanged. Its certainty, like any other "<letter>."-
    prefixed name, is now honestly UNCERTAIN rather than blindly HIGH —
    see `test_the_short_prefix_shape_is_flagged_uncertain_for_both_a_real_
    product_name_and_a_likely_corrupted_marker` for why this is the
    correct, safe outcome for both readings of this one ambiguous shape."""
    entities = [{"medicine": "B. Complex", "dosage": "500 mg", "frequency": "", "duration": ""}]
    rows, _ = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "B. Complex"


def test_a_bare_letter_period_marker_text_is_deliberately_left_alone():
    """ "G. Amoxicillin" (a corrupted list number, no parenthesis) is NOT
    stripped — the same shape is indistinguishable from a real product
    name like "B. Complex" without a drug dictionary, so guessing either
    way is avoided entirely rather than risk damaging a real name."""
    entities = [{"medicine": "G. Amoxicillin", "dosage": "", "frequency": "", "duration": ""}]
    rows, _ = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "G. Amoxicillin"


def test_the_short_prefix_shape_is_flagged_uncertain_for_both_a_real_product_name_and_a_likely_corrupted_marker():
    """Since the shape alone can't tell "G. Amoxicillin" (likely a
    corrupted list marker) apart from "B. Complex" (a real product name),
    the safe resolution is to flag BOTH as uncertain — never silently
    trusted as a clean HIGH-confidence reading, but never rewritten
    either. Wrong for neither: an uncertain real name is still shown
    correctly, just prompting a quick double-check; an uncertain corrupted
    name is honestly no longer claimed to be a clean read."""
    for name in ("G. Amoxicillin", "B. Complex"):
        entities = [{"medicine": name, "dosage": "", "frequency": "", "duration": ""}]
        rows, certainty = norm.normalize_prescription_rows(entities)
        assert rows[0]["medicine"] == name  # text always unchanged
        assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


def test_a_single_letter_period_prefix_with_no_capital_word_after_it_is_not_flagged():
    """The shape requires a CAPITALISED word right after the prefix (what
    a real name always has) — a value that's just "G." alone, or "g. abc"
    (no capital), is a different, narrower situation this check does not
    try to judge."""
    entities = [{"medicine": "G. abc", "dosage": "", "frequency": "", "duration": ""}]
    _rows, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0]["medicine"] == norm.CERTAINTY_HIGH


def test_a_real_parenthetical_not_at_the_very_start_is_never_stripped():
    """The paren-marker strip only ever matches at position 0 — a genuine
    parenthetical elsewhere in a name must survive completely unchanged
    (it is still flagged ambiguous by the separate embedded-junk-character
    check, same as any other mid-value "(" — that check is about
    certainty, not about whether this step would have deleted it)."""
    entities = [
        {"medicine": "Panadol (Extra)", "dosage": "500 mg", "frequency": "", "duration": ""}
    ]
    rows, _ = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == "Panadol (Extra)"


# ── Confident-fabrication guards (OCR/extraction fix-up, continued) ──────
# TrOCR's "confident hallucination" failure (see `ocr.py`'s module
# docstring): a severely degraded crop can make the model fall back to
# fluent but entirely fabricated text instead of failing cleanly. Neither
# `ocr._looks_degenerate` nor per-token confidence alone can catch it,
# since it is real words, not symbol noise. These are the field-shape
# guard: a real medicine/dosage value is never a full sentence.
WIKI_HALLUCINATION = (
    "What links hereRelated changesUpload fileSpecial pagesPermanent "
    "linkPage informationCite this pageWikidata item"
)


def test_a_real_hallucinated_sentence_in_the_medicine_field_is_flagged_ambiguous():
    """The exact real hallucination text observed earlier in this
    engagement — glued together with no real spacing, so this also proves
    the camelCase-boundary tokenizer, not just a plain word-count check."""
    entities = [{"medicine": WIKI_HALLUCINATION, "dosage": "", "frequency": "", "duration": ""}]
    rows, certainty = norm.normalize_prescription_rows(entities)
    assert rows[0]["medicine"] == WIKI_HALLUCINATION  # never rewritten, only flagged
    assert certainty[0]["medicine"] == norm.CERTAINTY_AMBIGUOUS


def test_a_constructed_full_sentence_in_the_medicine_field_is_flagged_ambiguous():
    entities = [
        {
            "medicine": "Patient should take this medication as prescribed by the doctor",
            "dosage": "",
            "frequency": "",
            "duration": "",
        }
    ]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0]["medicine"] == norm.CERTAINTY_AMBIGUOUS


def test_a_dosage_field_with_a_full_sentence_is_flagged_ambiguous():
    entities = [
        {
            "medicine": "Metformin",
            "dosage": "Patient should take this medication as prescribed",
            "frequency": "",
            "duration": "",
        }
    ]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0]["dosage"] == norm.CERTAINTY_AMBIGUOUS


def test_a_dosage_field_with_no_number_and_no_unit_word_is_flagged_ambiguous():
    """Short, not sentence-shaped, but still clearly not a real dosage —
    e.g. misattributed frequency text ("twice daily") landing in the
    dosage slot instead."""
    entities = [{"medicine": "Metformin", "dosage": "twice daily", "frequency": "", "duration": ""}]
    _, certainty = norm.normalize_prescription_rows(entities)
    assert certainty[0]["dosage"] == norm.CERTAINTY_AMBIGUOUS


def test_real_dosages_with_a_number_or_unit_are_never_flagged():
    for dosage in ("500 mg", "1-0-1", "1000 IU", "2 tablets", "5ml", "10mg"):
        entities = [{"medicine": "Metformin", "dosage": dosage, "frequency": "", "duration": ""}]
        _, certainty = norm.normalize_prescription_rows(entities)
        assert certainty[0]["dosage"] == norm.CERTAINTY_HIGH, dosage


def test_real_multi_word_and_combination_medicine_names_are_never_flagged():
    for name in (
        "Paracetamol",
        "Amoxicillin Clavulanate",
        "Vitamin B Complex",
        "Co-Amoxiclav",
        "G. Amoxicillin",
        "Panadol Extra",
    ):
        entities = [{"medicine": name, "dosage": "", "frequency": "", "duration": ""}]
        _, certainty = norm.normalize_prescription_rows(entities)
        assert certainty[0]["medicine"] != norm.CERTAINTY_AMBIGUOUS, name


def test_words_splits_on_camel_case_boundaries_as_well_as_whitespace():
    assert norm._words("hereRelated changesUpload") == [
        "here",
        "Related",
        "changes",
        "Upload",
    ]


# ── Junk list markers and misread dosage units, as seen on real scans ────
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("@) Amoxicillin", "Amoxicillin"),
        ("G) Vitamin D3", "Vitamin D3"),
        ("&, Losartan", "Losartan"),
        ("& Metformin", "Metformin"),
    ],
)
def test_a_junk_list_marker_is_stripped_and_the_row_is_flagged_uncertain(raw, cleaned):
    rows, certainty = norm.normalize_prescription_rows(
        [{"medicine": raw, "dosage": "", "frequency": "", "duration": ""}]
    )
    assert rows[0]["medicine"] == cleaned
    assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


@pytest.mark.parametrize("name", ["Tylenol & Codeine", "+Calcium", "Panadol (Extra)", "3TC"])
def test_a_name_that_merely_contains_or_starts_with_a_symbol_is_never_stripped(name):
    rows, _ = norm.normalize_prescription_rows(
        [{"medicine": name, "dosage": "", "frequency": "", "duration": ""}]
    )
    assert rows[0]["medicine"] == name


@pytest.mark.parametrize("name", ["db Metformin", "iv Fluid"])
def test_a_lowercase_letter_prefix_is_flagged_but_never_stripped(name):
    rows, certainty = norm.normalize_prescription_rows(
        [{"medicine": name, "dosage": "", "frequency": "", "duration": ""}]
    )
    assert rows[0]["medicine"] == name  # could be a real name: never rewritten
    assert certainty[0]["medicine"] == norm.CERTAINTY_UNCERTAIN


@pytest.mark.parametrize("dosage", ["500 ag", "20 mq", "500 rng"])
def test_a_dosage_whose_unit_is_not_a_real_unit_is_flagged_uncertain_not_corrected(dosage):
    rows, certainty = norm.normalize_prescription_rows(
        [{"medicine": "Metformin", "dosage": dosage, "frequency": "", "duration": ""}]
    )
    assert rows[0]["dosage"] == dosage
    assert certainty[0]["dosage"] == norm.CERTAINTY_UNCERTAIN


@pytest.mark.parametrize(
    "dosage",
    [
        "500 mg",
        "500mg",
        "1000 IU",
        "600 mg / 400 IU",
        "120mg/5ml",
        "250 mg/5 ml",
        "5 ml",
        "2 tablets",
        "1 capsule",
        "0.5 mg",
        "1-0-1",
        "2 puffs",
        "5 cc",
    ],
)
def test_real_dosages_are_never_flagged_for_their_unit(dosage):
    _, certainty = norm.normalize_prescription_rows(
        [{"medicine": "Metformin", "dosage": dosage, "frequency": "", "duration": ""}]
    )
    assert certainty[0]["dosage"] == norm.CERTAINTY_HIGH
