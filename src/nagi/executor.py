"""PancakeSwap V2 swaps on BSC testnet between BNB and BUSD.

Each swap carries its decision receipt: the 32-byte keccak256 of the decision
JSON is appended to the router calldata. The router's ABI decoder ignores
trailing bytes, so the swap behaves normally while anyone can read the receipt
hash from the transaction input on BscScan.

Grid sides are about BNB: "buy" spends BUSD to get BNB, "sell" spends BNB to get BUSD.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from web3 import Web3

RPC_URL = "https://data-seed-prebsc-1-s1.bnbchain.org:8545"
ROUTER = Web3.to_checksum_address("0xD99D1c33F9fC3444f8101754aBC46c52416550D1")
WBNB = Web3.to_checksum_address("0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd")
BUSD = Web3.to_checksum_address("0x78867BbEeF44f2326bF8DDd1941a4439382EF2A7")

ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactETHForTokens", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"}, {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]


def min_out(quoted: int, slippage_bps: int) -> int:
    """Lowest acceptable output for a quote, given a slippage tolerance in basis points."""
    if not 0 <= slippage_bps < 10_000:
        raise ValueError("slippage_bps must be in [0, 10000)")
    return quoted * (10_000 - slippage_bps) // 10_000


def with_receipt(calldata: str, receipt_hash: str) -> str:
    """Append a 32-byte receipt hash to ABI-encoded calldata."""
    tail = receipt_hash.removeprefix("0x")
    if len(tail) != 64:
        raise ValueError("receipt_hash must be 32 bytes of hex")
    return calldata + tail


@dataclass(frozen=True)
class Fill:
    side: str  # "buy" | "sell" (of BNB)
    tx_hash: str
    amount_in: int
    min_out: int
    receipt_hash: str


class Executor:
    def __init__(self, wallet, rpc_url: str = RPC_URL, slippage_bps: int = 100):
        self.wallet = wallet
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.router = self.w3.eth.contract(address=ROUTER, abi=ROUTER_ABI)
        self.busd = self.w3.eth.contract(address=BUSD, abi=ERC20_ABI)
        self.slippage_bps = slippage_bps

    @property
    def address(self) -> str:
        return Web3.to_checksum_address(self.wallet.address)

    def balances(self) -> dict[str, int]:
        return {
            "bnb": self.w3.eth.get_balance(self.address),
            "busd": self.busd.functions.balanceOf(self.address).call(),
        }

    def quote(self, amount_in: int, path: list[str]) -> int:
        return self.router.functions.getAmountsOut(amount_in, path).call()[-1]

    def sell_bnb(self, amount_wei: int, receipt_hash: str) -> Fill:
        path = [WBNB, BUSD]
        floor = min_out(self.quote(amount_wei, path), self.slippage_bps)
        data = self.router.encode_abi(
            "swapExactETHForTokens", args=[floor, path, self.address, self._deadline()]
        )
        tx_hash = self._send(ROUTER, with_receipt(data, receipt_hash), value=amount_wei)
        return Fill("sell", tx_hash, amount_wei, floor, receipt_hash)

    def buy_bnb(self, amount_busd: int, receipt_hash: str) -> Fill:
        path = [BUSD, WBNB]
        self._ensure_allowance(amount_busd)
        floor = min_out(self.quote(amount_busd, path), self.slippage_bps)
        data = self.router.encode_abi(
            "swapExactTokensForETH", args=[amount_busd, floor, path, self.address, self._deadline()]
        )
        tx_hash = self._send(ROUTER, with_receipt(data, receipt_hash))
        return Fill("buy", tx_hash, amount_busd, floor, receipt_hash)

    def _ensure_allowance(self, amount: int) -> None:
        if self.busd.functions.allowance(self.address, ROUTER).call() >= amount:
            return
        # Exact-amount approval: never leave an open-ended allowance behind.
        self._send(BUSD, self.busd.encode_abi("approve", args=[ROUTER, amount]))

    def _deadline(self) -> int:
        return int(time.time()) + 300

    def _send(self, to: str, data: str, value: int = 0) -> str:
        tx = {
            "chainId": self.w3.eth.chain_id,
            "from": self.address,
            "to": to,
            "data": data,
            "value": value,
            "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
            "gasPrice": self.w3.eth.gas_price,
        }
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        signed = self.wallet.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed["rawTransaction"])
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"transaction reverted: {tx_hash.hex()}")
        return "0x" + tx_hash.hex().removeprefix("0x")
