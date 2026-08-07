"""Single authority for Hyperliquid coin spelling.

Hyperliquid is case-sensitive on coin names. Its 1000x contracts carry a
LOWERCASE `k` prefix (kPEPE, kSHIB, kBONK, kLUNC, kFLOKI, kDOGS, kNEIRO), and
the very same universe ships genuinely upper-case names that also start with K
(KAS, KAITO). No lexical rule can tell a corrupt `KFLOKI` from a correct
`KAITO`, so the venue universe is the only authority — that is
`canonical_symbol`, and every path that is about to reach the exchange should
use it.

`normalize_symbol` is the fallback for boundaries that cannot reach the venue
(the HTTP handler resolves a payload before any client exists). It upper-cases
free text but never destroys case a caller already supplied, so a client that
sends the venue spelling gets it through intact.
"""

from __future__ import annotations

from typing import Iterable

from .hyperliquid_errors import HyperliquidValidationError


def normalize_symbol(raw: str) -> str:
    """Best-effort spelling for a coin, without consulting the venue.

    Dex-qualified (HIP-3) coins pass through verbatim: the `xyz:` prefix is
    case-sensitive and upper-casing corrupts it to `XYZ:`. A plain base that is
    entirely lower-case is treated as free text and upper-cased; anything that
    already carries case information is left exactly as supplied.
    """
    symbol = str(raw).strip()
    if ":" in symbol:
        return symbol
    if symbol == symbol.lower():
        return symbol.upper()
    return symbol


def canonical_symbol(raw: str, universe_names: Iterable[str]) -> str:
    """Resolve `raw` to the venue's exact spelling using its universe.

    An exact hit always wins, so a caller's precise spelling is never swapped
    for a case-folded sibling — silently substituting one real market for
    another is worse than failing. Falls back to `normalize_symbol` when the
    coin is absent, so the exchange rejects it by name exactly as it does today.

    Raises when two markets differ only by case and neither is an exact hit:
    picking one would place a real order on a market the caller never asked for.
    Hyperliquid has no such collision today.
    """
    symbol = str(raw).strip()
    names = list(universe_names)

    if symbol in names:
        return symbol

    folded = symbol.casefold()
    matches = [name for name in names if name.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise HyperliquidValidationError(
            f"Ambiguous symbol {symbol!r}: the venue lists "
            f"{sorted(matches)!r}, which differ only by case — refusing to guess"
        )

    return normalize_symbol(symbol)
