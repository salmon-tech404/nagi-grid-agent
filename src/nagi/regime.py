"""Calm/storm gate: the grid only runs while the market is going sideways."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from nagi import indicators as ind

# Kaufman (Trading Systems and Methods): ER >= 0.30 marks real directional movement.
STORM_ER = 0.30


@dataclass(frozen=True)
class Regime:
    state: str  # "calm" | "storm"
    er: float
    atr: float
    price: float
    reason: str

    @property
    def grid_allowed(self) -> bool:
        return self.state == "calm"


def classify(df: pd.DataFrame, er_period: int = 10, atr_period: int = 14) -> Regime:
    if len(df) < max(er_period, atr_period) + 1:
        raise ValueError(f"need at least {max(er_period, atr_period) + 1} bars, got {len(df)}")
    er = float(ind.efficiency_ratio(df["close"], er_period).iloc[-1])
    atr = float(ind.atr(df, atr_period).iloc[-1])
    price = float(df["close"].iloc[-1])
    if er >= STORM_ER:
        return Regime("storm", er, atr, price, f"ER {er:.2f} >= {STORM_ER}: trending, grid paused")
    return Regime("calm", er, atr, price, f"ER {er:.2f} < {STORM_ER}: sideways, grid active")
