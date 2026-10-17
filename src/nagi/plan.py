"""Grid plan arithmetic: price levels and per-grid orders for a stated range and budget.

"N grids" means N intervals between N+1 price lines. Geometric spacing keeps the
same percentage step between lines (ratio r = (upper / lower) ** (1 / N));
arithmetic spacing keeps the same price step. The budget is split evenly across
the N grids; each grid buys at its lower line and sells at its upper line.
"""

from __future__ import annotations

from typing import Any

SPACINGS = ("geometric", "arithmetic")


def grid_plan(base: str, budget_usdt: float, lower: float, upper: float, grids: int,
              spacing: str = "geometric", quote: str = "USDT") -> dict[str, Any]:
    if not (lower > 0 and upper > lower):
        raise ValueError("range must satisfy 0 < lower < upper")
    if int(grids) != grids or grids < 1:
        raise ValueError("grids must be a positive integer")
    if budget_usdt <= 0:
        raise ValueError("budget must be positive")
    spacing = spacing.lower()
    if spacing not in SPACINGS:
        raise ValueError(f"spacing must be one of {SPACINGS}")
    grids = int(grids)

    if spacing == "geometric":
        ratio = (upper / lower) ** (1 / grids)
        levels = [lower * ratio**i for i in range(grids + 1)]
        step = {"ratio": round(ratio, 10), "step_pct": round((ratio - 1) * 100, 6)}
    else:
        delta = (upper - lower) / grids
        levels = [lower + delta * i for i in range(grids + 1)]
        step = {"step_price": round(delta, 10)}
    levels[-1] = upper  # remove floating drift on the last line

    per_grid = budget_usdt / grids
    orders = []
    for i in range(grids):
        buy, sell = levels[i], levels[i + 1]
        qty = per_grid / buy
        orders.append({
            "grid": i + 1,
            "buy_price": round(buy, 6),
            "sell_price": round(sell, 6),
            "quote_amount": round(per_grid, 6),
            "base_quantity": round(qty, 10),
            "profit_per_cycle": round(qty * (sell - buy), 6),
            "profit_per_cycle_pct": round((sell / buy - 1) * 100, 6),
        })

    return {
        "base": base,
        "quote": quote,
        "budget": budget_usdt,
        "range": {"lower": lower, "upper": upper},
        "grids": grids,
        "spacing": spacing,
        **step,
        "price_levels": [round(p, 6) for p in levels],
        "per_grid_quote": round(per_grid, 6),
        "orders": orders,
        "totals": {
            "quote_allocated": round(per_grid * grids, 6),
            "base_if_all_filled": round(sum(o["base_quantity"] for o in orders), 10),
            "profit_if_every_grid_cycles_once": round(sum(o["profit_per_cycle"] for o in orders), 6),
        },
        "assumptions": [
            f"{grids} grids = {grids} intervals between {grids + 1} price lines",
            "budget split evenly across grids in the quote currency",
            "each grid buys at its lower line and sells at its upper line",
            "fees, slippage and gas are not included",
        ],
    }
