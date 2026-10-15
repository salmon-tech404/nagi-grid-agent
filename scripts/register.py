"""Register Nagi on the ERC-8004 Identity Registry (one-time).

The agentId is minted to the wallet in PRIVATE_KEY, which must be the
campaign wallet. Run only after the server is reachable at A2A_BASE_URL.

Usage:
    uv run python scripts/register.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from bnbagent import AgentEndpoint, ERC8004Agent, EVMWalletProvider  # noqa: E402

CAMPAIGN_WALLET = "0x3ee8cfa277a612b5a2ec26c69001f545ad4e95ff"


def main() -> None:
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
    description = (
        "Regime-aware grid trading agent (category: grid). Runs an ATR-spaced grid on BSC "
        "while the market is sideways and pauses when the efficiency ratio signals a trend."
    )
    print(f"Registering {name} from {sdk.wallet_address} on {network}")
    print(f"  A2A endpoint: {endpoint.endpoint}")
    result = sdk.register_agent(sdk.generate_agent_uri(name=name, description=description, endpoints=[endpoint]))
    print(f"  tx:       {result.get('transactionHash')}")
    print(f"  agent_id: {result.get('agentId')}")
    print("Save AGENT_ID to .env and put it in README.md.")


if __name__ == "__main__":
    main()
