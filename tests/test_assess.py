import pytest

from nagi.assess import FEE, pair_from_brief, plan_grid, summary_text
from nagi.executor import BUSD, WBNB

U = "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565"
JOB_1564 = ("Provide a read-only assessment of a WBNB/U spot grid strategy on BNB Smart Chain Testnet (chain 97).\n"
            "WBNB: 0xae13d989dac2f0debff460ac112a837c89baa7cd\nU: 0xc70b8741b8b07a6d61e54fd4b20f22fa648e5565\n"
            "Check available liquidity and explain whether a grid is suitable.")


def test_pair_comes_from_the_addresses_in_the_brief_wbnb_first():
    assert pair_from_brief(JOB_1564) == (WBNB, U)
    assert pair_from_brief(f"pair {U} and {WBNB.lower()}") == (WBNB, U)
    assert pair_from_brief("no addresses here") == (WBNB, BUSD)


def test_thin_pool_in_a_calm_market_is_a_small_test_only():
    p = plan_grid(pool_price=220.46, base_reserve=0.74, atr_pct=0.7, er=0.1)
    assert p["verdict"] == "suitable for a small test only"
    assert p["price_impact_per_order_pct"] < 0.5  # order sized to the impact cap
    assert p["range"]["lower"] < 220.46 < p["range"]["upper"]


def test_step_always_clears_the_round_trip_fee():
    p = plan_grid(pool_price=440, base_reserve=12, atr_pct=0.1, er=0.05)  # very quiet market
    assert p["spacing"]["step_pct"] >= 2.5 * FEE * 100
    assert p["net_edge_per_round_trip_pct"] > 0 and p["verdict"] == "suitable"


def test_trending_market_is_not_suitable_now():
    p = plan_grid(pool_price=440, base_reserve=12, atr_pct=0.8, er=0.55)
    assert p["verdict"] == "not suitable now" and any("trending" in r for r in p["reasons"])


def test_summary_reports_unsupported_pairs_plainly():
    text = summary_text({"supported": False, "pair": "AAA/BBB", "limitation": "No pool exists.",
                         "sources": ["block 1"]})
    assert "No pool exists." in text and "block 1" in text


def test_summary_states_fees_risks_sources_and_no_trades():
    a = plan_grid(220.46, 0.74, 0.7, 0.1)
    a.update({"supported": True, "pair": "WBNB/U", "liquidity": {"base_reserve": 0.74, "quote_reserve": 163.1,
              "pool_price_quote_per_base": 220.46}, "costs": {"swap_fee_pct": 0.25, "gas_cost_per_swap_bnb": 1.5e-05},
              "risks": ["r1", "r2", "r3"], "limitations": ["testnet prices"], "sources": ["block 137978736"]})
    text = summary_text(a)
    for piece in ("WBNB/U", "0.25 %", "r1", "testnet prices", "block 137978736", "No trades were executed"):
        assert piece in text
