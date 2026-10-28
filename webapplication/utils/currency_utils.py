"""Money helpers.

All business-logic money is ``Decimal`` quantized to 2 places (paise).  Binary
floats are never used for arithmetic.  Amounts with more than 2 decimal places
are rejected rather than silently rounded.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("999999999999.99")


def _to_decimal(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number.")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"{field} must be a finite number.")
        d = Decimal(repr(value))
    else:
        text = str(value).strip()
        for token in ("\u20b9", "Rs.", "Rs", "INR", ",", " "):
            text = text.replace(token, "")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{field} must be a valid number.") from None
    if not d.is_finite():
        raise ValueError(f"{field} must be a finite number.")
    return d


def money(value, field="Amount", allow_zero=True, allow_negative=False, blank_as_zero=True):
    """Parse user/service input into a 2-place Decimal or raise ValueError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if blank_as_zero:
            value = "0"
        else:
            raise ValueError(f"{field} is required.")
    d = _to_decimal(value, field)
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if q != d:
        raise ValueError(f"{field} must have at most 2 decimal places.")
    if abs(q) > MAX_AMOUNT:
        raise ValueError(f"{field} is too large.")
    if q < 0 and not allow_negative:
        raise ValueError(f"{field} cannot be negative.")
    if q == 0 and not allow_zero:
        raise ValueError(f"{field} must be greater than zero.")
    return q


def parse_stored(value):
    """Lenient parse for values read back from storage (never raises)."""
    if value is None or value == "":
        return ZERO
    try:
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return ZERO
            return Decimal(repr(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ZERO


def dec_str(value):
    """Canonical storage string for a money value, e.g. '1234.50'."""
    return f"{parse_stored(value):.2f}"


def format_money(value, symbol="\u20b9", indian=True):
    d = parse_stored(value)
    neg = d < 0
    whole, frac = f"{abs(d):.2f}".split(".")
    if indian and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    else:
        whole = f"{int(whole):,}"
    return f"{'-' if neg else ''}{symbol}{whole}.{frac}"
