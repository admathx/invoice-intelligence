"""Expands common foodservice-distributor description abbreviations before
embedding, e.g. "MOZZ SHRD WHL MLK" -> "MOZZ SHREDDED WHOLE MILK". Real
distributor invoices use a fairly standard, finite abbreviation vocabulary
(this is industry convention, not something invented for this repo), so a
maintained glossary is a legitimate normalization step, not a shortcut that
only works on synthetic data.
"""
import re

ABBREVIATION_EXPANSIONS: dict[str, str] = {
    "BNLS": "BONELESS",
    "SKLS": "SKINLESS",
    "SHRD": "SHREDDED",
    "WHL": "WHOLE",
    "MLK": "MILK",
    "CHKN": "CHICKEN",
    "BRST": "BREAST",
    "GRND": "GROUND",
    "FRZN": "FROZEN",
    "PLD": "PEELED",
    "DVND": "DEVEINED",
    "SLCD": "SLICED",
    "CRMBL": "CRUMBLED",
    "CRMBLD": "CRUMBLED",
    "GRTD": "GRATED",
    "DCD": "DICED",
    "TNDRLN": "TENDERLOIN",
    "TNDRS": "TENDERS",
    "DRMSTK": "DRUMSTICKS",
    "WRPD": "WRAPPED",
    "DISP": "DISPOSABLE",
    "SANIT": "SANITIZER",
    "CLNR": "CLEANER",
    "SOLN": "SOLUTION",
    "CONT": "CONTAINER",
    "PLST": "PLASTIC",
    "PRCHMT": "PARCHMENT",
    "PKG": "PACKAGE",
    "SEASNG": "SEASONING",
    "DRSNG": "DRESSING",
    "VNGR": "VINEGAR",
    "WRCSTR": "WORCESTERSHIRE",
    "BRDCRMB": "BREADCRUMBS",
    "DBND": "DEBONED",
    "GRAN": "GRANULATED",
    "X": "EXTRA",
    "VRG": "VIRGIN",
    "MOZZ": "MOZZARELLA",
    "PARM": "PARMESAN",
    "CHED": "CHEDDAR",
    "PROV": "PROVOLONE",
    "MASC": "MASCARPONE",
    "AMER": "AMERICAN",
    "ITAL": "ITALIAN",
    "VEG": "VEGETABLE",
    "CUKE": "CUCUMBER",
    "JAL": "JALAPENO",
    "MUSHRM": "MUSHROOM",
    "BROC": "BROCCOLI",
    "CAUL": "CAULIFLOWER",
    "ZUCC": "ZUCCHINI",
    "QUAT": "QUATERNARY",
    "STNLS": "STAINLESS",
    "WORC": "WORCESTERSHIRE",
    "YLW": "YELLOW",
    "WHT": "WHITE",
    "BLK": "BLACK",
    "GRN": "GREEN",
}


def expand_description(raw_description: str) -> str:
    words = raw_description.upper().split()
    return " ".join(ABBREVIATION_EXPANSIONS.get(w, w) for w in words)


_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")


def normalize_for_embedding(raw_description: str) -> str:
    expanded = expand_description(raw_description)
    return _NON_ALNUM.sub(" ", expanded).strip()
