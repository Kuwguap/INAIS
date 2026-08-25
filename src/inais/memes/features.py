"""The meme_signal head's context features — THE versioning contract.

The NN stores context_features as a plain real[] per example; per model name the length must
stay constant FOREVER: ragged arrays break training (nn.py np.array assembly), and a serving
vector of the wrong length silently returns None from nn.score. Therefore:

- meme_features() ALWAYS returns exactly FEATURE_LEN floats — missing data becomes a neutral
  default, never a shorter list.
- To change the features: bump MEME_FEATURES_VERSION, run
  `delete from nn_examples where model_name = 'meme_signal'`, and let the head retrain from
  newly-settled signals — the harvest filters meme_signals.feature_version, so old rows with
  the old layout are never mixed in.
- Harvest reads the STORED features off the signal row (never recomputes), so training and
  serving see byte-identical vectors.
"""

from __future__ import annotations

import math

from inais.integrations.dexscreener import Pair
from inais.integrations.rugcheck import RugReport

MEME_FEATURES_VERSION = 1
FEATURE_LEN = 12


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _log_scale(value: float | None, denom: float) -> float:
    """log10 squashed to [0,1]; None → 0 (absent reads as 'smallest')."""
    if value is None or value <= 0:
        return 0.0
    return min(1.0, math.log10(value + 1) / denom)


def meme_features(pair: Pair, report: RugReport | None,
                  age_min: float | None = None) -> list[float]:
    """Exactly FEATURE_LEN bounded floats describing one candidate at signal time."""
    total_txns = pair.buys_h1 + pair.sells_h1
    buy_pressure = pair.buys_h1 / total_txns if total_txns else 0.5
    top10 = report.top10_holder_pct if report and report.top10_holder_pct is not None else None
    lp_locked = bool(report and (report.lp_locked_pct or 0) >= 50)
    renounced = bool(report and not report.mint_authority_active
                     and not report.freeze_authority_active)
    return [
        _log_scale(pair.liquidity_usd, 6),                                   # 0: ≈$1M → 1.0
        _log_scale(pair.fdv_usd, 9),                                         # 1: ≈$1B → 1.0
        _log_scale(pair.volume_h24, 7),                                      # 2: ≈$10M → 1.0
        _log_scale(age_min, 4),                                              # 3: ≈1 week → 1.0
        _clamp((pair.change_m5 or 0.0) / 100),                               # 4
        _clamp((pair.change_h1 or 0.0) / 100),                               # 5
        _clamp((pair.change_h24 or 0.0) / 100),                              # 6
        _clamp(buy_pressure, 0.0, 1.0),                                      # 7
        _clamp(top10 / 100, 0.0, 1.0) if top10 is not None else 0.5,         # 8
        1.0 if lp_locked else 0.0,                                           # 9
        1.0 if renounced else 0.0,                                           # 10
        1.0 if (pair.has_socials or pair.has_website) else 0.0,              # 11
    ]
