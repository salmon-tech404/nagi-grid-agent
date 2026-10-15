import pandas as pd

from nagi.engine import GridEngine
from nagi.executor import Fill


class FakeExecutor:
    def __init__(self, bnb=10**18, busd=10**21):
        self.bal = {"bnb": bnb, "busd": busd}
        self.swaps = []

    def balances(self):
        return dict(self.bal)

    def quote(self, amount_in, path):
        return amount_in * 400

    def sell_bnb(self, amount_wei, receipt_hash):
        self.swaps.append(("sell", amount_wei))
        return Fill("sell", "0xsell", amount_wei, 0, receipt_hash)

    def buy_bnb(self, amount_busd, receipt_hash):
        self.swaps.append(("buy", amount_busd))
        return Fill("buy", "0xbuy", amount_busd, 0, receipt_hash)


def _sideways(last):
    """Zigzag history (ER ~ 0, ATR ~ 1.5) ending at `last`."""
    closes = [100.0, 101.0] * 20 + [last]
    s = pd.Series(closes)
    return pd.DataFrame({"open": s, "high": s + 0.5, "low": s - 0.5, "close": s})


def _trending():
    s = pd.Series([float(i) for i in range(100, 140)])
    return pd.DataFrame({"open": s, "high": s + 0.5, "low": s - 0.5, "close": s})


def test_first_calm_tick_opens_grid_without_trading():
    ex = FakeExecutor()
    eng = GridEngine(ex)
    events = eng.tick(_sideways(101.0))
    assert [e["kind"] for e in events] == ["grid-open"]
    assert eng.plan is not None and ex.swaps == []


def test_opening_without_busd_funds_the_buy_side_once():
    ex = FakeExecutor(busd=0)
    eng = GridEngine(ex)
    events = eng.tick(_sideways(101.0))
    assert [e["kind"] for e in events] == ["grid-open", "trade"]
    assert ex.swaps == [("sell", eng.order_bnb_wei * 4)]
    assert events[1]["amount_bnb_wei"] == eng.order_bnb_wei * 4


def test_session_with_busd_already_funded_takes_a_normal_entry():
    ex = FakeExecutor()  # plenty of BUSD: no setup trade, one ordinary entry sell
    events = GridEngine(ex).open_session(_sideways(101.0), job_id=8)
    assert [e["kind"] for e in events] == ["grid-open", "trade"] and ex.swaps == [("sell", 2 * 10**15)]


def test_price_falling_through_a_level_buys_with_receipt():
    ex = FakeExecutor()
    eng = GridEngine(ex)
    eng.tick(_sideways(101.0))
    lowest_buy = eng.plan.levels[0]
    events = eng.tick(_sideways(eng.plan.levels[3] - 0.01))  # just under the first level below centre
    trades = [e for e in events if e["kind"] == "trade"]
    assert trades and all(t["action"] == "buy" and t["receipt"].startswith("0x") for t in trades)
    assert ex.swaps[0][0] == "buy" and lowest_buy < eng.plan.center


def test_storm_pauses_once_and_drops_the_grid():
    ex = FakeExecutor()
    eng = GridEngine(ex)
    eng.tick(_sideways(101.0))
    first = eng.tick(_trending())
    second = eng.tick(_trending())
    assert [e["kind"] for e in first] == ["pause"]
    assert second == [] and eng.plan is None and ex.swaps == []


def test_sell_respects_gas_reserve():
    ex = FakeExecutor(bnb=GridEngine(None).gas_reserve_wei)  # nothing above the reserve
    eng = GridEngine(ex)
    events = eng.open_session(_sideways(101.0), job_id=7)
    assert events[-1]["kind"] == "skip" and ex.swaps == []


def test_session_entry_trades_when_calm_and_stands_still_in_storm():
    ex = FakeExecutor()
    eng = GridEngine(ex)
    calm = eng.open_session(_sideways(101.0), job_id=1)
    assert [e["kind"] for e in calm] == ["grid-open", "trade"] and calm[-1]["job_id"] == 1
    storm = GridEngine(FakeExecutor()).open_session(_trending(), job_id=2)
    assert [e["kind"] for e in storm] == ["session-standby"]


def test_read_only_session_never_trades():
    ex = FakeExecutor()
    events = GridEngine(ex).open_session(_sideways(101.0), job_id=4, read_only=True)
    assert [e["kind"] for e in events] == ["grid-open", "session-read-only"] and ex.swaps == []


def test_swap_failure_is_logged_not_raised():
    ex = FakeExecutor()

    def boom(*_):
        raise RuntimeError("rpc down")

    ex.sell_bnb = boom
    eng = GridEngine(ex)
    events = eng.open_session(_sideways(101.0), job_id=3)
    assert events[-1]["kind"] == "error" and "rpc down" in events[-1]["note"]
