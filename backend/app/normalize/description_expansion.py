"""Expands common foodservice-distributor description abbreviations before
embedding, e.g. "MOZZ SHRD WHL MLK" -> "MOZZ SHREDDED WHOLE MILK". Real
distributor invoices use a fairly standard, finite abbreviation vocabulary
(this is industry convention, not something invented for this repo), so a
maintained glossary is a legitimate normalization step, not a shortcut that
only works on synthetic data.
"""
import re
import unicodedata

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
    # Added from a set of realistic invoices from four broadline
    # distributors, where these left most items without a suggestion
    # ("SHRMP 16/20 P&D TAIL ON" scored 0.31 against Shrimp 16/20 Peeled
    # Deveined). All standard distributor shorthand.
    "SHRMP": "SHRIMP",
    # Pork butt is the shoulder cut; it matched Pork Chop Boneless.
    "BUTT": "SHOULDER",
    # Solid cheese is block cheese; "CHEESE CHEDDAR SOLID" was offered as
    # shredded. (Solid butter is block butter too.)
    "SOLID": "BLOCK",
    "P&D": "PEELED DEVEINED",
    "DEV": "DEVEINED",
    "TLO": "TAIL ON",
    "FIL": "FILLET",
    "FRZ": "FROZEN",
    "RST": "ROAST",
    "BRSKT": "BRISKET",
    "PKCR": "PACKER",
    "SKNLS": "SKINLESS",
    "CHS": "CHEESE",
    "WM": "WHOLE MILK",
    "CRM": "CREAM",
    "UNS": "UNSALTED",
    "UNSLTD": "UNSALTED",
    "HMBGR": "HAMBURGER",
    "HOAG": "HOAGIE",
    "FLR": "FLOUR",
    "AP": "ALL PURPOSE",
    "BLCHD": "BLEACHED",
    "GLTN": "GLUTEN",
    "HI": "HIGH",
    "HG": "HIGH GLUTEN",
    "LNG": "LONG",
    "RUSS": "RUSSET",
    "JBO": "JUMBO",
    "YEL": "YELLOW",
    "YLLW": "YELLOW",
    "PEPR": "PEPPER",
    "GND": "GROUND",
    "KOSHR": "KOSHER",
    "OLIV": "OLIVE",
    "XV": "EXTRA VIRGIN",
    "XTR": "EXTRA",
    "VIRGN": "VIRGIN",
    "SCE": "SAUCE",
    "ORIG": "ORIGINAL",
    "KTCHP": "KETCHUP",
    "HVY": "HEAVY",
    "DTY": "DUTY",
    "HD": "HEAVY DUTY",
    "LRG": "LARGE",
    "LG": "LARGE",
    "PCT": "PERCENT",
    "SL": "SLICED",
    "HRT": "HEARTS",
    "CLR": "CLEAR",
    "CLD": "COLD",
    "PPR": "PAPER",
    "CNTR": "CONTAINER",
    "HNGD": "HINGED",
    "TOGO": "TO GO",
    "NPKN": "NAPKIN",
    "DNR": "DINNER",
    "TWL": "TOWEL",
    "TRSH": "TRASH",
    "GLV": "GLOVE",
    "BLCH": "BLEACH",
    "CONC": "CONCENTRATE",
    "DEGRSR": "DEGREASER",
    "SANTZR": "SANITIZER",
    "QAT": "QUATERNARY",
    "WH": "WHITE",
    # Added from a set of matching traps, where the tersest shorthand got no
    # suggestion at all ("CHX BST B/S 6Z IQF", "MOZ SHR WM LMPS", "TOM RMA
    # 25", "RST BF CHK").
    "CHX": "CHICKEN",
    "CHIX": "CHICKEN",
    "CKN": "CHICKEN",
    "BST": "BREAST",
    "THGH": "THIGH",
    "B/S": "BONELESS SKINLESS",
    "BF": "BEEF",
    "GRD": "GROUND",
    "PRK": "PORK",
    "TRKY": "TURKEY",
    "SLMN": "SALMON",
    "MOZ": "MOZZARELLA",
    "SHRED": "SHREDDED",
    "LMPS": "LOW MOISTURE PART SKIM",
    "TOM": "TOMATO",
    "RMA": "ROMA",
    "LETT": "LETTUCE",
    "AVOC": "AVOCADO",
    "BTR": "BUTTER",
    "MAYO": "MAYONNAISE",
    "IQF": "FROZEN",
    "EVOO": "EXTRA VIRGIN OLIVE OIL",
    # From the sixth set: "PORK SHLDR" was offered Pork Tenderloin.
    "SHLDR": "SHOULDER",
    "HAMB": "HAMBURGER",
    "RUS": "RUSSET",
    "ORG": "ORGANIC",
}

# Kitchen Spanish, as local suppliers' invoices print it ("PECHUGA DE POLLO
# S/H", "ACEITE DE CANOLA", "QUESO MOZZARELLA RALLADO"). Looked up after
# accents are removed.
SPANISH: dict[str, str] = {
    "POLLO": "CHICKEN",
    "PECHUGA": "BREAST",
    "MUSLO": "THIGH",
    "ALAS": "WINGS",
    "S/H": "BONELESS",
    # Not RES: on an English row it is resealable, or reserve.
    "CARNE": "BEEF",
    "MOLIDA": "GROUND",
    "CERDO": "PORK",
    "PUERCO": "PORK",
    "TOCINO": "BACON",
    "CAMARON": "SHRIMP",
    "CAMARONES": "SHRIMP",
    "PESCADO": "FISH",
    "HUEVO": "EGGS",
    "HUEVOS": "EGGS",
    "GRANDE": "LARGE",
    "GRANDES": "LARGE",
    "QUESO": "CHEESE",
    "RALLADO": "SHREDDED",
    "LECHE": "MILK",
    "ENTERA": "WHOLE",
    "CREMA": "CREAM",
    "MANTEQUILLA": "BUTTER",
    "ACEITE": "OIL",
    "ARROZ": "RICE",
    "FRIJOL": "BEANS",
    "FRIJOLES": "BEANS",
    "NEGROS": "BLACK",
    "HARINA": "FLOUR",
    "AZUCAR": "SUGAR",
    "CEBOLLA": "ONION",
    "TOMATE": "TOMATO",
    "AGUACATE": "AVOCADO",
    "PAPA": "POTATO",
    "PAPAS": "POTATO",
    "LECHUGA": "LETTUCE",
    "SERVILLETAS": "NAPKINS",
    "VASOS": "CUPS",
    "GUANTES": "GLOVES",
    "CLORO": "BLEACH",
    "CONGELADO": "FROZEN",
    "CONGELADA": "FROZEN",
    "FRESCO": "FRESH",
    "FRESCA": "FRESH",
    "BLANCO": "WHITE",
    "BLANCA": "WHITE",
    "AMARILLA": "YELLOW",
    "AMARILLO": "YELLOW",
}
# Dropped from a description that has any of the words above.
_SPANISH_PARTICLES = {"DE", "DEL", "LA", "EL", "CON", "SIN", "Y"}

# Shorthand whose meaning depends on the word before it: GRN is GREEN on a
# pepper and GRAIN on rice, and "LONG GREEN" rice matched Rice Brown.
# CHK is chuck after beef (and chunk on tuna, chicken elsewhere, so it has
# no entry above); GRD is GROUND on beef and GRADE before an egg's A or AA.
_PHRASE_FIXES = (
    (re.compile(r"\bLONG GREEN\b"), "LONG GRAIN"),
    (re.compile(r"\bBEEF CHK\b"), "BEEF CHUCK"),
    (re.compile(r"\bGROUND (AA?)\b"), r"GRADE \1"),
)

# "16Z" is how some distributors print 16 OZ.
_OUNCES = re.compile(r"^(\d+(?:\.\d+)?)Z$")


def _without_accents(text: str) -> str:
    """"JALAPEÑO" as "JALAPENO": the accent used to become a space, leaving
    "JALAPE O"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def expand_description(raw_description: str) -> str:
    words = [_OUNCES.sub(r"\1OZ", w) for w in _without_accents(raw_description.upper()).split()]
    if any(w in SPANISH for w in words):
        words = [SPANISH.get(w, w) for w in words if w not in _SPANISH_PARTICLES]
    expanded = " ".join(ABBREVIATION_EXPANSIONS.get(w, w) for w in words)
    for wrong, right in _PHRASE_FIXES:
        expanded = wrong.sub(right, expanded)
    return expanded


# A percent sign stays: "MILK REDUCED FAT 2%" is 2% Milk at 0.75 with it and
# Whole Milk at 0.55 without.
_NON_ALNUM = re.compile(r"[^A-Z0-9% ]+")


def normalize_for_embedding(raw_description: str) -> str:
    expanded = expand_description(raw_description)
    return _NON_ALNUM.sub(" ", expanded).strip()


# The shortest catalog word a misspelling is corrected to, and the shortest
# word corrected. Long words only: one letter turns BATTER into BUTTER, and
# both are things a kitchen buys.
MIN_CORRECTED_TO = 7
MIN_CORRECTED = 6
_LETTERS = re.compile(r"[A-Z]+")


def vocabulary_of(names) -> frozenset[str]:
    """The words of product names that a misspelling may be corrected to."""
    return frozenset(
        word for name in names for word in _LETTERS.findall(_without_accents(name.upper())) if len(word) >= MIN_CORRECTED_TO
    )


def _one_slip_apart(a: str, b: str) -> bool:
    """One letter missing, added, wrong, or swapped with its neighbor."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    start = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), len(a))
    if len(a) < len(b):
        return a[start:] == b[start + 1 :]
    return a[start + 1 :] == b[start + 1 :] or (a[start : start + 2] == b[start : start + 2][::-1] and a[start + 2 :] == b[start + 2 :])


def correct_spelling(raw_description: str, vocabulary: frozenset[str]) -> str:
    """A description with its misspelled words put right: "CHIKEN BRST" is
    "CHICKEN BRST", "MOZARELLA" is "MOZZARELLA". Hand-keyed invoices from
    small suppliers are full of these, and a misspelling reads to the
    similarity search as a different word: "CHIKEN BREAST" was nearest Duck
    Breast.

    Only a word that is one slip from exactly one catalog word, starts with
    the same letter, and isn't a catalog word or known shorthand itself."""
    words = _without_accents(raw_description.upper()).split()

    def corrected(word: str) -> str:
        if len(word) < MIN_CORRECTED or not word.isalpha() or word in vocabulary:
            return word
        if word in ABBREVIATION_EXPANSIONS or word in SPANISH:
            return word
        near = [known for known in vocabulary if known[0] == word[0] and _one_slip_apart(word, known)]
        return near[0] if len(near) == 1 else word

    return " ".join(corrected(word) for word in words)


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
