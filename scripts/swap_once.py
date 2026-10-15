"""Execute one small receipt-tagged swap (BNB -> BUSD) to verify the executor end to end.

Usage:
    uv run python scripts/swap_once.py --dry-run   # quote + simulate, send nothing
    uv run python scripts/swap_once.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from bnbagent import EVMWalletProvider  # noqa: E402

from nagi import regime  # noqa: E402
from nagi.executor import BUSD, ROUTER, WBNB, Executor, min_out, with_receipt  # noqa: E402
from nagi.market import fetch_klines  # noqa: E402
from nagi.receipt import canonical, receipt_hash  # noqa: E402

AMOUNT_WEI = 10**15  # 0.001 tBNB


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    wallet = EVMWalletProvider(
        password=os.environ["WALLET_PASSWORD"], private_key=os.environ["PRIVATE_KEY"], persist=False
    )
    ex = Executor(wallet)
    r = regime.classify(asyncio.run(fetch_klines()))
    decision = {
        "agent": "Nagi",
        "action": "sell",
        "reason": "executor smoke test: " + r.reason,
        "regime": r.state,
        "er": round(r.er, 4),
        "price_ref": r.price,
        "amount_bnb_wei": AMOUNT_WEI,
        "ts": int(time.time()),
    }
    h = receipt_hash(decision)
    print("decision:", canonical(decision))
    print("receipt hash:", h)
    print("balances before:", ex.balances())
    quoted = ex.quote(AMOUNT_WEI, [WBNB, BUSD])
    print(f"quote: 0.001 BNB -> {quoted / 1e18:.6f} BUSD (min {min_out(quoted, ex.slippage_bps) / 1e18:.6f})")

    if dry_run:
        data = ex.router.encode_abi(
            "swapExactETHForTokens", args=[min_out(quoted, ex.slippage_bps), [WBNB, BUSD], ex.address, ex._deadline()]
        )
        gas = ex.w3.eth.estimate_gas(
            {"from": ex.address, "to": ROUTER, "data": with_receipt(data, h), "value": AMOUNT_WEI}
        )
        print(f"simulated OK with receipt appended, gas ~{gas}; --dry-run: nothing sent")
        return

    fill = ex.sell_bnb(AMOUNT_WEI, h)
    print("tx:", f"https://testnet.bscscan.com/tx/{fill.tx_hash}")
    print("balances after:", ex.balances())


if __name__ == "__main__":
    main()
