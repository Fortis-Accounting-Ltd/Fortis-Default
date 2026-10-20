"""Money helpers - every amount is a Decimal rounded to cents."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def to_dec(v) -> Decimal:
    """'1,234.50', '(1,234.50)', '1234.5-', '$12', 12.3, None, '-' -> Decimal (2 dp)."""
    if v is None:
        return ZERO
    if isinstance(v, Decimal):
        return q2(v)
    if isinstance(v, bool):
        raise ValueError(f"not an amount: {v!r}")
    if isinstance(v, int):
        return q2(Decimal(v))
    if isinstance(v, float):
        return q2(Decimal(repr(v)))
    s = str(v).strip().replace(",", "").replace("$", "").replace(" ", "")
    if s in ("", "-", "–", "—"):
        return ZERO
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("-") and len(s) > 1:
        neg, s = (not neg), s[:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not an amount: {v!r}") from None
    return q2(-d if neg else d)


def is_amount(v) -> bool:
    try:
        to_dec(v)
        return v not in (None, "")
    except ValueError:
        return False


def fmt(d: Decimal | None, blank_zero: bool = True) -> str:
    """1234.5 -> '1,234.50'; -12 -> '(12.00)'; 0 -> '' (or '0.00')."""
    if d is None:
        return ""
    d = q2(Decimal(d))
    if d == 0:
        return "" if blank_zero else "0.00"
    s = f"{abs(d):,.2f}"
    return f"({s})" if d < 0 else s


def as_str(d: Decimal) -> str:
    """Storage form: plain string with two decimals."""
    return f"{q2(Decimal(d)):.2f}"
