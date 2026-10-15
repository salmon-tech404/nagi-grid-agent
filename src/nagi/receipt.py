"""Decision receipts: every grid decision is a canonical JSON whose keccak256 goes onchain."""

from __future__ import annotations

import json
from typing import Any

from web3 import Web3


def canonical(decision: dict[str, Any]) -> str:
    return json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt_hash(decision: dict[str, Any]) -> str:
    return Web3.keccak(text=canonical(decision)).hex()
