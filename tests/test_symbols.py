"""Coin-spelling normalization for Hyperliquid.

Hyperliquid is case-sensitive on coin names: its 1000x contracts carry a
LOWERCASE `k` prefix (kPEPE, kSHIB, kBONK, kLUNC, kFLOKI, kDOGS, kNEIRO) while
the same universe also ships genuinely upper-case names that start with K
(KAS, KAITO). That makes the spelling undecidable from the string alone, which
is why the venue universe is the authority and the lexical rule must never
destroy case it was handed.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest

from hypertrade.routes.hyperliquid_errors import HyperliquidValidationError
from hypertrade.routes.symbols import canonical_symbol, normalize_symbol

# A faithful slice of the real main-perp universe: two k-contracts and the two
# genuinely upper-case K names that make a lexical rule impossible.
UNIVERSE = ["BTC", "LINK", "kFLOKI", "kNEIRO", "KAS", "KAITO"]


# ===================================================================
# normalize_symbol — the lexical fallback used where the venue is out of reach
# ===================================================================


def test_lowercase_free_text_is_uppercased():
    """Free text from an alert ('link') still reaches the venue spelling."""
    assert normalize_symbol("link") == "LINK"


def test_mixed_case_base_is_preserved():
    """A caller that already sent the venue spelling must get it back intact.

    This is the AZTEC/kFLOKI production failure: the desk canonicalised the leg
    to 'kFLOKI' and the daemon upper-cased it to 'KFLOKI', which does not exist.
    """
    assert normalize_symbol("kFLOKI") == "kFLOKI"


def test_dex_qualified_base_passes_through_verbatim():
    """HIP-3 dex prefixes are case-sensitive too ('xyz:' must not become 'XYZ:')."""
    assert normalize_symbol("xyz:KR200") == "xyz:KR200"


def test_surrounding_whitespace_is_stripped():
    """A padded base must not reach the exchange with its padding."""
    assert normalize_symbol("  btc  ") == "BTC"


# ===================================================================
# canonical_symbol — the venue universe is the authority
# ===================================================================


def test_corrupted_k_contract_is_resolved_to_the_venue_spelling():
    """'KFLOKI' does not exist on Hyperliquid; 'kFLOKI' does."""
    assert canonical_symbol("KFLOKI", UNIVERSE) == "kFLOKI"


def test_lowercase_free_text_resolves_to_the_venue_spelling():
    """The lexical rule cannot fix 'kfloki'; the universe can."""
    assert canonical_symbol("kfloki", UNIVERSE) == "kFLOKI"


def test_genuine_uppercase_k_name_is_left_alone():
    """KAITO/KAS are really upper-case — resolving must not invent a k-prefix."""
    assert canonical_symbol("KAITO", UNIVERSE) == "KAITO"
    assert canonical_symbol("KAS", UNIVERSE) == "KAS"


def test_exact_hit_wins_over_a_case_folded_sibling():
    """When both spellings exist, the caller's exact spelling is never swapped.

    Guards the desk's own hard-won lesson: a purely case-folded map silently
    substitutes one real market for another. Failing visibly beats being right
    about the wrong asset. Non-vacuous because the ambiguous case raises: delete
    the exact-hit branch and these two calls blow up instead of resolving.
    """
    universe = ["kFLOKI", "KFLOKI"]
    assert canonical_symbol("KFLOKI", universe) == "KFLOKI"
    assert canonical_symbol("kFLOKI", universe) == "kFLOKI"


def test_ambiguous_universe_refuses_to_guess():
    """Two markets differing only by case, and no exact hit, is not resolvable.

    Guessing here would place a real order on a real market that the caller did
    not ask for — the one outcome strictly worse than a rejected order. Raise so
    the desk gets a terminal reject and pauses, instead of a silent wrong fill.
    Hyperliquid has no such collision today; this is the guard for the day it
    lists one.
    """
    with pytest.raises(HyperliquidValidationError):
        canonical_symbol("kfloki", ["kFLOKI", "KFLOKI"])


def test_absent_symbol_falls_back_to_the_lexical_rule():
    """An unknown coin keeps today's behaviour: the exchange rejects it by name."""
    assert canonical_symbol("nosuch", UNIVERSE) == "NOSUCH"
    assert canonical_symbol("xyz:KR200", UNIVERSE) == "xyz:KR200"


def test_dex_qualified_symbol_resolves_within_its_own_universe():
    """Dex coins canonicalise against the dex universe, prefix preserved."""
    assert canonical_symbol("xyz:kr200", ["xyz:KR200"]) == "xyz:KR200"
