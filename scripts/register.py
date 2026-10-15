"""Register Nagi on the ERC-8004 Identity Registry (one-time).

The agentId is minted to the wallet in PRIVATE_KEY, which must be the
campaign wallet. Run only after the server is reachable at A2A_BASE_URL.

Usage:
    uv run python scripts/register.py --dry-run   # print the registration file, send nothing
    uv run python scripts/register.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from bnbagent import AgentEndpoint, ERC8004Agent, EVMWalletProvider  # noqa: E402

CAMPAIGN_WALLET = "0x3ee8cfa277a612b5a2ec26c69001f545ad4e95ff"
CATEGORY = "grid-trading"
SKILLS = ["grid trading", "regime filter", "PancakeSwap", "ATR grid", "ERC-8183 jobs"]
DESCRIPTION = (
    "Nagi is a regime-aware grid trading agent on BNB Chain (category: grid trading). "
    "It runs an ATR-spaced buy-low/sell-high grid on PancakeSwap while the market is "
    "sideways and pauses the grid when the Kaufman efficiency ratio signals a trend. "
    "Hire it via ERC-8183 for one grid session; it trades from its own wallet and "
    "delivers a report with every trade's tx hash and decision receipt."
)


def _with_category(agent_uri: str) -> str:
    """Add category + skills to the SDK-generated registration file (base64 data URI)."""
    prefix, payload = agent_uri.split(",", 1)
    doc = json.loads(base64.b64decode(payload))
    doc["category"] = CATEGORY
    doc["skills"] = SKILLS
    return prefix + "," + base64.b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    private_key = os.getenv("PRIVATE_KEY")
    password = os.getenv("WALLET_PASSWORD")
    if not private_key or not password:
        sys.exit("PRIVATE_KEY and WALLET_PASSWORD are required (see .env.example)")
    base_url = os.getenv("A2A_BASE_URL", "")
    if not base_url.startswith("https://"):
        sys.exit("A2A_BASE_URL must be the public https URL of the running server")

    wallet = EVMWalletProvider(password=password, private_key=private_key, persist=False)
    if wallet.address.lower() != CAMPAIGN_WALLET:
        sys.exit(f"wallet {wallet.address} is not the campaign wallet {CAMPAIGN_WALLET}")

    network = os.getenv("NETWORK", "bsc-testnet")
    sdk = ERC8004Agent(wallet_provider=wallet, network=network)
    endpoint = AgentEndpoint.a2a(base_url, version="0.3.0")
    name = os.getenv("AGENT_NAME", "Nagi")
    agent_uri = _with_category(sdk.generate_agent_uri(name=name, description=DESCRIPTION, endpoints=[endpoint]))

    print(f"Registering {name} from {sdk.wallet_address} on {network}")
    print(json.dumps(json.loads(base64.b64decode(agent_uri.split(",", 1)[1])), indent=1))
    if dry_run:
        print("--dry-run: nothing sent")
        return

    result = sdk.register_agent(agent_uri, metadata=[{"key": "category", "value": CATEGORY}])
    print(f"  tx:       {result.get('transactionHash')}")
    print(f"  agent_id: {result.get('agentId')}")
    print("Save AGENT_ID to .env and put it in README.md.")


if __name__ == "__main__":
    main()
