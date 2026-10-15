import pandas as pd
import pytest

from nagi import grid, indicators as ind, regime
from nagi.receipt import canonical, receipt_hash


def _ohlc(closes):
    s = pd.Series(closes, dtype=float)
    return pd.DataFrame({"open": s, "high": s + 0.5, "low": s - 0.5, "close": s})


def test_efficiency_ratio_hand_values():
    rising = pd.Series([float(i) for i in range(1, 40)])
    assert ind.efficiency_ratio(rising, 10).iloc[-1] == pytest.approx(1.0)
    zigzag = pd.Series([10.0, 11.0] * 20)
    assert ind.efficiency_ratio(zigzag, 10).iloc[-1] == pytest.approx(0.0)


def test_regime_calm_when_sideways_and_storm_when_trending():
    calm = regime.classify(_ohlc([10.0, 11.0] * 20))
    assert calm.state == "calm" and calm.grid_allowed
    storm = regime.classify(_ohlc([float(i) for i in range(1, 40)]))
    assert storm.state == "storm" and not storm.grid_allowed


def test_regime_rejects_short_history():
    with pytest.raises(ValueError):
        regime.classify(_ohlc([1.0, 2.0, 3.0]))


def test_build_plan_levels_are_symmetric_and_exclude_center():
    plan = grid.build_plan(center=100.0, atr=4.0, levels_per_side=2, step_atr=0.5, stop_atr=3.0)
    assert plan.step == 2.0
    assert plan.levels == (96.0, 98.0, 102.0, 104.0)
    assert (plan.lower_stop, plan.upper_stop) == (88.0, 112.0)


def test_signals_buy_on_fall_sell_on_rise_in_crossing_order():
    plan = grid.build_plan(center=100.0, atr=4.0, levels_per_side=2)
    assert grid.signals(plan, 100.0, 95.5) == [grid.Signal("buy", 98.0), grid.Signal("buy", 96.0)]
    assert grid.signals(plan, 95.5, 103.0) == [
        grid.Signal("sell", 96.0),
        grid.Signal("sell", 98.0),
        grid.Signal("sell", 102.0),
    ]
    assert grid.signals(plan, 100.0, 101.0) == []
    assert grid.signals(plan, 100.0, 100.0) == []


def test_signals_stop_outside_band():
    plan = grid.build_plan(center=100.0, atr=4.0)
    assert grid.signals(plan, 100.0, 88.0) == [grid.Signal("stop", 88.0)]
    assert grid.signals(plan, 100.0, 112.5) == [grid.Signal("stop", 112.5)]


def test_build_plan_rejects_bad_inputs():
    with pytest.raises(ValueError):
        grid.build_plan(center=0.0, atr=1.0)
    with pytest.raises(ValueError):
        grid.build_plan(center=1.0, atr=1.0, levels_per_side=0)


def test_receipt_hash_is_key_order_independent():
    a = {"side": "buy", "level": 98.0, "er": 0.12}
    b = {"er": 0.12, "level": 98.0, "side": "buy"}
    assert canonical(a) == canonical(b)
    assert receipt_hash(a) == receipt_hash(b)
    assert receipt_hash(a) != receipt_hash({**a, "side": "sell"})
