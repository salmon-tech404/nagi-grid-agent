import pytest

from nagi.executor import min_out, with_receipt


def test_min_out_applies_slippage_floor():
    assert min_out(10_000, 100) == 9_900
    assert min_out(10_000, 0) == 10_000
    assert min_out(999, 50) == 994  # rounds down: never accept less than the floor


def test_min_out_rejects_bad_slippage():
    with pytest.raises(ValueError):
        min_out(1, 10_000)
    with pytest.raises(ValueError):
        min_out(1, -1)


def test_with_receipt_appends_exactly_32_bytes():
    h = "0x" + "ab" * 32
    assert with_receipt("0x1234", h) == "0x1234" + "ab" * 32
    with pytest.raises(ValueError):
        with_receipt("0x1234", "0xabcd")
