"""Nagi's history read back from chain, so the dashboard survives restarts.

- Trades: BUSD Transfer logs into / out of Nagi's wallet (one per swap), plus the
  swap tx for the BNB side and the decision receipt appended to its calldata.
- Earnings: payment-token (U) transfers from the ERC-8183 escrow into the wallet.
- Prices: the real BNB/USDT 1-minute close at each trade's block time (Binance),
  so the paper P&L can be rebuilt after a restart.

The default BSC testnet RPC refuses eth_getLogs, so logs come from a public
node that serves them in bounded block ranges. Synchronous: run it in a thread.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from nagi.executor import BUSD, ROUTER, ROUTER_ABI, WBNB

logger = logging.getLogger("nagi.history")

# PublicNode serves eth_getLogs but only for roughly the last two days; older ranges come back empty.
# Set LOGS_RPC_URL to an archive endpoint (e.g. your own free NodeReal BSC testnet key) for full history.
LOGS_RPC = os.getenv("LOGS_RPC_URL") or "https://bsc-testnet-rpc.publicnode.com"
START_BLOCK = 136_935_000  # just before Nagi's first swap (15 Oct 2026)
TRANSFER = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")
KLINE_URL = "https://data-api.binance.vision/api/v3/klines"
SEED_FILE = Path(__file__).resolve().parent / "seed_history.json"


def _topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


def _addr(topic) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    return Web3.to_checksum_address("0x" + raw.removeprefix("0x")[-40:])


def split_receipt(calldata: bytes, router) -> str | None:
    """Return the 32-byte receipt hash appended after a router call's ABI arguments, if any."""
    if len(calldata) < 36 + 32 or (len(calldata) - 4) % 32:
        return None
    body = calldata[:-32]
    try:
        fn, args = router.decode_function_input(body)
        if bytes.fromhex(router.encode_abi(fn.fn_name, args=list(args.values())).removeprefix("0x")) != body:
            return None
    except Exception:
        return None
    return "0x" + calldata[-32:].hex()


class ChainHistory:
    def __init__(self, wallet: str, commerce: str, payment_token: str, rpc_url: str = LOGS_RPC,
                 start_block: int = START_BLOCK, chunk: int = 10_000):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.router = self.w3.eth.contract(address=ROUTER, abi=ROUTER_ABI)
        self.me = Web3.to_checksum_address(wallet)
        self.commerce = Web3.to_checksum_address(commerce)
        self.token = Web3.to_checksum_address(payment_token)
        self.next_block = start_block
        self.chunk = chunk
        self.start_block = start_block
        self.rescan_every = 6 * 3600
        self._last_full_scan = 0.0
        self.trades: dict[str, dict[str, Any]] = {}
        self.earnings: dict[str, dict[str, Any]] = {}
        self._ts_cache: dict[int, int] = {}
        self._load_seed(start_block)

    def _load_seed(self, default_start: int) -> None:
        if SEED_FILE.is_file():
            try:
                with open(SEED_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.trades.update(data.get("trades", {}))
                self.earnings.update(data.get("earnings", {}))
                seed_block = data.get("start_block")
                if seed_block and default_start == START_BLOCK:
                    self.start_block = seed_block
                    self.next_block = seed_block
                logger.info("loaded %s seeded trades, %s earnings; resume scan from block %s",
                            len(self.trades), len(self.earnings), self.next_block)
            except Exception as exc:
                logger.warning("failed to load seed history from %s: %s", SEED_FILE, exc)

    def refresh(self) -> None:
        # Public nodes occasionally return an empty log list for a range that does hold logs, which
        # would hide a trade forever. Re-walk the whole history now and then; known txs are skipped.
        if time.time() - self._last_full_scan > self.rescan_every:
            self.next_block = self.start_block
            self._last_full_scan = time.time()
        head = self.w3.eth.block_number
        while self.next_block <= head:
            end = min(self.next_block + self.chunk - 1, head)
            span = {"fromBlock": self.next_block, "toBlock": end}
            try:
                busd_out = self.w3.eth.get_logs({**span, "address": BUSD, "topics": [TRANSFER, _topic(self.me)]})
                busd_in = self.w3.eth.get_logs({**span, "address": BUSD, "topics": [TRANSFER, None, _topic(self.me)]})
                paid = self.w3.eth.get_logs({**span, "address": self.token,
                                             "topics": [TRANSFER, _topic(self.commerce), _topic(self.me)]})
                for log in busd_in:
                    self._add_trade(log, "sell")
                for log in busd_out:
                    self._add_trade(log, "buy")
                for log in paid:
                    tx = "0x" + log["transactionHash"].hex().removeprefix("0x")
                    if tx in self.earnings:
                        continue
                    try:
                        self.earnings[tx] = {"tx": tx, "amount_u": int(log["data"].hex() or "0", 16) / 1e18,
                                             "ts": self._block_ts(log["blockNumber"])}
                    except Exception as err:
                        logger.warning("failed to index earning %s: %s", tx, err)
            except Exception as exc:
                logger.warning("log scan for block range %s-%s encountered error: %s", self.next_block, end, exc)
                break
            self.next_block = end + 1

    def _add_trade(self, log, side: str) -> None:
        tx_hash = "0x" + log["transactionHash"].hex().removeprefix("0x")
        if tx_hash in self.trades:
            return
        try:
            tx = self.w3.eth.get_transaction(tx_hash)
            if Web3.to_checksum_address(tx["to"] or "0x" + "0" * 40) != ROUTER:
                return  # a plain BUSD transfer, not a swap
            busd = int(log["data"].hex() or "0", 16)
            if side == "sell":
                bnb = tx["value"]
            else:  # the pair sends WBNB to the router, which unwraps it to Nagi
                rc = self.w3.eth.get_transaction_receipt(tx_hash)
                bnb = sum(int(l["data"].hex() or "0", 16) for l in rc["logs"]
                          if Web3.to_checksum_address(l["address"]) == WBNB and len(l["topics"]) == 3
                          and _addr(l["topics"][2]) == ROUTER)
            ts = self._block_ts(log["blockNumber"])
            self.trades[tx_hash] = {
                "tx": tx_hash, "ts": ts, "action": side, "amount_bnb_wei": bnb, "busd": busd / 1e18,
                "receipt": split_receipt(bytes(tx["input"]), self.router), "price_ref": real_price_at(ts),
                "kind": "trade",
            }
        except Exception as exc:
            logger.warning("failed to fetch trade details for %s: %s", tx_hash, exc)

    def _block_ts(self, number: int) -> int:
        if number not in self._ts_cache:
            self._ts_cache[number] = int(self.w3.eth.get_block(number)["timestamp"])
        return self._ts_cache[number]

    def trade_list(self) -> list[dict[str, Any]]:
        return sorted(self.trades.values(), key=lambda t: t["ts"])

    def earned_u(self) -> float:
        return round(sum(e["amount_u"] for e in self.earnings.values()), 6)

    def active_days(self) -> list[str]:
        return sorted({datetime.fromtimestamp(t["ts"], timezone.utc).strftime("%Y-%m-%d") for t in self.trades.values()})


def real_price_at(ts: int) -> float | None:
    """Real BNB/USDT close of the 1-minute candle containing ts."""
    try:
        r = httpx.get(KLINE_URL, params={"symbol": "BNBUSDT", "interval": "1m", "startTime": (ts - ts % 60) * 1000,
                                         "limit": 1}, timeout=10)
        r.raise_for_status()
        rows = r.json()
        return float(rows[0][4]) if rows else None
    except Exception as exc:
        logger.warning("price lookup at %s failed: %s", ts, exc)
        return None
