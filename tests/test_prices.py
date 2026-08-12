"""Price parsing. Getting BRL formatting wrong is a whole order of magnitude."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deal_watcher.prices import PriceParseError, format_brl, parse_brl, to_decimal


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("R$ 3.199,99", "3199.99"),
        ("R$3.199,99", "3199.99"),
        ("3.199,99", "3199.99"),
        ("3199,99", "3199.99"),
        ("R$ 3.199", "3199"),
        ("R$ 12.345.678,90", "12345678.90"),
        ("3199.99", "3199.99"),  # already machine-formatted
        ("R$\xa02.899,00", "2899.00"),  # non-breaking space from HTML
        ("por: R$ 2.349,99 à vista no Pix", "2349.99"),
        ("R$ 999,00", "999.00"),
    ],
)
def test_parse_brl(text: str, expected: str) -> None:
    assert parse_brl(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "R$", "grátis", "sob consulta", "R$ 0,00"])
def test_parse_brl_rejects_junk(text: str) -> None:
    with pytest.raises(PriceParseError):
        parse_brl(text)


def test_thousands_separator_is_not_a_decimal_point() -> None:
    # The bug this guards: reading "R$ 3.199" as three reais and nineteen cents.
    assert parse_brl("R$ 3.199") == Decimal("3199")
    assert parse_brl("R$ 3.199") > Decimal("3000")


def test_to_decimal_accepts_json_number_types() -> None:
    assert to_decimal(3199) == Decimal("3199")
    assert to_decimal(3199.99) == Decimal("3199.99")
    assert to_decimal(Decimal("3199.99")) == Decimal("3199.99")
    assert to_decimal("R$ 3.199,99") == Decimal("3199.99")


def test_to_decimal_rejects_unsupported() -> None:
    with pytest.raises(PriceParseError):
        to_decimal(None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3199.99", "R$ 3.199,99"),
        ("3300", "R$ 3.300,00"),
        ("100.01", "R$ 100,01"),
        ("999", "R$ 999,00"),
        ("1234567.5", "R$ 1.234.567,50"),
    ],
)
def test_format_brl(value: str, expected: str) -> None:
    assert format_brl(Decimal(value)) == expected


def test_format_then_parse_round_trips() -> None:
    original = Decimal("3199.99")
    assert parse_brl(format_brl(original)) == original
