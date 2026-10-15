import pytest

from nagi.perf import grid_pnl

Q = 2 * 10**15  # 0.002 BNB


def t(ts, action, price, wei=Q):
    return {"kind": "trade", "ts": ts, "action": action, "price_ref": price, "amount_bnb_wei": wei}


def test_buy_low_sell_high_is_a_realized_round_trip():
    p = grid_pnl([t(1, "buy", 725.74), t(2, "sell", 731.72)], price_now=730.0)
    assert p["round_trips"] == 1
    assert p["realized_usd"] == pytest.approx((731.72 - 725.74) * 0.002)
    assert p["unrealized_usd"] == 0 and p["open_bnb"] == 0


def test_sell_high_buy_back_low_also_earns():
    p = grid_pnl([t(1, "sell", 731.72), t(2, "buy", 725.74)], price_now=700.0)
    assert p["realized_usd"] == pytest.approx(5.98 * 0.002)


def test_open_lots_are_marked_to_current_price():
    p = grid_pnl([t(1, "buy", 725.0), t(2, "sell", 735.0, wei=8 * 10**15)], price_now=730.0)
    # buy 0.002 closed by part of the sell; 0.006 BNB sold at 735 is still open and 5 dollars in the money
    assert p["round_trips"] == 1
    assert p["realized_usd"] == pytest.approx(10 * 0.002)
    assert p["unrealized_usd"] == pytest.approx(5 * 0.006)
    assert p["vs_hold_usd"] == pytest.approx(0.02 + 0.03)
    assert p["open_bnb"] == pytest.approx(-0.006)


def test_ignores_non_trades_and_handles_no_price():
    p = grid_pnl([{"kind": "grid-open", "ts": 1}, t(2, "buy", 700.0)], price_now=None)
    assert p["trades"] == 1 and p["unrealized_usd"] == 0
