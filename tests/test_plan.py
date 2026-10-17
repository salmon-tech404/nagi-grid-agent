import pytest

from nagi.plan import grid_plan


def test_geometric_bnb_case_from_pokter_check():
    p = grid_plan("BNB", 1000, 900, 1100, 8, "geometric")
    r = (1100 / 900) ** (1 / 8)
    assert len(p["price_levels"]) == 9 and p["price_levels"][0] == 900 and p["price_levels"][-1] == 1100
    assert p["ratio"] == pytest.approx(r)
    # every interval has the same percentage step
    lv = p["price_levels"]
    assert all(lv[i + 1] / lv[i] == pytest.approx(r, rel=1e-6) for i in range(8))
    assert p["per_grid_quote"] == 125
    first = p["orders"][0]
    assert first["base_quantity"] == pytest.approx(125 / 900)
    assert first["profit_per_cycle_pct"] == pytest.approx((r - 1) * 100, rel=1e-6)
    assert p["totals"]["quote_allocated"] == pytest.approx(1000)


def test_eth_and_cake_cases_from_pokter_check():
    eth = grid_plan("ETH", 2000, 1800, 2200, 4, "geometric")
    assert len(eth["orders"]) == 4 and eth["per_grid_quote"] == 500
    cake = grid_plan("CAKE", 500, 600, 900, 6, "geometric")
    assert cake["price_levels"][-1] == 900 and cake["ratio"] == pytest.approx(1.5 ** (1 / 6))


def test_arithmetic_spacing_has_equal_price_steps():
    p = grid_plan("BNB", 1000, 900, 1100, 4, "arithmetic")
    assert p["price_levels"] == [900, 950, 1000, 1050, 1100] and p["step_price"] == 50


@pytest.mark.parametrize("args", [(1000, 0, 1100, 8), (1000, 900, 900, 8), (1000, 900, 1100, 0), (0, 900, 1100, 8)])
def test_rejects_bad_inputs(args):
    with pytest.raises(ValueError):
        grid_plan("BNB", *args)
