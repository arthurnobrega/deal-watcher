"""Deciding whether a scraped offer really is the product we want.

This module is the safety valve of the whole project. A wrong match sends the
user to buy the wrong card, so every rule here is written to fail *closed*: when
the evidence is ambiguous, the offer is rejected. False negatives (a missed
deal) are cheap; false positives are not.

The rules are declared in config, not in code, so watching a CPU or an SSD later
needs no change here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from .config import MatchRules, ProductConfig
from .models import ProductOffer

#: Memory sizes as written in Brazilian store listings: "16GB", "16 GB",
#: "O16G" (ASUS model codes), "8G". Anchored on a non-digit to the left so
#: "2602" in "2602MHz" cannot be read as a memory size.
_MEMORY_RE = re.compile(r"(?<!\d)(\d{1,3})\s*g(?:b)?\b")

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Why an offer was accepted or rejected. The reason is logged, not guessed at."""

    matched: bool
    reason: str

    def __bool__(self) -> bool:
        return self.matched


def normalize(text: str) -> str:
    """Lowercase, strip accents, and collapse whitespace.

    Brazilian listings mix "Placa de Vídeo" and "Placa de Video"; patterns in
    config are written against this normalized form.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE_RE.sub(" ", without_accents.casefold()).strip()


def memory_sizes_gb(text: str) -> set[int]:
    """Every gigabyte figure mentioned in a product name.

    Returns *all* of them rather than guessing which one is the VRAM: the caller
    requires the wanted size to be present and the forbidden sizes to be absent,
    which is correct even when a name mentions several numbers.
    """
    return {int(match.group(1)) for match in _MEMORY_RE.finditer(normalize(text))}


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def match_offer(offer: ProductOffer, rules: MatchRules) -> MatchResult:
    """Check one offer against one product's rules."""
    if rules.require_available and not offer.available:
        return MatchResult(False, "out of stock")

    name = normalize(offer.name)

    for pattern in rules.reject_any:
        if _compile(pattern).search(name):
            return MatchResult(False, f"rejected by pattern {pattern!r}")

    for pattern in rules.require_all:
        if not _compile(pattern).search(name):
            return MatchResult(False, f"missing required pattern {pattern!r}")

    if rules.require_any and not any(_compile(p).search(name) for p in rules.require_any):
        return MatchResult(False, "matched none of require_any patterns")

    if rules.required_memory_gb is not None or rules.forbidden_memory_gb:
        sizes = memory_sizes_gb(offer.name)
        for forbidden in rules.forbidden_memory_gb:
            if forbidden in sizes:
                return MatchResult(False, f"mentions forbidden {forbidden}GB memory")
        if rules.required_memory_gb is not None and rules.required_memory_gb not in sizes:
            return MatchResult(False, f"no confident {rules.required_memory_gb}GB memory marker")

    return MatchResult(True, "matched")


def filter_offers(
    offers: tuple[ProductOffer, ...], product: ProductConfig
) -> tuple[tuple[ProductOffer, ...], tuple[tuple[ProductOffer, MatchResult], ...]]:
    """Split offers into matches and (offer, reason) pairs for everything rejected."""
    matched: list[ProductOffer] = []
    rejected: list[tuple[ProductOffer, MatchResult]] = []
    for offer in offers:
        result = match_offer(offer, product.match)
        if result:
            matched.append(offer)
        else:
            rejected.append((offer, result))
    return tuple(matched), tuple(rejected)
