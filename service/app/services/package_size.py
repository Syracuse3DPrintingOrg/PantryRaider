"""How much one package holds: the 64 fl oz in a bottle of milk. Pure.

An item's quantity and unit say how many there are (2 Bottles). Its package
size says what each one holds, so the pantry can show "2 x 64 fl oz" instead of
"2 Bottles". A size is always a weight or a volume with a fixed size
(_UNIT_SIZES in grocy.py), held as (amount, Grocy unit name), for example
(64.0, "Fluid ounce"). Grocy keeps it as a per-product unit conversion
(1 Bottle = 64 Fluid ounces), which recipe matching already reads.

Sizes come from three places: the AI reply's own package_size field, the
Open Food Facts "quantity" text on a barcode scan ("1.89 l", "6 x 330 ml"),
and a size written into the name ("Whole milk 64 oz"), which is how a typed
or dictated list usually carries it.
"""
from __future__ import annotations

import math
import re

from ..models.food import FoodItem
from .grocy import _KNOWN_UNITS, _UNIT_SIZES, _unit_key, unit_names

# The biggest package believed, in grams or milliliters: 100 kg or 100 L is
# already far past anything on a pantry shelf.
_MAX_BASE = 100_000.0

# One letter spellings that read as something else next to a number: "2 c"
# could be a count, and "1 t" or "1 T" a teaspoon or a tablespoon in a recipe,
# never on a package.
_AMBIGUOUS = {"c", "t", "T"}


def _measure_spellings() -> list[str]:
    """Every spelling of a weight or volume unit, longest first, so "fl oz"
    is tried before "oz" and "kg" before "g"."""
    out = set()
    for name, plural, aliases in _KNOWN_UNITS:
        kind = _UNIT_SIZES.get(name, ("count",))[0]
        if kind == "count":
            continue
        for spelling in (name, plural, *aliases):
            if spelling not in _AMBIGUOUS:
                out.add(spelling.lower())
    out.update({"fluid ounce", "fluid ounces", "fl ounce", "fl ounces"})
    return sorted(out, key=len, reverse=True)


def _spelling_pattern(spelling: str) -> str:
    # "fl oz" also matches "fl. oz.", "fl.oz" and "floz".
    return r"\.?\s*".join(re.escape(word) for word in spelling.split())


_NUMBER = r"\d+(?:[.,]\d+)?(?:\s+\d+/\d+)?|\d+/\d+"
_SIZE_RE = re.compile(
    r"(?<![\w.])"
    r"(?:(?P<mult>\d+)\s*[x×*]\s*)?"
    rf"(?P<num>{_NUMBER})\s*"
    rf"(?P<unit>{'|'.join(_spelling_pattern(s) for s in _measure_spellings())})"
    r"(?!\w)\.?",
    re.IGNORECASE,
)

# Pounds and ounces together, as US labels print anything past a pound:
# "1 lb 5 oz" is 21 ounces.
_POUNDS_OUNCES_RE = re.compile(
    r"(?<![\w.])(?P<lb>\d+)\s*(?:lbs?|pounds?)\.?,?\s*"
    r"(?P<oz>\d+(?:[.,]\d+)?)\s*(?:oz|ounces?)(?!\w)\.?",
    re.IGNORECASE,
)

# A size said in words: "half gallon", "half a gallon", "a quart", "two
# pounds". Only with a unit spelled out in full, so a one-letter unit next to
# a word ("Grade A L...") is never read as a size.
_WORD_NUMBERS = {"half": 0.5, "quarter": 0.25, "a": 1, "an": 1, "one": 1,
                 "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_WORD_UNITS = ("gallon", "gallons", "quart", "quarts", "pint", "pints", "liter",
               "liters", "litre", "litres", "pound", "pounds", "ounce", "ounces",
               "fluid ounce", "fluid ounces", "kilogram", "kilograms", "gram",
               "grams", "milliliter", "milliliters")
_WORD_SIZE_RE = re.compile(
    # Not the second half of "half and half", which is a cream.
    rf"(?<![\w.])(?<!and )(?<!and-)(?P<word>{'|'.join(_WORD_NUMBERS)})(?:[\s-]+an?)?[\s-]+"
    rf"(?P<unit>{'|'.join(sorted(_WORD_UNITS, key=len, reverse=True))})(?!\w)",
    re.IGNORECASE,
)


def _to_number(text: str) -> float | None:
    """64, 16.9, 1,5 (a decimal comma), 1/2 and 1 1/2 as a float, else None."""
    text = text.strip().replace(",", ".")
    total = 0.0
    for part in text.split():
        if "/" in part:
            top, _, bottom = part.partition("/")
            try:
                if float(bottom) == 0:
                    return None
                total += float(top) / float(bottom)
            except ValueError:
                return None
        else:
            try:
                total += float(part)
            except ValueError:
                return None
    return total if math.isfinite(total) else None


def _from_match(match: re.Match) -> tuple[float, str] | None:
    amount = _to_number(match.group("num"))
    if amount is None:
        return None
    if match.group("mult"):
        amount *= int(match.group("mult"))
    unit = unit_names(match.group("unit"))[0]
    if unit not in _UNIT_SIZES or _UNIT_SIZES[unit][0] == "count":
        return None
    if not 0 < amount * _UNIT_SIZES[unit][1] <= _MAX_BASE:
        return None
    return round(amount, 4), unit


def parse_size(text) -> tuple[float, str] | None:
    """The first package size written in ``text``, as (amount, Grocy unit).

    "64 oz" is (64.0, "Ounce"), "16.9 fl. oz (500 ml)" is (16.9, "Fluid
    ounce"), and a multipack "6 x 330 ml" is the whole pack, (1980.0,
    "Milliliter"), since one scan of it is one item. None when there is no
    weight or volume in it, or the amount is not believable.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    found = _sizes(text)
    if found:
        return found[0][2]
    # A unit typed on its own in a size box: "gallon" is one gallon.
    word = text.strip().lower().rstrip(".")
    if word in _WORD_UNITS or word in {"oz", "lb", "lbs", "fl oz", "kg", "ml"}:
        return parse_size(f"1 {word}")
    return None


def _sizes(text: str) -> list[tuple[int, int, tuple[float, str]]]:
    """Every believable size in ``text`` as (start, end, size), in order.
    A "1 lb 5 oz" is one size, never a pound and then five ounces."""
    found = []
    for match in _POUNDS_OUNCES_RE.finditer(text):
        ounces = int(match.group("lb")) * 16 + float(match.group("oz").replace(",", "."))
        if 0 < ounces * _UNIT_SIZES["Ounce"][1] <= _MAX_BASE:
            found.append((match.start(), match.end(), (round(ounces, 4), "Ounce")))
    for match in _WORD_SIZE_RE.finditer(text):
        unit = unit_names(match.group("unit"))[0]
        amount = _WORD_NUMBERS[match.group("word").lower()]
        if unit in _UNIT_SIZES and _UNIT_SIZES[unit][0] != "count":
            found.append((match.start(), match.end(), (float(amount), unit)))
    for match in _SIZE_RE.finditer(text):
        if any(start < match.end() and match.start() < end for start, end, _ in found):
            continue
        size = _from_match(match)
        if size:
            found.append((match.start(), match.end(), size))
    return sorted(found)


def split_name(name: str) -> tuple[str, tuple[float, str] | None]:
    """``name`` without the package size written in it, and that size.

    "Whole milk, 64 oz" is ("Whole milk", (64.0, "Ounce")) and "Rao's
    marinara (24 oz)" is ("Rao's marinara", (24.0, "Ounce")). The last size
    in the name is the one taken. A name that would be left empty, or that
    holds no size, comes back as it was, with None.
    """
    if not isinstance(name, str):
        return name, None
    found = _sizes(name)
    if not found:
        return name, None
    start, end, size = found[-1]
    rest = name[:start] + " " + name[end:]
    rest = re.sub(r"\(\s*\)|\[\s*\]", " ", rest)
    rest = " ".join(rest.split())
    rest = re.sub(r"\s+([,;)])", r"\1", rest)
    rest = re.sub(r"^[\s,;:/\-\u2013\u2014]+|[\s,;:/\-\u2013\u2014]+$", "", rest)
    if not re.search(r"[^\W\d_]", rest):
        return name, None
    return rest, size


def not_a_size(typed: str) -> str:
    """What to tell someone whose typed size names no weight or volume."""
    return (f"{typed.strip()!r} is not a size. Give a weight or a volume, "
            "like 64 oz, 500 g, 1.5 L or half gallon.")


def normalize_size(amount, unit) -> tuple[float, str] | None:
    """A size given as separate amount and unit fields, checked. Pure.

    The amount may be a number or text, and text may carry its own unit
    ("64 oz" with no unit field). None when the two do not make a weight or
    volume believable as one package.
    """
    if isinstance(amount, str):
        text = amount if not unit else f"{amount} {unit}"
        return parse_size(text)
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if not isinstance(unit, str) or not _unit_key(unit):
        return None
    return parse_size(f"{float(amount):g} {unit}")


# Liquids poured by volume: a bare "oz" on one of these means fluid ounces,
# which convert to the cups a recipe asks for. Anything thick or solid is sold
# by weight even in a jar (marinara, honey, yogurt), so it is left as weight.
_POURED = re.compile(
    r"\b(?:milk|cream|half[\s-]and[\s-]half|juice|water|soda|seltzer|broth|"
    r"stock|oil|vinegar|wine|beer|creamer|buttermilk|kombucha|lemonade|cider)\b",
    re.IGNORECASE)
_NOT_POURED = re.compile(
    r"\b(?:chocolate|cheese|sour|ice|whipped|powder|powdered|spray|condensed|"
    r"soup|bars?|mix|cakes?|cookies?|candy|chips|crackers|bouillon|cubes?|"
    r"baking|tartar)\b|\bcream of\b|\bin\s+(?:\w+\s+)?(?:oil|water|juice|syrup)\b",
    re.IGNORECASE)


# How many come in one package: "12 ct" eggs, a "12PK" of soda, "18 count",
# a dozen. Counted out into single ones, as "1 dozen" already is (a dozen eggs
# is 12 Pieces, so using one leaves 11). Labels give a size two ways: a pack
# of drinks gives each container's ("12PK 12 FL OZ" is twelve 12 fl oz cans),
# while a count gives the whole package's ("10 ct 17.5 oz" tortillas weigh
# 17.5 oz together).
_COUNT_RE = re.compile(
    r"(?<![\w.])(?:(?P<n>\d+)\s*-?\s*(?:(?P<pack>pk|pack|cans?|bottles?)|ct|count|pcs?|pieces?)"
    r"|(?P<half>half\s+(?:a\s+)?dozen)|(?:(?P<dn>\d+)\s*)?(?P<dz>dozen|doz))\b\.?",
    re.IGNORECASE)


def parse_count(text) -> int | None:
    """How many single ones a package holds, from "12 ct", "12PK", "18
    count", "dozen", "2 dozen" or "half dozen", else None. Pure."""
    found = _count_match(text)
    return found[0] if found else None


def size_is_for_each(text) -> bool:
    """True when the count in ``text`` is a pack of containers, so a size
    printed with it is each one's ("12PK 12 FL OZ"); False for a count of
    pieces, whose size is the whole package's ("10 ct 17.5 oz"). Pure."""
    found = _count_match(text)
    return bool(found and found[1])


def _count_match(text) -> tuple[int, bool] | None:
    """(count, whether it is a pack of containers), or None."""
    if not isinstance(text, str):
        return None
    match = _COUNT_RE.search(text)
    if not match:
        return None
    if match.group("half"):
        count = 6
    elif match.group("dz"):
        count = 12 * int(match.group("dn") or 1)
    else:
        count = int(match.group("n"))
    return (count, bool(match.group("pack"))) if 2 <= count <= 500 else None


def _strip_count(name: str) -> str:
    """``name`` without the count written in it ("COCA COLA 12PK")."""
    rest = _COUNT_RE.sub(" ", name)
    rest = " ".join(rest.split()).strip(" ,;:-")
    return rest if re.search(r"[^\W\d_]", rest) else name


def is_poured(name: str) -> bool:
    """True when ``name`` is a liquid sold by volume (milk, olive oil,
    chicken broth), not a food sold by weight that only mentions one (milk
    chocolate, cream cheese, tuna in oil). Pure."""
    return bool(_POURED.search(name or "")) and not _NOT_POURED.search(name or "")


def apply_package_size(item: FoodItem) -> FoodItem:
    """``item`` with its package size worked out, or unchanged. Pure.

    A size already on the item is checked and put in Grocy's spelling. With
    none, a size written in the name is taken out of it ("Whole milk 64 oz"
    becomes "Whole milk", 64 Ounces each), then one in the notes after
    "Size:". When the model gave the size as the amount itself (64 Ounces of
    something whose package holds 64 Ounces), the item is one package.
    """
    update: dict = {}
    # A count per package is counted out: 2 cartons of "12 ct" eggs are 24.
    count, each = item.package_count, item.package_count_each
    if not count and parse_count(item.name):
        count, each = parse_count(item.name), size_is_for_each(item.name)
    if count and _is_count_unit(item.unit):
        if not math.isclose(item.quantity, count):
            update.update(quantity=item.quantity * count)
        update.update(unit="Piece", package_count=None, package_count_each=None)
        if parse_count(item.name):
            update["name"] = _strip_count(item.name)
        if item.package_size and not each:
            # The whole package's weight, shared out: 17.5 oz of 10 tortillas.
            update["package_size"] = round(item.package_size / count, 4)
        item = item.model_copy(update=update)
        update = {}
    size = None
    if item.package_size is not None or item.package_unit:
        size = normalize_size(item.package_size, item.package_unit)
    name, from_name = split_name(item.name)
    if size is None and from_name:
        size = from_name
    if from_name and size == from_name:
        # A different size in the name than the one given is left in the
        # name: it may be telling the two packages apart.
        update["name"] = name
    if size is None and item.notes:
        noted = re.search(r"\bsize\s*[:=-]?\s*(.+)", item.notes, re.IGNORECASE)
        if noted:
            size = parse_size(noted.group(1))
    if size is None:
        if item.package_size is not None or item.package_unit:
            update.update(package_size=None, package_unit=None)
        return item.model_copy(update=update) if update else item
    if size[1] == "Ounce" and is_poured(update.get("name", item.name)):
        size = (size[0], "Fluid ounce")
    update.update(package_size=size[0], package_unit=size[1])
    packages = _package_count(item.quantity, item.unit, size)
    if packages is not None:
        update.update(quantity=packages, unit="item")
    return item.model_copy(update=update)


def _is_count_unit(unit: str) -> bool:
    """True when ``unit`` counts things (item, Piece, carton), not a weight
    or a volume."""
    return _UNIT_SIZES.get(unit_names(unit or "")[0], ("count",))[0] == "count"


def _package_count(quantity: float, unit: str, size: tuple[float, str]) -> float | None:
    """How many packages a weight or volume amount stands for, given what one
    holds. None when the amount is a count already, or a weighed amount that
    is no whole number of packages. Pure.

    A model often copies the size's unit into the unit field: a jar of 3.25 oz
    sugar comes back as quantity 1, unit oz, so the 1 counts jars. An amount
    that is the size itself, or a whole number of sizes, is that many
    packages: 10 oz of a 10 fl oz bottle is one bottle ("oz" read off a
    bottle often means fluid ounces) and 48 oz of a 24 oz jar is two.
    """
    unit = unit_names(unit)[0]
    kind = _UNIT_SIZES.get(unit, ("count",))[0]
    if kind == "count" or not quantity or quantity <= 0:
        return None
    if {unit, size[1]} == {"Ounce", "Fluid ounce"}:
        ratio = quantity / size[0]
    elif kind == _UNIT_SIZES[size[1]][0]:
        ratio = quantity * _UNIT_SIZES[unit][1] / (size[0] * _UNIT_SIZES[size[1]][1])
    else:
        ratio = None
    if ratio is not None and ratio >= 0.99 and math.isclose(ratio, round(ratio), rel_tol=0.01):
        return float(round(ratio))
    if float(quantity).is_integer():
        return float(quantity)
    return None
