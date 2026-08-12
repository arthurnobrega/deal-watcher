"""Product matching -- the tests that stop a wrong purchase alert.

Every name below is either taken verbatim from a Brazilian store listing or
built to mimic one.
"""

from __future__ import annotations

import pytest

from deal_watcher.config import MatchRules
from deal_watcher.matching import match_offer, memory_sizes_gb, normalize

from .conftest import make_offer

ACCEPTED = [
    "Placa de Vídeo ASUS DUAL RTX 5060 TI O16G NVIDIA GeForce, 16GB GDDR7, 128 bits",
    "Placa de Video MSI GeForce RTX 5060 TI 16Gb Ventus OC Black Plus 2X 128 Bits",
    "Placa de Vídeo Palit GeForce RTX5060Ti INFINITY 3 NVIDIA GeForce, 16GB GDDR7",
    "Placa de Video Gigabyte GeForce RTX 5060 Ti Windforce 16GB GDDR7 256-bit",
    "PLACA DE VIDEO ASUS PRIME RTX 5060 TI 16G GDDR7",
]

REJECTED = [
    # The expensive mistake: same model, half the memory.
    ("Placa de Vídeo RTX 5060 Ti GAMING OC 8G Gigabyte, 8GB GDDR7", "8GB variant"),
    ("Placa De Vídeo MSI RTX 5060 Ti Ventus 2X OC Plus, 8GB, GDDR7", "8GB variant"),
    # Adjacent models.
    ("Placa de Vídeo MSI GeForce RTX 5060 Shadow 2X OC, 8GB, GDDR7", "plain 5060"),
    ("Placa de Vídeo Galax RTX 5060 1-Click OC, 16GB GDDR7", "plain 5060, right memory"),
    ("Placa de Vídeo ASUS TUF RTX 5070 Ti OC 16GB GDDR7", "different model"),
    ("Placa de Vídeo Galax RTX 3060 Ti SG 1-Click OC Plus, 16GB", "old generation"),
    # Not a bare card.
    ("PC Gamer Ryzen 7 com RTX 5060 Ti 16GB e 32GB RAM", "complete PC"),
    ("Notebook Gamer com RTX 5060 Ti 16GB", "notebook"),
    ("Suporte Anti Sag para Placa de Vídeo RTX 5060 Ti 16GB", "accessory"),
    ("Cabo Adaptador 12VHPWR para RTX 5060 Ti 16GB", "accessory"),
    # Condition.
    ("Placa de Vídeo ASUS RTX 5060 Ti 16GB GDDR7 - USADO", "used"),
    ("Placa de Vídeo ASUS RTX 5060 Ti 16GB GDDR7 Open Box", "open box"),
    # No memory claim at all: not confident, so no alert.
    ("Placa de Vídeo Colorful iGame RTX 5060 Ti Ultra W DUO OC", "memory not stated"),
]


@pytest.mark.parametrize("name", ACCEPTED)
def test_accepts_real_16gb_cards(name: str, rtx_match_rules: MatchRules) -> None:
    result = match_offer(make_offer(name=name), rtx_match_rules)
    assert result, f"should have matched: {name} ({result.reason})"


@pytest.mark.parametrize(("name", "why"), REJECTED, ids=[why for _, why in REJECTED])
def test_rejects_everything_else(name: str, why: str, rtx_match_rules: MatchRules) -> None:
    result = match_offer(make_offer(name=name), rtx_match_rules)
    assert not result, f"should have rejected ({why}): {name}"
    assert result.reason


def test_out_of_stock_never_matches(rtx_match_rules: MatchRules) -> None:
    offer = make_offer(name=ACCEPTED[0], available=False)
    result = match_offer(offer, rtx_match_rules)
    assert not result
    assert "stock" in result.reason


def test_availability_can_be_ignored_when_configured() -> None:
    rules = MatchRules(require_available=False, require_all=(r"rtx\s*5060\s*ti",))
    assert match_offer(make_offer(name=ACCEPTED[0], available=False), rules)


class TestMemoryDetection:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("RTX 5060 Ti 16GB GDDR7", {16}),
            ("RTX 5060 Ti 16 GB GDDR7", {16}),
            ("ASUS DUAL RTX 5060 TI O16G", {16}),
            ("RTX 5060 Ti GAMING OC 8G", {8}),
            ("RTX 5060 Ti, 8GB, GDDR7, 128 bits", {8}),
            ("PC Gamer RTX 5060 Ti 16GB com 32GB RAM", {16, 32}),
            # The numbers that must not be read as memory.
            ("RTX 5060 Ti 16GB GDDR7, 2602MHz, 128 bits", {16}),
            ("RTX 5060 Ti 16GB PCIe 5.0 x8", {16}),
        ],
    )
    def test_memory_sizes(self, name: str, expected: set[int]) -> None:
        assert memory_sizes_gb(name) == expected

    def test_a_card_claiming_both_sizes_is_rejected(self, rtx_match_rules: MatchRules) -> None:
        # Ambiguous listings are exactly where a false positive would come from.
        offer = make_offer(name="RTX 5060 Ti 16GB / 8GB GDDR7")
        result = match_offer(offer, rtx_match_rules)
        assert not result
        assert "8GB" in result.reason


class TestNormalize:
    def test_strips_accents_and_case(self) -> None:
        assert normalize("Placa de Vídeo ASUS") == "placa de video asus"

    def test_collapses_whitespace(self) -> None:
        assert normalize("  RTX   5060\n Ti  ") == "rtx 5060 ti"


def test_require_any_is_an_or(rtx_match_rules: MatchRules) -> None:
    rules = rtx_match_rules.model_copy(update={"require_any": ("asus", "msi")})
    assert match_offer(make_offer(name="Placa ASUS RTX 5060 Ti 16GB"), rules)
    assert not match_offer(make_offer(name="Placa Palit RTX 5060 Ti 16GB"), rules)
