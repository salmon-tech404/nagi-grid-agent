"""Grid engine: one tick = real BNB price -> regime gate -> grid signals -> receipt-tagged swaps.

Decisions follow the real market (Binance klines); fills happen on the
PancakeSwap testnet pool, whose own price barely moves. Every decision, traded
or not, lands in an in-memory log that job reports and /activity read from.

The engine is synchronous (web3 calls block); callers run it in a thread and
serialize it with every other onchain write so nonces never collide.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import pandas as pd

from nagi import grid, regime
from nagi.executor import BUSD, WBNB
from nagi.receipt import receipt_hash

ORDER_BNB_WEI = 2 * 10**15  # 0.002 tBNB per grid rung
GAS_RESERVE_WEI = 5 * 10**16  # never trade below 0.05 tBNB, it pays for gas


class GridEngine:
    def __init__(self, executor, order_bnb_wei: int = ORDER_BNB_WEI, gas_reserve_wei: int = GAS_RESERVE_WEI,
                 history: int = 500):
        self.ex = executor
        self.order_bnb_wei = order_bnb_wei
        self.gas_reserve_wei = gas_reserve_wei
        self.plan: grid.GridPlan | None = None
        self.last_price: float | None = None
        self.state: str | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=history)

    def tick(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        r = regime.classify(df)
        if not r.grid_allowed:
            events = [] if self.state == "storm" else [self._record("pause", r)]
            self.state, self.plan, self.last_price = "storm", None, r.price
            return events
        self.state = "calm"
        if self.plan is None:
            return [self._open(r)]
        events = []
        for sig in grid.signals(self.plan, self.last_price, r.price):
            if sig.side == "stop":
                events.append(self._record("grid-stop", r, note="price left the ±3 ATR band; re-centre next tick"))
                self.plan = None
                break
            events.append(self._trade(sig.side, r, level=sig.level))
        self.last_price = r.price
        return events

    def open_session(self, df: pd.DataFrame, job_id: int) -> list[dict[str, Any]]:
        """Re-centre the grid for a hire; take an entry trade only if the market is calm."""
        r = regime.classify(df)
        if not r.grid_allowed:
            self.state, self.plan, self.last_price = "storm", None, r.price
            return [self._record("session-standby", r, job_id=job_id,
                                 note="hired during a trend: Nagi stands still until the market calms")]
        self.state = "calm"
        events = [self._open(r, job_id=job_id)]
        events.append(self._trade("sell", r, level=r.price, job_id=job_id, note="session entry"))
        return events

    def _open(self, r: regime.Regime, **extra) -> dict[str, Any]:
        self.plan = grid.build_plan(r.price, r.atr)
        self.last_price = r.price
        return self._record("grid-open", r, levels=[round(lv, 2) for lv in self.plan.levels], **extra)

    def _trade(self, side: str, r: regime.Regime, level: float, **extra) -> dict[str, Any]:
        decision = {
            "agent": "Nagi", "action": side, "level": round(level, 2), "price_ref": r.price,
            "regime": r.state, "er": round(r.er, 4), "atr": round(r.atr, 4), "ts": int(time.time()), **extra,
        }
        h = receipt_hash(decision)
        try:
            bal = self.ex.balances()
            if side == "sell":
                if bal["bnb"] < self.order_bnb_wei + self.gas_reserve_wei:
                    return self._log({**decision, "kind": "skip", "receipt": h, "note": "BNB at gas reserve"})
                fill = self.ex.sell_bnb(self.order_bnb_wei, h)
            else:
                busd_in = self.ex.quote(self.order_bnb_wei, [WBNB, BUSD])
                if bal["busd"] < busd_in:
                    return self._log({**decision, "kind": "skip", "receipt": h, "note": "not enough BUSD to buy"})
                fill = self.ex.buy_bnb(busd_in, h)
        except Exception as exc:  # a failed swap must not kill the loop
            return self._log({**decision, "kind": "error", "receipt": h, "note": str(exc)[:200]})
        return self._log({**decision, "kind": "trade", "receipt": h, "tx": fill.tx_hash,
                          "amount_in": str(fill.amount_in), "min_out": str(fill.min_out)})

    def _record(self, kind: str, r: regime.Regime, **extra) -> dict[str, Any]:
        return self._log({"kind": kind, "regime": r.state, "er": round(r.er, 4), "atr": round(r.atr, 4),
                          "price_ref": r.price, "reason": r.reason, "ts": int(time.time()), **extra})

    def _log(self, event: dict[str, Any]) -> dict[str, Any]:
        self.log.append(event)
        return event

    def events_between(self, start: int, end: int) -> list[dict[str, Any]]:
        return [e for e in self.log if start <= e["ts"] <= end]
