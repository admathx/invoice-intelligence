"""Turns a canonical SKU's plain-English name into a distributor-style abbreviated
description, e.g. "Mozzarella Shredded Whole Milk" -> "MOZZ SHRD WHL MLK".

Each distributor template applies this at a different `intensity` (probability per
word) so the four layouts don't all abbreviate identically even from the same word
list — this, not a different dictionary per distributor, is what SPEC.md §8 means by
"different abbreviation styles" in practice: real distributor invoices vary in *how
aggressively* they abbreviate, not in a wholly different vocabulary.
"""
import random

WORD_ABBREVIATIONS: dict[str, str] = {
    "BONELESS": "BNLS",
    "SKINLESS": "SKLS",
    "SHREDDED": "SHRD",
    "WHOLE": "WHL",
    "MILK": "MLK",
    "EXTRA": "X",
    "VIRGIN": "VRG",
    "CHICKEN": "CHKN",
    "BREAST": "BRST",
    "GROUND": "GRND",
    "FROZEN": "FRZN",
    "PEELED": "PLD",
    "DEVEINED": "DVND",
    "GALLON": "GAL",
    "SLICED": "SLCD",
    "CRUMBLED": "CRMBL",
    "GRATED": "GRTD",
    "DICED": "DCD",
    "TENDERLOIN": "TNDRLN",
    "DRUMSTICKS": "DRMSTK",
    "WRAPPED": "WRPD",
    "DISPOSABLE": "DISP",
    "SANITIZER": "SANIT",
    "CLEANER": "CLNR",
    "SOLUTION": "SOLN",
    "CONTAINER": "CONT",
    "PLASTIC": "PLST",
    "PARCHMENT": "PRCHMT",
    "PACKAGE": "PKG",
    "SEASONING": "SEASNG",
    "DRESSING": "DRSNG",
    "VINEGAR": "VNGR",
    "WORCESTERSHIRE": "WRCSTR",
    "BREADCRUMBS": "BRDCRMB",
    "TENDERS": "TNDRS",
    "TENDERS)": "TNDRS)",
    "DEBONED": "DBND",
    "GRANULATED": "GRAN",
}


def abbreviate_description(name: str, intensity: float, rng: random.Random) -> str:
    words = name.upper().split()
    out = []
    for word in words:
        replacement = WORD_ABBREVIATIONS.get(word)
        if replacement and rng.random() < intensity:
            out.append(replacement)
        else:
            out.append(word)
    return " ".join(out)
