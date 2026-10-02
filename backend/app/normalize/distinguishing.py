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
    # What it is made of.
    {"nitrile": ("NITRILE",), "vinyl": ("VINYL",), "latex": ("LATEX",), "rubber": ("RUBBER",), "poly": ("POLY",)},
    {"paper": ("PAPER",), "plastic": ("PLASTIC",), "foam": ("FOAM", "STYROFOAM")},
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
    left = {word for word in name.split() if not word.isdigit()}
    if " WHOLE MILK " in words and len(left) > 1:
        # "Whole milk" said of a cheese is its kind, not its name:
        # Mozzarella Shredded Whole Milk and Part Skim are both mozzarella.
        left.discard("MILK")
    return _Reading(points, frozenset(left))


def _contradicted_on(line: _Reading, product: _Reading) -> set:
    """The points both speak to and say different things on."""
    return {point for point, said in line.points.items() if point in product.points and not said & product.points[point]}


def _shortens(short: str, long: str) -> bool:
    """Whether `short` could be `long` written short: the same first letter
    and its letters in order ("TKY" for TURKEY, "CUCU" for a cut-off
    CUCUMBER, "EGG" for EGGS). Loose on purpose: it only decides whether a
    product is looked at, and shorthand the glossary doesn't know must not
    put the right one out of the running."""
    if len(short) < 3 or short[0] != long[0]:
        return False
    rest = iter(long)
    return all(letter in rest for letter in short)


def _share_a_word(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two names have a word in common, or a word of one could be
    a word of the other written short."""
    return any(x == y or _shortens(*sorted((x, y), key=len)) for x in a for y in b)


def conflicts(raw_description: str, product_name: str) -> bool:
    """Whether an invoice line and a product say different things on a
    point both of them speak to."""
    return bool(_contradicted_on(_read(raw_description), _read(product_name)))


def first_not_contradicted(
    raw_description: str, product_names: Sequence[str], near_enough: Sequence[bool] | None = None
) -> int | None:
    """Which of `product_names` (nearest first) a line can be offered, or
    None when there is no such product. `near_enough` says which of them
    are alike enough to be offered at all (every one, if not given).

    The first it doesn't contradict, as long as that is the nearest, or
    another variety of the nearest. Products that share no word of their
    name with the line are not looked at. Then two refinements, from a set
    of matching traps:

    - a variety of it further down that says what the line says is taken
      over one that says nothing: "PECHUGA DE POLLO" (chicken breast) was
      nearest Whole Chicken, with Chicken Breast Boneless Skinless second.
      Only one near enough to be offered: a good match isn't given up for
      a variety too unlike the line to suggest;
    - and when the catalog has two varieties the line can't tell apart, it
      is offered neither: "CONT TO GO", with no size, was offered the 8 oz
      container of three, and a suggestion is one click from being wrong.
    """
    line = _read(raw_description)
    products = [_read(name) for name in product_names]

    def open_to(product: _Reading) -> bool:
        return not (line.name and product.name and not _share_a_word(line.name, product.name))

    def same_thing(a: _Reading, b: _Reading) -> bool:
        return bool(a.name and b.name) and (a.name <= b.name or b.name <= a.name)

    def agreed(product: _Reading) -> int:
        return sum(1 for point, said in line.points.items() if said & product.points.get(point, set()))

    chosen: int | None = None
    nearest: _Reading | None = None
    at_issue: set = set()
    for index, product in enumerate(products):
        if not open_to(product):
            # Not a variety of anything the line names: "CHICKEN BREAST" is
            # nearer Duck Breast than Chicken Breast Boneless Skinless.
            continue
        contradicted = _contradicted_on(line, product)
        if nearest is None:
            if not contradicted:
                chosen = index
                break
            nearest, at_issue = product, contradicted
            continue
        # It has to say, on the point the nearest got wrong, what the line says.
        if not contradicted and same_thing(product, nearest) and at_issue <= product.points.keys():
            chosen = index
            break
    if chosen is None:
        return None

    for index in range(chosen + 1, len(products)):
        other = products[index]
        if (
            (near_enough is None or near_enough[index])
            and open_to(other)
            and not _contradicted_on(line, other)
            and same_thing(other, products[chosen])
            and agreed(other) > agreed(products[chosen])
        ):
            chosen = index

    mine = products[chosen]
    for index, other in enumerate(products):
        if index == chosen or other.name != mine.name or _contradicted_on(line, other):
            continue
        # Another variety the line fits as well: they differ on a point it
        # doesn't speak to.
        if any(point not in line.points for point in _contradicted_on(mine, other)):
            return None
    return chosen
