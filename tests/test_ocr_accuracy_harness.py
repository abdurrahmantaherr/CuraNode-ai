"""The OCR accuracy harness's own correctness — the CER/WER math itself,
not a run against the real engines (that's what running the script does;
see `backend/ops/scripts/ocr_accuracy_harness.py`). Pure, fast, deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "ops" / "scripts"))

from ocr_accuracy_harness import _cer, _edit_distance, _wer


def test_identical_strings_have_zero_edit_distance():
    assert _edit_distance("Metformin", "Metformin") == 0


def test_a_single_substitution_has_distance_one():
    assert _edit_distance("Metformin", "Metformln") == 1


def test_edit_distance_is_symmetric():
    assert _edit_distance("kitten", "sitting") == _edit_distance("sitting", "kitten")
    assert _edit_distance("kitten", "sitting") == 3  # a well-known textbook example


def test_cer_of_a_perfect_reading_is_zero():
    assert _cer("Metformin 500mg", "Metformin 500mg") == 0.0


def test_cer_counts_character_edits_as_a_fraction_of_the_reference_length():
    # "Metformln" vs "Metformin": 1 substitution, reference is 9 chars.
    assert round(_cer("Metformln", "Metformin"), 3) == round(1 / 9, 3)


def test_cer_of_completely_wrong_output_is_around_one():
    assert _cer("xxxxxxxxx", "Metformin") == 1.0


def test_cer_handles_an_empty_reference_without_dividing_by_zero():
    assert _cer("", "") == 0.0
    assert _cer("anything", "") == 1.0


def test_wer_of_a_perfect_reading_is_zero():
    assert _wer("Take 1 tablet twice daily", "Take 1 tablet twice daily") == 0.0


def test_wer_counts_whole_word_edits_not_characters():
    # One wrong word out of five — a single character typo inside a word
    # still counts as exactly one WORD edit, not proportional to how many
    # characters differ.
    ref = "Take 1 tablet twice daily"
    hyp = "Take 1 tablet thrice daily"
    assert _wer(hyp, ref) == 1 / 5


def test_wer_handles_an_empty_reference_without_dividing_by_zero():
    assert _wer("", "") == 0.0
    assert _wer("stray words", "") == 1.0


def test_cer_and_wer_disagree_on_a_single_character_typo_inside_one_word():
    """Exactly the distinction CER vs WER exists to capture: a one-character
    error inside one word is a SMALL character-level error but a WHOLE
    word-level error."""
    ref = "Metformin 500mg twice daily"
    hyp = "Metformln 500mg twice daily"
    assert 0 < _cer(hyp, ref) < 0.1  # small fraction of total characters
    assert _wer(hyp, ref) == 1 / 4  # but one full word out of four is wrong
