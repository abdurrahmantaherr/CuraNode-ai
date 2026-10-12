"""`documents.drug_names.suggest` — Step 7's local, bundled drug-name
suggestion list. Suggestions only: this module never rewrites anything, it
only proposes names the Review page shows beside the patient's own,
unconditionally editable text."""

from __future__ import annotations

from app.documents import drug_names as dn


def test_a_realistic_ocr_near_miss_is_suggested():
    assert dn.suggest("Cetirizing") == ["Cetirizine"]
    assert dn.suggest("Omperazole") == ["Omeprazole"]
    assert dn.suggest("Metformln") == ["Metformin"]
    assert dn.suggest("Vitamn D3") == ["Vitamin D3"]


def test_a_corrupted_list_marker_prefix_still_resolves_to_the_real_name():
    """ "G. Amoxicillin" is left completely untouched by `normalization.py`
    (see `_SHORT_PREFIX_MARKER_SHAPE`) — the suggestion layer picks up
    exactly where that left off, offering "Amoxicillin" as a possible
    reading without ever rewriting the stored value itself."""
    assert dn.suggest("G. Amoxicillin") == ["Amoxicillin"]


def test_pakistani_brand_names_are_matched_too():
    assert dn.suggest("Rizek") == ["Risek"]
    assert dn.suggest("Lasik") == ["Lasix"]
    assert dn.suggest("Losek") == ["Losec"]
    assert dn.suggest("Glucofage") == ["Glucophage"]
    assert dn.suggest("Panadal") == ["Panadol"]


def test_an_exact_match_gets_no_suggestion():
    """Already a known name — case-insensitively — so there is nothing to
    suggest; showing "Did you mean: Paracetamol?" for "Paracetamol" would
    just be noise."""
    assert dn.suggest("Paracetamol") == []
    assert dn.suggest("paracetamol") == []
    assert dn.suggest("PARACETAMOL") == []


def test_an_exact_match_is_never_second_guessed_against_a_similar_known_name():
    """Regression: "Atorvastatin" is already a known, correct name — it
    must never be flagged with "did you mean Rosuvastatin?" just because
    the two happen to look alike too (both score 0.75 against each other,
    above the suggestion threshold). Caught by this exact test failing
    before the exact-match check was moved ahead of candidate scoring."""
    assert dn.suggest("Atorvastatin") == []
    assert dn.suggest("Rosuvastatin") == []


def test_a_genuine_hallucination_or_random_text_gets_no_suggestion():
    """ "Mechanism" is the real TrOCR hallucination found earlier in this
    engagement (a degraded "Metformin" misread as this real English word) —
    it must never be offered a confident-looking drug-name suggestion just
    because it happens to share a few letters with something in the list."""
    assert dn.suggest("Mechanism") == []
    assert dn.suggest("xyz123") == []
    assert dn.suggest("") == []
    assert dn.suggest("   ") == []


def test_two_equally_close_known_names_are_both_returned_never_just_one(monkeypatch):
    """The look-alike/sound-alike safety rule, driven deterministically: two
    known names that are each a single same-position substitution away from
    the input score IDENTICALLY — the one true tie this implementation can
    produce — and must come back as BOTH, never a guessed single winner."""
    monkeypatch.setattr(dn, "KNOWN_DRUG_NAMES", frozenset({"Amoxicin", "Amoxitin"}))
    result = dn.suggest("Amoxidin")
    assert set(result) == {"Amoxicin", "Amoxitin"}


def test_one_clearly_closer_known_name_is_returned_alone(monkeypatch):
    """When one candidate is clearly closer than the rest (outside the
    ambiguity margin), only that one is suggested — ambiguity-handling
    must not turn every suggestion into a list "just in case"."""
    monkeypatch.setattr(
        dn, "KNOWN_DRUG_NAMES", frozenset({"Cetirizine", "Levocetirizine", "Paracetamol"})
    )
    assert dn.suggest("Ceterizine") == ["Cetirizine"]


def test_suggestions_are_capped_and_ordered_by_closeness_then_alphabetically(monkeypatch):
    close_names = frozenset({"Amoxacin", "Amoxicin", "Amoxidin", "Amoxitin", "Amoxizin"})
    monkeypatch.setattr(dn, "KNOWN_DRUG_NAMES", close_names)
    result = dn.suggest("Amoxicin")
    assert len(result) <= 4
    assert result == sorted(result, key=lambda n: (-dn._ratio("Amoxicin", n), n))


def test_suggest_never_mutates_its_input_or_the_known_list():
    before = frozenset(dn.KNOWN_DRUG_NAMES)
    dn.suggest("Cetirizing")
    assert dn.KNOWN_DRUG_NAMES == before
