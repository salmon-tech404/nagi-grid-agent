"""Paper P&L of the grid at real BNB prices, measured against simply holding BNB.

Every trade is a lot. A later trade on the other side closes the oldest open
lots first (FIFO): sell at S after buy at B, or buy back at B after sell at S,
both earn (S - B) x qty. That is exactly the gain over holding, because holding
would have earned nothing from those swings. Lots still open are marked to the
current price.

Fills happen on the PancakeSwap testnet pool at a different price, so this
measures the strategy on the real market, not the testnet wallet value.
"""

from __future__ import annotations

from collections import deque
from typing import Any


def grid_pnl(events: list[dict[str, Any]], price_now: float | None) -> dict[str, Any]:
    trades = sorted((e for e in events if e.get("kind") == "trade"), key=lambda e: e["ts"])
    open_lots: dict[str, deque[list[float]]] = {"buy": deque(), "sell": deque()}  # [price, qty]
    realized = 0.0
    round_trips = 0
    for t in trades:
        side, price = t["action"], float(t["price_ref"])
        qty = int(t.get("amount_bnb_wei") or 0) / 1e18
        other = open_lots["sell" if side == "buy" else "buy"]
        while qty > 1e-12 and other:
            lot = other[0]
            take = min(qty, lot[1])
            sell_px, buy_px = (lot[0], price) if side == "buy" else (price, lot[0])
            realized += (sell_px - buy_px) * take
            round_trips += 1  # every close counts, including a partial close of a bigger lot
            lot[1] -= take
            qty -= take
            if lot[1] <= 1e-12:
                other.popleft()
        if qty > 1e-12:
            open_lots[side].append([price, qty])

    unrealized = 0.0
    if price_now is not None:
        unrealized += sum((price_now - p) * q for p, q in open_lots["buy"])
        unrealized += sum((p - price_now) * q for p, q in open_lots["sell"])
    return {
        "trades": len(trades),
        "buys": sum(t["action"] == "buy" for t in trades),
        "sells": sum(t["action"] == "sell" for t in trades),
        "round_trips": round_trips,
        "realized_usd": round(realized, 6),
        "unrealized_usd": round(unrealized, 6),
        "vs_hold_usd": round(realized + unrealized, 6),
        "open_bnb": round(sum(q for _, q in open_lots["buy"]) - sum(q for _, q in open_lots["sell"]), 6),
    }
