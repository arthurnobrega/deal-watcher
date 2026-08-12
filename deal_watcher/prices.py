"""Parsing and formatting Brazilian prices.

Store markup writes prices as ``R$ 3.199,99``: dot groups thousands, comma is
the decimal separator. Getting this backwards would turn R$ 3.199,99 into
R$ 3.19, so parsing is deliberately explicit rather than a naive float cast.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_PRICE_RE = re.compile(r"\d[\d.,\s]*\d|\d")
_NON_NUMERIC_RE = re.compile(r"[^\d.,]")


class PriceParseError(ValueError):
    """Raised when a string contains no price we can read with confidence."""


def parse_brl(text: str) -> Decimal:
    """Parse a Brazilian-formatted price string into a :class:`Decimal`.

    Handles ``R$ 3.199,99``, ``3199,99``, ``3.199``, and ``3199.99`` (already
    machine-formatted). Raises :class:`PriceParseError` rather than guessing.
    """
    if not text:
        raise PriceParseError("empty price string")

    match = _PRICE_RE.search(text.replace("\xa0", " "))
    if match is None:
        raise PriceParseError(f"no digits in price string: {text!r}")

    digits = _NON_NUMERIC_RE.sub("", match.group(0))
    if "," in digits:
        # Brazilian format: dots group thousands, the comma is the decimal point.
        digits = digits.replace(".", "").replace(",", ".")
    elif digits.count(".") > 1:
        # "3.199.999" -- dots can only be thousand separators here.
        digits = digits.replace(".", "")
    elif "." in digits:
        integer, _, fraction = digits.partition(".")
        # A 3-digit group after a single dot is a thousands separator ("3.199"),
        # anything else is a decimal point ("3199.99").
        if len(fraction) == 3 and len(integer) <= 3:
            digits = integer + fraction

    try:
        value = Decimal(digits)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise PriceParseError(f"cannot parse price: {text!r}") from exc

    if value <= 0:
        raise PriceParseError(f"non-positive price: {text!r}")
    return value


def to_decimal(value: object) -> Decimal:
    """Coerce a JSON-sourced number (int/float/str) into a Decimal price."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return parse_brl(value)
    raise PriceParseError(f"unsupported price value: {value!r}")


def format_brl(value: Decimal) -> str:
    """Render a Decimal as ``R$ 3.199,99``."""
    quantized = value.quantize(Decimal("0.01"))
    integer, _, fraction = f"{quantized:.2f}".partition(".")
    negative = integer.startswith("-")
    integer = integer.lstrip("-")
    grouped = f"{int(integer):,}".replace(",", ".")
    return f"R$ {'-' if negative else ''}{grouped},{fraction}"
