"""Read-only grid assessment for a token pair: liquidity, suitability, a proposed grid, costs and risks.

Answers briefs like "Check available liquidity and explain whether a grid is suitable; if so propose a
price range, spacing and test budget, including fees, gas and key risks; state sources and timestamps".
Never trades. Pool data comes from the PancakeSwap V2 pair on BSC testnet; volatility and trend come from
the real BNB/USDT market when the pair contains WBNB.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from web3 import Web3

from nagi.executor import BUSD, ROUTER, WBNB

FEE = 0.0025  # PancakeSwap V2 swap fee
SWAP_GAS = 150_000  # measured 121k-147k for Nagi's router swaps
MIN_WBNB_RESERVE = 1.0  # below this, a few test orders move the pool price noticeably
MAX_IMPACT = 0.005  # keep each order's price impact under 0.5 %
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")

FACTORY_ABI = [{"name": "getPair", "type": "function", "stateMutability": "view",
                "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "address"}]}]
PAIR_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
]
ERC20_META = [
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
]
ROUTER_FACTORY_ABI = [{"name": "factory", "type": "function", "stateMutability": "view", "inputs": [],
                       "outputs": [{"type": "address"}]}]


def pair_from_brief(text: str) -> tuple[str, str]:
    """The two token addresses named in a brief, WBNB first when present; WBNB/BUSD if none are given."""
    found = []
    for a in ADDR_RE.findall(text or ""):
        a = Web3.to_checksum_address(a)
        if a not in found:
            found.append(a)
    if len(found) >= 2:
        a, b = found[:2]
        return (a, b) if a == WBNB or b != WBNB else (b, a)
    if len(found) == 1:
        return (WBNB, found[0]) if found[0] != WBNB else (WBNB, BUSD)
    return WBNB, BUSD


def plan_grid(pool_price: float, base_reserve: float, atr_pct: float, er: float) -> dict[str, Any]:
    """Pure maths: suitability verdict and a proposed grid from pool depth and market volatility."""
    max_order = base_reserve * MAX_IMPACT / (1 - MAX_IMPACT)  # base-token size that keeps impact under the cap
    step_pct = max(0.5 * atr_pct, 2.5 * FEE * 100)  # each step must clear a round-trip fee with room to spare
    half_range_pct = 3 * atr_pct
    grids = max(2, min(12, int(round(2 * half_range_pct / step_pct))))
    lower, upper = pool_price * (1 - half_range_pct / 100), pool_price * (1 + half_range_pct / 100)
    order = round(min(max_order, 0.002), 6)
    reasons = []
    if base_reserve < MIN_WBNB_RESERVE:
        reasons.append(f"thin pool: {base_reserve:.3f} base tokens in reserve (< {MIN_WBNB_RESERVE})")
    if er >= 0.30:
        reasons.append(f"market trending (efficiency ratio {er:.2f} >= 0.30): grids lose to holding in a trend")
    verdict = "suitable" if not reasons else ("suitable for a small test only" if er < 0.30 else "not suitable now")
    return {
        "verdict": verdict, "reasons": reasons,
        "range": {"lower": round(lower, 6), "upper": round(upper, 6)},
        "spacing": {"type": "geometric", "step_pct": round(step_pct, 4), "grids": grids},
        "order_size_base": order,
        "test_budget": {"base": round(order * grids / 2, 6), "quote": round(order * grids / 2 * pool_price, 6),
                        "note": "half the grids are buys funded in quote, half sells funded in base"},
        "price_impact_per_order_pct": round(order / (base_reserve + order) * 100, 4) if base_reserve else None,
        "round_trip_fee_pct": round(2 * FEE * 100, 4),
        "net_edge_per_round_trip_pct": round(step_pct - 2 * FEE * 100, 4),
    }


def assess_pair(w3: Web3, brief: str, regime=None, latest_candle_open: str | None = None) -> dict[str, Any]:
    """Read the pool on chain and produce the full assessment. `regime` is nagi.regime.Regime or None."""
    base, quote = pair_from_brief(brief)
    block = w3.eth.get_block("latest")
    at = datetime.fromtimestamp(block["timestamp"], timezone.utc).isoformat()
    sources = [f"BSC testnet block {block['number']} at {at}", f"PancakeSwap V2 router {ROUTER}"]
    factory = w3.eth.contract(address=ROUTER, abi=ROUTER_FACTORY_ABI).functions.factory().call()
    pair = w3.eth.contract(address=factory, abi=FACTORY_ABI).functions.getPair(base, quote).call()
    meta = {}
    for t in (base, quote):
        c = w3.eth.contract(address=t, abi=ERC20_META)
        try:
            meta[t] = (c.functions.symbol().call(), c.functions.decimals().call())
        except Exception:
            meta[t] = (t[:8], 18)
    pair_name = f"{meta[base][0]}/{meta[quote][0]}"
    if int(pair, 16) == 0:
        return {"pair": pair_name, "tokens": [base, quote], "supported": False,
                "limitation": "No PancakeSwap V2 pool exists for this pair on BSC testnet, so a grid cannot run here.",
                "sources": sources, "trades_executed": 0}
    pc = w3.eth.contract(address=pair, abi=PAIR_ABI)
    r0, r1, _ = pc.functions.getReserves().call()
    rb, rq = (r0, r1) if pc.functions.token0().call() == base else (r1, r0)
    base_res, quote_res = rb / 10 ** meta[base][1], rq / 10 ** meta[quote][1]
    pool_price = quote_res / base_res if base_res else 0.0
    sources.append(f"pair {pair} getReserves")
    limitations = []
    if regime is not None and base == WBNB:
        atr_pct, er = regime.atr / regime.price * 100, regime.er
        sources.append(f"Binance BNB/USDT 1h klines (ATR 14, efficiency ratio 10), latest candle opened "
                       f"{latest_candle_open or 'n/a'}")
        market = {"real_bnb_usdt": regime.price, "atr_pct_1h": round(atr_pct, 4), "efficiency_ratio": round(er, 4),
                  "regime": regime.state}
    else:
        atr_pct, er, market = 1.0, 0.0, None
        limitations.append("no real-market reference for this pair; volatility assumed at 1 % per hour")
    if base_res and regime is not None and base == WBNB and abs(pool_price / regime.price - 1) > 0.2:
        limitations.append(f"testnet pool price {pool_price:.2f} differs from the real market {regime.price:.2f}; "
                           "test tokens have no market, so prices and P&L here are not representative")
    gas_price = w3.eth.gas_price
    plan = plan_grid(pool_price, base_res, atr_pct, er)
    return {
        "pair": pair_name, "tokens": [base, quote], "supported": True, "pool": pair,
        "liquidity": {"base_reserve": round(base_res, 6), "quote_reserve": round(quote_res, 6),
                      "pool_price_quote_per_base": round(pool_price, 6)},
        "market": market, **plan,
        "costs": {"swap_fee_pct": FEE * 100, "gas_per_swap": SWAP_GAS, "gas_price_wei": gas_price,
                  "gas_cost_per_swap_bnb": round(SWAP_GAS * gas_price / 1e18, 8)},
        "risks": [
            "thin testnet liquidity: each order moves the pool price, and other users can move it further",
            "trend risk: in a one-way market a grid accumulates the losing side; pause when the efficiency ratio rises",
            "inventory risk: price leaving the range leaves the budget fully in one token",
            "slippage and failed swaps: use a minimum-output floor and short deadlines",
        ],
        "limitations": limitations, "sources": sources, "trades_executed": 0,
    }


def summary_text(a: dict[str, Any]) -> str:
    """A concise written report from an assessment dict."""
    if not a.get("supported"):
        return f"{a['pair']}: {a['limitation']} Sources: {'; '.join(a['sources'])}."
    liq, sp = a["liquidity"], a["spacing"]
    parts = [
        f"{a['pair']} on PancakeSwap V2 (BSC testnet) holds {liq['base_reserve']} and {liq['quote_reserve']} "
        f"(pool price {liq['pool_price_quote_per_base']}).",
        f"Verdict: {a['verdict']}" + (f" ({'; '.join(a['reasons'])})." if a["reasons"] else "."),
        f"Proposal: range {a['range']['lower']} to {a['range']['upper']}, {sp['grids']} geometric grids of "
        f"{sp['step_pct']} % each, order size {a['order_size_base']} per level, test budget "
        f"{a['test_budget']['base']} base + {a['test_budget']['quote']} quote.",
        f"Costs: {a['costs']['swap_fee_pct']} % fee per swap ({a['round_trip_fee_pct']} % per round trip, "
        f"net edge {a['net_edge_per_round_trip_pct']} % per cycle), about {a['costs']['gas_cost_per_swap_bnb']} "
        f"BNB gas per swap; price impact about {a['price_impact_per_order_pct']} % per order.",
        f"Key risks: {'; '.join(a['risks'][:3])}.",
    ]
    if a["limitations"]:
        parts.append(f"Limitations: {'; '.join(a['limitations'])}.")
    parts.append(f"Sources: {'; '.join(a['sources'])}. No trades were executed.")
    return " ".join(parts)
