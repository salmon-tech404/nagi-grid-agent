"""ATR-spaced grid: buy when price falls through a level, sell when it rises through one.

Price leaving the outer band ends the session (stop barrier); the job's
expiry is the time barrier (Triple Barrier, AFML ch. 3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GridPlan:
    center: float
    step: float
    levels: tuple[float, ...]
    lower_stop: float
    upper_stop: float


@dataclass(frozen=True)
class Signal:
    side: str  # "buy" | "sell" | "stop"
    level: float


def build_plan(
    center: float, atr: float, levels_per_side: int = 4, step_atr: float = 0.5, stop_atr: float = 3.0
) -> GridPlan:
    if center <= 0 or atr <= 0:
        raise ValueError("center and atr must be positive")
    if levels_per_side < 1:
        raise ValueError("levels_per_side must be >= 1")
    step = atr * step_atr
    levels = tuple(center + step * i for i in range(-levels_per_side, levels_per_side + 1) if i != 0)
    return GridPlan(
        center=center,
        step=step,
        levels=levels,
        lower_stop=center - atr * stop_atr,
        upper_stop=center + atr * stop_atr,
    )


def signals(plan: GridPlan, last_price: float, price: float) -> list[Signal]:
    """Levels crossed moving from last_price to price, in the order they were crossed."""
    if price <= plan.lower_stop or price >= plan.upper_stop:
        return [Signal("stop", price)]
    if price < last_price:
        crossed = [lv for lv in plan.levels if price <= lv < last_price]
        return [Signal("buy", lv) for lv in sorted(crossed, reverse=True)]
    if price > last_price:
        crossed = [lv for lv in plan.levels if last_price < lv <= price]
        return [Signal("sell", lv) for lv in sorted(crossed)]
    return []
