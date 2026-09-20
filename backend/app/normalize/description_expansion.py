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


def _identity_tokens(description: str) -> set[str]:
    """The words that say WHAT an item is, with pack-size noise removed.

    Purely numeric tokens are dropped because many distributors print the pack
    into the description ("MOZZ SHRD WHL MLK 4/5 LB"), and those digits are
    shared by every item sold in that pack — they made a mozzarella and a
    cheddar in the same case size look 62% alike, enough to hide a reassigned
    code. Pack size has its own parser (app/normalize/pack_size.py) and its own
    place in the pipeline; it is not product identity.
    """
    return {t for t in normalize_for_embedding(description).upper().split() if not t.isdigit()}


def _prefix_matches(token: str, others: set[str]) -> bool:
    """A token counts as present if some other token is it, extends it, or is
    extended by it. Distributor invoices truncate to a fixed column width
    ("CUCUMBER" prints as "CUCU", "BACON SLICED" as "BACON S"), so exact token
    equality reports two printings of the SAME line as completely unrelated.
    """
    return any(other.startswith(token) or token.startswith(other) for other in others)


def description_similarity(left: str, right: str) -> float:
    """How much two raw invoice descriptions look like the same item, 0..1.

    Deliberately free: no embedding call, no model call. It exists to guard the
    alias path (app/normalize/matcher.py), whose entire value is costing
    nothing — paying for a similarity model there would defeat the purpose.

    Compared raw-to-raw, not raw-to-canonical-name: an alias stores the exact
    text that was on the invoice, so the same distributor printing the same
    line next week scores high even where that text embeds poorly against the
    canonical SKU's proper name, which is precisely why the alias is worth
    having.

    Symmetric coverage rather than plain Jaccard, and prefix-tolerant per
    _prefix_matches: measured against the corpus, exact-token Jaccard scored
    the same item code's own descriptions at a median of 0.333 and put
    "CUCU"/"CUCUMBER" at 0.000, which would have disabled the alias path
    wholesale rather than catching reassigned codes.
    """
    left_tokens = _identity_tokens(left)
    right_tokens = _identity_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    matched = sum(_prefix_matches(t, right_tokens) for t in left_tokens)
    matched += sum(_prefix_matches(t, left_tokens) for t in right_tokens)
    return matched / (len(left_tokens) + len(right_tokens))
