"""The word that tells two products apart.

Description similarity puts "CHEESE MOZZARELLA BLOCK" close to Mozzarella
Shredded Whole Milk, "EGG MEDIUM 15DZ" close to Eggs Large and "CUPS HOT
PAPER 12 OZ" close to Cups 16oz Hot: nearly every word is shared, and the
one that isn't is the whole difference. Offered to a person as a suggestion,
each is one careless click from putting one product's price in another's
history; above the automatic threshold it needs no click at all.

So a candidate is passed over when the line and the product each say
something on the same point and say different things: a form (block,
shredded), a size (medium, large; 12 oz, 16 oz), a grade (80/20, 90/10;
pomace, pure), a color, a cut; or when, those words aside, they have no
word in common. A point only one of them speaks to is no
conflict: "GLOVES NITRILE LARGE" is Gloves Nitrile, and a frozen chicken
breast is offered Chicken Breast when that is the only one the catalog has.

Past a contradicted product, only another variety of the same thing is
taken: the same name apart from these words, and agreeing with the line on
the point the first one didn't ("ONION YELLOW" passes over Onion White for
Onion Yellow). Anything else that happens to come next is not: past the
shredded mozzarellas the nearest thing to a block of mozzarella was a block
of cheddar, and past both ground beefs the nearest to "BEEF GRND 81/19" was
a beefsteak tomato.
"""
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.normalize.description_expansion import expand_description

# Each group is one point a description can speak to: a value, and the
# words or phrases that say it. Longer phrases are looked for first and
# taken out, so "EXTRA LARGE" isn't also LARGE and "PART SKIM" isn't SKIM.
_GROUPS: tuple[dict[str, tuple[str, ...]], ...] = (
    # Form.
    {
        "shredded": ("SHREDDED", "SHRED"),
        "block": ("BLOCK", "LOAF"),
        "sliced": ("SLICED", "SLICES", "SLICE"),
        "grated": ("GRATED",),
        "crumbled": ("CRUMBLED", "CRUMBLES"),
        "diced": ("DICED",),
        "cubed": ("CUBED", "CUBES"),
        "string": ("STRING",),
    },
    # Size, in words.
    {
        "small": ("SMALL",),
        "medium": ("MEDIUM", "MED"),
        "large": ("LARGE",),
        "extra large": ("EXTRA LARGE", "XLARGE", "XL"),
        "jumbo": ("JUMBO",),
    },
    # Milk fat.
    {
        "whole": ("WHOLE",),
        "part skim": ("PART SKIM",),
        "skim": ("SKIM", "NONFAT", "FAT FREE"),
        "2%": ("2 PERCENT", "REDUCED FAT"),
        "1%": ("1 PERCENT", "LOWFAT", "LOW FAT"),
    },
    {"salted": ("SALTED",), "unsalted": ("UNSALTED", "NO SALT")},
    {"hot": ("HOT",), "cold": ("COLD",)},
    {"fresh": ("FRESH",), "frozen": ("FROZEN",)},
    {"raw": ("RAW",), "cooked": ("COOKED",)},
    {"ground": ("GROUND",), "whole": ("WHOLE",)},
    {"boneless": ("BONELESS",), "bone in": ("BONE IN",)},
    # Color.
    {
        "white": ("WHITE",),
        "yellow": ("YELLOW",),
        "red": ("RED",),
        "green": ("GREEN",),
        "brown": ("BROWN",),
        "black": ("BLACK",),
    },
    # Cut of a bird.
    {
        "breast": ("BREAST", "BREASTS"),
        "thigh": ("THIGH", "THIGHS"),
        "wing": ("WING", "WINGS"),
        "drumstick": ("DRUMSTICK", "DRUMSTICKS"),
        "tender": ("TENDER", "TENDERS"),
        "leg": ("LEG", "LEGS"),
    },
    # Grade of olive oil, and of beef.
    {"extra virgin": ("EXTRA VIRGIN",), "pure": ("PURE",), "pomace": ("POMACE",)},
    {"choice": ("CHOICE",), "select": ("SELECT",), "prime": ("PRIME",)},
)

# A size with its unit: "12 OZ", "16OZ", "18 INCH", "9in", "33GAL".
_MEASURE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(OZ|INCH(?:ES)?|IN|\"|GALLONS?|GAL|MM)(?![A-Z])")
_UNIT = {"INCH": "IN", "INCHES": "IN", '"': "IN", "GALLON": "GAL", "GALLONS": "GAL"}
# A ratio that is a grade or a count per pound ("80/20", "16/20 CT"), not a
# pack size ("4/5 LB", "4/10#", "6/#10").
_RATIO = re.compile(
    r"(?<![\d/.])(\d{1,3})/(\d{1,3})(?![\d/.])(?!\s*(?:#|(?:LBS?|OZ|GAL|DZ|KG|G|L|LT|ML|PK|CAN|QT|PT|EA)\b))"
)
_NOT_A_WORD = re.compile(r"[^A-Z0-9 ]+")
# Longest first, so "EXTRA LARGE" isn't also LARGE and "PART SKIM" isn't SKIM.
_PHRASES = [
    sorted(((phrase, value) for value, phrases in group.items() for phrase in phrases), key=lambda p: -len(p[0]))
    for group in _GROUPS
]


@dataclass
class _Reading:
    """What a description says: its value on each point it speaks to (a
    group's number, a size's unit, or "ratio"), and the words left over,
    which name the thing itself."""

    points: dict[object, set[str]]
    name: frozenset[str]


def _read(description: str) -> _Reading:
    expanded = expand_description(description).replace("%", " PERCENT ")
    points: dict[object, set[str]] = {}
    for number, unit in _MEASURE.findall(expanded):
        points.setdefault(_UNIT.get(unit, unit), set()).add(str(float(number)))
    ratios = {f"{int(a)}/{int(b)}" for a, b in _RATIO.findall(expanded)}
    if ratios:
        points["ratio"] = ratios
    rest = _RATIO.sub(" ", _MEASURE.sub(" ", expanded))
    words = name = f" {' '.join(_NOT_A_WORD.sub(' ', rest).split())} "
    for number, phrases in enumerate(_PHRASES):
        # Each group reads the whole description: WHOLE is said of milk and
        # of peppercorns, and one group taking it mustn't hide it from the
        # other.
        unread = words
        for phrase, value in phrases:
            if f" {phrase} " in unread:
                points.setdefault(number, set()).add(value)
                unread = unread.replace(f" {phrase} ", "  ")
                name = name.replace(f" {phrase} ", "  ")
    return _Reading(points, frozenset(word for word in name.split() if not word.isdigit()))


def _contradicted_on(line: _Reading, product: _Reading) -> set:
    """The points both speak to and say different things on."""
    return {point for point, said in line.points.items() if point in product.points and not said & product.points[point]}


def _share_a_word(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two names have a word in common, or one's word begins the
    other's ("EGG" and "EGGS", "CUCU" for a cut-off "CUCUMBER")."""
    return any(
        x == y or (min(len(x), len(y)) >= 3 and (x.startswith(y) or y.startswith(x))) for x in a for y in b
    )


def conflicts(raw_description: str, product_name: str) -> bool:
    """Whether an invoice line and a product say different things on a
    point both of them speak to."""
    return bool(_contradicted_on(_read(raw_description), _read(product_name)))


def first_not_contradicted(raw_description: str, product_names: Sequence[str]) -> int | None:
    """Which of `product_names` (nearest first) a line can be offered: the
    first it doesn't contradict, as long as that is the nearest, or another
    variety of the nearest. Products that share no word of their name with
    the line are not looked at. None when there is no such product."""
    line = _read(raw_description)
    nearest: _Reading | None = None
    at_issue: set = set()
    for index, name in enumerate(product_names):
        product = _read(name)
        if line.name and product.name and not _share_a_word(line.name, product.name):
            # Not a variety of anything the line names: "CHICKEN BREAST" is
            # nearer Duck Breast than Chicken Breast Boneless Skinless.
            continue
        contradicted = _contradicted_on(line, product)
        if nearest is None:
            if not contradicted:
                return index
            nearest, at_issue = product, contradicted
            continue
        same_thing = bool(product.name and nearest.name) and (product.name <= nearest.name or nearest.name <= product.name)
        # It has to say, on the point the nearest got wrong, what the line says.
        if not contradicted and same_thing and at_issue <= product.points.keys():
            return index
    return None
