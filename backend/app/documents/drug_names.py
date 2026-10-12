"""Step 7 of the OCR/extraction fix-up: suggested medicine-name corrections
from a small, LOCAL, bundled list — never an external service call, never a
full DRAP/formulary integration. Deliberately small and representative, not
exhaustive: common generics plus the Pakistani brand names patients and
clinics actually write on prescriptions.

A suggestion is display-only and additive, exactly like this whole OCR/
extraction pipeline's "never invent" rule: `suggest()` NEVER rewrites
`OcrExtraction.extracted_value`, never silently picks a winner, and never
replaces what the patient sees in the editable field. The Review page shows
it as a plain hint beside the original, unconditionally editable text — the
patient decides, every time.

Look-alike/sound-alike drug names are a well-documented real-world
medication-error risk (ISMP's own confused-drug-name list — e.g. Losec
(omeprazole) / Lasix (furosemide), both bundled below on purpose): when a
garbled name is roughly equally close to two or more DIFFERENT known names,
`suggest()` returns ALL of them together rather than guessing which one is
"closer" — silently picking one here would be worse than picking none, since
a confident-looking single suggestion for the wrong drug is exactly the kind
of error a patient is least likely to double-check.
"""

from __future__ import annotations

from difflib import SequenceMatcher

# A name below this similarity to EVERY known name is not suggested at
# all — calibrated directly against real near-misses seen earlier in this
# engagement ("Cetirizing" -> 0.90, "Omperazole" -> 0.90, "G. Amoxicillin"
# -> 0.88, "Metformln" -> 0.89) and against genuinely wrong readings that
# must NOT get a suggestion (a TrOCR hallucination, "Mechanism", tops out
# at 0.45 against this whole list; random noise, "xyz123", tops out at
# 0.24) — comfortable margin on both sides.
_MIN_SUGGESTION_SCORE = 0.72
# Two or more known names within this much of the TOP score are considered
# "equally close" and are ALL returned together — never just the single
# highest-scoring one — per the look-alike/sound-alike safety rule above.
_AMBIGUITY_MARGIN = 0.08
_MAX_SUGGESTIONS = 4

# Generics first, then common Pakistani brand names (generic noted in a
# comment). Spellings are the common market/INN spelling; this list is
# representative, not verified against DRAP's own registry — exactly why a
# suggestion is never applied automatically.
KNOWN_DRUG_NAMES: frozenset[str] = frozenset(
    {
        # Generics
        "Paracetamol",
        "Ibuprofen",
        "Amoxicillin",
        "Amoxicillin Clavulanate",
        "Metformin",
        "Atorvastatin",
        "Omeprazole",
        "Esomeprazole",
        "Pantoprazole",
        "Metronidazole",
        "Ciprofloxacin",
        "Azithromycin",
        "Cefixime",
        "Mefenamic Acid",
        "Diclofenac",
        "Aspirin",
        "Clopidogrel",
        "Losartan",
        "Amlodipine",
        "Atenolol",
        "Bisoprolol",
        "Metoprolol",
        "Simvastatin",
        "Rosuvastatin",
        "Levothyroxine",
        "Glimepiride",
        "Gliclazide",
        "Domperidone",
        "Ranitidine",
        "Famotidine",
        "Loratadine",
        "Cetirizine",
        "Fexofenadine",
        "Salbutamol",
        "Montelukast",
        "Prednisolone",
        "Dexamethasone",
        "Vitamin D3",
        "Vitamin B Complex",
        "Vitamin C",
        "Folic Acid",
        "Calcium Carbonate",
        "Ferrous Sulphate",
        "Tramadol",
        "Diazepam",
        "Alprazolam",
        "Sertraline",
        "Amitriptyline",
        "Gabapentin",
        "Pregabalin",
        "Levocetirizine",
        "Doxycycline",
        "Co-Amoxiclav",
        "Clarithromycin",
        "Erythromycin",
        "Furosemide",
        "Enalapril",
        "Lisinopril",
        "Ramipril",
        "Valsartan",
        "Telmisartan",
        "Lorazepam",
        # Pakistani brand names (generic)
        "Panadol",  # Paracetamol
        "Calpol",  # Paracetamol
        "Brufen",  # Ibuprofen
        "Augmentin",  # Amoxicillin Clavulanate
        "Amoxil",  # Amoxicillin
        "Ospamox",  # Amoxicillin
        "Flagyl",  # Metronidazole
        "Risek",  # Omeprazole
        "Losec",  # Omeprazole — classic ISMP look-alike/sound-alike pair with Lasix
        "Lasix",  # Furosemide
        "Zantac",  # Ranitidine
        "Glucophage",  # Metformin
        "Lipitor",  # Atorvastatin
        "Concor",  # Bisoprolol
        "Betaloc",  # Metoprolol
        "Ponstan",  # Mefenamic Acid
        "Disprin",  # Aspirin
        "Ecosprin",  # Aspirin
        "Voltral",  # Diclofenac
        "Ventolin",  # Salbutamol
        "Telfast",  # Fexofenadine
        "Claritin",  # Loratadine
        "Zyrtec",  # Cetirizine
        "Rigix",  # Cetirizine
        "Neurobion",  # Vitamin B Complex
        "Maltofer",  # Iron (ferrous)
        "Osteocare",  # Calcium + Vitamin D
        "Calcimax",  # Calcium
    }
)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def suggest(name: str) -> list[str]:
    """Possible intended readings of `name` from `KNOWN_DRUG_NAMES`, most
    similar first — `[]` if nothing clears the minimum similarity, or if
    `name` already exactly matches (case-insensitively) a known name, since
    there is then nothing to suggest. A genuinely ambiguous near-miss (two
    or more known names within `_AMBIGUITY_MARGIN` of the best score)
    returns ALL of them, never just one — see the module docstring."""
    cleaned = name.strip()
    if not cleaned:
        return []
    lowered = cleaned.lower()
    if any(known.lower() == lowered for known in KNOWN_DRUG_NAMES):
        # Already an exactly-recognised name — never suggest a DIFFERENT
        # known name just because it happens to look similar too (e.g.
        # "Atorvastatin" is already correct; it must never be second-
        # guessed with "did you mean Rosuvastatin?").
        return []
    scored = [(_ratio(cleaned, known), known) for known in KNOWN_DRUG_NAMES]
    candidates = [(score, known) for score, known in scored if score >= _MIN_SUGGESTION_SCORE]
    if not candidates:
        return []
    top_score = max(score for score, _ in candidates)
    close = sorted(
        (known for score, known in candidates if score >= top_score - _AMBIGUITY_MARGIN),
        key=lambda known: (-_ratio(cleaned, known), known),
    )
    return close[:_MAX_SUGGESTIONS]
