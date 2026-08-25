"""Deep-link builders — the ONLY place venue URLs exist, and it is pure string work.

This module is the security boundary between scraped strings and tappable buttons: every
builder validates its address against the base58 alphabet and returns None otherwise, so a
token name or description can never smuggle an arbitrary URL into a Telegram button. Zero I/O
here — no aiohttp import, ever (a wiring test asserts both properties).
"""

from __future__ import annotations

import re

# Solana addresses: base58, no 0 O I l, 32-44 chars.
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def valid_address(s: str) -> bool:
    return bool(BASE58_RE.match(s or ""))


def chart_url(pair_address: str) -> str | None:
    if not valid_address(pair_address):
        return None
    return f"https://dexscreener.com/solana/{pair_address}"


def jupiter_url(mint: str) -> str | None:
    if not valid_address(mint):
        return None
    return f"https://jup.ag/swap/SOL-{mint}"


def photon_url(pair_address: str) -> str | None:
    if not valid_address(pair_address):
        return None
    return f"https://photon-sol.tinyastro.io/en/lp/{pair_address}"  # VERIFY at implementation
